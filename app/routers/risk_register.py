"""Combined Risk Register (HIRA + EAI) — Phase 2.

Read-side aggregation endpoint that unions HIRA entries and EAI entries
into a single ranked register. The UI tab (`/risk-register`) calls this
to show "All / HIRA / EAI" with consistent shape.

This is intentionally a thin layer — no new schema. All state lives in
the source modules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.eai import EaiEntry, EaiFeatureFlag, EaiStudy
from app.models.hira import HiraEntry, HiraStudy
from app.models.user import User
from app.services.access_scope import build_query_scope

router = APIRouter(prefix="/api/risk-register", tags=["risk-register"])


class CombinedRegisterRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    type: Literal["HIRA", "EAI"]
    moduleNumber: str  # study number
    sequenceNumber: int
    plantId: str
    areaId: str | None
    departmentId: str | None
    activityDescription: str
    initialRiskOrImpactLevel: str
    initialRiskOrImpactScore: int
    residualRiskOrImpactLevel: str | None
    residualRiskOrImpactScore: int | None
    significantOrCritical: bool
    status: str
    lastReviewedAt: datetime | None
    nextReviewDue: datetime | None
    updatedAt: datetime


class CombinedRegisterResponse(BaseModel):
    items: list[CombinedRegisterRow]
    total: int
    hiraTotal: int
    eaiTotal: int


_RISK_RANK = {"LOW": 0, "MODERATE": 1, "MEDIUM": 1, "HIGH": 2, "SIGNIFICANT": 2, "CRITICAL": 3, "MAJOR": 3}


@router.get("/combined", response_model=CombinedRegisterResponse)
async def get_combined_register(
    plantId: str = Query(...),
    type_: Literal["all", "hira", "eai"] = Query("all", alias="type"),
    significantOnly: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    scope = await build_query_scope(db, user.id, "RISK.COMBINED_VIEW")
    if not scope.allows_plant(plantId):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied for this plant")
    items: list[CombinedRegisterRow] = []
    hira_total = 0
    eai_total = 0

    if type_ in ("all", "hira"):
        hira_rows = (
            await db.execute(
                select(HiraEntry, HiraStudy)
                .join(HiraStudy, HiraStudy.id == HiraEntry.studyId)
                .where(HiraStudy.plantId == plantId)
                .where(HiraEntry.isCurrentVersion.is_(True))
            )
        ).all()
        for entry, study in hira_rows:
            if significantOnly and entry.residualRiskLevel not in ("HIGH", "CRITICAL"):
                continue
            items.append(
                CombinedRegisterRow(
                    id=entry.id,
                    type="HIRA",
                    moduleNumber=study.number,
                    sequenceNumber=entry.sequenceNumber,
                    plantId=study.plantId,
                    areaId=entry.areaId,
                    departmentId=study.departmentId,
                    activityDescription=entry.activityDescription,
                    initialRiskOrImpactLevel=entry.initialRiskLevel,
                    initialRiskOrImpactScore=entry.initialRiskScore,
                    residualRiskOrImpactLevel=entry.residualRiskLevel,
                    residualRiskOrImpactScore=entry.residualRiskScore,
                    significantOrCritical=entry.residualRiskLevel in ("HIGH", "CRITICAL"),
                    status=entry.status,
                    lastReviewedAt=entry.lastReviewedAt,
                    nextReviewDue=entry.nextReviewDue,
                    updatedAt=entry.updatedAt,
                )
            )
        hira_total = len(hira_rows)

    # EAI gated by feature flag
    flag = (
        await db.execute(select(EaiFeatureFlag).where(EaiFeatureFlag.plantId == plantId))
    ).scalar_one_or_none()
    eai_enabled = bool(flag and (flag.eaiRegisterEnabled or flag.combinedRegisterEnabled))

    if eai_enabled and type_ in ("all", "eai"):
        eai_rows = (
            await db.execute(
                select(EaiEntry, EaiStudy)
                .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
                .where(EaiStudy.plantId == plantId)
                .where(EaiEntry.isCurrentVersion.is_(True))
            )
        ).all()
        for entry, study in eai_rows:
            if significantOnly and not entry.residualSignificant:
                continue
            items.append(
                CombinedRegisterRow(
                    id=entry.id,
                    type="EAI",
                    moduleNumber=study.number,
                    sequenceNumber=entry.sequenceNumber,
                    plantId=study.plantId,
                    areaId=entry.areaId,
                    departmentId=study.departmentId,
                    activityDescription=entry.activityDescription,
                    initialRiskOrImpactLevel=entry.initialImpactLevel,
                    initialRiskOrImpactScore=entry.initialImpactScore,
                    residualRiskOrImpactLevel=entry.residualImpactLevel,
                    residualRiskOrImpactScore=entry.residualImpactScore,
                    significantOrCritical=entry.residualSignificant,
                    status=entry.status,
                    lastReviewedAt=entry.lastReviewedAt,
                    nextReviewDue=entry.nextReviewDue,
                    updatedAt=entry.updatedAt,
                )
            )
        eai_total = len(eai_rows)

    # Sort by residual rank descending, then initial rank, then updatedAt desc
    def sort_key(row: CombinedRegisterRow) -> tuple[int, int, datetime]:
        return (
            -_RISK_RANK.get((row.residualRiskOrImpactLevel or "").upper(), 0),
            -_RISK_RANK.get(row.initialRiskOrImpactLevel.upper(), 0),
            row.updatedAt,
        )

    items.sort(key=sort_key)

    return CombinedRegisterResponse(
        items=items,
        total=len(items),
        hiraTotal=hira_total,
        eaiTotal=eai_total,
    )
