"""Person-risk analytics — the repeat-involvement flag (the "smarter analytics").

The site/hazard threshold rule trains a crew when a hazard recurs at a location.
THIS is the person-centric flow the platform was missing: it aggregates every
incident / near miss / observation logged *against a person's name*, scores their
risk deterministically, and — when they cross the configurable threshold — raises
a WorkerTrainingFlag AND auto-assigns the training their events point to (reusing
the assignment engine). Events → PERSON risk → training.

Attribution (spec: incidents + near misses + observations, not mere witnesses):
  • Incident      — IncidentPerson.userId, role NOT in {WITNESS, RESPONDER}
  • Near miss     — NearMissPersonInvolved / NearMissPersonAffected.userId
  • Observation   — Observation.responsiblePersonId (the person the unsafe
                    act/condition is attributed to; the only person field the
                    Observation schema carries)

Deterministic + airgap-safe: plain counting + weighted scoring, no model calls.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competency_matrix import Competency
from app.models.training_engine import TrainingAssignment, WorkerTrainingFlag
from app.services.training_engine import service
from app.services.training_engine.classify import build_classification_light, infer_sif
from app.services.training_engine.config import resolve_config
from app.services.training_engine.resolver import now_utc, resolve_competencies
from app.services.training_engine.rules import (
    AssignmentDraft,
    PersonEventStat,
    RuleConfigView,
    event_weight,
    person_involvement_rule,
)

# Roles that do NOT count an incident "against a person's name".
_EXCLUDED_INCIDENT_ROLES = {"WITNESS", "RESPONDER"}
_OPEN_ASSIGNMENT_STATES = ("assigned", "in_progress", "overdue", "escalated")


def _apply_event(st: PersonEventStat, module: str, cls: dict, *, ref: str | None, rec_id: str, date, role: str | None, sif: bool) -> None:
    sev = cls.get("severity")
    if module == "INCIDENT":
        st.incidentCount += 1
    elif module == "NEAR_MISS":
        st.nearMissCount += 1
    else:
        st.observationCount += 1
    if sif:
        st.sifCount += 1
    st.severityWeight += event_weight(module, sev, sif=sif)
    st.contributing.append(
        {
            "module": module,
            "id": rec_id,
            "ref": ref,
            "date": date.isoformat() if hasattr(date, "isoformat") and date else None,
            "role": role,
            "severity": sev,
            "sif": sif,
            "_cls": cls,  # kept in-memory for competency inference; stripped before persist
        }
    )


async def aggregate_person_events(
    db: AsyncSession, *, plant_ids: list[str] | None, window_days: int, person_id: str | None = None
) -> dict[str, PersonEventStat]:
    """Build per-person event stats over the window. plant_ids None = all plants
    (system scan); person_id set = restrict to one worker (detail view)."""
    from app.models.incident import Incident, IncidentPerson
    from app.models.near_miss import NearMiss
    from app.models.near_miss_children import NearMissPersonAffected, NearMissPersonInvolved
    from app.models.observation import Observation

    since = now_utc() - timedelta(days=window_days)
    stats: dict[str, PersonEventStat] = {}

    def _stat(uid: str, plant: str) -> PersonEventStat:
        return stats.setdefault(uid, PersonEventStat(personUserId=uid, plantId=plant))

    # ── Incidents ──
    inc_stmt = select(Incident).where(Incident.createdAt >= since)
    if hasattr(Incident, "isDeleted"):
        inc_stmt = inc_stmt.where(Incident.isDeleted.is_(False))
    if plant_ids is not None:
        inc_stmt = inc_stmt.where(Incident.plantId.in_(plant_ids))
    incidents = (await db.execute(inc_stmt)).scalars().all()
    inc_by_id = {i.id: i for i in incidents}
    if inc_by_id:
        pstmt = select(IncidentPerson).where(IncidentPerson.incidentId.in_(list(inc_by_id)))
        if person_id:
            pstmt = pstmt.where(IncidentPerson.userId == person_id)
        for p in (await db.execute(pstmt)).scalars().all():
            if not p.userId or (p.role or "").upper() in _EXCLUDED_INCIDENT_ROLES:
                continue
            i = inc_by_id.get(p.incidentId)
            if i is None:
                continue
            cls = build_classification_light("INCIDENT", i)
            sif = infer_sif("INCIDENT", {**cls, "injuryFatal": (p.injurySeverity or "").upper() == "FATAL"})
            _apply_event(_stat(p.userId, i.plantId), "INCIDENT", cls, ref=i.number, rec_id=i.id, date=i.createdAt, role=p.role, sif=sif)

    # ── Near misses ──
    nm_stmt = select(NearMiss).where(NearMiss.createdAt >= since)
    if hasattr(NearMiss, "isDeleted"):
        nm_stmt = nm_stmt.where(NearMiss.isDeleted.is_(False))
    if plant_ids is not None:
        nm_stmt = nm_stmt.where(NearMiss.plantId.in_(plant_ids))
    nms = (await db.execute(nm_stmt)).scalars().all()
    nm_by_id = {n.id: n for n in nms}
    if nm_by_id:
        inv_stmt = select(NearMissPersonInvolved).where(NearMissPersonInvolved.nearMissId.in_(list(nm_by_id)))
        aff_stmt = select(NearMissPersonAffected).where(NearMissPersonAffected.nearMissId.in_(list(nm_by_id)))
        if person_id:
            inv_stmt = inv_stmt.where(NearMissPersonInvolved.userId == person_id)
            aff_stmt = aff_stmt.where(NearMissPersonAffected.userId == person_id)
        rows = [*(await db.execute(inv_stmt)).scalars().all(), *(await db.execute(aff_stmt)).scalars().all()]
        seen: set[tuple[str, str]] = set()
        for p in rows:
            if not p.userId or (p.userId, p.nearMissId) in seen:
                continue
            seen.add((p.userId, p.nearMissId))
            n = nm_by_id.get(p.nearMissId)
            if n is None:
                continue
            cls = build_classification_light("NEAR_MISS", n)
            sif = infer_sif("NEAR_MISS", cls)
            _apply_event(_stat(p.userId, n.plantId), "NEAR_MISS", cls, ref=n.number, rec_id=n.id, date=n.createdAt, role="involved", sif=sif)

    # ── Observations (attributed via responsiblePersonId) ──
    obs_stmt = select(Observation).where(Observation.createdAt >= since)
    if hasattr(Observation, "isDeleted"):
        obs_stmt = obs_stmt.where(Observation.isDeleted.is_(False))
    if plant_ids is not None:
        obs_stmt = obs_stmt.where(Observation.plantId.in_(plant_ids))
    for o in (await db.execute(obs_stmt)).scalars().all():
        uid = getattr(o, "responsiblePersonId", None)
        if not uid or (person_id and uid != person_id):
            continue
        cls = build_classification_light("OBSERVATION", o)
        sif = infer_sif("OBSERVATION", cls)
        _apply_event(_stat(uid, o.plantId), "OBSERVATION", cls, ref=o.number, rec_id=o.id, date=o.createdAt, role="responsible", sif=sif)

    return stats


async def _infer_competencies(db: AsyncSession, st: PersonEventStat) -> tuple[list[str], list[dict]]:
    """Which competencies this person's events point to (via HazardToSkillMapping)."""
    counts: dict[str, int] = {}
    for ev in st.contributing:
        comps = await resolve_competencies(
            db, source_module=ev["module"], plant_id=st.plantId, classification=ev.get("_cls") or {}
        )
        for c in comps:
            counts[c["competencyId"]] = counts.get(c["competencyId"], 0) + 1
    if not counts:
        return [], []
    names: dict[str, str] = {}
    rows = (await db.execute(select(Competency).where(Competency.id.in_(list(counts))))).scalars().all()
    names = {c.id: c.name for c in rows}
    recommended = [
        {"competencyId": cid, "name": names.get(cid, cid), "fromEvents": cnt}
        for cid, cnt in sorted(counts.items(), key=lambda x: -x[1])
    ]
    return list(counts.keys()), recommended


