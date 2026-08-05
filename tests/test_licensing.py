"""Module Entitlement & Licensing — security, carve-out, lifecycle & offline tests.

Maps to the build-prompt scenarios TL-01..TL-15, with the tamper/security and
offline sets treated as MUST-PASS. Everything here is offline and deterministic:
we mint our own keypair and sign tokens directly, then drive the real
`evaluate_licence`, the real `require_module` API guard, and the real limit
enforcement — so we test the actual security boundary, not a mock of it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.licensing import state as licence_state
from app.licensing.crypto import (
    b64url_decode,
    b64url_encode,
    generate_keypair,
    sign_compact,
)
from app.licensing.enforcement import (
    assert_within_limit,
    is_module_enabled,
    require_module,
)
from app.licensing.payload import RuntimeLicenceState
from app.licensing.registry import CORE_MODULE_CODES
from app.licensing.validator import evaluate_licence

# ── test keypairs ────────────────────────────────────────────────────────────
ISSUER_PRIV, ISSUER_PUB = generate_keypair()           # the "real" Vizionforge key
ATTACKER_PRIV, ATTACKER_PUB = generate_keypair()       # a forged key
ROTATED_PRIV, ROTATED_PUB = generate_keypair()         # a rotated-in new key
KID = "test-2026"
ROTATED_KID = "test-2027"

BASE = datetime(2026, 6, 1, tzinfo=timezone.utc)


def resolver(kid: str):
    """Embedded-public-key lookup, mirroring keys.get_public_key. The app trusts
    KID; ROTATED_KID is added only in the rotation test's resolver."""
    return {KID: ISSUER_PUB}.get(kid)


def rotation_resolver(kid: str):
    """A build that embeds BOTH the old and the rotated key (build prompt §9)."""
    return {KID: ISSUER_PUB, ROTATED_KID: ROTATED_PUB}.get(kid)


def make_payload(**over) -> dict:
    p = {
        "iss": "vizionforge",
        "sub": "acme",
        "jti": over.get("jti", "lic-0001"),
        "iat": int(BASE.timestamp()),
        "nbf": int(BASE.timestamp()),
        "exp": int((BASE + timedelta(days=60)).timestamp()),
        "customerName": "Acme PSU Ltd",
        "edition": "CAMS_ONLY",
        "enabledModules": ["CAMS", "CAPA", "AI_ASSIST"],
        "limits": {},
        "licenceType": "POC",
        "gracePeriodDays": 7,
        "bindingMode": "SOFT",
        "featureFlags": {},
        "deploymentMode": "ON_PREM",
    }
    p.update(over)
    return p


def token(priv=ISSUER_PRIV, kid=KID, **over) -> str:
    return sign_compact(make_payload(**over), priv, kid)


def evaluate(tok, *, now=BASE, last_seen=None, install_id="install-A",
            resolve=resolver):
    return evaluate_licence(
        tok, system_now=now, last_seen=last_seen,
        local_installation_id=install_id, public_key_resolver=resolve,
    )


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def guard_blocks(module_code: str, st: RuntimeLicenceState) -> bool:
    """Drive the REAL require_module() FastAPI dependency against state `st`,
    returning True iff it raises 403 (i.e. the API would block the module)."""
    from fastapi import HTTPException

    licence_state._set_state(st)
    checker = require_module(module_code)
    try:
        run(checker())
        return False
    except HTTPException as e:
        assert e.status_code == 403
        return True


# ════════════════════════════════════════════════════════════════════════════
#  Validation & carve-out
# ════════════════════════════════════════════════════════════════════════════
def test_tl01_cams_only_enables_exactly_cams_set_plus_core():
    st = evaluate(token())
    assert st.status == "ACTIVE"
    # Enabled: the CAMS bundle + core.
    for code in ("CAMS", "CAPA", "AI_ASSIST"):
        assert is_module_enabled(code, st), code
    for code in CORE_MODULE_CODES:
        assert is_module_enabled(code, st), code
    # Everything else is OFF — at the API layer, independent of the UI (TL-01).
    for code in ("INCIDENT", "PTW", "ERM", "FACILITIES", "HIRA", "PPE"):
        assert not is_module_enabled(code, st), code
        assert guard_blocks(code, st), f"API guard should 403 {code}"
    # And the CAMS API is NOT blocked.
    assert not guard_blocks("CAMS", st)


def test_tl02_custom_list_plus_resolved_dependencies():
    # CUSTOM enabling only KRI must auto-enable its ERM base (dependency
    # resolution), so the shared ERM router is reachable.
    st = evaluate(token(edition="CUSTOM", enabledModules=["KRI"]))
    assert st.status == "ACTIVE"
    assert is_module_enabled("KRI", st)
    assert is_module_enabled("ERM", st)      # pulled in by depends_on
    assert not guard_blocks("ERM", st)
    assert not is_module_enabled("CAMS", st)
    assert guard_blocks("CAMS", st)


