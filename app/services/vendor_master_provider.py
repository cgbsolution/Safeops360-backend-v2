"""Vendor-master provider interface (T3 constraint #2).

Tier 3 attaches a risk/ESG profile to existing vendor master data where a
procurement/master-data context exists, and owns a lightweight vendor entity
where it does not. For the Meridian demo tenant there is no separate vendor
master, so the provider reports unavailable and the module owns every vendor.

Swap this module's body for a real adapter (ERP/procurement master, CSV import)
without touching the router — same graceful-degradation contract as the FSER
provider.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

# Meridian: no external vendor master wired. Real deployments populate this from
# the procurement master via an adapter keyed by masterDataRef.
_EXTERNAL_MASTER: dict[str, dict[str, Any]] = {}


def provider_available() -> bool:
    """True when an external vendor master is wired for this tenant."""
    return bool(_EXTERNAL_MASTER)


async def lookup(db: AsyncSession, master_ref: str | None) -> dict[str, Any] | None:
    """Resolve a vendor-master record by ref. Returns None when the module owns
    the vendor entity (no external master / ref not found) — caller falls back to
    the module-owned VendorProfile."""
    if not master_ref:
        return None
    return _EXTERNAL_MASTER.get(master_ref)


async def list_unattached(db: AsyncSession) -> list[dict[str, Any]]:
    """External master vendors not yet given a risk/ESG profile (empty for Meridian)."""
    return list(_EXTERNAL_MASTER.values())
