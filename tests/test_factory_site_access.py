"""Auto-granting a freshly provisioned Site.

Provisioning a Plant is only half of "the factory exists". Every plant-scoped
surface — the audit Owning-site dropdown above all — resolves sites through
`UserRole(scopeType='PLANT')`, and a brand-new Plant is on nobody's list. The
failure this pins down is the quiet one: the factory is created, the Facilities
dashboard shows it, and the Owning-site dropdown one click later does not.

Run against SQLite: `grant_site_access` is plain SELECT/INSERT over Plant and
UserRole, so the dialect is irrelevant to what is being asserted.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import select

from app.models._base import Base
from app.models.plant import Plant
from app.models.user import Role, User, UserRole
from app.services.factory import grant_site_access


def _plant(code: str) -> Plant:
    return Plant(code=code, name=f"Site {code}", location="—", state="—", unitType="Factory")


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    # Only the four tables this logic touches. `create_all` over the full
    # metadata pulls in Postgres ARRAY/JSONB columns from unrelated models,
    # which SQLite cannot render.
    tables = [Base.metadata.tables[t] for t in ("Plant", "User", "Role", "UserRole")]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def estate(db: AsyncSession):
    """Three existing sites, and the three kinds of user that matter:

      hse      — PLANT grants on every existing site (the platform's way of
                 spelling "covers the whole estate"; the RBAC seed writes one
                 row per plant rather than an ALL_PLANTS scope)
      local    — a PLANT grant on ONE site, a deliberate subset
      creator  — the person adding the factory
    """
    role = Role(code="HSE_MANAGER", name="HSE Manager", isActive=True)
    db.add(role)
    olds = [_plant("A"), _plant("B"), _plant("C")]
    users = {
        k: User(email=f"{k}@x.com", name=k, role="HSE_MANAGER", passwordHash="x")
        for k in ("hse", "local", "creator")
    }
    db.add_all(olds + list(users.values()))
    await db.flush()

    for p in olds:
        db.add(UserRole(userId=users["hse"].id, roleId=role.id, scopeType="PLANT", scopeValue=p.id))
    db.add(UserRole(userId=users["local"].id, roleId=role.id, scopeType="PLANT", scopeValue=olds[0].id))
    db.add(UserRole(userId=users["creator"].id, roleId=role.id, scopeType=None, scopeValue=None))
    await db.flush()
    return {"role": role, "olds": olds, "users": users}


async def _granted(db: AsyncSession, plant_id: str) -> set[str]:
    rows = (
        await db.execute(
            select(UserRole.userId).where(
                UserRole.scopeType == "PLANT", UserRole.scopeValue == plant_id
            )
        )
    ).all()
    return {r[0] for r in rows}


@pytest.mark.asyncio
async def test_creator_and_estate_wide_users_get_the_new_site(db, estate):
    new = _plant("NEW")
    db.add(new)
    await db.flush()

    written = await grant_site_access(db, plant_id=new.id, created_by=estate["users"]["creator"].id)

    got = await _granted(db, new.id)
    assert estate["users"]["creator"].id in got, "the person who added the factory must see it"
    assert estate["users"]["hse"].id in got, "whole-estate coverage must not develop a hole"
    assert estate["users"]["local"].id not in got, "a one-site grant is a subset, not an oversight"
    assert written == 2


@pytest.mark.asyncio
async def test_is_idempotent(db, estate):
    """The repair script and the create path can both touch the same site."""
    new = _plant("NEW")
    db.add(new)
    await db.flush()
    creator = estate["users"]["creator"].id

    first = await grant_site_access(db, plant_id=new.id, created_by=creator)
    second = await grant_site_access(db, plant_id=new.id, created_by=creator)

    assert first == 2 and second == 0
    assert len(await _granted(db, new.id)) == 2


@pytest.mark.asyncio
async def test_sequential_provisioning_keeps_estate_coverage(db, estate):
    """Two factories added back to back.

    The second call measures "covers the whole estate" against a set that now
    includes the first new site — so the grant written moments earlier is what
    keeps the estate-wide user qualifying. If the first grant were not flushed,
    the second site would silently skip them.
    """
    creator = estate["users"]["creator"].id
    ids = []
    for code in ("NEW1", "NEW2"):
        p = _plant(code)
        db.add(p)
        await db.flush()
        await grant_site_access(db, plant_id=p.id, created_by=creator)
        ids.append(p.id)

    for pid in ids:
        got = await _granted(db, pid)
        assert estate["users"]["hse"].id in got
        assert creator in got


@pytest.mark.asyncio
async def test_first_ever_site_grants_the_creator(db):
    """No pre-existing sites: nobody can "cover the estate", but the creator
    must still get the site they just made."""
    role = Role(code="ADMIN", name="Admin", isActive=True)
    u = User(email="a@x.com", name="a", role="ADMIN", passwordHash="x")
    db.add_all([role, u])
    await db.flush()
    db.add(UserRole(userId=u.id, roleId=role.id))
    p = _plant("FIRST")
    db.add(p)
    await db.flush()

    written = await grant_site_access(db, plant_id=p.id, created_by=u.id)

    assert written == 1
    assert await _granted(db, p.id) == {u.id}
