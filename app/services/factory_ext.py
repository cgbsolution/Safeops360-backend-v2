"""Facilities extension — service layer.

Pure helpers (no FastAPI imports): status engines computed on read, cross-tab
validators (return a list of hard-fail messages; the router raises 400),
lifecycle transition rules, Out-builders, and the detail-page loader. Mirrors
the date-math conventions in ``services/factory.py`` (``_aware`` +
``datetime.now(timezone.utc)``)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.factory_ext import (
    FactoryEquipment,
    FactoryLifecycleEvent,
    FactoryProfileChangeRequest,
    HazardousMaterial,
    RegulatoryRegistration,
)
from app.schemas import factory_ext as Sx
from app.services.cams import user_name_map

# Days-before-due that flips a computed status to the "soon" band.
SHELF_LIFE_LEAD_DAYS = 30
EQUIPMENT_LEAD_DAYS = 30


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ════════════════════════════════════════════════════════════════════════════
# Equipment — statutory-regime compliance engine
# ════════════════════════════════════════════════════════════════════════════
def _equipment_regimes(e: FactoryEquipment) -> list[tuple[str, bool, datetime | None]]:
    return [
        ("PUWER", e.puwerRequired, e.puwerNextDue),
        ("LOLER", e.lolerRequired, e.lolerNextDue),
        ("ELECTRICAL", e.electricalSafetyRequired, e.electricalNextDue),
        ("NOISE", e.noiseAssessmentRequired, e.noiseLastTest),  # noise = re-test annually; uses last-test + 1y below
    ]


def equipment_compliance(e: FactoryEquipment) -> tuple[str, datetime | None, list[str]]:
    """Returns (status, nextComplianceDue, overdueRegimes).

    status: OVERDUE if any required regime is past due, ATTENTION if any is due
    within the lead window OR a required regime has no date recorded, OK if all
    required regimes have a comfortably-future date, NA if nothing is required.
    Noise has no explicit next-due column, so it re-tests annually off
    ``noiseLastTest`` (a missing last-test reads as "needs a baseline")."""
    now = _now()
    soon = now + timedelta(days=EQUIPMENT_LEAD_DAYS)
    overdue: list[str] = []
    attention = False
    any_required = False
    next_dues: list[datetime] = []

    for name, required, raw_due in _equipment_regimes(e):
        if not required:
            continue
        any_required = True
        due = raw_due
        if name == "NOISE" and raw_due is not None:
            due = _aware(raw_due) + timedelta(days=365)
        if due is None:
            # required but never scheduled → needs attention
            attention = True
            continue
        due = _aware(due)
        next_dues.append(due)
        if due < now:
            overdue.append(name)
        elif due <= soon:
            attention = True

    if not any_required:
        status = "NA"
    elif overdue:
        status = "OVERDUE"
    elif attention:
        status = "ATTENTION"
    else:
        status = "OK"
    next_due = min(next_dues) if next_dues else None

    # Factor in the most recent recorded statutory inspection (Change 3). A FAIL
    # is the strongest non-compliant signal and overrides the regime view; a
    # PASS/CONDITIONAL on regime-less equipment lifts it out of the neutral "NA".
    result = (e.lastInspectionResult or "").upper()
    if result == "FAIL":
        return "OVERDUE", next_due, overdue if "INSPECTION" in overdue else [*overdue, "INSPECTION"]
    if status == "NA" and result in ("PASS", "CONDITIONAL_PASS"):
        return ("OK" if result == "PASS" else "ATTENTION"), next_due, overdue
    return status, next_due, overdue


def equipment_operator_gap(e: FactoryEquipment) -> bool:
    """HIGH-hazard equipment must carry at least one operator whose certification
    has not lapsed. ``certifiedOperators`` rows look like
    {name, certifiedOn, expiresOn}; a row with no expiry counts as valid."""
    if e.hazardLevel != "HIGH":
        return False
    ops = e.certifiedOperators or []
    if not ops:
        return True
    now = _now()
    for op in ops:
        exp = op.get("expiresOn") if isinstance(op, dict) else None
        if not exp:
            return False  # no expiry → treated as currently valid
        try:
            if _aware(datetime.fromisoformat(str(exp).replace("Z", "+00:00"))) >= now:
                return False
        except ValueError:
            return False  # unparseable date → don't over-flag
    return True


def equipment_out(e: FactoryEquipment) -> Sx.EquipmentOut:
    o = Sx.EquipmentOut.model_validate(e)
    o.complianceStatus, o.nextComplianceDue, o.overdueRegimes = equipment_compliance(e)
    o.operatorCertGapFlag = equipment_operator_gap(e)
    return o


def inspection_out(insp) -> Sx.EquipmentInspectionOut:
    return Sx.EquipmentInspectionOut.model_validate(insp)


# Re-test horizon (days) applied to the required regimes after a passing
# inspection — a clean PASS buys a full year; a CONDITIONAL_PASS only a quarter.
INSPECTION_HORIZON_DAYS = {"PASS": 365, "CONDITIONAL_PASS": 90}


def apply_inspection_to_equipment(e: FactoryEquipment, when: datetime, result: str) -> None:
    """Roll the parent equipment's cached inspection state forward after an
    inspection is recorded (Change 3). PASS / CONDITIONAL_PASS also advance every
    *required* statutory regime's last + next-due so the computed compliance
    badge clears; a FAIL leaves the regime dates untouched (the compliance engine
    forces OVERDUE off ``lastInspectionResult`` instead)."""
    e.lastInspectionDate = when
    e.lastInspectionResult = result
    # The Equipment tab surfaces the latest service touch via lastMaintenanceDate
    # (build-spec §Change 3): an inspection is one such touch.
    e.lastMaintenanceDate = when
    e.lastMaintenanceType = f"Statutory inspection ({result})"
    horizon = INSPECTION_HORIZON_DAYS.get(result)
    if horizon is None:
        return
    nxt = _aware(when) + timedelta(days=horizon)
    if e.puwerRequired:
        e.puwerLastInspection, e.puwerNextDue = when, nxt
    if e.lolerRequired:
        e.lolerLastInspection, e.lolerNextDue = when, nxt
    if e.electricalSafetyRequired:
        e.electricalLastCheck, e.electricalNextDue = when, nxt
    if e.noiseAssessmentRequired:
        e.noiseLastTest = when


# ════════════════════════════════════════════════════════════════════════════
# Hazardous Material — shelf-life / containment / utilisation / training
# ════════════════════════════════════════════════════════════════════════════
def shelf_life_status(expiry: datetime | None) -> str:
    if expiry is None:
        return "NA"
    now = _now()
    exp = _aware(expiry)
    if exp < now:
        return "EXPIRED"
    if exp <= now + timedelta(days=SHELF_LIFE_LEAD_DAYS):
        return "EXPIRING_SOON"
    return "VALID"


def days_to(expiry: datetime | None) -> int | None:
    if expiry is None:
        return None
    return (_aware(expiry) - _now()).days


def training_status(trained: int, total: int) -> str:
    if total <= 0:
        return "NA"
    if trained >= total:
        return "ALL_TRAINED"
    if trained <= 0:
        return "NOT_TRAINED"
    return "PARTIALLY_TRAINED"


def hazmat_out(h: HazardousMaterial) -> Sx.HazmatOut:
    o = Sx.HazmatOut.model_validate(h)
    o.shelfLifeStatus = shelf_life_status(h.expiryDate)
    o.daysToExpiry = days_to(h.expiryDate)
    if h.maxAllowableQty and h.maxAllowableQty > 0:
        o.utilisationPct = round(h.quantityStored / h.maxAllowableQty * 100, 1)
        o.overCapacity = h.quantityStored > h.maxAllowableQty
    o.reorderReached = h.reorderLevel is not None and h.quantityStored <= h.reorderLevel
    o.containmentRequiredVolume = round(h.quantityStored * 1.1, 2) if h.quantityStored else 0.0
    if h.secondaryContainmentPresent:
        o.containmentOk = (h.secondaryContainmentVolume or 0) >= (o.containmentRequiredVolume or 0)
    o.trainingStatus = training_status(h.handlersTrainedCount, h.handlersTotalCount)
    o.sdsMissingFlag = h.hazmatClassification == "HIGH" and not h.sdsDocId
    return o


# ════════════════════════════════════════════════════════════════════════════
# Regulatory Registration — renewal status engine
# ════════════════════════════════════════════════════════════════════════════
def regulatory_status(
    expiry: datetime | None,
    alert_threshold_days: int | None,
    renewal_in_progress: bool,
    stored: str | None = None,
) -> str:
    """Effective registration status. SUSPENDED is a manual override and passes
    through; the rest are derived. An expired registration always reads EXPIRED
    (the strongest signal); an in-progress renewal that is not yet expired reads
    PENDING_RENEWAL; otherwise expiry + threshold drives EXPIRING_SOON / VALID."""
    if stored == "SUSPENDED":
        return "SUSPENDED"
    if expiry is None:
        return "PENDING_RENEWAL" if renewal_in_progress else "VALID"
    now = _now()
    exp = _aware(expiry)
    if exp < now:
        return "EXPIRED"
    if renewal_in_progress:
        return "PENDING_RENEWAL"
    lead = 90 if alert_threshold_days is None else max(0, alert_threshold_days)
    if exp <= now + timedelta(days=lead):
        return "EXPIRING_SOON"
    return "VALID"


def regulatory_out(r: RegulatoryRegistration) -> Sx.RegulatoryOut:
    o = Sx.RegulatoryOut.model_validate(r)
    o.status = regulatory_status(r.expiryDate, r.alertThresholdDays, r.renewalInProgress, r.status)
    o.daysToExpiry = days_to(r.expiryDate)
    return o


def lifecycle_event_out(ev: FactoryLifecycleEvent) -> Sx.LifecycleEventOut:
    return Sx.LifecycleEventOut.model_validate(ev)


# ════════════════════════════════════════════════════════════════════════════
# Cross-tab / field validators (return hard-fail messages; router raises 400)
# ════════════════════════════════════════════════════════════════════════════
def validate_hazmat(data: dict) -> list[str]:
    """Hard rules enforced on hazmat create/update. ``data`` is the *effective*
    post-merge state (so PATCH validates the full resulting row)."""
    errors: list[str] = []
    qty = data.get("quantityStored")
    max_q = data.get("maxAllowableQty")
    if qty is not None and qty < 0:
        errors.append("Quantity stored cannot be negative.")
    if qty is not None and max_q is not None and max_q > 0 and qty > max_q:
        errors.append(f"Quantity stored ({qty}) exceeds the maximum allowable ({max_q}).")
    if data.get("secondaryContainmentPresent"):
        required = round((qty or 0) * 1.1, 2)
        vol = data.get("secondaryContainmentVolume") or 0
        if vol < required:
            errors.append(
                f"Secondary containment must hold ≥110% of stored quantity ({required}); got {vol}."
            )
    iss, exp = data.get("issueDate"), data.get("expiryDate")
    if iss and exp and _aware(exp) <= _aware(iss):
        errors.append("Expiry date must be after the issue date.")
    return errors


def validate_equipment(data: dict) -> list[str]:
    errors: list[str] = []
    cap = data.get("capacity")
    if cap is not None and cap <= 0:
        errors.append("Capacity must be greater than 0.")
    last_m, next_s = data.get("lastMaintenanceDate"), data.get("nextScheduledDate")
    if last_m and next_s and _aware(next_s) <= _aware(last_m):
        errors.append("Next scheduled maintenance must be after the last maintenance date.")
    return errors


def validate_regulatory(data: dict) -> list[str]:
    errors: list[str] = []
    iss, exp = data.get("issueDate"), data.get("expiryDate")
    if iss and exp and _aware(exp) <= _aware(iss):
        errors.append("Expiry date must be after the issue date.")
    return errors


# ════════════════════════════════════════════════════════════════════════════
# Profile change requests (governed edit → Plant Head → Lead Auditor)
# ════════════════════════════════════════════════════════════════════════════
# Editable master-data fields, with the label the audit trail shows. Anything
# not listed here is either immutable (siteId, factoryCode) or lives in its own
# governed sub-register (buildings, workforce, certifications …).
PROFILE_FIELD_LABELS: dict[str, str] = {
    "factoryName": "Factory name",
    "status": "Operational status",
    "ownershipType": "Ownership",
    "primaryIndustry": "Primary industry",
    "addressLine": "Address",
    "city": "City",
    "state": "State",
    "pincode": "Pincode",
    "latitude": "Latitude",
    "longitude": "Longitude",
    "establishedYear": "Established year",
    "factoryLicenseNo": "Factory licence no.",
    "factoryLicenseValidUntil": "Licence valid until",
    "registrationNos": "Registrations",
    "applicableActs": "Applicable acts",
    "pollutionControlBoard": "Pollution Control Board",
    "totalLandAreaSqm": "Total land area (m²)",
    "builtUpAreaSqm": "Built-up area (m²)",
    "buildingCount": "Building count",
    "profileStatus": "Profile status",
}

# A profile's master data becomes governed once it has been signed off — i.e.
# reached the terminal ACTIVE lifecycle stage. Before that the factory is still
# being drafted and edits are written straight through (with history).
GOVERNED_STAGES = frozenset({"ACTIVE"})

# status → the permission that may act on it, and where it goes next.
CR_PENDING_UNIT = "PENDING_UNIT"
CR_PENDING_COMPLIANCE = "PENDING_COMPLIANCE"
CR_APPLIED = "APPLIED"
CR_REJECTED = "REJECTED"
CR_WITHDRAWN = "WITHDRAWN"

CR_OPEN_STATUSES = (CR_PENDING_UNIT, CR_PENDING_COMPLIANCE)

# Who signs off at each step. Two sequential approvals: the Unit signs off that
# the change is factually right, Compliance signs off that it is admissible.
CR_STEP_PERMISSION: dict[str, str] = {
    CR_PENDING_UNIT: "FACILITY.PROFILE_APPROVE_UNIT",
    CR_PENDING_COMPLIANCE: "FACILITY.PROFILE_APPROVE_COMPLIANCE",
}
CR_STEP_ROLE_LABEL: dict[str, str] = {
    CR_PENDING_UNIT: "Plant Head (Unit)",
    CR_PENDING_COMPLIANCE: "Compliance Team — Lead Auditor",
}


def profile_edit_is_governed(stage: str | None) -> bool:
    return (stage or "") in GOVERNED_STAGES


def _render(value) -> str | None:
    """Stringify a field value for the human-readable diff."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware(value).date().isoformat()
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(" ".join(str(v) for v in item.values() if v not in (None, "")))
            else:
                parts.append(str(item))
        return ", ".join(p for p in parts if p) or "—"
    return str(value)


