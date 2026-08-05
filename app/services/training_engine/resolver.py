"""DB-access layer for the Training Engine — turns the pure rules' data needs
into queries. Keeps ALL ORM access out of rules.py so the rules stay unit-
testable. Role-scoping (spec §B.3) lives here: requiring_worker_ids resolves the
authoritative "whose role requires this competency" set the rules can never
exceed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competency_matrix import (
    CompetencyRecord,
    PersonRoleAssignment,
    RoleCompetencyRequirement,
)
from app.models.training_engine import HazardToSkillMapping, TrainingAssignment
from app.services.training_engine.classify import build_classification_light, mapping_matches
from app.services.training_engine.rules import RecordDueRef

_OPEN_ASSIGNMENT_STATES = ("assigned", "in_progress", "overdue", "escalated")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── mapping resolution (classification blob → competencies) ──────────────────
async def resolve_competencies(
    db: AsyncSession, *, source_module: str, plant_id: str | None, classification: dict
) -> list[dict]:
    """Active HazardToSkillMappings that match this record's classification.
    Returns [{competencyId, mappingId, priority}], de-duped by competency,
    priority-ordered. plantId NULL mappings are global; a plant-specific mapping
    also applies only to its plant."""
    rows = (
        await db.execute(
            select(HazardToSkillMapping)
            .where(HazardToSkillMapping.isActive.is_(True))
            .where(HazardToSkillMapping.isDeleted.is_(False))
        )
    ).scalars().all()
    out: list[dict] = []
    seen: set[str] = set()
    for m in rows:
        if m.sourceModule not in (source_module, "ANY"):
            continue
        if m.plantId is not None and m.plantId != plant_id:
            continue
        if not mapping_matches(m.classificationField, m.classificationValue, m.matchMode, classification):
            continue
        if m.competencyId in seen:
            continue
        seen.add(m.competencyId)
        out.append({"competencyId": m.competencyId, "mappingId": m.id, "priority": m.priority})
    out.sort(key=lambda x: x["priority"])
    return out


async def mappings_for_competency(
    db: AsyncSession, *, competency_id: str, plant_id: str | None
) -> list[HazardToSkillMapping]:
    rows = (
        await db.execute(
            select(HazardToSkillMapping)
            .where(HazardToSkillMapping.competencyId == competency_id)
            .where(HazardToSkillMapping.isActive.is_(True))
            .where(HazardToSkillMapping.isDeleted.is_(False))
        )
    ).scalars().all()
    return [m for m in rows if m.plantId is None or m.plantId == plant_id]


# ── role-scoping (the authoritative "requires this competency" worker set) ───
async def requiring_worker_ids(
    db: AsyncSession, *, competency_id: str, plant_id: str, department_id: str | None = None
) -> list[str]:
    """Workers at this plant whose ROLE requires the competency (spec §B.3).

    Primary path: PersonRoleAssignment → RoleDefinition → RoleCompetencyRequirement.
    Supplementary: anyone already holding a CompetencyRecord for it at this plant
    (holding the record means the competency is part of their role). Both are
    scoped sets — NEVER the full site roster. Empty result → the caller flags
    scoping_failed for manual review.
    """
    ids: set[str] = set()

    req_role_defs = (
        await db.execute(
            select(RoleCompetencyRequirement.roleDefinitionId).where(
                RoleCompetencyRequirement.competencyId == competency_id
            )
        )
    ).scalars().all()
    if req_role_defs:
        pra = (
            await db.execute(
                select(PersonRoleAssignment.personUserId)
                .where(PersonRoleAssignment.plantId == plant_id)
                .where(PersonRoleAssignment.roleDefinitionId.in_(list(req_role_defs)))
                .where(PersonRoleAssignment.status == "active")
            )
        ).scalars().all()
        ids.update(pra)

    holders = (
        await db.execute(
            select(CompetencyRecord.personUserId)
            .where(CompetencyRecord.plantId == plant_id)
            .where(CompetencyRecord.competencyId == competency_id)
        )
    ).scalars().all()
    ids.update(holders)

    if department_id and ids:
        from app.models.user import User

        scoped = (
            await db.execute(
                select(User.id).where(User.id.in_(list(ids))).where(User.department == department_id)
            )
        ).scalars().all()
        if scoped:  # only narrow when the department actually matches someone
            ids = set(scoped)

    return list(ids)


# ── threshold counting ───────────────────────────────────────────────────────
def _load_stmt_for_module(module: str, plant_id: str, since: datetime, department_id: str | None):
    """Build the SELECT for a module's records in the window, dept-scoped when the
    model carries departmentId (Observation does not)."""
    if module == "OBSERVATION":
        from app.models.observation import Observation

        model = Observation
    elif module == "NEAR_MISS":
        from app.models.near_miss import NearMiss

        model = NearMiss
    elif module == "INCIDENT":
        from app.models.incident import Incident

        model = Incident
    else:
        return None, None
    stmt = select(model).where(model.plantId == plant_id).where(model.createdAt >= since)
    if hasattr(model, "isDeleted"):
        stmt = stmt.where(model.isDeleted.is_(False))
    if department_id and hasattr(model, "departmentId"):
        stmt = stmt.where(model.departmentId == department_id)
    return stmt, model


async def count_mapped_records(
    db: AsyncSession,
    *,
    competency_id: str,
    plant_id: str,
    department_id: str | None,
    window_days: int,
) -> int:
    """Count distinct Incident/NearMiss/Observation records at the site (+dept)
    within the window whose classification matches ANY active mapping for this
    competency — the threshold-rule input."""
    mappings = await mappings_for_competency(db, competency_id=competency_id, plant_id=plant_id)
    if not mappings:
        return 0
    modules = {m.sourceModule for m in mappings}
    if "ANY" in modules:
        modules = {"INCIDENT", "NEAR_MISS", "OBSERVATION"}
    since = now_utc() - timedelta(days=window_days)
    count = 0
    for mod in modules:
        stmt, _model = _load_stmt_for_module(mod, plant_id, since, department_id)
        if stmt is None:
            continue
        records = (await db.execute(stmt)).scalars().all()
        applicable = [m for m in mappings if m.sourceModule in (mod, "ANY")]
        for r in records:
            cls = build_classification_light(mod, r)
            if any(
                mapping_matches(m.classificationField, m.classificationValue, m.matchMode, cls)
                for m in applicable
            ):
                count += 1
    return count


# ── recert scan ───────────────────────────────────────────────────────────────
async def records_due_for_recert(db: AsyncSession, *, window_days: int, plant_ids: list[str] | None) -> list[RecordDueRef]:
    """CompetencyRecords whose validUntil / nextRevalidationDue falls inside the
    window (from now forward). plant_ids None = all plants (system scan)."""
    now = now_utc()
    horizon = now + timedelta(days=window_days)
    stmt = select(CompetencyRecord).where(
        or_(
            and_(CompetencyRecord.validUntil.is_not(None), CompetencyRecord.validUntil <= horizon, CompetencyRecord.validUntil >= now),
            and_(CompetencyRecord.nextRevalidationDue.is_not(None), CompetencyRecord.nextRevalidationDue <= horizon, CompetencyRecord.nextRevalidationDue >= now),
        )
    )
    if plant_ids is not None:
        stmt = stmt.where(CompetencyRecord.plantId.in_(plant_ids))
    rows = (await db.execute(stmt)).scalars().all()
    return [
        RecordDueRef(
            personUserId=r.personUserId,
            competencyId=r.competencyId,
            plantId=r.plantId,
            dueDate=r.validUntil or r.nextRevalidationDue,
        )
        for r in rows
    ]


# ── dedupe ─────────────────────────────────────────────────────────────────────
async def open_assignment_persons(db: AsyncSession, *, competency_id: str, person_ids: list[str]) -> set[str]:
    if not person_ids:
        return set()
    rows = (
        await db.execute(
            select(TrainingAssignment.personUserId)
            .where(TrainingAssignment.competencyId == competency_id)
            .where(TrainingAssignment.personUserId.in_(person_ids))
            .where(TrainingAssignment.status.in_(_OPEN_ASSIGNMENT_STATES))
            .where(TrainingAssignment.isDeleted.is_(False))
        )
    ).scalars().all()
    return set(rows)


async def open_assignment_pairs(db: AsyncSession, *, pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """Which (personUserId, competencyId) already have an open assignment — the
    recert-rule dedupe (avoids re-assigning a refresher every scan)."""
    if not pairs:
        return set()
    person_ids = list({p for p, _c in pairs})
    comp_ids = list({c for _p, c in pairs})
    rows = (
        await db.execute(
            select(TrainingAssignment.personUserId, TrainingAssignment.competencyId)
            .where(TrainingAssignment.personUserId.in_(person_ids))
            .where(TrainingAssignment.competencyId.in_(comp_ids))
            .where(TrainingAssignment.status.in_(_OPEN_ASSIGNMENT_STATES))
            .where(TrainingAssignment.isDeleted.is_(False))
        )
    ).all()
    have = {(p, c) for p, c in rows}
    return {pair for pair in pairs if pair in have}


__all__ = [
    "now_utc",
    "resolve_competencies",
    "mappings_for_competency",
    "requiring_worker_ids",
    "count_mapped_records",
    "records_due_for_recert",
    "open_assignment_persons",
    "open_assignment_pairs",
]
