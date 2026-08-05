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


def get_public_key(kid: str) -> str | None:
    """Return the embedded public PEM for `kid`, or None if this build does not
    trust that key id (→ the validator fails closed with an INVALID licence)."""
    return TRUSTED_PUBLIC_KEYS.get(kid)


def trusted_kids() -> list[str]:
    return list(TRUSTED_PUBLIC_KEYS.keys())
