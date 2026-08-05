"""Programme cycle + slot state machines and their guards.

docs/cams/08 §3.

**The constraint that IS the audit trail:**

    No slot leaves PLANNED without either a non-null engagementId
    or a ProgrammeAmendment row referencing it.

Enforced in three deliberately redundant places, because this is the
certification-critical invariant and the module's own history shows
service-layer-only guards being bypassed by scripts:

  1. here — `transition_slot` is the ONLY writer of `ProgrammeSlot.status`
  2. the DB — a CHECK constraint on (status, engagementId, amendmentCount)
  3. the Band-0 integrity strip — "slots non-PLANNED with neither" must read 0

The pure transition tables at the top are unit-testable with no DB.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.programme import (
    AuditProgramme,
    ProgrammeAmendment,
    ProgrammeCycle,
    ProgrammeReview,
    ProgrammeScopeUnit,
    ProgrammeSlot,
    SlotScopeUnit,
)
from app.models.user import User
from app.services.assurance import canonical_hash
from app.services.independence import segregation_ok
from app.services.plant_directory import resolve_plant_names, site_label

# ── Pure transition tables ───────────────────────────────────────────

CYCLE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "DRAFT": ("UNDER_REVIEW",),
    "UNDER_REVIEW": ("DRAFT", "APPROVED"),
    "APPROVED": ("ACTIVE",),
    "ACTIVE": ("CLOSED",),
    "CLOSED": (),
}

# Transitions that represent a materialised engagement rather than an amendment.
SLOT_MATERIALISING = ("SCHEDULED", "IN_PROGRESS", "COMPLETED")

SLOT_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "PLANNED": ("SCHEDULED", "DEFERRED", "CANCELLED", "WAIVED"),
    "SCHEDULED": ("IN_PROGRESS", "COMPLETED", "DEFERRED", "CANCELLED"),
    "IN_PROGRESS": ("COMPLETED", "CANCELLED"),
    "COMPLETED": (),
    "DEFERRED": ("PLANNED", "SCHEDULED", "CANCELLED", "WAIVED"),
    "CANCELLED": (),
    "WAIVED": (),
}

# A transition to one of these requires reason + approver, and writes an
# amendment. This is the list that stops a slot silently vanishing.
SLOT_REQUIRES_AMENDMENT = ("DEFERRED", "CANCELLED", "WAIVED")

_AMENDMENT_FOR_STATE = {
    "DEFERRED": "DEFER",
    "CANCELLED": "CANCEL",
    "WAIVED": "WAIVE",
}


def cycle_transition_allowed(current: str, target: str) -> bool:
    return target in CYCLE_TRANSITIONS.get(current, ())


def slot_transition_allowed(current: str, target: str) -> bool:
    return target in SLOT_TRANSITIONS.get(current, ())


def slot_needs_amendment(target: str) -> bool:
    return target in SLOT_REQUIRES_AMENDMENT


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Cycle approval guard ─────────────────────────────────────────────


def approval_report(
    *,
    objectives: str,
    scope_units: list[Any],
    slots_per_unit: dict[str, int],
    approver_id: str | None,
    owner_id: str | None,
    submitter_id: str | None = None,
) -> list[dict[str, Any]]:
    """Pure: everything that stops a cycle being approved, STRUCTURED.

    ISO 19011 §5.2 makes objectives mandatory, and 9.2.2 makes frequency
    mandatory — so an approval that skipped either would produce a programme
    document that fails its own clause. A documented waiver is the only
    alternative to a frequency.

    Structured rather than a bare string list because the approver has to *fix*
    these, and "cannot approve — 14 problems" concatenated into one sentence is
    a wall the user re-reads on every attempt. Each row carries the scope unit
    it belongs to so the UI can put the message on the offending row. The
    message text is unchanged, and `approval_blockers` still returns exactly the
    strings it always did.
    """
    out: list[dict[str, Any]] = []

    def add(code: str, message: str, *, unit: Any = None) -> None:
        out.append(
            {
                "code": code,
                "message": message,
                "scopeUnitId": getattr(unit, "id", None) if unit is not None else None,
                "scopeUnitLabel": (
                    (getattr(unit, "dimensionLabel", None) or getattr(unit, "dimensionKey", None))
                    if unit is not None
                    else None
                ),
                "siteId": getattr(unit, "siteId", None) if unit is not None else None,
            }
        )

    if not (objectives or "").strip():
        add("OBJECTIVES_MISSING", "Programme objectives are required (ISO 19011 §5.2).")
    if not scope_units:
        add("NO_SCOPE_UNITS", "A cycle needs at least one scope unit.")
    if not approver_id:
        add("APPROVER_MISSING", "An approver is required.")
    else:
        if not segregation_ok(approver_id, owner_id):
            add(
                "APPROVER_IS_OWNER",
                "The programme owner cannot approve their own cycle — assign an independent "
                "approver.",
            )
        # Four-eyes on the pair with the real incentive problem. The owner guard
        # above does not catch it: a delegate can prepare and submit a cycle they
        # do not own, then approve their own submission.
        if submitter_id and not segregation_ok(approver_id, submitter_id):
            add(
                "APPROVER_IS_SUBMITTER",
                "The person who submitted this cycle for review cannot also approve it — "
                "approval needs a second pair of eyes.",
            )

    for u in scope_units:
        label = getattr(u, "dimensionLabel", None) or getattr(u, "dimensionKey", "scope unit")
        waived = bool(getattr(u, "waiverReason", None))
        freq = getattr(u, "requiredPerCycle", None)
        if waived:
            if not getattr(u, "waivedByUserId", None):
                add("WAIVER_UNAPPROVED", f"{label}: a waiver needs a named approver.", unit=u)
            continue
        if not freq:
            add(
                "FREQUENCY_MISSING",
                f"{label}: needs a required frequency, or a documented waiver.",
                unit=u,
            )
        elif slots_per_unit.get(getattr(u, "id", ""), 0) < 1:
            add("NO_SLOT", f"{label}: has a frequency but no planned slot.", unit=u)
    return out


def approval_blockers(
    *,
    objectives: str,
    scope_units: list[Any],
    slots_per_unit: dict[str, int],
    approver_id: str | None,
    owner_id: str | None,
    submitter_id: str | None = None,
) -> list[str]:
    """The same guard, flattened to messages. One implementation, two shapes."""
    return [
        b["message"]
        for b in approval_report(
            objectives=objectives,
            scope_units=scope_units,
            slots_per_unit=slots_per_unit,
            approver_id=approver_id,
            owner_id=owner_id,
            submitter_id=submitter_id,
        )
    ]


async def submit_cycle_for_review(
    db: AsyncSession, *, cycle_id: str, user: User
) -> dict[str, Any]:
    cycle = await db.get(ProgrammeCycle, cycle_id)
    if cycle is None:
        raise ValueError("Cycle not found")
    if not cycle_transition_allowed(cycle.status, "UNDER_REVIEW"):
        raise ValueError(f"A {cycle.status} cycle cannot be submitted for review")

    units = list(
        (
            await db.execute(
                select(ProgrammeScopeUnit).where(ProgrammeScopeUnit.cycleId == cycle_id)
            )
        ).scalars().all()
    )
    if not units:
        raise ValueError("A cycle needs at least one scope unit before review")
    for u in units:
        if not u.requiredPerCycle and not u.waiverReason:
            raise ValueError(
                f"{u.dimensionLabel or u.dimensionKey}: needs a required frequency, or a "
                "documented waiver, before review"
            )

    cycle.status = "UNDER_REVIEW"
    cycle.submittedForReviewAt = _utcnow()
    cycle.submittedByUserId = user.id
    await db.flush()
    return {"ok": True, "status": cycle.status, "submittedByUserId": cycle.submittedByUserId}


async def _cycle_plan(
    db: AsyncSession, cycle_id: str
) -> tuple[
    ProgrammeCycle | None,
    AuditProgramme | None,
    list[ProgrammeScopeUnit],
    list[ProgrammeSlot],
    dict[str, int],
]:
    """Everything the approval guard reads, loaded once.

    Shared by `approve_cycle` and the read-only preview so the screen that shows
    the blockers and the call that enforces them cannot disagree — a preview
    computed from a second query is a preview that lies the day the two drift.
    """
    cycle = await db.get(ProgrammeCycle, cycle_id)
    if cycle is None:
        return None, None, [], [], {}
    programme = await db.get(AuditProgramme, cycle.programmeId)
    units = list(
        (
            await db.execute(
                select(ProgrammeScopeUnit).where(ProgrammeScopeUnit.cycleId == cycle_id)
            )
        ).scalars().all()
    )
    slots = list(
        (
            await db.execute(select(ProgrammeSlot).where(ProgrammeSlot.cycleId == cycle_id))
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
    per_unit: dict[str, int] = {}
    for ln in links:
        per_unit[ln.scopeUnitId] = per_unit.get(ln.scopeUnitId, 0) + 1
    return cycle, programme, units, slots, per_unit


async def approval_preview(
    db: AsyncSession, *, cycle_id: str, approver_id: str | None
) -> dict[str, Any]:
    """What would block approval right now. Reads nothing else, writes nothing.

    The point is that the approver sees every failing scope unit BEFORE
    clicking, on the row it belongs to, rather than discovering a
    pipe-delimited sentence after a failed POST.
    """
    cycle, programme, units, slots, per_unit = await _cycle_plan(db, cycle_id)
    if cycle is None:
        raise ValueError("Cycle not found")

    blockers = approval_report(
        objectives=(programme.objectives if programme else ""),
        scope_units=units,
        slots_per_unit=per_unit,
        approver_id=approver_id,
        owner_id=(programme.ownerUserId if programme else None),
        submitter_id=cycle.submittedByUserId,
    )
    overlap = await _overlapping_approved_cycle(db, cycle, units)
    if overlap:
        blockers.append(
            {
                "code": "OVERLAPPING_CYCLE",
                "message": overlap,
                "scopeUnitId": None,
                "scopeUnitLabel": None,
                "siteId": None,
            }
        )

    # `approval_report` is deliberately pure (no DB), so the site name is
    # attached here — the blocker list is read on the governance panel, where a
    # cuid told the approver nothing about which site was blocking them.
    plant_names = await resolve_plant_names(db, [b.get("siteId") for b in blockers])
    for b in blockers:
        b["siteName"] = site_label(plant_names, b.get("siteId")) if b.get("siteId") else None

    return {
        "cycleId": cycle_id,
        "status": cycle.status,
        "canApprove": cycle_transition_allowed(cycle.status, "APPROVED") and not blockers,
        "transitionAllowed": cycle_transition_allowed(cycle.status, "APPROVED"),
        "ownerUserId": programme.ownerUserId if programme else None,
        "submittedByUserId": cycle.submittedByUserId,
        "blockers": blockers,
        "scopeUnitCount": len(units),
        "slotCount": len(slots),
    }


async def return_cycle_to_draft(
    db: AsyncSession, *, cycle_id: str, user: User
) -> dict[str, Any]:
    """UNDER_REVIEW → DRAFT. The reviewer's "not yet" answer.

    Legal in `CYCLE_TRANSITIONS` from the start and never exposed, which left
    the only route past a rejected review as approving it anyway. Nothing is
    frozen before approval, so this is an ordinary edit — the submitter stamp is
    cleared with it, because the next submission is a new act by whoever makes
    it.
    """
    cycle = await db.get(ProgrammeCycle, cycle_id)
    if cycle is None:
        raise ValueError("Cycle not found")
    if not cycle_transition_allowed(cycle.status, "DRAFT"):
        raise ValueError(f"A {cycle.status} cycle cannot be returned to draft")
    cycle.status = "DRAFT"
    cycle.submittedForReviewAt = None
    cycle.submittedByUserId = None
    await db.flush()
    return {"ok": True, "status": cycle.status}


async def approve_cycle(
    db: AsyncSession, *, cycle_id: str, approver_id: str, user: User
) -> dict[str, Any]:
    """Approve a cycle and FREEZE it.

    Everything after this point is a logged amendment, never an edit — which is
    what makes "why did this planned audit not happen?" answerable a year later.
    The snapshot carries a full-length SHA-256, same integrity discipline as the
    report snapshot (docs/cams/09 §2.5).
    """
    cycle, programme, units, slots, per_unit = await _cycle_plan(db, cycle_id)
    if cycle is None:
        raise ValueError("Cycle not found")
    if not cycle_transition_allowed(cycle.status, "APPROVED"):
        raise ValueError(f"A {cycle.status} cycle cannot be approved")

    # An approval is a personal act. Naming someone else in the body would let
    # one user manufacture a second pair of eyes out of thin air, which is the
    # whole control this guard exists to provide.
    if approver_id != user.id:
        raise ValueError(
            "You cannot approve a cycle on another user's behalf — the approver must be the "
            "signed-in user."
        )

    blockers = approval_blockers(
        objectives=(programme.objectives if programme else ""),
        scope_units=units,
        slots_per_unit=per_unit,
        approver_id=approver_id,
        owner_id=(programme.ownerUserId if programme else None),
        submitter_id=cycle.submittedByUserId,
    )

    # No two approved cycles may claim the same (site, dimension, period) —
    # otherwise coverage double-counts and the matrix reads better than reality.
    overlap = await _overlapping_approved_cycle(db, cycle, units)
    if overlap:
        blockers.append(overlap)

    if blockers:
        raise ValueError("Cannot approve — " + " | ".join(blockers))

    snapshot = {
        "programmeCode": programme.programmeCode if programme else None,
        "cycleLabel": cycle.cycleLabel,
        "periodStart": cycle.periodStart.isoformat(),
        "periodEnd": cycle.periodEnd.isoformat(),
        "objectives": programme.objectives if programme else "",
        "standardRefs": programme.standardRefs if programme else [],
        "scopeUnits": sorted(
            [
                {
                    "id": u.id,
                    "dimension": u.dimension,
                    "siteId": u.siteId,
                    "dimensionKey": u.dimensionKey,
                    "requiredPerCycle": u.requiredPerCycle,
                    "riskWeight": u.riskWeight,
                    "waiverReason": u.waiverReason,
                }
                for u in units
            ],
            key=lambda d: (d["siteId"] or "", d["dimensionKey"]),
        ),
        "slots": sorted(
            [
                {
                    "id": s.id,
                    "slotCode": s.slotCode,
                    "windowStart": s.windowStart.isoformat(),
                    "windowEnd": s.windowEnd.isoformat(),
                    "origin": s.origin,
                    "estimatedAuditorDays": s.estimatedAuditorDays,
                    "intendedLeadUserId": s.intendedLeadUserId,
                }
                for s in slots
            ],
            key=lambda d: d["slotCode"],
        ),
    }

    cycle.approvedSnapshot = snapshot
    cycle.approvedSnapshotHash = canonical_hash(snapshot, full=True)
    cycle.approvedByUserId = approver_id
    cycle.approvedAt = _utcnow()
    cycle.status = "APPROVED"
    await db.flush()
    return {
        "ok": True,
        "status": cycle.status,
        "approvedSnapshotHash": cycle.approvedSnapshotHash,
        "scopeUnits": len(units),
        "slots": len(slots),
    }


async def _overlapping_approved_cycle(
    db: AsyncSession, cycle: ProgrammeCycle, units: list[ProgrammeScopeUnit]
) -> str | None:
    keys = {(u.siteId, u.dimension, u.dimensionKey) for u in units}
    if not keys:
        return None
    others = list(
        (
            await db.execute(
                select(ProgrammeCycle).where(
                    ProgrammeCycle.id != cycle.id,
                    ProgrammeCycle.status.in_(("APPROVED", "ACTIVE")),
                    ProgrammeCycle.periodStart <= cycle.periodEnd,
                    ProgrammeCycle.periodEnd >= cycle.periodStart,
                )
            )
        ).scalars().all()
    )
    for other in others:
        o_units = list(
            (
                await db.execute(
                    select(ProgrammeScopeUnit).where(ProgrammeScopeUnit.cycleId == other.id)
                )
            ).scalars().all()
        )
        clash = keys & {(u.siteId, u.dimension, u.dimensionKey) for u in o_units}
        if clash:
            # Name the *programme*, not just the cycle label. "FY26 already
            # covers this" sends the user hunting; a rejection that does not say
            # where the conflict lives is a rejection people route around.
            other_programme = await db.get(AuditProgramme, other.programmeId)
            where = (
                f"“{other_programme.name}” ({other_programme.programmeCode}) cycle "
                f"“{other.cycleLabel}”"
                if other_programme
                else f"cycle “{other.cycleLabel}”"
            )
            sample = sorted(clash, key=lambda k: (k[0] or "", k[2]))[0]
            more = f" and {len(clash) - 1} other scope unit(s)" if len(clash) > 1 else ""
            return (
                f"{where} already covers {sample[2]}{more} at this site over an overlapping "
                "period — coverage would be double-counted"
            )
    return None


async def activate_cycle(db: AsyncSession, *, cycle_id: str, user: User) -> dict[str, Any]:
    """APPROVED → ACTIVE. The step that makes the cycle the live plan of record.

    Separate from approval on purpose: a plan can be approved in March for a
    year that starts in April, and until it starts it is not the plan slots are
    executed against. It is also the only route to CLOSED — `CYCLE_TRANSITIONS`
    allows closure from ACTIVE alone.
    """
    cycle = await db.get(ProgrammeCycle, cycle_id)
    if cycle is None:
        raise ValueError("Cycle not found")
    if not cycle_transition_allowed(cycle.status, "ACTIVE"):
        raise ValueError(f"A {cycle.status} cycle cannot be activated")
    cycle.status = "ACTIVE"
    cycle.activatedAt = _utcnow()
    await db.flush()
    return {"ok": True, "status": cycle.status}


async def close_cycle(db: AsyncSession, *, cycle_id: str, user: User) -> dict[str, Any]:
    """Close a cycle. Guarded on the ISO 19011 §5.6 review existing.

    Most tools model "monitor" and stop; "review and improve" is the clause an
    auditor actually asks about, so it is a guard rather than a suggestion.
    """
    cycle = await db.get(ProgrammeCycle, cycle_id)
    if cycle is None:
        raise ValueError("Cycle not found")
    if not cycle_transition_allowed(cycle.status, "CLOSED"):
        raise ValueError(f"A {cycle.status} cycle cannot be closed")

    reviews = (
        await db.execute(
            select(func.count(ProgrammeReview.id)).where(ProgrammeReview.cycleId == cycle_id)
        )
    ).scalar_one()
    if not reviews:
        raise ValueError(
            "A cycle cannot close without at least one programme review (ISO 19011 §5.6)."
        )

    open_slots = list(
        (
            await db.execute(
                select(ProgrammeSlot).where(
                    ProgrammeSlot.cycleId == cycle_id,
                    ProgrammeSlot.status.in_(("PLANNED", "SCHEDULED", "IN_PROGRESS")),
                )
            )
        ).scalars().all()
    )
    if open_slots:
        raise ValueError(
            f"{len(open_slots)} slot(s) are still open — complete, defer, cancel or waive them "
            f"first: {', '.join(s.slotCode for s in open_slots[:5])}"
        )

    cycle.status = "CLOSED"
    cycle.closedAt = _utcnow()
    await db.flush()
    return {"ok": True, "status": cycle.status}


# ── Slot transitions ─────────────────────────────────────────────────


async def transition_slot(
    db: AsyncSession,
    *,
    slot_id: str,
    target: str,
    user: User,
    reason: str | None = None,
    approver_id: str | None = None,
    new_window: tuple[date, date] | None = None,
    engagement_kind: str | None = None,
    engagement_id: str | None = None,
) -> dict[str, Any]:
    """The ONLY writer of `ProgrammeSlot.status`.

    Refuses any non-materialising transition without a reason AND an approver,
    and writes the `ProgrammeAmendment` in the same transaction — so the
    invariant cannot be satisfied by a caller that forgets the second step.
    """
    target = (target or "").upper()
    slot = await db.get(ProgrammeSlot, slot_id)
    if slot is None:
        raise ValueError("Slot not found")
    if not slot_transition_allowed(slot.status, target):
        raise ValueError(f"A {slot.status} slot cannot move to {target}")

    before = {
        "status": slot.status,
        "windowStart": slot.windowStart.isoformat(),
        "windowEnd": slot.windowEnd.isoformat(),
        "engagementId": slot.engagementId,
    }

    if target in SLOT_MATERIALISING:
        if target == "SCHEDULED":
            if not engagement_id or not engagement_kind:
                raise ValueError(
                    "Materialising a slot requires the engagement it produced — a slot cannot "
                    "become SCHEDULED without one."
                )
            slot.engagementKind = engagement_kind.upper()
            slot.engagementId = engagement_id
        elif not slot.engagementId:
            raise ValueError(
                f"Slot has no engagement — it cannot be {target}. Materialise it first."
            )
        slot.status = target
        await db.flush()
        return {"ok": True, "status": slot.status, "amendmentId": None}

    # Non-materialising: DEFERRED | CANCELLED | WAIVED — amendment required.
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise ValueError(
            f"Moving a slot to {target} requires a reason of at least 10 characters — a "
            "certification body will ask why this audit did not happen."
        )
    if not approver_id:
        raise ValueError(f"Moving a slot to {target} requires a named approver.")

    if target == "DEFERRED":
        if new_window is None:
            raise ValueError("A deferral requires a new window — a deferred slot is not a deleted one.")
        slot.windowStart, slot.windowEnd = new_window

    slot.status = target
    slot.amendmentCount = (slot.amendmentCount or 0) + 1

    amendment = ProgrammeAmendment(
        cycleId=slot.cycleId,
        slotId=slot.id,
        amendmentType=_AMENDMENT_FOR_STATE[target],
        reason=reason,
        beforeValue=before,
        afterValue={
            "status": slot.status,
            "windowStart": slot.windowStart.isoformat(),
            "windowEnd": slot.windowEnd.isoformat(),
        },
        approvedByUserId=approver_id,
        raisedByUserId=user.id,
    )
    db.add(amendment)
    await db.flush()
    return {"ok": True, "status": slot.status, "amendmentId": amendment.id}


async def attach_unplanned_engagement(
    db: AsyncSession,
    *,
    cycle_id: str,
    engagement_kind: str,
    engagement_id: str,
    scope_unit_ids: list[str],
    period_index: int,
    window: tuple[date, date],
    user: User,
    status: str = "COMPLETED",
) -> dict[str, Any]:
    """Create an `origin=UNPLANNED` slot for an engagement run outside the plan.

    The invariant this protects: **no completed engagement may fail to appear in
    coverage.** An audit that happened off-plan still covered something, and a
    programme that quietly ignored it would report a false gap.
    """
    existing = (
        await db.execute(
            select(ProgrammeSlot).where(
                ProgrammeSlot.cycleId == cycle_id,
                ProgrammeSlot.engagementKind == engagement_kind.upper(),
                ProgrammeSlot.engagementId == engagement_id,
            )
        )
    ).scalars().first()
    if existing is not None:
        return {"ok": True, "slotId": existing.id, "created": False}

    n = (
        await db.execute(
            select(func.count(ProgrammeSlot.id)).where(ProgrammeSlot.cycleId == cycle_id)
        )
    ).scalar_one()

    slot = ProgrammeSlot(
        cycleId=cycle_id,
        slotCode=f"U{n + 1:03d}",
        windowStart=window[0],
        windowEnd=window[1],
        periodIndex=period_index,
        origin="UNPLANNED",
        engagementKind=engagement_kind.upper(),
        engagementId=engagement_id,
        status=status,
        amendmentCount=1,  # the ADD_SLOT amendment below
        createdBy=user.id,
    )
    db.add(slot)
    await db.flush()

    for uid in scope_unit_ids:
        db.add(SlotScopeUnit(slotId=slot.id, scopeUnitId=uid))

    db.add(
        ProgrammeAmendment(
            cycleId=cycle_id,
            slotId=slot.id,
            amendmentType="ADD_SLOT",
            reason=(
                "Engagement was conducted outside the approved programme; an UNPLANNED slot was "
                "created so it is counted in coverage and resource load."
            ),
            afterValue={"engagementKind": engagement_kind.upper(), "engagementId": engagement_id},
            approvedByUserId=user.id,
            raisedByUserId=user.id,
        )
    )
    await db.flush()
    return {"ok": True, "slotId": slot.id, "created": True}


async def integrity_check(db: AsyncSession, cycle_id: str) -> dict[str, Any]:
    """Band-0 integrity strip queries. Every count here must read 0.

    The defects in the CAMS diagnosis survived a month because nothing surfaced
    them. This is the cheap insurance.
    """
    slots = list(
        (
            await db.execute(select(ProgrammeSlot).where(ProgrammeSlot.cycleId == cycle_id))
        ).scalars().all()
    )
    orphan_transitions = [
        s.slotCode
        for s in slots
        if s.status != "PLANNED" and not s.engagementId and not (s.amendmentCount or 0)
    ]
    units = list(
        (
            await db.execute(
                select(ProgrammeScopeUnit).where(ProgrammeScopeUnit.cycleId == cycle_id)
            )
        ).scalars().all()
    )
    cycle = await db.get(ProgrammeCycle, cycle_id)
    approved = bool(cycle and cycle.status in ("APPROVED", "ACTIVE", "CLOSED"))
    unfrequenced = [
        (u.dimensionLabel or u.dimensionKey)
        for u in units
        if approved and not u.requiredPerCycle and not u.waiverReason
    ]
    return {
        "cycleId": cycle_id,
        "slotsNonPlannedWithoutEngagementOrAmendment": orphan_transitions,
        "scopeUnitsWithoutFrequencyOrWaiver": unfrequenced,
        "clean": not orphan_transitions and not unfrequenced,
    }


__all__ = [
    "CYCLE_TRANSITIONS",
    "SLOT_TRANSITIONS",
    "SLOT_REQUIRES_AMENDMENT",
    "SLOT_MATERIALISING",
    "cycle_transition_allowed",
    "slot_transition_allowed",
    "slot_needs_amendment",
    "approval_blockers",
    "approval_report",
    "approval_preview",
    "submit_cycle_for_review",
    "return_cycle_to_draft",
    "approve_cycle",
    "activate_cycle",
    "close_cycle",
    "transition_slot",
    "attach_unplanned_engagement",
    "integrity_check",
]
