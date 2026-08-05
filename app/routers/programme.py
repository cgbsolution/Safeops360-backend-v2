"""Annual Audit Programme router.

docs/cams/08-audit-programme.md.

Permission codes reuse the existing CAMS set — no RBAC migration needed:
  CAMS.READ          read programmes, cycles, coverage, variance
  CAMS.SCHEDULE      create/edit programmes, cycles, scope units, slots
  CAMS.CLOSE         approve a cycle, transition a slot, close a cycle
  CAMS.ANALYTICS     coverage matrix + auditor load
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.programme import (
    AuditProgramme,
    ProgrammeAmendment,
    ProgrammeCycle,
    ProgrammeRecommendation,
    ProgrammeReview,
    ProgrammeScopeUnit,
    ProgrammeSlot,
    SlotScopeUnit,
)
from app.models.user import User
from app.services.access_scope import DEPLOYMENT_TENANT_ID, build_query_scope
from app.services.permissions import PermissionContext, can
from app.services.plant_directory import resolve_plant_names, site_label
from app.services.programme import coverage as cov
from app.services.programme import lifecycle as lc
from app.services.programme import materialise as mat
from app.services.programme import recommend as rec

router = APIRouter(prefix="/api/programme", tags=["programme"])


async def _require(db: AsyncSession, user: User, code: str, *, plant_id=None) -> None:
    res = await can(db, user.id, code, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission {code}")


def _bad(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


# ── Programmes ───────────────────────────────────────────────────────


class ProgrammeBody(BaseModel):
    programmeCode: str
    name: str
    objectives: str = ""
    scopeStatement: str = ""
    standardRefs: list[str] = Field(default_factory=list)
    ownerUserId: str
    tenantId: str | None = None
    fullCoverageThresholdPct: float = 80.0


@router.get("")
async def list_programmes(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    await _require(db, user, "CAMS.READ")
    # Tenant-scoped through the platform seam, not by hand. A programme has no
    # plantId — sites enter as scope units (§1 Decision 3) — so `apply` filters
    # on tenant alone here, which is the correct boundary for an estate-wide
    # artefact. Unscoped, this leaked every tenant's programme the moment a
    # second one existed.
    scope = await build_query_scope(db, user.id, "CAMS.READ")
    rows = list(
        (
            await db.execute(
                scope.apply(
                    select(AuditProgramme).where(AuditProgramme.isDeleted.is_(False)),
                    AuditProgramme,
                ).order_by(AuditProgramme.createdAt.desc())
            )
        ).scalars().all()
    )
    out = []
    for p in rows:
        cycles = list(
            (
                await db.execute(
                    select(ProgrammeCycle)
                    .where(ProgrammeCycle.programmeId == p.id)
                    .order_by(ProgrammeCycle.periodStart.desc())
                )
            ).scalars().all()
        )
        out.append(
            {
                "id": p.id,
                "programmeCode": p.programmeCode,
                "name": p.name,
                "objectives": p.objectives,
                "scopeStatement": p.scopeStatement,
                "standardRefs": p.standardRefs,
                "ownerUserId": p.ownerUserId,
                "status": p.status,
                "fullCoverageThresholdPct": p.fullCoverageThresholdPct,
                "cycles": [
                    {
                        "id": c.id,
                        "cycleLabel": c.cycleLabel,
                        "status": c.status,
                        "periodStart": c.periodStart.isoformat(),
                        "periodEnd": c.periodEnd.isoformat(),
                        "periodsPerCycle": c.periodsPerCycle,
                        # Governance provenance — who moved it, and when. The
                        # action bar needs it to enforce four-eyes in the UI
                        # rather than only discovering it on a failed POST.
                        "submittedByUserId": c.submittedByUserId,
                        "submittedForReviewAt": (
                            c.submittedForReviewAt.isoformat() if c.submittedForReviewAt else None
                        ),
                        "approvedByUserId": c.approvedByUserId,
                        "approvedAt": c.approvedAt.isoformat() if c.approvedAt else None,
                        "approvedSnapshotHash": c.approvedSnapshotHash,
                        "activatedAt": c.activatedAt.isoformat() if c.activatedAt else None,
                        "closedAt": c.closedAt.isoformat() if c.closedAt else None,
                    }
                    for c in cycles
                ],
            }
        )
    return {"items": out, "total": len(out)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_programme(
    body: ProgrammeBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.SCHEDULE")
    data = body.model_dump()
    # Stamped, never left NULL. `list_programmes` filters on the tenant seam, so
    # a programme created without one would be invisible to the screen that
    # created it — a silent write-only record.
    data["tenantId"] = data.get("tenantId") or DEPLOYMENT_TENANT_ID
    code = (data.get("programmeCode") or "").strip()
    if not code:
        raise HTTPException(400, "A programme code is required.")
    data["programmeCode"] = code
    clash = (
        await db.execute(select(AuditProgramme).where(AuditProgramme.programmeCode == code))
    ).scalars().first()
    if clash is not None:
        raise HTTPException(
            400,
            f"Programme code “{code}” is already used by “{clash.name}”. Codes are how a "
            "certification body cites a programme, so they are unique.",
        )
    row = AuditProgramme(**data, createdBy=user.id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"id": row.id, "ok": True, "programmeCode": row.programmeCode}


# ── Cycles ───────────────────────────────────────────────────────────


class CycleBody(BaseModel):
    programmeId: str
    cycleLabel: str
    periodStart: date
    periodEnd: date
    periodsPerCycle: int = 4


@router.post("/cycles", status_code=status.HTTP_201_CREATED)
async def create_cycle(
    body: CycleBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.SCHEDULE")
    if body.periodEnd <= body.periodStart:
        raise HTTPException(400, "periodEnd must be after periodStart")
    row = ProgrammeCycle(**body.model_dump(), createdBy=user.id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"id": row.id, "ok": True, "status": row.status}


@router.post("/cycles/{cycle_id}/submit")
async def submit_cycle(
    cycle_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    await _require(db, user, "CAMS.SCHEDULE")
    try:
        out = await lc.submit_cycle_for_review(db, cycle_id=cycle_id, user=user)
    except ValueError as e:
        raise _bad(e)
    await db.commit()
    return out


@router.post("/cycles/{cycle_id}/return-to-draft")
async def return_cycle_to_draft(
    cycle_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """The reviewer's "not yet". Legal in the state machine and never exposed."""
    await _require(db, user, "CAMS.SCHEDULE")
    try:
        out = await lc.return_cycle_to_draft(db, cycle_id=cycle_id, user=user)
    except ValueError as e:
        raise _bad(e)
    await db.commit()
    return out


