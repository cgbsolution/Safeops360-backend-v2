"""High-severity Unsafe Act → deroster workflow.

Lifecycle: flag (soft-lock) → confirm | overrule → corrective action → reinstate.

Two principles from the spec that the code enforces rather than merely
documents:

* **A flag is a soft-lock, not a punishment.** `pending_review` blocks new work
  assignment and nothing else. `visible_status()` is the single function every
  read path goes through, and it reports a pending flag as "under review" —
  the word "derostered" is not surfaced anywhere until a human has confirmed it.

* **Silence escalates, it never decides.** The timeout scan raises the alarm to
  the configured escalation contact and stops. There is no code path in this
  module that moves a record out of `pending_review` without an actor id.

Population note: `workersInvolved` is polymorphic (User | ContractorWorker) —
this platform has two disjoint people tables with no join between them. The
corrective-action gate differs accordingly and is the one place the two paths
diverge; see `corrective_action_state`.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.epc import ContractorWorker
from app.models.observation import Observation, ObservationType, Severity
from app.models.observation_sla import (
    DEFAULT_REVIEW_SLA_HOURS,
    DEROSTER_CONFIRMED,
    DEROSTER_OVERRULED,
    DEROSTER_PENDING,
    DEROSTER_REINSTATED,
    PARTY_CONTRACTOR_WORKER,
    PARTY_USER,
    ROSTER_ACTIVE,
    ROSTER_DEROSTERED,
    ROSTER_PENDING_REVIEW,
    ObservationDeroster,
    ObservationDerosterConfig,
    ObservationDerosterEvent,
    ObservationWorkerInvolved,
)
from app.models.user import User

# Severities that qualify. HIGH and CRITICAL only — Medium/Low unsafe acts do
# not flag anyone, deliberately (spec §2.3/§2.4: preserve the reporting culture).
QUALIFYING_SEVERITIES = frozenset({Severity.HIGH, Severity.CRITICAL})

# Only an UNSAFE_ACT flags a worker. An Unsafe *Condition* is a hazard the site
# owns, not something a person did — flagging on it would be the exact
# blame-shift the observation programme exists to avoid.
QUALIFYING_TYPE = ObservationType.UNSAFE_ACT

# Who may confirm / overrule / reinstate. "Section Head" is the business name
# for the OBSERVATION workflow's CHECKER step, whose seeded approverRole is
# DEPARTMENT_HEAD (app/seed/seed_workflows.py) — there is no SECTION_HEAD role
# in this system. The plant/corporate HSE leadership roles are included because
# they own the safety hold itself.
DECISION_ROLES = frozenset(
    {"DEPARTMENT_HEAD", "HSE_MANAGER", "PLANT_HSE_HEAD", "CORPORATE_HSE", "ADMIN"}
)

MIN_DECISION_REASON_CHARS = 10

NOTIF_REVIEW_REQUIRED = "deroster_review_required"
NOTIF_REVIEW_ESCALATED = "deroster_review_escalated"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class DerosterError(Exception):
    """Raised for a rejected transition or an unresolvable worker reference.
    Routers map this to 4xx via its `status_code`."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# ── qualification ────────────────────────────────────────────────────────────
def observation_qualifies(obs: Observation) -> bool:
    """True when this observation's type + severity make involved workers
    reviewable. Does not consider whether any worker was actually named —
    `worker_involved_required` is the form-validation question."""
    return obs.type == QUALIFYING_TYPE and obs.severity in QUALIFYING_SEVERITIES


def worker_involved_required(obs_type: object, severity: object) -> bool:
    """Whether the Worker Involved field is mandatory for this combination.
    Shared by the create route, the update route and the schema layer so the
    UI's asterisk and the server's 400 can never disagree."""
    t = obs_type if isinstance(obs_type, ObservationType) else ObservationType(str(obs_type))
    s = severity if isinstance(severity, Severity) else Severity(str(severity))
    return t == QUALIFYING_TYPE and s in QUALIFYING_SEVERITIES


def flag_reason_for(obs: Observation, category_label: str | None = None) -> str:
    """Auto-generated, per spec §1.2: 'High severity Unsafe Act — <category>'."""
    sev = getattr(obs.severity, "value", obs.severity)
    cat = category_label or obs.categoryCode or getattr(obs.category, "value", obs.category)
    pretty_cat = str(cat).replace("_", " ").title()
    return f"{str(sev).title()} severity Unsafe Act — {pretty_cat}"


