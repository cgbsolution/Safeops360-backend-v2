"""CAPA context builder for the agent platform.

Assembles the input context for the CAPA Assistant. The assistant
serves three task types (suggest_root_causes / suggest_actions /
suggest_verification), but the route doesn't know which yet — the
caller passes `taskType` as part of the invocation context override
(future), and for now this builder ships the full CAPA payload so
the same context is reusable across all three task types.

Builds a payload with the CAPA record, its existing root causes /
actions / contributors / linkages, the verification method library
(so suggest_verification can ground its method picks), and the small
set of recently-closed CAPAs at the same plant + source type the
assistant can sanity-check against.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capa import (
    Capa,
    CapaAction,
    CapaContributor,
    CapaLinkage,
    CapaRootCause,
    CapaSourceCategory,
    CapaSourceType,
    CapaVerificationMethod,
)


async def build_context(db: AsyncSession, record_id: str) -> dict[str, Any]:
    capa = await db.get(Capa, record_id)
    if capa is None:
        return {
            "sourceModule": "CAPA",
            "sourceRecordId": record_id,
            "_note": "CAPA record not found",
        }

    source_category = (
        await db.get(CapaSourceCategory, capa.sourceCategoryId)
        if capa.sourceCategoryId
        else None
    )
    source_type = (
        await db.get(CapaSourceType, capa.sourceTypeId) if capa.sourceTypeId else None
    )

    root_causes = (
        await db.execute(
            select(CapaRootCause)
            .where(CapaRootCause.capaId == capa.id)
            .order_by(CapaRootCause.sortOrder.asc())
        )
    ).scalars().all()

    actions = (
        await db.execute(
            select(CapaAction)
            .where(CapaAction.capaId == capa.id)
            .order_by(CapaAction.sortOrder.asc(), CapaAction.dueDate.asc())
        )
    ).scalars().all()

    contributors = (
        await db.execute(select(CapaContributor).where(CapaContributor.capaId == capa.id))
    ).scalars().all()

    linkages = (
        await db.execute(
            select(CapaLinkage).where(
                (CapaLinkage.fromCapaId == capa.id) | (CapaLinkage.toCapaId == capa.id)
            )
        )
    ).scalars().all()

    verification_methods = (
        await db.execute(
            select(CapaVerificationMethod)
            .where(CapaVerificationMethod.isActive.is_(True))
            .order_by(CapaVerificationMethod.sortOrder.asc())
        )
    ).scalars().all()

    # Recently closed CAPAs at same plant + source type — small N, for grounding
    similar_closed = (
        await db.execute(
            select(Capa)
            .where(Capa.plantId == capa.plantId)
            .where(Capa.sourceTypeCode == capa.sourceTypeCode)
            .where(Capa.id != capa.id)
            .where(Capa.state.in_(["VERIFIED", "CLOSED"]))
            .order_by(Capa.closedAt.desc().nullslast())
            .limit(5)
        )
    ).scalars().all()

    return {
        "sourceModule": "CAPA",
        "sourceRecordId": capa.id,
        "capa": {
            "id": capa.id,
            "capaNumber": capa.capaNumber,
            "title": capa.title,
            "plantId": capa.plantId,
            "sourceCategory": source_category.code if source_category else None,
            "sourceCategoryName": source_category.name if source_category else None,
            "sourceTypeCode": capa.sourceTypeCode,
            "sourceTypeName": source_type.name if source_type else None,
            "sourceReferenceId": capa.sourceReferenceId,
            "sourceReferenceSummary": capa.sourceReferenceSummary,
            "sourceMetadata": capa.sourceMetadata,
            "problemDescription": capa.problemDescription,
            "problemImpact": capa.problemImpact,
            "detectionMethod": capa.detectionMethod,
            "detectedAt": capa.detectedAt.isoformat() if capa.detectedAt else None,
            "affectedAreas": capa.affectedAreas,
            "affectedDepartments": capa.affectedDepartments,
            "affectedProducts": capa.affectedProducts,
            "affectedProcesses": capa.affectedProcesses,
            "primaryCategory": capa.primaryCategory,
            "actionType": capa.actionType,
            "severity": capa.severity,
            "priority": capa.priority,
            "isRecurring": capa.isRecurring,
            "rcaMethodology": capa.rcaMethodology,
            "rcaSummary": capa.rcaSummary,
            "rcaCompleted": capa.rcaCompleted,
            "contributingFactors": capa.contributingFactors,
            "verificationSuccessCriteria": capa.verificationSuccessCriteria,
            "measurementPeriodDays": capa.measurementPeriodDays,
            "verificationResult": capa.verificationResult,
            "state": capa.state,
            "closureTargetDate": capa.closureTargetDate.isoformat()
            if capa.closureTargetDate
            else None,
        },
        "existingRootCauses": [
            {
                "id": rc.id,
                "category": rc.category,
                "description": rc.description,
                "confidence": rc.confidence,
            }
            for rc in root_causes
        ],
        "existingActions": [
            {
                "id": a.id,
                "actionType": a.actionType,
                "description": a.description,
                "ownerUserId": a.ownerUserId,
                "ownerRole": a.ownerRole,
                "dueDate": a.dueDate.isoformat() if a.dueDate else None,
                "status": a.status,
                "evidenceOfCompletion": a.evidenceOfCompletion,
                "completedAt": a.completedAt.isoformat() if a.completedAt else None,
            }
            for a in actions
        ],
        "contributors": [
            {
                "userId": c.userId,
                "role": c.role,
                "contributionType": c.contributionType,
            }
            for c in contributors
        ],
        "linkages": [
            {
                "fromCapaId": link.fromCapaId,
                "toCapaId": link.toCapaId,
                "linkageType": link.linkageType,
                "rationale": link.rationale,
            }
            for link in linkages
        ],
        "availableVerificationMethods": [
            {
                "id": m.id,
                "code": m.code,
                "name": m.name,
                "description": m.description,
            }
            for m in verification_methods
        ],
        "similarClosedCapas": [
            {
                "id": c.id,
                "capaNumber": c.capaNumber,
                "title": c.title,
                "severity": c.severity,
                "rcaSummary": c.rcaSummary,
                "verificationResult": c.verificationResult,
                "closedAt": c.closedAt.isoformat() if c.closedAt else None,
            }
            for c in similar_closed
        ],
    }
