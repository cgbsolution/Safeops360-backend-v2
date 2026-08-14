"""Manhours submissions — the IS 3786 monthly return.

Mounts at /api/manhours-submissions, matching the paths the web wizard already
calls, so the frontend swap is a base-URL change rather than a rewrite.

Every mutation goes through `assert_editable` first: a submission past DRAFT /
UNLOCKED_FOR_REVISION is a reported figure and cannot be edited in place — the
unlock workflow exists for that, and it leaves an audit trail.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.manhours_submission import (
    ManhoursEmployeeCategory,
    ManhoursSubmission,
    ManhoursUnlockEvent,
    ManhoursVisitorRecord,
)
from app.models.plant import Plant
from app.models.user import User
from app.services import manhours_submission as svc
from app.services.permissions import PermissionContext, can, get_accessible_plants
from app.services.manhours_csv import generate_template, parse_category_csv
from app.services.manhours_validation import validate_submission
from app.services.register_view import status_counts, workflow_chips

router = APIRouter(prefix="/api/manhours-submissions", tags=["manhours-submissions"])


def _bad(e: svc.ManhoursStatusError) -> HTTPException:
    """A wrong-state transition is the caller's mistake, not a server fault."""
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


def _cols(row) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


async def _load(db: AsyncSession, submission_id: str) -> ManhoursSubmission:
    submission = await db.get(ManhoursSubmission, submission_id)
    if submission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Submission not found")
    return submission


async def _require(
    db: AsyncSession, user: User, permission: str, plant_id: str | None = None
) -> None:
    check = await can(db, user.id, permission, PermissionContext(plant_id=plant_id))
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")


# ── Register ─────────────────────────────────────────────────────────


