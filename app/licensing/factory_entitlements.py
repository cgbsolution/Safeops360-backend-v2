"""Per-factory (per-Plant) module allocation — the admin-managed layer that
sits *within* the signed-licence ceiling, now with an optional validity window.

An admin grants a factory a licensed module, optionally "for a period"
(validFrom..validUntil) or with no expiry. Effective access at a factory:

    usable at plant P  ==  is_module_enabled(code)            # signed ceiling
                           AND admin-enabled for P            # on/off
                           AND now within [validFrom, validUntil]   # window

So this layer can only ever RESTRICT within the licence — never grant a module
the licence doesn't include, and never reach beyond the licence's own validity.

Cache: plantId → { moduleCode → Override(enabled, valid_from, valid_until) }.
Only explicit rows are cached; a module with NO row is on with no time bound
(inherited from the licence). Refreshed on boot and after each admin save, so
the hot path never hits the DB. The window is evaluated against a caller-supplied
clock (the licence's monotonic effective clock) so a rollback can't extend it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal

log = logging.getLogger("safeops360.licensing")


@dataclass(frozen=True)
class Override:
    enabled: bool
    valid_from: datetime | None
    valid_until: datetime | None


# plantId → {moduleCode → Override}. Only explicit rows are cached.
_overrides: dict[str, dict[str, Override]] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def override_for(plant_id: str | None, code: str) -> Override | None:
    if not plant_id:
        return None
    return _overrides.get(plant_id, {}).get(code)


def is_enabled_for_plant(code: str, plant_id: str | None, now: datetime | None = None) -> bool:
    """Per-factory restriction + window check ONLY (the signed ceiling is
    checked separately by enforcement.is_module_enabled). True unless an admin
    has disabled `code` for this plant or the current time is outside its
    granted window."""
    ov = override_for(plant_id, code)
    if ov is None:
        return True  # no per-factory row → on, no time bound (licence default)
    if not ov.enabled:
        return False
    n = now or _utcnow()
    if ov.valid_from is not None and n < ov.valid_from:
        return False  # not started yet
    if ov.valid_until is not None and n > ov.valid_until:
        return False  # window has ended
    return True


def window_status(plant_id: str | None, code: str, now: datetime | None = None) -> str:
    """For the admin UI / diagnostics: ON | OFF | NOT_STARTED | EXPIRED | NO_ROW."""
    ov = override_for(plant_id, code)
    if ov is None:
        return "NO_ROW"
    if not ov.enabled:
        return "OFF"
    n = now or _utcnow()
    if ov.valid_from is not None and n < ov.valid_from:
        return "NOT_STARTED"
    if ov.valid_until is not None and n > ov.valid_until:
        return "EXPIRED"
    return "ON"


async def refresh(db: AsyncSession | None = None) -> None:
    """Reload the per-factory override cache from the DB."""
    if db is not None:
        await _load(db)
        return
    try:
        async with AsyncSessionLocal() as session:
            await _load(session)
    except Exception as e:  # noqa: BLE001 — never let this crash boot
        log.warning("Factory-entitlement cache refresh failed: %s", e)


async def _load(db: AsyncSession) -> None:
    from app.models.licensing import FactoryModuleEntitlement

    rows = (await db.execute(select(FactoryModuleEntitlement))).scalars().all()
    fresh: dict[str, dict[str, Override]] = {}
    for r in rows:
        fresh.setdefault(r.plantId, {})[r.moduleCode] = Override(
            enabled=r.enabled, valid_from=r.validFrom, valid_until=r.validUntil
        )
    global _overrides
    _overrides = fresh
    log.info("Factory-entitlement cache: %d plant(s) with overrides", len(fresh))


async def load_all(db: AsyncSession) -> list[dict]:
    """All explicit rows (for the admin matrix)."""
    from app.models.licensing import FactoryModuleEntitlement

    rows = (await db.execute(select(FactoryModuleEntitlement))).scalars().all()
    return [
        {
            "plantId": r.plantId,
            "moduleCode": r.moduleCode,
            "enabled": r.enabled,
            "validFrom": r.validFrom.isoformat() if r.validFrom else None,
            "validUntil": r.validUntil.isoformat() if r.validUntil else None,
        }
        for r in rows
    ]


async def set_for_plant(
    db: AsyncSession, plant_id: str, changes: dict[str, dict], updated_by: str | None
) -> None:
    """Upsert per-module state + window for a plant. `changes` maps moduleCode →
    {enabled: bool, validFrom: datetime|None, validUntil: datetime|None}. Caller
    must have validated the codes against the licence ceiling. Refreshes cache."""
    from app.models.licensing import FactoryModuleEntitlement

    existing = {
        r.moduleCode: r
        for r in (
            await db.execute(
                select(FactoryModuleEntitlement).where(
                    FactoryModuleEntitlement.plantId == plant_id
                )
            )
        ).scalars().all()
    }
    for code, spec in changes.items():
        enabled = bool(spec.get("enabled", True))
        vf = spec.get("validFrom")
        vu = spec.get("validUntil")
        row = existing.get(code)
        if row is None:
            db.add(
                FactoryModuleEntitlement(
                    plantId=plant_id, moduleCode=code, enabled=enabled,
                    validFrom=vf, validUntil=vu, updatedBy=updated_by,
                )
            )
        else:
            row.enabled = enabled
            row.validFrom = vf
            row.validUntil = vu
            row.updatedBy = updated_by
    await db.flush()
    await refresh(db)
