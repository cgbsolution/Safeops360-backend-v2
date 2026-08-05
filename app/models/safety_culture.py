"""Safety Culture Management — SQLAlchemy models.

A live culture engine (not a static survey app): every sub-capability writes into
a per-site ``CultureMaturityProfile`` whose aggregate becomes a Key Risk Indicator
on the Enterprise Risk Register (see app/services/safety_culture.py + erm_metrics.py).

Site == ``Plant.id`` (the canonical isolation boundary — QueryScope, audit plant
tagging and ERM ``EnterpriseRisk.plantId`` all key off it). References to existing
tables (Plant / User / Observation / Capa) are plain String columns, no FKs, to
match the Prisma-owned schema convention used across the platform.

Schema is applied by prisma/apply-safetyculture-ddl.ts (mirrored in schema.prisma).
camelCase columns to match the DB.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


def _created():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated():
    return mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)


# ── §1 Culture Maturity Model Engine ─────────────────────────────────────────
class CultureMaturityProfile(Base, IdMixin):
    """One live profile per site. componentScores are flattened to columns so a
    KRI provider can aggregate them in SQL; the monthly history lives in
    CultureMaturitySnapshot for trend queries."""

    __tablename__ = "CultureMaturityProfile"

    plantId: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    currentStage: Mapped[str] = mapped_column(String, nullable=False, default="Reactive")
    stageScore: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # Component scores (0-100) — see calculate_culture_score().
    leadershipEngagement: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    workerParticipation: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    leadingLaggingRatio: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    bbsQualityIndex: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    perceptionIndex: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    industryVertical: Mapped[str | None] = mapped_column(String)
    lastCalculatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = _created()
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (
        Index("ix_CultureMaturityProfile_stage", "currentStage"),
    )


class CultureMaturitySnapshot(Base, IdMixin):
    """Monthly snapshot, retained indefinitely (§1 `history`)."""

    __tablename__ = "CultureMaturitySnapshot"

    plantId: Mapped[str] = mapped_column(String, nullable=False)
    period: Mapped[str] = mapped_column(String, nullable=False)  # YYYY-MM
    stageScore: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    currentStage: Mapped[str] = mapped_column(String, nullable=False, default="Reactive")
    componentScores: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    snapshotAt: Mapped[datetime] = _created()

    __table_args__ = (
        UniqueConstraint("plantId", "period", name="uq_CultureMaturitySnapshot_plant_period"),
        Index("ix_CultureMaturitySnapshot_plant", "plantId", "period"),
    )


# ── §2 BBS Quality — closure-loop companion to Observation ────────────────────
class CultureObservationClosure(Base, IdMixin):
    """Closure-loop metadata for an Observation, kept out of the core Observation
    table (which is shared/Prisma-owned). One row per observation once it enters
    the quality closure loop (§2 `closureLoop`)."""

    __tablename__ = "CultureObservationClosure"

    observationId: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    linkedCapaId: Mapped[str | None] = mapped_column(String)
    linkedActionId: Mapped[str | None] = mapped_column(String)
    reobservationVerified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reobservationDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verifiedById: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = _created()
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (
        Index("ix_CultureObservationClosure_plant", "plantId"),
    )


class CultureObserverIntegrity(Base, IdMixin):
    """Shared integrity gate keyed to (plant, observer, period) — the single source
    of truth both the BBS Quality Index card and the Recognition leaderboard read,
    so the two screens can never contradict each other (§Fix 1).

    ``status`` lifecycle:
      • ``flagged_pending_review``    — gaming-pattern detector raised it; not yet reviewed
      • ``flagged_reviewed_upheld``   — a human reviewer confirmed the concern (stays gated)
      • ``flagged_reviewed_dismissed``— a human reviewer cleared it (points un-freeze)
      • ``clear``                     — never flagged (implicit default; rows only exist once flagged)

    While a period is pending-review or upheld, Recognition freezes that observer's
    quality-derived points (read-time gate in ``leaderboard``) — reversible, so a
    dismissal restores rank with no recompute.
    """

    __tablename__ = "CultureObserverIntegrity"

    plantId: Mapped[str] = mapped_column(String, nullable=False)
    observerId: Mapped[str] = mapped_column(String, nullable=False)
    period: Mapped[str] = mapped_column(String, nullable=False)  # YYYY-MM
    status: Mapped[str] = mapped_column(String, nullable=False, default="flagged_pending_review")
    reasons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # pattern snapshot at flag time
    reviewNote: Mapped[str | None] = mapped_column(Text)
    reviewedById: Mapped[str | None] = mapped_column(String)
    reviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    flaggedAt: Mapped[datetime] = _created()

    createdAt: Mapped[datetime] = _created()
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (
        UniqueConstraint("plantId", "observerId", "period", name="uq_CultureObserverIntegrity_key"),
        Index("ix_CultureObserverIntegrity_plant_period", "plantId", "period"),
    )


# ── §3 Leadership Engagement ─────────────────────────────────────────────────
class LeadershipWalk(Base, IdMixin):
    __tablename__ = "LeadershipWalk"

    plantId: Mapped[str] = mapped_column(String, nullable=False)
    leaderId: Mapped[str] = mapped_column(String, nullable=False)
    scheduledDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completedDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, nullable=False, default="Scheduled")  # Scheduled|Completed|Missed|Rescheduled
    cadence: Mapped[str | None] = mapped_column(String)  # WEEKLY|MONTHLY (for recurring schedules)

    areaVisited: Mapped[str | None] = mapped_column(String)
    workersInteracted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observationsRaised: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hazardsIdentified: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    # §3 structured walk checklist (replaces free-text-only capture). Shape:
    #   { hazardCategories: [str], workerInteractions: [{count, topic}],
    #     ppeCompliance: int(0-100), housekeepingRating: int(1-5) }
    checklist: Mapped[dict | None] = mapped_column(JSON)
    followUpActionIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Set when a scheduled walk is auto-flipped to Missed and escalated (§3/§8).
    escalatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _created()
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (
        Index("ix_LeadershipWalk_plant_status", "plantId", "status"),
        Index("ix_LeadershipWalk_leader", "leaderId", "scheduledDate"),
    )


# ── §4 Perception Survey Engine ──────────────────────────────────────────────
class PerceptionSurveyTemplate(Base, IdMixin):
    __tablename__ = "PerceptionSurveyTemplate"

    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    industryVertical: Mapped[str | None] = mapped_column(String)
    # questions: [{ id, text, dimension, scaleType }]
    questions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    cadence: Mapped[str] = mapped_column(String, nullable=False, default="QUARTERLY")

    createdById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _created()
    updatedAt: Mapped[datetime] = _updated()


class PerceptionSurveyResponse(Base, IdMixin):
    """Anonymous response — NO PII, no link to respondent identity. The
    ``respondentAnonymousToken`` is a one-way hash used only to prevent double
    submission within a period; it cannot be reversed to a user."""

    __tablename__ = "PerceptionSurveyResponse"

    surveyTemplateId: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    period: Mapped[str] = mapped_column(String, nullable=False)  # e.g. 2026-Q3
    respondentAnonymousToken: Mapped[str] = mapped_column(String, nullable=False)
    # responses: [{ questionId, score }]
    responses: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    submittedAt: Mapped[datetime] = _created()

    __table_args__ = (
        UniqueConstraint(
            "surveyTemplateId", "plantId", "period", "respondentAnonymousToken",
            name="uq_PerceptionResponse_once_per_period",
        ),
        Index("ix_PerceptionResponse_plant_period", "plantId", "period"),
    )


class PerceptionIndexSnapshot(Base, IdMixin):
    __tablename__ = "PerceptionIndexSnapshot"

    plantId: Mapped[str] = mapped_column(String, nullable=False)
    period: Mapped[str] = mapped_column(String, nullable=False)
    dimensionScores: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    compositeScore: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    responseCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    responseRatePercent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    thresholdMet: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    publishedAt: Mapped[datetime] = _created()

    __table_args__ = (
        UniqueConstraint("plantId", "period", name="uq_PerceptionIndex_plant_period"),
        Index("ix_PerceptionIndex_plant", "plantId", "period"),
    )


# ── §6 Recognition Layer ─────────────────────────────────────────────────────
class RecognitionEntry(Base, IdMixin):
    """Quality-weighted recognition point (never raw submission counts). Idempotent
    per (plant, user, category, period) so re-running the award job doesn't
    double-count."""

    __tablename__ = "RecognitionEntry"

    plantId: Mapped[str] = mapped_column(String, nullable=False)
    userId: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)  # ObservationStreak|QualityContribution|LeadershipWalkCompliance|TeamMilestone|MostImproved
    points: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    periodEarned: Mapped[str] = mapped_column(String, nullable=False)  # YYYY-MM
    badgeAwarded: Mapped[str | None] = mapped_column(String)
    streakWeeks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = _created()
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (
        UniqueConstraint("plantId", "userId", "category", "periodEarned", name="uq_Recognition_unique_award"),
        Index("ix_Recognition_plant_period", "plantId", "periodEarned"),
        Index("ix_Recognition_user", "userId"),
    )


__all__ = [
    "CultureMaturityProfile",
    "CultureMaturitySnapshot",
    "CultureObservationClosure",
    "CultureObserverIntegrity",
    "LeadershipWalk",
    "PerceptionSurveyTemplate",
    "PerceptionSurveyResponse",
    "PerceptionIndexSnapshot",
    "RecognitionEntry",
]
