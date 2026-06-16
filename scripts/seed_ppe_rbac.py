"""Additive PPE RBAC seed (PPE-01) — Phase 2 IMS.

Adds the PPE.* permission catalogue + role grants WITHOUT wiping the existing
RolePermission matrix (unlike `python -m app.seed.seed_rbac`, which rebuilds
every grant from scratch and would drop any out-of-band elevation). Idempotent:
upserts permissions, ensures the STORE_KEEPER role, and inserts only the
RolePermission rows that don't already exist. The canonical full-reseed copy of
these grants also lives in app/seed/seed_rbac.py.

Run from the backend root:
    .venv/Scripts/python.exe scripts/seed_ppe_rbac.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.user import Permission, Role, RolePermission

CRUD = ["CREATE", "READ", "UPDATE", "DELETE", "APPROVE", "EXECUTE", "VERIFY", "CLOSE", "EXPORT"]
SPECIAL = ["ISSUE", "INSPECT", "CATALOG_MANAGE", "RETIRE_APPROVE", "RECALL_MANAGE"]
ALL_PPE = CRUD + SPECIAL

PERMISSIONS = [
    {"code": f"PPE.{a}", "module": "PPE", "action": a, "description": f"{a} on PPE"} for a in CRUD
] + [
    {"code": "PPE.ISSUE", "module": "PPE", "action": "ISSUE", "description": "Issue / return PPE to a person"},
    {"code": "PPE.INSPECT", "module": "PPE", "action": "INSPECT", "description": "Conduct a PPE inspection"},
    {"code": "PPE.CATALOG_MANAGE", "module": "PPE", "action": "CATALOG_MANAGE", "description": "Create / edit PPE catalog types"},
    {"code": "PPE.RETIRE_APPROVE", "module": "PPE", "action": "RETIRE_APPROVE", "description": "Approve PPE item retirement"},
    {"code": "PPE.RECALL_MANAGE", "module": "PPE", "action": "RECALL_MANAGE", "description": "Initiate / resolve PPE batch recalls"},
]

# role_code → (actions, scope). Mirrors §7.1 of the build prompt.
GRANTS: dict[str, tuple[list[str], str]] = {
    "WORKER": (["READ"], "OWN_RECORDS"),
    "CONTRACTOR_WORKMAN": (["READ"], "OWN_RECORDS"),
    # Spec §3: Dept Supervisor issues PPE and records returns for OWN dept
    # (the return route also gates on PPE.ISSUE).
    "SUPERVISOR": (["READ", "ISSUE"], "OWN_DEPARTMENT"),
    # PTW issuers pre-flight crew PPE via POST /api/ppe/compliance/ptw-gate-check
    # (PPE-01 Pass 2) — needs read access to PPE compliance state.
    "PERMIT_ISSUER": (["READ"], "OWN_PLANT"),
    "STORE_KEEPER": (["CREATE", "READ", "UPDATE", "EXPORT", "ISSUE"], "OWN_PLANT"),
    "SAFETY_OFFICER": (["READ", "CREATE", "UPDATE", "EXPORT", "ISSUE", "INSPECT", "VERIFY", "RETIRE_APPROVE"], "OWN_PLANT"),
    "MAINTENANCE_HEAD": (["READ"], "OWN_PLANT"),
    "HSE_MANAGER": (ALL_PPE, "OWN_PLANT"),
    "PLANT_HEAD": (["READ", "EXPORT", "RETIRE_APPROVE", "RECALL_MANAGE"], "OWN_PLANT"),
    "CORPORATE_HSE": (["READ", "EXPORT", "CATALOG_MANAGE", "RETIRE_APPROVE", "RECALL_MANAGE"], "ALL_PLANTS"),
    "ADMIN": (ALL_PPE, "ALL_PLANTS"),
    "SYSTEM_ADMIN": (ALL_PPE, "ALL_PLANTS"),
}

STORE_KEEPER = {
    "code": "STORE_KEEPER",
    "name": "Store Keeper",
    "description": "Owns the safety store; issues / receives PPE and records goods receipt.",
    "isSystem": False,
    "sortOrder": 85,
    "defaultLanding": "/ppe",
}


def main() -> int:
    settings = get_settings()
    engine = create_engine(settings.sync_database_url, future=True)
    with Session(engine) as s:
        # 1) Permissions
        perms_added = 0
        for p in PERMISSIONS:
            existing = s.execute(select(Permission).where(Permission.code == p["code"])).scalar_one_or_none()
            if existing is None:
                s.add(Permission(**p))
                perms_added += 1
        s.flush()

        # 2) STORE_KEEPER role
        sk = s.execute(select(Role).where(Role.code == "STORE_KEEPER")).scalar_one_or_none()
        if sk is None:
            sk = Role(**STORE_KEEPER, isActive=True)
            s.add(sk)
            s.flush()
            print("   + created role STORE_KEEPER")

        roles_by_code = {r.code: r for r in s.execute(select(Role)).scalars().all()}
        perms_by_code = {p.code: p for p in s.execute(select(Permission)).scalars().all()}

        # 3) Grants (additive — skip ones that already exist)
        grants_added = 0
        skipped_roles: list[str] = []
        for role_code, (actions, scope) in GRANTS.items():
            role = roles_by_code.get(role_code)
            if role is None:
                skipped_roles.append(role_code)
                continue
            for action in actions:
                perm = perms_by_code.get(f"PPE.{action}")
                if perm is None:
                    continue
                exists = s.execute(
                    select(RolePermission).where(
                        RolePermission.roleId == role.id,
                        RolePermission.permissionId == perm.id,
                    )
                ).scalar_one_or_none()
                if exists is not None:
                    # Keep the scope in sync if it drifted.
                    if exists.scope != scope:
                        exists.scope = scope
                    continue
                s.add(RolePermission(roleId=role.id, permissionId=perm.id, scope=scope))
                grants_added += 1

        s.commit()
        print(f"Permissions added : {perms_added} (PPE catalogue total {len(PERMISSIONS)})")
        print(f"Grants added      : {grants_added}")
        if skipped_roles:
            print(f"Roles not found   : {skipped_roles}")
        print("Done. PPE RBAC applied additively (no existing grants wiped).")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
