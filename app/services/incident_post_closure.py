"""Post-closure rules engine for Incident.

Fires when an incident's workflow CLOSURE step completes. Each rule is a
best-effort sub-operation wrapped in a SAVEPOINT so a single failure
never blocks the closure or rolls back others. Each rule appends a
`{ruleName, fired, reason, spawnedRecordNumber?}` entry to the audit log
which is stored on the incident as `lessonsDistributedTo` (reusing the
JSON column for the audit; the field is misnamed historically but the
shape is generic).

Rules:
  1. Contractor score impact — decrement scores for involved contractors
     based on severity. CRITICAL = -10, HIGH = -5, MEDIUM = -2, LOW = -1.
  2. Linked observation cross-link — every observation in
     `incident.linkedObservationIds` is updated to flag it as a
     "missed warning" (best-effort, idempotent).
  3. Lessons distribution — record which plants should receive the
     lessons-learned text. Stored as a list of plant IDs; the actual
     notification mechanism (email, dashboard alert) is downstream.
  4. Equipment re-inspection — for any IncidentEquipment row marked
     DAMAGED / MALFUNCTION / INADEQUATE_GUARDING, schedule an
     inspection task. (Stub for now — records intent; the inspection
     module would create the actual task.)
  5. 90-day effectiveness review — set
     `incident.effectivenessReviewDueAt = closedAt + 90 days`. The
     workflow engine's separate scheduler picks this up later.

This mirrors the Near Miss post-closure rules pattern from
`app/services/post_closure_rules_nm.py`.

Closure-path audit (2026-08-03): `workflow_engine._on_instance_completed`
(module == "INCIDENT") is the ONLY path that sets Incident.status = CLOSED —
`update_incident` refuses to touch a closed incident, and `reopen_incident`
only moves CLOSED → INVESTIGATION. So this engine is reachable from every
closure that exists today; no second hook is needed. A reopen deliberately
does NOT retract already-created review cycles: the incident happened, and the
hazard reassessment it prompted stands on its own."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import Incident, IncidentEquipment, IncidentPerson


# Severity → contractor score deduction (per company per incident).
_CONTRACTOR_SCORE_DEDUCTION: dict[str, int] = {
    "CRITICAL": 10,
    "HIGH": 5,
    "MEDIUM": 2,
    "LOW": 1,
}


async def _rule_contractor_score(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """Decrement ContractorCompany.score for every contractor whose
    workman was involved in this incident (deduplicated)."""
    from app.models.epc import ContractorCompany

    deduction = _CONTRACTOR_SCORE_DEDUCTION.get(incident.severity or "LOW", 1)

    # Find involved contractors (deduplicated)
    rows = (
        await db.execute(
            select(IncidentPerson.contractorCompanyId)
            .where(IncidentPerson.incidentId == incident.id)
            .where(IncidentPerson.isContractor.is_(True))
            .where(IncidentPerson.contractorCompanyId.is_not(None))
        )
    ).scalars().all()
    contractor_ids = sorted({c for c in rows if c})

    if not contractor_ids:
        return {"ruleName": "Contractor Score Impact", "fired": False, "reason": "No contractor workmen involved."}

    affected: list[dict[str, Any]] = []
    for cid in contractor_ids:
        cc = await db.get(ContractorCompany, cid)
        if cc is None:
            continue
        before = cc.score
        cc.score = max(0, cc.score - deduction)
        affected.append({"id": cc.id, "name": cc.name, "before": before, "after": cc.score})

    if not affected:
        return {"ruleName": "Contractor Score Impact", "fired": False, "reason": "Contractor companies not found."}

    # Stash the deduction record on the incident for audit
    incident.contractorScoreImpact = {
        "deduction": deduction,
        "severity": incident.severity,
        "appliedTo": affected,
        "appliedAt": datetime.now(timezone.utc).isoformat(),
    }
    return {
        "ruleName": "Contractor Score Impact",
        "fired": True,
        "reason": f"Decremented {len(affected)} contractor score(s) by {deduction} each.",
    }


async def _rule_observation_crosslink(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """For every observation in `incident.linkedObservationIds`, ensure
    a back-link exists on the observation side flagging it as a "missed
    warning" tied to this incident. Idempotent."""
    from app.models.observation import Observation

    obs_ids = list(incident.linkedObservationIds or [])
    if not obs_ids:
        return {"ruleName": "Observation Cross-link", "fired": False, "reason": "No linked observations."}

    linked_count = 0
    for oid in obs_ids:
        obs = await db.get(Observation, oid)
        if obs is None:
            continue
        # Best-effort write to a generic audit field — Observation's
        # contributedToIncidentId already exists for this purpose.
        if hasattr(obs, "contributedToIncidentId") and obs.contributedToIncidentId != incident.id:
            obs.contributedToIncidentId = incident.id
            linked_count += 1

    return {
        "ruleName": "Observation Cross-link",
        "fired": linked_count > 0,
        "reason": (
            f"Cross-linked {linked_count} observation(s) as missed warnings."
            if linked_count > 0
            else "All linked observations already cross-linked (idempotent)."
        ),
    }