def _comparable(value):
    """Normalise for equality so a Pydantic model and a stored dict compare equal."""
    if isinstance(value, (list, tuple)):
        return [_comparable(v) for v in value]
    if isinstance(value, dict):
        return {k: _comparable(v) for k, v in sorted(value.items())}
    if hasattr(value, "model_dump"):
        return _comparable(value.model_dump())
    if isinstance(value, datetime):
        return _aware(value)
    return value


def build_profile_diff(profile, data: dict) -> list[dict]:
    """The [{field, label, from, to}] diff between a profile and a proposed
    update payload. Unchanged fields are dropped, so an empty result means the
    submit was a no-op and the caller should not open a change request."""
    diff: list[dict] = []
    for field, new_value in data.items():
        if field not in PROFILE_FIELD_LABELS:
            continue
        old_value = getattr(profile, field, None)
        if _comparable(old_value) == _comparable(new_value):
            continue
        diff.append(
            {
                "field": field,
                "label": PROFILE_FIELD_LABELS[field],
                "from": _render(old_value),
                "to": _render(new_value),
                # The raw value the approval actually applies. Kept alongside the
                # rendered strings so applying a request never re-parses display
                # text (dates especially would not survive the round trip).
                "value": _jsonable(new_value),
            }
        )
    return diff


