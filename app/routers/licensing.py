"""Licensing status & management API. Mounts at /api/licensing.

This router is CORE / always reachable — it is the screen a client lands on when
the app is locked (EXPIRED_LOCKED / INVALID / MISSING), so it must never itself
be gated by an entitlement. It exposes:

  GET  /api/licensing/status        entitlement + status view (admins get full)
  GET  /api/licensing/modules       caller's enabled module set (nav gating)
  GET  /api/licensing/installation  installationId + binding (admin)
  GET  /api/licensing/diagnostics   validation detail + tamper warnings (admin)
  POST /api/licensing/upload        upload/renew a .lic; validates then publishes (admin)
  POST /api/licensing/revalidate    force a re-validation pass (admin)
  GET  /api/licensing/organisation-modules  org-wide module allocation (super admin)
  PUT  /api/licensing/organisation-modules  set org-wide module allocation (super admin)

Entitlements are READ-ONLY here — they come only from the signed licence. No
endpoint in this app can grant a module; only uploading a validly-signed
licence changes entitlements (build prompt §5.3). The organisation and
per-factory endpoints can only RESTRICT within that ceiling.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import get_current_user
from app.licensing import factory_entitlements, keys, org_entitlements
from app.licensing.editions import get_edition
from app.licensing.enforcement import is_module_enabled_for_org, is_module_enabled_for_plant
from app.licensing.registry import CORE_MODULE_CODES, MODULE_REGISTRY
from app.licensing.state import (
    evaluate_dry_run,
    get_state,
    read_installation_identity,
    refresh_state,
    write_licence_token,
)
from app.models.user import User
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/licensing", tags=["licensing"])

# Permission codes that mark a licence administrator. LICENSING.MANAGE is the
# canonical one (seeded for ADMIN); the CONFIGURATION.* fallbacks let it
# work for system admins even before a reseed.
_ADMIN_PERMS = (
    "LICENSING.MANAGE",
    "CONFIGURATION.PERMISSIONS",
    "CONFIGURATION.ROLES",
    "CONFIGURATION.USERS",
)


async def _is_admin(db: AsyncSession, user: User) -> bool:
    for code in _ADMIN_PERMS:
        if (await can(db, user.id, code, PermissionContext())).allowed:
            return True
    # Role-code fallback for setups where the licence permission isn't seeded.
    return bool(user.role) and "ADMIN" in (user.role or "").upper()


async def _require_admin(db: AsyncSession, user: User) -> None:
    if not await _is_admin(db, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Licence administration requires admin rights")


# ── Super Admin (organisation owner) ─────────────────────────────────────────
# Deliberately NARROWER than _is_admin: a System Admin runs the portal, but only
# the Super Admin decides which modules the organisation as a whole uses. There
# is no "ADMIN in role name" fallback here — that would let every *_ADMIN role
# switch modules off for every plant at once.
SUPER_ADMIN_ROLE_CODE = "SUPER_ADMIN"
SUPER_ADMIN_PERMISSION = "ORGANISATION.MODULES"


async def _is_super_admin(db: AsyncSession, user: User) -> bool:
    """True for the organisation owner. Three independent paths, any of which
    suffices, so an RBAC edit can never orphan the organisation:
      1. the ORGANISATION.MODULES permission (the canonical grant),
      2. the SUPER_ADMIN role code on the user or any of their role rows,
      3. the configured anchor email (SUPER_ADMIN_EMAIL) — break-glass.
    """
    if (await can(db, user.id, SUPER_ADMIN_PERMISSION, PermissionContext())).allowed:
        return True
    if (user.role or "").upper() == SUPER_ADMIN_ROLE_CODE:
        return True
    from app.services.permissions import get_user_role_codes

    if SUPER_ADMIN_ROLE_CODE in await get_user_role_codes(db, user.id):
        return True
    anchor = (get_settings().super_admin_email or "").strip().lower()
    return bool(anchor) and (user.email or "").strip().lower() == anchor


async def _require_super_admin(db: AsyncSession, user: User) -> None:
    if not await _is_super_admin(db, user):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={
                "error": "super_admin_required",
                "message": (
                    "Organisation-wide module access is managed by the Super Admin. "
                    "Please contact your Super Admin to request a change."
                ),
            },
        )


# ── view builders ────────────────────────────────────────────────────────────
def _module_view(codes: set[str]) -> list[dict[str, Any]]:
    """Enabled product modules grouped for the entitlements panel."""
    out: list[dict[str, Any]] = []
    for code in sorted(codes):
        mod = MODULE_REGISTRY.get(code)
        if mod is None or mod.is_core:
            continue
        out.append({"code": mod.code, "name": mod.name, "group": mod.group})
    return out


def _public_status(state) -> dict[str, Any]:
    p = state.payload
    edition = get_edition(p.edition) if p else None
    return {
        "status": state.status,
        "isOperational": state.is_operational,
        "isLocked": state.is_locked,
        "daysToExpiry": state.days_to_expiry,
        "edition": p.edition if p else None,
        "editionName": edition.name if edition else None,
        "customerName": p.customer_name if p else None,
        "licenceType": p.licence_type if p else None,
        "deploymentMode": p.deployment_mode if p else None,
        "validFrom": p.valid_from.isoformat() if p else None,
        "validUntil": p.valid_until.isoformat() if p else None,
        "gracePeriodDays": p.grace_period_days if p else None,
        "warnDaysWindow": get_settings().licence_warn_days,
        "enabledModules": _module_view(state.enabled_module_set),
        "limits": {
            "maxSites": p.limits.max_sites if p else None,
            "maxUsers": p.limits.max_users if p else None,
            "maxFactories": p.limits.max_factories if p else None,
        } if p else {},
        "featureFlags": p.feature_flags if p else {},
        # Licensed modules the Super Admin has switched off org-wide. The UI
        # uses this to explain a missing module as "ask your Super Admin"
        # rather than "not in your edition" — two very different next steps.
        "orgDisabledModules": sorted(
            c for c in org_entitlements.disabled_codes() if c in state.enabled_module_set
        ),
    }


async def _usage_counts(db: AsyncSession) -> dict[str, int]:
    """Current usage for the limits panel (cap vs current)."""
    from app.models.factory import FactoryProfile
    from app.models.plant import Plant

    users = (await db.execute(select(func.count()).select_from(User))).scalar() or 0
    plants = (await db.execute(select(func.count()).select_from(Plant))).scalar() or 0
    try:
        factories = (await db.execute(select(func.count()).select_from(FactoryProfile))).scalar() or 0
    except Exception:  # noqa: BLE001 — facilities table may be absent in a carve-out
        factories = 0
    return {"users": int(users), "sites": int(plants), "factories": int(factories)}


# ── endpoints ────────────────────────────────────────────────────────────────
@router.get("/status")
async def licence_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Status + entitlements. Every authenticated user sees their enabled
    modules + status; only admins see diagnostics, installationId, and usage."""
    state = get_state()
    view = _public_status(state)
    is_admin = await _is_admin(db, user)
    view["isAdmin"] = is_admin
    view["isSuperAdmin"] = await _is_super_admin(db, user)

    if is_admin:
        view["usage"] = await _usage_counts(db)
        identity = await read_installation_identity(db)
        view["installationId"] = identity.installation_id if identity else None
        view["clockTamperWarning"] = state.clock_tamper_warning
        view["bindingWarning"] = state.binding_warning
        view["lastValidatedAt"] = state.last_validated_at.isoformat()
        view["validationError"] = state.validation_error
    return view


