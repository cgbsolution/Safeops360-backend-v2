"""Safety Culture Management — request/response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")


# ── §3 Leadership walks ──────────────────────────────────────────────────────
class LeadershipWalkCreate(_Base):
    plantId: str
    leaderId: str
    scheduledDate: datetime
    areaVisited: str | None = None
    cadence: str | None = None  # WEEKLY | MONTHLY (recurring intent)
    notes: str | None = None


class WalkChecklist(_Base):
    """§Fix 3 structured walk checklist — replaces free-text-only capture."""

    hazardCategories: list[str] = Field(default_factory=list)
    # [{ count, topic }] — worker interactions logged during the walk
    workerInteractions: list[dict] = Field(default_factory=list)
    ppeCompliance: int | None = None  # PPE spot-check %, 0-100
    housekeepingRating: int | None = None  # 1-5


class LeadershipWalkComplete(_Base):
    completedDate: datetime | None = None
    areaVisited: str | None = None
    workersInteracted: int = 0
    observationsRaised: int = 0
    hazardsIdentified: int = 0
    notes: str | None = None
    checklist: WalkChecklist | None = None
    followUpActionIds: list[str] = Field(default_factory=list)


class WalkRaiseObservation(_Base):
    """§Fix 3 — raise a hazard/observation from a leadership walk into the same BBS
    closure loop (Logged → Linked → Verified). Optionally spawn a CAPA immediately."""

    description: str
    category: str = "OTHERS"  # must match the live DB ObservationCategory enum
    severity: str = "MEDIUM"
    spawnCapa: bool = True


# ── §2 BBS closure loop ──────────────────────────────────────────────────────
class ObservationLinkAction(_Base):
    linkedCapaId: str | None = None
    linkedActionId: str | None = None
    # If true and no CAPA is linked yet, spawn a SAFETY_CULTURE CAPA for this obs.
    spawnCapa: bool = False


class ObservationVerifyClosure(_Base):
    reobservationDate: datetime | None = None
    verified: bool = True


# ── §Fix 1 integrity review ──────────────────────────────────────────────────
class IntegrityReview(_Base):
    period: str  # YYYY-MM the flag belongs to
    outcome: str  # "dismiss" (clear) | "uphold" (keep gated)
    note: str = Field(min_length=1)  # required reviewer note


# ── §4 Perception surveys ────────────────────────────────────────────────────
class SurveyQuestion(_Base):
    id: str
    text: str
    dimension: str  # TrustInReporting|PsychologicalSafety|ManagementCommitment|PeerAccountability
    scaleType: str = "likert5"


class SurveyTemplateCreate(_Base):
    name: str
    description: str | None = None
    industryVertical: str | None = None
    cadence: str = "QUARTERLY"
    questions: list[SurveyQuestion]


class SurveyAnswer(_Base):
    questionId: str
    score: int = Field(ge=1, le=5)


class SurveyResponseSubmit(_Base):
    plantId: str
    period: str  # e.g. 2026-Q3
    responses: list[SurveyAnswer]


# ── §6 Recognition ───────────────────────────────────────────────────────────
class RecognitionAwardRequest(_Base):
    plantId: str
    period: str | None = None  # defaults to current month
