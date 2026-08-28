"""Overdue fire-checklist reminders and escalation.

WHAT RUNS THIS
--------------
`scheduler.py`'s existing asyncio supervisor, as the `fire_checklist_overdue`
job — the same mechanism already running `fire_equipment_status` nightly. No new
scheduling infrastructure: the supervisor already gives every job its own
session, a SystemActor for audit attribution, a JobRun record, run-now, and
misfire catch-up on boot. A second scheduler would have to reimplement all five.

Notifications go through `erm_notifications.create_notification`, which writes
the in-app `Notification` row AND fires a best-effort email. That is the
platform's existing channel pair; there is no WhatsApp integration here and this
does not add one.

THE ASSIGNMENT PROBLEM, AND WHY THIS DOES NOT GUESS
---------------------------------------------------
`FireEquipment.assignedTechnicianId` is new and null on every row. Nothing on
this platform has ever assigned a fire asset to a person — `maintenanceContractor`
is free text naming a company ("SafeFire Services Pvt Ltd" on 24 of 37 assets),
not a user.

So "notify the assigned technician" has no one to notify yet, and there are two
legitimate answers: a data-entry pass assigning real people, or a location-based
default resolving to whoever holds a maintenance role at that plant. Those give
different people different accountability for a missed statutory inspection, so
the choice belongs to the business owner, not to this module.

Until it is made, `UNASSIGNED_STRATEGY` defaults to `report`: the sweep records
the period as overdue and unassigned, sends no technician notification, and the
gap becomes visible on the asset and in the escalation to the EHS lead. It does
NOT silently pick someone — a reminder sent to a guessed recipient is worse than
one that says nobody is assigned, because it looks handled.

Setting `FIRE_REMINDER_UNASSIGNED_STRATEGY=location_default` switches to the
role-based fallback once that decision is made.

ESCALATION IS IN ADDITION, NEVER INSTEAD
-----------------------------------------
At due+N the EHS lead is told as well. The technician's own notification is not
withdrawn or superseded — an escalation that silently replaced it would leave
the person who can actually fill the sheet with no outstanding prompt.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.fire_safety import FireChecklistReminder, FireEquipment
from app.models.user import User
from app.services import fire_checklists as svc
from app.services import fire_checklist_admin as admin
from app.services.erm_notifications import _users_with_role, create_notification

log = logging.getLogger("safeops360.fire.reminders")

STATE_PENDING = "PENDING"
STATE_NOTIFIED = "NOTIFIED"
STATE_ESCALATED = "ESCALATED"
STATE_RESOLVED = "RESOLVED"

# Roles tried in order for "the EHS lead for this location". A chain rather than
# one code because site structures differ: not every plant has a PLANT_HSE_HEAD,
# and an escalation that resolves to nobody is an escalation that did not happen.
EHS_LEAD_ROLES = ("PLANT_HSE_HEAD", "HSE_MANAGER", "SAFETY_OFFICER")
# Only consulted under the `location_default` strategy.
TECHNICIAN_ROLES = ("MAINTENANCE_HEAD", "STORE_KEEPER")


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        log.warning("%s is not a number; using %s", name, default)
        return default


def escalate_after_days() -> int:
    """Client-configurable. 3 days by default — long enough that a sheet missed
    over a weekend is not escalated before anyone could have filled it, short
    enough that a genuinely abandoned inspection surfaces the same week."""
    return _env_int("FIRE_REMINDER_ESCALATE_DAYS", 3)


# How far back a sweep mints rows, PER CADENCE. One flat window was wrong: at 45
# days a daily checklist yields 45 overdue periods per asset per template, so the
# first run on 37 assets minted 329 rows and buried the periods anyone can still
# act on. A daily sheet missed six weeks ago is history; a quarterly one is not.
_LOOKBACK_DEFAULTS = {"DAILY": 14, "MONTHLY": 62, "QUARTERLY": 200, "ANNUAL": 400}


def lookback_days(frequency: str = "MONTHLY") -> int:
    """How far back this cadence's sweep will mint reminder rows.

    `FIRE_REMINDER_LOOKBACK_DAYS` overrides every cadence when set — an escape
    hatch for a client who wants a longer tail, not the normal path.
    """
    override = (os.environ.get("FIRE_REMINDER_LOOKBACK_DAYS") or "").strip()
    if override:
        return _env_int("FIRE_REMINDER_LOOKBACK_DAYS", 45)
    return _LOOKBACK_DEFAULTS.get(frequency, 45)


def unassigned_strategy() -> str:
    """`report` (default) or `location_default`. See the module docstring."""
    raw = (os.environ.get("FIRE_REMINDER_UNASSIGNED_STRATEGY") or "report").strip().lower()
    return raw if raw in ("report", "location_default") else "report"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_dt(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════════════════════
# Recipients
# ═══════════════════════════════════════════════════════════════════════════
async def resolve_technician(db, asset: FireEquipment) -> tuple[str | None, bool]:
    """(user id, unassigned). Never invents a recipient under the default."""
    if asset.assignedTechnicianId:
        user = await db.get(User, asset.assignedTechnicianId)
        if user is not None:
            return user.id, False
        # Assigned to someone who no longer exists — a different problem from
        # never assigned, and the row records which.
        log.warning("Asset %s is assigned to missing user %s", asset.equipmentCode,
                    asset.assignedTechnicianId)
    if unassigned_strategy() == "location_default":
        for role in TECHNICIAN_ROLES:
            users = await _users_with_role(db, role, asset.plantId)
            scoped = [u for u in users if u.plantId == asset.plantId]
            if scoped:
                return scoped[0].id, False
    return None, True


async def resolve_ehs_leads(db, plant_id: str) -> list[User]:
    """Whoever the escalation goes to at this site, first role that yields anyone."""
    for role in EHS_LEAD_ROLES:
        users = await _users_with_role(db, role, plant_id)
        scoped = [u for u in users if u.plantId == plant_id]
        if scoped:
            # Bounded: a role held by 131 people at one site (which exists in
            # this data) must not become 131 emails a night.
            return scoped[:5]
    return []


# ═══════════════════════════════════════════════════════════════════════════
# The sweep
# ═══════════════════════════════════════════════════════════════════════════
async def _reminder_for(db, asset_id: str, template_id: str, period: str) -> FireChecklistReminder | None:
    return (
        await db.execute(
            select(FireChecklistReminder)
            .where(FireChecklistReminder.assetId == asset_id)
            .where(FireChecklistReminder.templateId == template_id)
            .where(FireChecklistReminder.period == period)
        )
    ).scalars().first()


async def sweep(db, *, today: date | None = None, dry_run: bool = False) -> dict[str, Any]:
    """One daily pass: mint reminders, notify at due+0, escalate at due+N.

    Idempotent. Re-running the same day changes nothing, because every action is
    guarded by the state already recorded on the reminder row.
    """
    today = today or _now().date()
    escalate_days = escalate_after_days()

    templates = [t for t in await admin.list_templates(db) if t.status == "APPROVED"]
    assets = (
        await db.execute(select(FireEquipment).where(FireEquipment.isDeleted.is_(False)))
    ).scalars().all()
    by_type: dict[str, list[FireEquipment]] = {}
    for a in assets:
        by_type.setdefault(a.type, []).append(a)

    created = notified = escalated = resolved = unassigned = 0
    skipped_no_lead = 0

    for tpl in templates:
        meta = tpl.documentMeta or {}
        asset_type = meta.get("assetType")
        frequency = meta.get("frequency", "MONTHLY")
        variant = meta.get("siteVariant")
        for asset in by_type.get(asset_type, []):
            # The same variant rule the scan page uses — offering the Unit-21 B
            # Loop sheet for a Zone panel would make it "overdue" forever.
            if variant and asset.assetSubtype and not admin_variant_ok(variant, asset.assetSubtype):
                continue
            try:
                periods = svc.overdue_periods(
                    frequency, today, lookback_days=lookback_days(frequency)
                )
            except svc.ChecklistError:
                continue

            for period in periods:
                due = svc.period_end(frequency, period)
                run = await svc.find_run(db, tpl, asset.id, period)
                done = run is not None and svc.stage_of(run) == svc.STAGE_APPROVED
                row = await _reminder_for(db, asset.id, tpl.id, period)

                if done:
                    # Completed late: keep the row as evidence, mark it resolved.
                    if row is not None and row.state != STATE_RESOLVED:
                        if not dry_run:
                            row.state = STATE_RESOLVED
                            row.resolvedAt = _now()
                        resolved += 1
                    continue

                if row is None:
                    tech_id, is_unassigned = await resolve_technician(db, asset)
                    row = FireChecklistReminder(
                        assetId=asset.id, templateId=tpl.id, templateCode=tpl.templateCode,
                        frequency=frequency, period=period, plantId=asset.plantId,
                        dueDate=_as_dt(due), state=STATE_PENDING,
                        technicianUserId=tech_id, unassigned=is_unassigned, escalatedTo=[],
                    )
                    created += 1
                    if is_unassigned:
                        unassigned += 1
                    if not dry_run:
                        db.add(row)
                        try:
                            async with db.begin_nested():
                                await db.flush()
                        except IntegrityError:
                            # Another worker minted it first. Re-read rather than
                            # fail the whole sweep for one row.
                            row = await _reminder_for(db, asset.id, tpl.id, period)
                            if row is None:
                                continue

                if dry_run:
                    continue

                # ── due + 0: tell the technician ──
                notified_this_sweep = False
                if row.state == STATE_PENDING:
                    if row.technicianUserId:
                        await _notify_technician(db, row, asset, tpl)
                        notified += 1
                    row.state = STATE_NOTIFIED
                    row.notifiedAt = _now()
                    notified_this_sweep = True

                # ── due + N: tell the EHS lead AS WELL ──
                #
                # `notified_this_sweep` is what stops a first run escalating a
                # whole backlog in one pass. Without it, every period already
                # older than N escalates in the same sweep that first notices it
                # — on this data that was 287 escalations and 238 emails from
                # one run, which is how a reminder system teaches people to
                # ignore it before it has sent a single useful message.
                #
                # It also makes the rule honest: escalation means "the
                # technician was told and it is still not done", which cannot be
                # true in the same pass that told them.
                #
                # Deliberately NOT a date comparison against `notifiedAt`: that
                # column records real send time, and comparing it to this
                # sweep's logical `today` mixes two clocks that only agree when
                # the job runs live. Same-pass is the property actually meant.
                overdue_by = (today - due).days
                if (
                    row.state == STATE_NOTIFIED
                    and overdue_by >= escalate_days
                    and not notified_this_sweep
                ):
                    leads = await resolve_ehs_leads(db, asset.plantId)
                    if not leads:
                        # Recorded, not swallowed: an escalation with no
                        # recipient is a site with no EHS lead configured, which
                        # is itself something someone needs to fix.
                        skipped_no_lead += 1
                        log.warning("No EHS lead at plant %s — cannot escalate %s/%s",
                                    asset.plantId, asset.equipmentCode, period)
                    else:
                        await _escalate(db, row, asset, tpl, leads, overdue_by)
                        escalated += 1

    if not dry_run:
        await db.flush()
    return {
        "date": today.isoformat(),
        "remindersCreated": created,
        "techniciansNotified": notified,
        "escalated": escalated,
        "resolved": resolved,
        "unassignedAssets": unassigned,
        "escalationsWithNoLead": skipped_no_lead,
        "escalateAfterDays": escalate_days,
        "strategy": unassigned_strategy(),
        "dryRun": dry_run,
    }


def admin_variant_ok(site_variant: str, subtype: str) -> bool:
    """Mirror of the scan page's variant rule, so a panel is never reported
    overdue on a sheet that does not apply to its addressing."""
    v = (site_variant or "").upper()
    s = (subtype or "").upper()
    if "ZONE" in v:
        return "ZONE" in s or not s
    if "LOOP" in v:
        return "LOOP" in s or not s
    return True


async def _notify_technician(db, row: FireChecklistReminder, asset: FireEquipment, tpl) -> None:
    await create_notification(
        db,
        user_id=row.technicianUserId,
        type="FIRE_CHECKLIST_OVERDUE",
        title=f"Overdue: {tpl.name} — {asset.equipmentCode}",
        body=(
            f"The {row.frequency.lower()} checklist for {asset.equipmentCode} "
            f"({asset.location}) covering {row.period} was due on "
            f"{row.dueDate:%d %b %Y} and has not been completed.\n\n"
            f"Scan the sticker on the unit, or open the fire checklists in SafeOps360."
        ),
        severity="WARNING",
        entity_type="FireEquipment",
        entity_id=asset.id,
        link_url=f"/fire-safety/equipment/{asset.id}",
    )


async def _escalate(db, row: FireChecklistReminder, asset: FireEquipment, tpl,
                    leads: list[User], overdue_by: int) -> None:
    who = (
        "No technician is assigned to this asset."
        if row.unassigned
        else "The assigned technician was notified when it became overdue."
    )
    for lead in leads:
        await create_notification(
            db,
            user_id=lead.id,
            type="FIRE_CHECKLIST_ESCALATED",
            title=f"Escalation: {tpl.name} overdue {overdue_by}d — {asset.equipmentCode}",
            body=(
                f"{asset.equipmentCode} ({asset.location}) has not had its "
                f"{row.frequency.lower()} checklist for {row.period} completed. "
                f"It was due on {row.dueDate:%d %b %Y}, {overdue_by} days ago.\n\n"
                f"{who}"
            ),
            severity="CRITICAL",
            entity_type="FireEquipment",
            entity_id=asset.id,
            link_url=f"/fire-safety/equipment/{asset.id}",
        )
    row.state = STATE_ESCALATED
    row.escalatedAt = _now()
    row.escalatedTo = [lead.id for lead in leads]


# ═══════════════════════════════════════════════════════════════════════════
# Read side — what the UI shows
# ═══════════════════════════════════════════════════════════════════════════
async def open_reminders_for_assets(db, asset_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Worst open reminder per asset, for the register's overdue badge.

    Worst rather than all: a register row has space for one badge, and
    "escalated" is the state that needs acting on when an asset has both.
    """
    if not asset_ids:
        return {}
    rows = (
        await db.execute(
            select(FireChecklistReminder)
            .where(FireChecklistReminder.assetId.in_(asset_ids))
            .where(FireChecklistReminder.state.in_([STATE_PENDING, STATE_NOTIFIED, STATE_ESCALATED]))
            .order_by(FireChecklistReminder.dueDate.asc())
        )
    ).scalars().all()
    rank = {STATE_ESCALATED: 3, STATE_NOTIFIED: 2, STATE_PENDING: 1}
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        prev = out.get(r.assetId)
        cand = {
            "state": r.state,
            "period": r.period,
            "templateCode": r.templateCode,
            "frequency": r.frequency,
            "dueDate": r.dueDate.isoformat() if r.dueDate else None,
            "escalatedAt": r.escalatedAt.isoformat() if r.escalatedAt else None,
            "unassigned": r.unassigned,
            "openCount": 1,
        }
        if prev is None:
            out[r.assetId] = cand
        else:
            prev["openCount"] += 1
            if rank.get(r.state, 0) > rank.get(prev["state"], 0):
                cand["openCount"] = prev["openCount"]
                out[r.assetId] = cand
    return out


__all__ = [
    "STATE_PENDING", "STATE_NOTIFIED", "STATE_ESCALATED", "STATE_RESOLVED",
    "EHS_LEAD_ROLES", "TECHNICIAN_ROLES",
    "escalate_after_days", "lookback_days", "unassigned_strategy",
    "resolve_technician", "resolve_ehs_leads", "sweep", "open_reminders_for_assets",
]
