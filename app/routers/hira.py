"""HIRA router — Phase 2 vertical slice.

Endpoints exposed:
  - GET    /api/hira/risk-matrices                — list active matrices
  - GET    /api/hira/risk-matrices/{id}           — matrix + scales + cells
  - GET    /api/hira/hazards                      — hazard library search
  - GET    /api/hira/controls                     — control library
  - GET    /api/hira/studies                      — plant-scoped study list
  - POST   /api/hira/studies                      — create study
  - GET    /api/hira/studies/{id}                 — study detail
  - GET    /api/hira/studies/{id}/entries         — entries in study
  - POST   /api/hira/studies/{id}/entries         — create entry
  - GET    /api/hira/entries/{id}                 — entry detail

Workflow integration: study creation does NOT yet kick off the workflow
engine. That happens in Phase 4 when the HIRA_STUDY_STANDARD definition
is seeded. Until then studies stay in DRAFT and can be edited freely.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:
    from dateutil.relativedelta import relativedelta as _relativedelta
    _HAS_RELATIVEDELTA = True
except ImportError:
    _HAS_RELATIVEDELTA = False

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, inspect as sa_inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.hira import (
    HiraCapa,
    HiraControl,
    HiraEntry,
    HiraEntryControl,
    HiraEntryHazard,
    HiraEntryRecommendedControl,
    HiraEntryRegulationRef,
    HiraHazard,
    HiraReviewCycle,
    HiraStudy,
    HiraStudyTeamMember,
    HiraVersion,
    RiskMatrix,
    RiskMatrixCell,
    RiskMatrixLikelihood,
    RiskMatrixSeverity,
)
from app.models.permit import PermitType
from app.models.plant import Plant
from app.models.user import User
from app.schemas.hira import (
    HiraCapaCreate,
    HiraCapaOut,
    HiraCapaUpdate,
    HiraControlOut,
    HiraDashboardCoverage,
    HiraDashboardHighRisk,
    HiraDashboardReviewCompliance,
    HiraDashboardRiskReduction,
    HiraDashboardTopHazard,
    HiraEntryControlReplaceRequest,
    HiraEntryCreate,
    HiraEntryHazardReplaceItem,
    HiraEntryListItem,
    HiraEntryListResponse,
    HiraEntryOut,
    HiraEntryRecommendedControlReplaceRequest,
    HiraEntryRegulationRefReplaceRequest,
    HiraEntryTransitionRequest,
    HiraEntryUpdate,
    HiraHazardOut,
    HiraHazardPermitUpdate,
    HiraIntegrationEntry,
    HiraIntegrationForFlraResponse,
    HiraIntegrationForPtwResponse,
    HiraInspectionPriorityResult,
    HiraReviewCycleBulkNoChangeRequest,
    HiraReviewCycleListItem,
    HiraReviewCycleOut,
    HiraReviewCycleSubmitRequest,
    HiraStudyCreate,
    HiraStudyListItem,
    HiraStudyListResponse,
    HiraStudyDetailResponse,
    HiraStudyOut,
    HiraStudyTransitionRequest,
    HiraStudyUpdate,
    HiraUnacceptableOverrideRequest,
    HiraVersionOut,
    RiskMatrixOut,
)
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
)

router = APIRouter(prefix="/api/hira", tags=["hira"])

# ─────────────────────────────────────────────────────────────────────
# ALARP tolerability banding
#
# ALARP ("As Low As Reasonably Practicable") sorts risk into three
# regions. The default maps the 4-level scale as agreed:
#   CRITICAL         -> UNACCEPTABLE        (must be reduced; warn+justify)
#   HIGH / MODERATE  -> TOLERABLE           (accept only if ALARP demonstrated)
#   LOW              -> BROADLY_ACCEPTABLE   (no further action needed)
# Per-matrix overrides live in RiskMatrix.alarpBands; this is the fallback.
# ─────────────────────────────────────────────────────────────────────
DEFAULT_ALARP_BANDS: dict[str, str] = {
    "LOW": "BROADLY_ACCEPTABLE",
    "MODERATE": "TOLERABLE",
    "HIGH": "TOLERABLE",
    "CRITICAL": "UNACCEPTABLE",
}


def _alarp_region(level: str | None, alarp_bands: dict | None) -> str | None:
    """Map a risk level to its ALARP region via the matrix bands (or default)."""
    if not level:
        return None
    bands = alarp_bands or DEFAULT_ALARP_BANDS
    return bands.get(level) or DEFAULT_ALARP_BANDS.get(level, "TOLERABLE")


def _alarp_demonstrated(entry: HiraEntry) -> bool:
    """A tolerable-region residual is ALARP only when the cost-benefit test is
    complete: further controls were considered, the residual reduction was
    judged grossly disproportionate to the cost/effort, and it is justified."""
    return bool(
        entry.alarpFurtherControlsConsidered is not None
        and entry.alarpGrosslyDisproportionate is True
        and (entry.alarpJustification or "").strip()
    )


def _evaluate_alarp(entry: HiraEntry, region: str | None, threshold: str | None, user_id: str) -> None:
    """Recompute ALARP status, sign-off and residualAcceptable from the entry's
    current residual region + demonstration fields. Call AFTER residual L/S and
    ALARP payload fields have been applied to the entry.

    Enforcement is 'warn only': an UNACCEPTABLE residual is never hard-blocked
    here — it is marked not-acceptable so the UI/register flag it and require a
    documented acceptance rationale.
    """
    entry.residualAlarpRegion = region

    if region is None:
        entry.alarpStatus = None
        return

    if region == "BROADLY_ACCEPTABLE":
        entry.alarpStatus = "NOT_REQUIRED"
        region_ok = True
    elif region == "TOLERABLE":
        demonstrated = _alarp_demonstrated(entry)
        entry.alarpStatus = "DEMONSTRATED" if demonstrated else "REQUIRED"
        if demonstrated and entry.alarpDemonstratedAt is None:
            entry.alarpDemonstratedById = user_id
            entry.alarpDemonstratedAt = datetime.now(timezone.utc)
        if not demonstrated:
            # Clear a stale sign-off if the demonstration was walked back.
            entry.alarpDemonstratedById = None
            entry.alarpDemonstratedAt = None
        region_ok = demonstrated
    else:  # UNACCEPTABLE
        entry.alarpStatus = "NOT_REQUIRED"
        region_ok = False

    # Legacy per-routine threshold remains a stricter-only secondary gate so
    # existing policy (e.g. emergency activities capped at LOW) is preserved.
    threshold_ok = True if not threshold else _acceptability_ok(entry.residualRiskLevel or "LOW", threshold)
    entry.residualAcceptable = region_ok and threshold_ok


# ─────────────────────────────────────────────────────────────────────
# Materiality — G21
#
# Before this, every edit to an entry on an APPROVED/ACTIVE study was treated
# identically: new version + mandatory change reason, and the entry stayed
# APPROVED regardless of what changed. That produced both failure modes at
# once — approval fatigue on typo fixes, and residual-risk changes landing
# under a stale approval with no re-sign-off.
#
# A change is MATERIAL when it moves the assessed risk or the decision that
# justified accepting it:
#   • initial or residual likelihood/severity/level changed (either mode —
#     manual pick or auto-derived from controls)
#   • the routine classification changed (it selects the acceptability
#     threshold, so it can flip residualAcceptable on its own)
#   • an ALARP *decision* flag changed (these drive alarpStatus, which drives
#     residualAcceptable). The ALARP free-text fields are NOT material.
#   • a hazard row was added/removed/re-scoped
#   • an existing control's effectiveness changed, or the control set changed
#   • a recommended control's status changed, or the proposal set changed
#
# Everything else — wording, rationales, target forecast, cross-module links,
# evidence references — is MINOR: versioned and reason-stamped as before, but
# it does not disturb the approval.
# ─────────────────────────────────────────────────────────────────────

# ALARP inputs that change the acceptability verdict rather than describing it.
# The cost band feeds the grossly-disproportionate decision, so it is material;
# the free-text benefit / justification are narrative and stay minor.
_MATERIAL_ALARP_FLAGS = ("alarpFurtherControlsConsidered", "alarpGrosslyDisproportionate", "alarpCostBand")
_MATERIAL_SCALAR_FIELDS = ("routine",) + _MATERIAL_ALARP_FLAGS

MATERIAL_TRIGGER = "MATERIAL_REVISION"
MINOR_TRIGGER = "MINOR_REVISION"

# Entry statuses that represent a live approval which a material edit must
# invalidate. IN_REVIEW is the existing status `POST /entries/{id}/approve`
# already accepts — no new status value is introduced.
_APPROVED_ENTRY_STATUSES = ("APPROVED", "ACTIVE")
# Distinct from IN_REVIEW (never-yet-approved) so a withdrawn approval is
# visible as such across the register, dashboards and Daily Brief.
REAPPROVAL_STATUS = "PENDING_REAPPROVAL"
# Statuses an approver may move to APPROVED via POST /entries/{id}/approve.
_APPROVABLE_ENTRY_STATUSES = ("IN_REVIEW", "PENDING_REAPPROVAL")


_SKIP_VERSION_DOC = (
    "Set by a caller that has already archived a version for this same logical "
    "save (the entry editor PATCHes first, then syncs hazards and controls). "
    "Without it one Save produced up to four HiraVersion rows, each demanding "
    "its own change reason. Defaults to false so a standalone API call is still "
    "versioned."
)


async def _archive_version_number(db: AsyncSession, entry: HiraEntry) -> int:
    """The number to file the outgoing state under, before `entry.versionNumber`
    advances past it.

    Model: `entry.versionNumber` is the number of the CURRENT live state, and
    HiraVersion rows archive superseded states. So a change archives the current
    state under `entry.versionNumber`, then the entry moves to n + 1.

    Two handlers disagreed about this. `update_entry` filed under
    `entry.versionNumber`; the control PUTs filed under `entry.versionNumber + 1`.
    A single Save (PATCH then child PUTs) therefore produced numbers 1, 3, 4 …
    and, once the entry's own counter caught up with a row that already existed,
    every later save died on the `(entryId, versionNumber)` unique constraint —
    a hard 500 with no way out through the UI.

    Taking max(existing)+1 whenever the naive number is already taken makes this
    collision-proof AND self-healing: entries left with a gap from the buggy
    window start saving again without any data repair. Same fix shape as the
    CAPA numbering count(*)+1 → max+1 change.
    """
    existing_max = (
        await db.execute(
            select(func.max(HiraVersion.versionNumber)).where(HiraVersion.entryId == entry.id)
        )
    ).scalar()
    number = entry.versionNumber
    if existing_max is not None and number <= existing_max:
        number = existing_max + 1
    return number


def _entry_snapshot(entry: HiraEntry) -> dict:
    """JSON-safe column-only snapshot of an entry for HiraVersion.snapshot.

    Every previous call site built this from `entry.__dict__` filtered with
    `not isinstance(v, list)`. That kept LOADED SCALAR RELATIONSHIPS — most
    notably `entry.study`, which every one of these handlers eager-loads — and
    json.dumps then blew up with "Object of type HiraStudy is not JSON
    serializable". Versioned edits therefore 500'd outright, which is why
    production held 22 HiraVersion rows and every one of them was
    INITIAL_APPROVAL. Driving the snapshot off the mapper's column list can't
    pick up a relationship at all.
    """
    snapshot = {}
    for col in sa_inspect(HiraEntry).columns.keys():
        value = getattr(entry, col, None)
        snapshot[col] = value.isoformat() if hasattr(value, "isoformat") else value
    return snapshot


def _risk_fingerprint(entry: HiraEntry) -> tuple:
    """The risk-bearing state of an entry, for before/after comparison.

    Read AFTER the risk recomputation blocks have run, so it captures the
    derived result whether the residual was hand-picked or auto-calculated
    from controls.
    """
    return (
        entry.initialLikelihoodScore,
        entry.initialSeverityScore,
        entry.initialRiskLevel,
        entry.residualLikelihoodScore,
        entry.residualSeverityScore,
        entry.residualRiskLevel,
    )


def _material_scalars(entry: HiraEntry) -> dict:
    return {f: getattr(entry, f, None) for f in _MATERIAL_SCALAR_FIELDS}


def _classify_entry_change(
    entry: HiraEntry,
    before_fingerprint: tuple,
    before_scalars: dict,
    data: dict,
) -> tuple[bool, list[str]]:
    """Return (is_material, reasons) for an in-flight PATCH.

    `data` still holds the not-yet-applied scalar fields, so material scalars
    are compared payload-vs-current; risk fields are compared via the
    fingerprint because they were already applied by the recompute blocks.
    """
    reasons: list[str] = []

    after_fingerprint = _risk_fingerprint(entry)
    if after_fingerprint != before_fingerprint:
        labels = (
            "initialLikelihoodScore",
            "initialSeverityScore",
            "initialRiskLevel",
            "residualLikelihoodScore",
            "residualSeverityScore",
            "residualRiskLevel",
        )
        for label, old, new in zip(labels, before_fingerprint, after_fingerprint):
            if old != new:
                reasons.append(f"{label}: {old} → {new}")

    for field in _MATERIAL_SCALAR_FIELDS:
        if field in data and data[field] != before_scalars.get(field):
            reasons.append(f"{field}: {before_scalars.get(field)} → {data[field]}")

    return (bool(reasons), reasons)


def _clear_unacceptable_override(entry: HiraEntry) -> None:
    """Void any recorded Unacceptable-risk override. Called when the risk basis
    moves (material change) — the prior authorisation covered a different
    assessment and must not carry over."""
    entry.unacceptableOverrideById = None
    entry.unacceptableOverrideAt = None
    entry.unacceptableOverrideJustification = None
    entry.unacceptableOverrideExpiresAt = None


def _apply_reapproval(entry: HiraEntry, is_material: bool) -> bool:
    """Drop a live approval when a material change lands. Returns True if the
    entry's status actually moved, so callers can report it.

    Keyed on the ENTRY's approval state, not the study's: an entry can be
    APPROVED while its study is still DRAFT, and that approval is just as real,
    so a material edit must withdraw it either way (closes the study-status
    hole). A material change also voids any Unacceptable-risk override — the
    authorisation covered the previous assessment.
    """
    if not is_material:
        return False
    if entry.status not in _APPROVED_ENTRY_STATUSES:
        return False
    _clear_unacceptable_override(entry)
    entry.status = REAPPROVAL_STATUS
    return True


def _resolve_change_trigger(explicit: str | None, is_material: bool) -> str:
    """Honour an explicit trigger from a review/MOC-driven caller; otherwise
    stamp the version with the classification we just computed instead of the
    blanket 'CORRECTION' the editor used to hardcode."""
    if explicit:
        return explicit
    return MATERIAL_TRIGGER if is_material else MINOR_TRIGGER


def _control_effectiveness_fingerprint(controls) -> set:
    """Identity + effectiveness of each existing control. A changed set OR a
    changed effectiveness verdict is material; re-wording a description is not."""
    return {
        (c.hierarchy, (c.description or "").strip(), c.effectiveness)
        for c in controls
    }


def _recommended_status_fingerprint(controls) -> set:
    """Identity + status of each proposal. Status transitions (PROPOSED →
    IMPLEMENTED, → REJECTED) are material; editing the rationale is not."""
    return {
        (c.hierarchy, (c.description or "").strip(), c.status)
        for c in controls
    }


_PERMIT_TYPE_CODES = {t.value for t in PermitType}

# Hazard category → the permit type a drafted PTW should default to. Only used
# when the library hazard does not narrow it itself via permitTypes.
_CATEGORY_PERMIT_DEFAULT = {
    "confined_space": PermitType.CONFINED_SPACE.value,
    "fire_explosion": PermitType.HOT_WORK.value,
    "thermal": PermitType.HOT_WORK.value,
    "height": PermitType.WORK_AT_HEIGHT.value,
    "electrical": PermitType.ELECTRICAL_LOTO.value,
}


def _suggest_hazard_regulation(lib_hazard) -> tuple[str | None, str | None]:
    """Best-effort (instrument, section) citation for a hazard row, derived
    from the library hazard's regulatory columns.

    Only a starting point: it is written once when the row is created and is
    freely overridable afterwards. Ad-hoc hazards with no library row, and
    library rows with none of these columns populated, yield (None, None) —
    the user then types the citation themselves.

    Order reflects what an Indian manufacturing auditor asks for first.
    """
    if lib_hazard is None:
        return (None, None)
    if lib_hazard.factoriesActSection:
        return ("Factories Act 1948", lib_hazard.factoriesActSection)
    if lib_hazard.isStandard:
        return ("Indian Standard", lib_hazard.isStandard)
    if lib_hazard.oshaStandard:
        return ("OSHA", lib_hazard.oshaStandard)
    if lib_hazard.isoReference:
        return ("ISO", lib_hazard.isoReference)
    return (None, None)


def _hazard_fingerprint(hazards) -> set:
    """Hazard identity + the two fields that define what the hazard means for
    this activity. Adding, removing or re-scoping a hazard is material."""
    return {
        (
            h.hazardId,
            (h.contextualDescription or "").strip(),
            (h.consequence or "").strip(),
        )
        for h in hazards
    }


# ─────────────────────────────────────────────────────────────────────
# Risk matrix master
# ─────────────────────────────────────────────────────────────────────


@router.get("/risk-matrices", response_model=list[RiskMatrixOut])
async def list_risk_matrices(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RiskMatrixOut]:
    """Active matrices the caller can reference when creating studies.

    HIRA.READ is sufficient — matrices are masters, not records. Everyone
    who can read HIRA needs to read the matrices to render risk levels.
    """
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    stmt = (
        select(RiskMatrix)
        .where(RiskMatrix.isActive.is_(True))
        .options(
            selectinload(RiskMatrix.likelihoods),
            selectinload(RiskMatrix.severities),
            selectinload(RiskMatrix.cells),
        )
        .order_by(RiskMatrix.isDefault.desc(), RiskMatrix.name)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [RiskMatrixOut.model_validate(r) for r in rows]


@router.get("/risk-matrices/{matrix_id}", response_model=RiskMatrixOut)
async def get_risk_matrix(
    matrix_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RiskMatrixOut:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    stmt = (
        select(RiskMatrix)
        .where(RiskMatrix.id == matrix_id)
        .options(
            selectinload(RiskMatrix.likelihoods),
            selectinload(RiskMatrix.severities),
            selectinload(RiskMatrix.cells),
        )
    )
    matrix = (await db.execute(stmt)).scalar_one_or_none()
    if matrix is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Risk matrix not found")
    return RiskMatrixOut.model_validate(matrix)


# ─────────────────────────────────────────────────────────────────────
# Hazard + control libraries
# ─────────────────────────────────────────────────────────────────────


@router.get("/hazards", response_model=list[HiraHazardOut])
async def list_hazards(
    q: str | None = Query(None, description="Free-text search across name, description, code"),
    category: str | None = None,
    energy_form: str | None = None,
    limit: int = Query(50, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[HiraHazardOut]:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    stmt = select(HiraHazard).where(HiraHazard.isActive.is_(True))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                HiraHazard.name.ilike(like),
                HiraHazard.description.ilike(like),
                HiraHazard.code.ilike(like),
            )
        )
    if category:
        stmt = stmt.where(HiraHazard.category == category)
    if energy_form:
        stmt = stmt.where(HiraHazard.energyForm == energy_form)
    stmt = stmt.order_by(HiraHazard.category, HiraHazard.name).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [HiraHazardOut.model_validate(r) for r in rows]


@router.patch("/hazards/{hazard_id}/permit-gate", response_model=HiraHazardOut)
async def set_hazard_permit_gate(
    hazard_id: str,
    payload: HiraHazardPermitUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraHazardOut:
    """Flag a library hazard as permit-requiring, so entry rows built from it
    surface the Create-PTW prompt. Library master data — gated on
    HIRA.LIBRARY_MANAGE, the same permission the hazard configuration screen
    is already listed under."""
    check = await can(db, user.id, "HIRA.LIBRARY_MANAGE", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    hazard = await db.get(HiraHazard, hazard_id)
    if hazard is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hazard not found")

    unknown = [t for t in (payload.permitTypes or []) if t not in _PERMIT_TYPE_CODES]
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown permit type(s): {', '.join(unknown)}",
        )

    hazard.requiresPermit = payload.requiresPermit
    # Clearing the gate clears the type narrowing with it, so a re-enabled
    # hazard never inherits a stale permit-type list.
    hazard.permitTypes = (payload.permitTypes or None) if payload.requiresPermit else None
    await db.flush()
    await db.refresh(hazard)
    return HiraHazardOut.model_validate(hazard)


@router.get("/controls", response_model=list[HiraControlOut])
async def list_controls(
    hierarchy: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[HiraControlOut]:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    stmt = select(HiraControl).where(HiraControl.isActive.is_(True))
    if hierarchy:
        stmt = stmt.where(HiraControl.hierarchy == hierarchy)
    stmt = stmt.order_by(HiraControl.hierarchy, HiraControl.description).limit(200)
    rows = (await db.execute(stmt)).scalars().all()
    return [HiraControlOut.model_validate(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────
# Studies
# ─────────────────────────────────────────────────────────────────────


@router.get("/studies", response_model=HiraStudyListResponse)
async def list_studies(
    status_filter: str | None = Query(None, alias="status"),
    plant_id: str | None = None,
    department_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraStudyListResponse:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    accessible_plants = await get_accessible_plants(db, user.id)

    base = (
        select(HiraStudy)
        .options(
            selectinload(HiraStudy.plant),
            selectinload(HiraStudy.department),
            selectinload(HiraStudy.area),
        )
    )
    if accessible_plants is None:
        pass  # ALL_PLANTS
    elif len(accessible_plants) == 0:
        return HiraStudyListResponse(items=[], total=0, statusCounts={})
    else:
        base = base.where(HiraStudy.plantId.in_(accessible_plants))

    stmt = base
    if status_filter:
        stmt = stmt.where(HiraStudy.status == status_filter)
    if plant_id:
        stmt = stmt.where(HiraStudy.plantId == plant_id)
    if department_id:
        stmt = stmt.where(HiraStudy.departmentId == department_id)

    # Newest-created first — platform-wide register convention.
    stmt = stmt.order_by(HiraStudy.createdAt.desc(), HiraStudy.id.desc()).limit(200)
    rows = (await db.execute(stmt)).scalars().all()

    # Bulk-fetch team leader names + entry counts in one query each
    leader_ids = list({r.teamLeaderId for r in rows})
    leader_names: dict[str, str] = {}
    if leader_ids:
        leader_rows = (await db.execute(select(User.id, User.name).where(User.id.in_(leader_ids)))).all()
        leader_names = {uid: nm for uid, nm in leader_rows}

    entry_counts: dict[str, int] = {}
    if rows:
        ec = (
            await db.execute(
                select(HiraEntry.studyId, func.count(HiraEntry.id))
                .where(HiraEntry.studyId.in_([r.id for r in rows]))
                .where(HiraEntry.isCurrentVersion.is_(True))
                .group_by(HiraEntry.studyId)
            )
        ).all()
        entry_counts = {sid: int(cnt) for sid, cnt in ec}

    items = []
    for r in rows:
        d = HiraStudyListItem.model_validate(r).model_dump()
        d["plantName"] = r.plant.name if r.plant else None
        d["departmentName"] = r.department.name if r.department else None
        d["areaName"] = r.area.name if r.area else None
        d["teamLeaderName"] = leader_names.get(r.teamLeaderId)
        d["entryCount"] = entry_counts.get(r.id, 0)
        items.append(HiraStudyListItem(**d))

    # Status counts — across the user's accessible scope (not just the filtered slice)
    sc_q = select(HiraStudy.status, func.count(HiraStudy.id)).group_by(HiraStudy.status)
    if accessible_plants is not None:
        sc_q = sc_q.where(HiraStudy.plantId.in_(accessible_plants))
    sc_rows = (await db.execute(sc_q)).all()
    status_counts = {s: int(c) for s, c in sc_rows}

    return HiraStudyListResponse(items=items, total=len(items), statusCounts=status_counts)


@router.post("/studies", response_model=HiraStudyOut, status_code=status.HTTP_201_CREATED)
async def create_study(
    payload: HiraStudyCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraStudyOut:
    await require_permission_with_context(
        "HIRA.CREATE", user, db, plant_id=payload.plantId
    )

    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plant")

    matrix = await db.get(RiskMatrix, payload.riskMatrixId)
    if matrix is None or not matrix.isActive:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid risk matrix")

    leader = await db.get(User, payload.teamLeaderId)
    if leader is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="teamLeaderId does not exist")

    # Number generation — HIRA-YYYY-PLT-NNN (MAX-based, gap-safe)
    max_stmt = select(func.max(HiraStudy.number)).where(HiraStudy.plantId == payload.plantId)
    last_number = (await db.execute(max_stmt)).scalar_one_or_none()
    if last_number:
        try:
            last_seq = int(last_number.rsplit("-", 1)[-1])
        except (ValueError, AttributeError):
            last_seq = 0
    else:
        last_seq = 0
    number = f"HIRA-{datetime.now(timezone.utc).year}-{plant.code}-{last_seq + 1:03d}"

    study = HiraStudy(
        number=number,
        plantId=payload.plantId,
        departmentId=payload.departmentId,
        areaId=payload.areaId,
        scopeType=payload.scopeType,
        activityIds=payload.activityIds,
        equipmentIds=payload.equipmentIds,
        processCode=payload.processCode,
        title=payload.title,
        description=payload.description,
        riskMatrixId=payload.riskMatrixId,
        teamLeaderId=payload.teamLeaderId,
        status="DRAFT",
        targetCompletionDate=payload.targetCompletionDate,
        reviewFrequency=payload.reviewFrequency,
        customReviewMonths=payload.customReviewMonths,
        applicableRegulations=payload.applicableRegulations,
        regulatoryReviewRequired=payload.regulatoryReviewRequired,
        createdById=user.id,
    )
    db.add(study)
    await db.flush()

    # Team members
    for tm in payload.team:
        db.add(
            HiraStudyTeamMember(
                studyId=study.id,
                userId=tm.userId,
                teamRole=tm.teamRole,
                department=tm.department,
            )
        )
    await db.flush()

    # Workflow init is intentionally deferred to Phase 4. Studies stay in
    # DRAFT until the workflow definition is seeded and submission routes
    # through the engine.

    await db.refresh(study)
    # Re-load with team eagerly so the response has them populated
    stmt = (
        select(HiraStudy)
        .where(HiraStudy.id == study.id)
        .options(selectinload(HiraStudy.team))
    )
    study = (await db.execute(stmt)).scalar_one()
    return HiraStudyOut.model_validate(study)


@router.get("/studies/{study_id}", response_model=HiraStudyOut)
async def get_study(
    study_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraStudyOut:
    stmt = (
        select(HiraStudy)
        .where(HiraStudy.id == study_id)
        .options(selectinload(HiraStudy.team))
    )
    study = (await db.execute(stmt)).scalar_one_or_none()
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")

    check = await can(
        db,
        user.id,
        "HIRA.READ",
        PermissionContext(record_id=study.id, plant_id=study.plantId, record={"createdById": study.createdById, "teamLeaderId": study.teamLeaderId}),
    )
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    return HiraStudyOut.model_validate(study)


@router.get("/studies/{study_id}/detail", response_model=HiraStudyDetailResponse)
async def get_study_detail(
    study_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraStudyDetailResponse:
    """Composite endpoint serving the study detail page in one round-trip.

    Returns study + team + matrix + entries + denormalised display names so
    the Next.js page renders without touching Prisma directly.
    """
    stmt = (
        select(HiraStudy)
        .where(HiraStudy.id == study_id)
        .options(
            selectinload(HiraStudy.team),
            selectinload(HiraStudy.plant),
            selectinload(HiraStudy.department),
            selectinload(HiraStudy.area),
        )
    )
    study = (await db.execute(stmt)).scalar_one_or_none()
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")

    check = await can(
        db,
        user.id,
        "HIRA.READ",
        PermissionContext(
            record_id=study.id,
            plant_id=study.plantId,
            record={"createdById": study.createdById, "teamLeaderId": study.teamLeaderId},
        ),
    )
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    # Matrix
    matrix = await db.get(RiskMatrix, study.riskMatrixId)

    # Entries (current versions only) with hazard / control counts
    entry_rows = (
        await db.execute(
            select(HiraEntry)
            .where(HiraEntry.studyId == study_id)
            .where(HiraEntry.isCurrentVersion.is_(True))
            .order_by(HiraEntry.sequenceNumber.asc())
        )
    ).scalars().all()
    entry_ids = [e.id for e in entry_rows]
    hazard_counts: dict[str, int] = {}
    ec_counts: dict[str, int] = {}
    rc_counts: dict[str, int] = {}
    if entry_ids:
        hc = (
            await db.execute(
                select(HiraEntryHazard.entryId, func.count(HiraEntryHazard.id))
                .where(HiraEntryHazard.entryId.in_(entry_ids))
                .group_by(HiraEntryHazard.entryId)
            )
        ).all()
        hazard_counts = {eid: int(c) for eid, c in hc}
        ec = (
            await db.execute(
                select(HiraEntryControl.entryId, func.count(HiraEntryControl.id))
                .where(HiraEntryControl.entryId.in_(entry_ids))
                .group_by(HiraEntryControl.entryId)
            )
        ).all()
        ec_counts = {eid: int(c) for eid, c in ec}
        rc = (
            await db.execute(
                select(HiraEntryRecommendedControl.entryId, func.count(HiraEntryRecommendedControl.id))
                .where(HiraEntryRecommendedControl.entryId.in_(entry_ids))
                .group_by(HiraEntryRecommendedControl.entryId)
            )
        ).all()
        rc_counts = {eid: int(c) for eid, c in rc}

    entries_payload = []
    for e in entry_rows:
        d = HiraEntryListItem.model_validate(e).model_dump()
        d["hazardCount"] = hazard_counts.get(e.id, 0)
        d["existingControlCount"] = ec_counts.get(e.id, 0)
        d["recommendedControlCount"] = rc_counts.get(e.id, 0)
        entries_payload.append(HiraEntryListItem(**d))

    # User name lookups
    user_ids = (
        {study.teamLeaderId, study.createdById}
        | ({study.approvedById} if study.approvedById else set())
        | {m.userId for m in study.team}
    )
    name_rows = (await db.execute(select(User.id, User.name).where(User.id.in_(user_ids)))).all()
    names = {uid: nm for uid, nm in name_rows}

    return HiraStudyDetailResponse(
        study=HiraStudyOut.model_validate(study),
        entries=entries_payload,
        plantName=study.plant.name if study.plant else None,
        departmentName=study.department.name if study.department else None,
        areaName=study.area.name if study.area else None,
        teamLeaderName=names.get(study.teamLeaderId),
        approvedByName=names.get(study.approvedById) if study.approvedById else None,
        createdByName=names.get(study.createdById),
        teamMemberNames={m.userId: names.get(m.userId) or "" for m in study.team},
        riskMatrix=(
            {
                "id": matrix.id,
                "code": matrix.code,
                "name": matrix.name,
                "likelihoodLevels": matrix.likelihoodLevels,
                "severityLevels": matrix.severityLevels,
                "acceptableResidual": matrix.acceptableResidual,
                "alarpBands": matrix.alarpBands or DEFAULT_ALARP_BANDS,
                "controlHierarchyEnforced": matrix.controlHierarchyEnforced,
            }
            if matrix
            else None
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# Entries
# ─────────────────────────────────────────────────────────────────────


@router.get("/studies/{study_id}/entries", response_model=HiraEntryListResponse)
async def list_entries(
    study_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraEntryListResponse:
    # Re-use the study read check so list inherits its scope
    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    check = await can(
        db,
        user.id,
        "HIRA.READ",
        PermissionContext(record_id=study.id, plant_id=study.plantId),
    )
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    stmt = (
        select(HiraEntry)
        .where(HiraEntry.studyId == study_id)
        .where(HiraEntry.isCurrentVersion.is_(True))
        .order_by(HiraEntry.sequenceNumber.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return HiraEntryListResponse(
        items=[HiraEntryListItem.model_validate(r) for r in rows],
        total=len(rows),
    )


def _compute_risk(
    matrix_cells: list[RiskMatrixCell],
    likelihood_score: int,
    severity_score: int,
) -> tuple[int, str, str]:
    """Return (riskScore, riskLevel, colorHex) from the matrix cell.

    Falls back to closest-cell-by-score proximity if no exact cell matches
    (defensive — the seed creates all cells).
    """
    for c in matrix_cells:
        if c.likelihoodScore == likelihood_score and c.severityScore == severity_score:
            return c.riskScore, c.riskLevel, c.colorHex
    score = likelihood_score * severity_score
    # Fallback: use closest cell by score proximity
    cells = matrix_cells
    closest = min(cells, key=lambda c: abs(c.riskScore - score)) if cells else None
    if closest:
        return closest.riskScore, closest.riskLevel, closest.colorHex
    # Last resort fallback
    if score >= 15:
        return score, "CRITICAL", "#dc2626"
    elif score >= 8:
        return score, "HIGH", "#ea580c"
    elif score >= 4:
        return score, "MODERATE", "#ca8a04"
    return score, "LOW", "#16a34a"


@router.post(
    "/studies/{study_id}/entries",
    response_model=HiraEntryOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_entry(
    study_id: str,
    payload: HiraEntryCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraEntryOut:
    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    if study.status not in ("DRAFT", "IN_PROGRESS"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot add entries to a study in status {study.status}. Initiate a review to revise.",
        )

    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=study.plantId, record_id=study.id
    )

    # Every hazard on a NEW entry must state its consequence — validated before
    # anything is written so a rejected entry leaves no partial row behind.
    blank_consequences = [
        h.hazardId for h in payload.hazards if not (h.consequence or "").strip()
    ]
    if blank_consequences:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Consequence is required for each hazard. Missing for hazardId(s): "
            + ", ".join(blank_consequences),
        )

    likelihood = await db.get(RiskMatrixLikelihood, payload.initialLikelihoodId)
    severity = await db.get(RiskMatrixSeverity, payload.initialSeverityId)
    if likelihood is None or severity is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid likelihood or severity id")
    if likelihood.matrixId != study.riskMatrixId or severity.matrixId != study.riskMatrixId:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Likelihood/severity must belong to the study's risk matrix",
        )

    # Load cells once to compute the initial risk level
    cells_stmt = select(RiskMatrixCell).where(RiskMatrixCell.matrixId == study.riskMatrixId)
    cells = list((await db.execute(cells_stmt)).scalars().all())
    risk_score, risk_level, risk_color = _compute_risk(cells, likelihood.score, severity.score)
    matrix = await db.get(RiskMatrix, study.riskMatrixId)
    initial_region = _alarp_region(risk_level, matrix.alarpBands if matrix else None)

    # Auto-assign sequenceNumber atomically
    seq_result = await db.execute(
        select(func.coalesce(func.max(HiraEntry.sequenceNumber), 0) + 1).where(HiraEntry.studyId == study_id)
    )
    next_seq = seq_result.scalar_one()

    entry = HiraEntry(
        studyId=study_id,
        sequenceNumber=next_seq,
        groupLabel=payload.groupLabel,
        activityDescription=payload.activityDescription,
        areaId=payload.areaId,
        subLocation=payload.subLocation,
        routine=payload.routine,
        frequency=payload.frequency,
        typicalDurationMin=payload.typicalDurationMin,
        personsEmployees=payload.personsEmployees,
        personsContractors=payload.personsContractors,
        personsVisitors=payload.personsVisitors,
        personsPublic=payload.personsPublic,
        affectedPersonGroups=payload.affectedPersonGroups,
        equipmentUsed=payload.equipmentUsed,
        materialsUsed=payload.materialsUsed,
        energySourcesPresent=payload.energySourcesPresent,
        initialLikelihoodId=payload.initialLikelihoodId,
        initialLikelihoodScore=likelihood.score,
        initialLikelihoodRationale=payload.initialLikelihoodRationale,
        initialSeverityId=payload.initialSeverityId,
        initialSeverityScore=severity.score,
        initialSeverityRationale=payload.initialSeverityRationale,
        initialRiskScore=risk_score,
        initialRiskLevel=risk_level,
        initialRiskColor=risk_color,
        initialAlarpRegion=initial_region,
        status="DRAFT",
        versionNumber=1,
        isCurrentVersion=True,
        createdById=user.id,
    )
    db.add(entry)
    await db.flush()
    await db.refresh(entry)

    # Save hazards with their consequence + regulatory citation. Where the
    # client left the citation blank we seed it from the library hazard, so a
    # standard hazard arrives already traceable; the user can overwrite it.
    lib_by_id: dict[str, HiraHazard] = {}
    if payload.hazards:
        lib_rows = (
            await db.execute(
                select(HiraHazard).where(
                    HiraHazard.id.in_([h.hazardId for h in payload.hazards])
                )
            )
        ).scalars().all()
        lib_by_id = {row.id: row for row in lib_rows}

    for idx, h in enumerate(payload.hazards):
        suggested_ref, suggested_section = _suggest_hazard_regulation(lib_by_id.get(h.hazardId))
        db.add(
            HiraEntryHazard(
                entryId=entry.id,
                hazardId=h.hazardId,
                contextualDescription=h.contextualDescription,
                consequence=(h.consequence or "").strip() or None,
                regulationRef=(h.regulationRef or "").strip() or suggested_ref,
                regulationSection=(h.regulationSection or "").strip() or suggested_section,
                sortOrder=idx,
            )
        )
    if payload.hazards:
        await db.flush()

    # Re-load with children eagerly
    stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry.id)
        .options(
            selectinload(HiraEntry.hazards).selectinload(HiraEntryHazard.hazard),
            selectinload(HiraEntry.existingControls),
            selectinload(HiraEntry.recommendedControls),
            selectinload(HiraEntry.regulationRefs),
            # HiraEntryOut declares `capas`; without it here Pydantic lazy-loads
            # on an async session and the create 500s AFTER the row was written.
            selectinload(HiraEntry.capas),
        )
    )
    entry = (await db.execute(stmt)).scalar_one()

    # Same hazard denormalisation get_entry does, so the created entry comes
    # back in the shape the editor expects.
    out = HiraEntryOut.model_validate(entry).model_dump()
    for i, hz in enumerate(out["hazards"]):
        src_hz = entry.hazards[i]
        if src_hz.hazard is not None:
            hz["hazardCode"] = src_hz.hazard.code
            hz["hazardCategory"] = src_hz.hazard.category
            hz["hazardName"] = src_hz.hazard.name
            hz["hazardRequiresPermit"] = bool(src_hz.hazard.requiresPermit)
            hz["hazardPermitTypes"] = src_hz.hazard.permitTypes or []
    return HiraEntryOut(**out)


@router.get("/entries/{entry_id}", response_model=HiraEntryOut)
async def get_entry(
    entry_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraEntryOut:
    stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry_id)
        .options(
            selectinload(HiraEntry.hazards).selectinload(HiraEntryHazard.hazard),
            selectinload(HiraEntry.existingControls),
            selectinload(HiraEntry.recommendedControls),
            selectinload(HiraEntry.regulationRefs),
            selectinload(HiraEntry.capas),
            selectinload(HiraEntry.study),
        )
    )
    entry = (await db.execute(stmt)).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")

    check = await can(
        db,
        user.id,
        "HIRA.READ",
        PermissionContext(record_id=entry.id, plant_id=entry.study.plantId),
    )
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    # Denormalise hazard names so the editor doesn't need a second lookup
    out = HiraEntryOut.model_validate(entry).model_dump()
    for i, hz in enumerate(out["hazards"]):
        src_hz = entry.hazards[i]
        if src_hz.hazard is not None:
            hz["hazardCode"] = src_hz.hazard.code
            hz["hazardCategory"] = src_hz.hazard.category
            hz["hazardName"] = src_hz.hazard.name
            hz["hazardRequiresPermit"] = bool(src_hz.hazard.requiresPermit)
            hz["hazardPermitTypes"] = src_hz.hazard.permitTypes or []
    return HiraEntryOut(**out)


# ═════════════════════════════════════════════════════════════════════
# Write endpoints — Phase: pure 3-tier migration
# ═════════════════════════════════════════════════════════════════════


@router.patch("/studies/{study_id}", response_model=HiraStudyOut)
async def update_study(
    study_id: str,
    payload: HiraStudyUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraStudyOut:
    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")

    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=study.plantId, record_id=study.id
    )

    # Once APPROVED/ACTIVE, only specific fields editable. Substantive edits
    # require a major-revision review cycle (mirrors the Next.js route logic).
    editable_in_active = {"nextScheduledReviewDate"}
    if study.status in ("APPROVED", "ACTIVE"):
        for field, value in payload.model_dump(exclude_unset=True).items():
            if field not in editable_in_active:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Study is approved/active. Substantive edits require a review cycle.",
                )

    PROTECTED_FIELDS = {"status", "approvedAt", "approvedById", "effectiveFrom"}
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        if k in PROTECTED_FIELDS:
            continue
        setattr(study, k, v)
    study.updatedById = user.id

    await db.flush()
    await db.refresh(study)

    stmt = (
        select(HiraStudy).where(HiraStudy.id == study.id).options(selectinload(HiraStudy.team))
    )
    study = (await db.execute(stmt)).scalar_one()
    return HiraStudyOut.model_validate(study)


@router.delete("/studies/{study_id}", response_model=HiraStudyOut)
async def archive_study(
    study_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraStudyOut:
    """Soft archive — statutory record, never DELETE."""
    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")

    await require_permission_with_context(
        "HIRA.DELETE", user, db, plant_id=study.plantId, record_id=study.id
    )

    study.status = "ARCHIVED"
    study.updatedById = user.id
    await db.flush()
    await db.refresh(study)

    stmt = (
        select(HiraStudy).where(HiraStudy.id == study.id).options(selectinload(HiraStudy.team))
    )
    study = (await db.execute(stmt)).scalar_one()
    return HiraStudyOut.model_validate(study)




@router.post("/studies/{study_id}/submit", response_model=HiraStudyOut)
async def submit_study(
    study_id: str,
    payload: HiraStudyTransitionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HiraStudyOut:
    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=study.plantId, record_id=study.id
    )
    TRANSITIONS = {"DRAFT": "IN_PROGRESS", "IN_PROGRESS": "TEAM_REVIEW", "TEAM_REVIEW": "APPROVAL_PENDING"}
    if study.status not in TRANSITIONS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot submit study in status {study.status}")
    study.status = TRANSITIONS[study.status]
    study.updatedById = user.id
    await db.flush()
    await db.refresh(study)
    return HiraStudyOut.model_validate(study)


@router.post("/studies/{study_id}/approve", response_model=HiraStudyOut)
async def approve_study(
    study_id: str,
    payload: HiraStudyTransitionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HiraStudyOut:
    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await require_permission_with_context(
        "HIRA.APPROVE", user, db, plant_id=study.plantId, record_id=study.id
    )
    if study.status != "APPROVAL_PENDING":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Study must be APPROVAL_PENDING to approve, current: {study.status}")
    now = datetime.now(timezone.utc)
    study.status = "APPROVED"
    study.approvedById = user.id
    study.approvedAt = now
    study.effectiveFrom = now
    study.updatedById = user.id
    await db.flush()
    await db.refresh(study)
    return HiraStudyOut.model_validate(study)


@router.post("/studies/{study_id}/activate", response_model=HiraStudyOut)
async def activate_study(
    study_id: str,
    payload: HiraStudyTransitionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HiraStudyOut:
    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await require_permission_with_context(
        "HIRA.APPROVE", user, db, plant_id=study.plantId, record_id=study.id
    )
    if study.status != "APPROVED":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Study must be APPROVED to activate, current: {study.status}")
    study.status = "ACTIVE"
    study.updatedById = user.id
    await db.flush()
    await db.refresh(study)
    return HiraStudyOut.model_validate(study)

def _derive_level(score: int) -> str:
    if score >= 15:
        return "CRITICAL"
    if score >= 8:
        return "HIGH"
    if score >= 4:
        return "MODERATE"
    return "LOW"


def _acceptability_ok(level: str, threshold: str) -> bool:
    order = ["LOW", "MODERATE", "HIGH", "CRITICAL"]
    return order.index(level) <= order.index(threshold)


# ─────────────────────────────────────────────────────────────────────
# Residual-from-controls auto-calculation
#
# When an entry is in auto mode (residualAutoCalculated is True), the residual
# likelihood/severity are DERIVED from the existing controls rather than being
# hand-picked on the matrix. Each control removes a base number of scale-steps
# from likelihood and/or severity depending on where it sits in the hierarchy,
# scaled by how effective it is. Multiple controls on one axis get diminishing
# returns (the strongest counts in full, the rest at half). Residual is floored
# at 1 and can never exceed the initial score (controls can't increase risk).
#
# The frontend mirrors this exactly for a live preview — keep the two in sync:
# see suggestResidualScores() in
# safeops_360/src/app/(dashboard)/hira/[id]/entries/[entryId]/entry-editor.tsx
# ─────────────────────────────────────────────────────────────────────

# (likelihoodStep, severityStep) removed at full effectiveness, per hierarchy.
CONTROL_REDUCTION: dict[str, tuple[int, int]] = {
    "ELIMINATION": (4, 3),
    "SUBSTITUTION": (2, 2),
    "ENGINEERING": (2, 1),
    "ADMINISTRATIVE": (1, 0),
    "PPE": (0, 1),
}
EFFECTIVENESS_FACTOR: dict[str, float] = {
    "EFFECTIVE": 1.0,
    "PARTIALLY_EFFECTIVE": 0.6,
    "NOT_VERIFIED": 0.3,
    "INEFFECTIVE": 0.0,
}
# A control whose effectiveness hasn't been recorded yet is credited as
# partially effective so simply adding it visibly moves the residual.
DEFAULT_EFFECTIVENESS_FACTOR = 0.6


def _axis_reduction(contribs: list[float]) -> int:
    """Diminishing-returns aggregate of per-control reductions on one axis:
    the strongest control counts in full, each additional one at half weight."""
    xs = sorted((c for c in contribs if c > 0), reverse=True)
    if not xs:
        return 0
    total = xs[0] + 0.5 * sum(xs[1:])
    return int(total + 0.5)  # round half up (matches JS Math.floor(x + 0.5))


def _suggest_residual_scores(
    initial_l: int, initial_s: int, controls: list[HiraEntryControl]
) -> tuple[int, int]:
    """Derive (residualLikelihoodScore, residualSeverityScore) from the control set."""
    l_contribs: list[float] = []
    s_contribs: list[float] = []
    for c in controls:
        base = CONTROL_REDUCTION.get((c.hierarchy or "").upper())
        if not base:
            continue
        if c.effectiveness:
            factor = EFFECTIVENESS_FACTOR.get(c.effectiveness, DEFAULT_EFFECTIVENESS_FACTOR)
        else:
            factor = DEFAULT_EFFECTIVENESS_FACTOR
        l_contribs.append(base[0] * factor)
        s_contribs.append(base[1] * factor)
    rl = max(1, initial_l - _axis_reduction(l_contribs))
    rs = max(1, initial_s - _axis_reduction(s_contribs))
    return rl, rs


async def _load_matrix_scales(
    db: AsyncSession, matrix_id: str
) -> tuple[list[RiskMatrixCell], dict[int, RiskMatrixLikelihood], dict[int, RiskMatrixSeverity]]:
    """Load the cells + score→row maps for a matrix, used to map derived scores
    back to likelihood/severity ids and a risk cell."""
    cells = list(
        (await db.execute(select(RiskMatrixCell).where(RiskMatrixCell.matrixId == matrix_id))).scalars().all()
    )
    liks = {
        l.score: l
        for l in (
            await db.execute(select(RiskMatrixLikelihood).where(RiskMatrixLikelihood.matrixId == matrix_id))
        ).scalars().all()
    }
    sevs = {
        s.score: s
        for s in (
            await db.execute(select(RiskMatrixSeverity).where(RiskMatrixSeverity.matrixId == matrix_id))
        ).scalars().all()
    }
    return cells, liks, sevs


def _apply_residual_from_controls(
    entry: HiraEntry,
    controls: list[HiraEntryControl],
    cells: list[RiskMatrixCell],
    likelihoods_by_score: dict[int, RiskMatrixLikelihood],
    severities_by_score: dict[int, RiskMatrixSeverity],
) -> None:
    """Compute the derived residual and set the residual L/S (+ score/level/color)
    on the entry. No-op if the derived scores can't be mapped to matrix rows."""
    rl, rs = _suggest_residual_scores(
        entry.initialLikelihoodScore, entry.initialSeverityScore, controls
    )
    lk = likelihoods_by_score.get(rl)
    sv = severities_by_score.get(rs)
    if lk is None or sv is None:
        return
    cell = next((c for c in cells if c.likelihoodScore == rl and c.severityScore == rs), None)
    entry.residualLikelihoodId = lk.id
    entry.residualLikelihoodScore = rl
    entry.residualSeverityId = sv.id
    entry.residualSeverityScore = rs
    entry.residualRiskScore = cell.riskScore if cell else rl * rs
    entry.residualRiskLevel = cell.riskLevel if cell else _derive_level(rl * rs)
    entry.residualRiskColor = cell.colorHex if cell else None


