"""Elevate the HSE_MANAGER role to ALL_PLANTS scope on every permission.

Per product decision: the HSE Manager is the org-wide safety lead and should
have full cross-plant access across all modules. This sets every
RolePermission.scope for the HSE_MANAGER role to ALL_PLANTS.

Prints the BEFORE per-permission scopes (so the change is reversible) and the
AFTER distribution. Read other roles are untouched.

    .venv/Scripts/python.exe scripts/elevate_hse_manager_rbac.py [ROLE_CODE]
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, text

from app.core.config import get_settings

ROLE_CODE = sys.argv[1] if len(sys.argv) > 1 else "HSE_MANAGER"


def main() -> int:
    eng = create_engine(get_settings().sync_database_url, future=True)
    with eng.begin() as c:
        role = c.execute(
            text('SELECT id, code, name FROM "Role" WHERE code = :c'),
            {"c": ROLE_CODE},
        ).mappings().first()
        if not role:
            print(f"!! no role with code {ROLE_CODE!r}")
            return 1
        rid = role["id"]
        print(f"Role: {role['code']} ({role['name']})  id={rid}\n")

        before = c.execute(
            text(
                'SELECT p.code AS perm, rp.scope '
                'FROM "RolePermission" rp JOIN "Permission" p ON p.id = rp."permissionId" '
                'WHERE rp."roleId" = :rid ORDER BY p.code'
            ),
            {"rid": rid},
        ).mappings().all()
        print(f"BEFORE — {len(before)} permission grants (for reversibility):")
        for r in before:
            print(f"   {r['scope']:<14} {r['perm']}")

        res = c.execute(
            text(
                'UPDATE "RolePermission" SET scope = :s '
                'WHERE "roleId" = :rid AND scope <> :s'
            ),
            {"s": "ALL_PLANTS", "rid": rid},
        )
        print(f"\nUpdated {res.rowcount} grant(s) to ALL_PLANTS.")

        after = c.execute(
            text(
                'SELECT scope, count(*) AS n FROM "RolePermission" '
                'WHERE "roleId" = :rid GROUP BY scope'
            ),
            {"rid": rid},
        ).mappings().all()
        print("AFTER — scope distribution: " + ", ".join(f"{r['scope']}={r['n']}" for r in after))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