def _jsonable(value):
    """Value coerced into something the JSON column can hold verbatim."""
    if isinstance(value, datetime):
        return _aware(value).isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def apply_change_request(profile, cr: FactoryProfileChangeRequest) -> None:
    """Write an approved request's values onto the profile. Datetime fields are
    revived from their ISO form; everything else is stored as-is."""
    datetime_fields = {"factoryLicenseValidUntil"}
    for change in cr.changes or []:
        field = change.get("field")
        if field not in PROFILE_FIELD_LABELS:
            continue
        value = change.get("value")
        if field in datetime_fields and isinstance(value, str) and value:
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                continue
        setattr(profile, field, value)


async def next_change_request_version(db: AsyncSession, profile_id: str) -> int:
    from sqlalchemy import func as _func

    current = (
        await db.execute(
            select(_func.max(FactoryProfileChangeRequest.version)).where(
                FactoryProfileChangeRequest.factoryProfileId == profile_id
            )
        )
    ).scalar()
    return int(current or 0) + 1


async def open_change_request(db: AsyncSession, profile_id: str) -> FactoryProfileChangeRequest | None:
    """The single in-flight request for a profile, if any."""
    return (
        await db.execute(
            select(FactoryProfileChangeRequest)
            .where(FactoryProfileChangeRequest.factoryProfileId == profile_id)
            .where(FactoryProfileChangeRequest.status.in_(CR_OPEN_STATUSES))
            .where(FactoryProfileChangeRequest.isDeleted.is_(False))
            .order_by(FactoryProfileChangeRequest.version.desc())
        )
    ).scalars().first()


