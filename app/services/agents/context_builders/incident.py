"""Rich context builder for INCIDENT-module agent invocations.

Replaces the generic record-fetch stub in agent_service._build_context.
Returns a dict shaped to give the RCA assistant the full investigation
picture: who/what/when/where, equipment involved, witnesses, timeline
events captured so far, evidence collected, documents reviewed, the
active permit at the time (if any), and the source near miss (if the
incident was promoted from one).

Several Incident child tables are declared as SQLAlchemy classes but
DON'T have a back-relationship on the Incident model yet — so we query
them directly by incidentId. That's cheap (each child table has an
incidentId index) and avoids touching the parent model.

Output size: kept tight by previewing long text to ~300 chars per
field. The agent's tool calls can pull more detail if it needs it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.equipment import Equipment
from app.models.incident import (
    Incident,
    IncidentCapa,
    IncidentDocumentReview,
    IncidentEquipment,
    IncidentEvidence,
    IncidentPerson,
    IncidentTimelineEvent,
    IncidentWitnessStatement,
)
from app.models.masters import Department
from app.models.epc import ContractorCompany
from app.models.near_miss import NearMiss
from app.models.permit import Permit
from app.models.plant import Area, Plant


async def build_context(db: AsyncSession, incident_id: str) -> dict[str, Any]:
    """Assemble the rich RCA-input context for an incident."""
    stmt = (
        select(Incident)
        .where(Incident.id == incident_id)
        .options(
            selectinload(Incident.personsInvolved),
            selectinload(Incident.witnessStatements),
            selectinload(Incident.equipmentInvolved),
        )
    )
    incident = (await db.execute(stmt)).scalar_one_or_none()
    if incident is None:
        raise ValueError(f"Incident {incident_id!r} not found")

    # ── FK targets ───────────────────────────────────────────────────
    plant = await db.get(Plant, incident.plantId) if incident.plantId else None
    area = await db.get(Area, incident.areaId) if incident.areaId else None
    department = (
        await db.get(Department, incident.departmentId)
        if incident.departmentId
        else None
    )
    active_permit = (
        await db.get(Permit, incident.activePermitId)
        if incident.activePermitId
        else None
    )
    source_near_miss = (
        await db.get(NearMiss, incident.sourceNearMissId)
        if incident.sourceNearMissId
        else None
    )

    # ── Child tables not wired as relationships ──────────────────────
    timeline = (
        (
            await db.execute(
                select(IncidentTimelineEvent)
                .where(IncidentTimelineEvent.incidentId == incident_id)
                .order_by(IncidentTimelineEvent.sequence.asc())
            )
        )
        .scalars()
        .all()
    )
    evidence = (
        (
            await db.execute(
                select(IncidentEvidence).where(IncidentEvidence.incidentId == incident_id)
            )
        )
        .scalars()
        .all()
    )
    documents = (
        (
            await db.execute(
                select(IncidentDocumentReview).where(
                    IncidentDocumentReview.incidentId == incident_id
                )
            )
        )
        .scalars()
        .all()
    )
    capas = (
        (
            await db.execute(
                select(IncidentCapa).where(IncidentCapa.incidentId == incident_id)
            )
        )
        .scalars()
        .all()
    )

    # ── Resolve equipment names for the equipmentInvolved join rows ──
    eq_ids = [ie.equipmentId for ie in incident.equipmentInvolved]
    equipment_by_id: dict[str, Equipment] = {}
    if eq_ids:
        rows = (
            (await db.execute(select(Equipment).where(Equipment.id.in_(eq_ids))))
            .scalars()
            .all()
        )
        equipment_by_id = {e.id: e for e in rows}

    # ── Contractor company names for persons ────────────────────────
    contractor_ids = {
        p.contractorCompanyId for p in incident.personsInvolved if p.contractorCompanyId
    }
    contractor_by_id: dict[str, str] = {}
    if contractor_ids:
        contractor_by_id = dict(
            (
                await db.execute(
                    select(ContractorCompany.id, ContractorCompany.name).where(
                        ContractorCompany.id.in_(contractor_ids)
                    )
                )
            ).all()
        )

    occurred = incident.occurredAt or incident.date
    return {
        "sourceModule": "INCIDENT",
        "incidentNumber": incident.number,
        "incidentId": incident.id,
        "type": _enum(incident.type),
        "severity": incident.severity,
        "isReportable": incident.isReportable,
        "reportableUnder": incident.reportableUnder,
        "status": _enum(incident.status),
        "when": {
            "occurredAt": _iso(occurred),
            "reportedAt": _iso(incident.reportedAt),
            "reportingDelayMinutes": incident.reportingDelayMinutes,
        },
        "where": {
            "plantId": incident.plantId,
            "plantName": plant.name if plant else None,
            "plantType": getattr(plant, "type", None) if plant else None,
            "areaLabel": getattr(area, "label", None) if area else None,
            "departmentName": getattr(department, "name", None) if department else None,
            "location": incident.location,
            "specificLocation": incident.specificLocation,
            "gpsLatitude": incident.gpsLatitude,
            "gpsLongitude": incident.gpsLongitude,
            "weatherConditions": incident.weatherConditions,
        },
        "description": (
            incident.initialDescription or incident.description or ""
        ),
        "immediateAction": incident.immediateAction,
        "activity": {
            "what": incident.activityBeingPerformed,
            "isRoutine": incident.activityIsRoutine,
        },
        "persons": [
            {
                "role": p.role,
                "isContractor": p.isContractor,
                "contractorCompany": contractor_by_id.get(p.contractorCompanyId or ""),
                "externalName": p.externalName,
                "userId": p.userId,
                "isInjured": p.isInjured,
                "bodyPartAffected": p.bodyPartAffected,
                "natureOfInjury": p.natureOfInjury,
                "injurySeverity": p.injurySeverity,
                "daysOff": p.daysOff,
                "daysRestricted": p.daysRestricted,
                "ppeWornAtTime": p.ppeWornAtTime,
            }
            for p in incident.personsInvolved
        ],
        "equipmentInvolved": [
            {
                "equipmentId": ie.equipmentId,
                "name": _eq_attr(equipment_by_id.get(ie.equipmentId), "name"),
                "code": _eq_attr(equipment_by_id.get(ie.equipmentId), "code"),
                "category": _eq_attr(equipment_by_id.get(ie.equipmentId), "category"),
                "criticality": _eq_attr(
                    equipment_by_id.get(ie.equipmentId), "criticality"
                ),
                "involvement": ie.involvement,
                "damageEstimate": (
                    float(ie.damageEstimate) if ie.damageEstimate is not None else None
                ),
                "repairStatus": ie.repairStatus,
            }
            for ie in incident.equipmentInvolved
        ],
        "witnesses": [
            {
                "witnessName": w.witnessName,
                "witnessRole": w.witnessRole,
                "userId": w.witnessUserId,
                "statementPreview": (w.statementText or "")[:300] if w.statementText else None,
                "takenAt": _iso(w.takenAt),
                "language": w.language,
            }
            for w in incident.witnessStatements
        ],
        "timeline": [
            {
                "sequence": t.sequence,
                "timestamp": _iso(t.timestamp),
                "description": (t.description or "")[:300],
                "source": t.source,
            }
            for t in timeline
        ],
        "evidence": [
            {
                "category": e.category,
                "title": e.title,
                "descriptionPreview": (e.description or "")[:200] if e.description else None,
                "collectedAt": _iso(e.collectedAt),
                "preservedFor": e.preservedFor,
            }
            for e in evidence
        ],
        "documentsReviewed": [
            {
                "documentType": d.documentType,
                "documentReference": d.documentReference,
                "complianceFinding": d.complianceFinding,
                "reviewNotesPreview": (d.reviewNotes or "")[:240] if d.reviewNotes else None,
            }
            for d in documents
        ],
        "existingCausesCapturedSoFar": {
            "immediateCauses": incident.immediateCauses or [],
            "underlyingCauses": incident.underlyingCauses or [],
            "rootCauses": incident.rootCauses or [],
            "contributingFactors": incident.contributingFactors or [],
        },
        "existingRcaSoFar": {
            "method": incident.rootCauseMethod,
            "summary": incident.rootCauseSummary,
            # NB: the full rootCauseData JSON is intentionally excluded
            # from context to avoid the agent anchoring on a partial
            # draft. Only the summary + the cause hierarchy is shown.
        },
        "existingCapas": [
            {
                "capaNumber": c.capaNumber,
                "description": (c.description or "")[:240],
                "type": c.type,
                "status": c.status,
                "rootCauseAddressed": c.rootCauseAddressed,
            }
            for c in capas
        ],
        "permitContext": (
            {
                "permitNumber": active_permit.number,
                "type": _enum(active_permit.type),
                "status": _enum(active_permit.status),
                "scopeOfWorkPreview": (active_permit.scopeOfWork or "")[:300],
                "isolationsRequiredPreview": (
                    (active_permit.isolationsRequired or "")[:200]
                    if active_permit.isolationsRequired
                    else None
                ),
                "gasTestRequired": active_permit.gasTestRequired,
                "gasTestResult": active_permit.gasTestResult,
                "fireWatchRequired": active_permit.fireWatchRequired,
            }
            if active_permit
            else None
        ),
        "sourceNearMiss": (
            {
                "nearMissNumber": source_near_miss.number,
                "date": _iso(source_near_miss.date),
                "potentialSeverity": _enum(source_near_miss.potentialSeverity),
                "hazardCategory": source_near_miss.hazardCategory,
                "descriptionPreview": (source_near_miss.description or "")[:300],
            }
            if source_near_miss
            else None
        ),
    }


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


def _enum(v: Any) -> str | None:
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)


def _eq_attr(equipment: Equipment | None, attr: str) -> Any:
    """Safely pull an attribute off an Equipment row that may be None
    (when the equipmentId on the IncidentEquipment row points at a
    decommissioned record we couldn't load — defensive)."""
    return getattr(equipment, attr, None) if equipment is not None else None
