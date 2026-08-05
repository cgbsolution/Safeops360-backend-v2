"""Enforcement — the security boundary (build prompt §5.2, §2.4).

Nav-hiding and route guards in the frontend are UX. THIS is the layer that
actually stops a disabled module: every gated router declares
`dependencies=[Depends(require_module("CODE"))]`, so a disabled module's API
returns 403 no matter how it's called — curl, replay, a hand-built request.

Three rules, all fail-closed:
  * core modules are ALWAYS enabled (identity, RBAC, licensing, …) so a client
    can never be locked out of their own data or the renewal screen (TL-14);
  * a product module is enabled iff the licence is operational AND the code is
    in the validated enabled set (so EXPIRED_LOCKED / INVALID / MISSING blocks
    every product API while leaving core reachable);
  * limits (sites/users/factories) are checked at create paths.

`is_module_enabled` is also the graceful-degradation primitive: cross-module
providers call it and treat "not entitled" exactly like "not present", which
generalises the CAMS-standalone degradation contract (§5.2, TL-15).
"""

from __future__ import annotations

from fastapi import Header, HTTPException, status

from app.licensing import factory_entitlements
from app.licensing.payload import RuntimeLicenceState
from app.licensing.registry import CORE_MODULE_CODES, MODULE_REGISTRY
from app.licensing.state import get_state


def current_state() -> RuntimeLicenceState:
    return get_state()


def is_module_enabled(code: str, state: RuntimeLicenceState | None = None) -> bool:
    """The single authority for 'is this module usable right now' at the
    DEPLOYMENT level (the signed-licence ceiling). Core is always on; product
    modules require an operational licence that grants them."""
    st = state or get_state()
    if code in CORE_MODULE_CODES:
        return True
    if not st.is_operational:
        return False
    return code in st.enabled_module_set


def _effective_now(state: RuntimeLicenceState):
    """The clock used for per-factory windows — the licence's monotonic
    effective clock when available, so a rollback can't extend a window either."""
    return state.effective_clock  # may be None → factory_entitlements uses utcnow()


def is_module_enabled_for_plant(
    code: str, plant_id: str | None, state: RuntimeLicenceState | None = None
) -> bool:
    """Effective access for a specific factory: the signed ceiling AND the
    admin's per-factory allocation (on/off + validity window). Core is never
    restricted per factory."""
    st = state or get_state()
    if not is_module_enabled(code, st):
        return False
    if code in CORE_MODULE_CODES:
        return True
    return factory_entitlements.is_enabled_for_plant(code, plant_id, _effective_now(st))


def require_module(module_code: str):
    """FastAPI dependency factory. Attach to a router or route:

        router = APIRouter(..., dependencies=[Depends(require_module("CAMS"))])

    Raises 403 with a machine-readable detail the frontend uses to decide
    between 'not in your edition' and 'licence locked, go renew'.
    """

    async def _checker(x_active_plant: str | None = Header(default=None)) -> None:
        st = get_state()
        mod = MODULE_REGISTRY.get(module_code)
        mod_name = mod.name if mod else module_code

        # 1. Signed-licence ceiling — the hard, tamper-proof boundary.
        if not is_module_enabled(module_code, st):
            if st.is_locked:
                reason = "licence_locked"
                message = (
                    f"Access is locked because the licence status is {st.status}. "
                    "Contact your administrator to upload a valid/renewed licence."
                )
            else:
                reason = "module_not_entitled"
                message = f"The '{mod_name}' module is not included in your licence edition."
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "entitlement_denied",
                    "reason": reason,
                    "module": module_code,
                    "licenceStatus": st.status,
                    "message": message,
                },
            )

        # 2. Per-factory allocation + validity window (within the ceiling) —
        #    only when the request carries an active plant. Absent header →
        #    ceiling-only (safe default).
        if x_active_plant and not factory_entitlements.is_enabled_for_plant(
            module_code, x_active_plant, _effective_now(st)
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "entitlement_denied",
                    "reason": "module_disabled_for_factory",
                    "module": module_code,
                    "plantId": x_active_plant,
                    "licenceStatus": st.status,
                    "message": f"The '{mod_name}' module is turned off for this factory.",
                },
            )

    return _checker


# ── Limit enforcement (create paths) ─────────────────────────────────────────
_LIMIT_FIELDS = {
    "sites": ("max_sites", "sites"),
    "users": ("max_users", "users"),
    "factories": ("max_factories", "factories"),
}


def limit_for(kind: str) -> int | None:
    """The cap for `kind` from the validated licence, or None for unlimited."""
    st = get_state()
    if st.payload is None:
        return None
    attr, _ = _LIMIT_FIELDS[kind]
    return getattr(st.payload.limits, attr, None)


def assert_within_limit(kind: str, current_count: int) -> None:
    """Call BEFORE creating the (current_count+1)-th record of `kind`. Raises
    403 'licence limit reached' when the cap is hit (TL-03)."""
    if kind not in _LIMIT_FIELDS:
        raise ValueError(f"Unknown limit kind: {kind}")
    cap = limit_for(kind)
    if cap is None:
        return  # unlimited
    if current_count >= cap:
        _, label = _LIMIT_FIELDS[kind]
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "licence_limit_reached",
                "reason": "limit_reached",
                "limit": kind,
                "cap": cap,
                "current": current_count,
                "message": (
                    f"Licence limit reached: your edition allows {cap} {label}. "
                    "Contact Vizionforge to raise the limit."
                ),
            },
        )


# Convenience alias so routers can write `Depends(ModuleGuard("CAMS"))`.
ModuleGuard = require_module

__all__ = [
    "require_module",
    "ModuleGuard",
    "is_module_enabled",
    "current_state",
    "limit_for",
    "assert_within_limit",
]