@router.get("")
async def list_submissions(
    year: int | None = None,
    plantId: str | None = None,
    status_filter: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The submissions register, plant-scoped, with status tab counts."""
    await _require(db, user, "MANHOURS.READ")
    plants = await get_accessible_plants(db, user.id)

    stmt = select(ManhoursSubmission)
    if plants is not None:
        if not plants:
            return {"items": [], "total": 0, "statusCounts": {}}
        stmt = stmt.where(ManhoursSubmission.plantId.in_(plants))
    if plantId:
        stmt = stmt.where(ManhoursSubmission.plantId == plantId)
    if year:
        stmt = stmt.where(ManhoursSubmission.reportingYear == year)

    counts = await status_counts(db, stmt, ManhoursSubmission.status)
    if status_filter:
        stmt = stmt.where(ManhoursSubmission.status == status_filter)

    rows = (
        await db.execute(
            stmt.order_by(
                ManhoursSubmission.reportingYear.desc(),
                ManhoursSubmission.reportingMonth.desc(),
            ).limit(200)
        )
    ).scalars().all()

    plant_names = dict(
        (
            await db.execute(
                select(Plant.id, Plant.name).where(
                    Plant.id.in_({r.plantId for r in rows})
                )
            )
        ).all()
    ) if rows else {}
    chips = await workflow_chips(db, "MANHOURS", [r.id for r in rows])

    items = []
    for r in rows:
        item = _cols(r)
        item["plantName"] = plant_names.get(r.plantId)
        item["workflow"] = chips.get(r.id)
        items.append(item)
    return {"items": items, "total": len(items), "statusCounts": counts}


class SubmissionCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    plantId: str
    reportingYear: int
    reportingMonth: int


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_submission(
    payload: SubmissionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Open the return for a plant-month.

    Idempotent by design: the (plant, year, month) unique constraint means one
    return per period, so re-opening an existing period hands back the existing
    draft rather than failing or duplicating it.
    """
    await _require(db, user, "MANHOURS.CREATE", payload.plantId)
    if not 1 <= payload.reportingMonth <= 12:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "reportingMonth must be 1-12")

    existing = (
        await db.execute(
            select(ManhoursSubmission)
            .where(ManhoursSubmission.plantId == payload.plantId)
            .where(ManhoursSubmission.reportingYear == payload.reportingYear)
            .where(ManhoursSubmission.reportingMonth == payload.reportingMonth)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {**_cols(existing), "alreadyExisted": True}

    start, end = svc.period_bounds(payload.reportingYear, payload.reportingMonth)
    submission = ManhoursSubmission(
        plantId=payload.plantId,
        reportingYear=payload.reportingYear,
        reportingMonth=payload.reportingMonth,
        reportingPeriodStart=start,
        reportingPeriodEnd=end,
        status="DRAFT",
    )
    db.add(submission)
    await db.flush()
    return {**_cols(submission), "alreadyExisted": False}


@router.get("/{submission_id}")
async def get_submission(
    submission_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The full return: categories, visitors, unlock history and the plant."""
    submission = await _load(db, submission_id)
    await _require(db, user, "MANHOURS.READ", submission.plantId)

    out = _cols(submission)
    plant = await db.get(Plant, submission.plantId)
    out["plant"] = {"id": plant.id, "name": plant.name, "code": plant.code} if plant else None

    categories = (
        await db.execute(
            select(ManhoursEmployeeCategory)
            .where(ManhoursEmployeeCategory.submissionId == submission_id)
            .order_by(ManhoursEmployeeCategory.categoryType)
        )
    ).scalars().all()
    out["categories"] = [_cols(c) for c in categories]

    visitors = (
        await db.execute(
            select(ManhoursVisitorRecord).where(
                ManhoursVisitorRecord.submissionId == submission_id
            )
        )
    ).scalar_one_or_none()
    out["visitors"] = _cols(visitors) if visitors else None

    unlocks = (
        await db.execute(
            select(ManhoursUnlockEvent)
            .where(ManhoursUnlockEvent.submissionId == submission_id)
            .order_by(ManhoursUnlockEvent.unlockedAt.desc())
        )
    ).scalars().all()
    out["unlockHistory"] = [_cols(u) for u in unlocks]
    return out


# Deduction fields the wizard may PATCH. Aggregates are server-derived, status
# moves through the transition endpoints, and review/lock metadata is owned by
# the workflow — none of them are writable here.
_PATCHABLE = {
    "hoursAnnualLeave",
    "hoursSickLeave",
    "hoursTraining",
    "hoursMaternityLeave",
    "hoursOther",
    "totalDaysWorked",
    "totalShiftsWorked",
    "submissionNotes",
}


@router.patch("/{submission_id}")
async def patch_submission(
    submission_id: str,
    payload: dict[str, Any] = Body(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    submission = await _load(db, submission_id)
    await _require(db, user, "MANHOURS.UPDATE", submission.plantId)
    try:
        svc.assert_editable(submission)
    except svc.ManhoursStatusError as e:
        raise _bad(e) from e

    unknown = set(payload) - _PATCHABLE
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Fields are not writable here: {', '.join(sorted(unknown))}",
        )
    for key, value in payload.items():
        setattr(submission, key, value)

    # Any deduction change moves the net exposure figure, which is the KPI
    # denominator — so the roll-ups are refreshed rather than left stale.
    refreshed = await svc.refresh_aggregates(db, submission_id)
    return _cols(refreshed)


# ── Categories ───────────────────────────────────────────────────────


@router.get("/{submission_id}/categories")
async def list_categories(
    submission_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    submission = await _load(db, submission_id)
    await _require(db, user, "MANHOURS.READ", submission.plantId)
    rows = (
        await db.execute(
            select(ManhoursEmployeeCategory).where(
                ManhoursEmployeeCategory.submissionId == submission_id
            )
        )
    ).scalars().all()
    return {"items": [_cols(r) for r in rows]}


class CategoryInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    categoryType: str
    departmentId: str | None = None
    shiftId: str | None = None
    contractorCompanyId: str | None = None
    averageHeadcount: int = 0
    peakHeadcount: int = 0
    endOfPeriodHeadcount: int = 0
    regularHours: float = 0
    overtimeHours: float = 0
    notes: str | None = None


@router.post("/{submission_id}/categories", status_code=status.HTTP_201_CREATED)
async def add_category(
    submission_id: str,
    payload: CategoryInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    submission = await _load(db, submission_id)
    await _require(db, user, "MANHOURS.UPDATE", submission.plantId)
    try:
        svc.assert_editable(submission)
    except svc.ManhoursStatusError as e:
        raise _bad(e) from e
    if payload.categoryType not in ("PERMANENT", "CONTRACT", "TRAINEE"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "categoryType must be PERMANENT, CONTRACT or TRAINEE",
        )

    row = ManhoursEmployeeCategory(
        submissionId=submission_id,
        **payload.model_dump(),
        totalHours=svc.category_total_hours(payload.regularHours, payload.overtimeHours),
    )
    db.add(row)
    await db.flush()
    await svc.refresh_aggregates(db, submission_id)
    return _cols(row)


@router.patch("/{submission_id}/categories/{category_id}")
async def update_category(
    submission_id: str,
    category_id: str,
    payload: dict[str, Any] = Body(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    submission = await _load(db, submission_id)
    await _require(db, user, "MANHOURS.UPDATE", submission.plantId)
    try:
        svc.assert_editable(submission)
    except svc.ManhoursStatusError as e:
        raise _bad(e) from e

    row = await db.get(ManhoursEmployeeCategory, category_id)
    if row is None or row.submissionId != submission_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Category row not found")

    for key, value in payload.items():
        if key in {"id", "submissionId", "totalHours"}:
            continue  # server-owned
        if hasattr(row, key):
            setattr(row, key, value)
    row.totalHours = svc.category_total_hours(row.regularHours, row.overtimeHours)
    await db.flush()
    await svc.refresh_aggregates(db, submission_id)
    return _cols(row)


@router.delete("/{submission_id}/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    submission_id: str,
    category_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    submission = await _load(db, submission_id)
    await _require(db, user, "MANHOURS.UPDATE", submission.plantId)
    try:
        svc.assert_editable(submission)
    except svc.ManhoursStatusError as e:
        raise _bad(e) from e
    row = await db.get(ManhoursEmployeeCategory, category_id)
    if row is None or row.submissionId != submission_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Category row not found")
    await db.delete(row)
    await db.flush()
    await svc.refresh_aggregates(db, submission_id)


# ── Visitors ─────────────────────────────────────────────────────────


class VisitorInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    totalVisitorCount: int = 0
    totalVisitorHours: float = 0
    notableVisits: str | None = None


@router.put("/{submission_id}/visitors")
async def upsert_visitors(
    submission_id: str,
    payload: VisitorInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """One visitor aggregate per return, so this is an upsert rather than a
    create — the wizard can save the step repeatedly."""
    submission = await _load(db, submission_id)
    await _require(db, user, "MANHOURS.UPDATE", submission.plantId)
    try:
        svc.assert_editable(submission)
    except svc.ManhoursStatusError as e:
        raise _bad(e) from e

    row = (
        await db.execute(
            select(ManhoursVisitorRecord).where(
                ManhoursVisitorRecord.submissionId == submission_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = ManhoursVisitorRecord(submissionId=submission_id, **payload.model_dump())
        db.add(row)
    else:
        for key, value in payload.model_dump().items():
            setattr(row, key, value)
    await db.flush()
    return _cols(row)


# ── Transitions ──────────────────────────────────────────────────────


class NotesInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    notes: str | None = None


class RejectInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decision: str = "RETURNED_FOR_REVISION"
    notes: str


class ReasonInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str


@router.post("/{submission_id}/submit")
async def submit(
    submission_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    submission = await _load(db, submission_id)
    await _require(db, user, "MANHOURS.UPDATE", submission.plantId)
    # Recompute before submitting: whatever the reviewer sees must match what
    # the categories actually say, not a stale roll-up.
    await svc.refresh_aggregates(db, submission_id)
    try:
        return await svc.submit_for_review(
            db, submission_id=submission_id, initiator_id=user.id
        )
    except svc.ManhoursStatusError as e:
        raise _bad(e) from e


class ReviewInput(BaseModel):
    """The Plant Head decision, in the shape the wizard already posts:
    { decision, notes }. The verb lives in the body rather than a query flag
    because REJECTED and RETURNED_FOR_REVISION are different outcomes that
    both mean "not approved" — a boolean could not tell them apart."""

    model_config = ConfigDict(extra="ignore")

    decision: str
    notes: str | None = None


_REVIEW_DECISIONS = {"APPROVED", "REJECTED", "RETURNED_FOR_REVISION"}


@router.post("/{submission_id}/review")
async def review(
    submission_id: str,
    payload: ReviewInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Plant Head decision.

    Anything other than APPROVED returns the submission to DRAFT for
    correction rather than ending it - the month still has to be filed.
    """
    submission = await _load(db, submission_id)
    await _require(db, user, "MANHOURS.APPROVE", submission.plantId)
    if payload.decision not in _REVIEW_DECISIONS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"decision must be one of {', '.join(sorted(_REVIEW_DECISIONS))}",
        )
    try:
        if payload.decision == "APPROVED":
            return await svc.plant_head_approve(
                db,
                submission_id=submission_id,
                approver_id=user.id,
                notes=payload.notes,
            )
        return await svc.plant_head_reject(
            db,
            submission_id=submission_id,
            reviewer_id=user.id,
            decision=payload.decision,
            notes=payload.notes or "",
        )
    except svc.ManhoursStatusError as e:
        raise _bad(e) from e


@router.post("/{submission_id}/lock")
async def lock(
    submission_id: str,
    payload: NotesInput = Body(default_factory=NotesInput),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    submission = await _load(db, submission_id)
    await _require(db, user, "MANHOURS.APPROVE", submission.plantId)
    try:
        return await svc.corporate_lock(
            db, submission_id=submission_id, locker_id=user.id, notes=payload.notes
        )
    except svc.ManhoursStatusError as e:
        raise _bad(e) from e


@router.post("/{submission_id}/unlock")
async def unlock(
    submission_id: str,
    payload: ReasonInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    submission = await _load(db, submission_id)
    await _require(db, user, "MANHOURS.APPROVE", submission.plantId)
    try:
        return await svc.unlock(
            db, submission_id=submission_id, unlocker_id=user.id, reason=payload.reason
        )
    except svc.ManhoursStatusError as e:
        raise _bad(e) from e


@router.post("/{submission_id}/relock")
async def relock(
    submission_id: str,
    payload: NotesInput = Body(default_factory=NotesInput),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    submission = await _load(db, submission_id)
    await _require(db, user, "MANHOURS.APPROVE", submission.plantId)
    try:
        return await svc.relock(
            db, submission_id=submission_id, locker_id=user.id, notes=payload.notes
        )
    except svc.ManhoursStatusError as e:
        raise _bad(e) from e


@router.get("/{submission_id}/kpi")
async def submission_kpis(
    submission_id: str,
    live: bool = Query(False, description="Recompute instead of reading the snapshot."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The period's KPIs.

    Reads the frozen snapshot when the return is locked — that is the whole
    point of freezing it. `live=true` recomputes, which is what the wizard
    shows while the return is still being edited.
    """
    submission = await _load(db, submission_id)
    await _require(db, user, "MANHOURS.READ", submission.plantId)

    if submission.kpiSnapshot and not live:
        return {"source": "snapshot", **submission.kpiSnapshot}

    from app.services.manhours_kpi_engine import KpiEngine
    from app.services.manhours_kpi_registry import REGISTRY_VERSION

    engine = KpiEngine(db)
    kpis = await engine.compute_all(
        submission.plantId, submission.reportingYear, submission.reportingMonth
    )
    return {
        "source": "live",
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "registryVersion": REGISTRY_VERSION,
        "scope": {"plantId": submission.plantId},
        "period": {
            "year": submission.reportingYear,
            "month": submission.reportingMonth,
        },
        "kpis": kpis,
    }


@router.get("/{submission_id}/validate")
async def validate(
    submission_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Run the submit-time checks without transitioning.

    The wizard calls this live while Steps 1-7 are edited, so the submitter
    sees what would block them before they try. Aggregates are refreshed first
    - validating a stale roll-up would report problems that no longer exist,
    or miss ones that do.
    """
    submission = await _load(db, submission_id)
    await _require(db, user, "MANHOURS.READ", submission.plantId)
    submission = await svc.refresh_aggregates(db, submission_id)

    category_rows = (
        await db.execute(
            select(ManhoursEmployeeCategory).where(
                ManhoursEmployeeCategory.submissionId == submission_id
            )
        )
    ).scalars().all()

    # Department / contractor NAMES, because the novelty checks compare
    # against what recent months looked like and ids are not stable across
    # a rename.
    from app.models.epc import ContractorCompany
    from app.models.masters import Department

    dept_ids = {c.departmentId for c in category_rows if c.departmentId}
    contractor_ids = {c.contractorCompanyId for c in category_rows if c.contractorCompanyId}
    dept_names = dict(
        (await db.execute(select(Department.id, Department.name).where(Department.id.in_(dept_ids)))).all()
    ) if dept_ids else {}
    contractor_names = dict(
        (
            await db.execute(
                select(ContractorCompany.id, ContractorCompany.name).where(
                    ContractorCompany.id.in_(contractor_ids)
                )
            )
        ).all()
    ) if contractor_ids else {}

    categories = [
        {
            "categoryType": c.categoryType,
            "totalHours": c.totalHours,
            "averageHeadcount": c.averageHeadcount,
            "departmentName": dept_names.get(c.departmentId) if c.departmentId else None,
            "contractorName": (
                contractor_names.get(c.contractorCompanyId) if c.contractorCompanyId else None
            ),
        }
        for c in category_rows
    ]

    # The last 6 LOCKED months for this plant. Only locked ones: a draft is
    # not yet a fact to compare against.
    prior_rows = (
        await db.execute(
            select(ManhoursSubmission)
            .where(ManhoursSubmission.plantId == submission.plantId)
            .where(ManhoursSubmission.id != submission_id)
            .where(ManhoursSubmission.status == "LOCKED")
            .order_by(
                ManhoursSubmission.reportingYear.desc(),
                ManhoursSubmission.reportingMonth.desc(),
            )
            .limit(6)
        )
    ).scalars().all()

    prior_months = []
    for p in prior_rows:
        p_cats = (
            await db.execute(
                select(
                    ManhoursEmployeeCategory.departmentId,
                    ManhoursEmployeeCategory.contractorCompanyId,
                ).where(ManhoursEmployeeCategory.submissionId == p.id)
            )
        ).all()
        p_dept_ids = {d for d, _c in p_cats if d}
        p_con_ids = {c for _d, c in p_cats if c}
        p_dept_names = dict(
            (
                await db.execute(
                    select(Department.id, Department.name).where(Department.id.in_(p_dept_ids))
                )
            ).all()
        ) if p_dept_ids else {}
        p_con_names = dict(
            (
                await db.execute(
                    select(ContractorCompany.id, ContractorCompany.name).where(
                        ContractorCompany.id.in_(p_con_ids)
                    )
                )
            ).all()
        ) if p_con_ids else {}
        prior_months.append(
            {
                "reportingYear": p.reportingYear,
                "reportingMonth": p.reportingMonth,
                "netExposureHours": p.netExposureHours,
                "totalEmployeeStrength": p.totalEmployeeStrength,
                "totalContractorStrength": p.totalContractorStrength,
                "departmentNames": list(p_dept_names.values()),
                "contractorNames": list(p_con_names.values()),
            }
        )

    report = validate_submission(_cols(submission), categories, prior_months)
    return {"report": report.to_dict()}


class CsvImportInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    categoryType: str
    csv: str


@router.post("/{submission_id}/categories/import-csv")
async def import_categories_csv(
    submission_id: str,
    payload: CsvImportInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Bulk-import category rows from a CSV.

    REPLACES every existing row of this categoryType rather than merging.
    Re-importing is the intended way to fix a mistake, and merging would
    silently double every headcount on the second attempt.

    Unknown department / contractor codes are reported as row errors and their
    rows are skipped - importing them as NULL would produce rows that look
    valid but belong to nobody.
    """
    submission = await _load(db, submission_id)
    await _require(db, user, "MANHOURS.UPDATE", submission.plantId)
    try:
        svc.assert_editable(submission)
    except svc.ManhoursStatusError as e:
        raise _bad(e) from e

    kind = payload.categoryType
    if kind not in ("PERMANENT", "CONTRACT", "TRAINEE"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "categoryType must be PERMANENT, CONTRACT or TRAINEE",
        )

    parsed = parse_category_csv(payload.csv, kind)
    errors = list(parsed["errors"])
    rows = parsed["rows"]

    # Resolve the human-facing codes in the file to ids.
    from app.models.epc import ContractorCompany
    from app.models.masters import Department

    keys = {r["key"] for r in rows}
    resolved: dict[str, str] = {}
    if keys:
        if kind == "CONTRACT":
            found = (
                await db.execute(
                    select(ContractorCompany.code, ContractorCompany.id).where(
                        ContractorCompany.code.in_(keys)
                    )
                )
            ).all()
        else:
            found = (
                await db.execute(
                    select(Department.code, Department.id).where(Department.code.in_(keys))
                )
            ).all()
        resolved = {code: rid for code, rid in found}

    importable = []
    for r in rows:
        target = resolved.get(r["key"])
        if target is None:
            errors.append(
                {
                    "row": 0,
                    "message": f'Unknown {"contractor" if kind == "CONTRACT" else "department"} code: {r["key"]}',
                }
            )
            continue
        importable.append((r, target))

    # Replace, not merge.
    existing = (
        await db.execute(
            select(ManhoursEmployeeCategory)
            .where(ManhoursEmployeeCategory.submissionId == submission_id)
            .where(ManhoursEmployeeCategory.categoryType == kind)
        )
    ).scalars().all()
    replaced = len(existing)
    for row in existing:
        await db.delete(row)
    await db.flush()

    for r, target in importable:
        db.add(
            ManhoursEmployeeCategory(
                submissionId=submission_id,
                categoryType=kind,
                departmentId=None if kind == "CONTRACT" else target,
                contractorCompanyId=target if kind == "CONTRACT" else None,
                averageHeadcount=r["averageHeadcount"],
                peakHeadcount=r["peakHeadcount"],
                endOfPeriodHeadcount=r["endOfPeriodHeadcount"],
                regularHours=r["regularHours"],
                overtimeHours=r["overtimeHours"],
                totalHours=svc.category_total_hours(r["regularHours"], r["overtimeHours"]),
                notes=r["notes"],
            )
        )
    await db.flush()
    await svc.refresh_aggregates(db, submission_id)
    return {"imported": len(importable), "replaced": replaced, "errors": errors}


@router.get("/{submission_id}/categories/template")
async def csv_template(
    submission_id: str,
    categoryType: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The import template, pre-filled with this plant's real codes so the
    user does not have to go and look them up."""
    submission = await _load(db, submission_id)
    await _require(db, user, "MANHOURS.READ", submission.plantId)
    if categoryType not in ("PERMANENT", "CONTRACT", "TRAINEE"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown categoryType")

    from app.models.epc import ContractorCompany
    from app.models.masters import Department

    if categoryType == "CONTRACT":
        codes = [
            c for (c,) in (await db.execute(select(ContractorCompany.code))).all() if c
        ]
    else:
        codes = [c for (c,) in (await db.execute(select(Department.code))).all() if c]
    return {"csv": generate_template(categoryType, sorted(codes))}
