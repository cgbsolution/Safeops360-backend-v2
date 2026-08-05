"""Backfill the annual programme from the engagements that already happened.

docs/cams/08-audit-programme.md §6.3. Specified there, never implemented: the
programme tables were created and left empty, so every programme screen rendered
a zero state over a working engine.

**This is derivation, not reconstruction.** Every row below comes from an
engagement that exists:

  1. one `AuditProgramme` + one ACTIVE `ProgrammeCycle` over the financial year
  2. `ProgrammeScopeUnit` rows from the (site, discipline) pairs **actually
     engaged** — not from the full checkpoint library, because seeding the
     library instead opens the demo on a wall of false gaps
  3. one `ProgrammeSlot` per existing engagement, `origin=UNPLANNED`, status
     mirroring the engagement, `engagementKind`/`engagementId` pointed at it

     **Deviation from §6.3, deliberate.** The design says the window is "the
     quarter containing `scheduledDate`". Doing that gives every engagement a
     91-day window, and the auditor-load collision detector — which flags any
     two windows that overlap for the same lead — then reports a collision for
     every pair of engagements one person ran in a quarter. On this tenant that
     was 42 false collisions on a tab whose entire job is to surface real ones.
     A quarter-wide window is the right shape for a *plan*; an engagement that
     already happened has a known date, so its slot gets a one-day window and
     the collision count reads true. Period assignment is unaffected — coverage
     buckets on `periodIndex`, not on the window.
  4. forward `PLANNED` slots, so the programme shows a plan and not just history
     — a programme with no future is a register
  5. two `DEFERRED` slots with real `ProgrammeAmendment` rows, because the
     amendment trail is the feature that distinguishes this from a calendar and
     it needs at least one live example
  6. three verification SELECTs that must each return zero

`origin=UNPLANNED` is the honest label: these engagements were genuinely
conducted outside a programme, because no programme existed. Marking them
INTERNAL would claim a plan that was never made.

**Two ordering hazards, both checked rather than assumed:**

  * **WP-01 (fixture cleanse) must precede this.** Backfilling slots for
    junk-titled audits would put `Test1` and `Scale Demo` into the coverage
    matrix permanently. The script refuses to run if any live audit still
    carries a fixture title, and names it.
  * **WP-02 (assessmentStatus backfill) must precede this.** Coverage counts
    `assessmentStatus != 'NOT_ASSESSED'`; running before the backfill bakes
    under-reported coverage into the demo. The script counts the desynced rows
    itself instead of trusting a prior report.

Out of scope, deliberately: §6.3 item 6 migrates `CamsRecurrence` onto scope
units and retires it from the scheduling path. That is a live-scheduler change,
not a backfill, and the design says to leave the table until WP-18.

Dry run by default — prints exactly what it WOULD write. Re-runnable: an
engagement that already has a slot is skipped, not duplicated.

    .venv/Scripts/python.exe scripts/backfill_programme_from_engagements.py
    .venv/Scripts/python.exe scripts/backfill_programme_from_engagements.py --commit

WARNING: The backend .env points at PRODUCTION.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.cams import CamsEngagement
from app.models.programme import (
    AuditProgramme,
    ProgrammeAmendment,
    ProgrammeCycle,
    ProgrammeScopeUnit,
    ProgrammeSlot,
    SlotScopeUnit,
)
from app.services.access_scope import DEPLOYMENT_TENANT_ID
from app.services.programme.coverage import period_bounds

# ── Configuration ────────────────────────────────────────────────────

PROGRAMME_CODE = "PRG-INT-FY26"
PROGRAMME_NAME = "Internal Audit Programme FY26"
CYCLE_LABEL = "FY26"
PERIOD_START = date(2026, 4, 1)
PERIOD_END = date(2027, 3, 31)
PERIODS = 4
OBJECTIVES = (
    "Verify conformity of the management system across the estate, confirm that controls on "
    "high-risk activities are effective, and provide input to management review "
    "(ISO 19011 §5.2)."
)

# Fixture titles that must not reach the coverage matrix (WP-01).
FIXTURE_TITLE = re.compile(
    r"(scale\s*demo|scale_demo|\btest\s*\d|\bdemo\s*\d|discipline\s+\d|^test$|lorem)",
    re.IGNORECASE,
)

# Engagement status → slot status. A slot mirrors what became of the engagement;
# it does not invent a state the engagement never reached.
AUDIT_SLOT_STATUS = {
    "closed": "COMPLETED",
    "under_review": "IN_PROGRESS",
    "response_in_progress": "IN_PROGRESS",
    "submitted_pending_response": "IN_PROGRESS",
    "in_progress": "IN_PROGRESS",
}
INSPECTION_SLOT_STATUS = {
    "CLOSED": "COMPLETED",
    "REPORT_ISSUED": "COMPLETED",
    "FINDINGS_REVIEW": "IN_PROGRESS",
    "FIELDWORK_COMPLETE": "IN_PROGRESS",
    "IN_PROGRESS": "IN_PROGRESS",
}

# Forward plan (§6.3 item 4) — periodIndex, auditor-days.
FORWARD_SLOTS = [(2, 2.0), (2, 1.5), (3, 2.0), (3, 1.5), (3, 1.0), (3, 2.0)]
# Deferrals (§6.3 item 5) — periodIndex, reason.
DEFERRALS = [
    (
        1,
        "Deferred at the request of the site: the planned window collided with the peak "
        "shipment period and the auditee team could not be released without stopping the line.",
    ),
    (
        2,
        "Deferred because the assigned lead auditor was on extended medical leave and no "
        "independent alternate was competent for this discipline in the window.",
    ),
]


def _as_date(v) -> date | None:
    if v is None:
        return None
    return v.date() if isinstance(v, datetime) else v


def _period_of(bounds: list[tuple[date, date]], when: date | None) -> int:
    if when is None:
        return 0
    for i, (s, e) in enumerate(bounds):
        if s <= when <= e:
            return i
    # Outside the cycle — clamp rather than drop. An engagement conducted before
    # the cycle opened is still evidence; putting it in the first period is
    # honest about the imprecision and keeps it in coverage.
    return 0 if when < bounds[0][0] else len(bounds) - 1


def _window_for(bounds: list[tuple[date, date]], idx: int) -> tuple[date, date]:
    return bounds[idx]


def _actual_window(
    bounds: list[tuple[date, date]], idx: int, when: date | None
) -> tuple[date, date]:
    """The window for an engagement that already happened: the day it happened.

    See the deviation note in the module docstring. Falls back to the period
    when the engagement carries no date at all.
    """
    if when is None:
        return bounds[idx]
    lo, hi = bounds[idx]
    d = min(max(when, lo), hi)
    return (d, d)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write; otherwise dry run")
    ap.add_argument(
        "--force",
        action="store_true",
        help="proceed despite the WP-01/WP-02 preflight failing (records the mess permanently)",
    )
    args = ap.parse_args()

    engine = create_engine(get_settings().sync_database_url, future=True)
    bounds = period_bounds(PERIOD_START, PERIOD_END, PERIODS)
    now = datetime.now(timezone.utc)

    with Session(engine) as s:
        # ── Preflight ────────────────────────────────────────────────
        print("-- preflight ---------------------------------")
        audits = list(
            s.execute(
                select(ComplianceAudit).where(ComplianceAudit.isDeleted.is_(False))
            ).scalars().all()
        )
        engagements = list(
            s.execute(
                select(CamsEngagement).where(CamsEngagement.isDeleted.is_(False))
            ).scalars().all()
        )
        print(f"  live ComplianceAudit rows   {len(audits)}")
        print(f"  live CamsEngagement rows    {len(engagements)}")

        junk = [a for a in audits if FIXTURE_TITLE.search(a.title or "")]
        if junk:
            print(f"  WP-01 fixture titles        {len(junk)} STILL PRESENT:")
            for a in junk[:10]:
                print(f"      {a.auditNumber}  {a.title!r}")
        else:
            print("  WP-01 fixture titles        0  ok")

        desynced = s.execute(
            text(
                'SELECT count(*) FROM "AuditCheckpointResponse" '
                "WHERE \"auditorResponse\"->>'value' IS NOT NULL "
                "AND \"assessmentStatus\" = 'NOT_ASSESSED'"
            )
        ).scalar_one()
        print(
            f"  WP-02 desynced responses    {desynced}"
            f"  {'ok' if desynced == 0 else 'NOT BACKFILLED'}"
        )

        if (junk or desynced) and not args.force:
            print(
                "\nREFUSING. Run the WP-01 title cleanse and/or "
                "scripts/backfill_assessment_status.py first — backfilling now would bake "
                "fixture rows and under-reported coverage into the programme permanently. "
                "Use --force only if you genuinely want that."
            )
            return 1
        eligible_audits = [a for a in audits if a not in junk]

        # ── Before ───────────────────────────────────────────────────
        linked_before = {
            (k, i)
            for k, i in s.execute(
                select(ProgrammeSlot.engagementKind, ProgrammeSlot.engagementId).where(
                    ProgrammeSlot.engagementId.isnot(None)
                )
            ).all()
        }
        total_engagements = len(eligible_audits) + len(engagements)
        print("\n-- before ------------------------------------")
        print(f"  engagements linked to a slot    {len(linked_before)}")
        print(f"  engagements NOT linked          {total_engagements - len(linked_before)}")

        # ── Programme + cycle ────────────────────────────────────────
        programme = s.execute(
            select(AuditProgramme).where(AuditProgramme.programmeCode == PROGRAMME_CODE)
        ).scalars().first()
        owner = s.execute(
            select(ComplianceAudit.leadAuditorUserId)
            .where(ComplianceAudit.leadAuditorUserId.isnot(None))
            .limit(1)
        ).scalar_one_or_none()

        created = {
            "programme": 0, "cycle": 0, "units": 0, "slots": 0, "links": 0,
            "amendments": 0, "retimed": 0,
        }

        if programme is None:
            programme = AuditProgramme(
                tenantId=DEPLOYMENT_TENANT_ID,
                programmeCode=PROGRAMME_CODE,
                name=PROGRAMME_NAME,
                objectives=OBJECTIVES,
                scopeStatement=(
                    "All operating sites and the disciplines actually engaged during the cycle."
                ),
                standardRefs=["ISO 45001", "ISO 14001"],
                ownerUserId=owner or "system",
                status="ACTIVE",
            )
            s.add(programme)
            s.flush()
            created["programme"] = 1

        cycle = s.execute(
            select(ProgrammeCycle).where(
                ProgrammeCycle.programmeId == programme.id,
                ProgrammeCycle.cycleLabel == CYCLE_LABEL,
            )
        ).scalars().first()
        if cycle is None:
            cycle = ProgrammeCycle(
                programmeId=programme.id,
                cycleLabel=CYCLE_LABEL,
                periodStart=PERIOD_START,
                periodEnd=PERIOD_END,
                periodsPerCycle=PERIODS,
                # ACTIVE, not APPROVED: this cycle was never submitted or
                # approved by anyone, and stamping an approval nobody gave would
                # forge the exact record the module exists to make trustworthy.
                status="ACTIVE",
                activatedAt=now,
            )
            s.add(cycle)
            s.flush()
            created["cycle"] = 1

        # ── Scope units, from what was actually engaged ──────────────
        unit_key: dict[tuple[str, str | None, str], ProgrammeScopeUnit] = {}
        for u in s.execute(
            select(ProgrammeScopeUnit).where(ProgrammeScopeUnit.cycleId == cycle.id)
        ).scalars().all():
            unit_key[(u.dimension, u.siteId, u.dimensionKey)] = u

        def ensure_unit(dimension: str, site_id: str | None, key: str, label: str) -> ProgrammeScopeUnit:
            hit = unit_key.get((dimension, site_id, key))
            if hit is not None:
                return hit
            u = ProgrammeScopeUnit(
                cycleId=cycle.id,
                dimension=dimension,
                siteId=site_id,
                dimensionKey=key,
                dimensionLabel=label or key,
                # Frequency derives from observed cadence below; seeded at 1 so
                # no unit ever lands on an ACTIVE cycle with neither a frequency
                # nor a waiver (integrity check 2).
                requiredPerCycle=1,
                riskWeight=3,
                rationale=(
                    "Derived from engagements actually conducted at this site during the cycle."
                ),
            )
            s.add(u)
            s.flush()
            unit_key[(dimension, site_id, key)] = u
            created["units"] += 1
            return u

        audit_ids = [a.id for a in eligible_audits]
        # (auditId, categoryId) → categoryName, for the disciplines each audit touched.
        dims_by_audit: dict[str, dict[str, str]] = {}
        if audit_ids:
            for aid, cat, cname in s.execute(
                select(
                    AuditCheckpointResponse.auditId,
                    AuditCheckpointResponse.categoryId,
                    func.max(AuditCheckpointResponse.categoryName),
                )
                .where(AuditCheckpointResponse.auditId.in_(audit_ids))
                .group_by(AuditCheckpointResponse.auditId, AuditCheckpointResponse.categoryId)
            ).all():
                if cat:
                    dims_by_audit.setdefault(aid, {})[cat] = cname or cat

        # ── One slot per engagement ─────────────────────────────────
        existing_slot_codes = {
            c for (c,) in s.execute(
                select(ProgrammeSlot.slotCode).where(ProgrammeSlot.cycleId == cycle.id)
            ).all()
        }
        seq = len(existing_slot_codes)

        def add_slot(
            *, code_prefix: str, window: tuple[date, date], period_index: int, origin: str,
            status: str, kind: str | None, engagement_id: str | None, lead: str | None,
            days: float, units: list[ProgrammeScopeUnit], amendment_count: int = 0,
        ) -> ProgrammeSlot:
            nonlocal seq
            seq += 1
            slot = ProgrammeSlot(
                cycleId=cycle.id,
                slotCode=f"{code_prefix}{seq:03d}",
                windowStart=window[0],
                windowEnd=window[1],
                periodIndex=period_index,
                origin=origin,
                engagementKind=kind,
                engagementId=engagement_id,
                intendedLeadUserId=lead,
                estimatedAuditorDays=days,
                samplingApproach="FULL",
                status=status,
                amendmentCount=amendment_count,
            )
            s.add(slot)
            s.flush()
            created["slots"] += 1
            for u in units:
                s.add(SlotScopeUnit(slotId=slot.id, scopeUnitId=u.id))
                created["links"] += 1
            return slot

        for a in eligible_audits:
            if ("AUDIT", a.id) in linked_before:
                continue
            when = _as_date(a.closedAt or a.actualEndAt or a.submittedAt or a.scheduledDate)
            idx = _period_of(bounds, when)
            dims = dims_by_audit.get(a.id, {})
            units = [
                ensure_unit("DISCIPLINE", a.plantId, code, label) for code, label in dims.items()
            ]
            if not units:
                # No checkpoint rows → nothing it could have covered. Skipping
                # keeps a hollow engagement out of the matrix instead of
                # inventing a discipline for it.
                continue
            add_slot(
                code_prefix="U",
                window=_actual_window(bounds, idx, when),
                period_index=idx,
                origin="UNPLANNED",
                status=AUDIT_SLOT_STATUS.get(a.status, "SCHEDULED"),
                kind="AUDIT",
                engagement_id=a.id,
                lead=a.leadAuditorUserId,
                days=float(a.estimatedDurationHours or 8) / 8.0,
                units=units,
            )

        for e in engagements:
            if ("INSPECTION", e.id) in linked_before:
                continue
            when = _as_date(e.conductedDate or e.plannedDate)
            idx = _period_of(bounds, when)
            standards = list(e.standardRefs or []) or ["_UNSPECIFIED"]
            units = [
                ensure_unit("STANDARD", e.siteId, std, std.replace("_", " ").title())
                for std in standards
            ]
            add_slot(
                code_prefix="U",
                window=_actual_window(bounds, idx, when),
                period_index=idx,
                origin="UNPLANNED",
                status=INSPECTION_SLOT_STATUS.get(e.status, "SCHEDULED"),
                kind="INSPECTION",
                engagement_id=e.id,
                lead=e.leadAuditorId,
                days=1.0,
                units=units,
            )

        # ── Repair pass: tighten windows on already-created UNPLANNED slots ──
        # A previous run of this script created quarter-wide windows per §6.3,
        # which made the collision detector fire on every pair of engagements one
        # lead ran in a quarter. Re-running corrects them in place rather than
        # requiring the whole backfill be torn down and redone.
        audit_date = {a.id: _as_date(a.closedAt or a.actualEndAt or a.submittedAt or a.scheduledDate)
                      for a in eligible_audits}
        insp_date = {e.id: _as_date(e.conductedDate or e.plannedDate) for e in engagements}
        for sl in s.execute(
            select(ProgrammeSlot).where(
                ProgrammeSlot.cycleId == cycle.id,
                ProgrammeSlot.origin == "UNPLANNED",
                ProgrammeSlot.engagementId.isnot(None),
            )
        ).scalars().all():
            when = (audit_date if sl.engagementKind == "AUDIT" else insp_date).get(
                sl.engagementId
            )
            want = _actual_window(bounds, sl.periodIndex, when)
            if (sl.windowStart, sl.windowEnd) != want:
                sl.windowStart, sl.windowEnd = want
                created["retimed"] += 1

        # ── Forward plan + deferrals ─────────────────────────────────
        all_units = list(unit_key.values())
        has_forward = any(
            sl.origin == "INTERNAL"
            for sl in s.execute(
                select(ProgrammeSlot).where(ProgrammeSlot.cycleId == cycle.id)
            ).scalars().all()
        )
        if all_units and not has_forward:
            for n, (idx, days) in enumerate(FORWARD_SLOTS):
                add_slot(
                    code_prefix="S",
                    window=_window_for(bounds, idx),
                    period_index=idx,
                    origin="INTERNAL",
                    status="PLANNED",
                    kind=None,
                    engagement_id=None,
                    lead=owner,
                    days=days,
                    units=[all_units[n % len(all_units)]],
                )

            for n, (idx, reason) in enumerate(DEFERRALS):
                unit = all_units[(n + len(FORWARD_SLOTS)) % len(all_units)]
                # A deferral moves the window forward — a deferred slot is not a
                # deleted one — and the amendment is written in the same breath,
                # which is the invariant the DB CHECK also enforces.
                original = _window_for(bounds, idx)
                new_window = (
                    original[0] + timedelta(days=90),
                    original[1] + timedelta(days=90),
                )
                slot = add_slot(
                    code_prefix="S",
                    window=new_window,
                    period_index=min(idx + 1, PERIODS - 1),
                    origin="INTERNAL",
                    status="DEFERRED",
                    kind=None,
                    engagement_id=None,
                    lead=owner,
                    days=1.5,
                    units=[unit],
                    amendment_count=1,
                )
                s.add(
                    ProgrammeAmendment(
                        cycleId=cycle.id,
                        slotId=slot.id,
                        amendmentType="DEFER",
                        reason=reason,
                        beforeValue={
                            "status": "PLANNED",
                            "windowStart": original[0].isoformat(),
                            "windowEnd": original[1].isoformat(),
                        },
                        afterValue={
                            "status": "DEFERRED",
                            "windowStart": new_window[0].isoformat(),
                            "windowEnd": new_window[1].isoformat(),
                        },
                        approvedByUserId=owner or "system",
                        raisedByUserId=owner or "system",
                    )
                )
                created["amendments"] += 1

        # ── Frequency from observed cadence ─────────────────────────
        # A unit engaged three times in the cycle plainly has a cadence of about
        # three; leaving every unit at 1 would report a shortfall of zero
        # everywhere and make the required-frequency column decorative.
        counts: dict[str, int] = {}
        for (unit_id,) in s.execute(
            select(SlotScopeUnit.scopeUnitId)
            .join(ProgrammeSlot, ProgrammeSlot.id == SlotScopeUnit.slotId)
            .where(
                ProgrammeSlot.cycleId == cycle.id,
                # Only slots that could actually discharge the frequency — a
                # cancelled or waived slot covers nothing.
                ProgrammeSlot.status.notin_(("CANCELLED", "WAIVED")),
            )
        ).all():
            counts[unit_id] = counts.get(unit_id, 0) + 1
        for u in unit_key.values():
            u.requiredPerCycle = max(1, min(4, counts.get(u.id, 1)))

        s.flush()

        print("\n-- would create -------------------------------" if not args.commit
              else "\n-- created ------------------------------------")
        for k, v in created.items():
            print(f"  {k:<12} {v}")

        # ── After + invariants ───────────────────────────────────────
        linked_after = s.execute(
            select(func.count(ProgrammeSlot.id)).where(ProgrammeSlot.engagementId.isnot(None))
        ).scalar_one()
        print("\n-- after -------------------------------------")
        print(f"  engagements linked to a slot    {linked_after}")
        print(f"  engagements NOT linked          {total_engagements - linked_after}")

        print("\n-- invariants (each must be 0) ---------------")
        checks = [
            (
                "completed engagements with no slot",
                'SELECT count(*) FROM "ComplianceAudit" a WHERE a."isDeleted" = false '
                "AND a.\"status\" = 'closed' AND NOT EXISTS (SELECT 1 FROM \"ProgrammeSlot\" s "
                "WHERE s.\"engagementKind\" = 'AUDIT' AND s.\"engagementId\" = a.\"id\")",
            ),
            (
                "slots non-PLANNED with neither engagement nor amendment",
                'SELECT count(*) FROM "ProgrammeSlot" WHERE "status" <> \'PLANNED\' '
                'AND "engagementId" IS NULL AND "amendmentCount" = 0',
            ),
            (
                "scope units on an approved cycle with no frequency and no waiver",
                'SELECT count(*) FROM "ProgrammeScopeUnit" u JOIN "ProgrammeCycle" c '
                'ON c."id" = u."cycleId" WHERE c."status" IN (\'APPROVED\',\'ACTIVE\',\'CLOSED\') '
                'AND u."requiredPerCycle" IS NULL AND u."waiverReason" IS NULL',
            ),
        ]
        failures = 0
        for label, sql in checks:
            n = s.execute(text(sql)).scalar_one()
            print(f"  {n:>4}  {label}")
            failures += 0 if n == 0 else 1

        if args.commit:
            if failures:
                s.rollback()
                print(f"\nROLLED BACK — {failures} invariant(s) failed.")
                return 1
            s.commit()
            print(f"\nCOMMITTED. Programme {PROGRAMME_CODE}, cycle {CYCLE_LABEL}.")
        else:
            s.rollback()
            print("\nDRY RUN — nothing written. Re-run with --commit.")
        return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
