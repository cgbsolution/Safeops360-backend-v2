"""CAMS calendar bookings — additive DDL.

Creates the single table behind the Microsoft 365 / Teams calendar booking
feature:

  * CalendarBooking  — one row per calendar event SafeOps360 owns (the audit
                       fieldwork block, the opening meeting, the closing
                       meeting), for either engine (AUDIT = ComplianceAudit,
                       INSPECTION = CamsEngagement).

Additive + re-runnable (CREATE TABLE / CREATE INDEX ... IF NOT EXISTS) through
the SYNC engine — never `prisma db push` (known Cams* drift would drop tables),
matching `add_assurance_integrity.py`.

    .venv/Scripts/python.exe scripts/add_calendar_bookings.py

Ends with verification SELECTs that must all report present (docs/cams/04-target
§9: no migration ships without one).
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

STMTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS "CalendarBooking" (
        "id"               TEXT PRIMARY KEY,
        "engagementKind"   TEXT NOT NULL,
        "engagementId"     TEXT NOT NULL,
        "bookingType"      TEXT NOT NULL,
        "siteId"           TEXT,
        "subject"          TEXT NOT NULL,
        "bodyHtml"         TEXT NOT NULL DEFAULT '',
        "location"         TEXT NOT NULL DEFAULT '',
        "startAt"          TIMESTAMPTZ NOT NULL,
        "endAt"            TIMESTAMPTZ NOT NULL,
        "timezone"         TEXT NOT NULL DEFAULT 'Asia/Kolkata',
        "organizerUserId"  TEXT,
        "organizerEmail"   TEXT,
        "attendees"        JSONB NOT NULL DEFAULT '[]'::jsonb,
        "roomEmail"        TEXT,
        "roomName"         TEXT,
        "roomStatus"       TEXT NOT NULL DEFAULT 'NONE',
        "roomPinned"       BOOLEAN NOT NULL DEFAULT FALSE,
        "isOnlineMeeting"  BOOLEAN NOT NULL DEFAULT TRUE,
        "onlineMeetingUrl" TEXT,
        "provider"         TEXT NOT NULL DEFAULT 'NONE',
        "providerEventId"  TEXT,
        "transactionId"    TEXT,
        "status"           TEXT NOT NULL DEFAULT 'PENDING',
        "revision"         INTEGER NOT NULL DEFAULT 0,
        "contentHash"      TEXT,
        "attemptCount"     INTEGER NOT NULL DEFAULT 0,
        "lastAttemptAt"    TIMESTAMPTZ,
        "lastSyncedAt"     TIMESTAMPTZ,
        "lastError"        TEXT,
        "cancelledAt"      TIMESTAMPTZ,
        "cancelReason"     TEXT,
        "createdAt"        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "createdBy"        TEXT,
        "updatedAt"        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "updatedBy"        TEXT
    )
    """,
    # The duplicate-invite defence. Every sync path upserts through this
    # constraint, so re-running a sync updates a booking and can never create a
    # second opening meeting for the same audit.
    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_CalendarBooking_type" '
    'ON "CalendarBooking" ("engagementKind", "engagementId", "bookingType")',
    'CREATE INDEX IF NOT EXISTS "ix_CalendarBooking_engagement" '
    'ON "CalendarBooking" ("engagementKind", "engagementId")',
    # The retry job's access path: PENDING rows whose window has not passed.
    'CREATE INDEX IF NOT EXISTS "ix_CalendarBooking_status_start" '
    'ON "CalendarBooking" ("status", "startAt")',
    'CREATE INDEX IF NOT EXISTS "ix_CalendarBooking_site" ON "CalendarBooking" ("siteId")',
    # ── Meeting rooms ────────────────────────────────────────────────
    # Re-runnable ADD COLUMNs so a deployment that already installed the first
    # version of this table picks the room columns up without a second script.
    'ALTER TABLE "CalendarBooking" ADD COLUMN IF NOT EXISTS "roomEmail" TEXT',
    'ALTER TABLE "CalendarBooking" ADD COLUMN IF NOT EXISTS "roomName" TEXT',
    "ALTER TABLE \"CalendarBooking\" ADD COLUMN IF NOT EXISTS \"roomStatus\" TEXT NOT NULL DEFAULT 'NONE'",
    'ALTER TABLE "CalendarBooking" ADD COLUMN IF NOT EXISTS "roomPinned" BOOLEAN NOT NULL DEFAULT FALSE',
    # The maintenance job chases rooms that have not answered yet.
    'CREATE INDEX IF NOT EXISTS "ix_CalendarBooking_roomStatus" '
    'ON "CalendarBooking" ("roomStatus", "endAt")',
    # Site-level default room — what makes "schedule the audit" book a room
    # without anyone choosing one each time.
    'ALTER TABLE "Plant" ADD COLUMN IF NOT EXISTS "defaultMeetingRoomEmail" TEXT',
    'ALTER TABLE "Plant" ADD COLUMN IF NOT EXISTS "defaultMeetingRoomName" TEXT',
]

TABLES = ["CalendarBooking"]
INDEXES = [
    "uq_CalendarBooking_type",
    "ix_CalendarBooking_engagement",
    "ix_CalendarBooking_status_start",
    "ix_CalendarBooking_roomStatus",
]
COLUMNS = [
    ("CalendarBooking", "roomEmail"),
    ("CalendarBooking", "roomStatus"),
    ("CalendarBooking", "roomPinned"),
    ("Plant", "defaultMeetingRoomEmail"),
    ("Plant", "defaultMeetingRoomName"),
]


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    failures = 0
    with Session(engine) as s:
        for stmt in STMTS:
            s.execute(text(stmt))
        s.commit()

        print("-- verification -----------------------------")
        for t in TABLES:
            ok = bool(
                s.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name=:t"
                    ),
                    {"t": t},
                ).first()
            )
            print(f"  table  {t:<28} {'present' if ok else 'MISSING'}")
            failures += 0 if ok else 1
        for ix in INDEXES:
            ok = bool(
                s.execute(
                    text("SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname=:i"),
                    {"i": ix},
                ).first()
            )
            print(f"  index  {ix:<28} {'present' if ok else 'MISSING'}")
            failures += 0 if ok else 1
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

        # Existing audits are NOT back-booked by this migration, deliberately.
        # Mass-inviting people to audits scheduled before the feature existed —
        # including ones already in progress — would be the loudest possible way
        # to introduce it. Bookings start with the next audit created, and an
        # existing audit can be booked on demand from its own screen.
        pending = s.execute(
            text(
                "SELECT count(*) FROM \"ComplianceAudit\" "
                "WHERE \"status\" IN ('scheduled','in_progress') AND \"isDeleted\" = FALSE"
            )
        ).scalar_one()
        print(
            f"\n  {pending} audit(s) are open and were NOT back-booked — by design.\n"
            "  Use 'Book calendars' on an audit, or POST /api/calendar/bookings/sync,\n"
            "  to claim time for an audit that predates this feature."
        )

    print("\nDONE" if not failures else f"\nDONE with {failures} MISSING object(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
