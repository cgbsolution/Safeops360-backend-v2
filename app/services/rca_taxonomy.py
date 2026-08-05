"""Two-layer RCA cause taxonomy — query + validation helpers.

Enterprise categories (~7) are common to ALL domains; sub-causes are domain-scoped
leaves that each roll up to exactly ONE category. That single rule is what lets a
category light up across multiple domains in the rollup (RCA-T07).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.rca import RootCauseCategory, RootCauseSubCause


async def list_categories(db: AsyncSession, *, active_only: bool = False) -> list[RootCauseCategory]:
    stmt = (
        select(RootCauseCategory)
        .where(RootCauseCategory.isDeleted.is_(False))
        .options(selectinload(RootCauseCategory.subCauses))
        .order_by(RootCauseCategory.displayOrder, RootCauseCategory.code)
    )
    if active_only:
        stmt = stmt.where(RootCauseCategory.isActive.is_(True))
    return list((await db.execute(stmt)).scalars().all())


def filter_subcauses_by_domain(rows: list, domain: str | None) -> list:
    """Pure domain filter (testable without a DB): a sub-cause with an empty
    applicableDomains is universal; otherwise it must list the domain. So a
    FINANCIAL RCA never sees LOTO/isolation (RCA-T06)."""
    if not domain:
        return list(rows)
    return [s for s in rows if not s.applicableDomains or domain in (s.applicableDomains or [])]


async def subcauses_for_domain(db: AsyncSession, domain: str | None) -> list[RootCauseSubCause]:
    """Sub-causes selectable for a domain. Filtering by JSON-array membership is
    done in Python so it works identically on Postgres + SQLite (tests)."""
    rows = list(
        (
            await db.execute(
                select(RootCauseSubCause)
                .where(RootCauseSubCause.isDeleted.is_(False))
                .where(RootCauseSubCause.isActive.is_(True))
                .order_by(RootCauseSubCause.code)
            )
        ).scalars().all()
    )
    return filter_subcauses_by_domain(rows, domain)


async def validate_subcause_parent(db: AsyncSession, category_id: str) -> RootCauseCategory:
    """Every sub-cause must map to exactly one existing enterprise category
    (RCA-T05). Returns the category or raises ValueError."""
    if not category_id:
        raise ValueError("A sub-cause must reference exactly one enterprise category.")
    cat = await db.get(RootCauseCategory, category_id)
    if cat is None or cat.isDeleted:
        raise ValueError("Parent enterprise category not found.")
    return cat


async def get_subcause(db: AsyncSession, sub_cause_id: str) -> RootCauseSubCause | None:
    return await db.get(RootCauseSubCause, sub_cause_id)