def test_tl03_limits_block_at_cap():
    st = evaluate(token(limits={"maxFactories": 16}))
    from fastapi import HTTPException

    licence_state._set_state(st)
    assert_within_limit("factories", 15)          # 16th allowed
    with pytest.raises(HTTPException):            # 17th blocked
        assert_within_limit("factories", 16)


# ════════════════════════════════════════════════════════════════════════════
#  Tamper / security  (MUST PASS)
# ════════════════════════════════════════════════════════════════════════════
def test_tl04_editing_any_claim_invalidates_signature():
    good = token()
    header_b64, payload_b64, sig_b64 = good.split(".")
    claims = json.loads(b64url_decode(payload_b64))
    # Attacker grants themselves every module and pushes out expiry.
    claims["enabledModules"] = ["CAMS", "INCIDENT", "ERM", "FACILITIES"]
    claims["exp"] = int((BASE + timedelta(days=3650)).timestamp())
    tampered = (
        header_b64
        + "."
        + b64url_encode(json.dumps(claims, separators=(",", ":")).encode())
        + "."
        + sig_b64
    )
    st = evaluate(tampered)
    assert st.status == "INVALID"
    assert st.is_locked
    # No path grants the smuggled modules; core still reachable (fail closed).
    for code in ("INCIDENT", "ERM", "FACILITIES", "CAMS"):
        assert not is_module_enabled(code, st)
        assert guard_blocks(code, st)
    assert is_module_enabled("CORE_RBAC", st)


def test_tl05_config_cannot_override_entitlements(monkeypatch):
    # Even with an env var screaming "enable everything", entitlements come ONLY
    # from the signed licence — there is no config path that grants a module.
    monkeypatch.setenv("ENABLED_MODULES", "INCIDENT,ERM,FACILITIES")
    monkeypatch.setenv("SAFEOPS_ENABLE_ALL", "true")
    st = evaluate(token())  # plain CAMS_ONLY
    assert not is_module_enabled("INCIDENT", st)
    assert not is_module_enabled("ERM", st)
    assert is_module_enabled("CAMS", st)


def test_tl06_missing_or_corrupt_file_fails_closed():
    missing = evaluate(None)
    assert missing.status == "MISSING" and missing.is_locked
    corrupt = evaluate("not-a-jwt")
    assert corrupt.status == "INVALID" and corrupt.is_locked
    half = evaluate("aaa.bbb")                    # wrong segment count
    assert half.status == "INVALID" and half.is_locked
    for st in (missing, corrupt, half):
        assert not is_module_enabled("CAMS", st)
        assert is_module_enabled("CORE_LICENSING", st)   # renewal screen reachable


def test_tl07_clock_rollback_cannot_extend_poc():
    tok = token()  # valid BASE .. BASE+60d, grace 7
    # The app has genuinely run to day 65 (past exp+? exp=60, grace=7 -> day 65
    # is inside grace). last_seen is the monotonic high-water mark.
    last_seen = BASE + timedelta(days=65)
    # Attacker rewinds the OS clock back to day 1 to "reset" the POC.
    rolled_back = BASE + timedelta(days=1)
    st = evaluate(tok, now=rolled_back, last_seen=last_seen)
    # Effective clock holds at day 65 -> still GRACE, never reset to ACTIVE.
    assert st.status == "GRACE"
    assert st.clock_tamper_warning is True
    # Without the monotonic guard, day 1 alone would read ACTIVE — proving the
    # high-water mark is what defeats the rollback.
    naive = evaluate(tok, now=rolled_back, last_seen=None)
    assert naive.status == "ACTIVE"


def test_tl08_installation_binding_soft_and_strict():
    bound = token(installationBinding="install-A")
    # Same install -> clean.
    ok = evaluate(bound, install_id="install-A")
    assert ok.status == "ACTIVE" and not ok.binding_warning
    # Different install, SOFT (default) -> still operational, but warns.
    soft = evaluate(bound, install_id="install-B")
    assert soft.is_operational and soft.binding_warning is True
    # Different install, STRICT -> hard fail, locked.
    strict = token(installationBinding="install-A", bindingMode="STRICT")
    hard = evaluate(strict, install_id="install-B")
    assert hard.status == "INVALID" and hard.is_locked


def test_tl09_non_vizionforge_key_fails_verification():
    # Signed with the attacker's private key but claiming the trusted kid.
    forged = token(priv=ATTACKER_PRIV, kid=KID)
    st = evaluate(forged)
    assert st.status == "INVALID" and st.is_locked
    # An unknown kid (key not embedded in this build) also fails closed.
    unknown = evaluate(token(kid="never-embedded"))
    assert unknown.status == "INVALID"