@router.get("/modules")
async def my_modules(
    plantId: str | None = Query(default=None),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The caller's enabled module set — consumed by frontend nav/route gating.
    Always excludes modules the Super Admin has switched off org-wide. When
    `plantId` is given, returns the EFFECTIVE set for that factory (signed
    ceiling minus org-wide restrictions minus the admin's per-factory
    restrictions). Returns codes only.

    `orgDisabledModules` is returned alongside so the route guard can tell a
    module that's missing because the organisation turned it off (ask your
    Super Admin) from one that's missing because the licence never had it
    (contact Vizionforge) — same absence, different message."""
    state = get_state()
    ceiling = sorted(state.enabled_module_set)
    if plantId:
        effective = [c for c in ceiling if is_module_enabled_for_plant(c, plantId, state)]
    else:
        effective = [c for c in ceiling if is_module_enabled_for_org(c, state)]
    return {
        "status": state.status,
        "isOperational": state.is_operational,
        "plantId": plantId,
        "enabledModules": effective,
        "orgDisabledModules": sorted(
            c for c in org_entitlements.disabled_codes() if c in state.enabled_module_set
        ),
        # Individual screens the Super Admin has hidden, as nav hrefs. Consumed
        # by the sidebar and the route guard; the API boundary stays at module
        # granularity (screens under one module share its routers).
        "disabledSubModules": sorted(org_entitlements.disabled_nav_hrefs()),
    }


@router.get("/installation")
async def installation_info(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require_admin(db, user)
    identity = await read_installation_identity(db)
    if identity is None:
        return {"installationId": None}
    state = get_state()
    bound = state.payload.installation_binding if state.payload else None
    return {
        "installationId": identity.installation_id,
        "firstBootAt": identity.first_boot_at.isoformat(),
        "lastSeenTimestamp": identity.last_seen_timestamp.isoformat(),
        "licenceBoundTo": bound,
        "bindingMatches": (bound is None) or (bound == identity.installation_id),
        "bindingWarning": state.binding_warning,
    }


@router.get("/diagnostics")
async def diagnostics(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require_admin(db, user)
    state = get_state()
    return {
        "status": state.status,
        "validationError": state.validation_error,
        "lastValidatedAt": state.last_validated_at.isoformat(),
        "effectiveClock": state.effective_clock.isoformat() if state.effective_clock else None,
        "clockTamperWarning": state.clock_tamper_warning,
        "bindingWarning": state.binding_warning,
        "trustedKeyIds": keys.trusted_kids(),
        "licenceJti": state.payload.jti if state.payload else None,
        "signingKid": None,  # the kid is in the token header; surfaced via re-eval
    }


@router.post("/upload")
async def upload_licence(
    payload: dict = Body(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Upload / renew a licence. The token is VALIDATED before it is persisted,
    so a bad upload can never clobber a working licence (build prompt §7)."""
    await _require_admin(db, user)
    token = (payload.get("licence") or payload.get("token") or "").strip()
    if not token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing 'licence' field")

    # Dry-run against the real installation identity (so a strict binding to a
    # different install is correctly rejected here, not silently persisted).
    trial = await evaluate_dry_run(db, token)
    if trial.status in {"INVALID", "MISSING"}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_licence",
                "message": "The uploaded licence failed validation and was NOT applied.",
                "detail": trial.validation_error,
            },
        )

    write_licence_token(token)
    state = await refresh_state(db)
    return {
        "applied": True,
        "message": f"Licence applied. Status is now {state.status}.",
        "status": _public_status(state),
    }