# ── persisting the named workers ─────────────────────────────────────────────
async def persist_workers_involved(
    db: AsyncSession, obs: Observation, entries: list, *, actor_id: str | None
) -> list[ObservationWorkerInvolved]:
    """Resolve each submitted worker reference against its own table and store
    it with a name/role/employer snapshot.

    Raises DerosterError on an unresolvable reference rather than silently
    dropping it — a High-severity act attributed to a worker who does not exist
    is worse than a rejected form. Duplicates within one submission are
    collapsed.
    """
    seen: set[tuple[str, str]] = set()
    out: list[ObservationWorkerInvolved] = []

    for entry in entries:
        party = entry.partyType
        ref = entry.userId if party == PARTY_USER else entry.contractorWorkerId
        if (party, ref) in seen:
            continue
        seen.add((party, ref))

        if party == PARTY_USER:
            person = await db.get(User, ref)
            if person is None:
                raise DerosterError(f"Worker not found: {ref}")
            name, role, employer = person.name, person.designation, person.department
        else:
            person = await db.get(ContractorWorker, ref)
            if person is None:
                raise DerosterError(f"Contractor worker not found: {ref}")
            from app.models.epc import ContractorCompany

            company = await db.get(ContractorCompany, person.contractorCompanyId)
            name, role = person.fullName, person.primaryTrade
            employer = company.name if company else None

        row = ObservationWorkerInvolved(
            observationId=obs.id,
            partyType=party,
            userId=ref if party == PARTY_USER else None,
            contractorWorkerId=ref if party == PARTY_CONTRACTOR_WORKER else None,
            nameSnapshot=name,
            roleSnapshot=role,
            employerSnapshot=employer,
            addedById=actor_id,
        )
        db.add(row)
        out.append(row)

    if out:
        await db.flush()
    return out


async def load_workers_involved(db: AsyncSession, observation_id: str) -> list[dict]:
    """Named workers on an observation, each with its deroster (if any) and the
    live roster status of the underlying person. Shaped for WorkerInvolvedOut."""
    workers = (
        await db.execute(
            select(ObservationWorkerInvolved)
            .where(ObservationWorkerInvolved.observationId == observation_id)
            .order_by(ObservationWorkerInvolved.createdAt)
        )
    ).scalars().all()
    if not workers:
        return []

    derosters = {
        d.workerInvolvedId: d
        for d in (
            await db.execute(
                select(ObservationDeroster).where(
                    ObservationDeroster.observationId == observation_id
                )
            )
        ).scalars().all()
    }

    out: list[dict] = []
    for w in workers:
        person = None
        if w.partyType == PARTY_USER and w.userId:
            person = await db.get(User, w.userId)
        elif w.contractorWorkerId:
            person = await db.get(ContractorWorker, w.contractorWorkerId)

        item: dict = {
            "id": w.id,
            "partyType": w.partyType,
            "userId": w.userId,
            "contractorWorkerId": w.contractorWorkerId,
            "name": w.nameSnapshot,
            "role": w.roleSnapshot,
            "employer": w.employerSnapshot,
            "rosterStatus": getattr(person, "rosterStatus", None) if person else None,
            "deroster": None,
        }
        d = derosters.get(w.id)
        if d is not None:
            visible = visible_status(d)
            item["deroster"] = {
                "id": d.id,
                "status": d.status,
                "displayLabel": visible["label"],
                "punitive": visible["punitive"],
                "flaggedAt": d.flaggedAt,
                "flaggedReason": d.flaggedReason,
                "reviewSlaHours": d.reviewSlaHours,
                "reviewDueAt": d.reviewDueAt,
                "reviewedById": d.reviewedById,
                "reviewedAt": d.reviewedAt,
                "reviewDecisionReason": d.reviewDecisionReason,
                "correctiveActionTrainingId": d.correctiveActionTrainingId,
                "correctiveActionCompetencyId": d.correctiveActionCompetencyId,
                "correctiveAction": (
                    await corrective_action_state(db, d) if d.status == DEROSTER_CONFIRMED else None
                ),
                "escalatedAt": d.escalatedAt,
                "escalatedToId": d.escalatedToId,
                "reinstatedById": d.reinstatedById,
                "reinstatedAt": d.reinstatedAt,
                "reinstatementNote": d.reinstatementNote,
            }
        out.append(item)
    return out


# ── config ───────────────────────────────────────────────────────────────────
async def resolve_config(db: AsyncSession, plant_id: str | None) -> ObservationDerosterConfig | None:
    """Plant row, else global row. Same precedence as the SLA matrix."""
    rows = (
        await db.execute(
            select(ObservationDerosterConfig).where(ObservationDerosterConfig.isActive.is_(True))
        )
    ).scalars().all()
    for r in rows:
        if plant_id is not None and r.plantId == plant_id:
            return r
    for r in rows:
        if r.plantId is None:
            return r
    return None


