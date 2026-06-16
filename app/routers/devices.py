"""Push-device registry. Mounts at /api/devices.

Mirrors the stubbed endpoints under /api/auth/devices so the mobile client
(see safeops-app/src/services/push.ts) can register its Expo push token at
the documented `/api/devices/register` path.

Real impl persists (user_id, token, platform, app_version, last_seen_at) to a
Device table and dedupes on (user_id, token). See BACKEND_TODO.md in the
mobile app for the production contract. Today this just acks the token so
the mobile flow doesn't 404.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, status

from app.core.deps import get_current_user
from app.models.user import User
from app.schemas.auth import DeviceRegisterRequest, DeviceRegisterResponse

router = APIRouter(prefix="/api/devices", tags=["devices"])
log = logging.getLogger("safeops360.devices")


@router.post("/register", response_model=DeviceRegisterResponse)
async def register_device(
    payload: DeviceRegisterRequest,
    user: User = Depends(get_current_user),
) -> DeviceRegisterResponse:
    log.info(
        "Device registered (stub) user=%s platform=%s tokenPrefix=%s",
        user.id,
        payload.platform,
        payload.token[:12],
    )
    fake_id = f"dev-{abs(hash(payload.token)) % 10**12}"
    return DeviceRegisterResponse(id=fake_id, ok=True)


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_device(
    device_id: str,
    user: User = Depends(get_current_user),
) -> None:
    log.info("Device unregistered (stub) user=%s id=%s", user.id, device_id)
    return None
