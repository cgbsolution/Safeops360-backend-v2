"""SCI — Safety Culture Index point ledger (SCI-01).

SQLAlchemy mirror of the Prisma `SciLedgerEntry`. Points come ONLY from
verified operational events; entries are immutable (void → compensating
negative entry, never delete). camelCase columns match Prisma. See
SCI_Kaizen_Build_Prompt §4.3.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class SciLedgerEntry(Base, IdMixin):
    __tablename__ = "SciLedgerEntry"
    __table_args__ = (
        UniqueConstraint(
            "userId", "sourceModule", "sourceTransactionId",
            name="SciLedgerEntry_userId_sourceModule_sourceTransactionId_key",
        ),
    )

    userId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    sourceModule: Mapped[str] = mapped_column(String, nullable=False)
    sourceTransactionId: Mapped[str] = mapped_column(String, nullable=False)
    eventType: Mapped[str] = mapped_column(String, nullable=False)
    basePoints: Mapped[int] = mapped_column(Integer, nullable=False)
    multiplier: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    finalPoints: Mapped[int] = mapped_column(Integer, nullable=False)
    isAnonymous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    isVoided: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    voidCompensatingEntryId: Mapped[str | None] = mapped_column(String)
    scoringPeriod: Mapped[str] = mapped_column(String, nullable=False)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    auditTrail: Mapped[list] = mapped_column(JSONB, nullable=False)


__all__ = ["SciLedgerEntry"]