# ── audit trail ──────────────────────────────────────────────────────────────
def record_event(
    db: AsyncSession,
    deroster: ObservationDeroster,
    *,
    action: str,
    actor_id: str | None,
    from_status: str | None = None,
    to_status: str | None = None,
    notes: str | None = None,
    context: dict | None = None,
) -> ObservationDerosterEvent:
    """Append one row to the deroster's audit trail. Staged in the caller's
    session so it commits with the transition it records — an event can never
    exist for a transition that rolled back, or vice versa."""
    ev = ObservationDerosterEvent(
        derosterId=deroster.id,
        observationId=deroster.observationId,
        action=action,
        fromStatus=from_status,
        toStatus=to_status,
        actorId=actor_id,
        notes=notes,
        context=context,
    )
    db.add(ev)
    return ev


# ── roster status writes (the only place these columns are set) ──────────────
async def _set_roster_status(
    db: AsyncSession, deroster: ObservationDeroster, status: str, *, deroster_ref: str | None
) -> None:
    """Write rosterStatus on whichever people table this worker lives in.

    Note for ContractorWorker: this touches `rosterStatus` and never
    `overallStatus`. The two are separate on purpose — overallStatus is the
    contractor coordinator's employment state, and letting an EPC status edit
    clear a safety hold (or vice versa) would be a silent safety regression.
    """
    if deroster.partyType == PARTY_USER and deroster.userId:
        person = await db.get(User, deroster.userId)
    elif deroster.partyType == PARTY_CONTRACTOR_WORKER and deroster.contractorWorkerId:
        person = await db.get(ContractorWorker, deroster.contractorWorkerId)
    else:
        person = None
    if person is None:
        return
    person.rosterStatus = status
    person.currentDerosterRef = deroster_ref


async def _has_other_open_flag(db: AsyncSession, deroster: ObservationDeroster) -> ObservationDeroster | None:
    """Another still-restricting flag on the same person, from a different
    observation. Without this check, clearing one flag would return a worker to
    `active` while a second, unresolved flag was still open on them."""
    stmt = select(ObservationDeroster).where(
        ObservationDeroster.id != deroster.id,
        ObservationDeroster.status.in_([DEROSTER_PENDING, DEROSTER_CONFIRMED]),
    )
    if deroster.partyType == PARTY_USER:
        stmt = stmt.where(ObservationDeroster.userId == deroster.userId)
    else:
        stmt = stmt.where(ObservationDeroster.contractorWorkerId == deroster.contractorWorkerId)
    return (await db.execute(stmt.limit(1))).scalars().first()


async def _release_or_hold(db: AsyncSession, deroster: ObservationDeroster) -> bool:
    """Return the worker to `active` unless another open flag still holds them.
    Returns True when the worker was actually released."""
    other = await _has_other_open_flag(db, deroster)
    if other is not None:
        await _set_roster_status(
            db,
            deroster,
            ROSTER_PENDING_REVIEW if other.status == DEROSTER_PENDING else ROSTER_DEROSTERED,
            deroster_ref=other.id,
        )
        return False
    await _set_roster_status(db, deroster, ROSTER_ACTIVE, deroster_ref=None)
    return True


# ── visibility ───────────────────────────────────────────────────────────────
def visible_status(deroster: ObservationDeroster) -> dict:
    """How a flag may be described outside the review panel.

    `pending_review` is internal: it reports as "Under safety review" with
    `punitive=False`, and callers rendering worker-facing or general-report UI
    must use this rather than the raw status string. Spec §2.4 — a soft-lock
    must not read as a sanction before anyone has decided.
    """
    if deroster.status == DEROSTER_PENDING:
        return {"label": "Under safety review", "code": "under_review", "punitive": False}
    if deroster.status == DEROSTER_CONFIRMED:
        return {"label": "Derostered", "code": "derostered", "punitive": True}
    if deroster.status == DEROSTER_OVERRULED:
        return {"label": "Review closed — no action", "code": "cleared", "punitive": False}
    return {"label": "Reinstated", "code": "reinstated", "punitive": False}