def _persistable_contributing(contributing: list[dict]) -> list[dict]:
    """Strip the in-memory classification blob before persisting to JSON."""
    return [{k: v for k, v in ev.items() if k != "_cls"} for ev in contributing]


async def _assign_flag_training(db: AsyncSession, flag: WorkerTrainingFlag, comp_ids: list[str], config: RuleConfigView) -> list[str]:
    """Auto-assign the flagged person the training their events map to, reusing
    the assignment engine. High/critical bands are mandatory."""
    mandatory = flag.riskBand in ("high", "critical")
    drafts = [
        AssignmentDraft(
            personUserId=flag.personUserId,
            competencyId=cid,
            source="person_risk",
            plantId=flag.plantId,
            provenance={
                "ruleType": "person_risk",
                "riskBand": flag.riskBand,
                "riskScore": flag.riskScore,
                "totalEvents": flag.totalEvents,
                "flagId": flag.id,
            },
            isMandatory=mandatory,
            dismissible=not mandatory,
            escalationFlag=flag.riskBand == "critical",
            dueOffsetDays=config.assignmentDueDays,
        )
        for cid in comp_ids
    ]
    await service.create_assignments(db, drafts, [], config=config)
    ids = (
        await db.execute(
            select(TrainingAssignment.id)
            .where(TrainingAssignment.personUserId == flag.personUserId)
            .where(TrainingAssignment.competencyId.in_(comp_ids))
            .where(TrainingAssignment.status.in_(_OPEN_ASSIGNMENT_STATES))
            .where(TrainingAssignment.isDeleted.is_(False))
        )
    ).scalars().all()
    return list(ids)


