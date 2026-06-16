"""Central permission service. Direct port of `src/lib/auth/permissions.ts`.

User → UserRole → Role → RolePermission → Permission
Each grant carries a scope (ALL_PLANTS / OWN_PLANT / OWN_DEPARTMENT / OWN_RECORDS).
Grants are additive — if any grant satisfies the scope, the action is allowed.

The cache is a simple in-process map keyed by user id with a 30-second TTL.
For multi-worker deployments swap this for Redis without changing the public API.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import Role, RolePermission, User, UserRole

PermissionScope = Literal["ALL_PLANTS", "OWN_PLANT", "OWN_DEPARTMENT", "OWN_RECORDS"]


@dataclass
class PermissionContext:
    module: str | None = None
    record_id: str | None = None
    plant_id: str | None = None
    department_id: str | None = None
    record: dict[str, Any] | None = None  # the underlying row when known


@dataclass
class CanResult:
    allowed: bool
    reason: str | None = None
    matched_scope: PermissionScope | None = None


@dataclass
class _CachedRow:
    permission_code: str
    scope: PermissionScope
    conditions: Any
    role_id: str


@dataclass
class _UserSnapshot:
    """Combined permissions + profile, cached together so `can()` is one
    cache hit on the hot path. Single DB-fetch on cache miss instead of two."""
    rows: list[_CachedRow]
    role_codes: list[str]  # all active role codes for the user
    plant_id: str | None
    plant_ids: set[str]  # all plants accessible via primary plantId + PLANT-scoped UserRoles
    department: str | None
    expires_at: float = field(default_factory=lambda: time.time() + 300.0)


# 5-minute cache. Permission edits are rare; admin UI calls
# `invalidate_user_permissions(user_id)` after changing grants for
# immediate effect. Five minutes vs 30 seconds means an active user's
# permission map is in memory for nearly every page render.
_CACHE: dict[str, _UserSnapshot] = {}
_TTL_SECONDS = 300.0


def invalidate_user_permissions(user_id: str | None = None) -> None:
    if user_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(user_id, None)


async def _load_user_snapshot(db: AsyncSession, user_id: str) -> _UserSnapshot:
    """One round-trip to load permissions + profile. Replaces the old pair
    of queries (`_load_user_permissions` + `_load_user_profile`) which were
    each ~30-50ms RTT against the Singapore Supabase pooler."""
    cached = _CACHE.get(user_id)
    if cached and cached.expires_at > time.time():
        return cached

    now = datetime.now(timezone.utc)
    user = await db.get(User, user_id)
    if user is None:
        empty = _UserSnapshot(rows=[], role_codes=[], plant_id=None, department=None)
        _CACHE[user_id] = empty
        return empty

    stmt = (
        select(UserRole)
        .where(UserRole.userId == user_id)
        .where((UserRole.validTo.is_(None)) | (UserRole.validTo > now))
        .options(selectinload(UserRole.role).selectinload(Role.permissions).selectinload(RolePermission.permission))
    )
    user_roles = (await db.execute(stmt)).scalars().all()

    rows: list[_CachedRow] = []
    role_codes: list[str] = []
    # Collect all plant IDs accessible to this user: primary plantId + all
    # PLANT-scoped UserRole entries (mirrors getAccessiblePlantIds() in TS).
    plant_ids: set[str] = set()
    if user.plantId:
        plant_ids.add(user.plantId)

    for ur in user_roles:
        if not ur.role.isActive:
            continue
        role_codes.append(ur.role.code)
        # Populate the role.id → role.code lookup we use elsewhere
        _ROLE_CODE_CACHE[ur.roleId] = ur.role.code
        for rp in ur.role.permissions:
            rows.append(
                _CachedRow(
                    permission_code=rp.permission.code,
                    scope=rp.scope,  # type: ignore[arg-type]
                    conditions=rp.conditions,
                    role_id=ur.roleId,
                )
            )
        # Collect cross-plant access from PLANT-scoped UserRole entries
        if ur.scopeType == "PLANT" and ur.scopeValue:
            plant_ids.add(ur.scopeValue)

    snap = _UserSnapshot(
        rows=rows,
        role_codes=role_codes,
        plant_id=user.plantId,
        plant_ids=plant_ids,
        department=user.department,
        expires_at=time.time() + _TTL_SECONDS,
    )
    _CACHE[user_id] = snap
    return snap


async def _load_user_permissions(db: AsyncSession, user_id: str) -> list[_CachedRow]:
    snap = await _load_user_snapshot(db, user_id)
    return snap.rows


# Lightweight stand-in for callers that previously asked for the User row.
class _UserProfileLite:
    def __init__(self, plant_id: str | None, plant_ids: set[str], department: str | None) -> None:
        self.plantId = plant_id
        self.plantIds = plant_ids
        self.department = department


async def _load_user_profile(db: AsyncSession, user_id: str) -> _UserProfileLite:
    snap = await _load_user_snapshot(db, user_id)
    return _UserProfileLite(snap.plant_id, snap.plant_ids, snap.department)


# Owner-style fields we recognise for OWN_RECORDS scope checks. Mirror of the
# TS list — keep them in sync when adding new modules.
_OWNER_FIELDS = (
    "originatorId",
    "ownerId",
    "reporterId",
    "observerId",
    "leaderId",
    "actionOwnerId",
    "responsiblePersonId",
    "issuerId",
    "receiverId",
    "inspectorId",
    "trainerId",
    "employeeId",
    "uploadedById",
    "createdById",
)


def _matches_own_records(user_id: str, record: dict[str, Any]) -> bool:
    if any(record.get(field) == user_id for field in _OWNER_FIELDS):
        return True
    for crew_field in ("workCrew", "crewSignatures", "teamMembers"):
        crew = record.get(crew_field)
        if isinstance(crew, list) and any(getattr(c, "userId", None) == user_id or (isinstance(c, dict) and c.get("userId") == user_id) for c in crew):
            return True
    return False


async def can(
    db: AsyncSession,
    user_id: str,
    permission_code: str,
    context: PermissionContext | None = None,
) -> CanResult:
    """The single function every layer calls."""
    ctx = context or PermissionContext()
    rows = await _load_user_permissions(db, user_id)
    matches = [r for r in rows if r.permission_code == permission_code]
    if not matches:
        return CanResult(allowed=False, reason=f"Missing permission '{permission_code}'")

    if any(m.scope == "ALL_PLANTS" for m in matches):
        return CanResult(allowed=True, matched_scope="ALL_PLANTS")

    profile = await _load_user_profile(db, user_id)
    if profile is None:
        return CanResult(allowed=False, reason="User profile lookup failed")

    for m in matches:
        if m.scope == "OWN_PLANT":
            if ctx.plant_id and profile.plantIds and ctx.plant_id in profile.plantIds:
                return CanResult(allowed=True, matched_scope="OWN_PLANT")
            if not ctx.plant_id and not ctx.record_id:
                return CanResult(allowed=True, matched_scope="OWN_PLANT")

        if m.scope == "OWN_DEPARTMENT":
            if ctx.department_id and profile.department and ctx.department_id == profile.department:
                return CanResult(allowed=True, matched_scope="OWN_DEPARTMENT")
            if not ctx.department_id and not ctx.record_id:
                return CanResult(allowed=True, matched_scope="OWN_DEPARTMENT")

        if m.scope == "OWN_RECORDS":
            if ctx.record and _matches_own_records(user_id, ctx.record):
                return CanResult(allowed=True, matched_scope="OWN_RECORDS")
            if not ctx.record_id:
                return CanResult(allowed=True, matched_scope="OWN_RECORDS")

    return CanResult(
        allowed=False,
        reason=f"Permission '{permission_code}' present but scope does not include this record",
    )


# Convenience wrappers — mirror the TS helpers
async def can_create(db: AsyncSession, user_id: str, module: str, plant_id: str | None = None) -> CanResult:
    return await can(db, user_id, f"{module}.CREATE", PermissionContext(module=module, plant_id=plant_id))


async def can_read(db: AsyncSession, user_id: str, module: str, record_id: str, record: dict | None = None) -> CanResult:
    return await can(db, user_id, f"{module}.READ", PermissionContext(module=module, record_id=record_id, record=record))


async def can_update(db: AsyncSession, user_id: str, module: str, record_id: str, record: dict | None = None) -> CanResult:
    return await can(db, user_id, f"{module}.UPDATE", PermissionContext(module=module, record_id=record_id, record=record))


async def can_approve(db: AsyncSession, user_id: str, module: str, record_id: str, record: dict | None = None) -> CanResult:
    return await can(db, user_id, f"{module}.APPROVE", PermissionContext(module=module, record_id=record_id, record=record))


async def can_execute(db: AsyncSession, user_id: str, module: str, record_id: str, record: dict | None = None) -> CanResult:
    return await can(db, user_id, f"{module}.EXECUTE", PermissionContext(module=module, record_id=record_id, record=record))


async def can_verify(db: AsyncSession, user_id: str, module: str, record_id: str, record: dict | None = None) -> CanResult:
    return await can(db, user_id, f"{module}.VERIFY", PermissionContext(module=module, record_id=record_id, record=record))


async def can_close(db: AsyncSession, user_id: str, module: str, record_id: str, record: dict | None = None) -> CanResult:
    return await can(db, user_id, f"{module}.CLOSE", PermissionContext(module=module, record_id=record_id, record=record))


async def get_permissions(db: AsyncSession, user_id: str) -> dict[str, bool]:
    """Batch lookup used by the UI to gate buttons in one round-trip."""
    rows = await _load_user_permissions(db, user_id)
    return {r.permission_code: True for r in rows}


async def get_module_scopes(db: AsyncSession, user_id: str, module_prefix: str) -> set[str]:
    """Return the unique scopes the user holds for permissions starting with
    `module_prefix` (e.g. "NEAR_MISS." or "OBSERVATION."). Used by masters
    endpoints to decide whether a dropdown should show plant-wide options
    or restrict to the caller's own department."""
    rows = await _load_user_permissions(db, user_id)
    return {r.scope for r in rows if r.permission_code.startswith(module_prefix)}


