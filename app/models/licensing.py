"""Persistent licensing state — a single-row table.

Holds the installation identity (for binding + clock-tamper) and the last
validation diagnostics (so the admin screen and tamper warnings survive a
restart). One row only, addressed by the fixed primary key SINGLETON_ID.

Column names are camelCase to match the Prisma/Postgres convention used across
this codebase. Applied to the live DB via hand-DDL (see
scripts/apply_licensing_ddl.py) — NOT via `prisma db push`, which would drop
the hand-managed Cams*/factory tables.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models._base import IdMixin, TimestampMixin, gen_id

# The licensing state is a singleton — one install, one row.
SINGLETON_ID = "licence-singleton"


class LicenceInstallation(Base, TimestampMixin):
    __tablename__ = "LicenceInstallation"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=SINGLETON_ID)

    # ── Installation identity (build prompt §3.5) ──
    installationId: Mapped[str] = mapped_column(String, nullable=False)
    firstBootAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Monotonic high-water mark — clock-rollback defence (build prompt §6.2).
    lastSeenTimestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # ── Last validation diagnostics (admin-only) ──
    lastStatus: Mapped[str | None] = mapped_column(String, nullable=True)
    lastValidatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lastError: Mapped[str | None] = mapped_column(String, nullable=True)
    clockTamperDetected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ── Last validated licence identity (supersession / replay tracking) ──
    lastLicenceJti: Mapped[str | None] = mapped_column(String, nullable=True)
    lastLicenceIat: Mapped[int | None] = mapped_column(nullable=True)


class FactoryModuleEntitlement(Base, IdMixin, TimestampMixin):
    """Admin-managed per-factory (per-Plant) module allocation, *within* the
    signed-licence ceiling.

    The signed licence sets the hard ceiling (which modules exist at all). This
    table only lets an admin RESTRICT a licensed module for a specific factory —
    it can NEVER grant a module the licence doesn't include, so the
    config-can't-grant-entitlements rule (build prompt §5.3) still holds.

    Opt-out semantics: a row exists only when an admin has explicitly set a
    module's state for a factory. `enabled = false` disables it for that factory;
    absence of a row means the module is on (inherited from the licence). So a
    fresh install with no rows behaves exactly as before — every licensed module
    on at every factory.
    """

    __tablename__ = "FactoryModuleEntitlement"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    moduleCode: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Optional validity window for this module AT THIS FACTORY. The admin grants
    # a plant a module "for a period". Both null = no time bound (never expires,
    # the default). validUntil null with validFrom set = active from a date,
    # forever. Always still capped by the signed-licence ceiling — a per-factory
    # window can never reach beyond what the licence grants.
    validFrom: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    validUntil: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updatedBy: Mapped[str | None] = mapped_column(String, nullable=True)
