"""Additive RBAC + CAPA-source seed for the Safety Culture Management module.

Adds the SAFETY_CULTURE.* permission catalogue + role grants WITHOUT wiping the
existing RolePermission matrix, and registers a SAFETY_CULTURE CapaSourceType so
a behaviour observation can spawn a follow-through CAPA via the canonical
capa_spawn path (source_code="SAFETY_CULTURE"). Idempotent.

Run from the backend root:
    .venv/Scripts/python.exe scripts/seed_safety_culture_rbac.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.capa import CapaSlaProfile, CapaSourceCategory, CapaSourceType
from app.models.user import Permission, Role, RolePermission

# Action vocabulary for the module.
ACTIONS = [
    ("READ", "Read culture dashboards, scores, walks, surveys, recognition"),
    ("CLOSURE", "Link an observation to CAPA/Action and verify re-observation closure"),
    ("WALK_SCHEDULE", "Schedule leadership safety walks"),
    ("WALK_LOG", "Log completion of a leadership walk"),
    ("SURVEY_ADMIN", "Create / edit perception survey templates"),
    ("SURVEY_RESPOND", "Submit an anonymous perception survey response"),
    ("RECALC", "Trigger culture score recalculation + recognition awards"),
    ("INTEGRITY_REVIEW", "Review a BBS observation-integrity flag (dismiss/uphold) — gates Recognition"),
    ("ADMIN", "Wire culture scores into the ERM KRI engine; module administration"),
]

PERMISSIONS = [
    {"code": f"SAFETY_CULTURE.{a}", "module": "SAFETY_CULTURE", "action": a, "description": d}
    for a, d in ACTIONS
]

ALL = [a for a, _ in ACTIONS]

# role_code → (actions, scope).
GRANTS: dict[str, tuple[list[str], str]] = {
    # Everyone can respond to an (anonymous) survey and see their own recognition.
    "WORKER": (["SURVEY_RESPOND"], "OWN_RECORDS"),
    "CONTRACTOR_WORKMAN": (["SURVEY_RESPOND"], "OWN_RECORDS"),
    # Frontline leaders log their own walks + respond to surveys; read own site.
    "SUPERVISOR": (["READ", "WALK_LOG", "SURVEY_RESPOND", "CLOSURE"], "OWN_DEPARTMENT"),
    "SAFETY_OFFICER": (["READ", "WALK_SCHEDULE", "WALK_LOG", "CLOSURE", "SURVEY_RESPOND"], "OWN_PLANT"),
    # Site HSE owns the culture programme for the plant.
    "HSE_MANAGER": (ALL, "OWN_PLANT"),
    "PLANT_HEAD": (["READ", "WALK_SCHEDULE", "WALK_LOG", "SURVEY_RESPOND", "RECALC", "INTEGRITY_REVIEW"], "OWN_PLANT"),
    "MAINTENANCE_HEAD": (["READ", "WALK_LOG", "SURVEY_RESPOND"], "OWN_PLANT"),
    "DEPARTMENT_HEAD": (["READ", "WALK_LOG", "SURVEY_RESPOND"], "OWN_DEPARTMENT"),
    # Enterprise EHS / risk leadership see the rollup + wire KRIs.
    "CORPORATE_HSE": (ALL, "ALL_PLANTS"),
    "ADMIN": (ALL, "ALL_PLANTS"),
    "SYSTEM_ADMIN": (ALL, "ALL_PLANTS"),
}

# ERM leadership roles (may be absent in some tenants) — read + KRI wiring.
OPTIONAL_GRANTS: dict[str, tuple[list[str], str]] = {
    "CRO": (["READ", "ADMIN"], "ALL_PLANTS"),
    "RISK_CHAMPION": (["READ"], "ALL_PLANTS"),
}


def _seed_capa_source(s: Session) -> None:
    cat = s.execute(select(CapaSourceCategory).where(CapaSourceCategory.code == "CULTURE")).scalar_one_or_none()
    if cat is None:
        taken = {c.prefix for c in s.execute(select(CapaSourceCategory)).scalars().all()}
        prefix = "SC" if "SC" not in taken else "CU"
        cat = CapaSourceCategory(code="CULTURE", name="Safety Culture", prefix=prefix, sortOrder=30, isActive=True)
        s.add(cat)
        s.flush()
        print(f"   + created CapaSourceCategory CULTURE (prefix {prefix})")

    st = s.execute(select(CapaSourceType).where(CapaSourceType.code == "SAFETY_CULTURE")).scalar_one_or_none()
    if st is None:
        s.add(CapaSourceType(
            code="SAFETY_CULTURE", name="Safety Culture Follow-through", categoryId=cat.id,
            description="Behaviour-observation closure-loop or culture-programme corrective action.",
            parentModuleLive=True, parentModuleName="SAFETY_CULTURE", sortOrder=30, isActive=True,
        ))
        print("   + created CapaSourceType SAFETY_CULTURE")
    else:
        st.isActive = True
        st.parentModuleLive = True
        st.parentModuleName = "SAFETY_CULTURE"

    sla = s.execute(select(CapaSlaProfile).where(CapaSlaProfile.code == "SAFETY_CULTURE_DEF")).scalar_one_or_none()
    if sla is None:
        s.add(CapaSlaProfile(
            code="SAFETY_CULTURE_DEF", sourceTypeCode="SAFETY_CULTURE", severity=None,
            initialResponseHours=48, rcaDueDays=7, actionsPlannedDueDays=14,
            closureTargetDays=30, recurrenceCheckDays=90, isActive=True,
        ))
        print("   + created CapaSlaProfile SAFETY_CULTURE_DEF")


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        perms_added = 0
        for p in PERMISSIONS:
            if s.execute(select(Permission).where(Permission.code == p["code"])).scalar_one_or_none() is None:
                s.add(Permission(**p))
                perms_added += 1
        s.flush()

        roles_by_code = {r.code: r for r in s.execute(select(Role)).scalars().all()}
        perms_by_code = {p.code: p for p in s.execute(select(Permission)).scalars().all()}

        grants_added = 0
        skipped_roles: list[str] = []
        all_grants = {**GRANTS, **{k: v for k, v in OPTIONAL_GRANTS.items() if k in roles_by_code}}
        for role_code, (actions, scope) in all_grants.items():
            role = roles_by_code.get(role_code)
            if role is None:
                skipped_roles.append(role_code)
                continue
            for action in actions:
                perm = perms_by_code.get(f"SAFETY_CULTURE.{action}")
                if perm is None:
                    continue
                exists = s.execute(
                    select(RolePermission).where(
                        RolePermission.roleId == role.id, RolePermission.permissionId == perm.id
                    )
                ).scalar_one_or_none()
                if exists is not None:
                    if exists.scope != scope:
                        exists.scope = scope
                    continue
                s.add(RolePermission(roleId=role.id, permissionId=perm.id, scope=scope))
                grants_added += 1

        _seed_capa_source(s)

        s.commit()
        print(f"Permissions added : {perms_added} (SAFETY_CULTURE catalogue total {len(PERMISSIONS)})")
        print(f"Grants added      : {grants_added}")
        if skipped_roles:
            print(f"Roles not found   : {skipped_roles}")
        print("Done. Safety Culture RBAC + CAPA source applied additively.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
