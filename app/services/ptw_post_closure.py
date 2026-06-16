"""Post-closure rules engine for PTW (Dimension 4).

Mirrors the observation/near-miss/incident pattern. Each rule runs in its
own try/except + SAVEPOINT — a single bad rule must NEVER block another
rule from firing, and must NEVER block the closure flow itself. Audit
output is written back to the Permit row's cross-module fields and
emitted to stderr for log scraping.

Currently wired:
  • SIMOPS Detection — find ACTIVE permits in the same area whose
    validity windows overlapped with this permit; persist to
    Permit.conflictingPermitIds for audit + dashboard.
  • Triggered Observation — auto-create a Safety Observation when this
    permit had any of:
      - gas exceedance reading
      - mid-permit suspension
      - HIGH/CRITICAL initial residual hazard on the linked FLRA
    Append the observation id to Permit.triggeredObservations.
  • Contractor Score — emit a stderr log line per closure when a
    contractor was named, scored against suspensions / exceedances /
    refusals. (No ContractorPerformance model in DB yet — this is a
    placeholder until that table lands.)
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.flra import FLRA, FLRAJobStep, FLRAStepHazard, FLRAStatus
from app.models.observation import (
    Observation,
    ObservationCategory,
    ObservationStatus,
    ObservationType,
    Severity,
)
from app.models.permit import (
    Permit,
    PermitGasTestReading,
    PermitStatus,
    PermitSuspension,
    PermitType,
)
from app.models.plant import Plant


# ─── Rule helpers ─────────────────────────────────────────────────────


def _category_for_permit(permit_type: PermitType) -> ObservationCategory:
    return {
        PermitType.HOT_WORK: ObservationCategory.HOT_WORK,
        PermitType.CONFINED_SPACE: ObservationCategory.CONFINED_SPACE,
        PermitType.WORK_AT_HEIGHT: ObservationCategory.WORK_AT_HEIGHT,
        PermitType.ELECTRICAL_LOTO: ObservationCategory.ELECTRICAL,
        PermitType.EXCAVATION: ObservationCategory.OTHER,
        PermitType.GENERAL_COLD: ObservationCategory.OTHER,
    }.get(permit_type, ObservationCategory.OTHER)


async def _next_observation_number(db: AsyncSession, plant_code: str) -> str:
    from sqlalchemy import func

    count = (
        await db.execute(select(func.count()).select_from(Observation))
    ).scalar_one()
    return f"OBS-{plant_code}-{count + 1:05d}"


# ─── Rules ────────────────────────────────────────────────────────────


async def _rule_simops_detection(
    db: AsyncSession, *, permit: Permit
) -> dict[str, Any]:
    """Find permits that were ACTIVE in the same plant whose validity
    windows overlapped with this permit's. Persist to conflictingPermitIds."""
    valid_from = permit.validFrom
    valid_to = permit.validTo
    if valid_from is None or valid_to is None:
        return {"ruleId": "rule_simops_detection", "fired": False, "reason": "no validity window"}

    rows = (
        await db.execute(
            select(Permit)
            .where(Permit.plantId == permit.plantId)
            .where(Permit.id != permit.id)
            .where(Permit.areaId == permit.areaId)
            .where(Permit.validFrom < valid_to)
            .where(Permit.validTo > valid_from)
        )
    ).scalars().all()

    ids = [p.id for p in rows]
    if ids:
        permit.conflictingPermitIds = ids
    return {
        "ruleId": "rule_simops_detection",
        "fired": len(ids) > 0,
        "data": {"overlappingPermitIds": ids, "count": len(ids)},
    }