# ── trigger ──────────────────────────────────────────────────────────────────
async def trigger_for_observation(
    db: AsyncSession,
    obs: Observation,
    workers: list[ObservationWorkerInvolved],
    *,
    actor_id: str | None,
    category_label: str | None = None,
) -> list[ObservationDeroster]:
    """Flag every named worker on a qualifying observation.

    One independent ObservationDeroster per worker (spec §7: multi-worker
    observations confirm/overrule separately). Idempotent — a worker who already
    has a flag on this observation is skipped, so a retried submission cannot
    double-flag. Returns the rows created.
    """
    if not observation_qualifies(obs) or not workers:
        return []

    cfg = await resolve_config(db, obs.plantId)
    sla_hours = cfg.reviewSlaHours if cfg else DEFAULT_REVIEW_SLA_HOURS
    reason = flag_reason_for(obs, category_label)
    flagged_at = now_utc()

    existing = {
        r.workerInvolvedId
        for r in (
            await db.execute(
                select(ObservationDeroster).where(ObservationDeroster.observationId == obs.id)
            )
        ).scalars().all()
    }

    created: list[ObservationDeroster] = []
    for w in workers:
        if w.id in existing:
            continue
        d = ObservationDeroster(
            observationId=obs.id,
            workerInvolvedId=w.id,
            partyType=w.partyType,
            userId=w.userId,
            contractorWorkerId=w.contractorWorkerId,
            plantId=obs.plantId,
            status=DEROSTER_PENDING,
            flaggedAt=flagged_at,
            flaggedReason=reason,
            reviewSlaHours=sla_hours,
            reviewDueAt=flagged_at + timedelta(hours=sla_hours),
        )
        db.add(d)
        await db.flush()
        await _set_roster_status(db, d, ROSTER_PENDING_REVIEW, deroster_ref=d.id)
        record_event(
            db,
            d,
            action="flagged",
            actor_id=actor_id,
            to_status=DEROSTER_PENDING,
            notes=reason,
            context={
                "observationNumber": obs.number,
                "worker": w.nameSnapshot,
                "reviewSlaHours": sla_hours,
                "reviewDueAt": d.reviewDueAt.isoformat(),
            },
        )
        created.append(d)

    if created:
        await notify_review_required(db, obs, created)
    return created


# ── notifications ────────────────────────────────────────────────────────────
async def _supervisor_ids(db: AsyncSession, deroster: ObservationDeroster) -> list[str]:
    """The worker's direct supervisor, where the schema actually records one.

    Contractor workers have one: the active MobilizationRecord's
    `reportingSupervisorUserId`. Employees do not — `User` has no manager FK.
    Rather than substitute the observation's responsible person (a different
    person doing a different job) this returns nothing for employees, and the
    caller records the gap in the audit trail instead of quietly implying the
    supervisor was told.
    """
    if deroster.partyType != PARTY_CONTRACTOR_WORKER or not deroster.contractorWorkerId:
        return []
    from app.models.epc import MobilizationRecord

    rows = (
        await db.execute(
            select(MobilizationRecord.reportingSupervisorUserId)
            .where(MobilizationRecord.contractorWorkerId == deroster.contractorWorkerId)
            .where(MobilizationRecord.actualDemobilisationDate.is_(None))
        )
    ).scalars().all()
    return [r for r in rows if r]


async def notify_review_required(
    db: AsyncSession, obs: Observation, derosters: list[ObservationDeroster]
) -> None:
    """Urgent notice to Section Head + HSE Manager + the worker's supervisor,
    immediately on trigger (spec §2.5). Best-effort — a notification failure
    must never roll back the flag itself, which is the safety-critical part."""
    from app.services.erm_notifications import _users_with_role, create_notification

    try:
        recipients: dict[str, None] = {}
        for role in ("DEPARTMENT_HEAD", "HSE_MANAGER"):
            for u in await _users_with_role(db, role, obs.plantId):
                recipients[u.id] = None
        supervisor_seen = False
        for d in derosters:
            for sid in await _supervisor_ids(db, d):
                recipients[sid] = None
                supervisor_seen = True

        names = ", ".join(
            d_names for d_names in [await _worker_name(db, d) for d in derosters] if d_names
        )
        link = f"/observations/{obs.id}#deroster"
        for uid in recipients:
            await create_notification(
                db,
                user_id=uid,
                type=NOTIF_REVIEW_REQUIRED,
                title=f"Safety review required — {obs.number}",
                body=(
                    f"{names} flagged for safety review following {obs.number}. "
                    f"{derosters[0].flaggedReason}. "
                    f"Confirm or overrule by {derosters[0].reviewDueAt:%d %b %Y %H:%M} UTC."
                ),
                severity="CRITICAL",
                entity_type="OBSERVATION",
                entity_id=obs.id,
                link_url=link,
            )
        if not supervisor_seen:
            for d in derosters:
                if d.partyType == PARTY_USER:
                    record_event(
                        db,
                        d,
                        action="supervisor_not_notified",
                        actor_id=None,
                        notes=(
                            "No direct supervisor recorded for this employee — "
                            "User has no manager field. Section Head and HSE Manager notified."
                        ),
                    )
    except Exception as e:  # noqa: BLE001
        print(f"[deroster] review notification failed for {obs.number}: {e}", file=sys.stderr)