@router.patch("/entries/{entry_id}", response_model=HiraEntryOut)
async def update_entry(
    entry_id: str,
    payload: HiraEntryUpdate,
    change_reason: str | None = Query(None, alias="changeReason"),
    # No default: an explicit trigger (SCHEDULED_REVIEW, INCIDENT_REVIEW, MOC …)
    # is honoured, and its absence means "classify this edit for me". The old
    # "CORRECTION" default made every version indistinguishable.
    change_trigger: str | None = Query(None, alias="changeTrigger"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraEntryOut:
    stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry_id)
        .options(
            selectinload(HiraEntry.study),
            selectinload(HiraEntry.existingControls),
        )
    )
    entry = (await db.execute(stmt)).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")

    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=entry.study.plantId, record_id=entry.id
    )

    # Capture the risk-bearing state BEFORE any recompute mutates the entry.
    # (The versioning block's "pre-edit snapshot" re-selects the same
    # identity-mapped object, so it is already post-mutation for these fields
    # and cannot be used for the comparison.)
    before_fingerprint = _risk_fingerprint(entry)
    before_scalars = _material_scalars(entry)

    data = payload.model_dump(exclude_unset=True)

    # Matrix drives risk levels, the ALARP band map, and the legacy per-routine
    # acceptable-residual threshold. Loaded once and reused below.
    matrix = await db.get(RiskMatrix, entry.study.riskMatrixId)
    alarp_bands = matrix.alarpBands if matrix else None

    # If initial L/S changed, recompute risk
    if "initialLikelihoodId" in data or "initialSeverityId" in data:
        l_id = data.get("initialLikelihoodId", entry.initialLikelihoodId)
        s_id = data.get("initialSeverityId", entry.initialSeverityId)
        l = await db.get(RiskMatrixLikelihood, l_id)
        s = await db.get(RiskMatrixSeverity, s_id)
        if not l or l.matrixId != entry.study.riskMatrixId:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid initialLikelihoodId for matrix")
        if not s or s.matrixId != entry.study.riskMatrixId:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid initialSeverityId for matrix")
        cell = (
            await db.execute(
                select(RiskMatrixCell)
                .where(RiskMatrixCell.matrixId == entry.study.riskMatrixId)
                .where(RiskMatrixCell.likelihoodScore == l.score)
                .where(RiskMatrixCell.severityScore == s.score)
            )
        ).scalar_one_or_none()
        entry.initialLikelihoodId = l.id
        entry.initialLikelihoodScore = l.score
        entry.initialSeverityId = s.id
        entry.initialSeverityScore = s.score
        entry.initialRiskScore = cell.riskScore if cell else l.score * s.score
        entry.initialRiskLevel = cell.riskLevel if cell else _derive_level(l.score * s.score)
        entry.initialRiskColor = cell.colorHex if cell else None
        entry.initialAlarpRegion = _alarp_region(entry.initialRiskLevel, alarp_bands)
        data.pop("initialLikelihoodId", None)
        data.pop("initialSeverityId", None)

    # Residual mode — auto-calculated from controls, or manually overridden.
    if "residualAutoCalculated" in data:
        entry.residualAutoCalculated = data.pop("residualAutoCalculated")
    auto_residual = entry.residualAutoCalculated is True

    if auto_residual:
        # Derive the residual from the entry's existing controls. Any residual
        # L/S the client sent is ignored while in auto mode.
        cells, liks_by_score, sevs_by_score = await _load_matrix_scales(db, entry.study.riskMatrixId)
        _apply_residual_from_controls(
            entry, list(entry.existingControls), cells, liks_by_score, sevs_by_score
        )
        data.pop("residualLikelihoodId", None)
        data.pop("residualSeverityId", None)
    # Manual residual recompute
    elif "residualLikelihoodId" in data or "residualSeverityId" in data:
        # Only clear all residual fields if BOTH are explicitly set to None
        both_null = (
            "residualLikelihoodId" in data and data["residualLikelihoodId"] is None
            and "residualSeverityId" in data and data["residualSeverityId"] is None
        )
        l_id = data.get("residualLikelihoodId") if "residualLikelihoodId" in data else entry.residualLikelihoodId
        s_id = data.get("residualSeverityId") if "residualSeverityId" in data else entry.residualSeverityId

        if both_null:
            entry.residualLikelihoodId = None
            entry.residualLikelihoodScore = None
            entry.residualSeverityId = None
            entry.residualSeverityScore = None
            entry.residualRiskScore = None
            entry.residualRiskLevel = None
            entry.residualRiskColor = None
            entry.residualAcceptable = None
            # ALARP is moot with no residual — clear region, status and sign-off.
            entry.residualAlarpRegion = None
            entry.alarpStatus = None
            entry.alarpDemonstratedById = None
            entry.alarpDemonstratedAt = None
        elif l_id and s_id:
            l = await db.get(RiskMatrixLikelihood, l_id)
            s = await db.get(RiskMatrixSeverity, s_id)
            if not l or l.matrixId != entry.study.riskMatrixId:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid residualLikelihoodId")
            if not s or s.matrixId != entry.study.riskMatrixId:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid residualSeverityId")
            cell = (
                await db.execute(
                    select(RiskMatrixCell)
                    .where(RiskMatrixCell.matrixId == entry.study.riskMatrixId)
                    .where(RiskMatrixCell.likelihoodScore == l.score)
                    .where(RiskMatrixCell.severityScore == s.score)
                )
            ).scalar_one_or_none()
            residual_level = cell.riskLevel if cell else _derive_level(l.score * s.score)
            entry.residualLikelihoodId = l.id
            entry.residualLikelihoodScore = l.score
            entry.residualSeverityId = s.id
            entry.residualSeverityScore = s.score
            entry.residualRiskScore = cell.riskScore if cell else l.score * s.score
            entry.residualRiskLevel = residual_level
            entry.residualRiskColor = cell.colorHex if cell else None
            # residualAcceptable + ALARP region/status computed post-setattr,
            # once the ALARP demonstration fields in the payload are applied.
        data.pop("residualLikelihoodId", None)
        data.pop("residualSeverityId", None)

    # Target (forecast) risk — the projected residual once the recommended
    # additional controls land. Hand-picked on the matrix; its ALARP region is
    # computed so the register/editor can show the Initial→Residual→Target path.
    if "targetLikelihoodId" in data or "targetSeverityId" in data:
        both_null = (
            "targetLikelihoodId" in data and data["targetLikelihoodId"] is None
            and "targetSeverityId" in data and data["targetSeverityId"] is None
        )
        t_l_id = data.get("targetLikelihoodId") if "targetLikelihoodId" in data else entry.targetLikelihoodId
        t_s_id = data.get("targetSeverityId") if "targetSeverityId" in data else entry.targetSeverityId
        if both_null:
            entry.targetLikelihoodId = None
            entry.targetLikelihoodScore = None
            entry.targetSeverityId = None
            entry.targetSeverityScore = None
            entry.targetRiskScore = None
            entry.targetRiskLevel = None
            entry.targetRiskColor = None
            entry.targetAlarpRegion = None
        elif t_l_id and t_s_id:
            tl = await db.get(RiskMatrixLikelihood, t_l_id)
            ts = await db.get(RiskMatrixSeverity, t_s_id)
            if not tl or tl.matrixId != entry.study.riskMatrixId:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid targetLikelihoodId")
            if not ts or ts.matrixId != entry.study.riskMatrixId:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid targetSeverityId")
            tcell = (
                await db.execute(
                    select(RiskMatrixCell)
                    .where(RiskMatrixCell.matrixId == entry.study.riskMatrixId)
                    .where(RiskMatrixCell.likelihoodScore == tl.score)
                    .where(RiskMatrixCell.severityScore == ts.score)
                )
            ).scalar_one_or_none()
            t_level = tcell.riskLevel if tcell else _derive_level(tl.score * ts.score)
            entry.targetLikelihoodId = tl.id
            entry.targetLikelihoodScore = tl.score
            entry.targetSeverityId = ts.id
            entry.targetSeverityScore = ts.score
            entry.targetRiskScore = tcell.riskScore if tcell else tl.score * ts.score
            entry.targetRiskLevel = t_level
            entry.targetRiskColor = tcell.colorHex if tcell else None
            entry.targetAlarpRegion = _alarp_region(t_level, alarp_bands)
        data.pop("targetLikelihoodId", None)
        data.pop("targetSeverityId", None)

    # Classify the edit before versioning — the verdict decides both the
    # version's changeTrigger and whether the approval survives.
    is_material, material_reasons = _classify_entry_change(
        entry, before_fingerprint, before_scalars, data
    )

    # Versioning — if approved/active study OR not v1, snapshot before mutating
    needs_version = entry.study.status in ("APPROVED", "ACTIVE") or entry.versionNumber > 1
    if needs_version:
        if not change_reason:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "changeReason query param is required when editing entries on approved/active studies",
            )
        # Snapshot the current entry (pre-edit) state
        snapshot_stmt = (
            select(HiraEntry)
            .where(HiraEntry.id == entry.id)
            .options(
                selectinload(HiraEntry.hazards),
                selectinload(HiraEntry.existingControls),
                selectinload(HiraEntry.recommendedControls),
                selectinload(HiraEntry.regulationRefs),
            )
        )
        snap_entry = (await db.execute(snapshot_stmt)).scalar_one()
        snapshot_dict = {"entry": _entry_snapshot(snap_entry)}
        changes = []
        for field, new_val in data.items():
            old_val = getattr(entry, field, None)
            if old_val != new_val:
                changes.append({
                    "field": field,
                    "from": str(old_val) if old_val is not None else None,
                    "to": str(new_val) if new_val is not None else None,
                })
        if material_reasons:
            changes.append({"materiality": MATERIAL_TRIGGER, "signals": material_reasons})
        archive_number = await _archive_version_number(db, entry)
        db.add(
            HiraVersion(
                entryId=entry.id,
                versionNumber=archive_number,
                snapshot=snapshot_dict,
                changes=changes,
                changeReason=change_reason,
                changeTrigger=_resolve_change_trigger(change_trigger, is_material),
                createdById=user.id,
            )
        )
        entry.versionNumber = archive_number + 1

    # Apply remaining scalar fields (protected fields cannot be patched via PATCH)
    ENTRY_PROTECTED_FIELDS = {"status", "versionNumber", "isCurrentVersion"}
    for k, v in data.items():
        if k in ENTRY_PROTECTED_FIELDS:
            continue
        setattr(entry, k, v)

    # Recompute ALARP banding + acceptability from the now-current residual
    # level, routine and demonstration fields. Runs whenever a residual exists
    # (covers residual-changed, routine-changed, and ALARP-fields-only edits).
    if entry.residualRiskLevel:
        region = _alarp_region(entry.residualRiskLevel, alarp_bands)
        threshold = (matrix.acceptableResidual or {}).get((entry.routine or "ROUTINE").lower()) if matrix else None
        _evaluate_alarp(entry, region, threshold, user.id)
    else:
        entry.residualAlarpRegion = None
        entry.alarpStatus = None

    # A material change invalidates the approval it was made under. `status` is
    # in ENTRY_PROTECTED_FIELDS so a client can never drive this itself — the
    # transition is server-side only, and re-approval goes back through
    # POST /entries/{id}/approve under HIRA.APPROVE.
    _apply_reapproval(entry, is_material)

    entry.updatedById = user.id

    await db.flush()
    await db.refresh(entry)

    refresh_stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry.id)
        .options(
            selectinload(HiraEntry.hazards).selectinload(HiraEntryHazard.hazard),
            selectinload(HiraEntry.existingControls),
            selectinload(HiraEntry.recommendedControls),
            selectinload(HiraEntry.regulationRefs),
            # HiraEntryOut declares `capas`, so omitting it here left Pydantic
            # to lazy-load on an async session — MissingGreenlet, i.e. the PATCH
            # response blew up after the write had already been flushed.
            selectinload(HiraEntry.capas),
        )
    )
    entry = (await db.execute(refresh_stmt)).scalar_one()

    # Denormalise the hazard library fields the same way get_entry does, so a
    # save returns the same shape a fresh load would.
    out = HiraEntryOut.model_validate(entry).model_dump()
    for i, hz in enumerate(out["hazards"]):
        src_hz = entry.hazards[i]
        if src_hz.hazard is not None:
            hz["hazardCode"] = src_hz.hazard.code
            hz["hazardCategory"] = src_hz.hazard.category
            hz["hazardName"] = src_hz.hazard.name
            hz["hazardRequiresPermit"] = bool(src_hz.hazard.requiresPermit)
            hz["hazardPermitTypes"] = src_hz.hazard.permitTypes or []
    return HiraEntryOut(**out)




