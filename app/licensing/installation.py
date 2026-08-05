"""Installation identity + monotonic last-seen (clock-tamper defence).

Two jobs:
  1. Give the install a stable id (generated at first boot) so a licence can be
     *bound* to it (build prompt §6.3) and so ops can issue install-pinned
     licences.
  2. Maintain a MONOTONIC high-water-mark timestamp (`last_seen`) advanced on
     every successful validation. Expiry is then checked against the *effective
     clock* = max(systemClock, last_seen), so winding the OS clock backward
     cannot extend a POC (build prompt §6.2, TL-07).

The store is abstracted so the security tests can use an in-memory store with a
frozen clock, while production uses the DB-backed store (installation.db).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

# How far the system clock may sit *behind* the recorded high-water mark before
# we raise a tamper warning. Small allowance for NTP jitter / VM pauses.
CLOCK_TAMPER_TOLERANCE = timedelta(minutes=5)


@dataclass
class InstallationIdentity:
    installation_id: str
    first_boot_at: datetime
    last_seen_timestamp: datetime


def new_installation_id() -> str:
    return str(uuid.uuid4())


def effective_clock(system_now: datetime, last_seen: datetime | None) -> datetime:
    """The clock used for all expiry decisions. Never moves backward across
    restarts because last_seen is monotonic and persisted."""
    if last_seen is None:
        return system_now
    return max(system_now, last_seen)


def advance_last_seen(last_seen: datetime | None, system_now: datetime) -> datetime:
    """New high-water mark after a validation pass."""
    if last_seen is None:
        return system_now
    return max(last_seen, system_now)


def detect_clock_rollback(system_now: datetime, last_seen: datetime | None) -> bool:
    """True when the OS clock sits meaningfully behind the high-water mark —
    i.e. someone (or something) wound it back. Enforcement still uses the
    effective clock; this is purely for the admin tamper alert."""
    if last_seen is None:
        return False
    return system_now < (last_seen - CLOCK_TAMPER_TOLERANCE)


# ── Store abstraction ────────────────────────────────────────────────────────
class InstallationStore(Protocol):
    async def get_or_create(self, *, now: datetime) -> InstallationIdentity: ...
    async def set_last_seen(self, ts: datetime) -> None: ...


class InMemoryInstallationStore:
    """For tests and as a safe fallback if the DB is unreachable at validation
    time (the licence still validates offline; only the persistent monotonic
    guarantee is reduced to process lifetime)."""

    def __init__(self, identity: InstallationIdentity | None = None) -> None:
        self._identity = identity

    async def get_or_create(self, *, now: datetime) -> InstallationIdentity:
        if self._identity is None:
            self._identity = InstallationIdentity(
                installation_id=new_installation_id(),
                first_boot_at=now,
                last_seen_timestamp=now,
            )
        return self._identity

    async def set_last_seen(self, ts: datetime) -> None:
        if self._identity is not None:
            self._identity.last_seen_timestamp = max(
                self._identity.last_seen_timestamp, ts
            )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