async def _worker_name(db: AsyncSession, deroster: ObservationDeroster) -> str | None:
    row = await db.get(ObservationWorkerInvolved, deroster.workerInvolvedId)
    return row.nameSnapshot if row else None


# ── decisions ────────────────────────────────────────────────────────────────
def _require_reason(reason: str | None) -> str:
    text = (reason or "").strip()
    if len(text) < MIN_DECISION_REASON_CHARS:
        raise DerosterError(
            f"A decision reason of at least {MIN_DECISION_REASON_CHARS} characters is required."
        )
    return text


def _require_pending(deroster: ObservationDeroster) -> None:
    """409 on anything already decided. Guards against a double submit from two
    people clicking the same notification link (spec §3)."""
    if deroster.status != DEROSTER_PENDING:
        raise DerosterError(
            f"This review has already been {deroster.status.replace('_', ' ')}.", status_code=409
        )


async def confirm(
    db: AsyncSession, deroster: ObservationDeroster, *, actor_id: str, reason: str
) -> ObservationDeroster:
    """Uphold the flag: the worker is derostered and a corrective action is
    minted. Reinstatement is blocked until that action completes."""
    _require_pending(deroster)
    text = _require_reason(reason)

    deroster.status = DEROSTER_CONFIRMED
    deroster.reviewedById = actor_id
    deroster.reviewedAt = now_utc()
    deroster.reviewDecisionReason = text
    await _set_roster_status(db, deroster, ROSTER_DEROSTERED, deroster_ref=deroster.id)
    record_event(
        db,
        deroster,
        action="confirmed",
        actor_id=actor_id,
        from_status=DEROSTER_PENDING,
        to_status=DEROSTER_CONFIRMED,
        notes=text,
    )
    await _mint_corrective_action(db, deroster, actor_id=actor_id)
    return deroster


async def overrule(
    db: AsyncSession, deroster: ObservationDeroster, *, actor_id: str, reason: str
) -> ObservationDeroster:
    """Reject the flag: the soft-lock is lifted and no record of sanction
    remains. No corrective action is minted."""
    _require_pending(deroster)
    text = _require_reason(reason)

    deroster.status = DEROSTER_OVERRULED
    deroster.reviewedById = actor_id
    deroster.reviewedAt = now_utc()
    deroster.reviewDecisionReason = text
    released = await _release_or_hold(db, deroster)
    record_event(
        db,
        deroster,
        action="overruled",
        actor_id=actor_id,
        from_status=DEROSTER_PENDING,
        to_status=DEROSTER_OVERRULED,
        notes=text,
        context={"rosterReleased": released},
    )
    return deroster


# ── corrective action ────────────────────────────────────────────────────────
async def _mint_corrective_action(
    db: AsyncSession, deroster: ObservationDeroster, *, actor_id: str
) -> None:
    """Create the corrective action a confirmed deroster must complete.

    Employees go through the existing Training & Competency engine — the same
    `assign_manual` entry point the incident-triggered path uses, so there is
    exactly one assignment code path (spec §2.6: "do not build a parallel
    assignment path").

    Contractor workers cannot hold a TrainingAssignment — `personUserId` is a
    User FK and ContractorWorker has no user account by design. Their gate is
    the EPC competency record instead: the same `competencyRecords` /
    `trainingCertificates` evidence that gate-clearance checks (c) and (d)
    already validate. The competency to be evidenced is resolved through the
    same HazardToSkillMapping table, so both populations are held to the same
    mapped skill — only the evidence store differs.
    """
    try:
        from app.services.training_engine import resolver
        from app.services.training_engine.classify import build_classification_light

        obs = await db.get(Observation, deroster.observationId)
        if obs is None:
            return
        cls = build_classification_light("OBSERVATION", obs)
        mapped = await resolver.resolve_competencies(
            db, source_module="OBSERVATION", plant_id=obs.plantId, classification=cls
        )
        if not mapped:
            record_event(
                db,
                deroster,
                action="corrective_action_unmapped",
                actor_id=actor_id,
                notes=(
                    "No hazard→skill mapping matched this observation — HSE to assign the "
                    "corrective action manually before reinstatement is possible."
                ),
            )
            return
        competency_id = mapped[0]["competencyId"]
        deroster.correctiveActionCompetencyId = competency_id

        if deroster.partyType == PARTY_USER and deroster.userId:
            from app.services.training_engine.service import assign_corrective_action

            assignment = await assign_corrective_action(
                db,
                plant_id=obs.plantId,
                person_user_id=deroster.userId,
                competency_id=competency_id,
                assigned_by=actor_id,
                source_module="OBSERVATION",
                source_record_id=obs.id,
                source_record_ref=obs.number,
                reason=f"Corrective action — deroster confirmed on {obs.number}",
            )
            if assignment is not None:
                deroster.correctiveActionTrainingId = assignment.id
                record_event(
                    db,
                    deroster,
                    action="training_linked",
                    actor_id=actor_id,
                    notes="Training assignment created via the Training & Competency engine.",
                    context={"trainingAssignmentId": assignment.id, "competencyId": competency_id},
                )
        else:
            record_event(
                db,
                deroster,
                action="training_linked",
                actor_id=actor_id,
                notes=(
                    "Contractor worker — reinstatement is gated on an EPC competency / "
                    "training-certificate record for the mapped competency."
                ),
                context={"competencyId": competency_id, "gate": "EPC_COMPETENCY_RECORD"},
            )
    except Exception as e:  # noqa: BLE001
        # Never let corrective-action minting roll back the confirmation. A
        # confirmed deroster with no action is recoverable (HSE assigns
        # manually); a lost confirmation is a safety hold that silently vanished.
        print(f"[deroster] corrective action minting failed for {deroster.id}: {e}", file=sys.stderr)


