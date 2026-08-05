"""Severity suggestion + calibration API for Safety Observations.

Route shape differs from the build spec deliberately. The spec specifies
`/api/safety-observations/severity-suggestion`; this module has been mounted at
`/api/observations` since the first vertical slice, and `sla-config/preview` set
the precedent for a form-support endpoint living under it. Introducing a second
prefix for the same resource would leave the proxy, the mobile module config and
the RBAC scope reading two different module names.

⚠ MOUNT ORDER: this router must be registered BEFORE `observations`, which owns
`GET /api/observations/{observation_id}` and would otherwise swallow
`/severity-suggestion` and resolve it as an observation id. Same reason
`observation_sla` is mounted first — see main.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.observation_severity import (
    MIN_OVERRIDE_REASON_CHARS,
    OVERRIDE_SOURCES,
    OVERRIDE_SOURCE_OBSERVER_FORM,
    SEVERITY_LADDER,
)
from app.models.user import User
from app.schemas.observation_severity import (
    CalibrationReportOut,
    CalibrationRow,
    SeveritySuggestionOut,
)
from app.services import observation_severity as sev
from app.services.permissions import get_accessible_plants

router = APIRouter(prefix="/api/observations", tags=["observations"])

DEFAULT_CALIBRATION_WINDOW_DAYS = 90


@router.get("/severity-suggestion", response_model=SeveritySuggestionOut)
async def severity_suggestion(
    observationType: str = Query(
        ...,
        description=(
            "The act/condition axis. Accepts a bare axis (ACT / CONDITION), a full "
            "ObservationType (UNSAFE_ACT), or the build spec's lowercase unsafe_act."
        ),
    ),
    category: str | None = Query(None, description="STOP categoryCode."),
    subCategory: str | None = Query(None, description="STOP subCategoryCode."),
    plantId: str | None = Query(None),
    areaId: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SeveritySuggestionOut:
    """The suggested severity for a classification. Read-only, no side effects.

    Called by the form on every relevant field change. Returns
    `suggested: null` rather than an error when no rule is seeded for the
    combination — the form then behaves exactly as it did before this feature
    existed, which is the graceful-degradation requirement of §6.6.
    """
    await require_permission_with_context("OBSERVATION.CREATE", user, db, plant_id=plantId)

    result = await sev.resolve(
        db,
        observation_type=observationType,
        category=category,
        sub_category=subCategory,
        plant_id=plantId,
        area_id=areaId,
    )
    return SeveritySuggestionOut(
        **result,
        # Sent so the client can order a dropdown and label an override
        # direction without a second copy of the ladder in the browser.
        severityLadder=list(SEVERITY_LADDER),
        minOverrideReasonChars=MIN_OVERRIDE_REASON_CHARS,
    )


@router.get("/severity-calibration", response_model=CalibrationReportOut)
async def severity_calibration(
    days: int = Query(
        DEFAULT_CALIBRATION_WINDOW_DAYS,
        ge=1,
        le=1095,
        description="Look-back window in days.",
    ),
    minOverrides: int = Query(1, ge=1, description="Hide pairs below this override count."),
    includeAllSources: bool = Query(
        False,
        description=(
            "Include capture-conversion and edit overrides. Off by default — a "
            "triager's or an editor's severity call is not observer disagreement."
        ),
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CalibrationReportOut:
    """Override frequency by category / sub-category — the calibration loop.

    Plant-scoped server-side from the caller's own OBSERVATION.READ scope, the
    same way the register is: a report that leaked other plants' override
    behaviour would be a quieter version of leaking their observations.
    """
    await require_permission_with_context("OBSERVATION.READ", user, db, plant_id=None)

    # None ⇒ ALL_PLANTS (no filter). Empty list ⇒ no accessible plants ⇒ no rows.
    accessible = await get_accessible_plants(db, user.id)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    sources = tuple(OVERRIDE_SOURCES) if includeAllSources else (OVERRIDE_SOURCE_OBSERVER_FORM,)

    rows = await sev.calibration_report(
        db,
        plant_ids=accessible,
        since=since,
        sources=sources,
        min_overrides=minOverrides,
    )
    return CalibrationReportOut(
        rows=[CalibrationRow(**r) for r in rows],
        since=since,
        sources=list(sources),
    )
