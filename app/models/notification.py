"""In-app notification record.

Powers the ERM automated-notification surface (owner-assignment alerts,
pre-due reminders, overdue escalations) plus the dashboard "alerts" feed.
Each row is one addressed-to-a-user notification; it may ALSO have been
emailed at create time (best-effort — see app.services.erm_notifications).

Schema is owned here (SQLAlchemy) — this is a NEW table, not mirrored from
Prisma. camelCase column names match the platform-wide Prisma convention so
DDL and future Prisma introspection stay consistent.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class Notification(Base, IdMixin):
    __tablename__ = "Notification"

    userId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    # e.g. RISK_OWNER_ASSIGNED, TREATMENT_ASSIGNED, TREATMENT_REMINDER,
    # TREATMENT_OVERDUE, APPROVAL_PENDING
    type: Mapped[str] = mapped_column(String, nullable=False)
    # INFO | WARNING | CRITICAL
    severity: Mapped[str] = mapped_column(String, nullable=False, default="INFO")
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # e.g. "EnterpriseRisk", "Capa"
    entityType: Mapped[str | None] = mapped_column(String)
    entityId: Mapped[str | None] = mapped_column(String)
    linkUrl: Mapped[str | None] = mapped_column(String)

    isRead: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    readAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_Notification_user_read", "userId", "isRead"),
    )


__all__ = ["Notification"]
