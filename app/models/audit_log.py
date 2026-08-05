"""Unified, tamper-evident audit log (P1-1).

One row per audited create / update / state-change / soft-delete / sensitive-read.
A per-entity SHA-256 hash chain (previousEntryHash → entryHash) makes any insert,
deletion, or modification of a historical entry self-detectable. The table is
append-only — the soft-delete guard blocks any delete of an AuditLog row.

Schema is owned by Prisma/DDL (apply-auditlog-ddl.ts); this mirror lets the
SQLAlchemy side write/read. camelCase columns to match.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class AuditLog(Base, IdMixin):
    __tablename__ = "AuditLog"

    sequenceNo: Mapped[int] = mapped_column(BigInteger, nullable=False)  # per-entity monotonic
    plantId: Mapped[str | None] = mapped_column(String)
    entityType: Mapped[str] = mapped_column(String, nullable=False)
    entityId: Mapped[str] = mapped_column(String, nullable=False)
    entityCode: Mapped[str | None] = mapped_column(String)
    action: Mapped[str] = mapped_column(String, nullable=False)
    actorId: Mapped[str | None] = mapped_column(String)
    actorType: Mapped[str] = mapped_column(String, nullable=False, default="SYSTEM")
    actorIp: Mapped[str | None] = mapped_column(String)
    # naive UTC (matches the TIMESTAMP(3) column) — the value we store is also what
    # we hash, so the chain recomputes identically without any tz conversion.
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    changedFields: Mapped[list | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(Text)
    correlationId: Mapped[str | None] = mapped_column(String)
    previousEntryHash: Mapped[str | None] = mapped_column(String)
    entryHash: Mapped[str] = mapped_column(String, nullable=False)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_AuditLog_entity", "entityType", "entityId", "sequenceNo"),
        Index("ix_AuditLog_actor", "actorId"),
        Index("ix_AuditLog_action", "action"),
        Index("ix_AuditLog_plant", "plantId"),
        Index("ix_AuditLog_ts", "timestamp"),
        Index("ix_AuditLog_corr", "correlationId"),
    )


__all__ = ["AuditLog"]
