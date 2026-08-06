"""Config-driven statutory inspection frequency — Fire & Life Safety spec §5.1.

Before this module, an asset's inspection cadence was `FireEquipment
.inspectionFrequencyDays`, an integer defaulting to 30 that was written once at
create time and never revisited. That has three problems worth naming:

  1. **A regulatory remap is a code change.** Extending to GCC means every
     extinguisher's cadence changes; with the interval baked into rows there is
     no rule to re-point, only 40,000 rows to rewrite.
  2. **There is no citation.** "Why is this asset on 90 days?" has no answer the
     inspector can show a regulator. `regulatoryReference` on the master row is
     that answer, and it travels with the due date.
  3. **A per-asset exception is indistinguishable from a default.** 30 days
     because that is the rule, and 30 days because nobody changed the default,
     look identical in the data.

Resolution is most-specific-wins, and every resolution reports *which* rule it
matched so the answer is auditable rather than merely correct:

    plantId + assetType + assetSubtype     site rule for CO2 extinguishers
    plantId + assetType                    site rule for all extinguishers
    region  + assetType + assetSubtype     regional rule, subtype-specific
    region  + assetType                    the seeded NBC 2016 defaults
    (no match)                             PLATFORM_FALLBACK_DAYS, flagged unresolved

A per-asset override still exists — `frequencyOverrideReason` on the asset — but
it must now carry a reason, and `resolve()` reports it as an override rather than
letting it masquerade as policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fire_safety import FireEquipment, InspectionFrequencyMaster

# The interval each frequency enum means, in days. Centralised so two config rows
# both claiming QUARTERLY can never resolve to different intervals — the failure
# mode of letting each caller do its own arithmetic.
FREQUENCY_DAYS: dict[str, int] = {
    "WEEKLY": 7,
    "MONTHLY": 30,
    "QUARTERLY": 90,
    "HALF_YEARLY": 182,
    "ANNUAL": 365,
}

# Used only when no master row matches at all. Deliberately conservative (monthly)
# and always reported with `resolved=False`, because an unresolved asset is a
# config gap to fix, not a cadence to trust.
PLATFORM_FALLBACK_DAYS = 30

# The region assumed when a caller does not supply one. Extending to GCC means
# seeding region='GCC' rows and passing region='GCC' — no code change here.
DEFAULT_REGION = "IN"


@dataclass(frozen=True)
class FrequencyResolution:
    """Why an asset is on the cadence it is on. Every field is renderable."""

    days: int
    frequency: str
    source: str  # PLANT_SUBTYPE | PLANT_TYPE | REGION_SUBTYPE | REGION_TYPE | ASSET_OVERRIDE | FALLBACK
    masterId: str | None = None
    regulatoryReference: str | None = None
    checklistTemplateId: str | None = None
    auditTypeId: str | None = None
    leadTimeDays: int = 7
    resolved: bool = True
    overrideReason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "frequency": self.frequency,
            "source": self.source,
            "masterId": self.masterId,
            "regulatoryReference": self.regulatoryReference,
            "checklistTemplateId": self.checklistTemplateId,
            "auditTypeId": self.auditTypeId,
            "leadTimeDays": self.leadTimeDays,
            "resolved": self.resolved,
            "overrideReason": self.overrideReason,
        }


def interval_days(row: InspectionFrequencyMaster) -> int:
    """Days for a master row. CUSTOM reads `customIntervalDays`; the DB CHECK
    guarantees it is present, so a missing one here means the row bypassed the
    constraint and monthly is the safe read."""
    if row.frequency == "CUSTOM":
        return row.customIntervalDays or PLATFORM_FALLBACK_DAYS
    return FREQUENCY_DAYS.get(row.frequency, PLATFORM_FALLBACK_DAYS)


def _specificity(row: InspectionFrequencyMaster, plant_id: str | None) -> int:
    """Rank a candidate row. Higher wins. Kept pure so the precedence order is
    unit-testable without a database."""
    plant_match = row.plantId is not None and row.plantId == plant_id
    subtype_match = row.assetSubtype is not None
    if plant_match and subtype_match:
        return 4
    if plant_match:
        return 3
    if subtype_match:
        return 2
    return 1


_SOURCE_FOR_RANK = {
    4: "PLANT_SUBTYPE",
    3: "PLANT_TYPE",
    2: "REGION_SUBTYPE",
    1: "REGION_TYPE",
}


async def _candidates(
    db: AsyncSession, *, asset_type: str, asset_subtype: str | None, plant_id: str | None, region: str
) -> list[InspectionFrequencyMaster]:
    """Every active rule that *could* apply. One query, filtered in Python — the
    candidate set per asset type is a handful of rows, so a five-way UNION would
    cost more than it saves."""
    stmt = (
        select(InspectionFrequencyMaster)
        .where(InspectionFrequencyMaster.assetType == asset_type)
        .where(InspectionFrequencyMaster.region == region)
        .where(InspectionFrequencyMaster.isActive.is_(True))
        .where(InspectionFrequencyMaster.isDeleted.is_(False))
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        r
        for r in rows
        # A plant-scoped rule for a *different* plant must not apply here.
        if (r.plantId is None or r.plantId == plant_id)
        # A subtype-scoped rule only applies to that subtype.
        and (r.assetSubtype is None or r.assetSubtype == asset_subtype)
    ]


async def resolve(
    db: AsyncSession,
    *,
    asset_type: str,
    asset_subtype: str | None = None,
    plant_id: str | None = None,
    region: str = DEFAULT_REGION,
    asset_override_days: int | None = None,
    asset_override_reason: str | None = None,
) -> FrequencyResolution:
    """Resolve the cadence for one asset, with its provenance.

    A per-asset override wins over config — an asset genuinely can need a tighter
    cycle than its class — but ONLY when it carries a reason. An override without
    one is treated as leftover default data rather than a decision, which is what
    it almost always is on rows created before this module existed.
    """
    if asset_override_days and (asset_override_reason or "").strip():
        return FrequencyResolution(
            days=asset_override_days,
            frequency="CUSTOM",
            source="ASSET_OVERRIDE",
            overrideReason=asset_override_reason,
        )

    rows = await _candidates(
        db, asset_type=asset_type, asset_subtype=asset_subtype, plant_id=plant_id, region=region
    )
    if not rows:
        return FrequencyResolution(
            days=PLATFORM_FALLBACK_DAYS,
            frequency="MONTHLY",
            source="FALLBACK",
            resolved=False,
        )

    rank, best = max(((_specificity(r, plant_id), r) for r in rows), key=lambda t: t[0])
    return FrequencyResolution(
        days=interval_days(best),
        frequency=best.frequency,
        source=_SOURCE_FOR_RANK.get(rank, "REGION_TYPE"),
        masterId=best.id,
        regulatoryReference=best.regulatoryReference,
        checklistTemplateId=best.checklistTemplateId,
        auditTypeId=best.auditTypeId,
        leadTimeDays=best.leadTimeDays,
    )


async def resolve_for_equipment(
    db: AsyncSession, equipment: FireEquipment, *, region: str = DEFAULT_REGION
) -> FrequencyResolution:
    """`resolve()` for an already-loaded asset row."""
    return await resolve(
        db,
        asset_type=equipment.type,
        asset_subtype=equipment.assetSubtype,
        plant_id=equipment.plantId,
        region=region,
        asset_override_days=equipment.inspectionFrequencyDays,
        asset_override_reason=equipment.frequencyOverrideReason,
    )


async def resolve_many(
    db: AsyncSession, equipment: list[FireEquipment], *, region: str = DEFAULT_REGION
) -> dict[str, FrequencyResolution]:
    """Batch resolution for the nightly recompute.

    Loads the master table once and resolves in memory. The per-asset version
    would issue one query per asset — at 40,000 assets that is the difference
    between a batch job that finishes and one that does not.
    """
    stmt = (
        select(InspectionFrequencyMaster)
        .where(InspectionFrequencyMaster.region == region)
        .where(InspectionFrequencyMaster.isActive.is_(True))
        .where(InspectionFrequencyMaster.isDeleted.is_(False))
    )
    all_rows = (await db.execute(stmt)).scalars().all()

    by_type: dict[str, list[InspectionFrequencyMaster]] = {}
    for r in all_rows:
        by_type.setdefault(r.assetType, []).append(r)

    out: dict[str, FrequencyResolution] = {}
    for eq in equipment:
        if eq.inspectionFrequencyDays and (eq.frequencyOverrideReason or "").strip():
            out[eq.id] = FrequencyResolution(
                days=eq.inspectionFrequencyDays,
                frequency="CUSTOM",
                source="ASSET_OVERRIDE",
                overrideReason=eq.frequencyOverrideReason,
            )
            continue
        cands = [
            r
            for r in by_type.get(eq.type, [])
            if (r.plantId is None or r.plantId == eq.plantId)
            and (r.assetSubtype is None or r.assetSubtype == eq.assetSubtype)
        ]
        if not cands:
            out[eq.id] = FrequencyResolution(
                days=PLATFORM_FALLBACK_DAYS, frequency="MONTHLY", source="FALLBACK", resolved=False
            )
            continue
        rank, best = max(((_specificity(r, eq.plantId), r) for r in cands), key=lambda t: t[0])
        out[eq.id] = FrequencyResolution(
            days=interval_days(best),
            frequency=best.frequency,
            source=_SOURCE_FOR_RANK.get(rank, "REGION_TYPE"),
            masterId=best.id,
            regulatoryReference=best.regulatoryReference,
            checklistTemplateId=best.checklistTemplateId,
            auditTypeId=best.auditTypeId,
            leadTimeDays=best.leadTimeDays,
        )
    return out


async def coverage_gaps(db: AsyncSession, *, region: str = DEFAULT_REGION) -> list[dict[str, Any]]:
    """Asset types present in the register with no frequency rule covering them.

    Surfaced on the admin config screen. An unconfigured type is not a harmless
    blank: those assets silently fall back to 30 days and read as "compliant" on
    every dashboard, which is the worst kind of wrong.
    """
    types = (
        await db.execute(
            select(FireEquipment.type, FireEquipment.plantId)
            .where(FireEquipment.isDeleted.is_(False))
            .where(FireEquipment.isActive.is_(True))
            .distinct()
        )
    ).all()
    configured = set(
        (
            await db.execute(
                select(InspectionFrequencyMaster.assetType)
                .where(InspectionFrequencyMaster.region == region)
                .where(InspectionFrequencyMaster.isActive.is_(True))
                .where(InspectionFrequencyMaster.isDeleted.is_(False))
            )
        )
        .scalars()
        .all()
    )
    gaps: dict[str, set[str]] = {}
    for asset_type, plant_id in types:
        if asset_type not in configured:
            gaps.setdefault(asset_type, set()).add(plant_id)
    return [
        {"assetType": t, "plantIds": sorted(p), "region": region, "fallbackDays": PLATFORM_FALLBACK_DAYS}
        for t, p in sorted(gaps.items())
    ]


__all__ = [
    "FREQUENCY_DAYS",
    "PLATFORM_FALLBACK_DAYS",
    "DEFAULT_REGION",
    "FrequencyResolution",
    "interval_days",
    "resolve",
    "resolve_for_equipment",
    "resolve_many",
    "coverage_gaps",
]
