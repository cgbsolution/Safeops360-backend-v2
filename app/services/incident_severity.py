"""Feature 5 — Incident severity/risk scoring + escalation.

Severity stops being a static label and starts driving behaviour:

  score = likelihoodOfRecurrence (1-5) × consequenceScore (1-5)   → 1-25

`likelihoodOfRecurrence` is derived from the trend matcher's recurrence count
(manually overridable); `consequenceScore` is set by the classifier. The band
label reuses the ERM 5×5 scoring service (`erm.band_for_score`, the active
`ScoringMatrixConfig.ratingBands`) so incidents and enterprise risks speak the
same language — no parallel scoring engine.

Escalation rules:
  • score ≥ threshold (default 15, plant-override deferred to Feature 8) →
    notify Corporate HSE at classification, not just at closure.
  • ≥3 incidents in the same equipment category within 90 days → escalate
    regardless of individual severity (Feature 3's trend engine feeding
    escalation directly).

Every escalation is written to `severityDetail.escalationLog`, fires the
existing notification service, and records an explicit tamper-evident audit
entry — same rigor as a manual action.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import Incident
from app.models.user import User
from app.services import incident_similarity
from app.services.audit_log import record_event
from app.services.erm import band_for_score, bands_from_active_matrix
from app.services.erm_notifications import create_notification

# Default escalation threshold (score). A per-plant override lands with
# Feature 8's plantCostConfig; until then this constant applies platform-wide.
DEFAULT_ESCALATION_SCORE = 15
RECURRENCE_ESCALATION_COUNT = 3
RECURRENCE_WINDOW_DAYS = 90

# Role code(s) that receive corporate-level escalation (seed_rbac: CORPORATE_HSE).
ESCALATION_ROLE_CODES = ["CORPORATE_HSE"]

# Fallback consequence when the classifier hasn't entered a numeric score —
# derived from the label so back-compat classifications still get a score.
_CONSEQUENCE_FROM_LABEL = {"LOW": 1, "MEDIUM": 2, "HIGH": 4, "CRITICAL": 5}

# Band ordering — used to floor a fallback-derived band so numeric scoring can
# escalate a severity label but never mechanically downgrade a human's explicit
# classification.
_BAND_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


def _higher_band(a: str | None, b: str | None) -> str:
    ra, rb = _BAND_RANK.get((a or "").upper(), -1), _BAND_RANK.get((b or "").upper(), -1)
    return a if ra >= rb else b


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clamp(v: int, lo: int = 1, hi: int = 5) -> int:
    return max(lo, min(hi, int(v)))


def _likelihood_from_recurrence(count: int) -> int:
    """Map trailing-window recurrence count → 1-5 likelihood band."""
    if count <= 0:
        return 1
    if count == 1:
        return 2
    if count == 2:
        return 3
    if count == 3:
        return 4
    return 5


def _consequence_fallback(label: str | None) -> int:
    return _CONSEQUENCE_FROM_LABEL.get((label or "").upper(), 2)


def escalation_reasons(
    score: int,
    recurrence: int,
    *,
    threshold: int = DEFAULT_ESCALATION_SCORE,
) -> list[str]:
    """Pure escalation-decision logic (unit-testable without a DB): a score at
    or above threshold, OR a recurrence count at/above the trend limit."""
    reasons: list[str] = []
    if score >= threshold:
        reasons.append(f"Risk score {score} ≥ escalation threshold {threshold}")
    if recurrence >= RECURRENCE_ESCALATION_COUNT:
        reasons.append(
            f"{recurrence} incidents in the same equipment category within "
            f"{RECURRENCE_WINDOW_DAYS} days"
        )
    return reasons


async def _escalation_recipients(db: AsyncSession) -> list[User]:
    rows = (
        await db.execute(
            select(User)
            .where(User.role.in_(ESCALATION_ROLE_CODES))
            .where(User.isActive.is_(True))
        )
    ).scalars().all()
    return list(rows)


async def apply_severity_scoring(
    db: AsyncSession,
    incident: Incident,
    *,
    consequence_score: int | None = None,
    likelihood_override: int | None = None,
    linked_risk_id: str | None = None,
    threshold: int = DEFAULT_ESCALATION_SCORE,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Compute (or recompute) the numeric severity for an incident, run the
    escalation engine, and persist `incident.severityDetail`. Also sets
    `incident.severity` to the derived band label so the label always agrees
    with the score. Returns the severityDetail dict. Caller commits."""

    prev = dict(incident.severityDetail or {})

    footprint = await incident_similarity.equipment_footprint(db, incident.id)
    recurrence = await incident_similarity.recurrence_count(
        db,
        categories=footprint["categories"],
        equipment_ids=footprint["equipmentIds"],
        plant_id=incident.plantId,
        window_days=RECURRENCE_WINDOW_DAYS,
        exclude_incident_id=incident.id,
    )

    # Likelihood: an explicit human override persists across recomputes; an
    # auto-derived likelihood is re-derived from the (possibly changed)
    # recurrence count every time — that is what makes the score recalculate
    # when a new similar incident is later logged (F5 acceptance criterion).
    if likelihood_override is not None:
        likelihood = _clamp(likelihood_override)
        likelihood_overridden = True
    elif prev.get("likelihoodOverridden") and prev.get("likelihoodOfRecurrence"):
        likelihood = _clamp(prev["likelihoodOfRecurrence"])
        likelihood_overridden = True
    else:
        likelihood = _clamp(_likelihood_from_recurrence(recurrence))
        likelihood_overridden = False
    # Explicit numeric consequence → the score fully drives the label. Fallback
    # consequence (derived from the existing label) must not downgrade it.
    consequence_explicit = consequence_score is not None
    consequence = _clamp(
        consequence_score
        if consequence_explicit
        else prev.get("consequenceScore") or _consequence_fallback(incident.severity)
    )
    score = likelihood * consequence
    band = band_for_score(score, await bands_from_active_matrix(db))
    label = band if consequence_explicit else _higher_band(band, incident.severity)

    detail: dict[str, Any] = {
        "score": score,
        "likelihoodOfRecurrence": likelihood,
        "likelihoodOverridden": likelihood_overridden,
        "consequenceScore": consequence,
        "band": label,  # authoritative label (floored to the human classification)
        "computedBand": band,  # raw band from score, before flooring
        "recurrenceCount": recurrence,
        "linkedRiskRegisterId": linked_risk_id
        if linked_risk_id is not None
        else prev.get("linkedRiskRegisterId"),
        "escalationTriggered": bool(prev.get("escalationTriggered")),
        "escalationLog": list(prev.get("escalationLog") or []),
        "computedAt": _now().isoformat(),
    }

    # Label mirror — keep `severity` consistent with the score-derived band.
    incident.severity = label
    incident.severityDetail = detail

    await _maybe_escalate(
        db, incident, detail, recurrence=recurrence, threshold=threshold, actor_id=actor_id
    )

    # Persist any escalation mutations back onto the incident.
    incident.severityDetail = detail

    await record_event(
        db,
        entity_type="Incident",
        entity_id=incident.id,
        entity_code=incident.number,
        plant_id=incident.plantId,
        action="SEVERITY_SCORED",
        before={"score": prev.get("score"), "band": prev.get("band")},
        after={"score": score, "band": band, "likelihood": likelihood, "consequence": consequence},
    )
    return detail


