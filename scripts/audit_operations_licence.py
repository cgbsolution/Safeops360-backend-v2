#!/usr/bin/env python
"""Pre-cutover audit — who loses what when FIRE + CHEMICAL start being gated.

    python scripts/audit_operations_licence.py

WHY THIS EXISTS
---------------
`fire_safety`, `fire_checklists` and `chemical` were mounted with no entry in
`ROUTER_MODULE`, so every deployment could reach the fire register, the checklist
engine and the full hazmat inventory regardless of what its licence granted.
Adding them to the map makes the licence real for the first time — which means
the deploy is not a no-op for anyone whose licence predates those two codes.

Run this against each installation BEFORE deploying that change. It answers one
question per deployment: does this licence carry FIRE and CHEMICAL, and if not,
what is already in the database that will become unreachable?

SCOPE — READ THIS BEFORE TRUSTING THE OUTPUT
--------------------------------------------
This portal is single-tenant: ONE organisation per installation, with one signed
licence.lic. There is no per-tenant licence table, so "audit every tenant" means
running this once per deployment, not once here. This script reports on the
licence THIS checkout is configured with and nothing else.

It also cannot fix anything. The private Ed25519 signing key lives only with the
Licence Authority (`.licence_keys/`, gitignored, absent here), so reissuing is
`scripts/licence_authority.py issue …` on the Authority host — see REMEDIATION
in the output.

Read-only: no writes, no licence mutation.
"""

from __future__ import annotations

import asyncio
import os
import sys

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

# Windows consoles default to cp1252, which cannot encode the rules and arrows
# below — and a UnicodeEncodeError halfway through would leave the operator with
# a half-printed audit they might act on.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import func, select  # noqa: E402

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.licensing import org_entitlements  # noqa: E402
from app.licensing.keys import get_public_key  # noqa: E402
from app.licensing.registry import ALL_PRODUCT_CODES, MODULE_REGISTRY  # noqa: E402
from app.licensing.router_map import ROUTER_MODULE  # noqa: E402
from app.licensing.state import get_state, refresh_state  # noqa: E402
from app.licensing.validator import evaluate_licence  # noqa: E402

# The routers this cutover starts gating, and the prefixes that go dark with them.
CUTOVER = {
    "fire_safety": ("FIRE", "/api/fire/*"),
    "fire_checklists": ("FIRE", "/api/fire/checklists/*, /api/fire/assets/*"),
    "chemical": ("CHEMICAL", "/api/chemicals/*"),
}


def rule(title: str) -> None:
    print(f"\n{title}\n" + "─" * max(len(title), 66))


# ── Fleet mode ───────────────────────────────────────────────────────────────
#
# The per-installation run below needs that installation's database. Fleet mode
# does not: a .lic file is self-contained and signature-verified offline, so one
# operator holding the collected licence files can answer "which installations
# lose Fire or Chemical" for all of them in one pass, without a connection to
# any of them. That is the only form the "audit every tenant" question can take
# in a product where each install carries its own licence.
def audit_files(paths: list[str]) -> int:
    from datetime import datetime, timezone

    rule("FLEET AUDIT — licence files only (no database required)")
    print(f"  {'FILE':<34} {'CUSTOMER':<24} {'STATUS':<14} {'FIRE':<6} {'CHEMICAL':<9} MISSING")
    print("  " + "-" * 104)
    at_risk: list[str] = []
    for path in paths:
        name = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as f:
                token = f.read().strip()
        except OSError as e:
            print(f"  {name:<34} unreadable: {e}")
            at_risk.append(name)
            continue
        state = evaluate_licence(
            token,
            system_now=datetime.now(timezone.utc),
            last_seen=None,
            # Binding is per-installation and cannot be checked from a file
            # alone; a STRICT-bound licence therefore reports its binding
            # warning rather than a false failure.
            local_installation_id=None,
            public_key_resolver=get_public_key,
        )
        p = state.payload
        customer = (p.customer_name if p else "—")[:23]
        fire = "yes" if "FIRE" in state.enabled_module_set else "NO"
        chem = "yes" if "CHEMICAL" in state.enabled_module_set else "NO"
        missing = len(set(ALL_PRODUCT_CODES) - set(state.enabled_module_set))
        print(f"  {name:<34} {customer:<24} {state.status:<14} {fire:<6} {chem:<9} {missing}")
        if fire == "NO" or chem == "NO" or not state.is_operational:
            at_risk.append(name)

    print()
    if at_risk:
        print(f"  {len(at_risk)} of {len(paths)} licence(s) MUST be reissued before the cutover:")
        for n in at_risk:
            print(f"    - {n}")
        print("\n  Reissue on the Authority host (scripts/licence_authority.py issue), then")
        print("  re-run this to confirm before deploying the ROUTER_MODULE change.")
    else:
        print(f"  All {len(paths)} licence(s) already carry FIRE and CHEMICAL — safe to deploy.")
    return 1 if at_risk else 0


