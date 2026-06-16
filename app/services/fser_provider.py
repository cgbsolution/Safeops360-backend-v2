"""FSER (Fire Safety & Emergency Response) provider interface.

BCM crisis plans READ emergency data through this provider — they do not model
or duplicate it (Phase-3 hard constraint #2). The real FSER module is a future
build; until then this provider returns curated site emergency data for the
Meridian demo plants and degrades gracefully (available=False) for any site
without data, so the crisis workspace FSER panel shows "unavailable" without
breaking (T3-14).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plant import Plant

# Curated FSER data keyed by plant code. When the real FSER module lands, swap
# this lookup for a call into it — the provider signature stays the same.
_FSER_BY_CODE: dict[str, dict] = {
    "NW": {
        "assemblyPoints": [
            {"name": "Assembly Point A — Main Gate Lawn", "capacity": 400, "wardenRole": "Site Incident Controller"},
            {"name": "Assembly Point B — North Car Park", "capacity": 250, "wardenRole": "Shift In-Charge"},
        ],
        "emergencyContacts": [
            {"name": "Site Fire Cell", "phone": "+91-100-NW-FIRE", "role": "On-site fire response"},
            {"name": "District Fire Brigade — Bharatpur", "phone": "101", "role": "External"},
            {"name": "Plant Medical Room", "phone": "+91-100-NW-MED", "role": "First response medical"},
        ],
        "sitePlanSummary": "North Works site emergency plan: evacuation via Gates 1/3, muster at AP-A/B, fire teams per shift, mutual-aid MoU with adjacent unit.",
    },
    "SW": {
        "assemblyPoints": [
            {"name": "Assembly Point 1 — SEZ Block C Frontage", "capacity": 350, "wardenRole": "Site Incident Controller"},
            {"name": "Assembly Point 2 — ETP Side Yard", "capacity": 180, "wardenRole": "Utilities Supervisor"},
        ],
        "emergencyContacts": [
            {"name": "Site Fire Cell", "phone": "+91-100-SW-FIRE", "role": "On-site fire response"},
            {"name": "SEZ Emergency Control", "phone": "+91-100-SEZ-EOC", "role": "Zone control room"},
            {"name": "District Fire Brigade — Nellore", "phone": "101", "role": "External"},
        ],
        "sitePlanSummary": "South Works site emergency plan: evacuation routes per the SEZ master plan, chlorine-leak SOP for chemical storage, ETP emergency bypass interlock, SCBA teams on every shift.",
    },
}


async def get_fser_panel(db: AsyncSession, site_id: str | None) -> dict | None:
    """Return the FSER panel for a site (by Plant.id). None / available=False when
    no FSER data exists — caller renders 'unavailable' and continues."""
    if not site_id:
        return {"available": False, "reason": "Corporate crisis — no single-site emergency plan."}
    try:
        plant = await db.get(Plant, site_id)
        if not plant:
            return {"available": False, "reason": "Site not found."}
        data = _FSER_BY_CODE.get(plant.code)
        if not data:
            return {"available": False, "reason": f"No FSER plan published for {plant.code}."}
        return {"available": True, "siteCode": plant.code, "siteName": plant.name, **data}
    except Exception:
        # Provider outage must never break the crisis workspace.
        return {"available": False, "reason": "FSER provider temporarily unavailable."}
