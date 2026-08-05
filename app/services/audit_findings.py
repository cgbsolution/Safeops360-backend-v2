"""WP-19 - Finding first-class on the audit side.

docs/cams/04-target.md §1 decision 1: "Promoting it is the single
highest-leverage schema change in this document."

**The problem.** An audit finding is an implicit property of a checkpoint
(`assessmentStatus IN (FAIL, PARTIAL)`). That is why it cannot carry:

  * a due date        -> the unified register shows blank Due on ~40% of rows,
                         structurally, not because anyone forgot (F-40)
  * an owner distinct from the checkpoint's auditee
  * a repeat chain    -> chains cannot span the two engines (F-3)
  * a severity of its own, adjustable on the evidence
  * an OBSERVATION classification -> 375 rows were dropped at the CamsFinding
                         boundary because that enum has no observation value

**Additive, not a migration.** `AuditCheckpointResponse` keeps its verdict
columns and the conduct screen is untouched. A finding row is created ALONGSIDE
when a checkpoint goes adverse. Collapsing the two is WP-18's job; doing it here
would turn a working module into a broken one under time pressure.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.cams import CamsFinding
from app.models.cams_completion import AuditFinding

# Checkpoint criticality -> finding severity. `observation` is the value the
# CamsFinding enum has no home for, which is why 375 real records vanished at
# the boundary; here it is a first-class outcome flagged `observationOnly`.
SEVERITY_FROM_CRITICALITY: dict[str, str] = {
    "critical": "CRITICAL_NC",
    "major": "MAJOR_NC",
    "minor": "MINOR_NC",
    "observation": "OBSERVATION",
}

# Days from finding to due date, by severity. A critical NC with a 90-day due
# date is not a control — the gradient IS the policy.
DEFAULT_DUE_DAYS: dict[str, int] = {
    "CRITICAL_NC": 7,
    "MAJOR_NC": 30,
    "MINOR_NC": 60,
    "OBSERVATION": 90,
}

OPEN_STATUSES = ("OPEN", "CAPA_RAISED", "IN_REMEDIATION", "VERIFICATION")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def severity_for(criticality: str | None) -> str:
    """Checkpoint criticality -> finding severity. Unknown fails UP to major.

    An unmapped criticality that quietly became an observation would understate
    a real problem — the same fail-safe direction the regime mapper uses.
    """
    return SEVERITY_FROM_CRITICALITY.get((criticality or "").lower(), "MAJOR_NC")


def due_date_for(severity: str, *, raised_on: date | None = None) -> date:
    """Severity-graded due date. Pure."""
    base = raised_on or _utcnow().date()
    return base + timedelta(days=DEFAULT_DUE_DAYS.get(severity, 30))


async def next_finding_code(db: AsyncSession, audit_number: str) -> str:
    """`AFN-<auditNumber>-001`.

    Uses MAX(sequence)+1, never COUNT(*)+1 — the count pattern collides with an
    existing code after any soft-delete, which is exactly how the CAPA numbering
    bug reached production.
    """
    prefix = f"AFN-{audit_number}-"
    rows = (
        await db.execute(
            select(AuditFinding.findingCode).where(AuditFinding.findingCode.like(f"{prefix}%"))
        )
    ).scalars().all()
    highest = 0
    for code in rows:
        tail = code.rsplit("-", 1)[-1]
        if tail.isdigit():
            highest = max(highest, int(tail))
    return f"{prefix}{highest + 1:03d}"


async def find_repeat_source(
    db: AsyncSession, *, site_id: str | None, checkpoint_code: str | None,
    clause_ref: str | None, exclude_audit_id: str,
) -> tuple[str | None, str | None]:
    """Has this been found before at this site? Returns (id, kind).

    Checks BOTH engines — the first time a repeat chain can span audits and
    inspections, which is the whole reason `repeatOfKind` exists.

    Matching requires a SHARED IDENTIFIER: the same checkpoint code within the
    audit engine, or the same standard clause across engines. Anything looser
    (same site, same discipline) flags nearly everything and destroys the
    signal — see the comment below.
    """
    if checkpoint_code:
        prior = (
            await db.execute(
                select(AuditFinding)
                .where(
                    AuditFinding.checkpointCode == checkpoint_code,
                    AuditFinding.siteId == site_id,
                    AuditFinding.auditId != exclude_audit_id,
                    AuditFinding.isDeleted.is_(False),
                )
                .order_by(AuditFinding.createdAt.desc())
                .limit(1)
            )
        ).scalars().first()
        if prior is not None:
            return prior.id, "AUDIT"

    # Cross-engine match requires a SHARED IDENTIFIER, not merely a shared site.
    #
    # The first version fell back to "any open CamsFinding at this site", which
    # flagged 63 of 63 backfilled findings as repeats. A repeat-finding marker
    # that fires on almost everything is worse than none: it destroys the signal
    # the Command Centre ranks on, and the diagnosis established there are ~6
    # genuine chains, not 63.
    #
    # `standardClauseRef` is the only identifier the two engines actually share
    # today. Where it is absent, the honest answer is "no chain detected" —
    # cross-engine chaining becomes reliable when WP-18 unifies the models or
    # WP-20 lands the clause catalogue.
    if clause_ref and site_id:
        insp = (
            await db.execute(
                select(CamsFinding)
                .where(
                    CamsFinding.siteId == site_id,
                    CamsFinding.standardClauseRef == clause_ref,
                    CamsFinding.isDeleted.is_(False),
                    CamsFinding.status.in_(OPEN_STATUSES),
                )
                .order_by(CamsFinding.createdAt.desc())
                .limit(1)
            )
        ).scalars().first()
        if insp is not None:
            return insp.id, "INSPECTION"
    return None, None


async def sync_finding_for_checkpoint(
    db: AsyncSession,
    *,
    audit: ComplianceAudit,
    response: AuditCheckpointResponse,
    actor_id: str | None = None,
) -> AuditFinding | None:
    """Create/update/retire the finding row for one checkpoint.

    Idempotent, and reversible: if a checkpoint is re-assessed as compliant its
    finding is soft-deleted rather than orphaned. A finding that outlives the
    verdict it came from is worse than no finding.
    """
    adverse = response.assessmentStatus in ("FAIL", "PARTIAL")
    existing = (
        await db.execute(
            select(AuditFinding).where(
                AuditFinding.checkpointResponseId == response.id,
                AuditFinding.isDeleted.is_(False),
            )
        )
    ).scalars().first()

    if not adverse:
        if existing is not None:
            existing.isDeleted = True
            existing.closedAt = _utcnow()
            await db.flush()
        return None

    severity = severity_for(response.criticality)
    if existing is not None:
        # Severity can be re-derived, but never overwrite a due date or owner an
        # auditor has deliberately set.
        existing.severity = severity
        existing.observationOnly = severity == "OBSERVATION"
        existing.description = response.observation or existing.description
        await db.flush()
        return existing

    repeat_id, repeat_kind = await find_repeat_source(
        db,
        site_id=audit.plantId,
        checkpoint_code=response.checkpointCode,
        clause_ref=response.requirementReference,
        exclude_audit_id=audit.id,
    )

    finding = AuditFinding(
        findingCode=await next_finding_code(db, audit.auditNumber),
        auditId=audit.id,
        checkpointResponseId=response.id,
        checkpointCode=response.checkpointCode,
        siteId=audit.plantId,
        disciplineCode=response.categoryId,
        title=(response.checkpointQuestion or response.checkpointCode or "Finding")[:255],
        description=response.observation or "",
        severity=severity,
        observationOnly=severity == "OBSERVATION",
        standard=response.standard,
        clauseRef=response.requirementReference,
        ownerId=response.assignedOwnerId or response.routedToUserId,
        dueDate=due_date_for(severity),
        status="OPEN",
        isRepeatFinding=repeat_id is not None,
        repeatOfFindingId=repeat_id,
        repeatOfKind=repeat_kind,
        createdById=actor_id,
    )
    db.add(finding)
    await db.flush()
    return finding


async def findings_for_audit(db: AsyncSession, audit_id: str) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(AuditFinding)
            .where(AuditFinding.auditId == audit_id, AuditFinding.isDeleted.is_(False))
            .order_by(AuditFinding.severity, AuditFinding.dueDate)
        )
    ).scalars().all()
    return [_to_dict(f) for f in rows]


def _to_dict(f: AuditFinding) -> dict[str, Any]:
    return {
        "id": f.id,
        "findingCode": f.findingCode,
        "auditId": f.auditId,
        "checkpointCode": f.checkpointCode,
        "siteId": f.siteId,
        "disciplineCode": f.disciplineCode,
        "title": f.title,
        "description": f.description,
        "severity": f.severity,
        "observationOnly": f.observationOnly,
        "standard": f.standard,
        "clauseRef": f.clauseRef,
        "ownerId": f.ownerId,
        # The field whose absence blanked ~40% of the unified register.
        "dueDate": f.dueDate.isoformat() if f.dueDate else None,
        "isOverdue": bool(
            f.dueDate and f.status in OPEN_STATUSES and f.dueDate < _utcnow().date()
        ),
        "status": f.status,
        "capaId": f.capaId,
        "isRepeatFinding": f.isRepeatFinding,
        "repeatOfFindingId": f.repeatOfFindingId,
        "repeatOfKind": f.repeatOfKind,
        "createdAt": f.createdAt.isoformat() if f.createdAt else None,
        "source": "AUDIT",
    }


__all__ = [
    "SEVERITY_FROM_CRITICALITY",
    "DEFAULT_DUE_DAYS",
    "severity_for",
    "due_date_for",
    "next_finding_code",
    "find_repeat_source",
    "sync_finding_for_checkpoint",
    "findings_for_audit",
]
