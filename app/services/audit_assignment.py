"""Who may be assigned to which audit role — driven entirely by RBAC scope.

The schedule wizard used to offer every user at the plant for every slot, so an
Insurance Manager could be made Lead Auditor and a Field Technician could be
made the reviewer. They would then hit a 403 the moment they tried to act,
because the audit engine gates each workflow action on a permission they never
held. The picker promised something the guard refuses.

This module closes that gap by deriving each picker from the same permission
the assignee will need:

    Lead auditor / Co-auditor  -> AUDIT_COMPLIANCE.EXECUTE  (conducts the audit)
    Plant manager (reviewer)   -> AUDIT_COMPLIANCE.APPROVE  (PM_ACCEPT / PM_SEND_BACK)
    Auditee                    -> AUDIT_COMPLIANCE.UPDATE   (AUDITEE_RESPOND)

Those are exactly the codes in the router's `_TRANSITION_PERM` table, so
"appears in the dropdown" and "is allowed to do the job" cannot drift apart.
`assignable_users()` builds the picker and `assert_assignable()` re-checks on
write — same predicate both times, so filtering the UI is a convenience, not
the control.

Everything is admin-controlled through Configuration -> Roles (which permission
each role carries, at which scope) and Configuration -> Users (which roles a
person holds, and at which plants). Grant a role the permission and its holders
appear; revoke it and they vanish on the next load. No list is hard-coded here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Permission, Role, RolePermission, User, UserRole

# Assignment slot -> the permission its occupant must hold. Keep in step with
# `_TRANSITION_PERM` in routers/audit_compliance.py.
SLOT_PERMISSION: dict[str, str] = {
    "leadAuditor": "AUDIT_COMPLIANCE.EXECUTE",
    "coAuditor": "AUDIT_COMPLIANCE.EXECUTE",
    "plantManager": "AUDIT_COMPLIANCE.APPROVE",
    "auditee": "AUDIT_COMPLIANCE.UPDATE",
}

# Human labels for the error surfaced when a write names an ineligible person.
SLOT_LABEL: dict[str, str] = {
    "leadAuditor": "lead auditor",
    "coAuditor": "co-auditor",
    "plantManager": "plant manager (reviewer)",
    "auditee": "auditee",
}

_PLANT_SCOPES = {"OWN_PLANT", "OWN_DEPARTMENT", "OWN_RECORDS"}


def _scope_covers_plant(scopes: set[str], plant_id: str, user_plants: set[str]) -> bool:
    """Does any grant let this person act at `plant_id`?

    ALL_PLANTS is unconditional. The narrower scopes need the plant in the
    user's own plant set (primary plantId + PLANT-scoped role assignments).
    OWN_RECORDS counts here: an auditee's grant only resolves against a record
    that names them, and being named IS what the assignment does — the record
    guard then applies for real on every later action.
    """
    if "ALL_PLANTS" in scopes:
        return True
    return bool(scopes & _PLANT_SCOPES) and plant_id in user_plants


async def _permission_index(
    db: AsyncSession, codes: Iterable[str]
) -> tuple[dict[str, dict[str, set[str]]], dict[str, set[str]]]:
    """One pass over the RBAC graph for the permission codes we care about.

    Returns (user_id -> permission_code -> {scopes}, user_id -> {plant ids}).
    Deliberately one query rather than calling can() per user per slot: a plant
    with 60 users would otherwise be 240 permission-cache loads per open of the
    schedule modal.
    """
    now = datetime.now(timezone.utc)
    wanted = set(codes)

    stmt = (
        select(UserRole.userId, Permission.code, RolePermission.scope,
               UserRole.scopeType, UserRole.scopeValue)
        .join(Role, Role.id == UserRole.roleId)
        .join(RolePermission, RolePermission.roleId == Role.id)
        .join(Permission, Permission.id == RolePermission.permissionId)
        .where(Role.isActive.is_(True))
        .where((UserRole.validTo.is_(None)) | (UserRole.validTo > now))
    )
    rows = (await db.execute(stmt)).all()

    perms: dict[str, dict[str, set[str]]] = {}
    plants: dict[str, set[str]] = {}
    for user_id, code, scope, scope_type, scope_value in rows:
        # A PLANT-scoped role assignment widens the user's plant set whatever
        # permission the row carries, so collect it before filtering by code.
        if scope_type == "PLANT" and scope_value:
            plants.setdefault(user_id, set()).add(scope_value)
        if code in wanted:
            perms.setdefault(user_id, {}).setdefault(code, set()).add(scope)
    return perms, plants


async def assignable_users(db: AsyncSession, *, plant_id: str) -> dict[str, Any]:
    """The four pickers for one plant, each a list of {id, name, role, department}.

    A person appears in a slot iff they hold that slot's permission at a scope
    covering this plant. Candidates are NOT limited to users whose home plant is
    `plant_id`: a corporate auditor holding ALL_PLANTS is legitimately
    assignable here, and the old `User.plantId == plantId` filter hid them.
    """
    perms, role_plants = await _permission_index(db, SLOT_PERMISSION.values())

    candidate_ids = set(perms)
    users = (
        await db.execute(select(User).where(User.id.in_(candidate_ids)).order_by(User.name))
    ).scalars().all() if candidate_ids else []

    slots: dict[str, list[dict[str, str]]] = {slot: [] for slot in SLOT_PERMISSION}
    for u in users:
        user_plants = set(role_plants.get(u.id, set()))
        if u.plantId:
            user_plants.add(u.plantId)
        dto = {"id": u.id, "name": u.name, "role": u.role, "department": u.department or ""}
        for slot, code in SLOT_PERMISSION.items():
            if _scope_covers_plant(perms[u.id].get(code, set()), plant_id, user_plants):
                slots[slot].append(dto)

    return {
        "plantId": plant_id,
        "permissions": dict(SLOT_PERMISSION),
        "assignable": slots,
    }


async def audit_team(
    db: AsyncSession, audit, *, discipline_names: dict[str, str] | None = None
) -> dict[str, Any]:
    """The full cast of one audit, for the detail screen's team panel.

    Every seat resolves to {userId, name, role, permission, authorised} and, for
    the two per-discipline seats, the disciplines that person covers. `role` is
    the person's job title; `authorised` is whether they hold this seat's
    permission RIGHT NOW — the two are not the same, and the gap is the point.
    An audit outlives a permission change: someone named lead auditor in March
    can lose EXECUTE in June, and until now nothing on the screen said so. They
    stay listed (removing them would rewrite history) but render unauthorised,
    which is exactly the state that explains why their Start Audit button is
    missing.

    A co-auditor with no disciplines listed conducts none by allocation — the
    lead covers every discipline not explicitly assigned to someone else.
    """
    names = discipline_names or {}

    lead_id = audit.leadAuditorUserId
    pm_id = audit.plantManagerUserId
    co = [
        {"userId": c.get("userId") if isinstance(c, dict) else c,
         "disciplineIds": (c.get("disciplineIds") or []) if isinstance(c, dict) else []}
        for c in (audit.coAuditors or [])
    ]
    au = [
        {"userId": a.get("userId") if isinstance(a, dict) else a,
         "disciplineIds": (a.get("responsibleCategories") or []) if isinstance(a, dict) else []}
        for a in (audit.auditees or [])
    ]

    ids = {i for i in [lead_id, pm_id, *(c["userId"] for c in co), *(a["userId"] for a in au)] if i}
    if not ids:
        return {"leadAuditor": None, "plantManager": None, "coAuditors": [], "auditees": [],
                "permissions": dict(SLOT_PERMISSION), "unauthorisedCount": 0}

    users = {
        u.id: u for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()
    }
    perms, role_plants = await _permission_index(db, SLOT_PERMISSION.values())

    def member(uid: str | None, slot: str, discipline_ids: list[str] | None = None):
        if not uid:
            return None
        u = users.get(uid)
        code = SLOT_PERMISSION[slot]
        user_plants = set(role_plants.get(uid, set()))
        if u is not None and u.plantId:
            user_plants.add(u.plantId)
        authorised = u is not None and _scope_covers_plant(
            perms.get(uid, {}).get(code, set()), audit.plantId, user_plants
        )
        out: dict[str, Any] = {
            "userId": uid,
            # A deleted user still has to render as something in an audit that
            # names them, rather than collapsing the row.
            "name": u.name if u else "Unknown user",
            "role": u.role if u else None,
            "department": (u.department or "") if u else "",
            "permission": code,
            "authorised": authorised,
        }
        if discipline_ids is not None:
            out["disciplines"] = [
                {"id": d, "name": names.get(d, d)} for d in discipline_ids
            ]
        return out

    lead = member(lead_id, "leadAuditor")
    pm = member(pm_id, "plantManager")
    co_out = [m for m in (member(c["userId"], "coAuditor", c["disciplineIds"]) for c in co) if m]
    au_out = [m for m in (member(a["userId"], "auditee", a["disciplineIds"]) for a in au) if m]

    everyone = [m for m in [lead, pm, *co_out, *au_out] if m]
    return {
        "leadAuditor": lead,
        "plantManager": pm,
        "coAuditors": co_out,
        "auditees": au_out,
        "permissions": dict(SLOT_PERMISSION),
        "memberCount": len(everyone),
        "unauthorisedCount": sum(1 for m in everyone if not m["authorised"]),
    }


async def assert_assignable(
    db: AsyncSession, *, plant_id: str, assignments: dict[str, list[str]]
) -> None:
    """Server-side enforcement for a write. `assignments` is slot -> user ids.

    Raises ValueError naming the offenders. The router turns that into a 400 —
    filtering the picker is a courtesy, this is the actual gate, so a crafted
    request cannot seat an unauthorised person in an audit role.
    """
    wanted_ids = {uid for ids in assignments.values() for uid in ids if uid}
    if not wanted_ids:
        return

    perms, role_plants = await _permission_index(db, SLOT_PERMISSION.values())
    users = {
        u.id: u
        for u in (await db.execute(select(User).where(User.id.in_(wanted_ids)))).scalars().all()
    }

    problems: list[str] = []
    for slot, ids in assignments.items():
        code = SLOT_PERMISSION.get(slot)
        if code is None:
            continue
        for uid in ids:
            if not uid:
                continue
            u = users.get(uid)
            if u is None:
                problems.append(f"unknown user {uid} as {SLOT_LABEL[slot]}")
                continue
            user_plants = set(role_plants.get(uid, set()))
            if u.plantId:
                user_plants.add(u.plantId)
            if not _scope_covers_plant(perms.get(uid, {}).get(code, set()), plant_id, user_plants):
                problems.append(f"{u.name} cannot be {SLOT_LABEL[slot]} — missing {code} at this plant")

    if problems:
        raise ValueError("; ".join(problems))
