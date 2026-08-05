# Module Entitlement & Licensing System

Cryptographically-signed, **offline-validatable** licensing that gates modules
from a signed licence (never from config/DB), enforces site/user/factory limits,
and auto-expires POCs — for on-prem and air-gapped deployments.

## How it works (30 seconds)

1. **Vizionforge** holds an **Ed25519 private key** (KMS/HSM in prod; here in
   `.licence_keys/`, gitignored). The app embeds only the **public key** by
   `kid` in `keys.py`. A public key can verify but never forge a licence.
2. A licence is a **compact JWS (EdDSA)** `.lic` file. The app verifies it
   locally — no network — and computes a status (ACTIVE / EXPIRING_SOON / GRACE
   / EXPIRED_LOCKED / INVALID / MISSING).
3. Every gated API router carries `Depends(require_module("CODE"))`. A module
   not in the validated licence returns **403 at the API**, regardless of the
   UI. Core modules (identity, RBAC, licensing…) are never gated.
4. POC expiry is checked against `max(systemClock, lastSeenTimestamp)`, so
   winding the OS clock back cannot extend it.

**Fail closed:** any validation uncertainty → locked, never open.

## Files

| Area | File |
|---|---|
| Module registry (gateable vocabulary) | `registry.py` |
| Editions / SKUs | `editions.py` |
| Signed-claim schema + runtime state | `payload.py` |
| Ed25519 compact-JWS sign/verify | `crypto.py` |
| Embedded **public** keys by `kid` | `keys.py` |
| Installation identity + clock-tamper | `installation.py` |
| Validator (pure `evaluate_licence`) | `validator.py` |
| Process-wide state + async orchestration | `state.py` |
| `require_module` guard + limits + degradation | `enforcement.py` |
| Router → module map | `router_map.py` |
| Admin/status API (`/api/licensing/*`) | `../routers/licensing.py` |
| DB table model | `../models/licensing.py` |
| **Licence Authority (issuer, never shipped)** | `../../scripts/licence_authority.py` |
| DDL apply (one-off) | `../../scripts/create_licensing_tables.py` |
| Security/tamper/offline tests | `../../tests/test_licensing.py` |

## Operator runbook

### First install / per-deployment
```bash
# 1. Create the table (additive, idempotent).
.venv/Scripts/python.exe scripts/create_licensing_tables.py

# 2. Issue a licence (Vizionforge side) and drop it at the licence path.
#    Default path: <backend>/licence.lic  (override with LICENCE_FILE_PATH).
.venv/Scripts/python.exe scripts/licence_authority.py issue \
    --customer-id acme --customer-name "Acme PSU" \
    --edition CAMS_ONLY --type POC --days 60 --grace-days 7 \
    --max-factories 16 --out licence.lic

# 3. Boot the app — it validates on startup and re-checks hourly.
```

### Issue a carve-out / custom licence
```bash
# CAMS-only POC, 16 factories, 60 days
licence_authority.py issue --customer-id x --customer-name "X" \
  --edition CAMS_ONLY --type POC --days 60 --max-factories 16 --out clients/x.lic

# Custom module set
licence_authority.py issue --customer-id y --customer-name "Y" \
  --edition CUSTOM --modules CAMS,INCIDENT,PTW --days 90 --out clients/y.lic

# Pin to an installation (admin reads installationId from /licence screen)
licence_authority.py issue ... --binding <installationId> --binding-mode STRICT
```

### Renew / change edition (live, no reinstall)
An admin uploads the new `.lic` on the in-app **Licence** screen
(`/licence`) → it is validated server-side **before** it replaces the old file
→ entitlements refresh on the next validation. The locked screen offers the same
upload + a data export.

### Key rotation (build prompt §9)
```bash
licence_authority.py genkey --kid vf-2027-01     # new keypair
# → paste the printed PUBLIC key into keys.py TRUSTED_PUBLIC_KEYS,
#   set CURRENT_SIGNING_KID = "vf-2027-01", ship the build (it now embeds BOTH
#   keys), and sign new licences with --kid vf-2027-01. Drop the old key once
#   every licence under it has expired/been reissued.
```

### Revoke (best-effort)
```bash
licence_authority.py revoke --jti <jti>
```
Air-gapped installs cannot check a CRL — revocation relies on **short expiry +
binding**, not on this flag.

## Editions

| Edition | Modules |
|---|---|
| `CAMS_ONLY` | CAMS + CAPA + AI |
| `IMS_CORE` | CAMS + the operational-safety suite (incident, HIRA/EAI, PTW, PPE, training, …) |
| `ERM_SUITE` | ERM + KRI/appetite/compliance/loss/BCM/controls/vendor/insurance |
| `FULL_PLATFORM` | everything |
| `CUSTOM` | explicit `--modules` list |

## Security guarantees (validated by `tests/test_licensing.py`, TL-01..15)
- Editing any claim invalidates the signature → INVALID → locked (TL-04).
- Config/env/DB cannot grant a module — only the signed licence can (TL-05).
- Missing/corrupt licence fails closed, not open (TL-06).
- Clock rollback cannot extend a POC (TL-07).
- Installation binding: soft-warn / strict-fail (TL-08).
- Non-Vizionforge / unknown-kid / `alg:none` all fail verification (TL-09).
- Core modules stay reachable under every state incl. locked (TL-14).
- All validation is offline — zero network (TL-13).

## Per-factory module access (within the licence ceiling)

The signed licence sets the **deployment ceiling** (which modules exist at all).
On top of that, an admin can turn licensed modules **on/off per factory** from the
**/licence → Per-factory module access** matrix. This can only *restrict* within
the licence — never grant a module the licence doesn't include — so the
config-can't-grant rule still holds.

- **Storage:** `FactoryModuleEntitlement` (plantId, moduleCode, enabled,
  validFrom, validUntil) — opt-out; absence of a row = on with no time bound.
  Cached in-memory (`factory_entitlements.py`), refreshed on boot + after save.
- **Validity window:** each module-at-a-factory can be granted for a period
  (validFrom..validUntil) or with no expiry (validUntil null). The window is
  evaluated against the licence's monotonic effective clock, so a clock rollback
  can't extend it either. Outside the window → blocked at that factory.
- **Effective access at a factory** = `is_module_enabled(code)` (signed ceiling)
  AND not disabled for that plant. `enforcement.is_module_enabled_for_plant`.
- **Runtime factory** = the active plant. The frontend writes it to the
  `safeops_active_plant` cookie (from `?plantId=` or the user's home plant); the
  API proxy forwards it as the `X-Active-Plant` header; `require_module` enforces
  per-factory when the header is present (ceiling-only when absent).
- **API:** `GET/PUT /api/licensing/factory-matrix` (admin),
  `GET /api/licensing/modules?plantId=` (effective set for nav gating).

## What ships vs what doesn't
- **Ships in the app:** everything in `app/licensing/`, the public keys, the
  `/api/licensing/*` router.
- **NEVER ships:** `scripts/licence_authority.py`, the private key in
  `.licence_keys/`, and `licence_registry.json`. All gitignored.
