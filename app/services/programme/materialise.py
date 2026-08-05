"""Turn a planned slot into a real engagement, and link it back.

docs/cams/08 §6.1: *"Materialise-to-engagement pre-fills the schedule flow from
the slot: site, scope units → disciplines, standards, intended lead, sampling
approach, estimated duration."*

**What this replaces.** The slot manager offered a free-text "engagement id"
box hard-coded to `engagementKind: "AUDIT"`. That is not materialisation — it is
asking the user to go and create an engagement somewhere else, copy a UUID out
of a URL, and paste it back. It could not link an inspection at all, despite the
pointer being polymorphic, and nothing checked that the pasted id was for the
right site or covered the planned scope. A mistyped id silently produced a slot
whose coverage and variance were computed against someone else's audit.

Here the plan IS the input: the slot's scope units define the disciplines, the
programme defines the standards, the window defines the date, and the intended
lead becomes the lead auditor. The engagement is created and the slot is
transitioned in ONE transaction, so a slot can never end up pointing at an
engagement that failed to create, and an engagement can never be created for a
slot that then failed to link.

Both engines are reachable, because `ProgrammeSlot.engagementKind` was always
polymorphic and only the UI pretended otherwise.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_compliance import AuditCheckpointLibrary
from app.models.cams import CamsEngagement
from app.models.programme import (
    AuditProgramme,
    ProgrammeCycle,
    ProgrammeScopeUnit,
    ProgrammeSlot,
    SlotScopeUnit,
)
from app.models.user import User
from app.services import cams as cams_svc
from app.services.plant_directory import resolve_plant_names, site_label
from app.services.programme import lifecycle as lc

# A slot's sampling approach is a planning decision that the engagement inherits
# only as a note today — neither engine has a sampling column. Carried into the
# scope statement rather than dropped, so the conducted engagement still states
# the basis it was planned on.
_SAMPLING_NOTE = {
    "FULL": "",
    "RANDOM_N_OF_M": "Sampling basis: random n-of-m.",
    "RISK_WEIGHTED": "Sampling basis: risk-weighted.",
    "JUDGEMENTAL": "Sampling basis: judgemental.",
}


async def slot_plan(db: AsyncSession, slot_id: str) -> dict[str, Any]:
    """Everything the schedule flow needs, derived from the plan.

    Read-only. The UI calls this to render a pre-filled form; `materialise`
    re-derives it server-side rather than trusting the round trip, so a stale
    tab cannot schedule against a scope that has since changed.
    """
    slot = await db.get(ProgrammeSlot, slot_id)
    if slot is None:
        raise ValueError("Slot not found")
    cycle = await db.get(ProgrammeCycle, slot.cycleId)
    programme = await db.get(AuditProgramme, cycle.programmeId) if cycle else None

    unit_ids = [
        ln.scopeUnitId
        for ln in (
            await db.execute(select(SlotScopeUnit).where(SlotScopeUnit.slotId == slot_id))
        ).scalars().all()
    ]
    units: list[ProgrammeScopeUnit] = []
    if unit_ids:
        units = list(
            (
                await db.execute(
                    select(ProgrammeScopeUnit).where(ProgrammeScopeUnit.id.in_(unit_ids))
                )
            ).scalars().all()
        )

    disciplines = sorted({u.dimensionKey for u in units if u.dimension == "DISCIPLINE"})
    unit_standards = sorted({u.dimensionKey for u in units if u.dimension == "STANDARD"})
    standards = sorted(set((programme.standardRefs if programme else []) or []) | set(unit_standards))

    # A slot covering several sites cannot become one engagement — both engines
    # are single-site. Estate-wide units (siteId NULL) do not constrain it.
    site_ids = sorted({u.siteId for u in units if u.siteId})
    site_id = site_ids[0] if len(site_ids) == 1 else None

    industry_code, matched, unmatched = await _library_for(db, disciplines)

    # The materialise dialog asks "this slot spans N sites — pick one", and the
    # slot detail lists its scope units by site. Both need names, not cuids.
    plant_names = await resolve_plant_names(db, site_ids)

    return {
        "slotId": slot.id,
        "slotCode": slot.slotCode,
        "cycleId": slot.cycleId,
        "programmeId": cycle.programmeId if cycle else None,
        "programmeName": programme.name if programme else None,
        "status": slot.status,
        "origin": slot.origin,
        "windowStart": slot.windowStart.isoformat(),
        "windowEnd": slot.windowEnd.isoformat(),
        "periodIndex": slot.periodIndex,
        "siteId": site_id,
        "siteName": site_label(plant_names, site_id) if site_id else None,
        "siteIds": site_ids,
        # Parallel to `siteIds` so the picker can render an option label without
        # a second round-trip. Keyed by id, not positional — order is the
        # caller's business.
        "siteNames": {sid: site_label(plant_names, sid) for sid in site_ids},
        "multiSite": len(site_ids) > 1,
        "disciplineCodes": disciplines,
        "standardRefs": standards,
        "intendedLeadUserId": slot.intendedLeadUserId,
        "estimatedAuditorDays": slot.estimatedAuditorDays,
        "samplingApproach": slot.samplingApproach,
        "samplingJustification": slot.samplingJustification,
        "industryCode": industry_code,
        "matchedDisciplineCodes": matched,
        "unmatchedDisciplineCodes": unmatched,
        "suggestedTitle": _suggested_title(programme, cycle, slot, units),
        "scopeUnits": [
            {
                "id": u.id,
                "dimension": u.dimension,
                "siteId": u.siteId,
                "siteName": site_label(plant_names, u.siteId),
                "dimensionKey": u.dimensionKey,
                "dimensionLabel": u.dimensionLabel or u.dimensionKey,
            }
            for u in units
        ],
        "alreadyMaterialised": bool(slot.engagementId),
        "engagementKind": slot.engagementKind,
        "engagementId": slot.engagementId,
    }


async def _library_for(
    db: AsyncSession, discipline_codes: list[str]
) -> tuple[str | None, list[str], list[str]]:
    """Pick the checkpoint library that actually contains the planned scope.

    A scope unit's `dimensionKey` IS a library `category_code` (that is the join
    `resolver.resolve_audit` counts coverage on), but the programme does not
    record which library it came from. Choosing the library that covers the most
    planned disciplines keeps the two in step without adding a column, and the
    codes it CANNOT cover are returned rather than silently dropped — a slot
    that quietly schedules 3 of its 5 planned disciplines is exactly the scope
    variance the programme exists to expose.
    """
    if not discipline_codes:
        return None, [], []
    libs = list(
        (
            await db.execute(
                select(AuditCheckpointLibrary).where(AuditCheckpointLibrary.isActive.is_(True))
            )
        ).scalars().all()
    )
    wanted = set(discipline_codes)
    best: tuple[int, str | None, set[str]] = (0, None, set())
    for lib in libs:
        codes = {c.get("category_code") for c in (lib.categories or [])}
        hit = wanted & codes
        if len(hit) > best[0]:
            best = (len(hit), lib.industryCode, hit)
    matched = sorted(best[2])
    return best[1], matched, sorted(wanted - best[2])


def _suggested_title(
    programme: AuditProgramme | None,
    cycle: ProgrammeCycle | None,
    slot: ProgrammeSlot,
    units: list[ProgrammeScopeUnit],
) -> str:
    labels = [u.dimensionLabel or u.dimensionKey for u in units if u.dimension == "DISCIPLINE"]
    scope = ", ".join(labels[:3]) + (f" +{len(labels) - 3}" if len(labels) > 3 else "")
    parts = [p for p in (cycle.cycleLabel if cycle else None, f"P{slot.periodIndex + 1}") if p]
    head = " ".join(parts)
    if scope:
        return f"{head} {scope} audit".strip()
    return f"{head} {programme.name if programme else 'programme'} audit".strip()


def _planned_datetime(window_start: date, window_end: date, *, on: date | None) -> datetime:
    """A window is not a date; scheduling one forces a date to be chosen.

    Defaults to the window's opening day rather than its midpoint, because a
    plan that starts at the top of its window has room to slip inside it — which
    is the whole reason the plan carries a window at all.
    """
    when = on or window_start
    when = min(max(when, window_start), window_end) if window_end >= window_start else when
    return datetime.combine(when, time(9, 0), tzinfo=timezone.utc)


async def materialise_slot(
    db: AsyncSession,
    *,
    slot_id: str,
    user: User,
    engagement_kind: str,
    lead_auditor_id: str | None = None,
    site_id: str | None = None,
    title: str | None = None,
    scheduled_on: date | None = None,
    # Matches the router's default so a direct caller and an HTTP caller produce
    # the same engagement type. Only read on the INSPECTION branch.
    engagement_type: str = "INSPECTION",
    template_id: str | None = None,
    plant_manager_user_id: str | None = None,
    auditee_user_ids: list[str] | None = None,
    co_auditor_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Create the engagement this slot planned, and link it — one transaction.

    Flushes but does not commit; the router owns the commit so the slot
    transition and the engagement land together or not at all.
    """
    kind = (engagement_kind or "").upper()
    if kind not in ("AUDIT", "INSPECTION"):
        raise ValueError("engagementKind must be AUDIT or INSPECTION")

    plan = await slot_plan(db, slot_id)
    slot = await db.get(ProgrammeSlot, slot_id)
    if slot is None:
        raise ValueError("Slot not found")
    if slot.engagementId:
        raise ValueError(
            f"{slot.slotCode} is already materialised against {slot.engagementKind} "
            f"{slot.engagementId}. Create a new slot rather than re-pointing this one — the "
            "existing link is what its coverage and variance are computed from."
        )
    if not lc.slot_transition_allowed(slot.status, "SCHEDULED"):
        raise ValueError(f"A {slot.status} slot cannot be materialised")

    resolved_site = site_id or plan["siteId"]
    if not resolved_site:
        raise ValueError(
            "This slot's scope units name more than one site (or none), so the site cannot be "
            "derived — choose the site this engagement runs at."
            if plan["multiSite"]
            else "This slot has no site in its scope units — choose the site this engagement "
            "runs at."
        )
    lead = lead_auditor_id or slot.intendedLeadUserId
    if not lead:
        raise ValueError("An engagement needs a lead auditor.")

    planned_at = _planned_datetime(slot.windowStart, slot.windowEnd, on=scheduled_on)
    resolved_title = (title or plan["suggestedTitle"]).strip()
    sampling_note = _SAMPLING_NOTE.get(slot.samplingApproach or "FULL", "")
    scope_statement = " ".join(
        p
        for p in (
            f"Planned under {plan['programmeName'] or 'the audit programme'}, slot "
            f"{slot.slotCode} ({slot.windowStart.isoformat()} – {slot.windowEnd.isoformat()}).",
            sampling_note,
            (slot.samplingJustification or "").strip(),
        )
        if p
    )

    if kind == "AUDIT":
        if not plan["industryCode"]:
            raise ValueError(
                "None of this slot's planned disciplines match an active checkpoint library, so "
                "an audit cannot be materialised from it. Link the slot to discipline scope "
                "units, or materialise it as an inspection."
            )
        # Imported here: audit_compliance imports a wide slice of the module and
        # importing it at module scope makes `programme` depend on the whole
        # audit engine just to read a slot.
        from app.services import audit_compliance as ac_svc

        audit = await ac_svc.create_audit(
            db,
            user=user,
            data={
                "plantId": resolved_site,
                "title": resolved_title,
                "industryCode": plan["industryCode"],
                "templateId": template_id,
                "selectedDisciplineIds": plan["matchedDisciplineCodes"],
                "scopePresetUsed": "PROGRAMME_SLOT",
                "scheduledDate": planned_at,
                "scheduledStartTime": "09:00",
                # Auditor-days → hours, so the plan's own estimate reaches the
                # engagement instead of the create form's hard-coded 4.
                "estimatedDurationHours": max(1, round((slot.estimatedAuditorDays or 1.0) * 8)),
                "leadAuditorUserId": lead,
                "plantManagerUserId": plant_manager_user_id,
                "coAuditors": [{"userId": u, "disciplineIds": []} for u in (co_auditor_ids or [])],
                "auditees": [{"userId": u, "responsibleCategories": []} for u in (auditee_user_ids or [])],
                "scopeDescription": scope_statement,
            },
        )
        engagement_id, code = audit.id, audit.auditNumber
    else:
        engagement = CamsEngagement(
            engagementCode=await cams_svc.next_engagement_code(db, engagement_type),
            title=resolved_title,
            engagementType=engagement_type,
            standardRefs=plan["standardRefs"],
            siteId=resolved_site,
            scopeStatement=scope_statement,
            leadAuditorId=lead,
            auditTeamIds=list(co_auditor_ids or []),
            auditeeOwnerId=plant_manager_user_id,
            plannedDate=planned_at,
            scheduledStart=planned_at,
            templateId=template_id,
            riskBasis="ROUTINE",
            status="SCHEDULED",
            sourceModule="PROGRAMME",
            sourceEntityId=slot.id,
            createdBy=user.id,
        )
        db.add(engagement)
        await db.flush()
        engagement_id, code = engagement.id, engagement.engagementCode

    # The link and the state change go through the ONE writer of slot status, so
    # the "no slot leaves PLANNED without an engagement or an amendment"
    # invariant is enforced here exactly as it is everywhere else.
    out = await lc.transition_slot(
        db,
        slot_id=slot_id,
        target="SCHEDULED",
        user=user,
        engagement_kind=kind,
        engagement_id=engagement_id,
    )
    return {
        **out,
        "engagementKind": kind,
        "engagementId": engagement_id,
        "engagementCode": code,
        "href": (
            f"/cams/audits/{engagement_id}" if kind == "AUDIT"
            else f"/cams/engagements/{engagement_id}"
        ),
        "unmatchedDisciplineCodes": plan["unmatchedDisciplineCodes"] if kind == "AUDIT" else [],
    }


__all__ = ["slot_plan", "materialise_slot"]