async def main() -> int:
    state = await refresh_state()
    payload = state.payload

    rule("LICENCE")
    print(f"  status          {state.status}")
    if payload is None:
        print("  No valid licence payload — every product module is already locked.")
        print("  Gating FIRE/CHEMICAL changes nothing here; fix the licence first.")
        return 2
    print(f"  customer        {payload.customer_name} ({payload.sub})")
    print(f"  edition         {payload.edition}")
    print(f"  issued          {payload.issued_at:%Y-%m-%d}   jti {payload.jti[:12]}…")
    print(f"  type            {payload.licence_type}   deployment {payload.deployment_mode}")
    print(f"  expires         {payload.valid_until:%Y-%m-%d}  ({state.days_to_expiry} days)")
    print(f"  modules granted {len(state.enabled_module_set)}")

    async with AsyncSessionLocal() as db:
        await org_entitlements.refresh(db)

        rule("THE TWO CODES THIS CUTOVER DEPENDS ON")
        verdicts: dict[str, bool] = {}
        for code in ("FIRE", "CHEMICAL"):
            in_licence = code in state.enabled_module_set
            org_off = not org_entitlements.is_enabled_for_org(code)
            verdicts[code] = in_licence and not org_off
            mark = "GRANTED" if in_licence else "NOT IN LICENCE"
            note = ""
            if in_licence and org_off:
                note = f"  ← but switched OFF org-wide: {org_entitlements.note_for(code) or 'no note'}"
            print(f"  {code:<10} {mark}{note}")

        rule("WHAT GOES DARK ON DEPLOY")
        losing = [(r, c, p) for r, (c, p) in CUTOVER.items() if not verdicts[c]]
        if not losing:
            print("  Nothing. Both codes are granted and enabled — this deploy is a no-op")
            print("  for access, and only closes the hole for deployments that lack them.")
        else:
            for router, code, prefixes in losing:
                assert ROUTER_MODULE.get(router) == code, f"{router} not mapped to {code}"
                print(f"  {router:<16} {prefixes}  → 403 (needs {code})")

        # Concrete blast radius: what already exists behind those routes.
        rule("DATA ALREADY BEHIND THOSE ROUTES")
        from app.models.capture import CaptureSubmission
        from app.models.chemical import ChemicalInventoryItem, ChemicalMaster
        from app.models.fire_safety import FireEquipment

        async def count(model, *where) -> int:
            q = select(func.count()).select_from(model)
            for w in where:
                q = q.where(w)
            return (await db.execute(q)).scalar() or 0

        fire_assets = await count(FireEquipment, FireEquipment.isDeleted.is_(False))
        chem_masters = await count(ChemicalMaster)
        chem_items = await count(ChemicalInventoryItem)
        linked_reports = await count(CaptureSubmission, CaptureSubmission.fireAssetId.isnot(None))
        print(f"  fire assets registered      {fire_assets}")
        print(f"  field reports linked to one {linked_reports}")
        print(f"  chemicals in the master     {chem_masters}")
        print(f"  chemical inventory items    {chem_items}")
        if losing and (fire_assets or chem_masters or chem_items):
            print("\n  ⚠  This deployment is USING data it is not licensed for. Losing the")
            print("     routes does not delete it, but the screens stop opening — so the")
            print("     licence has to be reissued BEFORE this deploys, not after.")

        rule("EVERY PRODUCT MODULE THIS LICENCE IS MISSING")
        missing = sorted(set(ALL_PRODUCT_CODES) - set(state.enabled_module_set))
        if not missing:
            print("  None — the licence covers the whole registry.")
        else:
            print(f"  {len(missing)} of {len(ALL_PRODUCT_CODES)} product codes are absent.")
            print("  FULL_PLATFORM expands from the registry AT ISSUE TIME, so a licence")
            print("  issued before a module was registered never gains it — reissuing is")
            print("  the only way to pick these up:")
            for code in missing:
                d = MODULE_REGISTRY[code]
                flag = "  ←  this cutover" if code in ("FIRE", "CHEMICAL") else ""
                print(f"    {code:<22} {d.name}{flag}")

        rule("REMEDIATION (Licence Authority host — the private key is not in this repo)")
        need = [c for c in ("FIRE", "CHEMICAL") if c not in state.enabled_module_set]
        if need:
            print("  python scripts/licence_authority.py issue \\")
            print(f"      --customer-id {payload.sub} --customer-name {payload.customer_name!r} \\")
            print(f"      --edition {payload.edition} --type {payload.licence_type} \\")
            print(f"      --deployment-mode {payload.deployment_mode} \\")
            print(f"      --out {payload.sub}.lic")
            print(f"\n  Reissuing {payload.edition} today re-expands from the current registry,")
            print(f"  which picks up {', '.join(need)} and the other {len(missing) - len(need)} missing codes.")
            print("  If that is TOO MUCH for what the client bought, issue --edition CUSTOM")
            print("  with an explicit --modules list instead — do not hand a client the whole")
            print("  platform just to unblock two routes.")
        else:
            print("  No reissue needed for this cutover.")

        rule("COMMERCIAL")
        print("  There is no OPERATIONS SKU. FIRE and CHEMICAL are the Operations bundle,")
        print("  so any deal that says 'Operations' has to be read against these two codes")
        print("  before its licence is issued. Confirm with whoever owns commercial paper")
        print("  which existing agreements that covers.")

    return 1 if losing else 0


if __name__ == "__main__":
    import argparse
    import glob

    ap = argparse.ArgumentParser(
        description="Audit whether FIRE + CHEMICAL are licensed before the Operations cutover.",
    )
    ap.add_argument(
        "--fleet", metavar="PATH", nargs="+",
        help="audit .lic FILES or directories instead of this installation "
             "(no database needed) — e.g. --fleet clients/ or --fleet a.lic b.lic",
    )
    ns = ap.parse_args()

    if ns.fleet:
        files: list[str] = []
        for entry in ns.fleet:
            if os.path.isdir(entry):
                files.extend(sorted(glob.glob(os.path.join(entry, "*.lic"))))
            else:
                files.append(entry)
        if not files:
            raise SystemExit("No .lic files found in the given paths.")
        raise SystemExit(audit_files(files))

    raise SystemExit(asyncio.run(main()))