async def _get_entry_detail(entry_id: str, db: AsyncSession) -> HiraEntryOut:
    """Shared helper to reload an entry with all child relations."""
    stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry_id)
        .options(
            selectinload(HiraEntry.hazards).selectinload(HiraEntryHazard.hazard),
            selectinload(HiraEntry.existingControls),
            selectinload(HiraEntry.recommendedControls),
            selectinload(HiraEntry.regulationRefs),
            selectinload(HiraEntry.capas),
            selectinload(HiraEntry.study),
        )
    )
    entry = (await db.execute(stmt)).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    return HiraEntryOut.model_validate(entry)


@router.post("/entries/{entry_id}/submit-for-review", response_model=HiraEntryOut)
async def submit_entry_for_review(
    entry_id: str,
    payload: HiraEntryTransitionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HiraEntryOut:
    entry = await db.get(HiraEntry, entry_id, options=[selectinload(HiraEntry.study)])
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=entry.study.plantId, record_id=entry.id
    )
    if entry.status not in ("DRAFT", "FLAGGED_FOR_REVIEW"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Entry cannot be submitted from status {entry.status}")
    entry.status = "IN_REVIEW"
    entry.updatedById = user.id
    await db.flush()
    return await _get_entry_detail(entry_id, db)


@router.post("/entries/{entry_id}/approve", response_model=HiraEntryOut)
async def approve_entry(
    entry_id: str,
    payload: HiraEntryTransitionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HiraEntryOut:
    entry = await db.get(HiraEntry, entry_id, options=[selectinload(HiraEntry.study)])
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context(
        "HIRA.APPROVE", user, db, plant_id=entry.study.plantId, record_id=entry.id
    )
    if entry.status not in _APPROVABLE_ENTRY_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Entry must be IN_REVIEW or PENDING_REAPPROVAL to approve, current: {entry.status}",
        )
    # ALARP governance: an Unacceptable residual cannot be approved on the normal
    # path. It must be reduced, OR authorised via the elevated, time-bounded
    # override (POST /entries/{id}/override-unacceptable, HIRA.OVERRIDE_UNACCEPTABLE).
    if entry.residualAlarpRegion == "UNACCEPTABLE" and not entry.unacceptableOverrideActive:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Residual risk is Unacceptable (ALARP): it cannot be approved. Reduce the residual with additional "
            "controls, or obtain an elevated Unacceptable-risk override before approving.",
        )
    entry.status = "APPROVED"
    entry.updatedById = user.id
    await db.flush()
    return await _get_entry_detail(entry_id, db)