def test_security_alg_confusion_and_issuer_pin():
    # 'alg: none' style downgrade — re-header to alg=none must not verify.
    good = token()
    h, p, s = good.split(".")
    none_header = b64url_encode(json.dumps({"alg": "none", "kid": KID, "typ": "JWT"}).encode())
    assert evaluate(f"{none_header}.{p}.{s}").status == "INVALID"
    # Wrong issuer (signed by our key but iss spoofed) -> INVALID.
    assert evaluate(token(iss="acme-corp")).status == "INVALID"


# ════════════════════════════════════════════════════════════════════════════
#  Lifecycle
# ════════════════════════════════════════════════════════════════════════════
def test_tl10_expiry_grace_lock_transitions():
    tok = token()  # exp = BASE+60d, grace 7, warn window default 14d
    assert evaluate(tok, now=BASE + timedelta(days=10)).status == "ACTIVE"
    assert evaluate(tok, now=BASE + timedelta(days=50)).status == "EXPIRING_SOON"
    assert evaluate(tok, now=BASE + timedelta(days=63)).status == "GRACE"
    locked = evaluate(tok, now=BASE + timedelta(days=70))
    assert locked.status == "EXPIRED_LOCKED" and locked.is_locked
    # In every state up to lock, CAMS works; once locked, CAMS is blocked but
    # core stays reachable (export / renewal).
    grace = evaluate(tok, now=BASE + timedelta(days=63))
    assert is_module_enabled("CAMS", grace)
    assert not is_module_enabled("CAMS", locked)
    assert is_module_enabled("CORE_LICENSING", locked)


def test_tl11_renewal_restores_and_changes_entitlements():
    # A locked CAMS_ONLY licence...
    locked = evaluate(token(), now=BASE + timedelta(days=70))
    assert locked.is_locked
    # ...replaced by an IMS_CORE renewal that is ACTIVE again. Validating the new
    # token (as the upload endpoint does) restores access + adds modules live.
    renewal = token(
        jti="lic-renew", edition="IMS_CORE",
        enabledModules=["CAMS", "CAPA", "INCIDENT", "PTW", "HIRA"],
        nbf=int((BASE + timedelta(days=69)).timestamp()),
        exp=int((BASE + timedelta(days=430)).timestamp()),
    )
    st = evaluate(renewal, now=BASE + timedelta(days=70))
    assert st.status == "ACTIVE"
    assert is_module_enabled("INCIDENT", st)   # newly granted
    assert is_module_enabled("PTW", st)


def test_perpetual_never_time_expires():
    # A PERPETUAL licence stays ACTIVE for the life of the install — even read
    # against a clock decades past its exp backstop. (POC/SUBSCRIPTION would be
    # EXPIRED_LOCKED at that point — see test_tl10.)
    perp = token(licenceType="PERPETUAL", deploymentMode="ON_PREM")
    assert evaluate(perp, now=BASE + timedelta(days=10)).status == "ACTIVE"
    assert evaluate(perp, now=BASE + timedelta(days=100 * 365)).status == "ACTIVE"
    far = evaluate(perp, now=BASE + timedelta(days=200 * 365))
    assert far.status == "ACTIVE" and far.is_operational
    assert far.days_to_expiry is None
    # Signature is still enforced — a tampered perpetual licence still locks.
    h, p, s = perp.split(".")
    assert evaluate(f"{h}.{p}.{s[:-3]}AAA").status == "INVALID"


def test_tl12_key_rotation():
    old = token(kid=KID)
    new = token(priv=ROTATED_PRIV, kid=ROTATED_KID)
    # A build embedding BOTH keys validates licences under either kid.
    assert evaluate(old, resolve=rotation_resolver).status == "ACTIVE"
    assert evaluate(new, resolve=rotation_resolver).status == "ACTIVE"
    # Retiring the old key (a build that no longer embeds KID) breaks only the
    # old-kid licence; the new one keeps working.
    only_new = lambda kid: {ROTATED_KID: ROTATED_PUB}.get(kid)  # noqa: E731
    assert evaluate(old, resolve=only_new).status == "INVALID"
    assert evaluate(new, resolve=only_new).status == "ACTIVE"


# ════════════════════════════════════════════════════════════════════════════
#  Offline
# ════════════════════════════════════════════════════════════════════════════
def test_tl13_validation_makes_no_network_call(monkeypatch):
    import socket

    def boom(*a, **k):
        raise AssertionError("validation attempted a network call")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    # Full validate + status + grace + lock all run on an isolated host.
    assert evaluate(token()).status == "ACTIVE"
    assert evaluate(token(), now=BASE + timedelta(days=70)).status == "EXPIRED_LOCKED"