async def _rule_triggered_observation(
    db: AsyncSession, *, permit: Permit
) -> dict[str, Any]:
    """Auto-create a Safety Observation when the permit had a gas
    exceedance, suspension, or HIGH/CRITICAL initial hazard on its FLRA."""
    triggers: list[str] = []

    # Exceedance check
    exceedance_count = 0
    rdg_rows = (
        await db.execute(
            select(PermitGasTestReading).where(
                PermitGasTestReading.permitId == permit.id,
                PermitGasTestReading.isExceedance == True,  # noqa: E712
            )
        )
    ).scalars().all()
    exceedance_count = len(rdg_rows)
    if exceedance_count:
        triggers.append(f"{exceedance_count} gas exceedance reading(s)")

    # Suspension check
    susp_count = (
        await db.execute(
            select(PermitSuspension).where(PermitSuspension.permitId == permit.id)
        )
    ).scalars().all()
    if susp_count:
        triggers.append(f"{len(susp_count)} suspension(s) during permit")

    # FLRA high-residual hazard check (only consider the live, non-superseded FLRA)
    high_hazards: list[str] = []
    flra = (
        await db.execute(
            select(FLRA)
            .where(FLRA.permitId == permit.id)
            .where(FLRA.status == FLRAStatus.COMPLETED)
            .order_by(FLRA.createdAt.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if flra is not None:
        steps = (
            await db.execute(
                select(FLRAJobStep).where(FLRAJobStep.flraId == flra.id)
            )
        ).scalars().all()
        if steps:
            step_ids = [s.id for s in steps]
            hazards = (
                await db.execute(
                    select(FLRAStepHazard).where(FLRAStepHazard.jobStepId.in_(step_ids))
                )
            ).scalars().all()
            for h in hazards:
                if h.initialRiskLevel in {"HIGH", "CRITICAL"}:
                    high_hazards.append(h.hazardDescription[:80])
            if high_hazards:
                triggers.append(
                    f"{len(high_hazards)} HIGH/CRITICAL initial hazard(s) on FLRA"
                )

    if not triggers:
        return {
            "ruleId": "rule_triggered_observation",
            "fired": False,
            "reason": "no exceedance/suspension/high-hazard triggers",
        }

    plant = await db.get(Plant, permit.plantId)
    plant_code = plant.code if plant else "PLANT"
    number = await _next_observation_number(db, plant_code)

    severity = Severity.HIGH if exceedance_count > 0 else Severity.MEDIUM

    description = (
        f"Auto-generated from PTW {permit.number} closure. "
        f"Triggers: {'; '.join(triggers)}. "
        f"Scope: {permit.scopeOfWork[:200]}."
    )

    obs = Observation(
        number=number,
        date=datetime.now(timezone.utc),
        type=ObservationType.UNSAFE_CONDITION,
        category=_category_for_permit(permit.type),
        severity=severity,
        plantId=permit.plantId,
        areaId=permit.areaId,
        observerId=permit.originatorId,
        description=description,
        status=ObservationStatus.OPEN,
    )
    db.add(obs)
    await db.flush()

    existing = list(permit.triggeredObservations or [])
    existing.append(obs.id)
    permit.triggeredObservations = existing

    return {
        "ruleId": "rule_triggered_observation",
        "fired": True,
        "data": {
            "observationId": obs.id,
            "observationNumber": obs.number,
            "triggers": triggers,
            "severity": severity.value,
        },
    }


async def _rule_contractor_score(
    db: AsyncSession, *, permit: Permit
) -> dict[str, Any]:
    """Score the contractor's behaviour on this permit. Audit-only until a
    ContractorPerformance model lands."""
    if not permit.contractorName:
        return {
            "ruleId": "rule_contractor_score",
            "fired": False,
            "reason": "no contractor named",
        }

    # +1 baseline; -2 per suspension; -5 per gas exceedance; -3 if any FLRA refusal
    score = 1

    suspensions = (
        await db.execute(
            select(PermitSuspension).where(PermitSuspension.permitId == permit.id)
        )
    ).scalars().all()
    score -= 2 * len(suspensions)

    exceedance = (
        await db.execute(
            select(PermitGasTestReading).where(
                PermitGasTestReading.permitId == permit.id,
                PermitGasTestReading.isExceedance == True,  # noqa: E712
            )
        )
    ).scalars().all()
    score -= 5 * len(exceedance)

    flra = (
        await db.execute(
            select(FLRA)
            .where(FLRA.permitId == permit.id)
            .order_by(FLRA.createdAt.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    refusal_count = 0
    if flra is not None:
        from app.models.flra import FLRACrewSignature

        refusals = (
            await db.execute(
                select(FLRACrewSignature).where(
                    FLRACrewSignature.flraId == flra.id,
                    FLRACrewSignature.refusedToSign == True,  # noqa: E712
                )
            )
        ).scalars().all()
        refusal_count = len(refusals)
        if refusal_count:
            score -= 3 * refusal_count

    return {
        "ruleId": "rule_contractor_score",
        "fired": True,
        "data": {
            "contractor": permit.contractorName,
            "score": score,
            "suspensions": len(suspensions),
            "exceedances": len(exceedance),
            "refusals": refusal_count,
        },
    }


async def _rule_lessons_distribution(
    db: AsyncSession, *, permit: Permit
) -> dict[str, Any]:
    """Stub for the AI lessons-distribution agent. The observation-flavoured
    agent at app/services/ai/agents/lessons.py is observation-specific; a
    PTW variant can be ported in a follow-up. For now we emit the trigger
    so the audit chain is visible."""
    return {
        "ruleId": "rule_lessons_distribution",
        "fired": False,
        "reason": "PTW lessons agent not yet wired (deferred — observation agent is observation-specific)",
        "data": {"closingRemark": (permit.closingRemark or "")[:280]},
    }


# ─── Entry point ──────────────────────────────────────────────────────


async def run_ptw_post_closure_rules(
    db: AsyncSession, *, permit_id: str
) -> list[dict[str, Any]]:
    """Run all PTW post-closure rules. Each rule is wrapped in a SAVEPOINT
    so a single failure doesn't poison the whole transaction. Returns the
    list of TriggerEvents. Emits a stderr log line per event."""
    permit = await db.get(Permit, permit_id)
    if permit is None or permit.status != PermitStatus.CLOSED:
        return []

    events: list[dict[str, Any]] = []

    rules = [
        ("simops_detection", _rule_simops_detection),
        ("triggered_observation", _rule_triggered_observation),
        ("contractor_score", _rule_contractor_score),
        ("lessons_distribution", _rule_lessons_distribution),
    ]

    for name, fn in rules:
        try:
            async with db.begin_nested():
                event = await fn(db, permit=permit)
                events.append(event)
                print(
                    f"[ptw-post-closure] {permit.number}: {event}", file=sys.stderr
                )
        except Exception as e:  # noqa: BLE001
            event = {"ruleId": f"rule_{name}", "fired": False, "error": str(e)}
            events.append(event)
            print(
                f"[ptw-post-closure] {permit.number}: rule {name} crashed: {e}",
                file=sys.stderr,
            )

    return events