async def corrective_action_state(db: AsyncSession, deroster: ObservationDeroster) -> dict:
    """Whether the corrective action is complete, and why not if it isn't.

    This is the authority the reinstate endpoint calls — the UI's disabled
    button is a courtesy, this is the gate (spec §7: reinstatement must be
    blocked even when the API is called directly).
    """
    if deroster.partyType == PARTY_USER:
        if not deroster.correctiveActionTrainingId:
            return {
                "required": True,
                "complete": False,
                "kind": "TRAINING_ASSIGNMENT",
                "assignmentId": None,
                "status": None,
                "reason": "No training assignment is linked to this deroster yet.",
            }
        from app.models.training_engine import TrainingAssignment

        assignment = await db.get(TrainingAssignment, deroster.correctiveActionTrainingId)
        status = getattr(assignment, "status", None)
        complete = status == "completed"
        return {
            "required": True,
            "complete": complete,
            "kind": "TRAINING_ASSIGNMENT",
            "assignmentId": deroster.correctiveActionTrainingId,
            "status": status,
            "competencyId": deroster.correctiveActionCompetencyId,
            "reason": None if complete else f"Linked training is '{status or 'missing'}', not completed.",
        }

    # Contractor worker — EPC competency / training-certificate evidence.
    worker = (
        await db.get(ContractorWorker, deroster.contractorWorkerId)
        if deroster.contractorWorkerId
        else None
    )
    if worker is None:
        return {
            "required": True,
            "complete": False,
            "kind": "EPC_COMPETENCY_RECORD",
            "reason": "Contractor worker record not found.",
        }
    competency_id = deroster.correctiveActionCompetencyId
    matched = _epc_evidence_after(worker, competency_id, deroster.reviewedAt)
    return {
        "required": True,
        "complete": matched is not None,
        "kind": "EPC_COMPETENCY_RECORD",
        "competencyId": competency_id,
        "evidence": matched,
        "reason": None
        if matched is not None
        else (
            "No competency or training-certificate record for the mapped competency "
            "has been added to this worker since the deroster was confirmed."
        ),
    }


def _epc_evidence_after(
    worker: ContractorWorker, competency_id: str | None, since: datetime | None
) -> dict | None:
    """A competencyRecords / trainingCertificates entry for `competency_id`
    dated after the deroster was confirmed.

    Deliberately requires evidence dated AFTER confirmation: a certificate the
    worker already held is exactly what did not prevent the unsafe act, so
    accepting it would let a confirmed deroster clear itself the moment it was
    raised.
    """
    if not competency_id:
        return None
    pools = [
        ("competencyRecords", worker.competencyRecords or []),
        ("trainingCertificates", worker.trainingCertificates or []),
    ]
    for source, entries in pools:
        for e in entries:
            if not isinstance(e, dict):
                continue
            ident = str(
                e.get("competencyId") or e.get("competencyCode") or e.get("code") or e.get("name") or ""
            )
            if ident != competency_id:
                continue
            stamp = _parse_date(
                e.get("completedAt") or e.get("issuedOn") or e.get("validFrom") or e.get("date")
            )
            if since is not None and (stamp is None or stamp < since):
                continue
            return {"source": source, "entry": e}
    return None


