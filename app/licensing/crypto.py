"""Cryptographic primitives for the licensing system.

A licence is a **compact JWS (JWT)** signed with **EdDSA over Ed25519** — the
format mandated by the build prompt (§2.2). We implement the compact-JWS
encode/verify directly on `cryptography`'s Ed25519 rather than leaning on a
JOSE library's EdDSA path, because:

  * this is the security boundary — a small, auditable, dependency-light
    implementation is easier to trust than a generic JOSE codepath; and
  * verification here must *only* ever accept EdDSA, with the algorithm pinned
    in code (never read from the attacker-controlled header) to defeat the
    classic "alg: none" / algorithm-confusion downgrade.

What ships in the client app: ONLY public keys (see keys.py). The private key
lives with the Licence Authority (KMS/HSM) and is used solely by the issuer
tool. A public key cannot mint or alter a valid licence.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ALG = "EdDSA"
TYP = "JWT"


class LicenceSignatureError(Exception):
    """Raised when a licence token fails structural or signature validation.
    The caller treats any instance of this as 'do not trust' → fail closed."""


# ── base64url (unpadded, per JWS) ────────────────────────────────────────────
def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(segment: str) -> bytes:
    # Re-pad to a multiple of 4 before decoding.
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


# ── Keypair generation (used by the Licence Authority, never by the app) ─────
def generate_keypair() -> tuple[str, str]:
    """Generate a fresh Ed25519 keypair. Returns (private_pem, public_pem) as
    PEM strings. The private PEM must be stored in a KMS/HSM or an
    access-controlled secret and NEVER committed or shipped."""
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private_pem, public_pem


def load_private_key(private_pem: str) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise LicenceSignatureError("Not an Ed25519 private key")
    return key


def load_public_key(public_pem: str) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(public_pem.encode("ascii"))
    if not isinstance(key, Ed25519PublicKey):
        raise LicenceSignatureError("Not an Ed25519 public key")
    return key


# ── Compact-JWS sign (Authority side) ────────────────────────────────────────
def sign_compact(payload: dict[str, Any], private_pem: str, kid: str) -> str:
    """Produce a compact JWS string: base64url(header).base64url(payload).sig.
    Header pins alg=EdDSA + the signing `kid` (for rotation)."""
    private_key = load_private_key(private_pem)
    header = {"alg": ALG, "kid": kid, "typ": TYP}
    header_b64 = b64url_encode(_canonical_json(header))
    payload_b64 = b64url_encode(_canonical_json(payload))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = private_key.sign(signing_input)
    return f"{header_b64}.{payload_b64}.{b64url_encode(signature)}"


# ── Compact-JWS verify (app side) ────────────────────────────────────────────
def decode_header_unverified(token: str) -> dict[str, Any]:
    """Read the JWS header WITHOUT verifying — used only to look up which
    embedded public key (`kid`) to verify against. The result is never
    trusted for anything but key selection."""
    parts = token.split(".")
    if len(parts) != 3:
        raise LicenceSignatureError("Malformed token: expected 3 segments")
    try:
        return json.loads(b64url_decode(parts[0]))
    except (ValueError, json.JSONDecodeError) as e:
        raise LicenceSignatureError(f"Unreadable header: {e}") from e


def verify_compact(token: str, public_pem: str) -> dict[str, Any]:
    """Verify an EdDSA compact JWS against `public_pem` and return the decoded
    payload. Raises LicenceSignatureError on ANY problem — wrong shape, wrong
    algorithm, bad signature, unreadable payload — so the caller fails closed.

    The algorithm is PINNED to EdDSA here and the header `alg` is checked
    against it; we never let the token's header choose the algorithm."""
    parts = token.split(".")
    if len(parts) != 3:
        raise LicenceSignatureError("Malformed token: expected 3 segments")
    header_b64, payload_b64, sig_b64 = parts

    try:
        header = json.loads(b64url_decode(header_b64))
    except (ValueError, json.JSONDecodeError) as e:
        raise LicenceSignatureError(f"Unreadable header: {e}") from e

    # Pin the algorithm — defeat 'alg: none' and RS/HS confusion attacks.
    if header.get("alg") != ALG:
        raise LicenceSignatureError(
            f"Unexpected alg {header.get('alg')!r}; only {ALG} is accepted"
        )

    public_key = load_public_key(public_pem)
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    try:
        signature = b64url_decode(sig_b64)
        public_key.verify(signature, signing_input)
    except (InvalidSignature, ValueError) as e:
        raise LicenceSignatureError("Signature verification failed") from e

    try:
        return json.loads(b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError) as e:
        raise LicenceSignatureError(f"Unreadable payload: {e}") from e


def _canonical_json(obj: dict[str, Any]) -> bytes:
    """Deterministic, compact JSON so a re-sign of identical claims is byte
    stable. Sorted keys + tight separators."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
