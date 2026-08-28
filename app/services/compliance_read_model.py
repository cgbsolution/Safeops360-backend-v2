"""Checklist completion rate — ONE aggregation, two surfaces.

WHAT THIS IS
------------
Fire and Chemical checklist runs already ARE `CamsEngagement` rows carrying
`sourceModule='FIRE'` / `'CHEMICAL'` and a `periodLabel`. So completion rate is
an aggregation over tables that already exist; no separate read model, no
mirrored store, and nothing to keep in sync.

It is deliberately ONE function with two callers — the Compliance Snapshot
inside CAMS and the completion panel on the Operations side. Two
implementations of "completion rate" would drift, and the first anyone would
notice is an auditor being shown 82% on one screen and 91% on another for the
same asset.

THE DENOMINATOR IS WHAT IS OWED, NOT WHAT EXISTS
------------------------------------------------
The subtle failure here is counting engagement rows. A checklist nobody ever
opened has no engagement row at all, so `completed / rows_that_exist` scores a
site that did nothing as 100% — the most dangerous possible reading of a fire
compliance number.

So the denominator is derived from the cadence: for each asset, each APPROVED
template that applies to its type, each period overlapping the window, one
occurrence is owed. `periods_in_range` comes from the same frequency engine the
register grid and the reminder job use, so all three agree on what a period is.

"CANNOT BE COMPUTED" IS NOT ZERO
---------------------------------
`rate` is None when nothing was owed — no assets, no applicable template, or a
window before the register existed. It is NOT 0.0 and NOT 100.0. A site with no
fire assets has not failed its fire compliance and has not passed it; the
question does not apply. Every caller must render that as "no data", which is
the convention the Manhours/LTIFR fix established for exactly this reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select

from app.models.cams import CamsEngagement
from app.models.fire_safety import FireEquipment
from app.services import fire_checklist_admin as admin
from app.services import fire_checklists as svc

MODULE_FIRE = "FIRE"
MODULE_CHEMICAL = "CHEMICAL"
DEFAULT_MODULES = (MODULE_FIRE, MODULE_CHEMICAL)

# CAMS statuses that mean the sheet is signed off. Same mapping the checklist
# service uses for its APPROVED stage — not a second opinion about what "done"
# means, which is how two screens end up disagreeing.
COMPLETED_STATUSES = frozenset({"REPORT_ISSUED", "CLOSED"})


@dataclass
class Completion:
    """One completion figure. `rate` is None when nothing was owed."""

    owed: int = 0
    completed: int = 0
    # Runs that exist but are not signed off — started and abandoned, which is a
    # different failure from never started and worth separating.
    inProgress: int = 0

    @property
    def missing(self) -> int:
        return max(0, self.owed - self.completed - self.inProgress)

    @property
    def rate(self) -> float | None:
        if self.owed <= 0:
            return None
        return round(self.completed * 100.0 / self.owed, 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "owed": self.owed,
            "completed": self.completed,
            "inProgress": self.inProgress,
            "missing": self.missing,
            # None, never 0.0 — see the module docstring.
            "rate": self.rate,
            "computable": self.owed > 0,
        }


@dataclass
class ComplianceResult:
    window: dict[str, str]
    modules: list[str]
    overall: Completion = field(default_factory=Completion)
    byPlant: dict[str, Completion] = field(default_factory=dict)
    byAsset: dict[str, Completion] = field(default_factory=dict)
    assetMeta: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "modules": self.modules,
            "overall": self.overall.as_dict(),
            "byPlant": {k: v.as_dict() for k, v in self.byPlant.items()},
            "byAsset": {
                k: {**v.as_dict(), **self.assetMeta.get(k, {})} for k, v in self.byAsset.items()
            },
        }


def default_window(today: date | None = None, *, months: int = 3) -> tuple[date, date]:
    """The trailing window a compliance panel shows by default.

    Ends YESTERDAY, not today: the current period is still open, and counting an
    unfinished month against a site reports every register as failing on the 1st.
    """
    today = today or datetime.now(timezone.utc).date()
    end = today - timedelta(days=1)
    start = end - timedelta(days=30 * months)
    return start, end


async def compute(
    db,
    *,
    modules: Iterable[str] = DEFAULT_MODULES,
    plant_ids: Iterable[str] | None = None,
    asset_ids: Iterable[str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> ComplianceResult:
    """Completion rate over the window, overall and broken down.

    Both surfaces call this. Neither computes anything of its own.
    """
    mods = [m for m in modules]
    if start is None or end is None:
        start, end = default_window()

    result = ComplianceResult(
        window={"start": start.isoformat(), "end": end.isoformat()}, modules=mods
    )

    # ── what is owed ──
    stmt = select(FireEquipment).where(FireEquipment.isDeleted.is_(False))
    if plant_ids is not None:
        stmt = stmt.where(FireEquipment.plantId.in_(list(plant_ids) or [""]))
    if asset_ids is not None:
        stmt = stmt.where(FireEquipment.id.in_(list(asset_ids) or [""]))
    assets = (await db.execute(stmt)).scalars().all()
    if not assets:
        # Nothing owed → rate stays None. A site with no fire assets has neither
        # passed nor failed its fire compliance.
        return result

    templates = [t for t in await admin.list_templates(db) if t.status == "APPROVED"]
    by_type: dict[str, list[Any]] = {}
    for t in templates:
        meta = t.documentMeta or {}
        by_type.setdefault(meta.get("assetType"), []).append(t)

    # (assetId, templateId, period) → owed. Built first so the run query can be
    # a single fetch rather than one per occurrence.
    owed_keys: set[tuple[str, str, str]] = set()
    for asset in assets:
        for tpl in by_type.get(asset.type, []):
            meta = tpl.documentMeta or {}
            variant = meta.get("siteVariant")
            if variant and asset.assetSubtype and not _variant_ok(variant, asset.assetSubtype):
                continue
            frequency = meta.get("frequency", "MONTHLY")
            try:
                periods = svc.periods_in_range(frequency, start, end)
            except svc.ChecklistError:
                continue
            for period in periods:
                owed_keys.add((asset.id, tpl.id, period))

    if not owed_keys:
        return result

    # ── what was done ──
    template_ids = {tid for _, tid, _ in owed_keys}
    runs = (
        await db.execute(
            select(CamsEngagement)
            .where(CamsEngagement.sourceModule.in_(mods))
            .where(CamsEngagement.templateId.in_(list(template_ids)))
            .where(CamsEngagement.periodLabel.isnot(None))
        )
    ).scalars().all()
    status_by_key: dict[tuple[str, str, str], str] = {}
    for r in runs:
        key = (r.sourceEntityId, r.templateId, r.periodLabel)
        if key in owed_keys:
            status_by_key[key] = r.status

    meta_by_asset = {
        a.id: {
            "equipmentCode": a.equipmentCode,
            "location": a.location,
            "assetType": a.type,
            "plantId": a.plantId,
        }
        for a in assets
    }
    plant_of = {a.id: a.plantId for a in assets}

    for asset_id, template_id, period in owed_keys:
        status = status_by_key.get((asset_id, template_id, period))
        done = status in COMPLETED_STATUSES
        started = status is not None and not done and status != "CANCELLED"

        for bucket in (
            result.overall,
            result.byPlant.setdefault(plant_of[asset_id], Completion()),
            result.byAsset.setdefault(asset_id, Completion()),
        ):
            bucket.owed += 1
            if done:
                bucket.completed += 1
            elif started:
                bucket.inProgress += 1

    result.assetMeta = {k: meta_by_asset[k] for k in result.byAsset if k in meta_by_asset}
    return result


def _variant_ok(site_variant: str, subtype: str) -> bool:
    """A unit-variant sheet only applies to a panel with that addressing —
    the same rule the scan page and the reminder job apply, so an inapplicable
    sheet is never counted as owed and never drags a site's rate down."""
    v = (site_variant or "").upper()
    s = (subtype or "").upper()
    if "ZONE" in v:
        return "ZONE" in s or not s
    if "LOOP" in v:
        return "LOOP" in s or not s
    return True


__all__ = [
    "MODULE_FIRE", "MODULE_CHEMICAL", "DEFAULT_MODULES", "COMPLETED_STATUSES",
    "Completion", "ComplianceResult", "default_window", "compute",
]
