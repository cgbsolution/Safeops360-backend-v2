"""Fold the SYSTEM_ADMIN role into ADMIN and delete it.

SYSTEM_ADMIN and ADMIN were declared aliases of one another but had drifted into
two different grant sets, so "the admin role" quietly depended on which of the
two a user happened to hold. ADMIN is now the single portal administrator.

Order matters — grants are copied BEFORE anything is removed, so the script is
safe to interrupt at any point:

  1. copy every SYSTEM_ADMIN grant ADMIN is missing  (union, no capability lost)
  2. move SYSTEM_ADMIN users onto ADMIN              (UserRole + User.role)
  3. re-point SUPER_ADMIN, which was derived from SYSTEM_ADMIN
  4. delete SYSTEM_ADMIN's grants, assignments, and the role row

Idempotent: a second run finds nothing to do. Dry-run by default.

    python scripts/merge_system_admin_into_admin.py           # report only
    python scripts/merge_system_admin_into_admin.py --apply   # commit
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from sqlalchemy import delete, select  # noqa: E402

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.models.user import (  # noqa: E402
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)

OLD = "SYSTEM_ADMIN"
NEW = "ADMIN"


async def run(apply: bool) -> int:
    async with AsyncSessionLocal() as db:
        roles = {r.code: r for r in (await db.execute(select(Role))).scalars().all()}
        old_role, new_role = roles.get(OLD), roles.get(NEW)

        if old_role is None:
            print(f"{OLD} does not exist — nothing to do (already merged).")
            return 0
        if new_role is None:
            print(f"!! {NEW} role is missing. Run the RBAC seed first; aborting.")
            return 1

        perms = {p.id: p.code for p in (await db.execute(select(Permission))).scalars().all()}
        all_rp = (await db.execute(select(RolePermission))).scalars().all()
        old_g = {(r.permissionId, r.scope) for r in all_rp if r.roleId == old_role.id}
        new_g = {(r.permissionId, r.scope) for r in all_rp if r.roleId == new_role.id}
        missing = old_g - new_g

        old_urs = [u for u in (await db.execute(select(UserRole))).scalars().all()
                   if u.roleId == old_role.id]
        new_pairs = {(u.userId, u.roleId)
                     for u in (await db.execute(select(UserRole))).scalars().all()
                     if u.roleId == new_role.id}
        old_users = (await db.execute(select(User).where(User.role == OLD))).scalars().all()

        print(f"{OLD} grants                    : {len(old_g)}")
        print(f"{NEW} grants                          : {len(new_g)}")
        print(f"grants to copy across (union)      : {len(missing)}")
        for pid, scope in sorted(missing, key=lambda x: (perms.get(x[0]) or "", x[1]))[:20]:
            print(f"    + {perms.get(pid)} [{scope}]")
        if len(missing) > 20:
            print(f"    … and {len(missing) - 20} more")
        print(f"UserRole rows to move              : {len(old_urs)}")
        print(f"User.role columns to rewrite       : {len(old_users)}")
        for u in old_users:
            print(f"    ~ {u.email}")

        if not apply:
            print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
            return 0

        # 1. Union the grants onto ADMIN first, so no capability is ever
        #    momentarily missing even if this is interrupted.
        for pid, scope in missing:
            db.add(RolePermission(roleId=new_role.id, permissionId=pid, scope=scope))
        await db.flush()

        # 2. Move the users. Reuse the existing scope; skip anyone who already
        #    holds ADMIN so we never create a duplicate assignment.
        moved = 0
        for ur in old_urs:
            if (ur.userId, new_role.id) in new_pairs:
                continue
            db.add(UserRole(userId=ur.userId, roleId=new_role.id,
                            scopeType=ur.scopeType, scopeValue=ur.scopeValue))
            new_pairs.add((ur.userId, new_role.id))
            moved += 1
        for u in old_users:
            u.role = NEW
        await db.flush()

        # 3. SUPER_ADMIN was derived from SYSTEM_ADMIN — give it anything it
        #    would otherwise lose when the source role disappears.
        sup = roles.get("SUPER_ADMIN")
        sup_added = 0
        if sup is not None:
            sup_g = {(r.permissionId, r.scope) for r in all_rp if r.roleId == sup.id}
            for pid, scope in old_g - sup_g:
                db.add(RolePermission(roleId=sup.id, permissionId=pid, scope=scope))
                sup_added += 1
            await db.flush()

        # 4. Remove the role itself.
        await db.execute(delete(RolePermission).where(RolePermission.roleId == old_role.id))
        await db.execute(delete(UserRole).where(UserRole.roleId == old_role.id))
        await db.execute(delete(Role).where(Role.id == old_role.id))
        await db.commit()

        print(f"\nApplied: +{len(missing)} grants to {NEW}, {moved} user(s) moved, "
              f"+{sup_added} grants to SUPER_ADMIN, {OLD} deleted.")
        return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Merge SYSTEM_ADMIN into ADMIN and delete it")
    ap.add_argument("--apply", action="store_true", help="commit (default is a dry run)")
    raise SystemExit(asyncio.run(run(ap.parse_args().apply)))