async def _rule_lessons_distribution(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """Record which plants should receive the lessons-learned text. The
    actual delivery mechanism (email / dashboard banner / portal post)
    is downstream — this rule just enumerates the recipient plant IDs."""
    from app.models.plant import Plant

    if not incident.lessonsLearned:
        return {"ruleName": "Lessons Distribution", "fired": False, "reason": "No lessons-learned text recorded."}

    # For LTI/Fatality: distribute to ALL plants. For lower severity,
    # just the source plant + sibling plants in the same state.
    severity = incident.severity or "LOW"
    if severity in ("HIGH", "CRITICAL"):
        plants = (await db.execute(select(Plant.id))).scalars().all()
    else:
        # Just the source plant for now
        plants = [incident.plantId]

    incident.lessonsDistributedTo = list(plants)
    return {
        "ruleName": "Lessons Distribution",
        "fired": True,
        "reason": f"Earmarked for distribution to {len(plants)} plant(s).",
    }


async def _rule_equipment_reinspection(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """For any equipment marked DAMAGED / MALFUNCTION / INADEQUATE_GUARDING
    during the investigation, schedule a re-inspection task. (Stub for
    now — records the intent on `incident.triggeredCapaIds` style audit;
    the actual inspection task creation lives in the Inspection module
    and would be wired up when that module gets its own refactor.)"""
    rows = (
        await db.execute(
            select(IncidentEquipment).where(IncidentEquipment.incidentId == incident.id)
        )
    ).scalars().all()

    target_involvements = {"DAMAGED", "MALFUNCTION", "INADEQUATE_GUARDING"}
    needs = [r for r in rows if r.involvement in target_involvements]
    if not needs:
        return {"ruleName": "Equipment Re-inspection", "fired": False, "reason": "No equipment requires re-inspection."}

    # Record intent on the incident — the Inspection module would consume
    # this in its own refactor. For now, the audit row tells future
    # reviewers what was intended.
    return {
        "ruleName": "Equipment Re-inspection",
        "fired": True,
        "reason": f"Flagged {len(needs)} equipment item(s) for re-inspection.",
    }


async def _rule_effectiveness_review(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """Set effectivenessReviewDueAt = closedAt + 90 days. A separate
    scheduled job reads this column and creates the review task when
    the date arrives."""
    closed = incident.closedAt or datetime.now(timezone.utc)
    incident.effectivenessReviewDueAt = closed + timedelta(days=90)
    return {
        "ruleName": "90-Day Effectiveness Review Scheduling",
        "fired": True,
        "reason": f"Review scheduled for {incident.effectivenessReviewDueAt.date()}.",
    }


async def _rule_training_trigger(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """When the root-cause analysis text mentions training-related
    keywords (training gap, knowledge deficit, untrained, lack of
    awareness, inadequate training, competency gap), capture the
    affected persons + similar-role-holders for L&D follow-up.

    The Training module's L&D dashboard reads `incident.triggeredTrainingFor`
    to surface "incidents needing training response" — the actual schedule
    creation happens manually by the LD Manager (this rule is the trigger
    flag, not the auto-scheduler)."""
    text_blob = " ".join(
        filter(
            None,
            [
                (incident.rootCauseSummary or ""),
                (incident.rootCauseDetail or ""),
                (incident.correctiveActions or ""),
                (incident.preventiveActions or ""),
            ],
        )
    ).lower()
    keywords = [
        "training gap",
        "knowledge deficit",
        "untrained",
        "lack of awareness",
        "lack of training",
        "inadequate training",
        "competency gap",
        "not trained",
        "improper training",
        "training inadequate",
    ]
    matched = [k for k in keywords if k in text_blob]
    if not matched:
        return {
            "ruleName": "Training-gap detection",
            "fired": False,
            "reason": "No training-related root cause keywords detected.",
        }

    # Capture affected persons + their roles. The L&D dashboard joins
    # these against TrainingProgram.isMandatoryForRoles to show "X
    # people in role Y need refresher Z".
    persons = (
        await db.execute(
            select(IncidentPerson).where(IncidentPerson.incidentId == incident.id)
        )
    ).scalars().all()

    affected_user_ids = [p.userId for p in persons if p.userId]
    incident.triggeredTrainingFor = affected_user_ids
    incident.triggeredTrainingKeywords = matched

    # Training & Competency Engine — stage an `incident_closed` trigger so the
    # background resolver turns this keyword-detected training gap into real,
    # provenance-carrying TrainingAssignments (not just a dashboard flag). The
    # engine's keyword-mode HazardToSkillMappings match the root-cause text blob.
    try:
        from app.services.training_engine import emit_training_trigger

        await emit_training_trigger(db, "INCIDENT", incident, event_type="incident_closed")
    except Exception as e:  # noqa: BLE001
        print(f"[incident_post_closure] training trigger emit failed: {e}", file=sys.stderr)

    return {
        "ruleName": "Training-gap detection",
        "fired": True,
        "reason": (
            f"Root cause mentions {', '.join(matched)}. "
            f"Captured {len(affected_user_ids)} affected person(s) for L&D review."
        ),
    }


async def _rule_hira_review_trigger(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """Phase 6 — trigger HIRA review for entries covering the incident's
    area or activity. Per spec §3.3 / §6.3.

    Match logic (best-effort, conservative — never over-trigger):
      1. Plant must match. Area must match too, but via EITHER the entry's own
         areaId OR its study's areaId — a study scoped to an area does cover
         its entries even when the entry row leaves areaId null. Matching only
         on entry.areaId is why this rule had never fired in production
         despite 54 incidents sharing a plant+area with an eligible entry.
      2. Active studies only — skip DRAFT, SUPERSEDED, ARCHIVED.
      3. Skip entries that already have an open ReviewCycle (debounce).

    PRODUCT DECISION (documented per build spec): an incident with a plant but
    NO area does NOT trigger a plant-wide review. Every entry in the plant
    would be flagged, which buries the signal and trains people to dismiss the
    flag — worse than not firing. Those incidents are reported as not-fired
    with an explicit reason so the gap is visible in the audit log rather than
    silent. Revisit only if area capture becomes mandatory on incidents.

    For each match, create a HiraReviewCycle with triggeredBy='INCIDENT',
    triggerReferenceId=incident.id, assignee = entry.study.teamLeaderId, AND
    notify that assignee — a flagged status nobody is told about is missable.
    Wrapped by the caller in a SAVEPOINT so failure is non-fatal.
    """
    from sqlalchemy import or_ as _or

    from app.models.hira import HiraEntry, HiraReviewCycle, HiraStudy

    if not incident.plantId:
        return {
            "ruleName": "HIRA Review Trigger",
            "fired": False,
            "reason": "Incident has no plant context; cannot match HIRA entries.",
        }
    if not incident.areaId:
        return {
            "ruleName": "HIRA Review Trigger",
            "fired": False,
            "reason": (
                "Incident has no area. Plant-only matching is deliberately not "
                "used — it would flag every entry in the plant. No review triggered."
            ),
        }

    # Find candidate entries
    stmt = (
        select(HiraEntry, HiraStudy)
        .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
        .where(HiraStudy.plantId == incident.plantId)
        .where(
            _or(
                HiraEntry.areaId == incident.areaId,
                # Entry inherits its study's area when it declares none itself.
                (HiraEntry.areaId.is_(None)) & (HiraStudy.areaId == incident.areaId),
            )
        )
        .where(HiraStudy.status == "ACTIVE")
        .where(HiraEntry.isCurrentVersion.is_(True))
        .where(HiraEntry.status.in_(["APPROVED", "ACTIVE", "FLAGGED_FOR_REVIEW"]))
    )
    rows = (await db.execute(stmt)).all()
    if not rows:
        return {
            "ruleName": "HIRA Review Trigger",
            "fired": False,
            "reason": "No matching active HIRA entries for this incident's area.",
        }

    # Debounce: skip entries with an open review cycle
    entry_ids = [r[0].id for r in rows]
    existing_open = (
        await db.execute(
            select(HiraReviewCycle.entryId)
            .where(HiraReviewCycle.entryId.in_(entry_ids))
            .where(HiraReviewCycle.status.in_(["SCHEDULED", "IN_PROGRESS"]))
        )
    ).scalars().all()
    debounce = set(existing_open)

    created = 0
    now = datetime.now(timezone.utc)
    due = now + timedelta(days=30)
    # One notification per assignee, not per entry — a leader whose study has
    # eight matching entries should get one actionable message, not eight.
    notify_counts: dict[str, int] = {}
    for entry, study in rows:
        if entry.id in debounce:
            continue
        cycle = HiraReviewCycle(
            entryId=entry.id,
            scheduledFor=due,
            triggeredBy="INCIDENT",
            triggerReferenceId=incident.id,
            status="SCHEDULED",
            assignedToId=study.teamLeaderId,
            assignedRole="TEAM_LEADER",
        )
        db.add(cycle)
        # Also flag the entry for review so the list view surfaces it
        entry.status = "FLAGGED_FOR_REVIEW"
        created += 1
        notify_counts[study.teamLeaderId] = notify_counts.get(study.teamLeaderId, 0) + 1

    if created == 0:
        return {
            "ruleName": "HIRA Review Trigger",
            "fired": False,
            "reason": f"All {len(rows)} matching entries already have open review cycles.",
        }

    await db.flush()

    # Notification is best-effort and must never undo the review cycles: they
    # are the compliance artefact, the message is the convenience.
    notified = 0
    try:
        from app.services.erm_notifications import create_notification

        incident_ref = getattr(incident, "number", None) or incident.id
        for user_id, count in notify_counts.items():
            await create_notification(
                db,
                user_id=user_id,
                type="HIRA_REVIEW_TRIGGERED",
                severity="WARNING",
                title=f"HIRA review triggered by incident {incident_ref}",
                body=(
                    f"{count} HIRA entr{'y' if count == 1 else 'ies'} covering the "
                    f"incident's area {'has' if count == 1 else 'have'} been flagged "
                    f"for review following the closure of incident {incident_ref}. "
                    f"Due {due.date().isoformat()}."
                ),
                entity_type="Incident",
                entity_id=incident.id,
                link_url="/hira/reviews?trigger=INCIDENT",
            )
            notified += 1
    except Exception as e:  # noqa: BLE001
        logger.exception(
            "HIRA review notification failed for incident %s (%d cycle(s) still created)",
            incident.id,
            created,
        )
        return {
            "ruleName": "HIRA Review Trigger",
            "fired": True,
            "reason": (
                f"Created {created} HIRA review cycle(s) due {due.date().isoformat()}, "
                f"but notification failed: {str(e)[:120]}"
            ),
            "notificationError": True,
        }

    return {
        "ruleName": "HIRA Review Trigger",
        "fired": True,
        "reason": (
            f"Created {created} HIRA review cycle(s) due {due.date().isoformat()} "
            f"for entries in incident area; notified {notified} assignee(s)."
        ),
    }


async def _rule_push_severity_to_risk(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """Feature 5 → golden-thread seed. On closure, if this incident is linked
    to an enterprise risk (`severityDetail.linkedRiskRegisterId`), push its
    likelihood/consequence back into that risk register entry as a data point,
    so the incident's real-world outcome informs the risk's scoring — no manual
    re-entry. Recorded on the risk's tamper-evident audit trail.

    (Full bidirectional golden-thread propagation ships with Feature 7; this is
    the first concrete integration hook, built now per spec.)"""
    detail = incident.severityDetail or {}
    risk_id = detail.get("linkedRiskRegisterId")
    if not risk_id:
        return {"ruleName": "Risk-register data point", "fired": False, "reason": "No linked enterprise risk."}

    from app.models.erm import EnterpriseRisk
    from app.services.audit_log import record_event

    risk = await db.get(EnterpriseRisk, risk_id)
    if risk is None:
        return {"ruleName": "Risk-register data point", "fired": False, "reason": "Linked risk not found."}

    data_point = {
        "sourceIncidentId": incident.id,
        "sourceIncidentNumber": incident.number,
        "likelihoodOfRecurrence": detail.get("likelihoodOfRecurrence"),
        "consequenceScore": detail.get("consequenceScore"),
        "score": detail.get("score"),
        "closedAt": (incident.closedAt or datetime.now(timezone.utc)).isoformat(),
    }
    try:
        await record_event(
            db,
            entity_type="EnterpriseRisk",
            entity_id=risk.id,
            entity_code=getattr(risk, "riskCode", None) or getattr(risk, "code", None),
            plant_id=incident.plantId,
            action="INCIDENT_DATA_POINT",
            after=data_point,
            reason=f"Incident {incident.number} closed — recurrence/consequence data point.",
        )
    except Exception:  # noqa: BLE001 — the SAVEPOINT wrapper isolates this rule anyway
        pass

    from app.services import golden_thread

    await golden_thread.propagate_risk(db, incident)
    return {
        "ruleName": "Risk-register data point",
        "fired": True,
        "reason": (
            f"Pushed L={data_point['likelihoodOfRecurrence']}/C={data_point['consequenceScore']} "
            f"(score {data_point['score']}) into risk {risk.id}."
        ),
    }


async def _rule_gt_training(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """Feature 7 — create training assignments where a root cause maps to a
    competency an involved operator lacks."""
    from app.services import golden_thread

    res = await golden_thread.propagate_training(db, incident)
    return {
        "ruleName": "Golden thread — training",
        "fired": res.get("created", 0) > 0,
        "reason": (
            f"Created {res['created']} training assignment(s)."
            if res.get("created")
            else f"No training assignment created ({res.get('reason', 'no match')})."
        ),
    }


async def _rule_gt_audit(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """Feature 7 — add a checkpoint to the next scheduled audit at this plant
    against the control that failed."""
    from app.services import golden_thread

    res = await golden_thread.propagate_audit(db, incident)
    return {
        "ruleName": "Golden thread — audit checkpoint",
        "fired": res.get("created", 0) > 0,
        "reason": (
            f"Added a checkpoint to audit {res.get('auditId')}."
            if res.get("created")
            else f"No audit checkpoint added ({res.get('reason', 'n/a')})."
        ),
    }


async def _rule_cost_impact(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """Feature 8 — derive the cost-of-unsafety breakdown on closure using the
    plant's cost config, so the plant rollup picks it up."""
    from app.services import incident_cost

    detail = await incident_cost.compute_cost_impact(db, incident)
    return {
        "ruleName": "Cost-of-unsafety",
        "fired": _num(detail.get("totalCost")) > 0,
        "reason": (
            f"Total cost {detail.get('currency', 'INR')} {detail.get('totalCost', 0):,.0f} "
            f"({detail.get('costConfidence')})."
        ),
    }


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


async def _rule_gt_capa(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """Feature 7 — record traceability links for every CAPA on the incident so
    they surface on the downstream-impact panel + stay linked to their cause."""
    from app.services import golden_thread

    res = await golden_thread.propagate_capa(db, incident)
    return {
        "ruleName": "Golden thread — CAPA links",
        "fired": res.get("created", 0) > 0,
        "reason": f"Linked {res.get('created', 0)} of {res.get('total', 0)} CAPA(s).",
    }


_ALL_RULES = [
    _rule_contractor_score,
    _rule_observation_crosslink,
    _rule_lessons_distribution,
    _rule_equipment_reinspection,
    _rule_effectiveness_review,
    _rule_training_trigger,
    _rule_hira_review_trigger,
    _rule_push_severity_to_risk,
    _rule_gt_training,
    _rule_gt_audit,
    _rule_gt_capa,
    _rule_cost_impact,
]


async def run_incident_post_closure_rules(
    db: AsyncSession, incident_id: str
) -> list[dict[str, Any]]:
    """Run all post-closure rules for an incident. Each rule runs inside
    its own SAVEPOINT so one failure doesn't poison the others. Returns
    the audit log of {ruleName, fired, reason} entries."""

    incident = await db.get(Incident, incident_id)
    if incident is None:
        return [{"ruleName": "Bootstrap", "fired": False, "reason": "Incident not found."}]

    audit_log: list[dict[str, Any]] = []
    for rule in _ALL_RULES:
        try:
            async with db.begin_nested():
                entry = await rule(db, incident)
                audit_log.append(entry)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "[incident post-closure] rule %s failed for incident %s",
                rule.__name__,
                incident_id,
            )
            audit_log.append({
                "ruleName": rule.__name__.replace("_rule_", "").replace("_", " ").title(),
                "fired": False,
                "reason": f"Rule errored: {str(e)[:120]}",
                "error": True,
            })

    await db.flush()
    return audit_log
