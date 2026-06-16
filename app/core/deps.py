from collections.abc import AsyncGenerator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import InvalidTokenError, safe_decode
from app.models.user import User
from app.services.permissions import PermissionContext, can

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    try:
        payload = safe_decode(creds.credentials)
    except InvalidTokenError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {e}") from e
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token has no subject")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return user


def require_permission(permission_code: str):
    """Dependency factory. Use as `Depends(require_permission('PTW.CREATE'))`."""

    async def _checker(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        result = await can(db, user.id, permission_code, PermissionContext())
        if not result.allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
        return user

    return _checker


# Convenience: dependency that builds and yields a context-aware permission check
# at request time, given runtime data the dependency can't see (recordId, plantId).
# Most route handlers call `can()` directly inside the handler instead.
async def require_permission_with_context(
    permission_code: str,
    user: User,
    db: AsyncSession,
    *,
    record_id: str | None = None,
    plant_id: str | None = None,
    record: dict | None = None,
) -> None:
    result = await can(
        db,
        user.id,
        permission_code,
        PermissionContext(record_id=record_id, plant_id=plant_id, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")


# Re-export for convenience
__all__ = [
    "get_current_user",
    "require_permission",
    "require_permission_with_context",
    "get_db",
    "AsyncGenerator",
]
