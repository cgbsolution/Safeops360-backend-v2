"""Branded fire registers, driven by `FireRegisterViewConfig` rather than by code.

WHAT THIS FIXES
---------------
`FireRegisterViewConfig` has existed, and been seeded with all three registers,
with nothing reading it. The extinguisher register rendered from a hardcoded
document constant and a hand-built table; the alarm-panel and hydrant registers
existed only as config rows nothing could reach. So "adding the next register is
a seed entry, not a screen" — the whole point of the table — was not true yet.

It is now. One resolver turns a config row into the same `{document, summary,
rows}` payload the extinguisher register already produced, so the existing PDF
and XLSX renderers, the doc-control header and the screen all work off config.

WHY THE EXTINGUISHER STILL HAS ITS OWN ROW BUILDER
--------------------------------------------------
Four of its sixteen columns — HP tested on / HP test due / Refilled on / Due for
refilling — are not columns on the asset at all. They are the issue and expiry
of the latest HYDROSTATIC_TEST and REFILL certificate, projected per row (see
`fire_register.py`, and the DELIBERATELY ABSENT block in models/fire_safety.py).
No generic field projection can produce them.

So the split is: the DOCUMENT is always config, and the ROWS come from a builder
chosen by asset type. That keeps the client's controlled sheet byte-identical
while everything else is genuinely config-driven — which is the honest shape,
rather than pretending one projection fits a document it does not fit.

WHAT A COLUMN KEY MAY REFER TO
------------------------------
Only a field this module actually projects. A config naming something else
renders blank rather than throwing — a register is a statutory document and it
must still print if one column is misconfigured — but `unmapped_columns()`
reports them, and the router surfaces that so a misconfigured register is
visible rather than quietly empty in one column.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.models.fire_safety import FireEquipment, FireRegisterViewConfig
from app.services import fire_register as fereg

EXTINGUISHER = fereg.EXTINGUISHER

# Template keys → which renderer draws the export. Held here so a config naming
# a key nothing implements is a caught error rather than a 500 halfway through a
# render, which is the reason `pdfTemplateKey` is a key and not a filename.
TEMPLATE_FE = "FE_REGISTER"
TEMPLATE_GENERIC = "GENERIC_REGISTER"
KNOWN_TEMPLATES = {TEMPLATE_FE, TEMPLATE_GENERIC}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime | None) -> str | None:
    if d is None:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).isoformat()


# ═══════════════════════════════════════════════════════════════════════════
# Config lookup
# ═══════════════════════════════════════════════════════════════════════════
async def list_configs(db, *, tenant_id: str | None = None) -> list[FireRegisterViewConfig]:
    """Every active register, most specific first.

    A tenant row shadows the platform default for the same asset type — that is
    what lets one client's controlled sheet differ without forking the screen.
    """
    rows = (
        await db.execute(
            select(FireRegisterViewConfig)
            .where(FireRegisterViewConfig.isActive.is_(True))
            .order_by(FireRegisterViewConfig.assetType.asc())
        )
    ).scalars().all()
    by_type: dict[str, FireRegisterViewConfig] = {}
    for r in rows:
        if r.tenantId not in (None, tenant_id):
            continue
        current = by_type.get(r.assetType)
        if current is None or (current.tenantId is None and r.tenantId is not None):
            by_type[r.assetType] = r
    return sorted(by_type.values(), key=lambda c: c.brandName)


async def config_for_slug(db, slug: str, *, tenant_id: str | None = None):
    return next((c for c in await list_configs(db, tenant_id=tenant_id) if c.routeSlug == slug), None)


async def config_for_type(db, asset_type: str, *, tenant_id: str | None = None):
    return next((c for c in await list_configs(db, tenant_id=tenant_id) if c.assetType == asset_type), None)


# ═══════════════════════════════════════════════════════════════════════════
# The document header — the doc-control block, straight from config
# ═══════════════════════════════════════════════════════════════════════════
def document_from_config(cfg: FireRegisterViewConfig) -> dict[str, Any]:
    """The header the screen prints and both exporters read.

    Same shape the hardcoded `FE_REGISTER_DOC` had, so `fire_checklist_pdf` and
    `fire_checklist_xlsx` need no new contract — the point of the retrofit is
    that the extinguisher output does not change.
    """
    return {
        "documentNo": cfg.documentNo,
        "supersedesNo": cfg.supersedesNo,
        "revision": cfg.revision,
        "effectiveDate": _iso(cfg.effectiveDate),
        "reviewDate": _iso(cfg.reviewDate),
        "title": cfg.brandName.upper(),
        "department": cfg.department,
        # [[key, label], …] as stored. The screen renders these, in this order.
        "columns": [
            [c.get("key"), c.get("label")] if isinstance(c, dict) else list(c)
            for c in (cfg.columns or [])
        ],
        "pdfTemplateKey": cfg.pdfTemplateKey if cfg.pdfTemplateKey in KNOWN_TEMPLATES else TEMPLATE_GENERIC,
        "assetType": cfg.assetType,
        "routeSlug": cfg.routeSlug,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Rows
# ═══════════════════════════════════════════════════════════════════════════
def generic_row(e: FireEquipment, *, sl_no: int | None = None,
                now: datetime | None = None) -> dict[str, Any]:
    """One row for any asset type that is not the extinguisher sheet.

    A flat projection of what `FireEquipment` actually holds. Nothing is invented
    here: a register asking for a field the asset does not carry gets None, and
    `unmapped_columns()` names it, because silently rendering an empty column on
    a statutory register is how a missing field survives an audit unnoticed.
    """
    now = now or _now()
    due = fereg._aware(e.nextInspectionDueDate)
    return {
        "id": e.id,
        "slNo": sl_no,
        "equipmentCode": e.equipmentCode,
        "assetSubtype": e.assetSubtype,
        "make": e.make,
        "model": e.model,
        "serialNo": e.serialNo,
        "capacity": e.capacitySpec,
        "location": e.location,
        "zoneId": e.zoneId,
        "installationDate": _iso(e.installationDate),
        "lastInspectionDate": _iso(e.lastInspectionDate),
        "nextInspectionDueDate": _iso(due),
        "status": e.status,
        "remarks": e.registerRemarks,
        "plantId": e.plantId,
        # The register's one computed column: the same red / amber-30 / green
        # ladder the extinguisher sheet uses, so a reader moving between the
        # three registers reads one visual rule, not three.
        "badges": {"inspection": fereg.badge_for(due, now)},
        "worstBadge": fereg.badge_for(due, now)["status"],
    }


def unmapped_columns(cfg: FireRegisterViewConfig, sample_row: dict[str, Any]) -> list[str]:
    """Column keys the row builder does not produce — a misconfiguration, named.

    Returned rather than raised: a register is a statutory document and must
    still print with one column blank. But it must not do so silently, because a
    blank column reads as "nothing recorded" rather than "nothing wired up".
    """
    keys = {c.get("key") if isinstance(c, dict) else c[0] for c in (cfg.columns or [])}
    return sorted(k for k in keys if k and k not in sample_row)


async def build_register(db, cfg: FireRegisterViewConfig, equipment: list[FireEquipment],
                         *, now: datetime | None = None) -> dict[str, Any]:
    """`{document, summary, rows}` for any configured register."""
    now = now or _now()
    document = document_from_config(cfg)
    # The overdue/escalated state, attached per row. Read here rather than in
    # each screen so the register, the alarm-panel register and the hydrant
    # register all show the same badge from the same source — an escalation
    # visible on one register and not another is worse than none at all.
    from app.services import fire_reminders as remsvc

    reminders = await remsvc.open_reminders_for_assets(db, [e.id for e in equipment])

    if cfg.assetType == EXTINGUISHER:
        # The controlled sheet, unchanged — certificate-projected columns and
        # the three due-date badges. Only its header now comes from config.
        payload = await fereg.build_register(db, equipment, now=now)
        payload["document"] = document
        for r in payload["rows"]:
            r["overdueChecklist"] = reminders.get(r["id"])
        payload["unmappedColumns"] = unmapped_columns(cfg, payload["rows"][0]) if payload["rows"] else []
        return payload

    ordered = sorted(equipment, key=lambda e: (e.location or "", e.equipmentCode))
    rows = [generic_row(e, sl_no=i, now=now) for i, e in enumerate(ordered, start=1)]
    for r in rows:
        r["overdueChecklist"] = reminders.get(r["id"])
    summary = {
        "total": len(rows),
        "overdue": sum(1 for r in rows if r["worstBadge"] == fereg.BADGE_OVERDUE),
        "dueSoon": sum(1 for r in rows if r["worstBadge"] == fereg.BADGE_DUE_SOON),
        "notRecorded": sum(1 for r in rows if r["worstBadge"] == fereg.BADGE_NONE),
    }
    return {
        "document": document,
        "summary": summary,
        "rows": rows,
        "unmappedColumns": unmapped_columns(cfg, rows[0]) if rows else [],
    }


__all__ = [
    "EXTINGUISHER", "TEMPLATE_FE", "TEMPLATE_GENERIC", "KNOWN_TEMPLATES",
    "list_configs", "config_for_slug", "config_for_type",
    "document_from_config", "generic_row", "unmapped_columns", "build_register",
]
