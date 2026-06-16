"""EPC Site Induction router. Mounts at /api/epc/inductions.

Records site-specific safety inductions. Every worker must complete a site
induction before mobilization can be approved. Inductions expire (configurable,
typically 12 months) and must be renewed. The gate clearance check queries
SiteInduction validity in real-time.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.epc import SiteInduction, MobilizationRecord, ContractorWorker, ConstructionSite
from app.models.user import User
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/epc/inductions", tags=["epc-inductions"])


# ─── RBAC helper ─────────────────────────────────────────────────────────────


async def _require(db: AsyncSession, user: User, permission_code: str) -> None:
    result = await can(db, user.id, permission_code, PermissionContext())
    if not result.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            result.reason or f"Missing permission {permission_code}",
        )


# ─── Request bodies ───────────────────────────────────────────────────────────


class InductionCreate(BaseModel):
    contractorWorkerId: str
    siteId: str
    mobilizationRecordId: str
    inductionType: str = "full_site_induction"
    topicsCovered: list = []
    clientRequirementsCovered: bool = False
    siteEmergencyProceduresCovered: bool = False
    siteLayoutFamiliarization: bool = False
    musterPointIdentified: bool = False
    ppeCoveredBool: bool = False
    ptwSystemExplained: bool = False
    incidentReportingExplained: bool = False
    conductedAt: datetime | None = None
    durationMinutes: int = 60
    inductionLanguage: str = "Hindi"
    interpreterUsed: bool = False
    interpreterName: str | None = None
    assessmentConducted: bool = False
    assessmentScore: float | None = None
    assessmentPassScore: float = 70.0
    workerAcknowledged: bool = False
    workerAcknowledgementMethod: str | None = None
    validityMonths: int = 12  # how long the induction is valid (from conducted date)
    inductionPhotoUrl: str | None = None


class InductionUpdate(BaseModel):
    assessmentScore: float | None = None
    assessmentPassed: bool | None = None
    workerAcknowledged: bool | None = None
    workerAcknowledgementMethod: str | None = None
    workerAcknowledgedAt: datetime | None = None
    acknowledgementUrl: str | None = None
    inductionPhotoUrl: str | None = None
    reInductionRequired: bool | None = None
    reInductionDate: datetime | None = None


# ─── Serialization helper ─────────────────────────────────────────────────────


def _induction_dict(ind: SiteInduction) -> dict:
    return {
        "id": ind.id,
        "contractorWorkerId": ind.contractorWorkerId,
        "siteId": ind.siteId,
        "mobilizationRecordId": ind.mobilizationRecordId,
        "inductionType": ind.inductionType,
        "topicsCovered": ind.topicsCovered or [],
        "clientRequirementsCovered": ind.clientRequirementsCovered,
        "siteEmergencyProceduresCovered": ind.siteEmergencyProceduresCovered,
        "siteLayoutFamiliarization": ind.siteLayoutFamiliarization,
        "musterPointIdentified": ind.musterPointIdentified,
        "ppeCoveredBool": ind.ppeCoveredBool,
        "ptwSystemExplained": ind.ptwSystemExplained,
        "incidentReportingExplained": ind.incidentReportingExplained,
        "conductedById": ind.conductedById,
        "conductedAt": ind.conductedAt.isoformat() if ind.conductedAt else None,
        "durationMinutes": ind.durationMinutes,
        "inductionLanguage": ind.inductionLanguage,
        "interpreterUsed": ind.interpreterUsed,
        "interpreterName": ind.interpreterName,
        "assessmentConducted": ind.assessmentConducted,
        "assessmentScore": ind.assessmentScore,
        "assessmentPassScore": ind.assessmentPassScore,
        "assessmentPassed": ind.assessmentPassed,
        "failedTopics": ind.failedTopics or [],
        "reInductionRequired": ind.reInductionRequired,
        "reInductionDate": ind.reInductionDate.isoformat() if ind.reInductionDate else None,
        "workerAcknowledged": ind.workerAcknowledged,
        "workerAcknowledgementMethod": ind.workerAcknowledgementMethod,
        "workerAcknowledgedAt": ind.workerAcknowledgedAt.isoformat() if ind.workerAcknowledgedAt else None,
        "inductionPhotoUrl": ind.inductionPhotoUrl,
        "validFrom": ind.validFrom.isoformat() if ind.validFrom else None,
        "validUntil": ind.validUntil.isoformat() if ind.validUntil else None,
        "isExpired": ind.isExpired,
        "createdAt": ind.createdAt.isoformat() if ind.createdAt else None,
    }


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get("")
async def list_inductions(
    siteId: str | None = Query(None),
    contractorWorkerId: str | None = Query(None),
    mobilizationRecordId: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List inductions. Filter by siteId, contractorWorkerId, or mobilizationRecordId."""
    await _require(db, user, "EPC.READ")

    q = select(SiteInduction)
    if siteId:
        q = q.where(SiteInduction.siteId == siteId)
    if contractorWorkerId:
        q = q.where(SiteInduction.contractorWorkerId == contractorWorkerId)
    if mobilizationRecordId:
        q = q.where(SiteInduction.mobilizationRecordId == mobilizationRecordId)

    q = q.order_by(SiteInduction.createdAt.desc())
    inductions = (await db.execute(q)).scalars().all()

    return {"inductions": [_induction_dict(ind) for ind in inductions], "total": len(inductions)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_induction(
    body: InductionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Record a new site induction. Updates linked MobilizationRecord after creation."""
    await _require(db, user, "EPC.CREATE")

    now = datetime.now(timezone.utc)
    conducted_at = body.conductedAt or now
    valid_from = conducted_at
    valid_until = conducted_at + timedelta(days=body.validityMonths * 30)

    assessment_passed: bool | None = None
    if body.assessmentConducted and body.assessmentScore is not None:
        assessment_passed = body.assessmentScore >= body.assessmentPassScore

    is_expired = valid_until < now

    induction = SiteInduction(
        contractorWorkerId=body.contractorWorkerId,
        siteId=body.siteId,
        mobilizationRecordId=body.mobilizationRecordId,
        inductionType=body.inductionType,
        topicsCovered=body.topicsCovered,
        clientRequirementsCovered=body.clientRequirementsCovered,
        siteEmergencyProceduresCovered=body.siteEmergencyProceduresCovered,
        siteLayoutFamiliarization=body.siteLayoutFamiliarization,
        musterPointIdentified=body.musterPointIdentified,
        ppeCoveredBool=body.ppeCoveredBool,
        ptwSystemExplained=body.ptwSystemExplained,
        incidentReportingExplained=body.incidentReportingExplained,
        conductedById=user.id,
        conductedAt=conducted_at,
        durationMinutes=body.durationMinutes,
        inductionLanguage=body.inductionLanguage,
        interpreterUsed=body.interpreterUsed,
        interpreterName=body.interpreterName,
        assessmentConducted=body.assessmentConducted,
        assessmentScore=body.assessmentScore,
        assessmentPassScore=body.assessmentPassScore,
        assessmentPassed=assessment_passed,
        workerAcknowledged=body.workerAcknowledged,
        workerAcknowledgementMethod=body.workerAcknowledgementMethod,
        inductionPhotoUrl=body.inductionPhotoUrl,
        validFrom=valid_from,
        validUntil=valid_until,
        isExpired=is_expired,
    )
    db.add(induction)
    await db.flush()  # get induction.id

    # Update linked MobilizationRecord preMobilisationChecks
    mob = await db.get(MobilizationRecord, body.mobilizationRecordId)
    if mob is not None:
        checks = dict(mob.preMobilisationChecks or {})
        checks["site_induction_complete"] = True
        checks["site_induction_id"] = induction.id
        mob.preMobilisationChecks = checks

    await db.commit()
    await db.refresh(induction)

    return _induction_dict(induction)


@router.get("/site/{site_id}/compliance")
async def site_compliance(
    site_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Induction compliance summary for a construction site."""
    await _require(db, user, "EPC.READ")

    now = datetime.now(timezone.utc)
    soon_threshold = now + timedelta(days=30)

    # All active mobilizations for this site
    active_mobs = (
        await db.execute(
            select(MobilizationRecord).where(
                MobilizationRecord.siteId == site_id,
                MobilizationRecord.status == "active",
            )
        )
    ).scalars().all()

    total_workers = len(active_mobs)
    inducted = 0
    expired = 0
    never_inducted = 0
    expiring_soon: list[dict] = []

    for mob in active_mobs:
        # Find the most recent induction for this worker at this site
        latest = (
            await db.execute(
                select(SiteInduction)
                .where(
                    SiteInduction.contractorWorkerId == mob.contractorWorkerId,
                    SiteInduction.siteId == site_id,
                )
                .order_by(SiteInduction.conductedAt.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if latest is None:
            never_inducted += 1
        elif latest.validUntil is None:
            never_inducted += 1
        else:
            valid_until = latest.validUntil
            if valid_until.tzinfo is None:
                valid_until = valid_until.replace(tzinfo=timezone.utc)

            if valid_until < now:
                expired += 1
            else:
                inducted += 1
                if valid_until <= soon_threshold:
                    expiring_soon.append({
                        "contractorWorkerId": mob.contractorWorkerId,
                        "validUntil": valid_until.isoformat(),
                    })

    compliance_pct = round((inducted / total_workers * 100), 1) if total_workers > 0 else 0.0

    return {
        "total_workers": total_workers,
        "inducted": inducted,
        "expired": expired,
        "never_inducted": never_inducted,
        "compliance_pct": compliance_pct,
        "expiring_soon": expiring_soon,
    }


@router.get("/{induction_id}")
async def get_induction(
    induction_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get a single induction by ID."""
    await _require(db, user, "EPC.READ")

    induction = await db.get(SiteInduction, induction_id)
    if induction is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Induction not found")

    return _induction_dict(induction)


@router.patch("/{induction_id}")
async def update_induction(
    induction_id: str,
    body: InductionUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Patch an induction — record acknowledgement, assessment score, etc."""
    await _require(db, user, "EPC.UPDATE")

    induction = await db.get(SiteInduction, induction_id)
    if induction is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Induction not found")

    if body.assessmentScore is not None:
        induction.assessmentScore = body.assessmentScore
    if body.assessmentPassed is not None:
        induction.assessmentPassed = body.assessmentPassed
    if body.workerAcknowledged is not None:
        induction.workerAcknowledged = body.workerAcknowledged
    if body.workerAcknowledgementMethod is not None:
        induction.workerAcknowledgementMethod = body.workerAcknowledgementMethod
    if body.workerAcknowledgedAt is not None:
        induction.workerAcknowledgedAt = body.workerAcknowledgedAt
    if body.acknowledgementUrl is not None:
        induction.inductionPhotoUrl = body.acknowledgementUrl  # stored in inductionPhotoUrl when used as ack URL
    if body.inductionPhotoUrl is not None:
        induction.inductionPhotoUrl = body.inductionPhotoUrl
    if body.reInductionRequired is not None:
        induction.reInductionRequired = body.reInductionRequired
    if body.reInductionDate is not None:
        induction.reInductionDate = body.reInductionDate

    await db.commit()
    await db.refresh(induction)

    return _induction_dict(induction)