async def recompute_affected_by(
    db: AsyncSession, new_incident: Incident, *, actor_id: str | None = None, limit: int = 25
) -> int:
    """Feature 3 → 5 auto-recalc. When a new incident is logged, prior scored,
    still-open incidents that share its equipment category now have a higher
    recurrence count — re-score them so their likelihood/escalation reflects the
    new pattern. Best-effort, bounded, skips human-overridden likelihoods (those
    persist). Returns the number of incidents recomputed."""
    from app.models.incident import IncidentStatus

    footprint = await incident_similarity.equipment_footprint(db, new_incident.id)
    ids = await incident_similarity.incidents_sharing_equipment(
        db,
        categories=footprint["categories"],
        equipment_ids=footprint["equipmentIds"],
        plant_id=new_incident.plantId,
        window_days=RECURRENCE_WINDOW_DAYS,
        exclude_incident_id=new_incident.id,
    )
    recomputed = 0
    for iid in ids[:limit]:
        inc = await db.get(Incident, iid)
        if inc is None or inc.severityDetail is None or inc.status == IncidentStatus.CLOSED:
            continue
        try:
            await apply_severity_scoring(db, inc, actor_id=actor_id)
            recomputed += 1
        except Exception:  # noqa: BLE001 — one failure never blocks incident creation
            continue
    return recomputed


async def _maybe_escalate(
    db: AsyncSession,
    incident: Incident,
    detail: dict[str, Any],
    *,
    recurrence: int,
    threshold: int,
    actor_id: str | None,
) -> None:
    reasons = escalation_reasons(detail["score"], recurrence, threshold=threshold)
    if not reasons:
        return

    reason_key = " | ".join(reasons)
    log: list[dict[str, Any]] = detail["escalationLog"]
    # Debounce: don't re-notify if the last escalation fired for the same reasons.
    if log and log[-1].get("reasonKey") == reason_key:
        return

    recipients = await _escalation_recipients(db)
    notified_ids: list[str] = []
    for u in recipients:
        try:
            await create_notification(
                db,
                user_id=u.id,
                type="INCIDENT_ESCALATION",
                title=f"Incident {incident.number} escalated to Corporate HSE",
                body=reason_key,
                severity="CRITICAL",
                entity_type="Incident",
                entity_id=incident.id,
                link_url=f"/incidents/{incident.id}",
            )
            notified_ids.append(u.id)
        except Exception:  # noqa: BLE001 — notification is best-effort, never blocks escalation
            continue

    entry = {
        "triggeredAt": _now().isoformat(),
        "reason": reason_key,
        "reasonKey": reason_key,
        "score": detail["score"],
        "notifiedRoles": ESCALATION_ROLE_CODES,
        "notifiedUserIds": notified_ids,
        "triggeredBy": actor_id or "system",
    }
    log.append(entry)
    detail["escalationTriggered"] = True

    # Explicit audit entry — the automated action is recorded with the same
    # rigor as a manual one, naming the trigger.
    await record_event(
        db,
        entity_type="Incident",
        entity_id=incident.id,
        entity_code=incident.number,
        plant_id=incident.plantId,
        action="ESCALATED",
        after={"score": detail["score"], "notifiedRoles": ESCALATION_ROLE_CODES},
        reason=reason_key,
    )

    # Best-effort domain event for the daily-brief feed (Feature 3/alerts).
    try:
        from app.services import events

        events.emit(  # synchronous: just stages the outbox row in this txn
            db,
            event_type="incident.escalated",
            entity_type="Incident",
            entity_id=incident.id,
            entity_ref=incident.number,
            site_id=incident.plantId,
            actor_id=actor_id,
            payload={"score": detail["score"], "reason": reason_key},
        )
    except Exception:  # noqa: BLE001 — outbox emit is optional here
        pass
