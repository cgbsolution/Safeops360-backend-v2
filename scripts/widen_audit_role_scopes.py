"""Widen the three audit-seat roles so anyone seated on an audit can work it.

Mirrors the scope changes made in `SafeOps360/prisma/seed-rbac.ts` onto the
live roles, which were seeded before that edit. Fresh environments get this
from the seed; existing ones need this script.

    LEAD_AUDITOR  AUDIT_COMPLIANCE.*        OWN_PLANT   -> ALL_PLANTS
    AUDITOR       AUDIT_COMPLIANCE.*        OWN_PLANT   -> ALL_PLANTS
    AUDITEE       AUDIT_COMPLIANCE.UPDATE   OWN_RECORDS -> ALL_PLANTS
    AUDITEE       AUDIT_COMPLIANCE.READ     OWN_RECORDS -> UNCHANGED

Why the auditee is split rather than widened wholesale: `_party_filter_for`
narrows the Audits register to a person's own engagements only while EVERY
AUDIT_COMPLIANCE.READ grant they hold is OWN_RECORDS. Widening READ as well
would quietly turn 75 auditees into company-wide audit readers. UPDATE is what
`_scope_covers_plant` consults for the auditee seat, so widening only UPDATE
makes them assignable everywhere while their register keeps its blinkers.
Responding stays gated by the routing check in `transition_checkpoint`.

Nothing here grants a NEW permission — every row already existed; only its
scope changes. Rows are matched by (role code, permission code), so a re-run
is a no-op.

Dry run by default. Pass --apply to write.

    python scripts/widen_audit_role_scopes.py
    python scripts/widen_audit_role_scopes.py --apply

The backend caches each user's permission snapshot for 5 minutes, so allow up
to that (or restart the service) before the change is visible.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.models.user import Permission, Role, RolePermission, UserRole  # noqa: E402
from app.services.permissions import invalidate_user_permissions  # noqa: E402

# role code -> (permission codes to widen, or None for "every AUDIT_COMPLIANCE.*")
PLAN: dict[str, set[str] | None] = {
    "LEAD_AUDITOR": None,
    "AUDITOR": None,
    "AUDITEE": {"AUDIT_COMPLIANCE.UPDATE"},
}
TARGET_SCOPE = "ALL_PLANTS"


async def main(apply: bool) -> None:
    async with AsyncSessionLocal() as db:
        changes: list[tuple[str, str, str]] = []

        for role_code, only in PLAN.items():
            role = (
                await db.execute(select(Role).where(Role.code == role_code))
            ).scalars().first()
            if role is None:
                print(f"!! role {role_code} not found — skipped")
                continue

            seats = (
                await db.execute(
                    select(UserRole.userId).where(UserRole.roleId == role.id)
                )
            ).all()
            holders = {r[0] for r in seats}

            rows = (
                await db.execute(
                    select(RolePermission, Permission)
                    .join(Permission, Permission.id == RolePermission.permissionId)
                    .where(RolePermission.roleId == role.id)
                    .where(Permission.code.like("AUDIT_COMPLIANCE.%"))
                    .order_by(Permission.code)
                )
            ).all()

            print(f"\n{'=' * 74}\n{role_code} — {len(holders)} user(s) hold this role")
            for rp, perm in rows:
                targeted = only is None or perm.code in only
                if not targeted:
                    print(f"   {perm.code:35} {rp.scope:12} unchanged (deliberate)")
                    continue
                if rp.scope == TARGET_SCOPE:
                    print(f"   {perm.code:35} {rp.scope:12} already correct")
                    continue
                print(f"   {perm.code:35} {rp.scope:12} -> {TARGET_SCOPE}")
                changes.append((role_code, perm.code, rp.scope))
                if apply:
                    rp.scope = TARGET_SCOPE

            if apply:
                # The snapshot is cached for 5 minutes per user; clear the
                # holders so the change lands on their next request rather
                # than whenever the TTL happens to expire.
                for uid in holders:
                    invalidate_user_permissions(uid)

        print(f"\n{'=' * 74}")
        if not changes:
            print("nothing to change — every targeted grant is already ALL_PLANTS.")
        elif apply:
            await db.commit()
            print(f"committed {len(changes)} scope change(s).")
            print("NOTE: this process cleared its own cache; the running API server has")
            print("its own. Restart it, or allow up to 5 minutes, before testing.")
        else:
            print(f"DRY RUN — {len(changes)} scope change(s) pending. Re-run with --apply.")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
