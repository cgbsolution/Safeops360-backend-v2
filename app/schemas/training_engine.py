"""Pydantic schemas for the Training & Competency Engine router.

House style: a private _Base with from_attributes so ORM rows serialise directly;
request bodies validate input. Reads mostly return dicts (matches competency.py).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")


# ── HazardToSkillMapping (the moat config) ───────────────────────────────────
class HazardMappingCreate(_Base):
    sourceModule: str = "ANY"  # INCIDENT | NEAR_MISS | OBSERVATION | ANY
    classificationField: str  # category | hazardCategory | initialRootCauseCategory | severity | type | keyword
    classificationValue: str
    competencyId: str
    matchMode: str = "exact"  # exact | keyword
    plantId: str | None = None
    priority: int = 100
    notes: str | None = None
    isActive: bool = True


class HazardMappingUpdate(_Base):
    sourceModule: str | None = None
    classificationField: str | None = None
    classificationValue: str | None = None
    competencyId: str | None = None
    matchMode: str | None = None
    plantId: str | None = None
    priority: int | None = None
    notes: str | None = None
    isActive: bool | None = None


class HazardMappingOut(_Base):
    id: str
    plantId: str | None
    sourceModule: str
    classificationField: str
    classificationValue: str
    matchMode: str
    competencyId: str
    priority: int
    notes: str | None
    isActive: bool


# ── TrainingRuleConfig (thresholds/windows) ──────────────────────────────────
class RuleConfigUpdate(_Base):
    plantId: str | None = None  # target config (null = global)
    thresholdCount: int | None = Field(default=None, ge=1, le=100)
    thresholdWindowDays: int | None = Field(default=None, ge=1, le=3650)
    severitySifImmediate: bool | None = None
    severityThreshold: str | None = None  # HIGH | CRITICAL
    recertWindowDays: int | None = Field(default=None, ge=1, le=3650)
    assignmentDueDays: int | None = Field(default=None, ge=1, le=3650)
    correlationWindowDays: int | None = Field(default=None, ge=1, le=3650)
    # person-risk analytics
    personFlagThreshold: int | None = Field(default=None, ge=1, le=100)
    personFlagWindowDays: int | None = Field(default=None, ge=1, le=3650)
    personRiskElevated: int | None = Field(default=None, ge=1, le=1000)
    personRiskHigh: int | None = Field(default=None, ge=1, le=1000)
    personRiskCritical: int | None = Field(default=None, ge=1, le=1000)


# ── TrainingContent (content adapter) ────────────────────────────────────────
class ContentCreate(_Base):
    competencyId: str
    title: str
    contentType: str  # video | document | quiz | vr_package | ar_package | external_link
    deliveryMode: str  # hosted | external_redirect | local_package
    contentRef: str
    description: str | None = None
    vendorId: str | None = None
    vendorName: str | None = None
    durationMinutes: int | None = None
    passingScore: int | None = None
    language: str = "en"
    isPrimary: bool = False
    plantId: str | None = None
    isActive: bool = True


class ContentUpdate(_Base):
    title: str | None = None
    contentType: str | None = None
    deliveryMode: str | None = None
    contentRef: str | None = None
    description: str | None = None
    vendorId: str | None = None
    vendorName: str | None = None
    durationMinutes: int | None = None
    passingScore: int | None = None
    language: str | None = None
    isPrimary: bool | None = None
    plantId: str | None = None
    isActive: bool | None = None


# ── TrainingAssignment ───────────────────────────────────────────────────────
class ManualAssignCreate(_Base):
    plantId: str
    personUserId: str
    competencyId: str
    dueDays: int | None = None
    contentId: str | None = None


class AssignmentCompleteBody(_Base):
    evidenceType: str = "training_completion"  # training_completion | assessment | manual_signoff
    evidenceId: str | None = None
    note: str | None = None


class AssignmentStatusBody(_Base):
    status: str  # in_progress | escalated | cancelled (never a dismissal of a mandatory one)
    note: str | None = None
