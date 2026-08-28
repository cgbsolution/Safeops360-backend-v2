"""Embedded TRUSTED public keys — keyed by `kid`.

Only PUBLIC keys live here. A public key can VERIFY a licence but cannot mint
or alter one, so shipping it in the client is safe and is exactly what makes
offline validation trustworthy (build prompt §2.1).

Rotation (build prompt §9): the app may carry MULTIPLE public keys so licences
signed under an old `kid` keep validating while new ones use the new key. Add
the new key here, ship the build, sign new licences with the new key, and drop
the retired key once every licence under it has expired or been reissued.

The matching PRIVATE keys live ONLY with the Licence Authority (KMS/HSM); they
are gitignored under .licence_keys/ and never imported by the running app.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("safeops360.licensing")

# kid → PEM-encoded Ed25519 SubjectPublicKeyInfo.
# `vf-2026-06` is the inaugural dev/POC signing key (generated 2026-06-23).
TRUSTED_PUBLIC_KEYS: dict[str, str] = {
    "vf-2026-06": (
        "-----BEGIN PUBLIC KEY-----\n"
        "MCowBQYDK2VwAyEA9jkc42PQ+wS17bD7dRWV0gbL2Q1uyypLGh/2Oic3+AI=\n"
        "-----END PUBLIC KEY-----\n"
    ),
}

# The `kid` the Licence Authority is currently signing new licences with. Used
# by the issuer tool only; the validator selects the key from the token header.
CURRENT_SIGNING_KID = "vf-2026-06"


# ── Additional trusted keys, from the environment (NON-PRODUCTION) ───────────
#
# A development or demo installation needs to be able to reissue its own licence
# — the registry gains modules over time and `FULL_PLATFORM` expands at ISSUE
# time, so a dev licence goes stale and starts 403-ing features that exist. The
# real signing key lives in the Authority's KMS/HSM and is deliberately not
# reachable from a developer's machine, so a local Authority run needs a local
# key that this build will accept.
#
# That key is supplied through the environment rather than added to the literal
# above, and the distinction is the whole point: `TRUSTED_PUBLIC_KEYS` ships
# inside every client build, so a dev key written there would be trusted on
# every production install forever, and whoever held the matching private key
# could mint a licence for any customer. An env-supplied key is trusted only
# where someone has explicitly configured it — set it in a dev `.env`, never in
# a production deployment.
#
# Two accepted forms, because a PEM is a multi-line file and `.env` is not:
#
#   LICENCE_DEV_PUBLIC_KEYS={"kid": "-----BEGIN PUBLIC KEY-----\n…"}   JSON
#   LICENCE_DEV_PUBLIC_KEYS=.licence_keys                              a directory
#
# The directory form reads every `<kid>.public.pem` in it and takes the kid from
# the filename — which is exactly what `licence_authority.py genkey` writes, so
# a local Authority run needs no copy-paste step and no PEM squeezed onto one
# line. Only `.public.pem` is read; a private key sitting in the same directory
# is never loaded.
#
# Fails SAFE in every direction: an unreadable value is ignored with a warning
# rather than crashing boot, a missing directory is ignored, and an env key can
# never override an embedded one — the real keys always win the lookup.
_ENV_VAR = "LICENCE_DEV_PUBLIC_KEYS"
_PUB_SUFFIX = ".public.pem"


def _keys_from_dir(path: str) -> dict[str, str]:
    """Every `<kid>.public.pem` in `path`. Missing directory → nothing, quietly:
    a developer who has not generated a local key yet is the normal case, not an
    error worth a warning on every boot."""
    if not os.path.isdir(path):
        log.warning("%s points at %r, which is not a directory; ignoring it.", _ENV_VAR, path)
        return {}
    out: dict[str, str] = {}
    for name in sorted(os.listdir(path)):
        if not name.endswith(_PUB_SUFFIX):
            continue
        try:
            with open(os.path.join(path, name), encoding="utf-8") as f:
                pem = f.read().strip()
        except OSError as e:
            log.warning("Could not read %s: %s", name, e)
            continue
        if "BEGIN PUBLIC KEY" not in pem:
            log.warning("%s is not a PEM public key; skipped.", name)
            continue
        out[name[: -len(_PUB_SUFFIX)]] = pem + "\n"
    return out


# Memo keyed on the raw env value: signature verification calls get_public_key
# on every licence check, and neither a directory scan nor a "this is not
# production" warning belongs on that path more than once. Re-keys itself if the
# variable changes, so tests can vary it without a reset hook.
_env_cache: tuple[str, dict[str, str]] | None = None


def _env_public_keys() -> dict[str, str]:
    global _env_cache
    raw = (os.environ.get(_ENV_VAR) or "").strip()
    if _env_cache is not None and _env_cache[0] == raw:
        return _env_cache[1]
    keys = _load_env_public_keys(raw)
    _env_cache = (raw, keys)
    return keys


def _load_env_public_keys(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    if not raw.startswith("{"):
        keys = _keys_from_dir(raw)
        if not keys:
            return {}
    else:
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("expected a JSON object of kid -> PEM")
            keys = {str(k): str(v) for k, v in parsed.items() if k and v}
        except (ValueError, TypeError) as e:
            log.warning("%s is set but unreadable (%s); ignoring it.", _ENV_VAR, e)
            return {}
    overlap = sorted(set(keys) & set(TRUSTED_PUBLIC_KEYS))
    if overlap:
        log.warning(
            "%s tries to redefine embedded key id(s) %s; the embedded keys win.",
            _ENV_VAR, ", ".join(overlap),
        )
    if keys:
        log.warning(
            "Trusting %d NON-PRODUCTION licence signing key(s) from %s: %s. "
            "This must not be set on a production deployment.",
            len(keys), _ENV_VAR, ", ".join(sorted(keys)),
        )
    return keys


def get_public_key(kid: str) -> str | None:
    """Return the trusted public PEM for `kid`, or None if this build does not
    trust that key id (→ the validator fails closed with an INVALID licence).

    Embedded keys are checked first so an env var can never shadow one.
    """
    embedded = TRUSTED_PUBLIC_KEYS.get(kid)
    if embedded is not None:
        return embedded
    return _env_public_keys().get(kid)


def trusted_kids() -> list[str]:
    return list(TRUSTED_PUBLIC_KEYS.keys()) + [
        k for k in _env_public_keys() if k not in TRUSTED_PUBLIC_KEYS
    ]
