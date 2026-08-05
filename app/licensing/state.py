"""Process-wide runtime licence state + async orchestration.

`get_state()` returns the cached `RuntimeLicenceState` that the enforcement
layer reads on every request (no DB / no crypto on the hot path — validation is
done out-of-band). `refresh_state()` re-runs validation: read the .lic file,
read the installation high-water mark from the DB, evaluate, persist the
advanced mark + diagnostics, and publish the new state. It runs on boot, on a
timer, and on licence upload.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal
from app.licensing import keys
from app.licensing.installation import (
    InstallationIdentity,
    new_installation_id,
)
from app.licensing.payload import RuntimeLicenceState
from app.licensing.validator import compute_advanced_last_seen, evaluate_licence

log = logging.getLogger("safeops360.licensing")
settings = get_settings()

# Backend root = the directory that contains the `app` package.
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── In-process cache (the hot path reads this) ───────────────────────────────
_current_state: RuntimeLicenceState = RuntimeLicenceState.locked(
    "MISSING", now=_utcnow(), error="Licence not yet validated"
)


def get_state() -> RuntimeLicenceState:
    return _current_state


def _set_state(state: RuntimeLicenceState) -> None:
    global _current_state
    _current_state = state


# ── Licence file location ────────────────────────────────────────────────────
def licence_file_path() -> str:
    """Where the .lic lives. Config-driven; defaults to <backend>/licence.lic.
    This only says WHERE the licence is — it can never grant entitlements."""
    return settings.licence_file_path or os.path.join(_BACKEND_ROOT, "licence.lic")


def read_licence_token() -> str | None:
    """Locate the licence token. Resolution order:
      1. the .lic FILE (on-prem default; also where a UI upload is written), then
      2. the LICENCE_TOKEN env var (robust for cloud/container deploys whose
         filesystem is ephemeral — set it once in the host's env and it survives
         restarts).
    A file, when present and non-empty, wins so a UI upload can override the
    baseline env token."""
    path = licence_file_path()
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    except OSError as e:  # unreadable / permission — fall through to env, fail closed
        log.warning("Could not read licence file %s: %s", path, e)

    env_token = (settings.licence_token or "").strip()
    return env_token or None


def write_licence_token(token: str) -> None:
    """Persist an uploaded licence to the configured path. The caller must have
    validated it first; we still re-validate via refresh_state afterwards."""
    path = licence_file_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(token.strip())


# ── DB-backed installation identity ──────────────────────────────────────────
async def _load_or_create_identity(db: AsyncSession, *, now: datetime):
    """Return (row, identity). Creates the singleton on first boot."""
    from app.models.licensing import SINGLETON_ID, LicenceInstallation

    row = (
        await db.execute(
            select(LicenceInstallation).where(LicenceInstallation.id == SINGLETON_ID)
        )
    ).scalar_one_or_none()

    if row is None:
        row = LicenceInstallation(
            id=SINGLETON_ID,
            installationId=new_installation_id(),
            firstBootAt=now,
            lastSeenTimestamp=now,
            clockTamperDetected=False,
        )
        db.add(row)
        await db.flush()

    identity = InstallationIdentity(
        installation_id=row.installationId,
        first_boot_at=row.firstBootAt,
        last_seen_timestamp=row.lastSeenTimestamp,
    )
    return row, identity


# ── Orchestration ────────────────────────────────────────────────────────────
async def refresh_state(db: AsyncSession | None = None) -> RuntimeLicenceState:
    """Re-validate the licence and publish the new state. Opens its own DB
    session when none is supplied (boot / timer). Any failure to reach the DB
    still produces a valid offline decision — only the persistent monotonic
    guarantee degrades to process lifetime."""
    if db is not None:
        return await _refresh_with_db(db)
    try:
        async with AsyncSessionLocal() as session:
            try:
                state = await _refresh_with_db(session)
                await session.commit()
                return state
            except Exception:
                await session.rollback()
                raise
    except Exception as e:  # noqa: BLE001 — never let validation crash the app
        log.warning("Licence refresh could not reach the DB: %s", e)
        return _refresh_offline_only()


async def _refresh_with_db(db: AsyncSession) -> RuntimeLicenceState:
    now = _utcnow()
    token = read_licence_token()
    row, identity = await _load_or_create_identity(db, now=now)

    state = evaluate_licence(
        token,
        system_now=now,
        last_seen=identity.last_seen_timestamp,
        local_installation_id=identity.installation_id,
        public_key_resolver=keys.get_public_key,
        warn_days=settings.licence_warn_days,
    )

    # Advance the monotonic high-water mark + persist diagnostics.
    row.lastSeenTimestamp = compute_advanced_last_seen(identity.last_seen_timestamp, now)
    row.lastStatus = state.status
    row.lastValidatedAt = now
    row.lastError = state.validation_error
    row.clockTamperDetected = state.clock_tamper_warning
    if state.payload is not None:
        row.lastLicenceJti = state.payload.jti
        row.lastLicenceIat = state.payload.iat

    _set_state(state)
    log.info(
        "Licence validated: status=%s edition=%s modules=%d tamper=%s",
        state.status,
        state.payload.edition if state.payload else "-",
        len(state.enabled_module_set),
        state.clock_tamper_warning,
    )
    return state


async def read_installation_identity(db: AsyncSession) -> InstallationIdentity | None:
    """Read the installation identity WITHOUT creating it (for display)."""
    from app.models.licensing import SINGLETON_ID, LicenceInstallation

    row = (
        await db.execute(
            select(LicenceInstallation).where(LicenceInstallation.id == SINGLETON_ID)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return InstallationIdentity(
        installation_id=row.installationId,
        first_boot_at=row.firstBootAt,
        last_seen_timestamp=row.lastSeenTimestamp,
    )


async def evaluate_dry_run(db: AsyncSession, token: str) -> RuntimeLicenceState:
    """Validate `token` against the real installation identity WITHOUT
    persisting or publishing it — used by the upload endpoint to reject a bad
    licence before it can clobber a working one."""
    now = _utcnow()
    _, identity = await _load_or_create_identity(db, now=now)
    return evaluate_licence(
        token,
        system_now=now,
        last_seen=identity.last_seen_timestamp,
        local_installation_id=identity.installation_id,
        public_key_resolver=keys.get_public_key,
        warn_days=settings.licence_warn_days,
    )


def _refresh_offline_only() -> RuntimeLicenceState:
    """DB-less validation fallback — still verifies the signature + expiry
    against the system clock (no persistent monotonic mark)."""
    now = _utcnow()
    token = read_licence_token()
    state = evaluate_licence(
        token,
        system_now=now,
        last_seen=None,
        local_installation_id=None,
        public_key_resolver=keys.get_public_key,
        warn_days=settings.licence_warn_days,
    )
    _set_state(state)
    return state
