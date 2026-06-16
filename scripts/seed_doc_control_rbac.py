"""Additive RBAC for Document Control (Pharma IMS Module 2)."""

from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.user import Permission, Role, RolePermission

ACTIONS = ["READ", "CREATE", "UPDATE", "APPROVE", "EXPORT", "REVIEW"]
PERMISSIONS = [{"code": f"DOCUMENT.{a}", "module": "DOCUMENT", "action": a, "description": f"{a} on DOCUMENT"} for a in ACTIONS]

ROLE_DEF = {"code": "DOCUMENT_CONTROLLER", "name": "Document Controller",
            "description": "Owns the controlled-document system; issuance, distribution, archival.",
            "sortOrder": 40, "defaultLanding": "/documents"}

GRANTS: dict[str, tuple[list[str], str]] = {
    "DOCUMENT_CONTROLLER": (ACTIONS, "OWN_PLANT"),
    "QA_OFFICER": (["READ", "CREATE", "REVIEW"], "OWN_PLANT"),
    "QA_MANAGER": (["READ", "CREATE", "REVIEW", "APPROVE", "EXPORT"], "OWN_PLANT"),
    "QA_DIRECTOR": (ACTIONS, "OWN_PLANT"),
    "QC_ANALYST": (["READ", "CREATE"], "OWN_PLANT"),
    "QC_SUPERVISOR": (["READ", "CREATE", "REVIEW"], "OWN_PLANT"),
    "PRODUCTION_SUPERVISOR": (["READ", "CREATE"], "OWN_PLANT"),
    "WORKER": (["READ"], "OWN_PLANT"),
    "CONTRACTOR_WORKMAN": (["READ"], "OWN_PLANT"),
    "SAFETY_OFFICER": (["READ", "CREATE", "REVIEW"], "OWN_PLANT"),
    "HSE_MANAGER": (ACTIONS, "OWN_PLANT"),
    "PLANT_HEAD": (ACTIONS, "OWN_PLANT"),
    "CORPORATE_HSE": (["READ", "EXPORT"], "ALL_PLANTS"),
    "ADMIN": (ACTIONS, "ALL_PLANTS"),
    "SYSTEM_ADMIN": (ACTIONS, "ALL_PLANTS"),
}


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        added_p = 0
        for p in PERMISSIONS:
            if s.execute(select(Permission).where(Permission.code == p["code"])).scalar_one_or_none() is None:
                s.add(Permission(**p)); added_p += 1
        if s.execute(select(Role).where(Role.code == ROLE_DEF["code"])).scalar_one_or_none() is None:
            s.add(Role(code=ROLE_DEF["code"], name=ROLE_DEF["name"], description=ROLE_DEF["description"],
                       isSystem=False, sortOrder=ROLE_DEF["sortOrder"], defaultLanding=ROLE_DEF["defaultLanding"], isActive=True))
        s.flush()
        roles = {r.code: r for r in s.execute(select(Role)).scalars().all()}
        perms = {p.code: p for p in s.execute(select(Permission)).scalars().all()}
        added_g = 0
        for rc, (acts, scope) in GRANTS.items():
            role = roles.get(rc)
            if role is None:
                continue
            for a in acts:
                perm = perms.get(f"DOCUMENT.{a}")
                if perm is None:
                    continue
                ex = s.execute(select(RolePermission).where(RolePermission.roleId == role.id, RolePermission.permissionId == perm.id)).scalar_one_or_none()
                if ex is not None:
                    if ex.scope != scope:
                        ex.scope = scope
                    continue
                s.add(RolePermission(roleId=role.id, permissionId=perm.id, scope=scope)); added_g += 1
        s.commit()
        print(f"DOCUMENT perms added: {added_p} | grants added: {added_g} | DOCUMENT_CONTROLLER role ensured")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
