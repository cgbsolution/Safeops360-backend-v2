"""Gas-test refresh + exceedance service.

A gas test plan (PermitGasTestPlan) carries the parametersToTest list:
    [{"parameter": "O2", "lowLimit": 19.5, "highLimit": 23.5, "unit": "%"}, ...]

For each reading the recorder posts a `readings` array shaped like:
    [{"parameter": "O2", "value": 20.5}, ...]

This service:
  • Computes per-parameter compliance against the plan
  • Flags `isExceedance` if any value is outside its [low, high] band
  • Sets `refreshDueBy = recordedAt + refreshFrequencyMinutes`
  • If exceedance: auto-suspends the permit AND opens a PermitSuspension row
    with reason="GAS_TEST_EXCEEDANCE" so the resumption flow requires re-FLRA

Pure DB logic — caller provides the AsyncSession and User context.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.permit import (
    Permit,
    PermitGasTestPlan,
    PermitGasTestReading,
    PermitStatus,
    PermitSuspension,
)


@dataclass
class GasReadingResult:
    reading_id: str
    is_exceedance: bool
    refresh_due_by: datetime
    failed_parameters: list[str]
    auto_suspended: bool


def _evaluate_readings(
    readings: list[dict[str, Any]] | None,
    parameters_to_test: list[dict[str, Any]] | None,
) -> tuple[bool, list[str]]:
    """Returns (is_exceedance, failed_parameters)."""
    if not readings or not parameters_to_test:
        return False, []
    plan_by_name = {
        str(p.get("parameter", "")).strip().upper(): p
        for p in parameters_to_test
        if isinstance(p, dict)
    }
    failed: list[str] = []
    for r in readings:
        if not isinstance(r, dict):
            continue
        name = str(r.get("parameter", "")).strip().upper()
        spec = plan_by_name.get(name)
        if not spec:
            continue
        try:
            value = float(r.get("value"))
        except (TypeError, ValueError):
            failed.append(name)
            continue
        low = spec.get("lowLimit")
        high = spec.get("highLimit")
        if low is not None and value < float(low):
            failed.append(name)
            continue
        if high is not None and value > float(high):
            failed.append(name)
    return (len(failed) > 0, failed)


async def record_gas_reading(
    db: AsyncSession,
    *,
    permit_id: str,
    user_id: str,
    readings: list[dict[str, Any]],
    instrument_serial: str | None,
    is_pre_entry: bool,
) -> GasReadingResult:
    """Record a gas test reading. If exceedance, auto-suspend the permit."""
    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise ValueError("Permit not found")

    plan = (
        await db.execute(
            select(PermitGasTestPlan).where(PermitGasTestPlan.permitId == permit_id)
        )
    ).scalar_one_or_none()
    refresh_freq = plan.refreshFrequencyMinutes if plan else 120
    parameters = plan.parametersToTest if plan else None

    is_exc, failed = _evaluate_readings(readings, parameters)
    now = datetime.now(timezone.utc)
    refresh_due = now + timedelta(minutes=refresh_freq)

    row = PermitGasTestReading(
        permitId=permit_id,
        recordedById=user_id,
        readings=readings,
        isExceedance=is_exc,
        exceedanceAction="AUTO_SUSPEND" if is_exc else None,
        instrumentSerial=instrument_serial,
        isPreEntry=is_pre_entry,
        refreshDueBy=refresh_due,
    )
    db.add(row)
    await db.flush()

    auto_suspended = False
    if is_exc and permit.status == PermitStatus.ACTIVE:
        permit.status = PermitStatus.SUSPENDED
        permit.suspendedAt = now
        permit.suspendedReason = (
            f"Gas test exceedance: {', '.join(failed)} out of band"
        )
        permit.isCurrentlySuspended = True
        db.add(
            PermitSuspension(
                permitId=permit_id,
                suspendedById=user_id,
                reason="GAS_TEST_EXCEEDANCE",
                reasonDetail=f"Exceedance on: {', '.join(failed)}.",
                reFlraRequired=True,
            )
        )
        await db.flush()
        auto_suspended = True

    return GasReadingResult(
        reading_id=row.id,
        is_exceedance=is_exc,
        refresh_due_by=refresh_due,
        failed_parameters=failed,
        auto_suspended=auto_suspended,
    )


async def get_refresh_status(
    db: AsyncSession, permit_id: str
) -> dict[str, Any]:
    """Snapshot of the refresh state for the active-phase UI countdown."""
    plan = (
        await db.execute(
            select(PermitGasTestPlan).where(PermitGasTestPlan.permitId == permit_id)
        )
    ).scalar_one_or_none()

    last = (
        await db.execute(
            select(PermitGasTestReading)
            .where(PermitGasTestReading.permitId == permit_id)
            .order_by(PermitGasTestReading.recordedAt.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if plan is None:
        return {"hasGasPlan": False}

    return {
        "hasGasPlan": True,
        "refreshFrequencyMinutes": plan.refreshFrequencyMinutes,
        "instrumentSerial": plan.instrumentSerial,
        "parametersToTest": plan.parametersToTest,
        "lastReadingAt": last.recordedAt.isoformat() if last else None,
        "lastIsExceedance": bool(last.isExceedance) if last else False,
        "refreshDueBy": last.refreshDueBy.isoformat()
        if last and last.refreshDueBy
        else None,
        "lastReadingId": last.id if last else None,
    }