@router.post("/entries/{entry_id}/override-unacceptable", response_model=HiraEntryOut)
async def override_unacceptable(
    entry_id: str,
    payload: HiraUnacceptableOverrideRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HiraEntryOut:
    """Elevated, time-bounded authorisation to accept an Unacceptable residual.

    Requires HIRA.OVERRIDE_UNACCEPTABLE (Plant Head / Corporate HSE tier), a
    justification, and an expiry after which the review scheduler auto-flags the
    entry. This is the ONLY way an Unacceptable residual can reach APPROVED, and
    it replaces the old free-text 'acceptance rationale'. Recorded to
    HiraVersion for the audit trail.
    """
    entry = await db.get(HiraEntry, entry_id, options=[selectinload(HiraEntry.study)])
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context(
        "HIRA.OVERRIDE_UNACCEPTABLE", user, db, plant_id=entry.study.plantId, record_id=entry.id
    )
    if entry.residualAlarpRegion != "UNACCEPTABLE":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Override applies only to an Unacceptable residual; this entry is "
            f"{entry.residualAlarpRegion or 'unassessed'}.",
        )

    now = datetime.now(timezone.utc)
    entry.unacceptableOverrideById = user.id
    entry.unacceptableOverrideAt = now
    entry.unacceptableOverrideJustification = payload.justification.strip()
    entry.unacceptableOverrideExpiresAt = now + timedelta(days=payload.expiresInDays)
    entry.updatedById = user.id

    # Audit trail — the override is a governance decision, not a data edit.
    archive_number = await _archive_version_number(db, entry)
    db.add(
        HiraVersion(
            entryId=entry.id,
            versionNumber=archive_number,
            snapshot=_entry_snapshot(entry),
            changes=[
                {
                    "action": "unacceptable_override",
                    "expiresAt": entry.unacceptableOverrideExpiresAt.isoformat(),
                    "justification": entry.unacceptableOverrideJustification,
                }
            ],
            changeTrigger="UNACCEPTABLE_OVERRIDE",
            changeReason=payload.justification.strip(),
            createdById=user.id,
        )
    )
    entry.versionNumber = archive_number + 1
    await db.flush()
    return await _get_entry_detail(entry_id, db)


