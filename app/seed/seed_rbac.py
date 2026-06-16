"""RBAC seed — direct port of prisma/seed-rbac.ts.

Idempotent. Run as:
  python -m app.seed.seed_rbac

Adds expanded roles, all permission codes, the default Role × Permission
matrix, and assigns demo users to RBAC roles via UserRole rows. Does NOT
touch the User.role denormalised column.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.user import Permission, Role, RolePermission, User, UserRole

ADDITIONAL_ROLES: list[dict[str, Any]] = [
    {"code": "SYSTEM_ADMIN", "name": "System Administrator", "description": "Full configuration access. Alias of ADMIN.", "isSystem": True, "sortOrder": 1, "defaultLanding": "/configuration/workflows"},
    {"code": "CORPORATE_HSE", "name": "Corporate HSE", "description": "All-plants HSE leadership; manages master data and roll-up reports.", "isSystem": True, "sortOrder": 5, "defaultLanding": "/dashboard"},
    {"code": "PERMIT_ISSUER", "name": "Permit Issuer", "description": "Originates and approves permits as Issuer.", "isSystem": True, "sortOrder": 25, "defaultLanding": "/inbox"},
    {"code": "SAFETY_OFFICER", "name": "Safety Officer", "description": "Verifies observations, near-miss closure, permit safety conditions.", "isSystem": True, "sortOrder": 27, "defaultLanding": "/inbox"},
    {"code": "SUPERVISOR", "name": "Supervisor", "description": "Frontline supervisor; raises FLRA, approves observations within own department.", "isSystem": True, "sortOrder": 80, "defaultLanding": "/inbox"},
    {"code": "DEPARTMENT_HEAD", "name": "Department Head", "description": "Department-scoped approval authority for non-permit records.", "isSystem": False, "sortOrder": 75, "defaultLanding": "/inbox"},
    {"code": "MAINTENANCE_HEAD", "name": "Maintenance Head", "description": "Owns equipment master + inspection assignments.", "isSystem": False, "sortOrder": 65, "defaultLanding": "/inspections"},
    {"code": "LD_MANAGER", "name": "L&D Manager", "description": "Owns training programs across all plants.", "isSystem": False, "sortOrder": 55, "defaultLanding": "/training"},
    {"code": "TRAINER", "name": "Trainer", "description": "Conducts training sessions and records outcomes.", "isSystem": False, "sortOrder": 90, "defaultLanding": "/training"},
    {"code": "CONTRACTOR_WORKMAN", "name": "Contractor Workman", "description": "External crew member; restricted to records they're crew on.", "isSystem": False, "sortOrder": 110, "defaultLanding": "/inbox"},
    # PPE Management — Phase 2 IMS. Store Keeper owns the safety store: issuance + receipt.
    {"code": "STORE_KEEPER", "name": "Store Keeper", "description": "Owns the safety store; issues / receives PPE and records goods receipt.", "isSystem": False, "sortOrder": 85, "defaultLanding": "/ppe"},
    # Skill Matrix — Phase 1 IMS (only HR_HEAD + EXTERNAL_ASSESSOR are new).
    {"code": "HR_HEAD", "name": "HR Head", "description": "Owns competency management cross-plant.", "isSystem": False, "sortOrder": 35, "defaultLanding": "/skill-matrix"},
    {"code": "EXTERNAL_ASSESSOR", "name": "External Assessor", "description": "External party scoped to assigned competency assessments.", "isSystem": False, "sortOrder": 95, "defaultLanding": "/skill-matrix"},
    # EPC — Engineering, Procurement & Construction module roles
    {"code": "SITE_HSE_MANAGER", "name": "Site HSE Manager (EPC)", "description": "HSE Manager at a specific construction site. Approves mobilizations, conducts inductions, overrides gate clearance.", "isSystem": False, "sortOrder": 42, "defaultLanding": "/epc"},
    {"code": "CONTRACTOR_COORDINATOR", "name": "Contractor Coordinator", "description": "Manages contractor company registration and worker mobilization.", "isSystem": False, "sortOrder": 58, "defaultLanding": "/epc/contractors"},
    {"code": "GATE_GUARD", "name": "Gate Guard", "description": "Performs gate clearance checks at construction site entry points.", "isSystem": False, "sortOrder": 105, "defaultLanding": "/epc/gate"},
]

# NOTE: this backend list is a deliberate subset (omits HIRA/CAPA/EAI which are
# seeded canonically by prisma/seed-rbac.ts). SKILL_MATRIX added for Phase 1 IMS;
# PPE added for Phase 2 IMS (gives PPE.{CREATE,READ,UPDATE,EXPORT,…} codes).
OPERATIONAL_MODULES = ["OBSERVATION", "NEAR_MISS", "PTW", "FLRA", "INCIDENT", "TRAINING", "INSPECTION", "MANHOURS", "SKILL_MATRIX", "PPE", "EPC", "AUDIT_COMPLIANCE"]
OPERATIONAL_ACTIONS = ["CREATE", "READ", "UPDATE", "DELETE", "APPROVE", "EXECUTE", "VERIFY", "CLOSE", "EXPORT"]

EXTRA_PERMISSIONS = [
    {"code": "CONFIGURATION.MASTERS", "module": "CONFIGURATION", "action": "MASTERS", "description": "Manage master data"},
    {"code": "CONFIGURATION.WORKFLOWS", "module": "CONFIGURATION", "action": "WORKFLOWS", "description": "Edit workflow definitions and steps"},
    {"code": "CONFIGURATION.USERS", "module": "CONFIGURATION", "action": "USERS", "description": "Create / edit / disable user accounts"},
    {"code": "CONFIGURATION.PERMISSIONS", "module": "CONFIGURATION", "action": "PERMISSIONS", "description": "Edit role × permission matrix"},
    {"code": "CONFIGURATION.ROLES", "module": "CONFIGURATION", "action": "ROLES", "description": "Create / edit roles and assign role membership"},
    {"code": "AUDIT.VIEW", "module": "AUDIT", "action": "VIEW", "description": "Read audit log"},
    # Skill Matrix non-CRUD (CRUD comes from OPERATIONAL_MODULES.SKILL_MATRIX)
    {"code": "SKILL_MATRIX.COMPETENCY_CONFIGURE", "module": "SKILL_MATRIX", "action": "COMPETENCY_CONFIGURE", "description": "Create / edit Competency + Skill masters"},
    {"code": "SKILL_MATRIX.ROLE_DEF_CONFIGURE", "module": "SKILL_MATRIX", "action": "ROLE_DEF_CONFIGURE", "description": "Create / edit RoleDefinition + requirements"},
    {"code": "SKILL_MATRIX.ASSESS", "module": "SKILL_MATRIX", "action": "ASSESS", "description": "Conduct competency assessments / sign supervised records"},
    {"code": "SKILL_MATRIX.SUSPEND", "module": "SKILL_MATRIX", "action": "SUSPEND", "description": "Suspend / reinstate a competency"},
    {"code": "SKILL_MATRIX.APPROVE_OVERRIDE", "module": "SKILL_MATRIX", "action": "APPROVE_OVERRIDE", "description": "Approve role assignment beyond grace period"},
    {"code": "SKILL_MATRIX.RECERT_CYCLE", "module": "SKILL_MATRIX", "action": "RECERT_CYCLE", "description": "Initiate / manage re-certification cycles"},
    {"code": "SKILL_MATRIX.CROSS_PERSON_VIEW", "module": "SKILL_MATRIX", "action": "CROSS_PERSON_VIEW", "description": "View competency records outside the holder's scope"},
    {"code": "SKILL_MATRIX.VERSION_VIEW", "module": "SKILL_MATRIX", "action": "VERSION_VIEW", "description": "View CompetencyRecordVersion history"},
    # PPE non-CRUD (CRUD comes from OPERATIONAL_MODULES.PPE) — Phase 2 IMS
    {"code": "PPE.ISSUE", "module": "PPE", "action": "ISSUE", "description": "Issue / return PPE to a person"},
    {"code": "PPE.INSPECT", "module": "PPE", "action": "INSPECT", "description": "Conduct a PPE inspection"},
    {"code": "PPE.CATALOG_MANAGE", "module": "PPE", "action": "CATALOG_MANAGE", "description": "Create / edit PPE catalog types"},
    {"code": "PPE.RETIRE_APPROVE", "module": "PPE", "action": "RETIRE_APPROVE", "description": "Approve PPE item retirement"},
    {"code": "PPE.RECALL_MANAGE", "module": "PPE", "action": "RECALL_MANAGE", "description": "Initiate / resolve PPE batch recalls"},
    # EPC — Engineering, Procurement & Construction module
    {"code": "EPC.GATE_OVERRIDE", "module": "EPC", "action": "GATE_OVERRIDE", "description": "Override a gate clearance rejection (HSE Manager / Site Manager only)"},
    {"code": "EPC.INDUCTION_CONDUCT", "module": "EPC", "action": "INDUCTION_CONDUCT", "description": "Conduct and record site inductions"},
    {"code": "EPC.MOBILIZATION_APPROVE", "module": "EPC", "action": "MOBILIZATION_APPROVE", "description": "Approve worker mobilization to a site"},
    {"code": "EPC.PREQUALIFY", "module": "EPC", "action": "PREQUALIFY", "description": "Approve or reject contractor company pre-qualification"},
]


# scope: ALL_PLANTS / OWN_PLANT / OWN_DEPARTMENT / OWN_RECORDS
ROLE_GRANTS: dict[str, list[dict[str, Any]]] = {
    "WORKER": [
        {"module": "SKILL_MATRIX", "actions": ["READ"], "scope": "OWN_RECORDS"},
        {"module": "OBSERVATION", "actions": ["CREATE", "READ"], "scope": "OWN_RECORDS"},
        {"module": "NEAR_MISS", "actions": ["CREATE", "READ"], "scope": "OWN_RECORDS"},
        {"module": "INCIDENT", "actions": ["CREATE", "READ"], "scope": "OWN_RECORDS"},
        {"module": "PTW", "actions": ["READ"], "scope": "OWN_RECORDS"},
        {"module": "FLRA", "actions": ["READ", "EXECUTE"], "scope": "OWN_RECORDS"},
        {"module": "TRAINING", "actions": ["READ"], "scope": "OWN_RECORDS"},
        {"module": "PPE", "actions": ["READ"], "scope": "OWN_RECORDS"},
    ],
    "CONTRACTOR_WORKMAN": [
        {"module": "SKILL_MATRIX", "actions": ["READ"], "scope": "OWN_RECORDS"},
        {"module": "OBSERVATION", "actions": ["CREATE", "READ"], "scope": "OWN_RECORDS"},
        {"module": "NEAR_MISS", "actions": ["CREATE", "READ"], "scope": "OWN_RECORDS"},
        {"module": "INCIDENT", "actions": ["CREATE", "READ"], "scope": "OWN_RECORDS"},
        {"module": "FLRA", "actions": ["READ", "EXECUTE"], "scope": "OWN_RECORDS"},
        {"module": "PTW", "actions": ["READ"], "scope": "OWN_RECORDS"},
        {"module": "PPE", "actions": ["READ"], "scope": "OWN_RECORDS"},
    ],
    "STORE_KEEPER": [
        {"module": "PPE", "actions": ["CREATE", "READ", "UPDATE", "EXPORT", "ISSUE"], "scope": "OWN_PLANT"},
        {"module": "INSPECTION", "actions": ["READ"], "scope": "OWN_PLANT"},
    ],
    "SUPERVISOR": [
        {"module": "SKILL_MATRIX", "actions": ["READ", "ASSESS", "EXECUTE"], "scope": "OWN_DEPARTMENT"},
        {"module": "OBSERVATION", "actions": ["CREATE", "READ", "APPROVE"], "scope": "OWN_DEPARTMENT"},
        {"module": "NEAR_MISS", "actions": ["CREATE", "READ"], "scope": "OWN_DEPARTMENT"},
        {"module": "INCIDENT", "actions": ["CREATE", "READ"], "scope": "OWN_DEPARTMENT"},
        {"module": "PTW", "actions": ["READ"], "scope": "OWN_DEPARTMENT"},
        {"module": "FLRA", "actions": ["CREATE", "READ", "EXECUTE"], "scope": "OWN_DEPARTMENT"},
        {"module": "TRAINING", "actions": ["READ"], "scope": "OWN_DEPARTMENT"},
        {"module": "PPE", "actions": ["READ"], "scope": "OWN_DEPARTMENT"},
    ],
    "PERMIT_ISSUER": [
        {"module": "OBSERVATION", "actions": ["CREATE", "READ"], "scope": "OWN_PLANT"},
        {"module": "NEAR_MISS", "actions": ["CREATE", "READ"], "scope": "OWN_PLANT"},
        {"module": "INCIDENT", "actions": ["CREATE", "READ"], "scope": "OWN_PLANT"},
        {"module": "PTW", "actions": ["CREATE", "READ", "UPDATE", "APPROVE", "EXPORT"], "scope": "OWN_PLANT"},
        {"module": "FLRA", "actions": ["CREATE", "READ"], "scope": "OWN_PLANT"},
        {"module": "TRAINING", "actions": ["READ"], "scope": "OWN_PLANT"},
    ],
    "SAFETY_OFFICER": [
        {"module": "OBSERVATION", "actions": ["CREATE", "READ", "VERIFY"], "scope": "OWN_PLANT"},
        {"module": "NEAR_MISS", "actions": ["CREATE", "READ", "APPROVE"], "scope": "OWN_PLANT"},
        {"module": "INCIDENT", "actions": ["CREATE", "READ"], "scope": "OWN_PLANT"},
        {"module": "PTW", "actions": ["READ", "APPROVE"], "scope": "OWN_PLANT"},
        {"module": "FLRA", "actions": ["READ"], "scope": "OWN_PLANT"},
        {"module": "INSPECTION", "actions": ["READ", "VERIFY"], "scope": "OWN_PLANT"},
        {"module": "PPE", "actions": ["READ", "CREATE", "UPDATE", "EXPORT", "ISSUE", "INSPECT", "VERIFY", "RETIRE_APPROVE"], "scope": "OWN_PLANT"},
    ],
    "HSE_MANAGER": [
        {"module": m, "actions": list(OPERATIONAL_ACTIONS), "scope": "OWN_PLANT"} for m in ["OBSERVATION", "NEAR_MISS", "INCIDENT", "PTW", "FLRA"]
    ] + [
        {"module": "TRAINING", "actions": ["CREATE", "READ", "UPDATE", "APPROVE", "EXPORT"], "scope": "OWN_PLANT"},
        {"module": "INSPECTION", "actions": list(OPERATIONAL_ACTIONS), "scope": "OWN_PLANT"},
        {"module": "MANHOURS", "actions": ["CREATE", "READ", "UPDATE", "EXPORT"], "scope": "OWN_PLANT"},
        {"module": "SKILL_MATRIX", "actions": ["READ", "COMPETENCY_CONFIGURE", "ROLE_DEF_CONFIGURE", "ASSESS", "SUSPEND", "CROSS_PERSON_VIEW", "VERSION_VIEW"], "scope": "OWN_PLANT"},
        {"module": "PPE", "actions": list(OPERATIONAL_ACTIONS) + ["ISSUE", "INSPECT", "CATALOG_MANAGE", "RETIRE_APPROVE", "RECALL_MANAGE"], "scope": "OWN_PLANT"},
        {"module": "EPC", "actions": ["CREATE", "READ", "UPDATE", "MOBILIZATION_APPROVE", "GATE_OVERRIDE", "INDUCTION_CONDUCT", "PREQUALIFY"], "scope": "OWN_PLANT"},
        {"module": "AUDIT_COMPLIANCE", "actions": ["CREATE", "READ", "UPDATE", "APPROVE", "EXECUTE", "VERIFY", "CLOSE", "EXPORT"], "scope": "OWN_PLANT"},
    ],
    "PLANT_HEAD": [
        {"module": m, "actions": list(OPERATIONAL_ACTIONS), "scope": "OWN_PLANT"} for m in ["OBSERVATION", "NEAR_MISS", "INCIDENT"]
    ] + [
        {"module": "PTW", "actions": ["READ", "APPROVE", "CLOSE", "EXPORT"], "scope": "OWN_PLANT"},
        {"module": "FLRA", "actions": ["READ"], "scope": "OWN_PLANT"},
        {"module": "TRAINING", "actions": ["READ", "EXPORT"], "scope": "OWN_PLANT"},
        {"module": "INSPECTION", "actions": ["READ", "EXPORT"], "scope": "OWN_PLANT"},
        {"module": "MANHOURS", "actions": ["READ", "APPROVE", "EXPORT"], "scope": "OWN_PLANT"},
        {"module": "SKILL_MATRIX", "actions": ["READ", "APPROVE_OVERRIDE", "RECERT_CYCLE", "EXPORT", "COMPETENCY_CONFIGURE", "ROLE_DEF_CONFIGURE", "ASSESS", "SUSPEND", "CROSS_PERSON_VIEW", "VERSION_VIEW"], "scope": "OWN_PLANT"},
        {"module": "PPE", "actions": ["READ", "EXPORT", "RETIRE_APPROVE", "RECALL_MANAGE"], "scope": "OWN_PLANT"},
        {"module": "AUDIT_COMPLIANCE", "actions": ["CREATE", "READ", "UPDATE", "EXECUTE", "APPROVE", "VERIFY", "CLOSE", "EXPORT"], "scope": "OWN_PLANT"},
    ],
    "CORPORATE_HSE": [
        {"module": m, "actions": list(OPERATIONAL_ACTIONS), "scope": "ALL_PLANTS"} for m in ["OBSERVATION", "NEAR_MISS", "INCIDENT"]
    ] + [
        {"module": "PTW", "actions": ["READ", "EXPORT"], "scope": "ALL_PLANTS"},
        {"module": "FLRA", "actions": ["READ", "EXPORT"], "scope": "ALL_PLANTS"},
        {"module": "TRAINING", "actions": list(OPERATIONAL_ACTIONS), "scope": "ALL_PLANTS"},
        {"module": "INSPECTION", "actions": ["READ", "EXPORT"], "scope": "ALL_PLANTS"},
        {"module": "MANHOURS", "actions": list(OPERATIONAL_ACTIONS), "scope": "ALL_PLANTS"},
        {"module": "CONFIGURATION", "actions": ["MASTERS"], "scope": "ALL_PLANTS"},
        {"module": "AUDIT", "actions": ["VIEW"], "scope": "ALL_PLANTS"},
        {"module": "AUDIT_COMPLIANCE", "actions": ["READ", "EXPORT"], "scope": "ALL_PLANTS"},
        {"module": "SKILL_MATRIX", "actions": ["READ", "APPROVE_OVERRIDE", "RECERT_CYCLE", "EXPORT", "COMPETENCY_CONFIGURE", "ROLE_DEF_CONFIGURE", "ASSESS", "SUSPEND", "CROSS_PERSON_VIEW", "VERSION_VIEW"], "scope": "ALL_PLANTS"},
        {"module": "PPE", "actions": ["READ", "EXPORT", "CATALOG_MANAGE", "RETIRE_APPROVE", "RECALL_MANAGE"], "scope": "ALL_PLANTS"},
        {"module": "EPC", "actions": ["READ", "CREATE", "UPDATE", "DELETE", "APPROVE", "EXECUTE", "VERIFY", "CLOSE", "EXPORT", "GATE_OVERRIDE", "MOBILIZATION_APPROVE", "INDUCTION_CONDUCT", "PREQUALIFY"], "scope": "ALL_PLANTS"},
    ],
    "TRAINER": [
        {"module": "SKILL_MATRIX", "actions": ["READ", "ASSESS"], "scope": "OWN_RECORDS"},
        {"module": "TRAINING", "actions": ["READ", "EXECUTE"], "scope": "OWN_PLANT"},
    ],
    "LD_MANAGER": [
        {"module": "SKILL_MATRIX", "actions": ["READ", "RECERT_CYCLE", "EXPORT", "COMPETENCY_CONFIGURE", "ROLE_DEF_CONFIGURE", "ASSESS", "CROSS_PERSON_VIEW", "VERSION_VIEW"], "scope": "ALL_PLANTS"},
        {"module": "TRAINING", "actions": ["CREATE", "READ", "UPDATE", "APPROVE", "EXECUTE", "VERIFY", "EXPORT"], "scope": "ALL_PLANTS"},
    ],
    "DEPARTMENT_HEAD": [
        {"module": "SKILL_MATRIX", "actions": ["READ", "ASSESS", "SUSPEND", "APPROVE_OVERRIDE"], "scope": "OWN_DEPARTMENT"},
        {"module": "OBSERVATION", "actions": ["CREATE", "READ", "APPROVE"], "scope": "OWN_DEPARTMENT"},
        {"module": "NEAR_MISS", "actions": ["CREATE", "READ", "APPROVE"], "scope": "OWN_DEPARTMENT"},
        {"module": "INCIDENT", "actions": ["CREATE", "READ"], "scope": "OWN_DEPARTMENT"},
        {"module": "PTW", "actions": ["READ"], "scope": "OWN_DEPARTMENT"},
    ],
    "MAINTENANCE_HEAD": [
        {"module": "INSPECTION", "actions": ["CREATE", "READ", "UPDATE", "APPROVE", "EXECUTE", "VERIFY", "CLOSE", "EXPORT"], "scope": "OWN_PLANT"},
        {"module": "PTW", "actions": ["READ"], "scope": "OWN_PLANT"},
        {"module": "PPE", "actions": ["READ"], "scope": "OWN_PLANT"},
    ],
    "ENVIRONMENT_MANAGER": [
        {"module": "OBSERVATION", "actions": ["READ"], "scope": "OWN_PLANT"},
        {"module": "INCIDENT", "actions": ["READ"], "scope": "OWN_PLANT"},
    ],
    "CONTRACTOR_COORDINATOR": [
        {"module": "PTW", "actions": ["READ"], "scope": "OWN_PLANT"},
        {"module": "TRAINING", "actions": ["READ"], "scope": "OWN_PLANT"},
        {"module": "EPC", "actions": ["CREATE", "READ", "UPDATE", "INDUCTION_CONDUCT"], "scope": "OWN_PLANT"},
    ],
    "OCCUPATIONAL_HEALTH_OFFICER": [
        {"module": "INCIDENT", "actions": ["READ"], "scope": "OWN_PLANT"},
        {"module": "TRAINING", "actions": ["READ"], "scope": "OWN_PLANT"},
    ],
    "EMERGENCY_RESPONSE_COORDINATOR": [
        {"module": "INCIDENT", "actions": ["READ"], "scope": "OWN_PLANT"},
    ],
    "INDUSTRIAL_HYGIENIST": [
        {"module": "INSPECTION", "actions": ["READ"], "scope": "OWN_PLANT"},
        {"module": "INCIDENT", "actions": ["READ"], "scope": "OWN_PLANT"},
    ],
    "ADMIN": [
        {"module": m, "actions": list(OPERATIONAL_ACTIONS), "scope": "ALL_PLANTS"} for m in OPERATIONAL_MODULES
    ] + [
        {"module": "CONFIGURATION", "actions": ["MASTERS", "WORKFLOWS", "USERS", "PERMISSIONS", "ROLES"], "scope": "ALL_PLANTS"},
        {"module": "AUDIT", "actions": ["VIEW"], "scope": "ALL_PLANTS"},
        {"module": "SKILL_MATRIX", "actions": ["COMPETENCY_CONFIGURE", "ROLE_DEF_CONFIGURE", "ASSESS", "SUSPEND", "APPROVE_OVERRIDE", "RECERT_CYCLE", "CROSS_PERSON_VIEW", "VERSION_VIEW"], "scope": "ALL_PLANTS"},
        {"module": "PPE", "actions": ["ISSUE", "INSPECT", "CATALOG_MANAGE", "RETIRE_APPROVE", "RECALL_MANAGE"], "scope": "ALL_PLANTS"},
    ],
    "SYSTEM_ADMIN": [
        {"module": m, "actions": list(OPERATIONAL_ACTIONS), "scope": "ALL_PLANTS"} for m in OPERATIONAL_MODULES
    ] + [
        {"module": "CONFIGURATION", "actions": ["MASTERS", "WORKFLOWS", "USERS", "PERMISSIONS", "ROLES"], "scope": "ALL_PLANTS"},
        {"module": "AUDIT", "actions": ["VIEW"], "scope": "ALL_PLANTS"},
        {"module": "SKILL_MATRIX", "actions": ["COMPETENCY_CONFIGURE", "ROLE_DEF_CONFIGURE", "ASSESS", "SUSPEND", "APPROVE_OVERRIDE", "RECERT_CYCLE", "CROSS_PERSON_VIEW", "VERSION_VIEW"], "scope": "ALL_PLANTS"},
        {"module": "PPE", "actions": ["ISSUE", "INSPECT", "CATALOG_MANAGE", "RETIRE_APPROVE", "RECALL_MANAGE"], "scope": "ALL_PLANTS"},
    ],
    # ─── Skill Matrix — 2 new roles (Phase 1 IMS), grants per spec §8.1 ───
    "HR_HEAD": [
        {"module": "SKILL_MATRIX", "actions": ["READ", "EXPORT", "COMPETENCY_CONFIGURE", "ROLE_DEF_CONFIGURE", "SUSPEND", "CROSS_PERSON_VIEW", "VERSION_VIEW"], "scope": "ALL_PLANTS"},
        {"module": "TRAINING", "actions": ["READ", "EXPORT"], "scope": "ALL_PLANTS"},
    ],
    "EXTERNAL_ASSESSOR": [
        {"module": "SKILL_MATRIX", "actions": ["READ", "ASSESS"], "scope": "OWN_RECORDS"},
    ],
    # ─── EPC — Engineering, Procurement & Construction roles ───────────────────
    "SITE_HSE_MANAGER": [
        {"module": "EPC", "actions": ["CREATE", "READ", "UPDATE", "MOBILIZATION_APPROVE", "GATE_OVERRIDE", "INDUCTION_CONDUCT", "PREQUALIFY"], "scope": "OWN_PLANT"},
    ],
    "GATE_GUARD": [
        {"module": "EPC", "actions": ["READ", "GATE_OVERRIDE"], "scope": "OWN_PLANT"},
    ],
}

DEMO_OVERLAYS = [
    {"emailContains": "rajesh", "roleCode": "PERMIT_ISSUER"},
    {"emailContains": "rajesh", "roleCode": "SUPERVISOR"},
    {"emailContains": "priya", "roleCode": "SAFETY_OFFICER"},
    {"emailContains": "ravi", "roleCode": "CORPORATE_HSE"},
    {"emailContains": "anjali", "roleCode": "HSE_MANAGER"},
    {"emailContains": "suresh", "roleCode": "PLANT_HEAD"},
]


async def upsert_role(db: AsyncSession, r: dict[str, Any]) -> Role:
    existing = (await db.execute(select(Role).where(Role.code == r["code"]))).scalar_one_or_none()
    if existing:
        existing.name = r["name"]
        existing.description = r["description"]
        existing.sortOrder = r["sortOrder"]
        existing.defaultLanding = r["defaultLanding"]
        existing.isActive = True
        return existing
    new = Role(
        code=r["code"], name=r["name"], description=r["description"],
        isSystem=r["isSystem"], sortOrder=r["sortOrder"], defaultLanding=r["defaultLanding"], isActive=True,
    )
    db.add(new)
    await db.flush()
    return new


async def upsert_permission(db: AsyncSession, p: dict[str, Any]) -> Permission:
    existing = (await db.execute(select(Permission).where(Permission.code == p["code"]))).scalar_one_or_none()
    if existing:
        existing.module = p["module"]
        existing.action = p["action"]
        existing.description = p["description"]
        return existing
    new = Permission(code=p["code"], module=p["module"], action=p["action"], description=p["description"])
    db.add(new)
    await db.flush()
    return new


async def main() -> None:
    print("🔐  RBAC seed: roles + permissions + grants + user-role assignments")
    async with AsyncSessionLocal() as db:
        # 1) Roles
        for r in ADDITIONAL_ROLES:
            await upsert_role(db, r)

        # 2) Permission catalogue
        catalogue: list[dict[str, Any]] = []
        for m in OPERATIONAL_MODULES:
            for a in OPERATIONAL_ACTIONS:
                catalogue.append({"code": f"{m}.{a}", "module": m, "action": a, "description": f"{a} on {m}"})
        catalogue.extend(EXTRA_PERMISSIONS)
        for p in catalogue:
            await upsert_permission(db, p)
        print(f"   permissions catalogued: {len(catalogue)}")

        # 3) Wipe + re-create role-permission grants
        await db.execute(delete(RolePermission))
        await db.flush()

        roles_by_code = {r.code: r for r in (await db.execute(select(Role))).scalars().all()}
        perms_by_code = {p.code: p for p in (await db.execute(select(Permission))).scalars().all()}
        grants_created = 0
        for role_code, grants in ROLE_GRANTS.items():
            role = roles_by_code.get(role_code)
            if role is None:
                continue
            for g in grants:
                for action in g["actions"]:
                    perm = perms_by_code.get(f"{g['module']}.{action}")
                    if perm is None:
                        continue
                    db.add(RolePermission(roleId=role.id, permissionId=perm.id, scope=g["scope"]))
                    grants_created += 1
        await db.flush()
        print(f"   role-permission grants created: {grants_created}")

        # 4) Backfill UserRole from User.role + apply demo overlays
        await db.execute(delete(UserRole))
        await db.flush()

        all_users = (await db.execute(select(User))).scalars().all()
        for u in all_users:
            rid = roles_by_code.get(u.role)
            if rid is None:
                continue
            db.add(
                UserRole(
                    userId=u.id,
                    roleId=rid.id,
                    scopeType="PLANT" if u.plantId else None,
                    scopeValue=u.plantId,
                )
            )
        await db.flush()

        for o in DEMO_OVERLAYS:
            user = next((u for u in all_users if o["emailContains"].lower() in u.email.lower()), None)
            role = roles_by_code.get(o["roleCode"])
            if user is None or role is None:
                continue
            existing = (
                await db.execute(
                    select(UserRole).where(UserRole.userId == user.id, UserRole.roleId == role.id)
                )
            ).scalar_one_or_none()
            if existing:
                continue
            db.add(
                UserRole(
                    userId=user.id, roleId=role.id,
                    scopeType="PLANT" if user.plantId else None,
                    scopeValue=user.plantId,
                )
            )

        # SYSTEM_ADMIN overlay on the bootstrap admin
        admin_user = next((u for u in all_users if "admin" in u.email.lower()), None)
        sysadmin_role = roles_by_code.get("SYSTEM_ADMIN")
        if admin_user and sysadmin_role:
            existing = (
                await db.execute(
                    select(UserRole).where(UserRole.userId == admin_user.id, UserRole.roleId == sysadmin_role.id)
                )
            ).scalar_one_or_none()
            if not existing:
                db.add(UserRole(userId=admin_user.id, roleId=sysadmin_role.id))

        await db.commit()

        ur_count = (await db.execute(select(__import__("sqlalchemy", fromlist=["func"]).func.count()).select_from(UserRole))).scalar_one()
        print(f"   user-role assignments: {ur_count}")
    print("✅  RBAC seed complete.")


if __name__ == "__main__":
    asyncio.run(main())