async def _upsert_flag(db: AsyncSession, st: PersonEventStat, result, comp_ids, recommended) -> WorkerTrainingFlag:
    now = now_utc()
    existing = (
        await db.execute(select(WorkerTrainingFlag).where(WorkerTrainingFlag.personUserId == st.personUserId))
    ).scalar_one_or_none()
    contributing = _persistable_contributing(st.contributing)
    if existing is None:
        flag = WorkerTrainingFlag(
            plantId=st.plantId,
            personUserId=st.personUserId,
            riskScore=result.riskScore,
            riskBand=result.riskBand,
            windowDays=st.contributing and 365 or 365,
            incidentCount=st.incidentCount,
            nearMissCount=st.nearMissCount,
            observationCount=st.observationCount,
            sifCount=st.sifCount,
            totalEvents=result.totalEvents,
            contributingRecords=contributing,
            recommendedCompetencies=recommended,
            mappedCompetencyIds=comp_ids,
            # NOT NULL in the DB (Prisma String[] defaults to []). A freshly
            # flagged worker has no assignments yet — they're written back at
            # run_person_risk_scan/assign_now AFTER this insert — so seed [] here
            # rather than let it flush as NULL (asyncpg NotNullViolationError).
            assignmentIds=[],
            status="flagged",
            flaggedAt=now,
            lastEvaluatedAt=now,
        )
        db.add(flag)
        await db.flush()
        return flag
    # update in place; a cleared flag re-flags when the person recurs
    if existing.status == "cleared":
        existing.status = "flagged"
        existing.flaggedAt = now
    existing.plantId = st.plantId
    existing.riskScore = result.riskScore
    existing.riskBand = result.riskBand
    existing.incidentCount = st.incidentCount
    existing.nearMissCount = st.nearMissCount
    existing.observationCount = st.observationCount
    existing.sifCount = st.sifCount
    existing.totalEvents = result.totalEvents
    existing.contributingRecords = contributing
    existing.recommendedCompetencies = recommended
    existing.mappedCompetencyIds = comp_ids
    existing.lastEvaluatedAt = now
    return existing


