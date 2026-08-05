#!/usr/bin/env python
"""Licence Authority — Vizionforge-internal licence issuer.

⚠️  THIS TOOL IS NOT PART OF THE SHIPPED CLIENT APP. ⚠️
It holds/accesses the PRIVATE Ed25519 signing key and is the only thing that
ever touches it (build prompt §4, §10). Keep it on a controlled host; keep the
private key in a KMS/HSM or an access-controlled encrypted secret. Never deploy
this file, the private key, or the registry to a client.

It reuses the *read-only* shared definitions from app.licensing (registry,
editions, payload schema, crypto) — those contain no secrets — but the secret
(the private key) is loaded from disk at run time and never embedded.

Commands:
  genkey   generate a new Ed25519 keypair (rotation / first setup)
  issue    expand an edition → modules, build + sign a payload, write a .lic
  list     show the issued-licence registry
  revoke   mark a jti revoked (best-effort CRL; offline installs can't check it)
  verify   locally re-verify a .lic against the embedded public key (sanity)

Example — a 60-day CAMS-only POC for a 16-factory client:
  python scripts/licence_authority.py issue \
      --customer-id acme --customer-name "Acme PSU Ltd" \
      --edition CAMS_ONLY --type POC --days 60 --grace-days 7 \
      --max-factories 16 --deployment-mode ON_PREM \
      --out clients/acme.lic
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

# Make `app` importable when run from the backend root.
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.licensing import keys  # noqa: E402
from app.licensing.crypto import (  # noqa: E402
    LicenceSignatureError,
    generate_keypair,
    sign_compact,
    verify_compact,
)
from app.licensing.editions import EDITIONS, expand_edition, get_edition  # noqa: E402
from app.licensing.registry import resolve_dependencies, unknown_modules  # noqa: E402

KEYS_DIR = os.path.join(_BACKEND_ROOT, ".licence_keys")
REGISTRY_PATH = os.path.join(KEYS_DIR, "licence_registry.json")
ISSUER = "vizionforge"


# ── helpers ──────────────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(s: str) -> datetime:
    """Parse YYYY-MM-DD (or full ISO) into a UTC datetime."""
    try:
        if len(s) == 10:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError as e:
        raise SystemExit(f"Bad date {s!r}: {e}") from e


def _load_registry() -> list[dict]:
    if not os.path.exists(REGISTRY_PATH):
        return []
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_registry(entries: list[dict]) -> None:
    os.makedirs(KEYS_DIR, exist_ok=True)
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


def _private_key_path(kid: str, override: str | None) -> str:
    return override or os.path.join(KEYS_DIR, f"{kid}.private.pem")


def _parse_kv(items: list[str] | None) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"Bad feature flag {item!r}; expected key=true/false")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip().lower() in {"1", "true", "yes", "on"}
    return out


# ── commands ─────────────────────────────────────────────────────────────────
def cmd_genkey(args: argparse.Namespace) -> None:
    kid = args.kid
    priv, pub = generate_keypair()
    os.makedirs(KEYS_DIR, exist_ok=True)
    priv_path = os.path.join(KEYS_DIR, f"{kid}.private.pem")
    pub_path = os.path.join(KEYS_DIR, f"{kid}.public.pem")
    if os.path.exists(priv_path) and not args.force:
        raise SystemExit(f"{priv_path} exists; use --force to overwrite")
    with open(priv_path, "w", encoding="utf-8") as f:
        f.write(priv)
    with open(pub_path, "w", encoding="utf-8") as f:
        f.write(pub)
    print(f"Wrote {priv_path} (KEEP SECRET — KMS/HSM in production)")
    print(f"Wrote {pub_path}")
    print("\nEmbed this PUBLIC key in app/licensing/keys.py under TRUSTED_PUBLIC_KEYS:")
    print(f'    "{kid}": (')
    for line in pub.strip().splitlines():
        print(f'        "{line}\\n"')
    print("    ),")


def cmd_issue(args: argparse.Namespace) -> None:
    edition = get_edition(args.edition)
    if edition is None:
        raise SystemExit(f"Unknown edition {args.edition!r}. Known: {', '.join(EDITIONS)}")

    custom = [m.strip() for m in (args.modules.split(",") if args.modules else []) if m.strip()]
    if args.edition == "CUSTOM" and not custom:
        raise SystemExit("CUSTOM edition requires --modules m1,m2,...")

    base_modules = expand_edition(args.edition, custom)
    bad = unknown_modules(base_modules)
    if bad:
        raise SystemExit(f"Unknown module code(s): {', '.join(bad)}")

    # Resolve dependencies so the claim is self-consistent and transparent.
    # Core is implicit/always-on, so it is NOT written into the licence.
    from app.licensing.registry import CORE_MODULE_CODES

    enabled = sorted(resolve_dependencies(base_modules) - set(CORE_MODULE_CODES))

    # Dates → unix timestamps.
    nbf_dt = _parse_date(args.valid_from) if args.valid_from else _now()
    if args.days is not None:
        exp_dt = nbf_dt + timedelta(days=args.days)
    elif args.valid_until:
        d = _parse_date(args.valid_until)
        # Treat a bare date as the END of that day.
        exp_dt = d if len(args.valid_until) > 10 else d + timedelta(hours=23, minutes=59, seconds=59)
    elif args.type == "PERPETUAL":
        # Perpetual licences never time-expire (the validator ignores exp for
        # PERPETUAL). We still set a far-future exp as a defence-in-depth
        # backstop so the licence is harmless even on an older validator build.
        exp_dt = nbf_dt + timedelta(days=365 * 100)
    else:
        raise SystemExit("Provide --days N or --valid-until YYYY-MM-DD (or --type PERPETUAL)")

    if exp_dt <= nbf_dt:
        raise SystemExit("validUntil must be after validFrom")

    # Limits — edition defaults, overridable per flag.
    dl = edition.default_limits
    limits: dict[str, int] = {}
    max_sites = args.max_sites if args.max_sites is not None else dl.max_sites
    max_users = args.max_users if args.max_users is not None else dl.max_users
    max_factories = args.max_factories if args.max_factories is not None else dl.max_factories
    if max_sites is not None:
        limits["maxSites"] = max_sites
    if max_users is not None:
        limits["maxUsers"] = max_users
    if max_factories is not None:
        limits["maxFactories"] = max_factories

    grace = args.grace_days if args.grace_days is not None else (7 if args.type == "POC" else 0)

    jti = args.jti or uuid.uuid4().hex
    payload: dict = {
        "iss": ISSUER,
        "sub": args.customer_id,
        "jti": jti,
        "iat": int(_now().timestamp()),
        "nbf": int(nbf_dt.timestamp()),
        "exp": int(exp_dt.timestamp()),
        "customerName": args.customer_name,
        "edition": args.edition,
        "enabledModules": enabled,
        "limits": limits,
        "licenceType": args.type,
        "gracePeriodDays": grace,
        "bindingMode": args.binding_mode,
        "featureFlags": _parse_kv(args.feature_flag),
        "deploymentMode": args.deployment_mode,
    }
    if args.binding:
        payload["installationBinding"] = args.binding

    kid = args.kid or keys.CURRENT_SIGNING_KID
    priv_path = _private_key_path(kid, args.private_key)
    if not os.path.exists(priv_path):
        raise SystemExit(f"Private key not found: {priv_path} (run genkey --kid {kid})")
    with open(priv_path, encoding="utf-8") as f:
        private_pem = f.read()

    token = sign_compact(payload, private_pem, kid)

    # Sanity: verify with the embedded public key for this kid before delivery.
    public_pem = keys.get_public_key(kid)
    if public_pem is None:
        print(f"⚠️  WARNING: kid {kid!r} is not embedded in app/licensing/keys.py — "
              "the client app will REJECT this licence until you embed it.")
    else:
        try:
            verify_compact(token, public_pem)
        except LicenceSignatureError as e:
            raise SystemExit(f"Self-verify failed (key mismatch?): {e}") from e

    out_path = args.out or os.path.join(_BACKEND_ROOT, "clients", f"{args.customer_id}.lic")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(token)

    # Record in the issued-licence registry.
    entries = _load_registry()
    entries.append({
        "jti": jti,
        "customerId": args.customer_id,
        "customerName": args.customer_name,
        "edition": args.edition,
        "enabledModules": enabled,
        "limits": limits,
        "licenceType": args.type,
        "kid": kid,
        "iat": payload["iat"],
        "nbf": payload["nbf"],
        "exp": payload["exp"],
        "validFrom": nbf_dt.isoformat(),
        "validUntil": exp_dt.isoformat(),
        "gracePeriodDays": grace,
        "installationBinding": args.binding,
        "deploymentMode": args.deployment_mode,
        "file": out_path,
        "revoked": False,
    })
    _save_registry(entries)

    print(f"Issued licence {jti} for {args.customer_name} [{args.edition}]")
    print(f"  modules : {', '.join(enabled)}")
    print(f"  limits  : {limits or 'unlimited'}")
    print(f"  valid   : {nbf_dt.date()} -> {exp_dt.date()}  (grace {grace}d, type {args.type})")
    print(f"  signed  : kid={kid}")
    print(f"  file    : {out_path}")


def cmd_list(args: argparse.Namespace) -> None:
    entries = _load_registry()
    if not entries:
        print("No licences issued yet.")
        return
    print(f"{'jti':<34} {'customer':<22} {'edition':<14} {'validUntil':<12} {'rev'}")
    print("-" * 92)
    for e in entries:
        vu = e.get("validUntil", "")[:10]
        rev = "yes" if e.get("revoked") else ""
        print(f"{e['jti']:<34} {e['customerName'][:22]:<22} {e['edition']:<14} {vu:<12} {rev}")


def cmd_revoke(args: argparse.Namespace) -> None:
    entries = _load_registry()
    found = False
    for e in entries:
        if e["jti"] == args.jti:
            e["revoked"] = True
            found = True
    if not found:
        raise SystemExit(f"jti {args.jti} not in registry")
    _save_registry(entries)
    print(f"Revoked {args.jti}. NOTE: offline/air-gapped installs cannot check a "
          "CRL — revocation relies on short expiry + binding, not on this flag.")


def cmd_verify(args: argparse.Namespace) -> None:
    with open(args.file, encoding="utf-8") as f:
        token = f.read().strip()
    from app.licensing.crypto import decode_header_unverified

    kid = decode_header_unverified(token).get("kid")
    public_pem = keys.get_public_key(kid)
    if public_pem is None:
        raise SystemExit(f"kid {kid!r} not embedded in this build → app would reject it")
    payload = verify_compact(token, public_pem)
    print(f"OK — signature valid under kid={kid}")
    print(json.dumps(payload, indent=2))


# ── argparse ─────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Vizionforge Licence Authority (internal)")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("genkey", help="generate a new Ed25519 keypair")
    g.add_argument("--kid", required=True, help="key id, e.g. vf-2027-01")
    g.add_argument("--force", action="store_true")
    g.set_defaults(func=cmd_genkey)

    i = sub.add_parser("issue", help="issue + sign a licence")
    i.add_argument("--customer-id", required=True)
    i.add_argument("--customer-name", required=True)
    i.add_argument("--edition", required=True, choices=list(EDITIONS))
    i.add_argument("--modules", help="comma list (CUSTOM, or add-ons to an edition)")
    i.add_argument("--type", default="POC", choices=["POC", "SUBSCRIPTION", "PERPETUAL"])
    i.add_argument("--days", type=int, help="validity length in days (from validFrom)")
    i.add_argument("--valid-from", help="YYYY-MM-DD (default: now)")
    i.add_argument("--valid-until", help="YYYY-MM-DD (alternative to --days)")
    i.add_argument("--grace-days", type=int, help="grace window past expiry (POC default 7)")
    i.add_argument("--max-sites", type=int)
    i.add_argument("--max-users", type=int)
    i.add_argument("--max-factories", type=int)
    i.add_argument("--binding", help="installationId to pin this licence to")
    i.add_argument("--binding-mode", default="SOFT", choices=["SOFT", "STRICT"])
    i.add_argument("--deployment-mode", default="ON_PREM", choices=["ON_PREM", "CLOUD"])
    i.add_argument("--feature-flag", action="append", help="key=true (repeatable)")
    i.add_argument("--kid", help=f"signing key id (default {keys.CURRENT_SIGNING_KID})")
    i.add_argument("--private-key", help="path to private PEM (default .licence_keys/<kid>.private.pem)")
    i.add_argument("--jti", help="override the licence id (default random)")
    i.add_argument("--out", help="output .lic path")
    i.set_defaults(func=cmd_issue)

    li = sub.add_parser("list", help="list issued licences")
    li.set_defaults(func=cmd_list)

    r = sub.add_parser("revoke", help="mark a jti revoked (best-effort)")
    r.add_argument("--jti", required=True)
    r.set_defaults(func=cmd_revoke)

    v = sub.add_parser("verify", help="locally verify a .lic against embedded public key")
    v.add_argument("--file", required=True)
    v.set_defaults(func=cmd_verify)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