@router.get("/export")
async def export_my_data(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Data-portability export — a permitted action even when the app is locked
    (build prompt §7), so a client never loses access to their own data. Returns
    the organisation's foundational records plus a per-table row-count manifest
    so a full DB dump can be requested with confidence. Admin-only."""
    await _require_admin(db, user)
    from app.models.masters import Department
    from app.models.plant import Plant

    plants = (await db.execute(select(Plant))).scalars().all()
    departments = (await db.execute(select(Department))).scalars().all()
    users = (await db.execute(select(User))).scalars().all()

    # Row-count manifest across the major tables (best-effort; a missing table
    # in a carve-out simply reports 0).
    manifest_tables = [
        "Observation", "NearMiss", "Incident", "Permit", "HiraEntry", "Capa",
        "ComplianceAudit", "CamsEngagement", "FactoryProfile", "EnterpriseRisk",
        "TrainingRecord", "PpeItem", "Manhours",
    ]
    manifest: dict[str, int] = {}
    for tbl in manifest_tables:
        try:
            # tbl is from the hardcoded allowlist above, never user input.
            n = (await db.execute(text(f'SELECT count(*) FROM "{tbl}"'))).scalar() or 0  # noqa: S608
            manifest[tbl] = int(n)
        except Exception:  # noqa: BLE001
            manifest[tbl] = 0

    state = get_state()
    return {
        "exportType": "safeops360.data-portability.v1",
        "licence": {
            "customerName": state.payload.customer_name if state.payload else None,
            "edition": state.payload.edition if state.payload else None,
            "status": state.status,
        },
        "organisation": {
            "plants": [{"id": p.id, "code": p.code, "name": p.name} for p in plants],
            "departments": [{"id": d.id, "name": d.name} for d in departments],
            "users": [
                {"id": u.id, "name": u.name, "email": u.email, "role": u.role}
                for u in users
            ],
        },
        "recordCounts": manifest,
        "note": (
            "Foundational records + row-count manifest. For a full per-module "
            "data dump, contact your administrator with this manifest."
        ),
    }


@router.get("/organisation-modules")
async def organisation_modules(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The organisation-wide module allocation (Super Admin). Returns the FULL
    product-module catalogue — every module this portal ships — each flagged
    with whether the signed licence includes it.

    Listing the whole catalogue (rather than only the licensed subset) is
    deliberate: the Super Admin owns the organisation and needs to see every
    module that exists, including the ones the current licence doesn't cover.
    `licensed: false` modules are already blocked by the ceiling, so their
    org-level setting is inert — but it is stored, so the moment a renewed
    licence adds the module the organisation's decision is already in force
    rather than defaulting to on.

    Core modules are excluded: identity, RBAC, licensing and the dashboard shell
    can never be switched off by anyone (TL-14)."""
    await _require_super_admin(db, user)
    from app.models.plant import Plant

    state = get_state()
    catalogue = [
        MODULE_REGISTRY[c]
        for c in sorted(MODULE_REGISTRY)
        if c not in CORE_MODULE_CODES
    ]
    rows = await org_entitlements.load_all(db)
    plant_count = (await db.execute(select(func.count()).select_from(Plant))).scalar() or 0

    return {
        # The organisation this portal serves. Single-tenant, so it comes from
        # the signed licence's customer name (e.g. "Page Industries").
        "organisation": {
            "name": state.payload.customer_name if state.payload else None,
            "edition": state.payload.edition if state.payload else None,
            "plantCount": int(plant_count),
        },
        "modules": [
            {
                "code": m.code,
                "name": m.name,
                "group": m.group,
                # No row → on, inherited from the licence.
                "enabled": rows.get(m.code, {}).get("enabled", True),
                # False = outside the signed licence. Shown so the catalogue is
                # complete and honest; the toggle is stored but has no effect
                # until a licence including the module is uploaded.
                "licensed": m.code in state.enabled_module_set,
                "note": rows.get(m.code, {}).get("note"),
                "updatedBy": rows.get(m.code, {}).get("updatedBy"),
                "updatedAt": rows.get(m.code, {}).get("updatedAt"),
            }
            for m in catalogue
        ],
        # Screen-level decisions, as bare hrefs (e.g. "/cams/calendar"). The
        # frontend owns the nav tree and renders it; this is only the set the
        # Super Admin has switched off, so there is no second catalogue to drift.
        "disabledSubModules": sorted(org_entitlements.disabled_nav_hrefs()),
    }


@router.put("/organisation-modules")
async def update_organisation_modules(
    payload: dict = Body(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Set organisation-wide module states (Super Admin). Body:
        { "modules": { "CAMS": true, "PTW": {"enabled": false, "note": "..."} } }

    Any known product module may be set, licensed or not. This does NOT breach
    the config-can't-grant rule: every write here is a restriction, and the
    effective check is `licence AND org AND factory`, so recording a module as
    org-enabled can never make an unlicensed module reachable — the ceiling
    still fails it. Storing the decision for an unlicensed module is the point:
    when a renewed licence adds it, the organisation's choice already applies
    instead of silently defaulting to on.

    Core modules are rejected outright — identity, RBAC, licensing and the
    dashboard shell must stay reachable under every state (TL-14), so nobody,
    Super Admin included, can switch them off.

    Turning a licensed module off disables it at EVERY plant immediately; users
    hitting it get 'Please contact your Super Admin to request access to this
    module.'"""
    await _require_super_admin(db, user)
    changes = payload.get("modules") or {}
    sub_changes = payload.get("subModules") or {}
    if not isinstance(changes, dict) or not isinstance(sub_changes, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "modules / subModules must be objects")
    if not changes and not sub_changes:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Expected { modules: {code: bool|spec} } and/or { subModules: {href: bool} }",
        )

    # Sub-module keys are nav hrefs owned by the frontend, so there is no
    # registry to validate against — only the shape. Anything not starting with
    # "/" is a client bug, not a legitimate screen.
    bad_href = [h for h in sub_changes if not isinstance(h, str) or not h.startswith("/")]
    if bad_href:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "bad_submodule_key",
                "message": "Sub-module keys must be nav paths starting with '/'.",
                "keys": bad_href,
            },
        )

    unknown = [c for c in changes if c not in MODULE_REGISTRY]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "unknown_module",
                "message": "Unknown module code — not present in the module registry.",
                "modules": unknown,
            },
        )
    core = [c for c in changes if c in CORE_MODULE_CODES]
    if core:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "core_module_immutable",
                "message": "Core modules are always on and cannot be disabled.",
                "modules": core,
            },
        )

    state = get_state()

    # Accept either a bare bool (on/off) or a spec object {enabled, note}.
    normalised: dict[str, dict] = {}
    for code, v in changes.items():
        if isinstance(v, bool):
            normalised[code] = {"enabled": v, "note": None}
            continue
        if not isinstance(v, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Bad value for {code}")
        note = v.get("note")
        if note is not None and not isinstance(note, str):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{code}: note must be text")
        normalised[code] = {"enabled": bool(v.get("enabled", True)), "note": (note or "").strip() or None}

    # Screen-level entries share the same store under the NAV: namespace.
    for href, v in sub_changes.items():
        enabled = v if isinstance(v, bool) else bool((v or {}).get("enabled", True))
        normalised[org_entitlements.nav_key(href)] = {"enabled": enabled, "note": None}

    # get_db commits the request transaction on return (same contract the
    # per-factory handler relies on); set_modules refreshes the hot-path cache.
    await org_entitlements.set_modules(db, normalised, user.id)
    disabled = sorted(c for c in changes if not normalised[c]["enabled"])
    sub_off = sorted(h for h in sub_changes if not normalised[org_entitlements.nav_key(h)]["enabled"])
    # Changes to modules the licence doesn't cover are stored but inert — say so
    # rather than let the Super Admin believe they just turned something off
    # that was never reachable in the first place.
    inert = sorted(c for c in changes if c not in state.enabled_module_set)
    message = (
        f"Saved. {len(disabled)} module(s) and {len(sub_off)} screen(s) are now off"
        " for the whole organisation."
        if (disabled or sub_off)
        else "Saved. All selected modules and screens are available across the organisation."
    )
    if inert:
        message += (
            f" {len(inert)} of these are not in the current licence, so the setting"
            " is stored but has no effect until a licence including them is uploaded."
        )
    return {
        "applied": True,
        "modules": {c: normalised[c]["enabled"] for c in changes},
        "subModules": {h: normalised[org_entitlements.nav_key(h)]["enabled"] for h in sub_changes},
        "disabledCount": len(disabled),
        "disabledSubModuleCount": len(sub_off),
        "notLicensed": inert,
        "message": message,
    }


@router.get("/factory-matrix")
async def factory_matrix(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The per-factory module allocation matrix (admin). Returns the factories
    (Plants), the LICENSED product modules (the manageable ceiling), and the
    current enabled/disabled state per factory. Modules outside the licence are
    not listed — they can never be granted here."""
    await _require_admin(db, user)
    from app.models.plant import Plant

    state = get_state()
    # Only licensed, non-core product modules are manageable.
    licensed = [
        MODULE_REGISTRY[c]
        for c in sorted(state.enabled_module_set)
        if c not in CORE_MODULE_CODES and c in MODULE_REGISTRY
    ]
    plants = (await db.execute(select(Plant).order_by(Plant.name))).scalars().all()
    overrides = await factory_entitlements.load_all(db)
    by_plant: dict[str, dict[str, dict]] = {}
    for o in overrides:
        by_plant.setdefault(o["plantId"], {})[o["moduleCode"]] = {
            "enabled": o["enabled"],
            "validFrom": o["validFrom"],
            "validUntil": o["validUntil"],
        }

    return {
        # `orgDisabled` marks a licensed module the Super Admin has switched off
        # for the whole organisation. It stays listed rather than vanishing, so
        # a plant admin can see WHY the toggle has no effect instead of hunting
        # for a module that silently disappeared from their screen.
        "modules": [
            {
                "code": m.code,
                "name": m.name,
                "group": m.group,
                "orgDisabled": not org_entitlements.is_enabled_for_org(m.code),
            }
            for m in licensed
        ],
        "factories": [
            {
                "id": p.id,
                "code": p.code,
                "name": p.name,
                # explicit per-module overrides {code: {enabled, validFrom, validUntil}};
                # any licensed module absent here is on with no time bound.
                "overrides": by_plant.get(p.id, {}),
            }
            for p in plants
        ],
    }


@router.put("/factory-matrix")
async def update_factory_matrix(
    payload: dict = Body(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Set per-factory module states for ONE factory (admin). Body:
        { "plantId": "...", "modules": { "CAMS": true, "PTW": false } }
    Only licensed modules may be set — attempting to manage a module outside the
    licence ceiling is rejected (config can never grant entitlements)."""
    await _require_admin(db, user)
    plant_id = payload.get("plantId")
    changes = payload.get("modules") or {}
    if not plant_id or not isinstance(changes, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Expected { plantId, modules: {code: bool} }")

    state = get_state()
    licensed = {c for c in state.enabled_module_set if c not in CORE_MODULE_CODES}
    bad = [c for c in changes if c not in licensed]
    if bad:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "module_not_licensed",
                "message": "Cannot manage modules outside the licence ceiling.",
                "modules": bad,
            },
        )

    # Accept either a bare bool (on/off, no window) or a spec object
    # {enabled, validFrom, validUntil}. Dates may be YYYY-MM-DD (interpreted as
    # whole days, UTC) or full ISO datetimes.
    normalised: dict[str, dict] = {}
    for code, v in changes.items():
        if isinstance(v, bool):
            normalised[code] = {"enabled": v, "validFrom": None, "validUntil": None}
            continue
        if not isinstance(v, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Bad value for {code}")
        vf = _parse_window(v.get("validFrom"), end_of_day=False)
        vu = _parse_window(v.get("validUntil"), end_of_day=True)
        if vf and vu and vu < vf:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{code}: validUntil before validFrom")
        normalised[code] = {"enabled": bool(v.get("enabled", True)), "validFrom": vf, "validUntil": vu}

    await factory_entitlements.set_for_plant(db, plant_id, normalised, user.id)
    return {
        "applied": True,
        "plantId": plant_id,
        "modules": {
            c: {
                "enabled": s["enabled"],
                "validFrom": s["validFrom"].isoformat() if s["validFrom"] else None,
                "validUntil": s["validUntil"].isoformat() if s["validUntil"] else None,
            }
            for c, s in normalised.items()
        },
    }


def _parse_window(value, *, end_of_day: bool):
    """Parse a date/datetime string into a UTC datetime, or None. A bare date
    means the start (00:00) or end (23:59:59) of that day."""
    if not value:
        return None
    from datetime import datetime, timedelta, timezone

    try:
        if len(value) == 10:  # YYYY-MM-DD
            d = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return d + timedelta(hours=23, minutes=59, seconds=59) if end_of_day else d
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Bad date {value!r}: {e}") from e


@router.post("/revalidate")
async def revalidate(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require_admin(db, user)
    state = await refresh_state(db)
    return {"status": _public_status(state)}