async def run_person_risk_scan(db: AsyncSession, *, plant_ids: list[str] | None = None, auto_assign: bool = True) -> dict:
    """Scheduler job body: aggregate every person's events, flag those over the
    threshold, and auto-assign the training their events point to."""
    config = await resolve_config(db, None)
    stats = await aggregate_person_events(db, plant_ids=plant_ids, window_days=config.personFlagWindowDays)
    flagged = assigned = 0
    for uid, st in stats.items():
        result = person_involvement_rule(stats=st, config=config)
        if not result.flagged:
            continue
        comp_ids, recommended = await _infer_competencies(db, st)
        flag = await _upsert_flag(db, st, result, comp_ids, recommended)
        flag.windowDays = config.personFlagWindowDays
        flagged += 1
        if auto_assign and comp_ids:
            ids = await _assign_flag_training(db, flag, comp_ids, config)
            if ids:
                flag.assignmentIds = ids
                flag.status = "training_assigned"
                assigned += 1
    await db.commit()
    return {"evaluated": len(stats), "flagged": flagged, "assigned": assigned}


async def compute_person_detail(db: AsyncSession, person_id: str) -> dict:
    """Live per-person risk view for the detail screen (recomputes from events,
    independent of the last scan), merged with any persisted flag actions."""
    config = await resolve_config(db, None)
    stats = await aggregate_person_events(db, plant_ids=None, window_days=config.personFlagWindowDays, person_id=person_id)
    st = stats.get(person_id) or PersonEventStat(personUserId=person_id, plantId="")
    result = person_involvement_rule(stats=st, config=config)
    _comp_ids, recommended = await _infer_competencies(db, st)
    existing = (
        await db.execute(select(WorkerTrainingFlag).where(WorkerTrainingFlag.personUserId == person_id))
    ).scalar_one_or_none()
    return {
        "personUserId": person_id,
        "windowDays": config.personFlagWindowDays,
        "flagged": result.flagged,
        "riskScore": result.riskScore,
        "riskBand": result.riskBand,
        "reasons": result.reasons,
        "counts": {
            "incident": st.incidentCount,
            "nearMiss": st.nearMissCount,
            "observation": st.observationCount,
            "sif": st.sifCount,
            "total": result.totalEvents,
        },
        "contributingRecords": _persistable_contributing(st.contributing),
        "recommendedCompetencies": recommended,
        "flag": None
        if existing is None
        else {
            "id": existing.id,
            "status": existing.status,
            "assignmentIds": existing.assignmentIds or [],
            "acknowledgedBy": existing.acknowledgedBy,
            "clearedBy": existing.clearedBy,
            "clearReason": existing.clearReason,
        },
    }


async def assign_now(db: AsyncSession, person_id: str, *, actor_id: str | None = None) -> dict:
    """On-demand: recompute one person's risk and assign the mapped training
    (from the worker-risk detail screen). Idempotent via the engine's dedupe."""
    config = await resolve_config(db, None)
    stats = await aggregate_person_events(db, plant_ids=None, window_days=config.personFlagWindowDays, person_id=person_id)
    st = stats.get(person_id)
    if st is None:
        return {"assigned": 0, "reason": "no events attributed to this person"}
    result = person_involvement_rule(stats=st, config=config)
    comp_ids, recommended = await _infer_competencies(db, st)
    if not comp_ids:
        return {"assigned": 0, "reason": "no competency mapping matches this person's events"}
    flag = await _upsert_flag(db, st, result, comp_ids, recommended)
    flag.windowDays = config.personFlagWindowDays
    ids = await _assign_flag_training(db, flag, comp_ids, config)
    if ids:
        flag.assignmentIds = ids
        flag.status = "training_assigned"
    await db.commit()
    return {"assigned": len(ids), "flagId": flag.id, "competencies": comp_ids}


__all__ = ["run_person_risk_scan", "aggregate_person_events", "compute_person_detail", "assign_now"]
