"""Auth router. Mounts at /api/auth.

Endpoints:
  POST /api/auth/login              — email + password → JWT (+ refresh stub)
  POST /api/auth/refresh            — rotate access token from a valid bearer
  GET  /api/auth/me                 — current user
  GET  /api/auth/permissions        — permission-code → bool map
  POST /api/auth/forgot-password    — issues an OTP (dev: surfaced in response)
  POST /api/auth/verify-otp         — accepts OTP, returns reset token
  POST /api/auth/reset-password     — applies new password using reset token
  POST /api/auth/devices            — register push device (stub: no-op)
  DELETE /api/auth/devices/{id}     — unregister push device (stub: no-op)

NextAuth on the web frontend keeps the session cookie/JWT. Its credentials
provider POSTs /login; the access_token returned is what NextAuth signs into
the session and what the frontend forwards as `Authorization: Bearer …` to
every other backend endpoint.

The mobile app (Expo) calls /login + /refresh + /forgot-password + /verify-otp
+ /reset-password + /devices. Refresh / forgot-password / OTP / device flows
are intentionally lightweight stubs — see BACKEND_TODO.md in the mobile
project for the production contract.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.security import (
    InvalidTokenError,
    create_access_token,
    hash_password,
    safe_decode,
    verify_password,
)
from app.models.user import User
from app.schemas.auth import (
    DeviceRegisterRequest,
    DeviceRegisterResponse,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    LoginResponse,
    PermissionsResponse,
    RefreshRequest,
    RefreshResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
    UserOut,
    VerifyOtpRequest,
    VerifyOtpResponse,
)
from app.services.permissions import get_permissions

router = APIRouter(prefix="/api/auth", tags=["auth"])

log = logging.getLogger("safeops360.auth")
settings = get_settings()


# --- In-memory OTP store (dev-only). One entry per email; overwritten by the
# most recent request. Cleared on process restart. Production must persist
# these (with hash + expiry + rate limit) — see BACKEND_TODO.md. ---
_OTP_STORE: dict[str, dict[str, Any]] = {}
_OTP_TTL_SECONDS = 600  # 10 minutes
_RESET_TOKEN_TTL_SECONDS = 900  # 15 minutes
_RESET_TOKEN_AUDIENCE = "safeops:password-reset"


def _user_to_out(u: User) -> UserOut:
    return UserOut(
        id=u.id,
        email=u.email,
        name=u.name,
        role=u.role,
        plantId=u.plantId,
        designation=u.designation,
        department=u.department,
    )


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> LoginResponse:
    stmt = select(User).where(User.email == payload.email.lower())
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        # Product decision: the login surfaces "user not found" distinctly from
        # "wrong password" so an operator can tell an absent/typo'd account from
        # a bad password. This deliberately trades the anti-enumeration stance
        # (the /demo-user lookup below already enumerates @safeops360.in users)
        # for a clearer demo UX. The web frontend maps 404 -> "User not found"
        # and 401 -> "Invalid credentials. Please try again."
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if not verify_password(payload.password, user.passwordHash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    token = create_access_token(
        subject=user.id,
        extra_claims={"role": user.role, "plantId": user.plantId, "email": user.email},
    )
    # Stub refresh token: mirror the access token so the mobile client has
    # something to store. Real refresh-token rotation is BACKEND_TODO #1.
    return LoginResponse(
        access_token=token,
        refresh_token=token,
        user=_user_to_out(user),
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_token_endpoint(
    payload: RefreshRequest | None = None,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> RefreshResponse:
    """Mint a fresh access token.

    Production should validate a server-side refresh token (DB row, rotation,
    revocation). For now we accept ANY JWT we previously issued — supplied
    either as the `refresh_token` body field or as the `Authorization: Bearer
    …` header. This lets the mobile app exercise the refresh flow without
    blocking on full token-rotation infra (BACKEND_TODO #1).
    """
    token: str | None = None
    if payload and payload.refresh_token:
        token = payload.refresh_token
    elif authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing refresh token")

    try:
        claims = safe_decode(token)
    except InvalidTokenError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid refresh token: {e}") from e

    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token has no subject")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer exists")

    new_token = create_access_token(
        subject=user.id,
        extra_claims={"role": user.role, "plantId": user.plantId, "email": user.email},
    )
    return RefreshResponse(access_token=new_token, refresh_token=new_token)


@router.get("/permissions", response_model=PermissionsResponse)
async def my_permissions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PermissionsResponse:
    perms = await get_permissions(db, user.id)
    return PermissionsResponse(permissions=perms)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return _user_to_out(user)


@router.get("/demo-user")
async def demo_user_lookup(email: str, db: AsyncSession = Depends(get_db)) -> dict[str, str | None]:
    """Public lookup used by the login page's demo role picker — given the
    composed demo email, returns just the user's display name + designation.
    Restricted to @safeops360.in addresses so it can't be used as a generic
    user-enumeration oracle."""
    e = (email or "").strip().lower()
    if not e or not e.endswith("@safeops360.in"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "demo email required")
    row = (await db.execute(select(User).where(User.email == e))).scalar_one_or_none()
    if row is None:
        return {"name": None, "designation": None}
    return {"name": row.name, "designation": row.designation}


# --- Password reset flow (dev stub) ------------------------------------------


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
async def forgot_password(
    payload: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)
) -> ForgotPasswordResponse:
    email = payload.email.lower()
    # Don't leak whether the email exists — always return ok=True.
    row = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    otp = f"{secrets.randbelow(900_000) + 100_000}"  # 6-digit
    if row is not None:
        _OTP_STORE[email] = {"otp": otp, "expiresAt": time.time() + _OTP_TTL_SECONDS}
        log.info("Password-reset OTP issued for %s: %s (dev mode)", email, otp)
    # Dev convenience: surface the OTP in the response body in non-production
    # so QA can complete the flow without an email gateway. Strip in prod.
    dev_otp = otp if (row is not None and not settings.is_production) else None
    return ForgotPasswordResponse(ok=True, dev_otp=dev_otp)


@router.post("/verify-otp", response_model=VerifyOtpResponse)
async def verify_otp(payload: VerifyOtpRequest) -> VerifyOtpResponse:
    email = payload.email.lower()
    entry = _OTP_STORE.get(email)
    if entry is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No OTP requested for this email")
    if entry["expiresAt"] < time.time():
        _OTP_STORE.pop(email, None)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "OTP expired")
    if entry["otp"] != payload.otp:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid OTP")

    # Burn the OTP so it can't be reused.
    _OTP_STORE.pop(email, None)

    # Issue a short-lived reset token. Reuse the JWT machinery — different
    # audience claim distinguishes it from access tokens.
    now = int(time.time())
    reset_jwt = jwt.encode(
        {
            "sub": email,
            "aud": _RESET_TOKEN_AUDIENCE,
            "iat": now,
            "exp": now + _RESET_TOKEN_TTL_SECONDS,
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    return VerifyOtpResponse(resetToken=reset_jwt)


@router.post("/reset-password", response_model=ResetPasswordResponse)
async def reset_password(
    payload: ResetPasswordRequest, db: AsyncSession = Depends(get_db)
) -> ResetPasswordResponse:
    try:
        claims = jwt.decode(
            payload.resetToken,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=_RESET_TOKEN_AUDIENCE,
        )
    except JWTError as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Invalid or expired reset token: {e}"
        ) from e

    email = (claims.get("sub") or "").lower()
    if not email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Reset token has no subject")

    row = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if row is None:
        # Don't leak that the account is gone, but obviously we can't reset.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Account not eligible for reset")

    row.passwordHash = hash_password(payload.newPassword)
    await db.commit()
    log.info("Password reset completed for %s", email)
    return ResetPasswordResponse(ok=True)


# --- Push device registration (stub) ---------------------------------------


@router.post("/devices", response_model=DeviceRegisterResponse)
async def register_device(
    payload: DeviceRegisterRequest,
    user: User = Depends(get_current_user),
) -> DeviceRegisterResponse:
    # Stub: real impl persists (user_id, token, platform, app_version, last_seen_at)
    # to a Device table and dedupes on (user_id, token). For now we just log.
    log.info(
        "Device registered (stub) user=%s platform=%s tokenPrefix=%s",
        user.id,
        payload.platform,
        payload.token[:12],
    )
    # Use a deterministic id derived from the token so the mobile client can
    # call DELETE /devices/{id} with a value it has on hand without us
    # needing a backing store.
    fake_id = f"dev-{abs(hash(payload.token)) % 10**12}"
    return DeviceRegisterResponse(id=fake_id, ok=True)


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_device(
    device_id: str,
    user: User = Depends(get_current_user),
) -> None:
    # Stub: real impl removes the row by id (scoped to the requesting user).
    log.info("Device unregistered (stub) user=%s id=%s", user.id, device_id)
    return None
