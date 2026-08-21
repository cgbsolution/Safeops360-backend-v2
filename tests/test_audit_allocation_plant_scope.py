"""Checkpoint allocation must use RBAC reach, not the assignee's home plant.

The regression this pins down was visible on screen as "Allocation failed —
Owner belongs to a different plant", on an audit whose entire team had been
seated through the platform's own pickers.

`audit_assignment` decides who may hold an audit seat, and documents itself as
having deliberately dropped the `User.plantId == plantId` test: RBAC grants
plant reach two other ways — an ALL_PLANTS scope, and a PLANT-scoped UserRole
on a site that is not your home site. Both are ordinary. A site auditor is
routinely given a neighbouring unit precisely so audits stay independent.

`allocate_checkpoints` had kept the dropped test. So the picker offered someone,
the team screen seated them, and per-checkpoint allocation then refused them
with a message that was not even true. These tests assert the two paths now
answer the same question the same way.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models._base import Base
from app.models.plant import Plant
from app.models.user import Permission, Role, RolePermission, User, UserRole
from app.services import audit_assignment as A

EXECUTE = "AUDIT_COMPLIANCE.EXECUTE"  # lead / co-auditor — conducts checkpoints
UPDATE = "AUDIT_COMPLIANCE.UPDATE"    # auditee — responds to findings


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Base.metadata.tables[t]
        for t in ("Plant", "User", "Role", "Permission", "RolePermission", "UserRole")
    ]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def world(db: AsyncSession):
    """Two sites. `home` is where everyone lives; `away` is the audited site."""
    home = Plant(code="NW", name="North", location="—", state="—", unitType="Factory")
    away = Plant(code="AXM", name="Bengaluru", location="—", state="—", unitType="Factory")
    perms = {c: Permission(code=c, module="AUDIT_COMPLIANCE", action=c.split(".")[1])
             for c in (EXECUTE, UPDATE)}
    db.add_all([home, away, *perms.values()])
    await db.flush()
    return {"home": home, "away": away, "perms": perms}


async def _person(db, world, *, name, code, scope, granted_plants):
    """A user whose home plant is `home`, holding `code` at `scope`, with
    PLANT-scoped role rows on `granted_plants`."""
    role = Role(code=f"R_{name}", name=name, isActive=True)
    u = User(email=f"{name}@x.com", name=name, role=name, passwordHash="x",
             plantId=world["home"].id)
    db.add_all([role, u])
    await db.flush()
    db.add(RolePermission(roleId=role.id, permissionId=world["perms"][code].id, scope=scope))
    if granted_plants:
        for p in granted_plants:
            db.add(UserRole(userId=u.id, roleId=role.id, scopeType="PLANT", scopeValue=p.id))
    else:
        db.add(UserRole(userId=u.id, roleId=role.id))
    await db.flush()
    return u


async def _allows(db, plant, slot, user) -> bool:
    try:
        await A.assert_assignable(db, plant_id=plant.id, assignments={slot: [user.id]})
        return True
    except ValueError:
        return False


@pytest.mark.asyncio
async def test_plant_scoped_role_on_another_site_is_allowed(db, world):
    """The exact shape that produced the bug: home plant NW, audit at AXM,
    an explicit PLANT-scoped grant on AXM. The old `plantId` test rejected
    this person; RBAC says they may act there."""
    auditor = await _person(db, world, name="SiteAuditor", code=EXECUTE,
                            scope="OWN_PLANT", granted_plants=[world["away"]])

    assert await _allows(db, world["away"], "coAuditor", auditor)


@pytest.mark.asyncio
async def test_all_plants_scope_needs_no_grant_at_all(db, world):
    """A corporate auditor covers every site, including ones they hold no
    explicit row for."""
    corporate = await _person(db, world, name="Corporate", code=EXECUTE,
                              scope="ALL_PLANTS", granted_plants=None)

    assert await _allows(db, world["away"], "coAuditor", corporate)


@pytest.mark.asyncio
async def test_no_reach_to_that_site_is_still_refused(db, world):
    """The guard must still bite. Same home plant, no grant on the audited
    site — this person genuinely may not act there."""
    outsider = await _person(db, world, name="Outsider", code=EXECUTE,
                             scope="OWN_PLANT", granted_plants=[world["home"]])

    assert not await _allows(db, world["away"], "coAuditor", outsider)


@pytest.mark.asyncio
async def test_slots_are_judged_on_their_own_permission(db, world):
    """Reach at the site is not a blanket pass. An auditee holds UPDATE, so
    they may answer findings there but may not conduct checkpoints — the
    distinction the single plant test could never express."""
    auditee = await _person(db, world, name="Auditee", code=UPDATE,
                            scope="OWN_PLANT", granted_plants=[world["away"]])

    assert await _allows(db, world["away"], "auditee", auditee)
    assert not await _allows(db, world["away"], "coAuditor", auditee)


@pytest.mark.asyncio
async def test_refusal_names_the_missing_permission(db, world):
    """The old message ("belongs to a different plant") was false as often as
    not, and told nobody what to fix."""
    outsider = await _person(db, world, name="Outsider", code=EXECUTE,
                             scope="OWN_PLANT", granted_plants=[world["home"]])

    with pytest.raises(ValueError, match=EXECUTE):
        await A.assert_assignable(
            db, plant_id=world["away"].id, assignments={"coAuditor": [outsider.id]}
        )