@router.get("/entries/{entry_id}/capas", response_model=list[HiraCapaOut])
async def list_entry_capas(
    entry_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[HiraCapaOut]:
    entry = await db.get(HiraEntry, entry_id, options=[selectinload(HiraEntry.study)])
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context("HIRA.READ", user, db, plant_id=entry.study.plantId, record_id=entry.id)
    rows = (await db.execute(
        select(HiraCapa).where(HiraCapa.entryId == entry_id).order_by(HiraCapa.createdAt)
    )).scalars().all()
    return [HiraCapaOut.model_validate(r) for r in rows]


@router.post("/entries/{entry_id}/capas", response_model=HiraCapaOut, status_code=status.HTTP_201_CREATED)
async def create_entry_capa(
    entry_id: str,
    payload: HiraCapaCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HiraCapaOut:
    entry = await db.get(HiraEntry, entry_id, options=[selectinload(HiraEntry.study)])
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context("HIRA.UPDATE", user, db, plant_id=entry.study.plantId, record_id=entry.id)
    # Auto-generate CAPA number
    count_r = await db.execute(select(func.count()).select_from(HiraCapa).where(HiraCapa.entryId == entry_id))
    count = count_r.scalar_one()
    capa_number = f"CAPA-{entry_id[:8].upper()}-{count + 1:03d}"
    capa = HiraCapa(
        entryId=entry_id,
        number=capa_number,
        description=payload.description,
        controlHierarchy=payload.controlHierarchy,
        ownerId=payload.ownerId,
        targetDate=payload.targetDate,
        status="OPEN",
        createdById=user.id,
        updatedById=user.id,
    )
    db.add(capa)
    await db.flush()
    await db.refresh(capa)
    return HiraCapaOut.model_validate(capa)


@router.patch("/capas/{capa_id}", response_model=HiraCapaOut)
async def update_capa(
    capa_id: str,
    payload: HiraCapaUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HiraCapaOut:
    capa = await db.get(HiraCapa, capa_id, options=[selectinload(HiraCapa.entry).selectinload(HiraEntry.study)])
    if capa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CAPA not found")
    await require_permission_with_context("HIRA.UPDATE", user, db, plant_id=capa.entry.study.plantId)
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(capa, k, v)
    capa.updatedById = user.id
    await db.flush()
    await db.refresh(capa)
    return HiraCapaOut.model_validate(capa)


@router.get("/entries/{entry_id}/hazards/{row_id}/ptw-prefill", response_model=dict)
async def hazard_ptw_prefill(
    entry_id: str,
    row_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Context for drafting a PTW from a permit-required hazard row.

    Read-only: it does NOT create the permit. The PTW create screen consumes
    this to pre-fill, and carries hiraEntryId/hiraEntryHazardId through to
    permit creation so the link survives. Keeping the write on the PTW side
    means the permit still goes through its own validation and numbering.
    """
    # select(), not db.get() — db.get() drops the eager-load options when the
    # row is already in the identity map, which then lazy-loads under async.
    row = (
        await db.execute(
            select(HiraEntryHazard)
            .where(HiraEntryHazard.id == row_id)
            .options(selectinload(HiraEntryHazard.hazard))
        )
    ).scalar_one_or_none()
    if row is None or row.entryId != entry_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hazard row not found on this entry")

    entry = (
        await db.execute(
            select(HiraEntry)
            .where(HiraEntry.id == entry_id)
            .options(selectinload(HiraEntry.study), selectinload(HiraEntry.area))
        )
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context(
        "HIRA.READ", user, db, plant_id=entry.study.plantId, record_id=entry.id
    )

    lib = row.hazard
    if lib is None or not lib.requiresPermit:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This hazard is not flagged as permit-requiring in the hazard library.",
        )

    allowed = [t for t in (lib.permitTypes or []) if t in _PERMIT_TYPE_CODES]
    suggested = allowed[0] if allowed else _CATEGORY_PERMIT_DEFAULT.get(lib.category)

    scope_bits = [entry.activityDescription, lib.name]
    if row.contextualDescription:
        scope_bits.append(row.contextualDescription)

    return {
        "hiraEntryId": entry.id,
        "hiraEntryHazardId": row.id,
        "plantId": entry.study.plantId,
        "areaId": entry.areaId,
        "areaName": entry.area.name if entry.area else None,
        "location": entry.subLocation or (entry.area.name if entry.area else ""),
        "specificLocation": entry.subLocation,
        "scopeOfWork": " — ".join(b for b in scope_bits if b),
        "suggestedPermitType": suggested,
        "allowedPermitTypes": allowed,
        "hazardName": lib.name,
        "hazardCategory": lib.category,
        "consequence": row.consequence,
        "residualRiskLevel": entry.residualRiskLevel,
        "studyNumber": entry.study.number,
    }


@router.put("/entries/{entry_id}/hazards", response_model=dict)
async def replace_entry_hazards(
    entry_id: str,
    payload: list[HiraEntryHazardReplaceItem],
    change_reason: str | None = Query(None, alias="changeReason"),
    skip_version: bool = Query(False, alias="skipVersion", description=_SKIP_VERSION_DOC),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    # select() rather than db.get(): when the entry is already in the session's
    # identity map, db.get() returns it and quietly DISCARDS the eager-load
    # options, leaving entry.hazards to lazy-load and raise MissingGreenlet on
    # an async session.
    entry = (
        await db.execute(
            select(HiraEntry)
            .where(HiraEntry.id == entry_id)
            .options(selectinload(HiraEntry.study), selectinload(HiraEntry.hazards))
        )
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=entry.study.plantId, record_id=entry.id
    )

    existing_by_hazard = {hz.hazardId: hz for hz in entry.hazards}

    # Consequence is required going forward (ISO 45001 cl.6.1.2.1 wants it as a
    # distinct element). Rows already in the database with a NULL consequence
    # are grandfathered — they can be re-saved untouched — but a NEW hazard row
    # cannot omit it, and an existing populated value cannot be blanked out.
    # No backfill is attempted here: retro-populating ~96 live rows is a data
    # decision, not a code one.
    missing: list[str] = []
    for h in payload:
        if (h.consequence or "").strip():
            continue
        prior = existing_by_hazard.get(h.hazardId)
        if prior is None or (prior.consequence or "").strip():
            missing.append(h.hazardId)
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Consequence is required for each hazard. Missing for hazardId(s): "
            + ", ".join(missing),
        )

    before_hazards = _hazard_fingerprint(entry.hazards)
    after_hazards = _hazard_fingerprint(payload)
    is_material = before_hazards != after_hazards

    needs_version = (
        entry.study.status in ("APPROVED", "ACTIVE") or entry.versionNumber > 1
    ) and not skip_version
    if needs_version:
        if not change_reason:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "changeReason is required when study is APPROVED or ACTIVE",
            )
        archive_number = await _archive_version_number(db, entry)
        db.add(HiraVersion(
            entryId=entry.id,
            versionNumber=archive_number,
            snapshot=_entry_snapshot(entry),
            changes=[
                {
                    "action": "hazards_replaced",
                    "changeReason": change_reason,
                    "materiality": MATERIAL_TRIGGER if is_material else MINOR_TRIGGER,
                }
            ],
            changeTrigger=MATERIAL_TRIGGER if is_material else MINOR_TRIGGER,
            changeReason=change_reason,
            createdById=user.id,
        ))
        entry.versionNumber = archive_number + 1
        await db.flush()

    # Reconcile in place rather than delete-and-reinsert: a hazard row's id is
    # now referenced by Permit.hiraEntryHazardId, and a blanket delete would
    # SET NULL those links on every unrelated save.
    incoming_hazard_ids = {h.hazardId for h in payload}
    for hazard_id, hz in existing_by_hazard.items():
        if hazard_id not in incoming_hazard_ids:
            await db.delete(hz)

    # Library rows for the citation suggestion applied to newly added hazards.
    new_hazard_ids = incoming_hazard_ids - set(existing_by_hazard)
    lib_by_id: dict[str, HiraHazard] = {}
    if new_hazard_ids:
        lib_rows = (
            await db.execute(select(HiraHazard).where(HiraHazard.id.in_(new_hazard_ids)))
        ).scalars().all()
        lib_by_id = {row.id: row for row in lib_rows}

    for idx, h in enumerate(payload):
        sort_order = h.sortOrder if h.sortOrder is not None else idx
        row = existing_by_hazard.get(h.hazardId)
        if row is not None:
            row.contextualDescription = h.contextualDescription
            if (h.consequence or "").strip():
                row.consequence = h.consequence.strip()
            row.regulationRef = (h.regulationRef or "").strip() or None
            row.regulationSection = (h.regulationSection or "").strip() or None
            row.sortOrder = sort_order
        else:
            suggested_ref, suggested_section = _suggest_hazard_regulation(
                lib_by_id.get(h.hazardId)
            )
            db.add(HiraEntryHazard(
                entryId=entry.id,
                hazardId=h.hazardId,
                contextualDescription=h.contextualDescription,
                consequence=(h.consequence or "").strip() or None,
                regulationRef=(h.regulationRef or "").strip() or suggested_ref,
                regulationSection=(h.regulationSection or "").strip() or suggested_section,
                sortOrder=sort_order,
            ))

    entry.updatedById = user.id
    reapproval_required = _apply_reapproval(entry, is_material)
    await db.flush()
    return {
        "count": len(payload),
        "material": is_material,
        "reapprovalRequired": reapproval_required,
        "entryStatus": entry.status,
    }


@router.put("/entries/{entry_id}/existing-controls")
async def replace_existing_controls(
    entry_id: str,
    payload: HiraEntryControlReplaceRequest,
    change_reason: str | None = Query(None, alias="changeReason"),
    skip_version: bool = Query(False, alias="skipVersion", description=_SKIP_VERSION_DOC),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry_id)
        .options(selectinload(HiraEntry.study))
    )
    entry = (await db.execute(stmt)).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=entry.study.plantId, record_id=entry.id
    )

    # Read the current control set BEFORE the wholesale replace so the
    # effectiveness comparison has something to compare against.
    existing = (
        await db.execute(select(HiraEntryControl).where(HiraEntryControl.entryId == entry_id))
    ).scalars().all()
    before_controls = _control_effectiveness_fingerprint(existing)
    after_controls = _control_effectiveness_fingerprint(payload.controls)
    is_material = before_controls != after_controls

    needs_version = entry.study.status in ("APPROVED", "ACTIVE") and not skip_version
    if needs_version:
        if not change_reason:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "changeReason is required when study is APPROVED or ACTIVE")
        archive_number = await _archive_version_number(db, entry)
        version = HiraVersion(
            entryId=entry.id,
            versionNumber=archive_number,
            snapshot=_entry_snapshot(entry),
            changes=[
                {
                    "action": "controls_replaced",
                    "changeReason": change_reason,
                    "materiality": MATERIAL_TRIGGER if is_material else MINOR_TRIGGER,
                }
            ],
            # Was "CONTROLS_UPDATED" unconditionally; now reflects whether the
            # control set's effectiveness actually moved.
            changeTrigger=MATERIAL_TRIGGER if is_material else MINOR_TRIGGER,
            changeReason=change_reason,
            # NOTE: this was `changedById`, which is not a column on HiraVersion
            # — SQLAlchemy raised TypeError, so replacing controls on an
            # APPROVED/ACTIVE study failed outright. Matches the PATCH handler.
            createdById=user.id,
        )
        db.add(version)
        entry.versionNumber = archive_number + 1
        await db.flush()

    # Wholesale replace
    for e in existing:
        await db.delete(e)

    for idx, c in enumerate(payload.controls):
        db.add(
            HiraEntryControl(
                entryId=entry_id,
                controlId=c.controlId,
                hierarchy=c.hierarchy,
                description=c.description,
                effectiveness=c.effectiveness,
                verificationMethod=c.verificationMethod,
                verificationFreq=c.verificationFreq,
                responsibleRole=c.responsibleRole,
                evidenceAttached=c.evidenceAttached,
                documentReference=c.documentReference,
                sortOrder=c.sortOrder if c.sortOrder is not None else idx,
            )
        )

    entry.updatedById = user.id
    await db.flush()

    rows = (
        await db.execute(
            select(HiraEntryControl)
            .where(HiraEntryControl.entryId == entry_id)
            .order_by(HiraEntryControl.sortOrder.asc())
        )
    ).scalars().all()

    # If the entry derives its residual from controls, recompute it now that the
    # control set has changed, then re-evaluate ALARP + acceptability.
    residual: dict | None = None
    if entry.residualAutoCalculated is True:
        matrix = await db.get(RiskMatrix, entry.study.riskMatrixId)
        cells, liks_by_score, sevs_by_score = await _load_matrix_scales(db, entry.study.riskMatrixId)
        _apply_residual_from_controls(entry, list(rows), cells, liks_by_score, sevs_by_score)
        if entry.residualRiskLevel:
            region = _alarp_region(entry.residualRiskLevel, matrix.alarpBands if matrix else None)
            threshold = (
                (matrix.acceptableResidual or {}).get((entry.routine or "ROUTINE").lower()) if matrix else None
            )
            _evaluate_alarp(entry, region, threshold, user.id)
        await db.flush()
        residual = {
            "residualLikelihoodScore": entry.residualLikelihoodScore,
            "residualSeverityScore": entry.residualSeverityScore,
            "residualRiskScore": entry.residualRiskScore,
            "residualRiskLevel": entry.residualRiskLevel,
            "residualRiskColor": entry.residualRiskColor,
            "residualAcceptable": entry.residualAcceptable,
            "residualAlarpRegion": entry.residualAlarpRegion,
            "alarpStatus": entry.alarpStatus,
        }

    # Control effectiveness feeds the residual; a change to it invalidates the
    # approval the residual was signed off under.
    reapproval_required = _apply_reapproval(entry, is_material)
    await db.flush()

    return {
        "controls": [{"id": r.id, "hierarchy": r.hierarchy, "description": r.description} for r in rows],
        "residual": residual,
        "material": is_material,
        "reapprovalRequired": reapproval_required,
        "entryStatus": entry.status,
    }


@router.put("/entries/{entry_id}/recommended-controls")
async def replace_recommended_controls(
    entry_id: str,
    payload: HiraEntryRecommendedControlReplaceRequest,
    change_reason: str | None = Query(None, alias="changeReason"),
    skip_version: bool = Query(False, alias="skipVersion", description=_SKIP_VERSION_DOC),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry_id)
        .options(selectinload(HiraEntry.study))
    )
    entry = (await db.execute(stmt)).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=entry.study.plantId, record_id=entry.id
    )

    existing = (
        await db.execute(
            select(HiraEntryRecommendedControl).where(
                HiraEntryRecommendedControl.entryId == entry_id
            )
        )
    ).scalars().all()
    # A proposal moving PROPOSED → IMPLEMENTED / REJECTED (or appearing /
    # disappearing) changes the ALARP argument; editing its rationale does not.
    before_recommended = _recommended_status_fingerprint(existing)
    after_recommended = _recommended_status_fingerprint(payload.controls)
    is_material = before_recommended != after_recommended

    needs_version = entry.study.status in ("APPROVED", "ACTIVE") and not skip_version
    if needs_version:
        if not change_reason:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "changeReason is required when study is APPROVED or ACTIVE")
        archive_number = await _archive_version_number(db, entry)
        version = HiraVersion(
            entryId=entry.id,
            versionNumber=archive_number,
            snapshot=_entry_snapshot(entry),
            changes=[
                {
                    "action": "recommended_controls_replaced",
                    "changeReason": change_reason,
                    "materiality": MATERIAL_TRIGGER if is_material else MINOR_TRIGGER,
                }
            ],
            changeTrigger=MATERIAL_TRIGGER if is_material else MINOR_TRIGGER,
            changeReason=change_reason,
            # Was `changedById` + `isCurrentVersion`, neither of which exists on
            # HiraVersion — this path raised TypeError before ever committing.
            createdById=user.id,
        )
        db.add(version)
        entry.versionNumber = archive_number + 1
        await db.flush()

    incoming_ids = {c.id for c in payload.controls if c.id and not c.id.startswith("new-")}

    # Delete rows not in incoming AND not linked to a CAPA (preserves linked rows)
    for e in existing:
        if e.id not in incoming_ids and not e.capaId:
            await db.delete(e)

    existing_by_id = {e.id: e for e in existing}
    for c in payload.controls:
        if c.id and c.id in existing_by_id:
            row = existing_by_id[c.id]
            row.hierarchy = c.hierarchy
            row.description = c.description
            row.rationale = c.rationale
            row.targetLikelihoodReduction = c.targetLikelihoodReduction
            row.targetSeverityReduction = c.targetSeverityReduction
            row.estimatedCostBand = c.estimatedCostBand
            row.proposedImplementationDate = c.proposedImplementationDate
            row.responsibleId = c.responsibleId
            row.status = c.status
            row.evidenceAttached = c.evidenceAttached
            # Clearing the checkbox clears the reference, mirroring how
            # Section 4's existing-control evidence pair behaves.
            row.documentReference = c.documentReference if c.evidenceAttached else None
        else:
            db.add(
                HiraEntryRecommendedControl(
                    entryId=entry_id,
                    hierarchy=c.hierarchy,
                    description=c.description,
                    rationale=c.rationale,
                    targetLikelihoodReduction=c.targetLikelihoodReduction,
                    targetSeverityReduction=c.targetSeverityReduction,
                    estimatedCostBand=c.estimatedCostBand,
                    proposedImplementationDate=c.proposedImplementationDate,
                    responsibleId=c.responsibleId,
                    status=c.status,
                    evidenceAttached=c.evidenceAttached,
                    documentReference=c.documentReference if c.evidenceAttached else None,
                )
            )

    entry.updatedById = user.id
    reapproval_required = _apply_reapproval(entry, is_material)
    await db.flush()

    rows = (
        await db.execute(
            select(HiraEntryRecommendedControl)
            .where(HiraEntryRecommendedControl.entryId == entry_id)
            .order_by(HiraEntryRecommendedControl.createdAt.asc())
        )
    ).scalars().all()
    return {
        "controls": [{"id": r.id, "hierarchy": r.hierarchy, "status": r.status} for r in rows],
        "material": is_material,
        "reapprovalRequired": reapproval_required,
        "entryStatus": entry.status,
    }


@router.put("/entries/{entry_id}/regulation-refs")
async def replace_regulation_refs(
    entry_id: str,
    payload: HiraEntryRegulationRefReplaceRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry_id)
        .options(selectinload(HiraEntry.study))
    )
    entry = (await db.execute(stmt)).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    await require_permission_with_context(
        "HIRA.UPDATE", user, db, plant_id=entry.study.plantId, record_id=entry.id
    )

    existing = (
        await db.execute(
            select(HiraEntryRegulationRef).where(HiraEntryRegulationRef.entryId == entry_id)
        )
    ).scalars().all()
    for e in existing:
        await db.delete(e)
    for r in payload.refs:
        if not r.regulation.strip():
            continue
        db.add(
            HiraEntryRegulationRef(
                entryId=entry_id,
                regulation=r.regulation.strip(),
                section=r.section,
                requirementSummary=r.requirementSummary,
            )
        )
    entry.updatedById = user.id
    await db.flush()

    rows = (
        await db.execute(
            select(HiraEntryRegulationRef)
            .where(HiraEntryRegulationRef.entryId == entry_id)
            .order_by(HiraEntryRegulationRef.createdAt.asc())
        )
    ).scalars().all()
    return {"refs": [{"id": r.id, "regulation": r.regulation, "section": r.section} for r in rows]}


# ─────────────────────────────────────────────────────────────────────
# Review cycles
# ─────────────────────────────────────────────────────────────────────


@router.get("/review-cycles", response_model=list[HiraReviewCycleListItem])
async def list_review_cycles(
    status_filter: str | None = Query(None, alias="status"),
    trigger_filter: str | None = Query(None, alias="trigger"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[HiraReviewCycleListItem]:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    accessible = await get_accessible_plants(db, user.id)
    stmt = (
        select(HiraReviewCycle)
        .join(HiraEntry, HiraReviewCycle.entryId == HiraEntry.id)
        .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
        .options(
            selectinload(HiraReviewCycle.entry).selectinload(HiraEntry.study)
        )
    )
    if accessible is None:
        pass
    elif len(accessible) == 0:
        return []
    else:
        stmt = stmt.where(HiraStudy.plantId.in_(accessible))
    if status_filter:
        stmt = stmt.where(HiraReviewCycle.status == status_filter)
    else:
        stmt = stmt.where(HiraReviewCycle.status.in_(["SCHEDULED", "IN_PROGRESS"]))
    if trigger_filter:
        stmt = stmt.where(HiraReviewCycle.triggeredBy == trigger_filter)
    stmt = stmt.order_by(HiraReviewCycle.scheduledFor.asc()).limit(200)
    rows = (await db.execute(stmt)).scalars().all()

    result = []
    for r in rows:
        item = HiraReviewCycleListItem(
            id=r.id,
            entryId=r.entryId,
            scheduledFor=r.scheduledFor,
            triggeredBy=r.triggeredBy,
            triggerReferenceId=r.triggerReferenceId,
            status=r.status,
            assignedToId=r.assignedToId,
            outcome=r.outcome,
            createdAt=r.createdAt,
            entryTitle=r.entry.activityDescription if r.entry else None,
            entrySequenceNumber=r.entry.sequenceNumber if r.entry else None,
            studyNumber=r.entry.study.number if r.entry and r.entry.study else None,
            studyTitle=r.entry.study.title if r.entry and r.entry.study else None,
        )
        result.append(item)
    return result


@router.post("/review-cycles/bulk-no-change")
async def bulk_no_change(
    payload: HiraReviewCycleBulkNoChangeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Mark multiple SCHEDULED/IN_PROGRESS cycles as NO_CHANGE_REQUIRED in one call."""
    check = await can(db, user.id, "HIRA.EXECUTE", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    accessible_plants = await get_accessible_plants(db, user.id)

    rows = (
        await db.execute(
            select(HiraReviewCycle)
            .where(HiraReviewCycle.id.in_(payload.cycleIds))
            .options(selectinload(HiraReviewCycle.entry).selectinload(HiraEntry.study))
        )
    ).scalars().all()

    # Filter to only cycles within accessible plants
    if accessible_plants is not None:
        rows = [r for r in rows if r.entry and r.entry.study and r.entry.study.plantId in accessible_plants]

    now = datetime.now(timezone.utc)
    notes = payload.notes or "No change required — bulk submission"
    updated: list[str] = []
    skipped: list[str] = []

    for cycle in rows:
        if cycle.status not in ("SCHEDULED", "IN_PROGRESS"):
            skipped.append(cycle.id)
            continue
        next_due = _compute_next_review_due(
            cycle.entry.study.reviewFrequency, cycle.entry.study.customReviewMonths
        )
        cycle.status = "COMPLETED"
        cycle.completedAt = now
        cycle.completedById = user.id
        cycle.outcome = "NO_CHANGE_REQUIRED"
        cycle.outcomeNotes = notes
        cycle.entry.lastReviewedAt = now
        cycle.entry.lastReviewedById = user.id
        cycle.entry.nextReviewDue = next_due
        cycle.entry.reviewCount = (cycle.entry.reviewCount or 0) + 1
        cycle.entry.lastReviewType = "SCHEDULED"
        cycle.entry.status = "ACTIVE"
        updated.append(cycle.id)

    await db.flush()
    return {"updated": updated, "skipped": skipped}


@router.get("/review-cycles/{cycle_id}", response_model=HiraReviewCycleOut)
async def get_review_cycle(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraReviewCycleOut:
    cycle = await db.get(HiraReviewCycle, cycle_id)
    if cycle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cycle not found")
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    return HiraReviewCycleOut.model_validate(cycle)


def _compute_next_review_due(frequency: str, custom_months: int | None) -> datetime:
    from datetime import timedelta

    months = {
        "QUARTERLY": 3,
        "BIENNIAL": 24,
        "ANNUAL": 12,
        "CUSTOM": custom_months or 12,
        "TRIGGERED_ONLY": 36,
    }.get(frequency, 12)
    now = datetime.now(timezone.utc)
    if _HAS_RELATIVEDELTA:
        return now + _relativedelta(months=months)
    else:
        return now + timedelta(days=months * 30)


@router.post("/review-cycles/{cycle_id}/submit", response_model=HiraReviewCycleOut)
async def submit_review_cycle(
    cycle_id: str,
    payload: HiraReviewCycleSubmitRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraReviewCycleOut:
    stmt = (
        select(HiraReviewCycle)
        .where(HiraReviewCycle.id == cycle_id)
        .options(selectinload(HiraReviewCycle.entry).selectinload(HiraEntry.study))
    )
    cycle = (await db.execute(stmt)).scalar_one_or_none()
    if cycle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cycle not found")
    if cycle.status not in ("SCHEDULED", "IN_PROGRESS"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot submit in status {cycle.status}")

    valid = {"NO_CHANGE_REQUIRED", "MINOR_REVISION", "MAJOR_REVISION", "NEW_ENTRY_CREATED", "ENTRY_ARCHIVED"}
    if payload.outcome not in valid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid outcome")

    await require_permission_with_context(
        "HIRA.EXECUTE",
        user,
        db,
        plant_id=cycle.entry.study.plantId,
        record_id=cycle.entryId,
    )

    now = datetime.now(timezone.utc)
    next_due = _compute_next_review_due(
        cycle.entry.study.reviewFrequency, cycle.entry.study.customReviewMonths
    )

    entry_status_patch = None
    if payload.outcome == "MAJOR_REVISION":
        entry_status_patch = "FLAGGED_FOR_REVIEW"
    elif payload.outcome == "ENTRY_ARCHIVED":
        entry_status_patch = "ARCHIVED"
    elif payload.outcome in ("NO_CHANGE_REQUIRED", "MINOR_REVISION"):
        entry_status_patch = "ACTIVE"

    # MAJOR_REVISION stays IN_PROGRESS until Team Leader re-approves the entry
    if payload.outcome == "MAJOR_REVISION":
        cycle.status = "IN_PROGRESS"
    else:
        cycle.status = "COMPLETED"
        cycle.completedAt = now
        cycle.completedById = user.id
    cycle.outcome = payload.outcome
    cycle.outcomeNotes = payload.outcomeNotes

    cycle.entry.lastReviewedAt = now
    cycle.entry.lastReviewedById = user.id
    if payload.outcome != "MAJOR_REVISION":
        cycle.entry.nextReviewDue = next_due
        cycle.entry.reviewCount = (cycle.entry.reviewCount or 0) + 1
    cycle.entry.lastReviewType = {
        "SCHEDULE": "SCHEDULED",
        "INCIDENT": "INCIDENT_TRIGGERED",
        "MOC": "MOC_TRIGGERED",
        "AUDIT_FINDING": "AUDIT_TRIGGERED",
        "MANUAL": "MANUAL_TRIGGERED",
        "NEAR_MISS": "NEAR_MISS_TRIGGERED",
        "OBSERVATION": "OBSERVATION_TRIGGERED",
        "REGULATORY_CHANGE": "REGULATORY_CHANGE_TRIGGERED",
    }.get(cycle.triggeredBy, "AD_HOC")
    cycle.entry.triggeredByRecordId = cycle.triggerReferenceId
    if entry_status_patch:
        cycle.entry.status = entry_status_patch

    await db.flush()
    await db.refresh(cycle)
    return HiraReviewCycleOut.model_validate(cycle)


# ─────────────────────────────────────────────────────────────────────
# Versions
# ─────────────────────────────────────────────────────────────────────


@router.get("/entries/{entry_id}/versions", response_model=list[HiraVersionOut])
async def list_versions(
    entry_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[HiraVersionOut]:
    stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == entry_id)
        .options(selectinload(HiraEntry.study))
    )
    entry = (await db.execute(stmt)).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")

    check = await can(
        db,
        user.id,
        "HIRA.READ",
        PermissionContext(record_id=entry.id, plant_id=entry.study.plantId),
    )
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    rows = (
        await db.execute(
            select(HiraVersion)
            .where(HiraVersion.entryId == entry_id)
            .order_by(HiraVersion.versionNumber.desc())
        )
    ).scalars().all()
    return [HiraVersionOut.model_validate(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────
# Integrations — FLRA / PTW / Inspection priority
# ─────────────────────────────────────────────────────────────────────


def _serialize_integration_entry(e: HiraEntry, study: HiraStudy) -> HiraIntegrationEntry:
    return HiraIntegrationEntry(
        id=e.id,
        sequenceNumber=e.sequenceNumber,
        activityDescription=e.activityDescription,
        initialRiskLevel=e.initialRiskLevel,
        initialRiskScore=e.initialRiskScore,
        residualRiskLevel=e.residualRiskLevel,
        residualRiskScore=e.residualRiskScore,
        residualAcceptable=e.residualAcceptable,
        studyId=study.id,
        studyNumber=study.number,
        studyTitle=study.title,
        hazards=[],
        influencesPtwRiskLevel=e.influencesPtwRiskLevel,
        influencesPtwPermitTypes=e.influencesPtwPermitTypes,
    )


@router.get("/integrations/for-flra", response_model=HiraIntegrationForFlraResponse)
async def for_flra(
    plant_id: str = Query(..., alias="plantId"),
    area_id: str | None = Query(None, alias="areaId"),
    activity_keyword: str | None = Query(None, alias="activityKeyword"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraIntegrationForFlraResponse:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    stmt = (
        select(HiraEntry, HiraStudy)
        .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
        .where(HiraStudy.plantId == plant_id)
        .where(HiraStudy.status == "ACTIVE")
        .where(HiraEntry.isCurrentVersion.is_(True))
        .where(HiraEntry.status.in_(["APPROVED", "ACTIVE", "FLAGGED_FOR_REVIEW", "PENDING_REAPPROVAL"]))
    )
    if area_id:
        stmt = stmt.where(HiraEntry.areaId == area_id)
    if activity_keyword:
        stmt = stmt.where(HiraEntry.activityDescription.ilike(f"%{activity_keyword}%"))
    stmt = stmt.limit(200)
    rows = (await db.execute(stmt)).all()
    entries = [_serialize_integration_entry(e, s) for e, s in rows]
    return HiraIntegrationForFlraResponse(entries=entries, count=len(entries))


@router.get("/integrations/for-ptw", response_model=HiraIntegrationForPtwResponse)
async def for_ptw(
    plant_id: str = Query(..., alias="plantId"),
    area_id: str | None = Query(None, alias="areaId"),
    permit_type: str | None = Query(None, alias="permitType"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraIntegrationForPtwResponse:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    base = (
        select(HiraEntry, HiraStudy)
        .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
        .where(HiraStudy.plantId == plant_id)
        .where(HiraStudy.status == "ACTIVE")
        .where(HiraEntry.isCurrentVersion.is_(True))
        .where(HiraEntry.status.in_(["APPROVED", "ACTIVE", "FLAGGED_FOR_REVIEW", "PENDING_REAPPROVAL"]))
    )
    if area_id:
        base = base.where(HiraEntry.areaId == area_id)

    explicit_q = base.where(HiraEntry.influencesPtwRiskLevel.is_(True))
    explicit = explicit_q.limit(200)
    high_risk = base.where(
        or_(HiraEntry.residualRiskLevel == "HIGH", HiraEntry.residualRiskLevel == "CRITICAL")
    ).limit(200)

    explicit_rows = (await db.execute(explicit)).all()
    high_rows = (await db.execute(high_risk)).all()

    # Filter by permit_type in Python — `influencesPtwPermitTypes` is a JSON
    # column, so SQLAlchemy's `.contains()` emits a `LIKE` (jsonb ~~ text) that
    # Postgres rejects. An entry applies when it lists no specific permit types
    # (NULL/empty = all) or explicitly includes this permit type.
    if permit_type:
        explicit_rows = [
            (e, s)
            for (e, s) in explicit_rows
            if not e.influencesPtwPermitTypes or permit_type in e.influencesPtwPermitTypes
        ]

    by_id: dict[str, tuple[HiraEntry, HiraStudy]] = {}
    for e, s in explicit_rows:
        by_id[e.id] = (e, s)
    for e, s in high_rows:
        by_id.setdefault(e.id, (e, s))

    sorted_entries = sorted(
        by_id.values(),
        key=lambda x: (x[0].residualRiskScore or x[0].initialRiskScore),
        reverse=True,
    )
    entries = [_serialize_integration_entry(e, s) for e, s in sorted_entries]
    gating = sum(1 for e in entries if e.residualRiskLevel == "CRITICAL")
    high = sum(1 for e in entries if e.residualRiskLevel == "HIGH")
    advisory = None
    if gating > 0:
        advisory = f"STOP — {gating} CRITICAL residual risk entr{'y' if gating == 1 else 'ies'} in this area. Corporate HSE approval required."
    elif high > 0:
        advisory = f"{high} HIGH residual risk entr{'y' if high == 1 else 'ies'} in this area — additional controls recommended for this permit."
    return HiraIntegrationForPtwResponse(
        entries=entries, count=len(entries), gatingBlockers=gating, highCount=high, advisory=advisory
    )


@router.get("/integrations/for-inspection", response_model=HiraInspectionPriorityResult)
async def for_inspection(
    plant_id: str = Query(..., alias="plantId"),
    area_id: str | None = Query(None, alias="areaId"),
    equipment_id: str | None = Query(None, alias="equipmentId"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraInspectionPriorityResult:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    stmt = (
        select(HiraEntry)
        .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
        .where(HiraStudy.plantId == plant_id)
        .where(HiraStudy.status == "ACTIVE")
        .where(HiraEntry.isCurrentVersion.is_(True))
        .where(HiraEntry.status.in_(["APPROVED", "ACTIVE", "FLAGGED_FOR_REVIEW", "PENDING_REAPPROVAL"]))
    )
    if area_id:
        stmt = stmt.where(HiraEntry.areaId == area_id)
    candidates = (await db.execute(stmt.limit(200))).scalars().all()

    if equipment_id:
        candidates = [
            c for c in candidates if equipment_id in ((c.equipmentUsed or []) if isinstance(c.equipmentUsed, list) else [])
        ]

    if not candidates:
        return HiraInspectionPriorityResult(
            multiplier=1.0, rationale="No HIRA entries match — baseline frequency.", sourceEntries=[]
        )

    order = ["LOW", "MODERATE", "HIGH", "CRITICAL"]
    highest = max(candidates, key=lambda c: order.index(c.residualRiskLevel or "LOW"))
    level = highest.residualRiskLevel or "LOW"
    multiplier = {"CRITICAL": 4.0, "HIGH": 2.0, "MODERATE": 1.5}.get(level, 1.0)
    sources = [
        {"id": c.id, "sequenceNumber": c.sequenceNumber, "residualRiskLevel": c.residualRiskLevel}
        for c in candidates
        if c.residualRiskLevel == level
    ][:5]
    return HiraInspectionPriorityResult(
        multiplier=multiplier,
        rationale=f"{len(candidates)} HIRA entries match this scope; highest residual = {level}. Apply {multiplier}x baseline inspection frequency.",
        sourceEntries=sources,
    )


# ─────────────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────────────


@router.get("/studies/{study_id}/export.csv")
async def export_study_csv(
    study_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import Response

    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    await require_permission_with_context(
        "HIRA.EXPORT", user, db, plant_id=study.plantId, record_id=study.id
    )

    stmt = (
        select(HiraStudy)
        .where(HiraStudy.id == study_id)
        .options(
            selectinload(HiraStudy.plant),
            selectinload(HiraStudy.department),
            selectinload(HiraStudy.area),
            selectinload(HiraStudy.entries).selectinload(HiraEntry.area),
            selectinload(HiraStudy.entries).selectinload(HiraEntry.hazards).selectinload(HiraEntryHazard.hazard),
            selectinload(HiraStudy.entries).selectinload(HiraEntry.existingControls),
            selectinload(HiraStudy.entries).selectinload(HiraEntry.recommendedControls),
            selectinload(HiraStudy.entries).selectinload(HiraEntry.regulationRefs),
        )
    )
    study = (await db.execute(stmt)).scalar_one()
    current_entries = [e for e in study.entries if e.isCurrentVersion]
    current_entries.sort(key=lambda e: e.sequenceNumber)

    def esc(s: str | None) -> str:
        if s is None:
            return ""
        s = str(s)
        if any(c in s for c in [",", '"', "\n", "\r"]):
            return '"' + s.replace('"', '""') + '"'
        return s

    rows: list[list[str]] = []
    rows.append([f"HIRA Register — {study.number}"])
    rows.append([f"Title: {study.title}"])
    rows.append([f"Plant: {study.plant.name if study.plant else '—'}"])
    rows.append([f"Status: {study.status}"])
    rows.append([f"Generated: {datetime.now(timezone.utc).isoformat()}"])
    rows.append([f"Total Entries: {len(current_entries)}"])
    rows.append([""])
    rows.append(
        [
            "Sr.No.",
            "Activity",
            "Area",
            "Routine",
            "Frequency",
            "Hazards",
            # Hazard-grain ISO 45001 cl.6.1.2.1 elements, kept next to the
            # hazards they belong to and separate from the entry-level refs.
            "Consequences",
            "Hazard Reg Refs",
            "Init L",
            "Init S",
            "Init Risk",
            "Init Level",
            "Existing Controls",
            "Resid L",
            "Resid S",
            "Resid Risk",
            "Resid Level",
            "ALARP Region",
            "ALARP Status",
            "Acceptable",
            "Target Level",
            "Target ALARP",
            "Recommended",
            "Recommended Evidence",
            "Reg Refs (entry)",
            "Status",
        ]
    )

    for e in current_entries:
        rows.append(
            [
                str(e.sequenceNumber),
                e.activityDescription,
                e.area.name if e.area else "",
                e.routine,
                e.frequency,
                "; ".join(f"{h.hazard.name if h.hazard else '(deleted)'} [{h.hazard.category if h.hazard else '?'}]" for h in e.hazards),
                "; ".join(
                    f"{h.hazard.name if h.hazard else 'Hazard'}: "
                    f"{(h.consequence or '').strip() or '— not recorded —'}"
                    for h in e.hazards
                ),
                "; ".join(
                    f"{h.hazard.name if h.hazard else 'Hazard'}: "
                    f"{' '.join(x for x in (h.regulationRef, h.regulationSection) if x)}"
                    for h in e.hazards
                    if h.regulationRef or h.regulationSection
                ),
                str(e.initialLikelihoodScore),
                str(e.initialSeverityScore),
                str(e.initialRiskScore),
                e.initialRiskLevel,
                "; ".join(f"{c.hierarchy}: {c.description}" for c in e.existingControls),
                str(e.residualLikelihoodScore) if e.residualLikelihoodScore is not None else "",
                str(e.residualSeverityScore) if e.residualSeverityScore is not None else "",
                str(e.residualRiskScore) if e.residualRiskScore is not None else "",
                e.residualRiskLevel or "",
                (e.residualAlarpRegion or "").replace("_", " ").title(),
                (e.alarpStatus or "").replace("_", " ").title(),
                "" if e.residualAcceptable is None else ("Yes" if e.residualAcceptable else "No"),
                e.targetRiskLevel or "",
                (e.targetAlarpRegion or "").replace("_", " ").title(),
                "; ".join(f"[{c.status}] {c.hierarchy}: {c.description}" for c in e.recommendedControls),
                "; ".join(
                    f"{c.hierarchy}: {c.documentReference or 'evidence on file'}"
                    for c in e.recommendedControls
                    if c.evidenceAttached or c.documentReference
                ),
                "; ".join(f"{r.regulation} {r.section or ''}".strip() for r in e.regulationRefs),
                e.status,
            ]
        )

    csv = "﻿" + "\r\n".join(",".join(esc(c) for c in r) for r in rows)
    return Response(
        content=csv,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{study.number}.csv"'},
    )


# ─────────────────────────────────────────────────────────────────────
# Dashboard aggregates
# ─────────────────────────────────────────────────────────────────────


@router.get("/dashboard/coverage", response_model=HiraDashboardCoverage)
async def dashboard_coverage(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraDashboardCoverage:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    accessible = await get_accessible_plants(db, user.id)

    from app.models.masters import Department

    total_q = select(func.count()).select_from(Department).where(Department.active.is_(True))
    if accessible is not None:
        if not accessible:
            return HiraDashboardCoverage(totalDepartments=0, coveredDepartments=0, coveragePct=0)
        total_q = total_q.where(Department.plantId.in_(accessible))

    total = (await db.execute(total_q)).scalar_one() or 0

    covered_q = (
        select(func.count(func.distinct(HiraStudy.departmentId)))
        .where(HiraStudy.status == "ACTIVE")
        .where(HiraStudy.departmentId.is_not(None))
    )
    if accessible is not None:
        covered_q = covered_q.where(HiraStudy.plantId.in_(accessible))
    covered = (await db.execute(covered_q)).scalar_one() or 0

    pct = int(round((covered / total) * 100)) if total > 0 else 0
    return HiraDashboardCoverage(totalDepartments=total, coveredDepartments=covered, coveragePct=pct)


@router.get("/dashboard/high-risk", response_model=HiraDashboardHighRisk)
async def dashboard_high_risk(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraDashboardHighRisk:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    accessible = await get_accessible_plants(db, user.id)

    base = (
        select(func.count())
        .select_from(HiraEntry)
        .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
        .where(HiraEntry.isCurrentVersion.is_(True))
        .where(HiraEntry.status.in_(["APPROVED", "ACTIVE", "FLAGGED_FOR_REVIEW", "PENDING_REAPPROVAL"]))
    )
    if accessible is not None:
        if not accessible:
            return HiraDashboardHighRisk(high=0, critical=0, total=0)
        base = base.where(HiraStudy.plantId.in_(accessible))

    high = (await db.execute(base.where(HiraEntry.residualRiskLevel == "HIGH"))).scalar_one() or 0
    critical = (await db.execute(base.where(HiraEntry.residualRiskLevel == "CRITICAL"))).scalar_one() or 0
    return HiraDashboardHighRisk(high=high, critical=critical, total=high + critical)


@router.get("/dashboard/risk-reduction", response_model=HiraDashboardRiskReduction)
async def dashboard_risk_reduction(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraDashboardRiskReduction:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    accessible = await get_accessible_plants(db, user.id)

    stmt = (
        select(HiraEntry.initialRiskScore, HiraEntry.residualRiskScore)
        .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
        .where(HiraEntry.isCurrentVersion.is_(True))
        .where(HiraEntry.status.in_(["APPROVED", "ACTIVE", "PENDING_REAPPROVAL"]))
    )
    if accessible is not None:
        if not accessible:
            return HiraDashboardRiskReduction(initialTotal=0, residualTotal=0, reductionPct=0)
        stmt = stmt.where(HiraStudy.plantId.in_(accessible))

    rows = (await db.execute(stmt)).all()
    initial_total = sum(r[0] or 0 for r in rows)
    residual_total = sum((r[1] if r[1] is not None else (r[0] or 0)) for r in rows)
    pct = int(round(((initial_total - residual_total) / initial_total) * 100)) if initial_total > 0 else 0
    return HiraDashboardRiskReduction(
        initialTotal=initial_total, residualTotal=residual_total, reductionPct=pct
    )


@router.get("/dashboard/top-hazards", response_model=list[HiraDashboardTopHazard])
async def dashboard_top_hazards(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[HiraDashboardTopHazard]:
    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    accessible = await get_accessible_plants(db, user.id)

    stmt = (
        select(HiraHazard.category, func.count(HiraEntryHazard.id).label("c"))
        .join(HiraEntryHazard, HiraEntryHazard.hazardId == HiraHazard.id)
        .join(HiraEntry, HiraEntryHazard.entryId == HiraEntry.id)
        .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
        .where(HiraEntry.isCurrentVersion.is_(True))
        .where(HiraEntry.status.in_(["APPROVED", "ACTIVE", "FLAGGED_FOR_REVIEW", "PENDING_REAPPROVAL"]))
        .group_by(HiraHazard.category)
        .order_by(func.count(HiraEntryHazard.id).desc())
        .limit(5)
    )
    if accessible is not None:
        if not accessible:
            return []
        stmt = stmt.where(HiraStudy.plantId.in_(accessible))
    rows = (await db.execute(stmt)).all()
    return [HiraDashboardTopHazard(category=cat, count=int(cnt)) for cat, cnt in rows]


@router.post("/cron/review-scheduler")
async def cron_review_scheduler(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Daily scheduled review job. Called from the Next.js cron route that
    Vercel cron pings. Auth via JWT mint by the cron proxy.
    """
    from datetime import timedelta

    # Allow CORPORATE_HSE / ADMIN to run this (cron-internal users)
    check = await can(db, user.id, "HIRA.EXECUTE", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cron job requires HIRA.EXECUTE")

    now = datetime.now(timezone.utc)
    in30 = now + timedelta(days=30)
    in7 = now + timedelta(days=7)
    stats = {
        "created_T_minus_30": 0,
        "flagged_T_minus_7": 0,
        "forced_overdue": 0,
        "errors": [],
    }

    # T-30 candidates: active entries with nextReviewDue within 30 days AND no open cycle
    candidates = (
        await db.execute(
            select(HiraEntry, HiraStudy)
            .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
            .where(HiraEntry.isCurrentVersion.is_(True))
            .where(HiraEntry.status.in_(["APPROVED", "ACTIVE", "PENDING_REAPPROVAL"]))
            .where(HiraEntry.nextReviewDue.is_not(None))
            .where(HiraEntry.nextReviewDue <= in30)
            .where(HiraEntry.nextReviewDue >= now)
            .where(
                ~HiraEntry.id.in_(
                    select(HiraReviewCycle.entryId).where(
                        HiraReviewCycle.status.in_(["SCHEDULED", "IN_PROGRESS"])
                    )
                )
            )
            .limit(500)
        )
    ).all()
    for entry, study in candidates:
        try:
            db.add(
                HiraReviewCycle(
                    entryId=entry.id,
                    scheduledFor=entry.nextReviewDue,
                    triggeredBy="SCHEDULE",
                    status="SCHEDULED",
                    assignedToId=study.teamLeaderId,
                    assignedRole="TEAM_LEADER",
                )
            )
            stats["created_T_minus_30"] += 1
        except Exception as e:
            stats["errors"].append(f"T-30 entry {entry.id}: {e}")

    # T-7 flag: entries with SCHEDULED cycle in next 7 days
    to_flag = (
        await db.execute(
            select(HiraEntry.id)
            .where(HiraEntry.isCurrentVersion.is_(True))
            .where(HiraEntry.status.in_(["APPROVED", "ACTIVE", "PENDING_REAPPROVAL"]))
            .where(
                HiraEntry.id.in_(
                    select(HiraReviewCycle.entryId)
                    .where(HiraReviewCycle.status == "SCHEDULED")
                    .where(HiraReviewCycle.scheduledFor <= in7)
                    .where(HiraReviewCycle.scheduledFor >= now)
                )
            )
        )
    ).scalars().all()
    if to_flag:
        await db.execute(
            HiraEntry.__table__.update()
            .where(HiraEntry.id.in_(to_flag))
            .values(status="FLAGGED_FOR_REVIEW")
        )
        stats["flagged_T_minus_7"] = len(to_flag)

    # T+0 overdue
    overdue = (
        await db.execute(
            select(HiraEntry, HiraStudy)
            .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
            .where(HiraEntry.isCurrentVersion.is_(True))
            .where(HiraEntry.status.in_(["APPROVED", "ACTIVE", "FLAGGED_FOR_REVIEW", "PENDING_REAPPROVAL"]))
            .where(HiraEntry.nextReviewDue.is_not(None))
            .where(HiraEntry.nextReviewDue < now)
            .where(
                ~HiraEntry.id.in_(
                    select(HiraReviewCycle.entryId).where(
                        HiraReviewCycle.status.in_(["SCHEDULED", "IN_PROGRESS"])
                    )
                )
            )
            .limit(500)
        )
    ).all()
    for entry, study in overdue:
        try:
            db.add(
                HiraReviewCycle(
                    entryId=entry.id,
                    scheduledFor=entry.nextReviewDue,
                    triggeredBy="SCHEDULE",
                    status="SCHEDULED",
                    assignedToId=study.teamLeaderId,
                    assignedRole="TEAM_LEADER",
                )
            )
            entry.status = "FLAGGED_FOR_REVIEW"
            stats["forced_overdue"] += 1
        except Exception as e:
            stats["errors"].append(f"Overdue entry {entry.id}: {e}")

    # Expired Unacceptable-risk overrides: the time-bounded authorisation has
    # lapsed. Void the override and flag the entry so an Unacceptable residual
    # can never sit approved on a stale override.
    expired = (
        await db.execute(
            select(HiraEntry)
            .where(HiraEntry.isCurrentVersion.is_(True))
            .where(HiraEntry.unacceptableOverrideExpiresAt.is_not(None))
            .where(HiraEntry.unacceptableOverrideExpiresAt < now)
            .where(HiraEntry.unacceptableOverrideById.is_not(None))
            .limit(500)
        )
    ).scalars().all()
    stats["override_expired"] = 0
    for entry in expired:
        _clear_unacceptable_override(entry)
        if entry.status in ("APPROVED", "ACTIVE"):
            entry.status = "FLAGGED_FOR_REVIEW"
        stats["override_expired"] += 1

    await db.flush()
    return {"success": True, "ranAt": now.isoformat(), "stats": stats}


@router.post("/cron/training-expiry")
async def cron_training_expiry(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Daily training-expiry HIRA flag job."""
    from datetime import timedelta

    from app.models.training import TrainingCertificate

    check = await can(db, user.id, "HIRA.EXECUTE", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cron job requires HIRA.EXECUTE")

    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(days=1)
    stats = {"entriesFlagged": 0, "cyclesCreated": 0, "errors": []}

    expired = (
        await db.execute(
            select(
                TrainingCertificate.id,
                TrainingCertificate.programId,
            ).where(
                or_(
                    (TrainingCertificate.validTo >= day_ago)
                    & (TrainingCertificate.validTo < now),
                    TrainingCertificate.status == "EXPIRED",
                )
            ).limit(500)
        )
    ).all()
    if not expired:
        return {"success": True, "ranAt": now.isoformat(), "stats": stats, "note": "No newly-expired certs"}

    expired_program_ids = {c[1] for c in expired}
    cert_by_program: dict[str, str] = {}
    for cid, pid in expired:
        cert_by_program.setdefault(pid, cid)

    candidates = (
        await db.execute(
            select(HiraEntry, HiraStudy)
            .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
            .where(HiraEntry.isCurrentVersion.is_(True))
            .where(HiraEntry.status.in_(["APPROVED", "ACTIVE", "PENDING_REAPPROVAL"]))
            .where(HiraEntry.triggersTrainingProgramIds.is_not(None))
            .limit(2000)
        )
    ).all()

    for entry, study in candidates:
        refs = entry.triggersTrainingProgramIds or []
        hit = next((r for r in refs if r in expired_program_ids), None)
        if not hit:
            continue
        existing = (
            await db.execute(
                select(HiraReviewCycle.id)
                .where(HiraReviewCycle.entryId == entry.id)
                .where(HiraReviewCycle.status.in_(["SCHEDULED", "IN_PROGRESS"]))
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing:
            continue
        try:
            db.add(
                HiraReviewCycle(
                    entryId=entry.id,
                    scheduledFor=now + timedelta(days=14),
                    triggeredBy="MANUAL",
                    triggerReferenceId=cert_by_program.get(hit, hit),
                    status="SCHEDULED",
                    assignedToId=study.teamLeaderId,
                    assignedRole="TEAM_LEADER",
                    outcomeNotes=f"Training certificate for program {hit} expired",
                )
            )
            entry.status = "FLAGGED_FOR_REVIEW"
            stats["entriesFlagged"] += 1
            stats["cyclesCreated"] += 1
        except Exception as e:
            stats["errors"].append(f"Entry {entry.id}: {e}")

    await db.flush()
    return {"success": True, "ranAt": now.isoformat(), "stats": stats}


@router.get("/wizard/study-options")
async def study_wizard_options(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Returns the master data the new-study wizard needs in one round-trip:
    plants the caller can see, their departments + areas, all active users
    (for team picker), all active risk matrices.
    """
    from app.models.masters import Department
    from app.models.plant import Area, Plant

    # Shared master-data endpoint: the HIRA new-study wizard AND every CAPA intake
    # form (/capa/new/*) read it for the plant + user pickers. A CAPA creator
    # (e.g. the CRO) often holds CAPA.CREATE but not HIRA.CREATE — gating only on
    # HIRA.CREATE 403'd that call and crashed the CAPA pages. Accept either role.
    hira_ok = (await can(db, user.id, "HIRA.CREATE", PermissionContext())).allowed
    capa_ok = (await can(db, user.id, "CAPA.CREATE", PermissionContext())).allowed
    if not (hira_ok or capa_ok):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")

    accessible = await get_accessible_plants(db, user.id)

    plants_q = select(Plant.id, Plant.code, Plant.name)
    if accessible is not None:
        if not accessible:
            return {"plants": [], "departments": [], "areas": [], "users": [], "riskMatrices": []}
        plants_q = plants_q.where(Plant.id.in_(accessible))
    plant_rows = (await db.execute(plants_q.order_by(Plant.name))).all()

    plant_ids = [r[0] for r in plant_rows]
    depts = (
        await db.execute(
            select(Department.id, Department.plantId, Department.name)
            .where(Department.plantId.in_(plant_ids))
            .where(Department.active.is_(True))
            .order_by(Department.name)
        )
    ).all() if plant_ids else []
    areas = (
        await db.execute(
            select(Area.id, Area.plantId, Area.name)
            .where(Area.plantId.in_(plant_ids))
            .order_by(Area.name)
        )
    ).all() if plant_ids else []
    users_q = (
        select(User.id, User.name, User.email, User.department, User.plantId)
        .order_by(User.name)
        .limit(500)
    )
    if plant_ids:
        users_q = users_q.where(User.plantId.in_(plant_ids))
    users = (await db.execute(users_q)).all()
    matrices = (
        await db.execute(
            select(
                RiskMatrix.id,
                RiskMatrix.code,
                RiskMatrix.name,
                RiskMatrix.likelihoodLevels,
                RiskMatrix.severityLevels,
                RiskMatrix.isDefault,
                RiskMatrix.controlHierarchyEnforced,
            )
            .where(RiskMatrix.isActive.is_(True))
            .order_by(RiskMatrix.isDefault.desc(), RiskMatrix.name)
        )
    ).all()
    return {
        "plants": [
            {
                "id": pid,
                "code": code,
                "name": nm,
                "departments": [
                    {"id": d[0], "name": d[2]} for d in depts if d[1] == pid
                ],
                "areas": [{"id": a[0], "name": a[2]} for a in areas if a[1] == pid],
            }
            for pid, code, nm in plant_rows
        ],
        "departments": [{"id": d[0], "plantId": d[1], "name": d[2]} for d in depts],
        "areas": [{"id": a[0], "plantId": a[1], "name": a[2]} for a in areas],
        "users": [
            {"id": u[0], "name": u[1], "email": u[2], "department": u[3], "plantId": u[4]} for u in users
        ],
        "riskMatrices": [
            {
                "id": m[0],
                "code": m[1],
                "name": m[2],
                "likelihoodLevels": m[3],
                "severityLevels": m[4],
                "isDefault": m[5],
                "controlHierarchyEnforced": m[6],
            }
            for m in matrices
        ],
    }


@router.get("/wizard/entry-options")
async def entry_wizard_options(
    study_id: str = Query(..., alias="studyId"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Returns the form-option data the new-entry wizard needs: the parent
    study's matrix (scales + cells), the active hazard library, and the
    plant's areas.
    """
    from app.models.plant import Area

    study = await db.get(HiraStudy, study_id)
    if study is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Study not found")
    check = await can(
        db,
        user.id,
        "HIRA.UPDATE",
        PermissionContext(record_id=study.id, plant_id=study.plantId),
    )
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")

    matrix_stmt = (
        select(RiskMatrix)
        .where(RiskMatrix.id == study.riskMatrixId)
        .options(
            selectinload(RiskMatrix.likelihoods),
            selectinload(RiskMatrix.severities),
            selectinload(RiskMatrix.cells),
        )
    )
    matrix = (await db.execute(matrix_stmt)).scalar_one_or_none()
    if matrix is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Risk matrix not found or inactive")

    hazards = (
        await db.execute(
            select(HiraHazard)
            .where(HiraHazard.isActive.is_(True))
            .order_by(HiraHazard.category, HiraHazard.name)
            .limit(300)
        )
    ).scalars().all()

    areas = (
        await db.execute(
            select(Area.id, Area.name).where(Area.plantId == study.plantId).order_by(Area.name)
        )
    ).all()

    return {
        "studyStatus": study.status,
        "matrix": RiskMatrixOut.model_validate(matrix).model_dump(),
        "hazards": [HiraHazardOut.model_validate(h).model_dump() for h in hazards],
        "areas": [{"id": a[0], "name": a[1]} for a in areas],
    }


@router.get("/dashboard/review-compliance", response_model=HiraDashboardReviewCompliance)
async def dashboard_review_compliance(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HiraDashboardReviewCompliance:
    from datetime import timedelta

    check = await can(db, user.id, "HIRA.READ", PermissionContext())
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    accessible = await get_accessible_plants(db, user.id)

    now = datetime.now(timezone.utc)
    in30 = now + timedelta(days=30)
    ago90 = now - timedelta(days=90)

    base = (
        select(func.count())
        .select_from(HiraReviewCycle)
        .join(HiraEntry, HiraReviewCycle.entryId == HiraEntry.id)
        .join(HiraStudy, HiraEntry.studyId == HiraStudy.id)
    )
    if accessible is not None:
        if not accessible:
            return HiraDashboardReviewCompliance(overdue=0, dueSoon30Days=0, completedLast90Days=0)
        base = base.where(HiraStudy.plantId.in_(accessible))

    overdue = (
        await db.execute(
            base.where(HiraReviewCycle.status == "SCHEDULED").where(HiraReviewCycle.scheduledFor < now)
        )
    ).scalar_one() or 0
    due_soon = (
        await db.execute(
            base.where(HiraReviewCycle.status == "SCHEDULED")
            .where(HiraReviewCycle.scheduledFor >= now)
            .where(HiraReviewCycle.scheduledFor <= in30)
        )
    ).scalar_one() or 0
    completed_90 = (
        await db.execute(
            base.where(HiraReviewCycle.status == "COMPLETED").where(HiraReviewCycle.completedAt >= ago90)
        )
    ).scalar_one() or 0
    return HiraDashboardReviewCompliance(
        overdue=overdue, dueSoon30Days=due_soon, completedLast90Days=completed_90
    )


# ── P2-7 ISO 45001 §8.1.2 control-hierarchy validation (soft warning) ─────────
@router.post("/validate-control-hierarchy")
async def validate_control_hierarchy_endpoint(
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return ISO 45001 §8.1.2 hierarchy warnings for a set of control types.
    Soft (non-blocking) — the form shows warnings; the assessor acknowledges to save."""
    from app.services.iso_validation import validate_control_hierarchy
    control_types = payload.get("controlTypes") or []
    enforce = payload.get("enforce", True)
    warnings = validate_control_hierarchy(control_types, enforce=enforce)
    return {"warnings": warnings, "ok": len(warnings) == 0}
