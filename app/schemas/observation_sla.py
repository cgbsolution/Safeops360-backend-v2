"""Pydantic contracts for the observation SLA matrix, worker involvement and
the deroster workflow."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.observation_sla import (
    CATEGORY_GROUPS,
    DEFAULT_REVIEW_SLA_HOURS,
    PARTY_TYPES,
)


# ── SLA matrix ───────────────────────────────────────────────────────────────
class SlaRowIn(BaseModel):
    severity: str
    categoryGroup: str
    slaDays: int = Field(ge=1, le=365)
    isActive: bool = True

    def validated(self) -> "SlaRowIn":
        if self.severity.upper() not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
            raise ValueError(f"Unknown severity '{self.severity}'.")
        if self.categoryGroup.upper() not in CATEGORY_GROUPS:
            raise ValueError(f"Unknown category group '{self.categoryGroup}'.")
        return self


class SlaRowOut(BaseModel):
    id: str
    plantId: str | None
    severity: str
    categoryGroup: str
    slaDays: int
    isActive: bool
    updatedAt: datetime
    updatedById: str | None
    # True when this row is the global default rather than a plant override —
    # the admin table renders inherited rows differently from owned ones.
    inherited: bool = False

    model_config = ConfigDict(from_attributes=True)


class DerosterConfigOut(BaseModel):
    reviewSlaHours: int = DEFAULT_REVIEW_SLA_HOURS
    escalationContactUserId: str | None = None
    escalationRoleCode: str = "HSE_MANAGER"
    inherited: bool = False


class SlaConfigOut(BaseModel):
    plantId: str | None
    rows: list[SlaRowOut]
    deroster: DerosterConfigOut


class SlaConfigUpsert(BaseModel):
    """Bulk upsert. `rows` replaces the matrix for this scope; omitted
    combinations are left untouched rather than deleted, so a partial save from
    a filtered table cannot silently deactivate policy."""

    model_config = ConfigDict(extra="ignore")

    rows: list[SlaRowIn] = []
    reviewSlaHours: int | None = Field(default=None, ge=1, le=720)
    escalationContactUserId: str | None = None
    escalationRoleCode: str | None = None


class SlaPreviewOut(BaseModel):
    matched: bool
    categoryGroup: str | None
    # category_axis | category_any | axis_fallback | pending — how the group was
    # decided. `pending` means the category has no agreed Behavioural/Physical
    # classification yet, which is NOT the same as a missing matrix row.
    categoryGroupSource: str | None = None
    # PENDING_DECISION | NO_POLICY | null. Lets the form explain which kind of
    # gap it hit rather than blaming config that is actually fine.
    reason: str | None = None
    severity: str
    slaDays: int | None
    targetDate: datetime | None
    label: str | None
    scope: str | None = None


# ── STOP category → Behavioural | Physical mapping ──────────────────────────
class CategoryGroupOut(BaseModel):
    id: str
    categoryCode: str
    categoryLabel: str
    stopReferenceCode: str
    axis: str
    categoryGroup: str
    # True when this category has no agreed classification. Observations in it
    # resolve no SLA and fall back to manual entry.
    pending: bool = False
    notes: str | None = None


class CategoryGroupIn(BaseModel):
    categoryCode: str
    categoryGroup: str
    axis: str | None = None  # defaults to ANY
    notes: str | None = None


class CategoryGroupUpsert(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rows: list[CategoryGroupIn] = []


# ── worker involvement ───────────────────────────────────────────────────────
class WorkerInvolvedIn(BaseModel):
    """One named worker. Exactly one of userId / contractorWorkerId, matching
    partyType — validated server-side in the router, where the DB is reachable."""

    partyType: str
    userId: str | None = None
    contractorWorkerId: str | None = None

    def validated(self) -> "WorkerInvolvedIn":
        if self.partyType not in PARTY_TYPES:
            raise ValueError(f"Unknown partyType '{self.partyType}'.")
        has_user = bool(self.userId)
        has_worker = bool(self.contractorWorkerId)
        if has_user == has_worker:
            raise ValueError("Provide exactly one of userId / contractorWorkerId.")
        if self.partyType == "USER" and not has_user:
            raise ValueError("partyType USER requires userId.")
        if self.partyType == "CONTRACTOR_WORKER" and not has_worker:
            raise ValueError("partyType CONTRACTOR_WORKER requires contractorWorkerId.")
        return self


class DerosterOut(BaseModel):
    id: str
    status: str
    # How this flag may be described outside the review panel. A pending flag
    # reads "Under safety review", never "derostered" — see
    # services/observation_deroster.visible_status.
    displayLabel: str
    punitive: bool
    flaggedAt: datetime
    flaggedReason: str
    reviewSlaHours: int
    reviewDueAt: datetime
    reviewedById: str | None = None
    reviewedAt: datetime | None = None
    reviewDecisionReason: str | None = None
    correctiveActionTrainingId: str | None = None
    correctiveActionCompetencyId: str | None = None
    correctiveAction: dict | None = None
    escalatedAt: datetime | None = None
    escalatedToId: str | None = None
    reinstatedById: str | None = None
    reinstatedAt: datetime | None = None
    reinstatementNote: str | None = None


class WorkerInvolvedOut(BaseModel):
    id: str
    partyType: str
    userId: str | None
    contractorWorkerId: str | None
    name: str
    role: str | None
    employer: str | None
    rosterStatus: str | None = None
    deroster: DerosterOut | None = None


class WorkerSearchOut(BaseModel):
    """A row in the Worker Involved picker. Unified shape across both people
    tables so the client renders one list."""

    partyType: str
    id: str
    name: str
    role: str | None = None
    employer: str | None = None
    code: str | None = None
    plantId: str | None = None
    rosterStatus: str = "active"


# ── deroster actions ─────────────────────────────────────────────────────────
class DerosterDecisionIn(BaseModel):
    reason: str = Field(min_length=10)


class DerosterReinstateIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    note: str | None = None


class DerosterEventOut(BaseModel):
    id: str
    action: str
    fromStatus: str | None
    toStatus: str | None
    actorId: str | None
    notes: str | None
    context: dict | None
    createdAt: datetime

    model_config = ConfigDict(from_attributes=True)


# ── target closure date ──────────────────────────────────────────────────────
class TargetDateOverrideIn(BaseModel):
    date: datetime
    reason: str = Field(min_length=10)


class TargetDateReassignIn(BaseModel):
    """Section Head reassignment at review. No reason required — this is a
    normal workflow step, not an override (spec §2.2)."""

    date: datetime


class TargetDateHistoryOut(BaseModel):
    id: str
    targetDate: datetime | None
    source: str
    reason: str | None
    slaConfigApplied: dict | None
    changedById: str | None
    changedAt: datetime

    model_config = ConfigDict(from_attributes=True)


__all__ = [
    "SlaRowIn",
    "SlaRowOut",
    "SlaConfigOut",
    "SlaConfigUpsert",
    "SlaPreviewOut",
    "CategoryGroupOut",
    "CategoryGroupIn",
    "CategoryGroupUpsert",
    "DerosterConfigOut",
    "WorkerInvolvedIn",
    "WorkerInvolvedOut",
    "WorkerSearchOut",
    "DerosterOut",
    "DerosterDecisionIn",
    "DerosterReinstateIn",
    "DerosterEventOut",
    "TargetDateOverrideIn",
    "TargetDateReassignIn",
    "TargetDateHistoryOut",
]
