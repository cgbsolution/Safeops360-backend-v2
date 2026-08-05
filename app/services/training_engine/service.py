"""Training Engine orchestration (DB + side effects).

Wires the pure rules (rules.py) to the database and the platform's notification
+ competency machinery:
  • drain_trigger_events   — the resolver job: drain the outbox, run severity +
                             threshold rules, persist assignments, escalate/flag
  • run_recert_scan        — the recertification rule as a scheduled scan
  • run_overdue_scan       — flip past-due assignments → overdue + notify
  • complete_assignment    — worker completes → CompetencyRecord updated →
                             correlation data point logged (spec workflow 2)
  • assign_manual          — HSE/admin manual assignment

Everything is idempotent + best-effort on notifications (never raises into a
scheduler tick). Severity-rule assignments are created non-dismissible with an
HSE Manager escalation, per the business rules.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competency_matrix import Competency, CompetencyRecord
from app.models.training_engine import (
    TrainingAssignment,
    TrainingContent,
    TrainingRuleConfig,
    TrainingTriggerEvent,
)
from app.services.erm_notifications import _users_with_role, create_notification
from app.services.training_engine import resolver
from app.services.training_engine.config import resolve_config
from app.services.training_engine.resolver import now_utc
from app.services.training_engine.rules import (
    AssignmentDraft,
    ReviewFlag,
    RuleConfigView,
    recert_rule,
    severity_rule,
    threshold_rule,
)

_VALIDATING_EVIDENCE = {"assessment", "manual_signoff"}


# ── content picker (vendor-opaque: engine only reads competencyId) ───────────
async def pick_primary_content(db: AsyncSession, competency_id: str, plant_id: str | None) -> str | None:
    """The default TrainingContent to attach for a competency. The engine treats
    the returned id as opaque — it never inspects contentType/vendorId (spec §C)."""
    rows = (
        await db.execute(
            select(TrainingContent)
            .where(TrainingContent.competencyId == competency_id)
            .where(TrainingContent.isActive.is_(True))
            .where(TrainingContent.isDeleted.is_(False))
        )
    ).scalars().all()
    if not rows:
        return None
    plant_rows = [c for c in rows if c.plantId == plant_id] if plant_id else []
    pool = plant_rows or rows
    primary = next((c for c in pool if c.isPrimary), None)
    return (primary or pool[0]).id


async def _hse_manager_ids(db: AsyncSession, plant_id: str | None) -> list[str]:
    try:
        users = await _users_with_role(db, "HSE_MANAGER", plant_id)
        return [u.id for u in users]
    except Exception:  # noqa: BLE001
        return []


# ── persistence ────────────────────────────────────────────────────────────────
async def _exists_open(db: AsyncSession, *, person: str, competency: str, source_record_id: str | None) -> bool:
    stmt = (
        select(TrainingAssignment.id)
        .where(TrainingAssignment.personUserId == person)
        .where(TrainingAssignment.competencyId == competency)
        .where(TrainingAssignment.status.in_(resolver._OPEN_ASSIGNMENT_STATES))
        .where(TrainingAssignment.isDeleted.is_(False))
        .limit(1)
    )
    if source_record_id:
        stmt = stmt.where(TrainingAssignment.sourceRecordId == source_record_id)
    return (await db.execute(stmt)).first() is not None


async def _persist_draft(
    db: AsyncSession, draft: AssignmentDraft, *, config: RuleConfigView, assigned_by: str | None
) -> TrainingAssignment | None:
    if await _exists_open(db, person=draft.personUserId, competency=draft.competencyId, source_record_id=draft.sourceRecordId):
        return None  # dedupe — an open assignment already covers this
    content_id = await pick_primary_content(db, draft.competencyId, draft.plantId)
    due = now_utc() + timedelta(days=draft.dueOffsetDays or config.assignmentDueDays)
    escalated_to = None
    if draft.escalationFlag:
        mgrs = await _hse_manager_ids(db, draft.plantId)
        escalated_to = mgrs[0] if mgrs else None
    a = TrainingAssignment(
        plantId=draft.plantId,
        personUserId=draft.personUserId,
        competencyId=draft.competencyId,
        source=draft.source,
        ruleType=draft.source,
        sourceModule=draft.sourceModule,
        sourceRecordId=draft.sourceRecordId,
        sourceRecordRef=draft.sourceRecordRef,
        triggerMappingId=draft.triggerMappingId,
        provenance=draft.provenance,
        contentId=content_id,
        assignedByUserId=assigned_by,
        dueDate=due,
        status="assigned",
        isMandatory=draft.isMandatory,
        dismissible=draft.dismissible,
        escalationFlag=draft.escalationFlag,
        escalatedToUserId=escalated_to,
    )
    db.add(a)
    await db.flush()
    return a


async def _notify_new_assignment(db: AsyncSession, a: TrainingAssignment) -> None:
    comp = await db.get(Competency, a.competencyId)
    comp_name = comp.name if comp else a.competencyId
    mandatory = " (mandatory — cannot be declined)" if a.isMandatory else ""
    await create_notification(
        db,
        user_id=a.personUserId,
        type="TRAINING_ASSIGNED",
        title=f"Training assigned: {comp_name}",
        body=(
            f"You have been assigned training for '{comp_name}'{mandatory}.\n"
            f"Reason: {a.source.replace('_', ' ')}"
            + (f" from {a.sourceRecordRef}" if a.sourceRecordRef else "")
            + (f"\nDue by {a.dueDate.date()}." if a.dueDate else "")
        ),
        severity="WARNING" if a.isMandatory else "INFO",
        entity_type="TrainingAssignment",
        entity_id=a.id,
        link_url=f"/training/assignments/{a.id}",
        send_mail=a.isMandatory,
    )
    if a.escalationFlag and a.escalatedToUserId:
        await create_notification(
            db,
            user_id=a.escalatedToUserId,
            type="TRAINING_ESCALATION",
            title=f"SIF-potential training assignment: {comp_name}",
            body=(
                f"A serious-event training assignment was auto-created for a worker "
                f"following {a.sourceRecordRef or a.sourceModule}. Competency: {comp_name}. "
                f"This assignment is mandatory and cannot be dismissed by the worker."
            ),
            severity="CRITICAL",
            entity_type="TrainingAssignment",
            entity_id=a.id,
            link_url=f"/training/assignments/{a.id}",
            send_mail=True,
        )


async def _notify_flag(db: AsyncSession, flag: ReviewFlag) -> None:
    for mgr_id in await _hse_manager_ids(db, flag.plantId):
        await create_notification(
            db,
            user_id=mgr_id,
            type="TRAINING_REVIEW_FLAG",
            title="Training assignment needs manual review",
            body=f"{flag.reason}: {flag.detail}",
            severity="WARNING",
            entity_type=flag.sourceModule or "TrainingEngine",
            entity_id=flag.sourceRecordId or flag.competencyId,
            link_url="/training/assignments?tab=review",
            send_mail=False,
        )


async def create_assignments(
    db: AsyncSession,
    drafts: list[AssignmentDraft],
    flags: list[ReviewFlag],
    *,
    config: RuleConfigView,
    assigned_by: str | None = None,
) -> dict:
    created = 0
    escalated = 0
    for d in drafts:
        a = await _persist_draft(db, d, config=config, assigned_by=assigned_by)
        if a is None:
            continue
        created += 1
        if a.escalationFlag:
            escalated += 1
        try:
            await _notify_new_assignment(db, a)
        except Exception as e:  # noqa: BLE001
            print(f"[training_engine] notify failed for assignment {a.id}: {e}", file=sys.stderr)
    for f in flags:
        try:
            await _notify_flag(db, f)
        except Exception as e:  # noqa: BLE001
            print(f"[training_engine] flag notify failed: {e}", file=sys.stderr)
    return {"created": created, "escalated": escalated, "flagged": len(flags)}


# ── the resolver: process one trigger event ──────────────────────────────────
async def process_trigger_event(db: AsyncSession, ev: TrainingTriggerEvent, config: RuleConfigView) -> dict:
    cls = ev.classification or {}
    comps = await resolver.resolve_competencies(
        db, source_module=ev.sourceModule, plant_id=ev.plantId, classification=cls
    )
    competency_ids = [c["competencyId"] for c in comps]
    mapping_by_comp = {c["competencyId"]: c["mappingId"] for c in comps}

    drafts: list[AssignmentDraft] = []
    flags: list[ReviewFlag] = []

    # RULE 2 — severity (immediate, individual, non-dismissible)
    sev = severity_rule(
        classification=cls,
        mapped_competency_ids=competency_ids,
        plant_id=ev.plantId,
        source_module=ev.sourceModule,
        source_record_id=ev.sourceRecordId,
        source_record_ref=ev.sourceRecordRef,
        config=config,
        mapping_by_competency=mapping_by_comp,
    )
    drafts.extend(sev.drafts)
    flags.extend(sev.flags)

    # RULE 1 — threshold (per mapped competency), always role-scoped
    dept = cls.get("departmentId")
    for c in comps:
        cid = c["competencyId"]
        count = await resolver.count_mapped_records(
            db, competency_id=cid, plant_id=ev.plantId, department_id=dept, window_days=config.thresholdWindowDays
        )
        requiring = await resolver.requiring_worker_ids(db, competency_id=cid, plant_id=ev.plantId, department_id=dept)
        covered = await resolver.open_assignment_persons(db, competency_id=cid, person_ids=requiring)
        out = threshold_rule(
            competency_id=cid,
            plant_id=ev.plantId,
            department_id=dept,
            matched_record_count=count,
            requiring_worker_ids=requiring,
            already_covered_ids=covered,
            config=config,
            trigger_mapping_id=c["mappingId"],
            provenance={"triggerRecordRef": ev.sourceRecordRef, "triggerModule": ev.sourceModule},
        )
        drafts.extend(out.drafts)
        flags.extend(out.flags)

    res = await create_assignments(db, drafts, flags, config=config)
    return res


async def drain_trigger_events(db: AsyncSession, batch: int = 100) -> dict:
    """Scheduler job body: drain unprocessed TrainingTriggerEvents through the
    rule engine. A poisoned event is marked processed with its error so it can
    never wedge the queue (same discipline as the alerts resolver)."""
    events = (
        await db.execute(
            select(TrainingTriggerEvent)
            .where(TrainingTriggerEvent.processedAt.is_(None))
            .order_by(TrainingTriggerEvent.occurredAt.asc())
            .limit(batch)
        )
    ).scalars().all()

    created = escalated = flagged = errors = 0
    for ev in events:
        try:
            config = await resolve_config(db, ev.plantId)
            res = await process_trigger_event(db, ev, config)
            created += res.get("created", 0)
            escalated += res.get("escalated", 0)
            flagged += res.get("flagged", 0)
            ev.processedAt = now_utc()
            ev.processingError = None
        except Exception as e:  # noqa: BLE001
            print(f"[training_engine] trigger {ev.id} failed: {e}", file=sys.stderr)
            ev.processedAt = now_utc()
            ev.processingError = str(e)[:500]
            errors += 1
    await db.commit()
    return {"events": len(events), "created": created, "escalated": escalated, "flagged": flagged, "errors": errors}


# ── recert scan (RULE 4) ──────────────────────────────────────────────────────
async def run_recert_scan(db: AsyncSession) -> dict:
    config = await resolve_config(db, None)
    records = await resolver.records_due_for_recert(db, window_days=config.recertWindowDays, plant_ids=None)
    if not records:
        await db.commit()
        return {"due": 0, "created": 0}
    pairs = [(r.personUserId, r.competencyId) for r in records]
    covered = await resolver.open_assignment_pairs(db, pairs=pairs)
    out = recert_rule(records_due=records, already_covered_ids=covered, config=config)
    res = await create_assignments(db, out.drafts, out.flags, config=config)
    await db.commit()
    return {"due": len(records), **res}


# ── overdue scan ──────────────────────────────────────────────────────────────
async def run_overdue_scan(db: AsyncSession) -> dict:
    now = now_utc()
    rows = (
        await db.execute(
            select(TrainingAssignment)
            .where(TrainingAssignment.status.in_(["assigned", "in_progress"]))
            .where(TrainingAssignment.dueDate.is_not(None))
            .where(TrainingAssignment.dueDate < now)
            .where(TrainingAssignment.isDeleted.is_(False))
        )
    ).scalars().all()
    flipped = 0
    for a in rows:
        a.status = "overdue"
        flipped += 1
        try:
            comp = await db.get(Competency, a.competencyId)
            await create_notification(
                db,
                user_id=a.personUserId,
                type="TRAINING_OVERDUE",
                title=f"Training overdue: {comp.name if comp else a.competencyId}",
                body="Your assigned training is now overdue. Please complete it as soon as possible.",
                severity="WARNING",
                entity_type="TrainingAssignment",
                entity_id=a.id,
                link_url=f"/training/assignments/{a.id}",
                send_mail=a.isMandatory,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[training_engine] overdue notify failed: {e}", file=sys.stderr)
    await db.commit()
    return {"overdue": flipped}


# ── completion → competency update → correlation (spec workflow 2) ───────────
def _add_months(dt: datetime, months: int) -> datetime:
    # Month arithmetic without a dateutil dependency (day clamped to 28 for safety).
    total = dt.month - 1 + months
    year = dt.year + total // 12
    month = total % 12 + 1
    return dt.replace(year=year, month=month, day=min(dt.day, 28))


async def _apply_completion_to_record(db: AsyncSession, a: TrainingAssignment, evidence_type: str, actor_id: str | None) -> str | None:
    record = (
        await db.execute(
            select(CompetencyRecord)
            .where(CompetencyRecord.personUserId == a.personUserId)
            .where(CompetencyRecord.competencyId == a.competencyId)
        )
    ).scalar_one_or_none()
    comp = await db.get(Competency, a.competencyId)
    now = now_utc()
    validating = evidence_type in _VALIDATING_EVIDENCE
    valid_until = _add_months(now, comp.defaultValidityMonths) if (validating and comp) else None
    next_reval = (
        valid_until - timedelta(days=(comp.preExpiryWarningDays if comp else 90)) if valid_until else None
    )
    if record is None:
        record = CompetencyRecord(
            plantId=a.plantId,
            personUserId=a.personUserId,
            competencyId=a.competencyId,
            state="validated_active" if validating else "in_training",
            currentValidatedAt=now if validating else None,
            currentValidatedByUserId=actor_id if validating else None,
            currentValidationMethod=evidence_type if validating else None,
            validFrom=now if validating else None,
            validUntil=valid_until,
            nextRevalidationDue=next_reval,
            lastProgressEventAt=now,
            createdByUserId=actor_id or "SYSTEM:training_engine",
        )
        db.add(record)
        await db.flush()
    else:
        record.lastProgressEventAt = now
        record.updatedByUserId = actor_id
        if validating:
            record.state = "validated_active"
            record.currentValidatedAt = now
            record.currentValidatedByUserId = actor_id
            record.currentValidationMethod = evidence_type
            record.validFrom = now
            record.validUntil = valid_until
            record.nextRevalidationDue = next_reval
        elif record.state in ("not_yet_attempted", "expired", "expired_in_grace", "lapsed"):
            record.state = "in_training"
    return record.id


async def complete_assignment(
    db: AsyncSession,
    a: TrainingAssignment,
    *,
    evidence_type: str = "training_completion",
    evidence_id: str | None = None,
    note: str | None = None,
    actor_id: str | None = None,
) -> dict:
    now = now_utc()
    a.status = "completed"
    a.completedAt = now
    a.completionEvidenceType = evidence_type
    a.completionEvidenceId = evidence_id
    a.completionNote = note
    a.updatedBy = actor_id
    a.competencyRecordId = await _apply_completion_to_record(db, a, evidence_type, actor_id)

    # Correlation data point when the assignment traces to a triggering record.
    logged = False
    if a.source in ("severity_rule", "threshold_rule") and a.sourceModule and a.sourceRecordId:
        try:
            from app.services.training_engine.correlation import log_completion

            await log_completion(db, a)
            logged = True
        except Exception as e:  # noqa: BLE001
            print(f"[training_engine] correlation log failed: {e}", file=sys.stderr)
    await db.flush()
    return {"assignmentId": a.id, "competencyRecordId": a.competencyRecordId, "correlationLogged": logged}


# ── manual assignment (HSE / admin) ──────────────────────────────────────────
async def assign_manual(
    db: AsyncSession,
    *,
    plant_id: str,
    person_user_id: str,
    competency_id: str,
    assigned_by: str,
    due_days: int | None = None,
    content_id: str | None = None,
) -> TrainingAssignment:
    config = await resolve_config(db, plant_id)
    draft = AssignmentDraft(
        personUserId=person_user_id,
        competencyId=competency_id,
        source="manual",
        plantId=plant_id,
        provenance={"ruleType": "manual", "assignedBy": assigned_by},
        isMandatory=False,
        dismissible=True,
        dueOffsetDays=due_days or config.assignmentDueDays,
    )
    a = await _persist_draft(db, draft, config=config, assigned_by=assigned_by)
    if a is None:
        # An open assignment already exists — return it rather than erroring.
        existing = (
            await db.execute(
                select(TrainingAssignment)
                .where(TrainingAssignment.personUserId == person_user_id)
                .where(TrainingAssignment.competencyId == competency_id)
                .where(TrainingAssignment.status.in_(resolver._OPEN_ASSIGNMENT_STATES))
                .where(TrainingAssignment.isDeleted.is_(False))
                .limit(1)
            )
        ).scalar_one_or_none()
        return existing
    if content_id:
        a.contentId = content_id
    try:
        await _notify_new_assignment(db, a)
    except Exception as e:  # noqa: BLE001
        print(f"[training_engine] manual notify failed: {e}", file=sys.stderr)
    return a


async def assign_corrective_action(
    db: AsyncSession,
    *,
    plant_id: str,
    person_user_id: str,
    competency_id: str,
    assigned_by: str | None,
    source_module: str,
    source_record_id: str,
    source_record_ref: str | None = None,
    reason: str = "",
) -> TrainingAssignment | None:
    """Assignment that must complete before a safety hold can be lifted
    (Observation deroster, spec §2.6).

    NOT `assign_manual`: that path is deliberately dismissible and
    non-mandatory, which would let the worker decline the very training their
    reinstatement is gated on. This mints the same shape the severity rule
    does — mandatory, non-dismissible, HSE-escalated — through the same
    `AssignmentDraft` + `_persist_draft` machinery, so there is still exactly
    one place assignments are created.

    Returns the existing open assignment if one already covers this person +
    competency + source record (dedupe), or None if neither could be resolved.
    """
    config = await resolve_config(db, plant_id)
    draft = AssignmentDraft(
        personUserId=person_user_id,
        competencyId=competency_id,
        source="deroster_corrective_action",
        plantId=plant_id,
        sourceModule=source_module,
        sourceRecordId=source_record_id,
        sourceRecordRef=source_record_ref,
        provenance={
            "ruleType": "deroster_corrective_action",
            "assignedBy": assigned_by,
            "reason": reason,
            "gatesReinstatement": True,
        },
        isMandatory=True,
        dismissible=False,
        escalationFlag=True,
        dueOffsetDays=config.assignmentDueDays,
    )
    a = await _persist_draft(db, draft, config=config, assigned_by=assigned_by)
    if a is None:
        a = (
            await db.execute(
                select(TrainingAssignment)
                .where(TrainingAssignment.personUserId == person_user_id)
                .where(TrainingAssignment.competencyId == competency_id)
                .where(TrainingAssignment.status.in_(resolver._OPEN_ASSIGNMENT_STATES))
                .where(TrainingAssignment.isDeleted.is_(False))
                .limit(1)
            )
        ).scalar_one_or_none()
        return a
    try:
        await _notify_new_assignment(db, a)
    except Exception as e:  # noqa: BLE001
        print(f"[training_engine] corrective-action notify failed: {e}", file=sys.stderr)
    return a


__all__ = [
    "assign_corrective_action",
    "pick_primary_content",
    "create_assignments",
    "process_trigger_event",
    "drain_trigger_events",
    "run_recert_scan",
    "run_overdue_scan",
    "complete_assignment",
    "assign_manual",
]
