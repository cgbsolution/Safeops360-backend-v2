"""InsightSnapshot — weekly-recomputed insight lifecycle store (Weekly Insight
Engine, spec §2).

One row per insight identity per weekly run. The lifecycle state machine and the
meta-insight promotion read prior weeks off this table, so it is a hard
prerequisite for `escalating` / `persistent` / `meta_response_failure` — without
history those states can never fire.

Backend-only table (like Attachment / RiskAttachment): reached solely through
FastAPI, never Prisma, so it is NOT in schema.prisma. Created by the hand-DDL
applier `scripts/create_insight_snapshot_table.py` (idempotent). camelCase
columns match the house Prisma convention.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class InsightSnapshot(Base, IdMixin):
    __tablename__ = "InsightSnapshot"

    tenantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    module: Mapped[str] = mapped_column(String, nullable=False)

    # Stable identity across weeks: `${type}:${scopeHash}`.
    identityKey: Mapped[str] = mapped_column(String, nullable=False)

    type: Mapped[str] = mapped_column(String, nullable=False)
    weekOf: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    computedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    scoreComponents: Mapped[dict | None] = mapped_column(JSON)  # audit + tuning (§5)
    lifecycleState: Mapped[str] = mapped_column(String, nullable=False, default="new")

    consecutiveWeeksSurfaced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutiveEscalations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # drives meta (§7)
    firstSeenWeek: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lastHeroWeek: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    payload: Mapped[dict | None] = mapped_column(JSON)  # type-specific rail data (§4)
    recordIds: Mapped[list | None] = mapped_column(JSON)  # records this insight is grounded in

    wasHero: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rowPosition: Mapped[int | None] = mapped_column(Integer)  # 0,1,2 if in the secondary row

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


__all__ = ["InsightSnapshot"]