def change_request_out(cr: FactoryProfileChangeRequest, names: dict[str, str] | None = None) -> Sx.ChangeRequestOut:
    o = Sx.ChangeRequestOut.model_validate(cr)
    names = names or {}
    o.requestedByName = names.get(cr.requestedBy or "")
    o.unitApprovedByName = names.get(cr.unitApprovedBy or "")
    o.complianceApprovedByName = names.get(cr.complianceApprovedBy or "")
    o.rejectedByName = names.get(cr.rejectedBy or "")
    return o


# ════════════════════════════════════════════════════════════════════════════
# Lifecycle transition rules
# ════════════════════════════════════════════════════════════════════════════
# Forward transitions only (the revision loop-back VALIDATION→EXECUTION is its
# own endpoint). Stage owner = the role accountable for acting in that stage.
FORWARD_TRANSITIONS: dict[str, list[str]] = {
    "INITIATED": ["EXECUTION"],
    "EXECUTION": ["VALIDATION"],
    "VALIDATION": ["ACTIVE"],
    "ACTIVE": [],
    "ARCHIVED": [],
}

STAGE_OWNER_ROLE: dict[str, str | None] = {
    "INITIATED": "PLANT_HEAD",
    "EXECUTION": "PLANT_HEAD",
    "VALIDATION": "HSE_MANAGER",
    "ACTIVE": "HSE_MANAGER",
    "ARCHIVED": None,
}


