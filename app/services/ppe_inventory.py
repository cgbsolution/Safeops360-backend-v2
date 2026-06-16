"""PPE Management service (PPE-01) — lifecycle engine + compliance.

The single place that mutates PPE inventory state. Every transition
(commission → issue → return → inspect → retire) is recorded with an audit
entry on the item's `stateHistory`, and issuance/inspection rows are
append-only. Compliance + lifecycle status (service life, inspection due) are
COMPUTED on read from the stored dates so they can never go stale.

Conventions mirror scr_register.py: module-level async functions that take the
request session and commit at the end.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ppe import (
    PpeInspection,
    PpeIssuance,
    PpeItem,
    PpeRequirementProfile,
    PpeType,
)
from app.models.plant import Plant
from app.models.user import User

# Thresholds (days)
INSPECTION_DUE_SOON_DAYS = 30
SERVICE_LIFE_WARN_DAYS = 90

# Profile scope id used for "everyone at the plant needs this" base PPE.
ALL_ROLES = "*ALL*"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    """Postgres timestamptz round-trips as aware; defensively coerce naive."""
    if d is None:
        return None
    return d if d.tzinfo is not None else d.replace(tzinfo=timezone.utc)


def add_years(d: datetime, years: int) -> datetime:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # Feb 29 → Feb 28 in a non-leap year
        return d.replace(year=d.year + years, day=28)


def _periodic_interval_days(ppe_type: PpeType) -> int | None:
    """Smallest non-null interval in the type's inspection schedule — the
    cadence that drives nextInspectionDueDate (pre_use has interval null)."""
    intervals = [
        s.get("interval_days")
        for s in (ppe_type.inspectionSchedule or [])
        if s.get("interval_days")
    ]
    return min(intervals) if intervals else None


# ─── Computed lifecycle status (read-side, never stored) ─────────────────


def inspection_status(item: PpeItem, now: datetime | None = None) -> tuple[str, int | None]:
    now = now or _utcnow()
    due = _aware(item.nextInspectionDueDate)
    if item.lastInspectedAt is None and due is None:
        return "never_inspected", None
    if due is None:
        return "current", None
    if now > due:
        return "overdue", (now - due).days
    if (due - now).days <= INSPECTION_DUE_SOON_DAYS:
        return "due_soon", None
    return "current", None


def service_life_status(item: PpeItem, now: datetime | None = None) -> tuple[bool, int]:
    """(exceeded, days_remaining)."""
    now = now or _utcnow()
    end = _aware(item.serviceLifeEndDate)
    if end is None:
        return False, 0
    remaining = (end - now).days
    return remaining < 0, remaining


def item_validity(item: PpeItem, now: datetime | None = None) -> dict[str, Any]:
    """Is this item fit to protect someone TODAY? Returns a status + reason
    used by both the Items view and People Compliance / (future) PTW gate."""
    now = now or _utcnow()
    insp, overdue_days = inspection_status(item, now)
    exceeded, remaining = service_life_status(item, now)

    blockers: list[str] = []
    warnings: list[str] = []
    if item.status in ("retired", "lost", "stolen", "recalled", "quarantined", "under_repair"):
        blockers.append(f"item is {item.status}")
    if item.batchUnderRecall:
        blockers.append("under batch recall")
    if exceeded:
        blockers.append("service life exceeded")
    if insp == "overdue":
        blockers.append(f"inspection overdue {overdue_days}d")
    if item.condition == "unserviceable":
        blockers.append("condition unserviceable")

    if insp == "due_soon":
        warnings.append("inspection due soon")
    if not exceeded and 0 <= remaining <= SERVICE_LIFE_WARN_DAYS:
        warnings.append(f"service life ends in {remaining}d")

    if blockers:
        level = "block"
    elif warnings:
        level = "warn"
    else:
        level = "pass"
    return {
        "level": level,
        "blockers": blockers,
        "warnings": warnings,
        "inspectionStatus": insp,
        "inspectionOverdueDays": overdue_days,
        "serviceLifeExceeded": exceeded,
        "serviceLifeRemainingDays": remaining,
    }


def item_dict(item: PpeItem, now: datetime | None = None) -> dict[str, Any]:
    now = now or _utcnow()
    v = item_validity(item, now)
    return {
        "id": item.id,
        "itemNumber": item.itemNumber,
        "serialNumber": item.serialNumber,
        "ppeTypeId": item.ppeTypeId,
        "ppeTypeCode": item.ppeTypeCode,
        "ppeTypeName": item.ppeTypeName,
        "manufacturer": item.manufacturer,
        "model": item.model,
        "batchLotNumber": item.batchLotNumber,
        "manufactureDate": _iso(item.manufactureDate),
        "plantId": item.plantId,
        "departmentId": item.departmentId,
        "storageLocation": item.storageLocation,
        "status": item.status,
        "condition": item.condition,
        "currentHolderUserId": item.currentHolderUserId,
        "issuedSince": _iso(item.issuedSince),
        "commissionedAt": _iso(item.commissionedAt),
        "serviceLifeEndDate": _iso(item.serviceLifeEndDate),
        "serviceLifeExceeded": v["serviceLifeExceeded"],
        "serviceLifeRemainingDays": v["serviceLifeRemainingDays"],
        "lastInspectedAt": _iso(item.lastInspectedAt),
        "nextInspectionDueDate": _iso(item.nextInspectionDueDate),
        "inspectionStatus": v["inspectionStatus"],
        "inspectionOverdueDays": v["inspectionOverdueDays"],
        "batchUnderRecall": item.batchUnderRecall,
        "validity": v["level"],
        "validityReason": "; ".join(v["blockers"] or v["warnings"]) or "valid",
        "versionNumber": item.versionNumber,
    }


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _push_history(item: PpeItem, from_status: str, to_status: str, by: str, reason: str) -> None:
    history = list(item.stateHistory or [])
    history.append(
        {
            "from_status": from_status,
            "to_status": to_status,
            "changed_at": _utcnow().isoformat(),
            "changed_by_user_id": by,
            "reason": reason,
        }
    )
    item.stateHistory = history
    item.versionNumber = (item.versionNumber or 1) + 1


# ─── Number generators ───────────────────────────────────────────────────


async def _plant_short(db: AsyncSession, plant_id: str) -> str:
    plant = await db.get(Plant, plant_id)
    return (plant.code if plant and plant.code else plant_id[:4]).upper()


async def _next_item_number(db: AsyncSession, plant_short: str, ppe_type: PpeType, offset: int = 0) -> str:
    sub = (ppe_type.subcategory or ppe_type.code.split("-")[0] or "ITEM").upper().replace("_", "")[:10]
    count = (
        await db.execute(select(func.count(PpeItem.id)).where(PpeItem.ppeTypeId == ppe_type.id))
    ).scalar_one()
    return f"PPE-{plant_short}-{sub}-{(count + offset + 1):04d}"


async def _next_issuance_number(db: AsyncSession, plant_short: str) -> str:
    year = _utcnow().year
    count = (
        await db.execute(
            select(func.count(PpeIssuance.id)).where(PpeIssuance.issuanceNumber.like(f"ISS-{plant_short}-{year}-%"))
        )
    ).scalar_one()
    return f"ISS-{plant_short}-{year}-{(count + 1):04d}"


# ─── Lifecycle transitions ───────────────────────────────────────────────


async def commission_items(
    db: AsyncSession,
    *,
    plant_id: str,
    ppe_type_id: str,
    quantity: int,
    manufacturer: str,
    model: str,
    batch_lot_number: str,
    manufacture_date: datetime,
    purchase_date: datetime | None,
    storage_location: str | None,
    actor_id: str,
    department_id: str | None = None,
    cost: float | None = None,
    serial_prefix: str | None = None,
) -> list[PpeItem]:
    """Goods receipt → create N in-stock items, service life + first inspection
    due computed from the type. Mirrors workflow §4.1."""
    ppe_type = await db.get(PpeType, ppe_type_id)
    if ppe_type is None:
        raise ValueError(f"PpeType {ppe_type_id} not found")

    now = _utcnow()
    interval = _periodic_interval_days(ppe_type)
    plant_short = await _plant_short(db, plant_id)
    created: list[PpeItem] = []
    for i in range(quantity):
        item_number = await _next_item_number(db, plant_short, ppe_type, offset=i)
        serial = f"{serial_prefix or ppe_type.code}-{batch_lot_number or 'NB'}-{i + 1:03d}"
        item = PpeItem(
            tenantId=None,
            itemNumber=item_number,
            serialNumber=serial,
            ppeTypeId=ppe_type.id,
            ppeTypeCode=ppe_type.code,
            ppeTypeName=ppe_type.name,
            manufacturer=manufacturer,
            model=model,
            batchLotNumber=batch_lot_number,
            manufactureDate=manufacture_date,
            purchaseDate=purchase_date,
            cost=cost,
            plantId=plant_id,
            departmentId=department_id,
            storageLocation=storage_location,
            status="in_stock",
            condition="new",
            commissionedAt=now,
            serviceLifeEndDate=add_years(manufacture_date, ppe_type.serviceLifeYears),
            nextInspectionDueDate=(now + timedelta(days=interval)) if interval else None,
            stateHistory=[
                {
                    "from_status": "—",
                    "to_status": "in_stock",
                    "changed_at": now.isoformat(),
                    "changed_by_user_id": actor_id,
                    "reason": "Commissioned (goods receipt)",
                }
            ],
            versionNumber=1,
        )
        db.add(item)
        created.append(item)
    await db.commit()
    for it in created:
        await db.refresh(it)
    return created


async def issue_item(
    db: AsyncSession,
    *,
    item_id: str,
    to_user_id: str,
    by_user_id: str,
    purpose: str = "personal_assignment",
    linked_permit_id: str | None = None,
    condition_at_issuance: str = "good",
    briefing_provided: bool = True,
    recipient_acknowledged: bool = True,
    expected_return_date: datetime | None = None,
    notes: str = "",
) -> PpeIssuance:
    """Assign an in-stock item to a person. Validates serviceability first."""
    item = await db.get(PpeItem, item_id)
    if item is None:
        raise ValueError("PPE item not found")
    if item.status != "in_stock":
        raise ValueError(f"Cannot issue — item is '{item.status}', not in stock")
    v = item_validity(item)
    if v["level"] == "block":
        raise ValueError(f"Cannot issue — {', '.join(v['blockers'])}")

    to_user = await db.get(User, to_user_id)
    by_user = await db.get(User, by_user_id)
    if to_user is None:
        raise ValueError("Recipient user not found")
    # Multi-plant scoping: stock belongs to one plant store; issuing it to a
    # worker of another plant records the issuance under the item's plant
    # where the worker's own compliance view never sees it. Plant-less users
    # (corporate roles) are allowed.
    if to_user.plantId and to_user.plantId != item.plantId:
        raise ValueError(
            f"Cannot issue — {to_user.name} belongs to a different plant. "
            "Transfer the stock or issue from their plant's store."
        )

    now = _utcnow()
    plant_short = await _plant_short(db, item.plantId)
    issuance = PpeIssuance(
        tenantId=None,
        issuanceNumber=await _next_issuance_number(db, plant_short),
        ppeItemId=item.id,
        ppeTypeCode=item.ppeTypeCode,
        ppeTypeName=item.ppeTypeName,
        serialNumber=item.serialNumber,
        issuedToUserId=to_user.id,
        issuedToName=to_user.name,
        issuedToDepartment=to_user.department or "",
        issuedToRole=to_user.role or "",
        issuedByUserId=by_user_id,
        issuedByName=by_user.name if by_user else "SYSTEM",
        issuedAt=now,
        expectedReturnDate=expected_return_date,
        issuancePurpose=purpose,
        linkedPermitId=linked_permit_id,
        conditionAtIssuance=condition_at_issuance,
        conditionNotesAtIssuance=notes,
        preIssuanceInspectionDone=True,
        preIssuanceInspectorUserId=by_user_id,
        recipientAcknowledged=recipient_acknowledged,
        recipientAcknowledgedAt=now if recipient_acknowledged else None,
        briefingProvided=briefing_provided,
        briefingByUserId=by_user_id if briefing_provided else None,
        status="active",
        plantId=item.plantId,
    )
    db.add(issuance)
    await db.flush()  # get issuance.id

    _push_history(item, item.status, "issued", by_user_id, f"Issued to {to_user.name} ({purpose})")
    item.status = "issued"
    item.currentHolderUserId = to_user.id
    item.currentIssuanceId = issuance.id
    item.issuedSince = now
    item.condition = condition_at_issuance

    await db.commit()
    await db.refresh(issuance)
    return issuance


async def return_item(
    db: AsyncSession,
    *,
    issuance_id: str,
    by_user_id: str,
    condition_at_return: str = "good",
    notes: str = "",
) -> PpeIssuance:
    """Close an active issuance and route the item back to stock or quarantine."""
    issuance = await db.get(PpeIssuance, issuance_id)
    if issuance is None:
        raise ValueError("Issuance not found")
    if issuance.status != "active":
        raise ValueError(f"Issuance already closed ({issuance.status})")
    item = await db.get(PpeItem, issuance.ppeItemId)

    now = _utcnow()
    damaged = condition_at_return in ("damaged", "destroyed")
    issuance.status = "damaged_return" if damaged else "returned"
    issuance.returnedAt = now
    issuance.returnedByUserId = by_user_id
    issuance.conditionAtReturn = condition_at_return
    issuance.conditionNotesAtReturn = notes
    issuance.postReturnInspectionRequired = damaged or condition_at_return == "fair"

    if item is not None:
        new_status = "quarantined" if damaged else "in_stock"
        _push_history(item, item.status, new_status, by_user_id, f"Returned ({condition_at_return})")
        item.status = new_status
        item.currentHolderUserId = None
        item.currentIssuanceId = None
        item.issuedSince = None
        item.condition = "needs_inspection" if issuance.postReturnInspectionRequired else condition_at_return
        item.lastConditionUpdateAt = now
        item.lastConditionUpdateByUserId = by_user_id

    await db.commit()
    await db.refresh(issuance)
    return issuance


async def record_inspection(
    db: AsyncSession,
    *,
    item_id: str,
    inspector_user_id: str,
    inspection_type: str = "periodic",
    trigger: str = "scheduled",
    overall_result: str = "pass",
    item_status_after: str | None = None,
    checklist_items: list[dict] | None = None,
    defects_found: list[dict] | None = None,
    conditions: str = "",
    is_third_party: bool = False,
    third_party_company: str = "",
    linked_permit_id: str | None = None,
    linked_incident_id: str | None = None,
) -> PpeInspection:
    """Record an inspection and apply its outcome to the item lifecycle."""
    item = await db.get(PpeItem, item_id)
    if item is None:
        raise ValueError("PPE item not found")
    ppe_type = await db.get(PpeType, item.ppeTypeId)
    inspector = await db.get(User, inspector_user_id)
    now = _utcnow()

    # Default the post-inspection status from the result if not given.
    if item_status_after is None:
        if overall_result == "fail":
            item_status_after = "quarantined_pending_repair"
        else:
            item_status_after = "returned_to_service"

    # A failed item can NEVER go straight back into service — only to
    # quarantine, repair, or end-of-life. Without this guard a fail +
    # returned_to_service combination silently reset the item to "good".
    ALLOWED_AFTER: dict[str, set[str]] = {
        "pass": {"returned_to_service", "recalled_to_store"},
        "conditional_pass": {"returned_to_service", "recalled_to_store", "quarantined_pending_repair"},
        "fail": {"quarantined_pending_repair", "retired", "condemned"},
    }
    allowed = ALLOWED_AFTER.get(overall_result)
    if allowed is None:
        raise ValueError(f"Unknown inspection result '{overall_result}'")
    if item_status_after not in allowed:
        raise ValueError(
            f"Inspection result '{overall_result}' cannot set item status "
            f"'{item_status_after}' — allowed: {', '.join(sorted(allowed))}"
        )

    _, remaining = service_life_status(item, now)
    inspection = PpeInspection(
        tenantId=None,
        ppeItemId=item.id,
        ppeTypeCode=item.ppeTypeCode,
        serialNumber=item.serialNumber,
        inspectionType=inspection_type,
        trigger=trigger,
        linkedPermitId=linked_permit_id,
        linkedIncidentId=linked_incident_id,
        conductedAt=now,
        inspectorUserId=inspector_user_id,
        inspectorName=inspector.name if inspector else "SYSTEM",
        inspectorQualification="Competent Person" if not is_third_party else "Third-party",
        isThirdPartyInspection=is_third_party,
        thirdPartyCompany=third_party_company,
        checklistItems=checklist_items or [],
        overallResult=overall_result,
        defectsFound=defects_found or [],
        conditions=conditions,
        reInspectionRequired=overall_result == "conditional_pass",
        itemStatusAfterInspection=item_status_after,
        serviceLifeRemainingDays=remaining,
        capaSpawned=any((d.get("severity") == "critical") for d in (defects_found or [])),
        plantId=item.plantId,
    )
    db.add(inspection)

    # Apply outcome to the item.
    interval = _periodic_interval_days(ppe_type) if ppe_type else None
    item.lastInspectedAt = now
    item.lastInspectedByUserId = inspector_user_id
    if item_status_after in ("returned_to_service",):
        # Back to whatever it was before — issued items stay issued.
        target = "issued" if item.currentHolderUserId else "in_stock"
        item.nextInspectionDueDate = (now + timedelta(days=interval)) if interval else None
        item.condition = "good"
    elif item_status_after == "quarantined_pending_repair":
        target = "quarantined"
        item.condition = "unserviceable"
    elif item_status_after in ("retired", "condemned"):
        target = "retired"
        item.condition = "unserviceable"
    elif item_status_after == "recalled_to_store":
        target = "in_stock"
    else:
        target = item.status

    # Every outcome other than returned_to_service physically withdraws the
    # item from its holder — the open issuance MUST close and the holder
    # fields MUST clear, or the item is "in stock" while an issuance is still
    # ACTIVE (it could then be issued a second time → two workers covered by
    # one physical item).
    if item_status_after != "returned_to_service" and item.currentIssuanceId:
        issuance = await db.get(PpeIssuance, item.currentIssuanceId)
        if issuance is not None and issuance.status == "active":
            issuance.status = "returned"
            issuance.returnedAt = now
            issuance.returnedByUserId = inspector_user_id
            issuance.conditionAtReturn = item.condition
            issuance.conditionNotesAtReturn = (
                f"Withdrawn by inspection ({overall_result} → {item_status_after})"
            )
        item.currentHolderUserId = None
        item.currentIssuanceId = None
        item.issuedSince = None

    if target != item.status:
        _push_history(item, item.status, target, inspector_user_id, f"Inspection {overall_result} → {item_status_after}")
        item.status = target

    await db.commit()
    await db.refresh(inspection)
    return inspection


async def retire_item(
    db: AsyncSession,
    *,
    item_id: str,
    actor_id: str,
    reason: str,
) -> PpeItem:
    """Retire an item (service-life end, condemned, recall, destruction). Closes
    any active issuance."""
    item = await db.get(PpeItem, item_id)
    if item is None:
        raise ValueError("PPE item not found")
    if item.status == "retired":
        return item

    # Close an open issuance if the item is currently held.
    if item.currentIssuanceId:
        issuance = await db.get(PpeIssuance, item.currentIssuanceId)
        if issuance and issuance.status == "active":
            issuance.status = "returned"
            issuance.returnedAt = _utcnow()
            issuance.returnedByUserId = actor_id
            issuance.conditionAtReturn = "destroyed"
            issuance.conditionNotesAtReturn = f"Recovered for retirement: {reason}"

    _push_history(item, item.status, "retired", actor_id, reason)
    item.status = "retired"
    item.condition = "unserviceable"
    item.currentHolderUserId = None
    item.currentIssuanceId = None
    item.issuedSince = None
    await db.commit()
    await db.refresh(item)
    return item


# ─── People Compliance (the operational heart) ───────────────────────────


async def people_compliance(db: AsyncSession, *, plant_id: str) -> dict[str, Any]:
    """For every person with a PPE requirement profile, what do they hold and
    is each holding valid? Drives the People Compliance view + the audit pack.
    Requirement profiles are keyed by User.role (plus an ALL-roles base set)."""
    now = _utcnow()

    profiles = (
        await db.execute(
            select(PpeRequirementProfile)
            .where(PpeRequirementProfile.plantId == plant_id)
            .where(PpeRequirementProfile.isActive.is_(True))
            .where(PpeRequirementProfile.scopeType == "role")
        )
    ).scalars().all()
    role_reqs: dict[str, list[dict]] = {}
    for p in profiles:
        role_reqs.setdefault(p.scopeId, []).extend(p.requiredPpe or [])
    base_reqs = role_reqs.get(ALL_ROLES, [])

    users = (await db.execute(select(User).where(User.plantId == plant_id))).scalars().all()

    # Active issuances for the plant → held items per user.
    issuances = (
        await db.execute(
            select(PpeIssuance)
            .where(PpeIssuance.plantId == plant_id)
            .where(PpeIssuance.status == "active")
        )
    ).scalars().all()
    item_ids = {i.ppeItemId for i in issuances}
    items_by_id: dict[str, PpeItem] = {}
    if item_ids:
        rows = (await db.execute(select(PpeItem).where(PpeItem.id.in_(item_ids)))).scalars().all()
        items_by_id = {it.id: it for it in rows}
    held_by_user: dict[str, list[tuple[PpeIssuance, PpeItem | None]]] = {}
    for iss in issuances:
        held_by_user.setdefault(iss.issuedToUserId, []).append((iss, items_by_id.get(iss.ppeItemId)))

    people: list[dict] = []
    n_compliant = n_gaps = n_critical = 0
    for u in users:
        reqs = _dedup_requirements(base_reqs + role_reqs.get(u.role or "", []))
        if not reqs:
            continue
        holdings = held_by_user.get(u.id, [])
        req_rows: list[dict] = []
        # Overall compliance is driven by MANDATORY items only. A missing
        # recommended item is surfaced (status "recommended") but never makes a
        # person non-compliant — they have everything the law requires.
        mand_block = mand_warn = False
        for req in reqs:
            type_code = req.get("ppe_type_code")
            level = req.get("requirement_level", "mandatory")
            is_mandatory = level == "mandatory"
            # A person can hold several items of one type (e.g. an expired
            # pair not yet returned plus its replacement) — judge them by
            # their BEST holding, never the first row found.
            candidates = [(iss, it) for (iss, it) in holdings if iss.ppeTypeCode == type_code]
            match = None
            if candidates:
                rank = {"pass": 0, "warn": 1, "block": 2}
                match = min(
                    candidates,
                    key=lambda c: rank[item_validity(c[1], now)["level"]] if c[1] else rank["block"],
                )
            if match is None:
                status = "block" if is_mandatory else "recommended"
                if is_mandatory:
                    mand_block = True
                req_rows.append({
                    "ppeTypeCode": type_code,
                    "ppeTypeName": req.get("ppe_type_name", type_code),
                    "requirementLevel": level,
                    "held": False,
                    "status": status,
                    "reason": "not issued",
                })
                continue
            iss, it = match
            v = item_validity(it, now) if it else {"level": "block", "blockers": ["item missing"], "warnings": []}
            level_status = v["level"]
            if is_mandatory:
                if level_status == "block":
                    mand_block = True
                elif level_status == "warn":
                    mand_warn = True
            req_rows.append({
                "ppeTypeCode": type_code,
                "ppeTypeName": req.get("ppe_type_name", type_code),
                "requirementLevel": level,
                "held": True,
                "itemNumber": it.itemNumber if it else None,
                "serialNumber": it.serialNumber if it else None,
                "status": level_status,
                "reason": "; ".join(v["blockers"] or v["warnings"]) or "valid",
            })

        overall = "critical" if mand_block else ("gaps" if mand_warn else "compliant")
        if overall == "compliant":
            n_compliant += 1
        elif overall == "gaps":
            n_gaps += 1
        else:
            n_critical += 1
        people.append({
            "userId": u.id,
            "name": u.name,
            "role": u.role,
            "department": u.department or "—",
            "overall": overall,
            "requirements": req_rows,
        })

    people.sort(key=lambda p: ({"critical": 0, "gaps": 1, "compliant": 2}[p["overall"]], p["name"]))
    return {
        "plantId": plant_id,
        "summary": {
            "totalPeople": len(people),
            "compliant": n_compliant,
            "gaps": n_gaps,
            "criticalGaps": n_critical,
        },
        "people": people,
    }


def _dedup_requirements(reqs: list[dict]) -> list[dict]:
    """Collapse duplicate PPE types; mandatory beats recommended."""
    by_code: dict[str, dict] = {}
    for r in reqs:
        code = r.get("ppe_type_code")
        if not code:
            continue
        if code not in by_code:
            by_code[code] = dict(r)
        elif r.get("requirement_level") == "mandatory":
            by_code[code]["requirement_level"] = "mandatory"
    return list(by_code.values())
