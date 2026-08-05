"""SafeOps360 — Module Entitlement & Licensing System.

A cryptographically-signed, offline-validatable licensing layer that gates
modules from a signed licence (never from config/DB), enforces site/user
limits, and supports time-boxed POC expiry for on-prem / air-gapped installs.

Security model (see Audit_Compliance build prompt §2):
  * Asymmetric signing — Vizionforge holds the Ed25519 PRIVATE key; only the
    public key (by `kid`) ships in this package. The app can verify but never
    forge a licence.
  * Offline validation — signature verified with the embedded public key;
    expiry checked against a monotonic clock high-water mark. Zero network.
  * Fail closed — any validation uncertainty restricts the app; it never
    fails open into full access.

Import map:
  registry      — canonical gateable module list (the vocabulary licences use)
  editions      — named SKU bundles → module sets
  payload       — the signed-claim schema + runtime state types
  crypto        — Ed25519 keypair gen + compact-JWS sign/verify primitives
  keys          — embedded TRUSTED public keys (private key never lives here)
  installation  — installation identity + monotonic last-seen (clock-tamper)
  validator     — load → verify → compute status → RuntimeLicenceState
  state         — process-wide runtime licence state holder
  enforcement   — require_module() API guard (the security boundary) + limits
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