class ApproveBody(BaseModel):
    # Optional: the approver IS the signed-in user, and the service refuses any
    # other value. Kept in the body for the existing callers rather than removed.
    approvedByUserId: str | None = None


@router.get("/cycles/{cycle_id}/approval-report")
async def get_approval_report(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Everything blocking approval, per scope unit, WITHOUT attempting it.

    The approver has to fix these; discovering them one pipe-delimited sentence
    at a time after a failed POST is how a governance gate turns into a
    guessing game.
    """
    await _require(db, user, "CAMS.READ")
    try:
        return await lc.approval_preview(db, cycle_id=cycle_id, approver_id=user.id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/cycles/{cycle_id}/approve")
async def approve_cycle(
    cycle_id: str,
    body: ApproveBody | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Approve and FREEZE. Everything after this is an amendment, never an edit."""
    await _require(db, user, "CAMS.CLOSE")
    try:
        out = await lc.approve_cycle(
            db,
            cycle_id=cycle_id,
            approver_id=(body.approvedByUserId if body and body.approvedByUserId else user.id),
            user=user,
        )
    except ValueError as e:
        raise _bad(e)
    await db.commit()
    return out


@router.post("/cycles/{cycle_id}/activate")
async def activate_cycle(
    cycle_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """APPROVED → ACTIVE. Without it an approved cycle can never close."""
    await _require(db, user, "CAMS.CLOSE")
    try:
        out = await lc.activate_cycle(db, cycle_id=cycle_id, user=user)
    except ValueError as e:
        raise _bad(e)
    await db.commit()
    return out


@router.post("/cycles/{cycle_id}/close")
async def close_cycle(
    cycle_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    await _require(db, user, "CAMS.CLOSE")
    try:
        out = await lc.close_cycle(db, cycle_id=cycle_id, user=user)
    except ValueError as e:
        raise _bad(e)
    await db.commit()
    return out


# ── Scope units ──────────────────────────────────────────────────────


class ScopeUnitBody(BaseModel):
    cycleId: str
    dimension: Literal["DISCIPLINE", "STANDARD", "PROCESS", "SUPPLIER", "CLAUSE"] = "DISCIPLINE"
    siteId: str | None = None
    dimensionKey: str
    dimensionLabel: str = ""
    requiredPerCycle: int | None = None
    riskWeight: int = 3
    rationale: str = ""


@router.post("/scope-units", status_code=status.HTTP_201_CREATED)
async def create_scope_unit(
    body: ScopeUnitBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.SCHEDULE", plant_id=body.siteId)
    if body.dimension == "CLAUSE":
        # Defined in the model from day one so it is an additive upgrade, but
        # rejected until WP-20's ClauseRef catalogue exists — `standard` and
        # `requirementReference` are free text today, which is enough for a
        # string-grouped rollup and NOT enough to assert clause coverage.
        raise HTTPException(
            400,
            "Clause-level scope requires the clause catalogue (WP-20). Use DISCIPLINE or "
            "STANDARD until it lands.",
        )
    row = ProgrammeScopeUnit(**body.model_dump())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"id": row.id, "ok": True}


class ScopeUnitBulkBody(BaseModel):
    """Site × dimension, expanded server-side.

    A 16-factory group defining 10 disciplines is 160 rows; asking for them one
    POST at a time is why the screen was never built. `siteIds=[None]` (or an
    empty list) expresses an estate-wide unit.
    """

    cycleId: str
    dimension: Literal["DISCIPLINE", "STANDARD", "PROCESS", "SUPPLIER"] = "DISCIPLINE"
    siteIds: list[str | None] = Field(default_factory=list)
    dimensions: list[dict[str, Any]] = Field(default_factory=list)  # {key,label,requiredPerCycle?,riskWeight?}
    requiredPerCycle: int | None = None
    riskWeight: int = 3
    rationale: str = ""


@router.post("/scope-units/bulk", status_code=status.HTTP_201_CREATED)
async def bulk_create_scope_units(
    body: ScopeUnitBulkBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.SCHEDULE")
    cycle = await db.get(ProgrammeCycle, body.cycleId)
    if cycle is None:
        raise HTTPException(404, "Cycle not found")
    if cycle.status != "DRAFT":
        raise HTTPException(
            400,
            f"This cycle is {cycle.status.replace('_', ' ').lower()}. Scope can only be added "
            "while it is a draft — an approved cycle changes by amendment.",
        )
    if not body.dimensions:
        raise HTTPException(400, "Select at least one discipline or standard.")

    sites: list[str | None] = list(body.siteIds) or [None]
    existing = {
        (u.dimension, u.siteId, u.dimensionKey)
        for u in (
            await db.execute(
                select(ProgrammeScopeUnit).where(ProgrammeScopeUnit.cycleId == body.cycleId)
            )
        ).scalars().all()
    }

    created = 0
    skipped = 0
    for site_id in sites:
        for d in body.dimensions:
            key = str(d.get("key") or "").strip()
            if not key:
                continue
            # The unique constraint would reject these anyway; catching them here
            # means re-running the wizard tops the cycle up instead of 500ing.
            if (body.dimension, site_id, key) in existing:
                skipped += 1
                continue
            existing.add((body.dimension, site_id, key))
            db.add(
                ProgrammeScopeUnit(
                    cycleId=body.cycleId,
                    dimension=body.dimension,
                    siteId=site_id,
                    dimensionKey=key,
                    dimensionLabel=str(d.get("label") or key),
                    requiredPerCycle=d.get("requiredPerCycle", body.requiredPerCycle),
                    riskWeight=int(d.get("riskWeight") or body.riskWeight),
                    rationale=str(d.get("rationale") or body.rationale),
                )
            )
            created += 1
    await db.commit()
    return {"ok": True, "created": created, "skippedExisting": skipped}


class ScopeUnitPatchBody(BaseModel):
    requiredPerCycle: int | None = None
    riskWeight: int | None = None
    rationale: str | None = None
    # A waiver is the only legitimate alternative to a frequency at approval, so
    # it needs a write path — without one, `approval_blockers` could name a
    # requirement the UI had no way to satisfy.
    waiverReason: str | None = None
    clearWaiver: bool = False


@router.patch("/scope-units/{unit_id}")
async def update_scope_unit(
    unit_id: str,
    body: ScopeUnitPatchBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.SCHEDULE")
    unit = await db.get(ProgrammeScopeUnit, unit_id)
    if unit is None:
        raise HTTPException(404, "Scope unit not found")
    cycle = await db.get(ProgrammeCycle, unit.cycleId)
    if cycle is None:
        raise HTTPException(404, "Cycle not found")
    if cycle.status not in ("DRAFT", "UNDER_REVIEW"):
        raise HTTPException(
            400,
            f"This cycle is {cycle.status.replace('_', ' ').lower()} and frozen. Change it "
            "through an amendment, not an edit.",
        )

    if body.requiredPerCycle is not None:
        if body.requiredPerCycle < 0:
            raise HTTPException(400, "A required frequency cannot be negative.")
        unit.requiredPerCycle = body.requiredPerCycle or None
    if body.riskWeight is not None:
        unit.riskWeight = max(1, min(5, body.riskWeight))
    if body.rationale is not None:
        unit.rationale = body.rationale
    if body.clearWaiver:
        unit.waiverReason = None
        unit.waivedByUserId = None
        unit.waivedAt = None
    elif body.waiverReason is not None:
        reason = body.waiverReason.strip()
        if len(reason) < 10:
            raise HTTPException(
                400,
                "A waiver needs a reason of at least 10 characters — it is the record that "
                "explains why this scope was not audited.",
            )
        unit.waiverReason = reason
        unit.waivedByUserId = user.id
        unit.waivedAt = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True, "id": unit.id}


@router.delete("/scope-units/{unit_id}")
async def delete_scope_unit(
    unit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Draft only. Once a cycle is approved the scope is the frozen plan."""
    await _require(db, user, "CAMS.SCHEDULE")
    unit = await db.get(ProgrammeScopeUnit, unit_id)
    if unit is None:
        raise HTTPException(404, "Scope unit not found")
    cycle = await db.get(ProgrammeCycle, unit.cycleId)
    if cycle is not None and cycle.status != "DRAFT":
        raise HTTPException(
            400,
            f"This cycle is {cycle.status.replace('_', ' ').lower()}. A scope unit can only be "
            "removed while the cycle is a draft.",
        )
    links = list(
        (
            await db.execute(select(SlotScopeUnit).where(SlotScopeUnit.scopeUnitId == unit_id))
        ).scalars().all()
    )
    for ln in links:
        await db.delete(ln)
    await db.delete(unit)
    await db.commit()
    return {"ok": True, "unlinkedSlots": len(links)}


# ── Slots ────────────────────────────────────────────────────────────


class SlotBody(BaseModel):
    cycleId: str
    slotCode: str
    windowStart: date
    windowEnd: date
    periodIndex: int = 0
    origin: Literal["INTERNAL", "EXTERNAL", "UNPLANNED"] = "INTERNAL"
    externalBody: str | None = None
    intendedLeadUserId: str | None = None
    ownerUserId: str | None = None
    estimatedAuditorDays: float = 1.0
    samplingApproach: Literal["FULL", "RANDOM_N_OF_M", "RISK_WEIGHTED", "JUDGEMENTAL"] = "FULL"
    samplingJustification: str | None = None
    scopeUnitIds: list[str] = Field(default_factory=list)


@router.post("/slots", status_code=status.HTTP_201_CREATED)
async def create_slot(
    body: SlotBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.SCHEDULE")
    if body.windowEnd < body.windowStart:
        raise HTTPException(400, "windowEnd must not precede windowStart")
    # docs/cams/09 §2.4 — the auditable artefact is the justification, so a
    # non-FULL approach without one is refused rather than silently accepted.
    if body.samplingApproach != "FULL" and not (body.samplingJustification or "").strip():
        raise HTTPException(
            400, "A sampling approach other than FULL requires a written justification."
        )
    data = body.model_dump()
    unit_ids = data.pop("scopeUnitIds")
    slot = ProgrammeSlot(**data, createdBy=user.id)
    db.add(slot)
    await db.flush()
    for uid in unit_ids:
        db.add(SlotScopeUnit(slotId=slot.id, scopeUnitId=uid))
    await db.commit()
    return {"id": slot.id, "ok": True, "status": slot.status}


@router.get("/slots/{slot_id}")
async def get_slot(
    slot_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """One slot, with its scope units resolved and its legal next moves.

    Backs the `/cams/programme/[id]/slots/[slotId]` route from §6.1 — a slot
    carries a window, a scope, a load estimate, a sampling basis and an
    amendment history, which is more than a dialog can hold honestly.
    """
    await _require(db, user, "CAMS.READ")
    slot = await db.get(ProgrammeSlot, slot_id)
    if slot is None:
        raise HTTPException(404, "Slot not found")
    cycle = await db.get(ProgrammeCycle, slot.cycleId)
    programme = await db.get(AuditProgramme, cycle.programmeId) if cycle else None
    try:
        plan = await mat.slot_plan(db, slot_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    amendments = list(
        (
            await db.execute(
                select(ProgrammeAmendment)
                .where(ProgrammeAmendment.slotId == slot_id)
                .order_by(ProgrammeAmendment.approvedAt.desc())
            )
        ).scalars().all()
    )
    return {
        "slot": {
            "id": slot.id,
            "slotCode": slot.slotCode,
            "cycleId": slot.cycleId,
            "windowStart": slot.windowStart.isoformat(),
            "windowEnd": slot.windowEnd.isoformat(),
            "periodIndex": slot.periodIndex,
            "origin": slot.origin,
            "externalBody": slot.externalBody,
            "engagementKind": slot.engagementKind,
            "engagementId": slot.engagementId,
            "intendedLeadUserId": slot.intendedLeadUserId,
            "ownerUserId": slot.ownerUserId,
            "estimatedAuditorDays": slot.estimatedAuditorDays,
            "actualAuditorDays": slot.actualAuditorDays,
            "samplingApproach": slot.samplingApproach,
            "samplingJustification": slot.samplingJustification,
            "status": slot.status,
            "amendmentCount": slot.amendmentCount,
            "notes": slot.notes,
            "scopeUnitIds": [u["id"] for u in plan["scopeUnits"]],
            "allowedTransitions": list(lc.SLOT_TRANSITIONS.get(slot.status, ())),
        },
        "plan": plan,
        "programme": (
            {"id": programme.id, "name": programme.name, "programmeCode": programme.programmeCode}
            if programme
            else None
        ),
        "cycle": (
            {
                "id": cycle.id,
                "cycleLabel": cycle.cycleLabel,
                "status": cycle.status,
                "periodStart": cycle.periodStart.isoformat(),
                "periodEnd": cycle.periodEnd.isoformat(),
                "periodsPerCycle": cycle.periodsPerCycle,
            }
            if cycle
            else None
        ),
        "amendments": [
            {
                "id": a.id,
                "amendmentType": a.amendmentType,
                "reason": a.reason,
                "beforeValue": a.beforeValue,
                "afterValue": a.afterValue,
                "approvedByUserId": a.approvedByUserId,
                "approvedAt": a.approvedAt.isoformat() if a.approvedAt else None,
            }
            for a in amendments
        ],
    }


class MaterialiseBody(BaseModel):
    """What the plan cannot decide for you.

    Everything else — disciplines, standards, window, estimate, sampling basis —
    comes off the slot server-side. There is deliberately no `engagementId`
    field: pasting one was the old flow, and it could not tell a valid link from
    a typo.
    """

    engagementKind: Literal["AUDIT", "INSPECTION"] = "AUDIT"
    leadAuditorUserId: str | None = None
    siteId: str | None = None
    title: str | None = None
    scheduledOn: date | None = None
    engagementType: Literal[
        "INTERNAL_AUDIT", "COMPLIANCE_AUDIT", "INSPECTION",
        "SUPPLIER_AUDIT", "LAYERED_PROCESS_AUDIT", "MANAGEMENT_REVIEW",
    ] = "INSPECTION"
    templateId: str | None = None
    plantManagerUserId: str | None = None
    auditeeUserIds: list[str] = Field(default_factory=list)
    coAuditorUserIds: list[str] = Field(default_factory=list)


@router.post("/slots/{slot_id}/materialise")
async def materialise_slot(
    slot_id: str,
    body: MaterialiseBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create the engagement this slot planned and link it back — one call.

    Replaces the manual engagement-id paste. The engagement and the slot
    transition commit together, so neither can exist without the other.
    """
    await _require(db, user, "CAMS.SCHEDULE", plant_id=body.siteId)
    try:
        out = await mat.materialise_slot(
            db,
            slot_id=slot_id,
            user=user,
            engagement_kind=body.engagementKind,
            lead_auditor_id=body.leadAuditorUserId,
            site_id=body.siteId,
            title=body.title,
            scheduled_on=body.scheduledOn,
            engagement_type=body.engagementType,
            template_id=body.templateId,
            plant_manager_user_id=body.plantManagerUserId,
            auditee_user_ids=body.auditeeUserIds,
            co_auditor_ids=body.coAuditorUserIds,
        )
    except ValueError as e:
        await db.rollback()
        raise _bad(e)
    await db.commit()
    return out


class SlotTransitionBody(BaseModel):
    target: Literal[
        "SCHEDULED", "IN_PROGRESS", "COMPLETED", "DEFERRED", "CANCELLED", "WAIVED", "PLANNED"
    ]
    reason: str | None = None
    approvedByUserId: str | None = None
    newWindowStart: date | None = None
    newWindowEnd: date | None = None
    engagementKind: Literal["AUDIT", "INSPECTION"] | None = None
    engagementId: str | None = None


@router.post("/slots/{slot_id}/transition")
async def transition_slot(
    slot_id: str,
    body: SlotTransitionBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The only writer of slot status. Non-materialising moves write an amendment
    in the same transaction — a slot cannot silently stop existing."""
    await _require(db, user, "CAMS.CLOSE")
    window = None
    if body.newWindowStart and body.newWindowEnd:
        window = (body.newWindowStart, body.newWindowEnd)
    try:
        out = await lc.transition_slot(
            db,
            slot_id=slot_id,
            target=body.target,
            user=user,
            reason=body.reason,
            approver_id=body.approvedByUserId,
            new_window=window,
            engagement_kind=body.engagementKind,
            engagement_id=body.engagementId,
        )
    except ValueError as e:
        raise _bad(e)
    await db.commit()
    return out


# ── Coverage, variance, integrity ────────────────────────────────────


@router.get("/cycles/{cycle_id}/coverage")
async def get_coverage(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """THE coverage accessor. Every surface reads this — there is no second
    read path and no stored coverage flag (the F-29 lesson)."""
    await _require(db, user, "CAMS.READ")
    try:
        return (await cov.coverage_for_cycle(db, cycle_id)).as_dict()
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/cycles/{cycle_id}/variance")
async def get_variance(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.READ")
    items = await cov.variance_for_cycle(db, cycle_id)
    return {
        "items": items,
        "total": len(items),
        "lateCount": sum(1 for i in items if i["isLate"]),
        "scopeVarianceCount": sum(1 for i in items if i["hasScopeVariance"]),
        "notExecutedCount": sum(1 for i in items if i["notExecuted"]),
    }


@router.get("/cycles/{cycle_id}/amendments")
async def get_amendments(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.READ")
    rows = list(
        (
            await db.execute(
                select(ProgrammeAmendment)
                .where(ProgrammeAmendment.cycleId == cycle_id)
                .order_by(ProgrammeAmendment.approvedAt.desc())
            )
        ).scalars().all()
    )
    return {
        "items": [
            {
                "id": a.id,
                "slotId": a.slotId,
                "amendmentType": a.amendmentType,
                "reason": a.reason,
                "beforeValue": a.beforeValue,
                "afterValue": a.afterValue,
                "approvedByUserId": a.approvedByUserId,
                "approvedAt": a.approvedAt.isoformat() if a.approvedAt else None,
            }
            for a in rows
        ],
        "total": len(rows),
    }


@router.get("/cycles/{cycle_id}/integrity")
async def get_integrity(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Band-0 integrity strip. Every list here must be empty.

    The CAMS defects survived a month because nothing surfaced them.
    """
    await _require(db, user, "CAMS.READ")
    return await lc.integrity_check(db, cycle_id)


# ── Read endpoints for the management surfaces ───────────────────────


@router.get("/cycles/{cycle_id}/scope-units")
async def list_scope_units(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.READ")
    rows = list(
        (
            await db.execute(
                select(ProgrammeScopeUnit)
                .where(ProgrammeScopeUnit.cycleId == cycle_id)
                .order_by(ProgrammeScopeUnit.siteId, ProgrammeScopeUnit.dimensionKey)
            )
        ).scalars().all()
    )
    # `siteId` is a Plant cuid. Every scope-unit consumer (the scope-unit table,
    # the slot builder, the recommendation panel) renders the site, so resolve
    # the name here once rather than leaving each screen to print the id.
    plant_names = await resolve_plant_names(db, [u.siteId for u in rows])
    return {
        "items": [
            {
                "id": u.id,
                "dimension": u.dimension,
                "siteId": u.siteId,
                "siteName": site_label(plant_names, u.siteId),
                "dimensionKey": u.dimensionKey,
                "dimensionLabel": u.dimensionLabel or u.dimensionKey,
                "requiredPerCycle": u.requiredPerCycle,
                "riskWeight": u.riskWeight,
                "rationale": u.rationale,
                "isWaived": bool(u.waiverReason),
                "waiverReason": u.waiverReason,
            }
            for u in rows
        ],
        "total": len(rows),
    }


@router.get("/cycles/{cycle_id}/slots")
async def list_slots(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Slots with their scope-unit links and their legal next transitions.

    Returning `allowedTransitions` per row keeps the state machine in ONE place
    (`lifecycle.SLOT_TRANSITIONS`) — the UI renders whatever the server says is
    legal rather than re-implementing the table and drifting from it.
    """
    await _require(db, user, "CAMS.READ")
    slots = list(
        (
            await db.execute(
                select(ProgrammeSlot)
                .where(ProgrammeSlot.cycleId == cycle_id)
                .order_by(ProgrammeSlot.windowStart, ProgrammeSlot.slotCode)
            )
        ).scalars().all()
    )
    links: list[SlotScopeUnit] = []
    if slots:
        links = list(
            (
                await db.execute(
                    select(SlotScopeUnit).where(
                        SlotScopeUnit.slotId.in_([s.id for s in slots])
                    )
                )
            ).scalars().all()
        )
    by_slot: dict[str, list[str]] = {}
    for ln in links:
        by_slot.setdefault(ln.slotId, []).append(ln.scopeUnitId)

    return {
        "items": [
            {
                "id": s.id,
                "slotCode": s.slotCode,
                "windowStart": s.windowStart.isoformat(),
                "windowEnd": s.windowEnd.isoformat(),
                "periodIndex": s.periodIndex,
                "origin": s.origin,
                "externalBody": s.externalBody,
                "engagementKind": s.engagementKind,
                "engagementId": s.engagementId,
                "intendedLeadUserId": s.intendedLeadUserId,
                "estimatedAuditorDays": s.estimatedAuditorDays,
                "samplingApproach": s.samplingApproach,
                "samplingJustification": s.samplingJustification,
                "status": s.status,
                "amendmentCount": s.amendmentCount,
                "scopeUnitIds": by_slot.get(s.id, []),
                "allowedTransitions": list(lc.SLOT_TRANSITIONS.get(s.status, ())),
                "notes": s.notes,
            }
            for s in slots
        ],
        "total": len(slots),
    }


# ── Risk-based frequency recommendation (docs/cams/08 §5) ────────────


@router.get("/cycles/{cycle_id}/recommendations")
async def get_recommendations(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Read persisted recommendations WITHOUT recomputing.

    Separate from the POST because recomputation touches every scope unit and
    should be an explicit act, not a side effect of opening a screen.
    """
    await _require(db, user, "CAMS.READ")
    rows = list(
        (
            await db.execute(
                select(ProgrammeRecommendation)
                .where(ProgrammeRecommendation.cycleId == cycle_id)
                .order_by(ProgrammeRecommendation.score.desc())
            )
        ).scalars().all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "scopeUnitId": r.scopeUnitId,
                "currentFrequency": r.currentFrequency,
                "recommendedFrequency": r.recommendedFrequency,
                "score": r.score,
                "band": r.band,
                "inputs": r.inputs,
                "unavailableInputs": r.unavailableInputs,
                "narrative": r.narrative,
                "computedAt": r.computedAt.isoformat() if r.computedAt else None,
                "acceptedAt": r.acceptedAt.isoformat() if r.acceptedAt else None,
                "acceptedFrequency": r.acceptedFrequency,
                "rejectedAt": r.rejectedAt.isoformat() if r.rejectedAt else None,
                "rejectionReason": r.rejectionReason,
                # Open = neither accepted nor rejected. The UI only offers
                # accept/reject on these.
                "isOpen": r.acceptedAt is None and r.rejectedAt is None,
            }
            for r in rows
        ],
        "total": len(rows),
        "weights": rec.WEIGHTS,
    }


@router.post("/cycles/{cycle_id}/recommendations")
async def compute_recommendations(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Recompute the frequency recommendation for every scope unit.

    Deterministic arithmetic over the client's own cross-module history — no
    LLM, no hosted call. Each result carries its INPUTS so the UI can render
    the arithmetic and a reviewer can disagree with a number rather than a
    verdict. Nothing here mutates a frequency.
    """
    await _require(db, user, "CAMS.ANALYTICS")
    try:
        items = await rec.recommend_for_cycle(db, cycle_id, persist=True)
    except ValueError as e:
        raise HTTPException(404, str(e))
    await db.commit()
    return {
        "items": items,
        "total": len(items),
        "weights": rec.WEIGHTS,
        "increaseCount": sum(1 for i in items if i["band"] == "INCREASE"),
        "reduceCount": sum(1 for i in items if i["band"] == "REDUCE"),
    }


class AcceptRecommendationBody(BaseModel):
    frequency: int | None = None


@router.post("/recommendations/{recommendation_id}/accept")
async def accept_recommendation(
    recommendation_id: str,
    body: AcceptRecommendationBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The human gate — the ONLY path that writes a frequency.

    `frequency` allows accepting the direction while disagreeing with the
    magnitude, which is a normal outcome; forcing a binary choice would push
    reviewers to reject good recommendations.
    """
    await _require(db, user, "CAMS.SCHEDULE")
    try:
        out = await rec.accept_recommendation(
            db, recommendation_id=recommendation_id, user_id=user.id, frequency=body.frequency
        )
    except ValueError as e:
        raise _bad(e)
    await db.commit()
    return out


class RejectRecommendationBody(BaseModel):
    reason: str = Field(min_length=5)


@router.post("/recommendations/{recommendation_id}/reject")
async def reject_recommendation(
    recommendation_id: str,
    body: RejectRecommendationBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.SCHEDULE")
    try:
        out = await rec.reject_recommendation(
            db, recommendation_id=recommendation_id, user_id=user.id, reason=body.reason
        )
    except ValueError as e:
        raise _bad(e)
    await db.commit()
    return out


# ── Programme review (ISO 19011 §5.6) ────────────────────────────────


class ReviewBody(BaseModel):
    cycleId: str
    reviewDate: date
    participantUserIds: list[str] = Field(default_factory=list)
    externalParticipants: list[dict[str, Any]] = Field(default_factory=list)
    programmeFindings: str = ""
    decisions: str = ""
    effectivenessAssessment: str | None = None
    resultingAmendmentIds: list[str] = Field(default_factory=list)


@router.post("/reviews", status_code=status.HTTP_201_CREATED)
async def create_review(
    body: ReviewBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The ISO 19011 §5.6 review record. It is the hard gate on closing a cycle.

    Validated rather than accepted blank: `close_cycle` refuses without at least
    one review, so an empty review would let anyone unlock closure by pressing
    save on a form with nothing in it — the gate would look like governance and
    enforce nothing.
    """
    await _require(db, user, "CAMS.SCHEDULE")
    cycle = await db.get(ProgrammeCycle, body.cycleId)
    if cycle is None:
        raise HTTPException(404, "Cycle not found")
    substance = f"{body.programmeFindings.strip()} {body.decisions.strip()}".strip()
    if len(substance) < 10:
        raise HTTPException(
            400,
            "A programme review needs findings about the programme itself, or the decisions "
            "taken — it is the record that unlocks closing the cycle.",
        )
    if not body.participantUserIds and not body.externalParticipants:
        raise HTTPException(400, "Record who attended the review.")
    if body.reviewDate < cycle.periodStart:
        raise HTTPException(
            400,
            f"The review date precedes the cycle ({cycle.periodStart.isoformat()}).",
        )
    row = ProgrammeReview(**body.model_dump(), reviewedByUserId=user.id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"id": row.id, "ok": True}


@router.get("/cycles/{cycle_id}/reviews")
async def list_reviews(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.READ")
    rows = list(
        (
            await db.execute(
                select(ProgrammeReview)
                .where(ProgrammeReview.cycleId == cycle_id)
                .order_by(ProgrammeReview.reviewDate.desc())
            )
        ).scalars().all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "reviewDate": r.reviewDate.isoformat(),
                "participantUserIds": r.participantUserIds,
                "externalParticipants": r.externalParticipants,
                "programmeFindings": r.programmeFindings,
                "decisions": r.decisions,
                "effectivenessAssessment": r.effectivenessAssessment,
                "resultingAmendmentIds": r.resultingAmendmentIds,
                "reviewedByUserId": r.reviewedByUserId,
                "createdAt": r.createdAt.isoformat() if r.createdAt else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }
