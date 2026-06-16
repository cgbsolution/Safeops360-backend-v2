"""Users router. Replaces /api/users — used by the UserPicker on the frontend.

Response shape matches what `src/components/ui/user-picker.tsx` expects:
  { "users": [{ id, name, email, designation, department, role,
                plant: { id, name, code } | null }] }
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.user import Permission, Role, RolePermission, User, UserRole

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("")
async def search_users(
    q: str | None = None,
    plantId: str | None = None,
    department: str | None = None,
    departmentId: str | None = None,
    role: list[str] = Query(default_factory=list),
    permission: str | None = Query(default=None, description="Filter to users holding this permission code (any active role × grant)."),
    excludeSelf: bool = False,
    take: int = Query(default=20, le=100),
    skip: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Search users with optional filters. Drives the UserPicker dropdown."""
    stmt = select(User).options(selectinload(User.plant))
    if plantId:
        stmt = stmt.where(User.plantId == plantId)
    dep_filter = departmentId or department
    if dep_filter:
        stmt = stmt.where(User.department == dep_filter)
    if role:
        # Each `role` query param can itself be comma-separated; flatten + upper.
        codes: list[str] = []
        for entry in role:
            for piece in str(entry).split(","):
                p = piece.strip().upper()
                if p:
                    codes.append(p)
        if codes:
            # Match against the canonical UserRole assignments (the legacy
            # denormalised `User.role` only carries the user's *primary*
            # role and misses overlays like PERMIT_ISSUER assigned via the
            # UserRole table — those overlays are how operational roles are
            # actually granted in seed_rbac.py). Falling back to User.role
            # in addition keeps back-compat with any callers that pass a
            # role code that's only present in the legacy column.
            role_user_ids = (
                select(UserRole.userId)
                .join(Role, Role.id == UserRole.roleId)
                .where(Role.code.in_(codes))
            )
            stmt = stmt.where(or_(User.id.in_(role_user_ids), User.role.in_(codes)))
    if permission:
        # Narrow to users who hold the named permission via any of their
        # active roles. Used by pickers like "pick an inspector" so the
        # form can't offer someone the workflow will later reject.
        eligible_user_ids = (
            select(UserRole.userId)
            .join(RolePermission, RolePermission.roleId == UserRole.roleId)
            .join(Permission, Permission.id == RolePermission.permissionId)
            .where(Permission.code == permission)
        )
        stmt = stmt.where(User.id.in_(eligible_user_ids))
    if excludeSelf:
        stmt = stmt.where(User.id != user.id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                User.name.ilike(like),
                User.email.ilike(like),
                User.designation.ilike(like),
            )
        )
    stmt = stmt.order_by(User.name).offset(skip).limit(take)
    rows = (await db.execute(stmt)).scalars().all()

    return {
        "users": [
            {
                "id": u.id,
                "name": u.name,
                "email": u.email,
                "role": u.role,
                "designation": u.designation,
                "department": u.department,
                "plant": (
                    {"id": u.plant.id, "name": u.plant.name, "code": u.plant.code}
                    if u.plant
                    else None
                ),
            }
            for u in rows
        ]
    }
