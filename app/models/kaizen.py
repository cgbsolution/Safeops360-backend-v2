"""Kaizen Wall (SCI-01 §7) — governed community-wall posts.

SQLAlchemy mirror of the Prisma `KaizenPost`. submitterUserId is ALWAYS stored
(audit/dedup/points) but withheld from the committee review screen and the
public wall when anonymous. Declined posts are retained, never deleted.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class KaizenPost(Base, IdMixin):
    __tablename__ = "KaizenPost"

    submitterUserId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    isAnonymous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    category: Mapped[str] = mapped_column(String, nullable=False)
    hazardSeveritySelf: Mapped[str] = mapped_column(String, nullable=False)
    photoUrl: Mapped[str | None] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    locationTag: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING_COMMITTEE")
    committeeScoresJson: Mapped[list | None] = mapped_column(JSONB)
    finalCommitteeScore: Mapped[float | None] = mapped_column(Float)
    pointsAwardedSubmitter: Mapped[int | None] = mapped_column(Integer)
    declineFeedback: Mapped[str | None] = mapped_column(Text)
    parentPostId: Mapped[str | None] = mapped_column(String)
    crossPlantDistributed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reactionsCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reviewedByUserId: Mapped[str | None] = mapped_column(String)
    reviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # KWP-01 AI pre-screen
    aiDuplicateFlag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    aiLinkedTransactionId: Mapped[str | None] = mapped_column(String)
    aiCategorySuggestion: Mapped[str | None] = mapped_column(String)
    aiFlagReason: Mapped[str | None] = mapped_column(String)
    committeeRotationId: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KaizenCommitteeRotation(Base, IdMixin):
    __tablename__ = "KaizenCommitteeRotation"

    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    periodMonth: Mapped[str] = mapped_column(String, nullable=False)  # YYYY-MM
    memberUserIds: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    hseManagerSeat: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


__all__ = ["KaizenPost", "KaizenCommitteeRotation"]
