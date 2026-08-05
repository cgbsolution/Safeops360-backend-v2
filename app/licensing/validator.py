"""In-product licence validator.

`evaluate_licence` is a PURE, synchronous function — given a token string, the
clocks, and the local install id, it returns a fully-decided
`RuntimeLicenceState`. No file I/O, no DB, no network. That purity is what lets
the security/offline tests (TL-04..09, TL-13) run deterministically with a
frozen clock, and it is the whole offline-validation guarantee in one place.

`refresh_state` is the async orchestration: read the .lic file, read the
installation high-water mark from the store, call `evaluate_licence`, persist
the advanced high-water mark, and publish the new state. It is called on boot,
on a periodic timer, and on licence-file upload.

Fail-closed is structural: every error path returns a locked state. There is no
branch that returns ACTIVE without a verified signature, a valid issuer, an
in-window effective clock, and (in strict mode) a matching binding.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timezone

from pydantic import ValidationError

from app.licensing.crypto import (
    LicenceSignatureError,
    decode_header_unverified,
    verify_compact,
)
from app.licensing.installation import (
    advance_last_seen,
    detect_clock_rollback,
    effective_clock,
)
from app.licensing.payload import LicencePayload, RuntimeLicenceState
from app.licensing.registry import build_enabled_set

EXPECTED_ISSUER = "vizionforge"

# Default warn window (days before exp that flips status to EXPIRING_SOON).
DEFAULT_WARN_DAYS = 14

PublicKeyResolver = Callable[[str], "str | None"]


def evaluate_licence(
    token: str | None,
    *,
    system_now: datetime,
    last_seen: datetime | None,
    local_installation_id: str | None,
    public_key_resolver: PublicKeyResolver,
    warn_days: int = DEFAULT_WARN_DAYS,
) -> RuntimeLicenceState:
    """Decide the full licence state. Pure: same inputs → same output."""

    now = _as_utc(system_now)

    # 1. No licence at all → MISSING (fail closed, not open).
    if not token or not token.strip():
        return RuntimeLicenceState.locked("MISSING", now=now,
                                          error="No licence file present")

    # 2. Select the embedded public key by the token's kid (header only — never
    #    trusted for anything but key lookup).
    try:
        header = decode_header_unverified(token)
    except LicenceSignatureError as e:
        return RuntimeLicenceState.locked("INVALID", now=now, error=str(e))

    kid = header.get("kid")
    if not kid:
        return RuntimeLicenceState.locked("INVALID", now=now,
                                          error="Licence header has no kid")
    public_pem = public_key_resolver(kid)
    if public_pem is None:
        return RuntimeLicenceState.locked(
            "INVALID", now=now,
            error=f"Untrusted signing key id {kid!r} (not embedded in this build)",
        )

    # 3. Verify the EdDSA signature. ANY failure → INVALID (tamper defence,
    #    TL-04 / TL-09).
    try:
        raw_payload = verify_compact(token, public_pem)
    except LicenceSignatureError as e:
        return RuntimeLicenceState.locked("INVALID", now=now, error=str(e))

    # 4. Parse + structurally validate the claims.
    try:
        payload = LicencePayload.model_validate(raw_payload)
    except ValidationError as e:
        return RuntimeLicenceState.locked("INVALID", now=now,
                                          error=f"Malformed claims: {e.errors()[:3]}")

    # 5. Issuer pin — defence in depth on top of the key/kid binding.
    if payload.iss != EXPECTED_ISSUER:
        return RuntimeLicenceState.locked(
            "INVALID", now=now,
            error=f"Unexpected issuer {payload.iss!r}",
        )

    # 6. Effective clock — max(systemClock, last_seen). A clock rollback cannot
    #    move this backward (TL-07).
    eff = effective_clock(now, _as_utc_opt(last_seen))
    tamper = detect_clock_rollback(now, _as_utc_opt(last_seen))

    enabled = build_enabled_set(payload.enabled_modules)

    # 7. not-yet-valid (nbf in the future against the effective clock).
    if eff < payload.valid_from:
        state = RuntimeLicenceState.locked(
            "INVALID", now=now,
            error="Licence not yet valid (validFrom is in the future)",
        )
        state.payload = payload
        state.effective_clock = eff
        state.clock_tamper_warning = tamper
        return state

    # 8. Status from licence type. PERPETUAL licences never time-expire — they
    #    stay ACTIVE for the life of the install (no EXPIRING_SOON/GRACE/lock
    #    from the clock). POC/SUBSCRIPTION are governed by exp + grace. The exp
    #    claim is still carried as a far-future backstop, but is not enforced
    #    for PERPETUAL. Signature, issuer, nbf and binding still apply.
    if payload.licence_type == "PERPETUAL":
        status, days = "ACTIVE", None
    else:
        status, days = _status_from_expiry(payload, eff, warn_days)

    # 9. Installation binding (build prompt §6.3). Strict → hard fail; soft →
    #    warn + admin alert but keep operating.
    binding_warning = False
    if payload.installation_binding:
        if payload.installation_binding != (local_installation_id or ""):
            if payload.binding_mode == "STRICT":
                state = RuntimeLicenceState.locked(
                    "INVALID", now=now,
                    error="Installation binding mismatch (strict mode)",
                )
                state.payload = payload
                state.effective_clock = eff
                state.enabled_module_set = enabled
                return state
            binding_warning = True  # SOFT default

    return RuntimeLicenceState(
        status=status,
        last_validated_at=now,
        payload=payload,
        days_to_expiry=days,
        enabled_module_set=enabled,
        effective_clock=eff,
        clock_tamper_warning=tamper,
        binding_warning=binding_warning,
    )


def _status_from_expiry(payload: LicencePayload, eff: datetime, warn_days: int):
    """Return (status, days_to_expiry) for a structurally-valid licence whose
    nbf has passed. days_to_expiry is ceil'd so partial days round up."""
    exp = payload.valid_until
    secs_to_exp = (exp - eff).total_seconds()
    days_to_exp = math.ceil(secs_to_exp / 86400)

    if eff <= exp:
        if secs_to_exp <= warn_days * 86400:
            return "EXPIRING_SOON", days_to_exp
        return "ACTIVE", days_to_exp

    # Past exp — are we still inside the grace window?
    grace_secs = payload.grace_period_days * 86400
    secs_past_exp = (eff - exp).total_seconds()
    if secs_past_exp <= grace_secs:
        return "GRACE", days_to_exp  # negative
    return "EXPIRED_LOCKED", days_to_exp


def compute_advanced_last_seen(last_seen: datetime | None, system_now: datetime) -> datetime:
    """Helper the orchestration persists after each pass."""
    return advance_last_seen(_as_utc_opt(last_seen), _as_utc(system_now))


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _as_utc_opt(dt: datetime | None) -> datetime | None:
    return None if dt is None else _as_utc(dt)
