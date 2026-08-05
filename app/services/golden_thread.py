"""Feature 7 — Golden-thread propagation engine.

One closed incident ripples into Risk Register, Training, Audit and CAPA — and
every ripple is recorded in `GoldenThreadLink` so an incident's whole downstream
impact is queryable in one place ("show me everything this incident touched").

Each propagation function is independent and failure-isolated by the caller
(`incident_post_closure` wraps each in a SAVEPOINT), so a training-detector
failure never blocks the risk-register update. Reopening an incident walks the
links back (`reverse_for_incident`).

This is the real Feature 7 layer on top of the Feature 5 risk hook that already
existed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import Incident, IncidentCapa, IncidentPerson
from app.models.incident_intel import CompetencyMapping, GoldenThreadLink


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def link(
    db: AsyncSession,
    *,
    incident_id: str,
    target_type: str,
    target_id: str,
    target_ref: str | None = None,
    link_type: str = "created",
    triggered_by: str = "system",
    meta: dict | None = None,
) -> GoldenThreadLink | None:
    """Record a traceability link (idempotent per source+targetType+targetId
    while not reversed). Returns the row, or None if it already existed."""
    existing = (
        await db.execute(
            select(GoldenThreadLink)
            .where(GoldenThreadLink.sourceIncidentId == incident_id)
            .where(GoldenThreadLink.targetType == target_type)
            .where(GoldenThreadLink.targetId == target_id)
            .where(GoldenThreadLink.reversedAt.is_(None))
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None
    row = GoldenThreadLink(
        sourceIncidentId=incident_id,
        targetType=target_type,
        targetId=target_id,
        targetRef=target_ref,
        linkType=link_type,
        triggeredBy=triggered_by,
        meta=meta,
    )
    db.add(row)
    await db.flush()
    return row


def _root_cause_blob(incident: Incident) -> str:
    parts = list(incident.rootCauses or [])
    parts += [incident.rootCauseSummary or "", incident.rootCauseDetail or ""]
    return " ".join(p for p in parts if p).lower()


# ─── Propagation functions (called from post-closure, each SAVEPOINT-wrapped) ──

async def propagate_risk(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """Record the F5 risk-register data-point push as a golden-thread link."""
    detail = incident.severityDetail or {}
    risk_id = detail.get("linkedRiskRegisterId")
    if not risk_id:
        return {"target": "risk_register", "created": 0, "reason": "no linked risk"}
    created = await link(
        db, incident_id=incident.id, target_type="risk_register", target_id=risk_id,
        link_type="updated",
        meta={"likelihood": detail.get("likelihoodOfRecurrence"), "consequence": detail.get("consequenceScore"), "score": detail.get("score")},
    )
    return {"target": "risk_register", "created": 1 if created else 0}


async def propagate_capa(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """Every CAPA raised on the incident (esp. via Feature 1's linkedCauseId)
    gets a traceability link so it surfaces on the incident's downstream panel."""
    rows = (
        await db.execute(select(IncidentCapa).where(IncidentCapa.incidentId == incident.id))
    ).scalars().all()
    created = 0
    for c in rows:
        made = await link(
            db, incident_id=incident.id, target_type="capa", target_id=c.id,
            target_ref=c.capaNumber, link_type="created",
            meta={"linkedCauseId": c.linkedCauseId, "ownerId": c.ownerId,
                  "targetDate": c.targetDate.isoformat() if c.targetDate else None, "status": c.status},
        )
        if made:
            created += 1
    return {"target": "capa", "created": created, "total": len(rows)}


async def propagate_training(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """If a root cause matches a competency mapping and an involved operator
    lacks that competency, create a training assignment (TrainingRegistration,
    registrationType=TRIGGERED) against an open schedule for a related program."""
    from app.models.competency_matrix import Competency, CompetencyRecord
    from app.models.training import TrainingRegistration, TrainingSchedule

    blob = _root_cause_blob(incident)
    if not blob:
        return {"target": "training_assignment", "created": 0, "reason": "no root cause text"}

    mappings = (
        await db.execute(select(CompetencyMapping).where(CompetencyMapping.active.is_(True)))
    ).scalars().all()
    matched_competency_ids = {m.competencyId for m in mappings if m.causeKeyword.lower() in blob}
    if not matched_competency_ids:
        return {"target": "training_assignment", "created": 0, "reason": "no competency mapping matched"}

    persons = (
        await db.execute(
            select(IncidentPerson.userId).where(IncidentPerson.incidentId == incident.id).where(IncidentPerson.userId.is_not(None))
        )
    ).scalars().all()
    operator_ids = sorted({u for u in persons if u})
    if not operator_ids:
        return {"target": "training_assignment", "created": 0, "reason": "no involved operators with a user account"}

    created = 0
    for competency_id in matched_competency_ids:
        comp = await db.get(Competency, competency_id)
        if comp is None:
            continue
        program_ids = list(getattr(comp, "relatedTrainingProgramIds", None) or [])
        schedule = None
        if program_ids:
            schedule = (
                await db.execute(
                    select(TrainingSchedule)
                    .where(TrainingSchedule.programId.in_(program_ids))
                    .where(TrainingSchedule.status.in_(["PUBLISHED", "NOMINATIONS_OPEN"]))
                    .limit(1)
                )
            ).scalar_one_or_none()
        for uid in operator_ids:
            # Skip operators who already hold a current, valid competency.
            rec = (
                await db.execute(
                    select(CompetencyRecord)
                    .where(CompetencyRecord.personUserId == uid)
                    .where(CompetencyRecord.competencyId == competency_id)
                )
            ).scalar_one_or_none()
            holds = bool(rec and rec.validUntil and rec.validUntil > _now())
            if holds:
                continue
            if schedule is None:
                continue  # gap flagged elsewhere (incident.triggeredTrainingFor); no schedule to assign to
            # De-dupe on (schedule, user).
            dup = (
                await db.execute(
                    select(TrainingRegistration)
                    .where(TrainingRegistration.scheduleId == schedule.id)
                    .where(TrainingRegistration.userId == uid)
                )
            ).scalar_one_or_none()
            if dup is not None:
                continue
            reg = TrainingRegistration(
                scheduleId=schedule.id, userId=uid, registrationType="TRIGGERED",
                triggerReason="INCIDENT_TRIGGERED", triggerSourceId=incident.id,
                approvalStatus="APPROVED", status="REGISTERED", prerequisitesMet=True,
            )
            db.add(reg)
            await db.flush()
            await link(
                db, incident_id=incident.id, target_type="training_assignment", target_id=reg.id,
                target_ref=getattr(comp, "name", None), link_type="created",
                meta={"userId": uid, "competencyId": competency_id, "scheduleId": schedule.id},
            )
            created += 1
    return {"target": "training_assignment", "created": created}


async def propagate_audit(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """Flag the next scheduled audit at this plant to include a checkpoint
    against the control that failed. Best-effort (needs a scheduled audit with a
    resolvable discipline); wrapped by the caller's SAVEPOINT."""
    from app.models.audit_compliance import ComplianceAudit
    from app.models.user import User
    from app.services import audit_compliance as ac

    if not incident.plantId:
        return {"target": "audit_checkpoint", "created": 0, "reason": "no plant"}
    audit = (
        await db.execute(
            select(ComplianceAudit)
            .where(ComplianceAudit.plantId == incident.plantId)
            .where(ComplianceAudit.status == "scheduled")
            .where(ComplianceAudit.scheduledDate >= _now())
            .where(ComplianceAudit.isDeleted.is_(False))
            .order_by(ComplianceAudit.scheduledDate.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if audit is None:
        return {"target": "audit_checkpoint", "created": 0, "reason": "no upcoming scheduled audit"}

    root = (incident.rootCauses or ["the identified root cause"])[0]
    question = f"Verify the control addressing incident {incident.number}: {root}"[:480]
    # Resolve a discipline already on the audit (add_adhoc_checkpoint requires one).
    discipline_id = None
    coverage = getattr(audit, "disciplineCoverage", None) or getattr(audit, "disciplines", None)
    if isinstance(coverage, list) and coverage:
        first = coverage[0]
        discipline_id = first.get("disciplineId") or first.get("id") if isinstance(first, dict) else None
    if not discipline_id:
        return {"target": "audit_checkpoint", "created": 0, "reason": "audit has no resolvable discipline"}

    # System actor for the ad-hoc insertion.
    system_user = (await db.execute(select(User).where(User.role == "SYSTEM_ADMIN").limit(1))).scalar_one_or_none()
    if system_user is None:
        return {"target": "audit_checkpoint", "created": 0, "reason": "no system actor"}
    try:
        res = await ac.add_adhoc_checkpoint(
            db, user=system_user, audit_id=audit.id,
            payload={"disciplineId": discipline_id, "question": question, "severity": "major",
                     "guidance": f"Auto-added by golden thread from incident {incident.number}."},
        )
        cp = (res or {}).get("checkpoint") or {}
        cp_id = cp.get("id") or audit.id
        await link(
            db, incident_id=incident.id, target_type="audit_checkpoint", target_id=str(cp_id),
            target_ref=getattr(audit, "auditNumber", None) or audit.id, link_type="created",
            meta={"auditId": audit.id, "question": question},
        )
        return {"target": "audit_checkpoint", "created": 1, "auditId": audit.id}
    except Exception as e:  # noqa: BLE001
        return {"target": "audit_checkpoint", "created": 0, "reason": f"add_adhoc_checkpoint failed: {str(e)[:80]}"}


# ─── Query + reversal ──────────────────────────────────────────────────────

async def downstream_impact(db: AsyncSession, incident_id: str) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(GoldenThreadLink)
            .where(GoldenThreadLink.sourceIncidentId == incident_id)
            .order_by(GoldenThreadLink.createdAt.asc())
        )
    ).scalars().all()
    return [
        {
            "id": r.id, "targetType": r.targetType, "targetId": r.targetId, "targetRef": r.targetRef,
            "linkType": r.linkType, "triggeredBy": r.triggeredBy, "meta": r.meta,
            "reversed": r.reversedAt is not None,
            "createdAt": r.createdAt.isoformat() if r.createdAt else None,
        }
        for r in rows
    ]


async def links_for_target(db: AsyncSession, target_type: str, target_id: str) -> list[dict[str, Any]]:
    """Provenance lookup — which incident(s) touched this downstream record."""
    rows = (
        await db.execute(
            select(GoldenThreadLink, Incident.number)
            .join(Incident, Incident.id == GoldenThreadLink.sourceIncidentId, isouter=True)
            .where(GoldenThreadLink.targetType == target_type)
            .where(GoldenThreadLink.targetId == target_id)
            .where(GoldenThreadLink.reversedAt.is_(None))
        )
    ).all()
    return [{"incidentId": r[0].sourceIncidentId, "incidentNumber": r[1], "linkType": r[0].linkType} for r in rows]


async def reverse_for_incident(db: AsyncSession, incident_id: str, *, actor_id: str | None = None) -> int:
    """Walk back the golden thread when an incident is reopened: cancel still-open
    TRIGGERED training assignments and mark all links reversed. Returns count."""
    from app.models.training import TrainingRegistration

    rows = (
        await db.execute(
            select(GoldenThreadLink)
            .where(GoldenThreadLink.sourceIncidentId == incident_id)
            .where(GoldenThreadLink.reversedAt.is_(None))
        )
    ).scalars().all()
    for r in rows:
        if r.targetType == "training_assignment":
            reg = await db.get(TrainingRegistration, r.targetId)
            if reg is not None and reg.status in ("REGISTERED", "NOT_REQUIRED"):
                reg.status = "CANCELLED"
        r.reversedAt = _now()
    await db.flush()
    return len(rows)
