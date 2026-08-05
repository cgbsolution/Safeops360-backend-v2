"""Normalise an engagement of either kind into one shape.

docs/cams/08 §1 Decision 1: the programme sits above BOTH engines, so
`ProgrammeSlot` points at an engagement polymorphically. This module is that
pointer's dereference — and it is deliberately the *whole* abstraction, because
**WP-18 (unify the two engines) needs exactly this seam anyway**. Building it
here means WP-18 inherits a tested abstraction with real callers instead of
inventing one mid-merge; when unification lands, this file collapses to a single
branch and every caller is unchanged.

Six of the seven concepts a programme needs already line up one-to-one across the
two engines (site, lead auditor, team, auditee side, planned date, type). The
seventh — standards — is free text on the audit side and a JSON list on the
inspection side, which is what WP-20's clause catalogue fixes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.cams import CamsEngagement, CamsResponse

# An engagement counts toward coverage only once it is genuinely finished.
AUDIT_COMPLETE_STATES = ("closed",)
INSPECTION_COMPLETE_STATES = ("REPORT_ISSUED", "CLOSED")

AUDIT_LIVE_STATES = ("in_progress", "submitted_pending_response", "response_in_progress", "under_review")
INSPECTION_LIVE_STATES = ("IN_PROGRESS", "FIELDWORK_COMPLETE", "FINDINGS_REVIEW")


@dataclass
class ResolvedEngagement:
    """The normalised view every programme surface reads."""

    kind: str  # AUDIT | INSPECTION
    id: str
    code: str
    title: str
    siteId: str | None
    status: str
    isComplete: bool
    isLive: bool
    plannedDate: datetime | None
    actualDate: datetime | None
    leadAuditorId: str | None
    scorePct: float | None
    # dimensionKey -> (assessed, total) for the dimensions this engagement touched.
    assessedByDimension: dict[str, tuple[int, int]] = field(default_factory=dict)
    samplingApproach: str = "FULL"

    def as_dict(self) -> dict[str, Any]:
        return {
            "engagementKind": self.kind,
            "engagementId": self.id,
            "code": self.code,
            "title": self.title,
            "siteId": self.siteId,
            "status": self.status,
            "isComplete": self.isComplete,
            "isLive": self.isLive,
            "plannedDate": self.plannedDate.isoformat() if self.plannedDate else None,
            "actualDate": self.actualDate.isoformat() if self.actualDate else None,
            "leadAuditorId": self.leadAuditorId,
            "scorePct": self.scorePct,
            "samplingApproach": self.samplingApproach,
            "dimensions": {
                k: {"assessed": a, "total": t} for k, (a, t) in self.assessedByDimension.items()
            },
        }


def _as_date(v: Any) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    return None


async def resolve_audit(
    db: AsyncSession, audit: ComplianceAudit, *, with_dimensions: bool = True
) -> ResolvedEngagement:
    dims: dict[str, tuple[int, int]] = {}
    if with_dimensions:
        # "Assessed" is defined ONCE, against the column the module has already
        # been burned by. ⚠ This depends on WP-02's backfill: until the 242
        # desynced rows are reconciled, coverage under-reports on exactly the
        # audits F-29 broke. That is a stated hard dependency, not a surprise.
        rows = (
            await db.execute(
                select(
                    AuditCheckpointResponse.categoryId,
                    AuditCheckpointResponse.assessmentStatus,
                ).where(AuditCheckpointResponse.auditId == audit.id)
            )
        ).all()
        for cat, status in rows:
            if not cat:
                continue
            assessed, total = dims.get(cat, (0, 0))
            dims[cat] = (assessed + (1 if status != "NOT_ASSESSED" else 0), total + 1)

    return ResolvedEngagement(
        kind="AUDIT",
        id=audit.id,
        code=audit.auditNumber,
        title=audit.title,
        siteId=audit.plantId,
        status=audit.status,
        isComplete=audit.status in AUDIT_COMPLETE_STATES,
        isLive=audit.status in AUDIT_LIVE_STATES,
        plannedDate=_as_date(audit.scheduledDate),
        actualDate=_as_date(audit.closedAt or audit.actualEndAt or audit.submittedAt),
        leadAuditorId=audit.leadAuditorUserId,
        scorePct=audit.overallCompliancePct,
        assessedByDimension=dims,
        samplingApproach="FULL",
    )


async def resolve_inspection(
    db: AsyncSession, eng: CamsEngagement, *, with_dimensions: bool = True
) -> ResolvedEngagement:
    dims: dict[str, tuple[int, int]] = {}
    if with_dimensions:
        rows = (
            await db.execute(
                select(CamsResponse).where(CamsResponse.engagementId == eng.id)
            )
        ).scalars().all()
        # The inspection engine has no discipline column; its scope unit is the
        # standard it was run against. One bucket per declared standard, with the
        # response counts attributed to each — coarser than the audit side, and
        # honestly so until WP-18 gives both engines one scope model.
        answered = sum(1 for r in rows if getattr(r, "result", None))
        total = len(rows)
        for std in (eng.standardRefs or []) or ["_UNSPECIFIED"]:
            dims[std] = (answered, total)

    return ResolvedEngagement(
        kind="INSPECTION",
        id=eng.id,
        code=eng.engagementCode,
        title=eng.title,
        siteId=eng.siteId,
        status=eng.status,
        isComplete=eng.status in INSPECTION_COMPLETE_STATES,
        isLive=eng.status in INSPECTION_LIVE_STATES,
        plannedDate=_as_date(eng.plannedDate),
        actualDate=_as_date(eng.conductedDate),
        leadAuditorId=eng.leadAuditorId,
        scorePct=eng.scorePercent,
        assessedByDimension=dims,
        samplingApproach="FULL",
    )


async def resolve(
    db: AsyncSession, kind: str | None, engagement_id: str | None, *, with_dimensions: bool = True
) -> ResolvedEngagement | None:
    """The dereference. Returns None for an unmaterialised slot — that is the
    normal case for a `PLANNED` slot, not an error."""
    if not kind or not engagement_id:
        return None
    kind = kind.upper()
    if kind == "AUDIT":
        audit = await db.get(ComplianceAudit, engagement_id)
        if audit is None or audit.isDeleted:
            return None
        return await resolve_audit(db, audit, with_dimensions=with_dimensions)
    if kind == "INSPECTION":
        eng = await db.get(CamsEngagement, engagement_id)
        if eng is None or eng.isDeleted:
            return None
        return await resolve_inspection(db, eng, with_dimensions=with_dimensions)
    return None


async def resolve_many(
    db: AsyncSession, refs: Iterable[tuple[str | None, str | None]], *, with_dimensions: bool = True
) -> dict[tuple[str, str], ResolvedEngagement]:
    out: dict[tuple[str, str], ResolvedEngagement] = {}
    for kind, eid in refs:
        if not kind or not eid:
            continue
        r = await resolve(db, kind, eid, with_dimensions=with_dimensions)
        if r is not None:
            out[(kind.upper(), eid)] = r
    return out


__all__ = [
    "ResolvedEngagement",
    "resolve",
    "resolve_many",
    "resolve_audit",
    "resolve_inspection",
    "AUDIT_COMPLETE_STATES",
    "INSPECTION_COMPLETE_STATES",
]