async def get_accessible_plants(db: AsyncSession, user_id: str) -> list[str] | None:
    """Returns the plant IDs the user can act in. None == unrestricted.
    Includes the user's primary plantId plus all PLANT-scoped UserRole entries
    (mirrors getAccessiblePlantIds() in the Next.js frontend)."""
    rows = await _load_user_permissions(db, user_id)
    if any(r.scope == "ALL_PLANTS" for r in rows):
        return None
    profile = await _load_user_profile(db, user_id)
    if profile is None:
        return []
    return list(profile.plantIds) if profile.plantIds else []


async def get_user_role_codes(db: AsyncSession, user_id: str) -> list[str]:
    """Role codes the user holds. Pulled from the cached snapshot — no DB
    query on the hot path. Includes roles even when the role has no
    permissions assigned (relevant for workflow engine role-match checks)."""
    snap = await _load_user_snapshot(db, user_id)
    return list(snap.role_codes)


_ROLE_CODE_CACHE: dict[str, str] = {}


def _role_code_for(role_id: str) -> str | None:
    """role.id → role.code lookup, populated by `_load_user_snapshot`.
    Avoids a separate Role table query for `get_user_role_codes()` —
    relevant because the workflow engine triple-check calls it on every
    transition. The cache is rebuilt naturally as users log in."""
    return _ROLE_CODE_CACHE.get(role_id)


async def has_role(db: AsyncSession, user_id: str, role_code: str) -> bool:
    return role_code in await get_user_role_codes(db, user_id)
