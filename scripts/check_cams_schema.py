"""Doctor: does the live schema match what the CAMS models now SELECT?

READ-ONLY. No DDL, no writes. Run this first when CAMS screens start erroring.

**Why this exists.** SQLAlchemy emits every mapped column in its SELECT list. So
the moment a column is added to a model, EVERY query against that table fails
until the DDL is applied - including queries that never touch the new column.
`ComplianceAudit` gained `reopenCount`, so `SELECT ... FROM "ComplianceAudit"`
breaks, and with it every audit screen in CAMS. This is the same failure the
Facilities module hit when the factory_ext DDL was unapplied.

    .venv/Scripts/python.exe scripts/check_cams_schema.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

# (table, column, the script that creates it)
REQUIRED_COLUMNS: list[tuple[str, str, str]] = [
    ("Area", "ownerUserId", "add_assurance_integrity.py"),
    ("AuditReport", "snapshotHashFull", "add_assurance_integrity.py"),
    ("ComplianceAudit", "reopenCount", "add_assurance_integrity.py"),
    ("ComplianceAudit", "lastReopenedAt", "add_assurance_integrity.py"),
    ("ComplianceAudit", "lastReopenReason", "add_assurance_integrity.py"),
    ("ComplianceAudit", "signOffs", "add_signoff_column.py"),
    ("CamsAuditType", "scoringRules", "add_audit_type_config.py"),
    ("CamsAuditType", "regimeCode", "add_audit_type_config.py"),
    ("CamsAuditType", "competenceEnforcement", "add_audit_type_config.py"),
]

REQUIRED_TABLES: list[tuple[str, str]] = [
    ("DisciplineOwner", "add_assurance_integrity.py"),
    ("IndependenceWaiver", "add_assurance_integrity.py"),
    ("EngagementCompetenceSnapshot", "add_assurance_integrity.py"),
    ("EngagementMeeting", "add_assurance_integrity.py"),
    ("ReportErratum", "add_assurance_integrity.py"),
    ("AuditProgramme", "add_programme_tables.py"),
    ("ProgrammeCycle", "add_programme_tables.py"),
    ("ProgrammeScopeUnit", "add_programme_tables.py"),
    ("ProgrammeSlot", "add_programme_tables.py"),
    ("SlotScopeUnit", "add_programme_tables.py"),
    ("ProgrammeReview", "add_programme_tables.py"),
    ("ProgrammeAmendment", "add_programme_tables.py"),
    ("ProgrammeRecommendation", "add_programme_tables.py"),
    ("DisciplineHazardMap", "add_programme_tables.py"),
    ("AuditFinding", "add_cams_completion.py"),
    ("EvidencePackJob", "add_cams_completion.py"),
    ("NotificationPreference", "add_cams_completion.py"),
    ("SupplierAuditLink", "add_cams_completion.py"),
    ("CheckpointTranslation", "add_cams_completion.py"),
]

# Tables whose SELECTs break outright when a column above is missing - i.e. the
# blast radius, expressed as the screens the user will see fail.
BLAST_RADIUS = {
    "ComplianceAudit": "every CAMS audit screen (register, detail, conduct, reports)",
    "AuditReport": "audit report pages",
    "Area": "anything listing areas - observations, near-miss, permits, HIRA, incidents",
}


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    missing_cols: list[tuple[str, str, str]] = []
    missing_tables: list[tuple[str, str]] = []

    with Session(engine) as s:
        print("-- columns ------------------------------------------")
        for tbl, col, script in REQUIRED_COLUMNS:
            ok = bool(
                s.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=:t AND column_name=:c"
                    ),
                    {"t": tbl, "c": col},
                ).first()
            )
            print(f"  {'OK  ' if ok else 'MISS'}  {tbl}.{col}")
            if not ok:
                missing_cols.append((tbl, col, script))

        print("\n-- tables -------------------------------------------")
        for tbl, script in REQUIRED_TABLES:
            ok = bool(
                s.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name=:t"
                    ),
                    {"t": tbl},
                ).first()
            )
            print(f"  {'OK  ' if ok else 'MISS'}  {tbl}")
            if not ok:
                missing_tables.append((tbl, script))

    if not missing_cols and not missing_tables:
        print("\nOK: Schema matches the models. If CAMS is still erroring, the cause is elsewhere -")
        print("   check the uvicorn log for the actual traceback.")
        return 0

    print("\nPROBLEM: SCHEMA DRIFT - this is why CAMS is erroring.\n")
    if missing_cols:
        print("   Missing COLUMNS break every SELECT against their table, including")
        print("   queries that never reference the new column:\n")
        for tbl, col, _ in missing_cols:
            hit = BLAST_RADIUS.get(tbl)
            print(f"     {tbl}.{col}" + (f"  ->  breaks {hit}" if hit else ""))
        print()

    scripts = sorted({s for _, _, s in missing_cols} | {s for _, s in missing_tables})
    print("   Fix - run these, then RESTART uvicorn (not --reload):\n")
    for sc in scripts:
        print(f"     .venv/Scripts/python.exe scripts/{sc}")
    print("\n   Both are additive and re-runnable. Never `prisma db push` (Cams* drift).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
