"""Meeting-record calendar propagation + scheduler-organised bookings - additive DDL.

Two columns:

  * EngagementMeeting."addToCalendar" - whether the people named on this
    meeting minute are also carried onto the engagement's calendar bookings.
  * CalendarBooking."organizerFallbackEmail" - the mailbox to organise from when
    the lead auditor's address is not one Graph knows. It holds whoever
    scheduled the engagement, so a demo/external lead auditor no longer means
    the booking simply fails. Stored because the retry job delivers from the row
    alone, with no engagement in hand to resolve it from.

Why a stored flag rather than a one-off action at save time: the calendar is a
projection that gets recomputed from scratch on every sync (team change, retry
job, operator pressing Sync). If "these attendees were invited" lived only in
the moment of saving, the very next recompute would drop them back out again.
The flag is what makes the record part of the desired state.

DEFAULT FALSE is deliberate. Meetings recorded before this shipped were minutes
and nothing more; defaulting them to TRUE would mean the next sync of every
historical audit mailed invitations to people about meetings that already
happened. New records opt in from the form, which defaults the box to ticked.

Additive + re-runnable (ADD COLUMN ... IF NOT EXISTS) through the SYNC engine -
never `prisma db push` (known Cams* drift would drop tables).

    .venv/Scripts/python.exe scripts/add_meeting_calendar_sync.py

Ends with verification SELECTs that must all report present (the rule from
docs/cams/04-target.md §9: no migration ships without one).
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

STMTS: list[str] = [
    'ALTER TABLE "EngagementMeeting" '
    'ADD COLUMN IF NOT EXISTS "addToCalendar" BOOLEAN NOT NULL DEFAULT FALSE',
    'ALTER TABLE "CalendarBooking" '
    'ADD COLUMN IF NOT EXISTS "organizerFallbackEmail" TEXT',
]

COLUMNS: list[tuple[str, str]] = [
    ("EngagementMeeting", "addToCalendar"),
    ("CalendarBooking", "organizerFallbackEmail"),
]


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    failures = 0
    with Session(engine) as s:
        for stmt in STMTS:
            s.execute(text(stmt))
        s.commit()

        print("-- verification -----------------------------")
        for tbl, col in COLUMNS:
            ok = bool(
                s.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=:t AND column_name=:c"
                    ),
                    {"t": tbl, "c": col},
                ).first()
            )
            print(f"  column {tbl}.{col:<24} {'present' if ok else 'MISSING'}")
            failures += 0 if ok else 1

        existing = s.execute(text('SELECT count(*) FROM "EngagementMeeting"')).scalar_one()
        print(
            f"\n  {existing} existing meeting record(s) keep addToCalendar = FALSE."
            "\n  They stay minutes only; re-save one with the box ticked to carry its"
            "\n  attendees onto the audit's calendar."
        )

    print("\nDONE" if not failures else f"\nDONE with {failures} MISSING object(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