def _parse_date(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


# ── reinstatement ────────────────────────────────────────────────────────────
async def reinstate(
    db: AsyncSession, deroster: ObservationDeroster, *, actor_id: str, note: str | None = None
) -> ObservationDeroster:
    """Return a confirmed-and-remediated worker to active duty.

    Never automatic on training completion — a human with the authority has to
    decide the person is ready (spec §2.6).
    """
    if deroster.status == DEROSTER_REINSTATED:
        raise DerosterError("This worker has already been reinstated.", status_code=409)
    if deroster.status != DEROSTER_CONFIRMED:
        raise DerosterError(
            "Only a confirmed deroster can be reinstated "
            f"(this one is '{deroster.status.replace('_', ' ')}').",
            status_code=409,
        )
    state = await corrective_action_state(db, deroster)
    if not state.get("complete"):
        raise DerosterError(
            f"Corrective action is not complete. {state.get('reason') or ''}".strip(),
            status_code=409,
        )

    deroster.status = DEROSTER_REINSTATED
    deroster.reinstatedById = actor_id
    deroster.reinstatedAt = now_utc()
    deroster.reinstatementNote = (note or "").strip() or None
    released = await _release_or_hold(db, deroster)
    record_event(
        db,
        deroster,
        action="reinstated",
        actor_id=actor_id,
        from_status=DEROSTER_CONFIRMED,
        to_status=DEROSTER_REINSTATED,
        notes=deroster.reinstatementNote,
        context={"correctiveAction": state, "rosterReleased": released},
    )
    return deroster


# ── timeout escalation (scheduler) ───────────────────────────────────────────
async def run_escalation_scan(db: AsyncSession) -> dict:
    """Raise the alarm on reviews whose SLA has passed with no decision.

    Escalates once per record — `escalatedAt` is the latch — and NEVER decides.
    An unattended review stays `pending_review` (worker still soft-locked)
    until a human acts, because auto-confirming would sanction someone nobody
    reviewed and auto-overruling would clear a hazard nobody looked at.
    """
    from app.services.erm_notifications import _users_with_role, create_notification

    now = now_utc()
    due = (
        await db.execute(
            select(ObservationDeroster)
            .where(ObservationDeroster.status == DEROSTER_PENDING)
            .where(ObservationDeroster.escalatedAt.is_(None))
            .where(ObservationDeroster.reviewDueAt <= now)
        )
    ).scalars().all()

    escalated = 0
    for d in due:
        cfg = await resolve_config(db, d.plantId)
        recipients: list[str] = []
        if cfg and cfg.escalationContactUserId:
            recipients = [cfg.escalationContactUserId]
        else:
            role = (cfg.escalationRoleCode if cfg else None) or "HSE_MANAGER"
            recipients = [u.id for u in await _users_with_role(db, role, d.plantId)]

        obs = await db.get(Observation, d.observationId)
        name = await _worker_name(db, d)
        overdue_h = int((now - d.reviewDueAt).total_seconds() // 3600)
        for uid in recipients:
            try:
                await create_notification(
                    db,
                    user_id=uid,
                    type=NOTIF_REVIEW_ESCALATED,
                    title=f"ESCALATED — safety review overdue on {obs.number if obs else d.observationId}",
                    body=(
                        f"The safety review for {name or 'a flagged worker'} has been pending for "
                        f"{overdue_h}h past its {d.reviewSlaHours}h SLA. {d.flaggedReason}. "
                        "The worker remains under review — no decision has been made automatically."
                    ),
                    severity="CRITICAL",
                    entity_type="OBSERVATION",
                    entity_id=d.observationId,
                    link_url=f"/observations/{d.observationId}#deroster",
                )
            except Exception as e:  # noqa: BLE001
                print(f"[deroster] escalation notify failed for {d.id}: {e}", file=sys.stderr)

        d.escalatedAt = now
        d.escalatedToId = recipients[0] if recipients else None
        record_event(
            db,
            d,
            action="escalated",
            actor_id=None,
            notes=f"Review SLA of {d.reviewSlaHours}h passed with no decision. Status unchanged.",
            context={"overdueHours": overdue_h, "notified": recipients},
        )
        escalated += 1

    await db.flush()
    return {
        "summary": f"{escalated} deroster review(s) escalated",
        "recordsAffected": escalated,
    }


# ── Daily Brief card (spec §6, downstream checklist item 1) ─────────────────
async def sync_daily_brief_cards(db: AsyncSession) -> dict:
    """Surface open safety reviews as Daily Brief / Executive Sentinel cards.

    The checklist asked whether a pending deroster warrants a card type and
    recommended yes: it is high-severity, time-boxed, and the only thing on the
    brief where inaction leaves a person under an unresolved hold.

    Cards are written directly as `Alert` rows rather than through the insight
    engine, because this is a discrete record with an owner and an SLA, not a
    statistical pattern the insight rules would infer.

    Idempotent on `dedupeKey`. A review that has been decided has its card
    resolved, so the brief empties itself without a separate cleanup pass.
    """
    from app.models.alerts import Alert

    open_rows = (
        await db.execute(
            select(ObservationDeroster).where(ObservationDeroster.status == DEROSTER_PENDING)
        )
    ).scalars().all()

    created = refreshed = resolved = 0
    now = now_utc()

    for d in open_rows:
        key = f"deroster:{d.id}"
        existing = (
            await db.execute(
                select(Alert).where(Alert.dedupeKey == key).where(Alert.isDeleted.is_(False)).limit(1)
            )
        ).scalars().first()

        obs = await db.get(Observation, d.observationId)
        name = await _worker_name(db, d)
        overdue = d.reviewDueAt <= now
        # Overdue reviews are critical; inside SLA they are attention-level —
        # the brief should not scream about something with hours left on it.
        severity = "critical" if overdue else "attention"
        title = f"Safety review {'overdue' if overdue else 'awaiting decision'} — {name or 'flagged worker'}"
        body = (
            f"{d.flaggedReason} on {obs.number if obs else d.observationId}. "
            f"{'Review SLA passed ' + str(int((now - d.reviewDueAt).total_seconds() // 3600)) + 'h ago.' if overdue else 'Due ' + d.reviewDueAt.strftime('%d %b %H:%M') + ' UTC.'} "
            "The worker is on hold for new work assignment until a decision is made."
        )
        link = f"/observations/{d.observationId}#deroster"

        if existing is not None:
            if existing.title != title or existing.severity != severity or existing.bodyText != body:
                existing.title = title
                existing.severity = severity
                existing.bodyText = body
                existing.updatedAt = now
                refreshed += 1
            if existing.status in ("resolved", "muted") and overdue:
                existing.status = "new"
            continue

        db.add(
            Alert(
                siteId=d.plantId,
                severity=severity,
                title=title,
                bodyParams={
                    "source": "deroster",
                    "module": "OBSERVATION",
                    "derosterId": d.id,
                    "reviewDueAt": d.reviewDueAt.isoformat(),
                    "escalated": d.escalatedAt is not None,
                    "suggestedAction": "Confirm or overrule the safety review.",
                },
                bodyText=body,
                sourceEventType="deroster_review",
                sourceEntityType="OBSERVATION",
                sourceEntityId=d.observationId,
                impactedEntities=[
                    {
                        "type": "OBSERVATION",
                        "id": d.observationId,
                        "ref": obs.number if obs else d.observationId,
                        "label": name or "Flagged worker",
                        "href": link,
                    }
                ],
                deepLink=link,
                dedupeKey=key,
                audienceRoles=["DEPARTMENT_HEAD", "HSE_MANAGER", "PLANT_HSE_HEAD"],
                audienceSiteIds=[d.plantId],
            )
        )
        created += 1

    # Retire cards for reviews that have since been decided.
    open_keys = {f"deroster:{d.id}" for d in open_rows}
    stale = (
        await db.execute(
            select(Alert)
            .where(Alert.sourceEventType == "deroster_review")
            .where(Alert.status != "resolved")
            .where(Alert.isDeleted.is_(False))
        )
    ).scalars().all()
    for a in stale:
        if a.dedupeKey not in open_keys:
            a.status = "resolved"
            resolved += 1

    await db.flush()
    return {"created": created, "refreshed": refreshed, "resolved": resolved}


__all__ = [
    "DerosterError",
    "sync_daily_brief_cards",
    "QUALIFYING_SEVERITIES",
    "QUALIFYING_TYPE",
    "DECISION_ROLES",
    "NOTIF_REVIEW_REQUIRED",
    "NOTIF_REVIEW_ESCALATED",
    "observation_qualifies",
    "worker_involved_required",
    "flag_reason_for",
    "persist_workers_involved",
    "load_workers_involved",
    "resolve_config",
    "record_event",
    "visible_status",
    "trigger_for_observation",
    "confirm",
    "overrule",
    "reinstate",
    "corrective_action_state",
    "run_escalation_scan",
]
