"""Seed EAI review cycles from the CURRENT EAI register (ISO 14001 §9.1).

The /eai/reviews page was empty because no EaiReviewCycle rows exist. This
derives a realistic review programme from the entries that are actually in
the register (4 studies × 4 entries across NW + SW):

  • every ACTIVE entry gets its standing ANNUAL review (SCHEDULED, trigger
    SCHEDULE) at the entry's own nextReviewDue date
  • chlorine-leak ERP entries (seq 4, chlorination studies): COMPLETED
    INCIDENT-triggered reviews (post leak-drill verification, no change)
  • NW boiler combustion entry: COMPLETED AUDIT_FINDING review with a
    MINOR_REVISION outcome (stack-monitoring records gap)
  • Scope-1 GHG entries (the only residualSignificant=True rows): an
    IN_PROGRESS REGULATORY_CHANGE review (CPCB GHG reporting mandate)
  • NW cooling-tower blowdown entry: SCHEDULED MOC-triggered review
    (biocide chemistry change)
  • one SKIPPED MANUAL duplicate on NW chlorine dosing

Completed cycles also stamp the entry's review metadata (lastReviewedAt /
reviewCount / lastReviewType) the same way the submit endpoint does.

Idempotent-ish: refuses to run if cycles already exist; `--reset` wipes all
EaiReviewCycle rows first.

Run from the backend root:
    .venv/Scripts/python.exe scripts/seed_eai_review_cycles.py [--reset]
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from app.core.db import AsyncSessionLocal, engine
from app.models.eai import EaiEntry, EaiReviewCycle, EaiStudy

NOW = datetime.now(timezone.utc)


async def main() -> None:
    reset = "--reset" in sys.argv
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(func.count(EaiReviewCycle.id)))).scalar_one()
        if existing and not reset:
            print(f"{existing} review cycles already exist — run with --reset to reseed.")
            return
        if existing:
            await db.execute(delete(EaiReviewCycle))
            print(f"Deleted {existing} existing cycles.")

        rows = (
            await db.execute(
                select(EaiEntry, EaiStudy)
                .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
                .where(EaiEntry.isCurrentVersion.is_(True))
                .order_by(EaiStudy.number, EaiEntry.sequenceNumber)
            )
        ).all()
        if not rows:
            print("No EAI entries found — seed the EAI register first.")
            return

        created = {"SCHEDULED": 0, "IN_PROGRESS": 0, "COMPLETED": 0, "SKIPPED": 0}

        def add(entry: EaiEntry, study: EaiStudy, *, scheduled_for: datetime,
                trigger: str, status: str, outcome: str | None = None,
                outcome_notes: str | None = None, changes: list | None = None,
                started_at: datetime | None = None,
                completed_at: datetime | None = None) -> None:
            db.add(EaiReviewCycle(
                entryId=entry.id,
                scheduledFor=scheduled_for,
                triggeredBy=trigger,
                status=status,
                assignedToId=study.teamLeaderId,
                assignedRole="EAI Team Leader",
                startedAt=started_at,
                completedAt=completed_at,
                completedById=study.teamLeaderId if completed_at else None,
                outcome=outcome,
                outcomeNotes=outcome_notes,
                changesMade=changes,
            ))
            created[status] += 1
            # Mirror what the submit endpoint stamps on the entry.
            if status == "COMPLETED" and completed_at is not None:
                entry.lastReviewedAt = completed_at
                entry.lastReviewedById = study.teamLeaderId
                entry.lastReviewType = trigger
                entry.reviewCount = (entry.reviewCount or 0) + 1

        for entry, study in rows:
            is_chlor = "Chlorination" in study.title
            is_boiler = "Boiler" in study.title
            is_nw = "NW" in study.title

            # 1. Standing annual review for every active entry.
            add(entry, study,
                scheduled_for=entry.nextReviewDue or (NOW + timedelta(days=365)),
                trigger="SCHEDULE", status="SCHEDULED")

            # 2. Chlorine leak ERP entries → completed INCIDENT review.
            if is_chlor and entry.sequenceNumber == 4:
                add(entry, study,
                    scheduled_for=NOW - timedelta(days=24),
                    trigger="INCIDENT", status="COMPLETED",
                    outcome="NO_CHANGE_REQUIRED",
                    outcome_notes=(
                        "Re-review after the chlorine leak emergency drill and the "
                        "minor cylinder-valve seep incident. ERP activation steps, "
                        "SCBA staging and neighbour-notification protocol verified "
                        "as still accurate. No change to aspects, impacts or controls."
                    ),
                    started_at=NOW - timedelta(days=24),
                    completed_at=NOW - timedelta(days=21))

            # 3. NW boiler combustion entry → completed AUDIT_FINDING review.
            if is_boiler and is_nw and entry.sequenceNumber == 1:
                add(entry, study,
                    scheduled_for=NOW - timedelta(days=45),
                    trigger="AUDIT_FINDING", status="COMPLETED",
                    outcome="MINOR_REVISION",
                    outcome_notes=(
                        "ISO 14001 surveillance audit observation: stack-emission "
                        "monitoring records were not referenced in the existing "
                        "controls. Control list updated to cite the quarterly "
                        "third-party stack monitoring report as the verification "
                        "method. Significance determination unchanged."
                    ),
                    changes=[{"field": "existing_controls",
                              "change": "Added quarterly stack monitoring report as verification evidence"}],
                    started_at=NOW - timedelta(days=44),
                    completed_at=NOW - timedelta(days=40))

            # 4. Scope-1 GHG entries (the significant ones) → regulatory
            #    change review currently in progress.
            if entry.residualSignificant:
                add(entry, study,
                    scheduled_for=NOW + timedelta(days=7),
                    trigger="REGULATORY_CHANGE", status="IN_PROGRESS",
                    outcome_notes=None,
                    started_at=NOW - timedelta(days=3))

            # 5. NW cooling tower blowdown → MOC-triggered review scheduled.
            if is_boiler and is_nw and entry.sequenceNumber == 2:
                add(entry, study,
                    scheduled_for=NOW + timedelta(days=14),
                    trigger="MOC", status="SCHEDULED")

            # 6. NW chlorine dosing → manual review raised then skipped as a
            #    duplicate of the standing annual.
            if is_chlor and is_nw and entry.sequenceNumber == 2:
                add(entry, study,
                    scheduled_for=NOW - timedelta(days=10),
                    trigger="MANUAL", status="SKIPPED",
                    outcome_notes=(
                        "Raised manually during the monthly HSE walk-down; skipped "
                        "as a duplicate — the same scope is covered by the annual "
                        "scheduled review of this entry."
                    ),
                    completed_at=NOW - timedelta(days=9))

        await db.commit()
        total = sum(created.values())
        print(f"Created {total} review cycles across {len(rows)} entries:")
        for k, v in created.items():
            print(f"  {k:12} {v}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
