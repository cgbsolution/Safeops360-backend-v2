"""Organisation-wide module allocation — the Super-Admin-managed layer that
sits *within* the signed-licence ceiling and *above* the per-factory layer.

This portal is single-tenant: one organisation (e.g. Page Industries), many
plants. The Super Admin decides which licensed modules the organisation uses at
all; plant admins then allocate what survives that decision to individual
factories. Effective access at a factory is therefore:

    usable at plant P  ==  is_module_enabled(code)          # signed ceiling
                           AND org-enabled(code)            # THIS layer
                           AND admin-enabled for P          # per-factory layer

So this layer can only ever RESTRICT within the licence — never grant a module
the licence doesn't include (build prompt §5.3).

Cache: moduleCode → Override(enabled, note). Only explicit rows are cached; a
module with NO row is on (inherited from the licence). Refreshed on boot and
after each Super Admin save, so the hot path never hits the DB — same contract
as factory_entitlements.

No validity window here on purpose. A per-factory grant is naturally
time-boxed ("this plant gets CAMS for the pilot quarter"); an organisation-level
decision is a standing yes/no, and a silently-expiring org module would look
like an outage to every plant at once.

── Sub-modules (screen-level) ────────────────────────────────────────────────
The same table also stores SCREEN-level decisions, keyed `NAV:<href>` (e.g.
`NAV:/cams/calendar`). A licensed module like CAMS owns fourteen screens; the
Super Admin can hide individual ones without giving up the module.

Two rules make this safe and keep the licence the only thing that grants:
  * a sub-module can only ever be hidden WITHIN a module the licence already
    grants — hiding a screen is always a restriction, never a grant;
  * the key is the nav href itself, so there is no second catalogue to drift
    out of step with the sidebar. The frontend owns the tree (it renders it);
    this layer only remembers which entries are switched off.

Sub-module control is a NAV + route-guard boundary, not an API one: the screens
under one module share that module's routers, so the API still enforces at
module granularity. Hiding a screen removes the route from the product; it does
not independently harden the endpoints behind it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal

log = logging.getLogger("safeops360.licensing")


@dataclass(frozen=True)
class OrgOverride:
    enabled: bool
    note: str | None


# moduleCode → OrgOverride. Only explicit rows are cached.
_overrides: dict[str, OrgOverride] = {}


def override_for(code: str) -> OrgOverride | None:
    return _overrides.get(code)


def is_enabled_for_org(code: str) -> bool:
    """Organisation-level restriction ONLY (the signed ceiling is checked
    separately by enforcement.is_module_enabled). True unless the Super Admin
    has explicitly turned `code` off for the organisation."""
    ov = _overrides.get(code)
    return True if ov is None else ov.enabled


# Sub-module rows are namespaced so module codes and screen hrefs can share one
# table without ever colliding — a module code is bare (`CAMS`), a screen is
# prefixed (`NAV:/cams/calendar`).
NAV_PREFIX = "NAV:"


def nav_key(href: str) -> str:
    """The storage key for a nav href. Trailing slashes are stripped so
    `/cams/` and `/cams` can't become two rows fighting each other."""
    return f"{NAV_PREFIX}{href.rstrip('/') or '/'}"


def is_nav_enabled(href: str) -> bool:
    """Screen-level restriction ONLY. True unless the Super Admin has hidden
    this exact nav entry. The caller must still check the parent module."""
    ov = _overrides.get(nav_key(href))
    return True if ov is None else ov.enabled


def disabled_codes() -> frozenset[str]:
    """MODULE codes the Super Admin has switched off org-wide. Used by the API
    to tell the frontend *why* a module is missing, so the UI can show the
    'contact your Super Admin' message rather than the licence-edition one.
    Excludes sub-module rows — see disabled_nav_hrefs()."""
    return frozenset(
        c for c, ov in _overrides.items() if not ov.enabled and not c.startswith(NAV_PREFIX)
    )


def disabled_nav_hrefs() -> frozenset[str]:
    """Screen hrefs the Super Admin has hidden, with the namespace stripped —
    the shape the sidebar and route guard consume directly."""
    return frozenset(
        c[len(NAV_PREFIX):]
        for c, ov in _overrides.items()
        if not ov.enabled and c.startswith(NAV_PREFIX)
    )


def note_for(code: str) -> str | None:
    """The Super Admin's internal note against a disabled module, if any. Never
    surfaced to ordinary users — the Super Admin screen only."""
    ov = _overrides.get(code)
    return ov.note if ov else None


async def refresh(db: AsyncSession | None = None) -> None:
    """Reload the organisation override cache from the DB."""
    if db is not None:
        await _load(db)
        return
    try:
        async with AsyncSessionLocal() as session:
            await _load(session)
    except Exception as e:  # noqa: BLE001 — never let this crash boot
        log.warning("Organisation-entitlement cache refresh failed: %s", e)


async def _load(db: AsyncSession) -> None:
    from app.models.licensing import OrganisationModuleEntitlement

    rows = (await db.execute(select(OrganisationModuleEntitlement))).scalars().all()
    fresh: dict[str, OrgOverride] = {
        r.moduleCode: OrgOverride(enabled=r.enabled, note=r.note) for r in rows
    }
    global _overrides
    _overrides = fresh
    off = sum(1 for o in fresh.values() if not o.enabled)
    log.info("Organisation-entitlement cache: %d row(s), %d module(s) off", len(fresh), off)


async def load_all(db: AsyncSession) -> dict[str, dict]:
    """All explicit rows keyed by module code (for the Super Admin screen)."""
    from app.models.licensing import OrganisationModuleEntitlement

    rows = (await db.execute(select(OrganisationModuleEntitlement))).scalars().all()
    return {
        r.moduleCode: {
            "enabled": r.enabled,
            "note": r.note,
            "updatedBy": r.updatedBy,
            "updatedAt": r.updatedAt.isoformat() if r.updatedAt else None,
        }
        for r in rows
    }


async def set_modules(
    db: AsyncSession, changes: dict[str, dict], updated_by: str | None
) -> None:
    """Upsert organisation-wide module state. `changes` maps moduleCode →
    {enabled: bool, note: str|None}. The caller must have validated the codes
    against the licence ceiling and confirmed the caller is a Super Admin.
    Refreshes the cache so the change takes effect on the next request."""
    from app.models.licensing import OrganisationModuleEntitlement

    existing = {
        r.moduleCode: r
        for r in (await db.execute(select(OrganisationModuleEntitlement))).scalars().all()
    }
    for code, spec in changes.items():
        enabled = bool(spec.get("enabled", True))
        note = spec.get("note") or None
        row = existing.get(code)
        if row is None:
            db.add(
                OrganisationModuleEntitlement(
                    moduleCode=code, enabled=enabled, note=note, updatedBy=updated_by
                )
            )
        else:
            row.enabled = enabled
            row.note = note
            row.updatedBy = updated_by
    await db.flush()
    await refresh(db)
