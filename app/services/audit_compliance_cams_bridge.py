"""Bridge: mirror green Audit & Compliance audits into the shared CAMS engine.

The green flow (app/services/audit_compliance.py) is the source of truth for
audit conduct + the new auditor->plant-head->auditee workflow. To make the CAMS
dashboards (Command Centre, Compliance Tracker, Calendar, Analytics, Board Pack)
reflect these audits with zero reader changes, each green audit is mirrored into
a CamsEngagement (sourceModule="AUDIT_COMPLIANCE") with one CamsFinding per
failed/partial checkpoint — using the same consumer-engine pattern other modules
use (svc.create_consumer_engagement, §8 "one engine").

Mirroring is best-effort: wrapped in a SAVEPOINT so a CAMS hiccup never blocks
the green audit. Sync is one-directional (green -> CAMS); the green audit owns
the data.
"""

from __future__ import annotations

import sys
import traceback
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.cams import CamsEngagement, CamsFinding
from app.services import cams as cams_svc

# Green checkpoint criticality -> CAMS finding severity.
_SEVERITY = {
    "critical": "CRITICAL_NC",
    "major": "MAJOR_NC",
    "minor": "MINOR_NC",
    "informational": "OBSERVATION",
}

# Green audit status -> CamsEngagement status.
_ENGAGEMENT_STATUS = {
    "scheduled": "SCHEDULED",
    "in_progress": "IN_PROGRESS",
    "pending_plant_head": "FIELDWORK_COMPLETE",
    "auditee_response": "FINDINGS_REVIEW",
    "auditor_review": "FINDINGS_REVIEW",
    "pending_acceptance": "REPORT_ISSUED",
    "closed": "CLOSED",
    "cancelled": "CANCELLED",
}

_FINDING_VALUES = ("fail", "partial")


def _is_finding(r: AuditCheckpointResponse) -> bool:
    val = (r.auditorResponse or {}).get("value") if r.auditorResponse else None
    return val in _FINDING_VALUES


async def mirror_on_submit(
    db: AsyncSession, *, audit: ComplianceAudit, user: Any
) -> dict[str, str]:
    """Create the mirror CamsEngagement + one CamsFinding per failed/partial
    checkpoint. Returns {AuditCheckpointResponse.id: CamsFinding.id} so the
    caller can link auto-CAPA to the finding. Returns {} on any failure (the
    caller then falls back to the checkpoint id for CAPA provenance)."""
    if audit.camsEngagementId:
        return {}  # already mirrored
    finding_map: dict[str, str] = {}
    try:
        async with db.begin_nested():
            engagement = await cams_svc.create_consumer_engagement(
                db,
                source_module="AUDIT_COMPLIANCE",
                title=f"{audit.auditNumber} — {audit.title}",
                engagement_type="AUDIT_INTERNAL",
                lead_auditor_id=audit.leadAuditorUserId,
                planned_date=audit.scheduledDate,
                site_id=audit.plantId,
                area_or_asset_ref=(audit.scopeAreas or [None])[0] if audit.scopeAreas else None,
                scope_statement=audit.scopeDescription or audit.title,
                actor_id=user.id,
            )
            engagement.status = _ENGAGEMENT_STATUS.get(audit.status, "FIELDWORK_COMPLETE")
            engagement.conductedDate = audit.actualEndAt or audit.scheduledDate
            engagement.scorePercent = audit.overallCompliancePct
            audit.camsEngagementId = engagement.id

            for r in audit.responses:
                if not _is_finding(r):
                    continue
                code = await cams_svc.next_finding_code(db)
                obs = (r.auditorResponse or {}).get("text_observation", "")
                f = CamsFinding(
                    findingCode=code,
                    engagementId=engagement.id,
                    sourceQuestionId=r.id,  # stable link back to the checkpoint
                    title=(r.checkpointQuestion or "Non-conformance")[:200],
                    description=obs or r.checkpointQuestion or "",
                    severity=_SEVERITY.get(r.criticality, "MINOR_NC"),
                    standardClauseRef=r.requirementReference or None,
                    siteId=audit.plantId,
                    areaOrAssetRef=r.categoryName,
                    ownerId=r.routedToUserId,
                    status="OPEN",
                    createdBy=user.id,
                )
                db.add(f)
                await db.flush()
                finding_map[r.id] = f.id
        return finding_map
    except Exception as e:  # noqa: BLE001
        print(f"CAMS mirror_on_submit failed for {audit.auditNumber}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return {}


async def sync_status(db: AsyncSession, *, audit: ComplianceAudit) -> None:
    """Reconcile the mirror engagement's status + each finding's status from the
    current green audit / checkpoint state. Idempotent; best-effort."""
    if not audit.camsEngagementId:
        return
    try:
        async with db.begin_nested():
            engagement = await db.get(CamsEngagement, audit.camsEngagementId)
            if engagement is None:
                return
            engagement.status = _ENGAGEMENT_STATUS.get(audit.status, engagement.status)
            engagement.scorePercent = audit.overallCompliancePct
            if audit.status == "closed":
                engagement.overallResult = "PASS" if audit.auditPassed else "FAIL"
                engagement.conductedDate = engagement.conductedDate or audit.actualEndAt

            findings = (
                await db.execute(select(CamsFinding).where(CamsFinding.engagementId == engagement.id))
            ).scalars().all()
            by_checkpoint = {f.sourceQuestionId: f for f in findings if f.sourceQuestionId}
            for r in audit.responses:
                f = by_checkpoint.get(r.id)
                if f is None:
                    continue
                f.ownerId = r.routedToUserId or f.ownerId
                if r.overallStatus == "response_accepted":
                    if f.status != "CLOSED":
                        f.status = "CLOSED"
                        f.closedAt = engagement.updatedAt
                else:
                    f.status = "OPEN"
    except Exception as e:  # noqa: BLE001
        print(f"CAMS sync_status failed for {audit.auditNumber}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