def stage_owner_role(stage: str) -> str | None:
    return STAGE_OWNER_ROLE.get(stage)


def allowed_next_stages(stage: str) -> list[str]:
    return FORWARD_TRANSITIONS.get(stage, [])


def can_request_revisions(stage: str) -> bool:
    # HSE bounces a factory back from VALIDATION to EXECUTION for fixes.
    return stage == "VALIDATION"


def validate_advance(current: str, to: str) -> str | None:
    """None when legal, else an error message."""
    if current in ("ACTIVE", "ARCHIVED"):
        return f"Factory is in a terminal lifecycle stage ({current}); cannot advance."
    if to not in allowed_next_stages(current):
        nxt = allowed_next_stages(current) or ["—"]
        return f"Illegal transition {current} → {to}. Allowed next: {', '.join(nxt)}."
    return None


# ════════════════════════════════════════════════════════════════════════════
# Detail-page loader (called from the factory router's _profile_detail)
# ════════════════════════════════════════════════════════════════════════════
async def load_profile_extras(db: AsyncSession, profile_id: str) -> dict:
    """Returns the four extension lists for a profile, serialized + status-
    computed, for the F-02 detail payload."""
    equipment = (
        await db.execute(
            select(FactoryEquipment)
            .where(FactoryEquipment.factoryProfileId == profile_id)
            .where(FactoryEquipment.isDeleted.is_(False))
            .order_by(FactoryEquipment.equipmentName.asc())
        )
    ).scalars().all()
    hazmat = (
        await db.execute(
            select(HazardousMaterial)
            .where(HazardousMaterial.factoryProfileId == profile_id)
            .where(HazardousMaterial.isDeleted.is_(False))
            .order_by(HazardousMaterial.chemicalName.asc())
        )
    ).scalars().all()
    regs = (
        await db.execute(
            select(RegulatoryRegistration)
            .where(RegulatoryRegistration.factoryProfileId == profile_id)
            .where(RegulatoryRegistration.isDeleted.is_(False))
            .order_by(RegulatoryRegistration.expiryDate.asc().nulls_last())
        )
    ).scalars().all()
    events = (
        await db.execute(
            select(FactoryLifecycleEvent)
            .where(FactoryLifecycleEvent.factoryProfileId == profile_id)
            .where(FactoryLifecycleEvent.isDeleted.is_(False))
            .order_by(FactoryLifecycleEvent.createdAt.desc())
        )
    ).scalars().all()
    # Governed-edit trail: newest first, and the one open request (if any)
    # surfaced separately so the detail page can show its approval banner
    # without the client having to re-derive "which of these is in flight".
    change_rows = (
        await db.execute(
            select(FactoryProfileChangeRequest)
            .where(FactoryProfileChangeRequest.factoryProfileId == profile_id)
            .where(FactoryProfileChangeRequest.isDeleted.is_(False))
            .order_by(FactoryProfileChangeRequest.version.desc())
        )
    ).scalars().all()
    names = await user_name_map(
        db,
        [
            uid
            for cr in change_rows
            for uid in (cr.requestedBy, cr.unitApprovedBy, cr.complianceApprovedBy, cr.rejectedBy)
        ],
    )
    change_requests = [change_request_out(cr, names) for cr in change_rows]
    pending = next((c for c in change_requests if c.status in CR_OPEN_STATUSES), None)
    return {
        "equipment": [equipment_out(e) for e in equipment],
        "hazardousMaterials": [hazmat_out(h) for h in hazmat],
        "regulatoryRegistrations": [regulatory_out(r) for r in regs],
        "lifecycleEvents": [lifecycle_event_out(ev) for ev in events],
        "changeRequests": change_requests,
        "pendingChangeRequest": pending,
    }
