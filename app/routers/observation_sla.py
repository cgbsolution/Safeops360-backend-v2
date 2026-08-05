"""Admin API for the Observation SLA matrix + deroster review config.

Route shape differs from the build spec deliberately. The spec specified
`/api/tenants/:tenantId/observation-sla-config`; there is no Tenant table in
this schema — plants are the scoping unit, with a global (`plantId IS NULL`)
default underneath, exactly as `TrainingRuleConfig` does it. So the scope is a
`plantId` query parameter and omitting it addresses the global default.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.observation import ObservationTaxonomy, ObservationType, Severity
from app.models.observation_sla import (
    AXIS_ANY,
    CATEGORY_GROUP_PENDING,
    CATEGORY_GROUP_VALUES,
    CATEGORY_GROUPS,
    DEFAULT_REVIEW_SLA_HOURS,
    ObservationCategoryGroup,
    ObservationDerosterConfig,
    ObservationSlaConfig,
)
from app.models.user import User
from app.schemas.observation_sla import (
    CategoryGroupOut,
    CategoryGroupUpsert,
    DerosterConfigOut,
    SlaConfigOut,
    SlaConfigUpsert,
    SlaPreviewOut,
    SlaRowOut,
)
from app.services import observation_sla as sla

router = APIRouter(prefix="/api/observations/sla-config", tags=["observations"])

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")


@router.get("", response_model=SlaConfigOut)
async def get_sla_config(
    plantId: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SlaConfigOut:
    """The effective matrix for a scope: the plant's own rows where they exist,
    the global default elsewhere. `inherited` marks which is which so the admin
    table can show what a plant is actually operating under rather than an
    empty grid."""
    await require_permission_with_context("OBSERVATION.READ", user, db, plant_id=plantId)

    rows = (await db.execute(select(ObservationSlaConfig))).scalars().all()
    by_key: dict[tuple[str, str], ObservationSlaConfig] = {}
    for r in rows:
        key = (r.severity, r.categoryGroup)
        if r.plantId == plantId:
            by_key[key] = r
        elif r.plantId is None and key not in by_key:
            by_key.setdefault(key, r)
    # A plant row must always win over a global one, even if the global was
    # seen first in the unordered result set.
    for r in rows:
        if plantId is not None and r.plantId == plantId:
            by_key[(r.severity, r.categoryGroup)] = r

    out: list[SlaRowOut] = []
    for sev in SEVERITIES:
        for group in CATEGORY_GROUPS:
            row = by_key.get((sev, group))
            if row is None:
                continue
            item = SlaRowOut.model_validate(row)
            item.inherited = plantId is not None and row.plantId is None
            out.append(item)

    cfg = await _resolve_deroster_config(db, plantId)
    return SlaConfigOut(plantId=plantId, rows=out, deroster=cfg)


async def _resolve_deroster_config(db: AsyncSession, plant_id: str | None) -> DerosterConfigOut:
    from app.services.observation_deroster import resolve_config

    row = await resolve_config(db, plant_id)
    if row is None:
        return DerosterConfigOut()
    return DerosterConfigOut(
        reviewSlaHours=row.reviewSlaHours,
        escalationContactUserId=row.escalationContactUserId,
        escalationRoleCode=row.escalationRoleCode,
        inherited=plant_id is not None and row.plantId is None,
    )


@router.put("", response_model=SlaConfigOut)
async def upsert_sla_config(
    payload: SlaConfigUpsert,
    plantId: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SlaConfigOut:
    """Bulk upsert of the matrix + review SLA for one scope.

    Editing the matrix never touches observations already submitted — each one
    carries a frozen copy of the policy it was held to in
    `Observation.targetDateSlaConfig`. New policy applies forward only
    (spec §7, first checklist item).
    """
    await require_permission_with_context(
        "CONFIGURATION.MASTERS", user, db, plant_id=plantId
    )

    for row in payload.rows:
        try:
            row.validated()
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    existing = (
        await db.execute(
            select(ObservationSlaConfig).where(
                ObservationSlaConfig.plantId.is_(None)
                if plantId is None
                else ObservationSlaConfig.plantId == plantId
            )
        )
    ).scalars().all()
    index = {(r.severity, r.categoryGroup): r for r in existing}

    for row in payload.rows:
        sev = row.severity.upper()
        group = row.categoryGroup.upper()
        current = index.get((sev, group))
        if current is None:
            current = ObservationSlaConfig(
                plantId=plantId, severity=sev, categoryGroup=group, slaDays=row.slaDays
            )
            db.add(current)
            index[(sev, group)] = current
        current.slaDays = row.slaDays
        current.isActive = row.isActive
        current.updatedById = user.id

    if (
        payload.reviewSlaHours is not None
        or payload.escalationContactUserId is not None
        or payload.escalationRoleCode is not None
    ):
        cfg = (
            await db.execute(
                select(ObservationDerosterConfig).where(
                    ObservationDerosterConfig.plantId.is_(None)
                    if plantId is None
                    else ObservationDerosterConfig.plantId == plantId
                )
            )
        ).scalars().first()
        if cfg is None:
            cfg = ObservationDerosterConfig(
                plantId=plantId, reviewSlaHours=DEFAULT_REVIEW_SLA_HOURS
            )
            db.add(cfg)
        if payload.reviewSlaHours is not None:
            cfg.reviewSlaHours = payload.reviewSlaHours
        if payload.escalationContactUserId is not None:
            cfg.escalationContactUserId = payload.escalationContactUserId or None
        if payload.escalationRoleCode is not None:
            cfg.escalationRoleCode = payload.escalationRoleCode
        cfg.isActive = True
        cfg.updatedById = user.id

    await db.flush()
    return await get_sla_config(plantId=plantId, user=user, db=db)


@router.get("/preview", response_model=SlaPreviewOut)
async def preview_sla(
    plantId: str,
    type: ObservationType,
    severity: Severity,
    categoryCode: str | None = Query(
        None,
        description=(
            "STOP category. Required to resolve the Behavioural/Physical group for "
            "at-risk types; omit for SAFE_ACT / SAFE_CONDITION, which carry no STOP "
            "category and fall back to the axis."
        ),
    ),
    date: datetime | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SlaPreviewOut:
    """What the form shows before submission.

    The group comes from the ObservationCategoryGroup mapping, so the preview
    only fully resolves for an at-risk observation once a category is chosen —
    which matches the spec's trigger ("on Severity + Category both selected").
    """
    await require_permission_with_context("OBSERVATION.CREATE", user, db, plant_id=plantId)
    result = await sla.preview(
        db,
        plant_id=plantId,
        obs_type=type,
        severity=severity,
        category_code=categoryCode,
        observation_date=date or sla.now_utc(),
    )
    return SlaPreviewOut(**result)


@router.get("/category-groups", response_model=list[CategoryGroupOut])
async def list_category_groups(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CategoryGroupOut]:
    """The STOP category → Behavioural | Physical mapping, joined to the
    taxonomy so the admin screen can show the label and STOP reference rather
    than a bare code."""
    await require_permission_with_context("OBSERVATION.READ", user, db, plant_id=None)

    rows = (
        await db.execute(
            select(ObservationCategoryGroup).where(ObservationCategoryGroup.isActive.is_(True))
        )
    ).scalars().all()

    tax_rows = (
        await db.execute(
            select(
                ObservationTaxonomy.categoryCode,
                ObservationTaxonomy.categoryLabel,
                ObservationTaxonomy.stopReferenceCode,
            ).where(ObservationTaxonomy.isActive.is_(True))
        )
    ).all()
    labels = {c: (lbl, stop) for c, lbl, stop in tax_rows}

    out: list[CategoryGroupOut] = []
    for r in sorted(rows, key=lambda x: labels.get(x.categoryCode, ("", "ZZZ"))[1]):
        label, stop = labels.get(r.categoryCode, (r.categoryCode, ""))
        out.append(
            CategoryGroupOut(
                id=r.id,
                categoryCode=r.categoryCode,
                categoryLabel=label,
                stopReferenceCode=stop,
                axis=r.axis,
                categoryGroup=r.categoryGroup,
                pending=r.categoryGroup == CATEGORY_GROUP_PENDING,
                notes=r.notes,
            )
        )
    return out


@router.put("/category-groups", response_model=list[CategoryGroupOut])
async def upsert_category_groups(
    payload: CategoryGroupUpsert,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CategoryGroupOut]:
    """Set the group for one or more STOP categories.

    Applies forward only, like the matrix itself: every submitted observation
    carries a frozen `targetDateSlaConfig` snapshot, so re-grouping a category
    cannot restate the closure date of a record already in the register.
    """
    await require_permission_with_context("CONFIGURATION.MASTERS", user, db, plant_id=None)

    for row in payload.rows:
        if row.categoryGroup not in CATEGORY_GROUP_VALUES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Unknown category group '{row.categoryGroup}'. "
                f"Expected one of {', '.join(CATEGORY_GROUP_VALUES)}.",
            )
        axis = (row.axis or AXIS_ANY).upper()
        existing = (
            await db.execute(
                select(ObservationCategoryGroup)
                .where(ObservationCategoryGroup.categoryCode == row.categoryCode)
                .where(ObservationCategoryGroup.axis == axis)
            )
        ).scalars().first()
        if existing is None:
            existing = ObservationCategoryGroup(categoryCode=row.categoryCode, axis=axis)
            db.add(existing)
        existing.categoryGroup = row.categoryGroup
        existing.notes = row.notes
        existing.isActive = True
        existing.updatedById = user.id

    await db.flush()
    return await list_category_groups(user=user, db=db)