# ════════════════════════════════════════════════════════════════════════════
#  Cross-cutting
# ════════════════════════════════════════════════════════════════════════════
def test_tl14_core_modules_reachable_under_every_state():
    states = [
        evaluate(token()),                                    # ACTIVE
        evaluate(token(), now=BASE + timedelta(days=70)),     # EXPIRED_LOCKED
        evaluate("garbage"),                                  # INVALID
        evaluate(None),                                       # MISSING
    ]
    for st in states:
        for code in CORE_MODULE_CODES:
            assert is_module_enabled(code, st), (st.status, code)
            assert not guard_blocks(code, st)                 # never 403 a core route


def test_per_factory_allocation_within_ceiling():
    # A FULL_PLATFORM licence (ceiling = everything). An admin disables PTW for
    # factory A only.
    from app.licensing import factory_entitlements as fe
    from app.licensing.enforcement import is_module_enabled_for_plant

    st = evaluate(token(edition="FULL_PLATFORM",
                        enabledModules=["CAMS", "PTW", "INCIDENT"]))
    licence_state._set_state(st)
    fe._overrides = {"plant-A": {"PTW": fe.Override(enabled=False, valid_from=None, valid_until=None)}}
    try:
        # Per-factory effective access.
        assert is_module_enabled_for_plant("PTW", "plant-A") is False   # disabled here
        assert is_module_enabled_for_plant("PTW", "plant-B") is True    # on elsewhere
        assert is_module_enabled_for_plant("CAMS", "plant-A") is True   # untouched
        # Core is never restricted per factory.
        assert is_module_enabled_for_plant("CORE_RBAC", "plant-A") is True

        # The API guard honours the active-plant header.
        from fastapi import HTTPException
        def api_blocks(code, plant):
            try:
                run(require_module(code)(x_active_plant=plant)); return False
            except HTTPException:
                return True
        assert api_blocks("PTW", "plant-A")            # disabled for this factory
        assert not api_blocks("PTW", "plant-B")        # fine in another factory
        assert not api_blocks("PTW", None)             # no active plant → ceiling only

        # The ceiling still wins: a module OUTSIDE the licence can never be
        # granted per-factory, even with no disable row.
        cams_only = evaluate(token())                  # CAMS_ONLY, no ERM
        licence_state._set_state(cams_only)
        assert is_module_enabled_for_plant("ERM", "plant-A") is False
        assert is_module_enabled_for_plant("ERM", "plant-B") is False
    finally:
        fe._overrides = {}


def test_per_factory_validity_window():
    # A factory is granted CAMS only for a fixed period; never-expire for PTW.
    from app.licensing import factory_entitlements as fe
    from app.licensing.enforcement import is_module_enabled_for_plant

    st = evaluate(token(edition="FULL_PLATFORM", enabledModules=["CAMS", "PTW"]))
    licence_state._set_state(st)
    window_from = BASE + timedelta(days=10)
    window_to = BASE + timedelta(days=40)
    fe._overrides = {
        "plant-A": {
            "CAMS": fe.Override(enabled=True, valid_from=window_from, valid_until=window_to),
            "PTW": fe.Override(enabled=True, valid_from=None, valid_until=None),  # never expires
        }
    }
    try:
        # CAMS is gated by the window; the per-factory check evaluates against
        # the supplied clock.
        before = BASE + timedelta(days=5)
        within = BASE + timedelta(days=20)
        after = BASE + timedelta(days=50)
        assert fe.is_enabled_for_plant("CAMS", "plant-A", before) is False   # not started
        assert fe.is_enabled_for_plant("CAMS", "plant-A", within) is True    # in window
        assert fe.is_enabled_for_plant("CAMS", "plant-A", after) is False    # window ended
        # PTW has no window → always on (within the ceiling).
        assert fe.is_enabled_for_plant("PTW", "plant-A", before) is True
        assert fe.is_enabled_for_plant("PTW", "plant-A", after) is True
        # window_status reflects the lifecycle.
        assert fe.window_status("plant-A", "CAMS", before) == "NOT_STARTED"
        assert fe.window_status("plant-A", "CAMS", within) == "ON"
        assert fe.window_status("plant-A", "CAMS", after) == "EXPIRED"
        # Ceiling still wins: a module outside the licence is off regardless of window.
        cams_only = evaluate(token())  # CAMS_ONLY, no PTW
        licence_state._set_state(cams_only)
        assert is_module_enabled_for_plant("PTW", "plant-A", cams_only) is False
    finally:
        fe._overrides = {}


def test_tl15_disabled_module_treated_as_absent():
    # The graceful-degradation primitive: a cross-module provider checks
    # is_module_enabled and treats "not entitled" exactly like "not present".
    st = evaluate(token())  # CAMS_ONLY — no ERM
    assert is_module_enabled("ERM", st) is False
    # ...so CAMS enrichment that would read ERM obligations simply omits them,
    # rather than erroring — same contract as CAMS-standalone.
