"""One-off: create the Module Entitlement & Licensing table.

Additive only — CREATE TABLE IF NOT EXISTS through the SYNC (psycopg2) engine,
so we never risk the 80+ existing tables on the shared Supabase DB (same policy
as create_ppe_tables.py / apply-factory-ddl.ts). Mirrors the SQLAlchemy model in
app/models/licensing.py. Re-runnable.

Run from the backend root:
    .venv/Scripts/python.exe scripts/create_licensing_tables.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS "LicenceInstallation" (
        "id" TEXT PRIMARY KEY,
        "installationId" TEXT NOT NULL,
        "firstBootAt" TIMESTAMPTZ NOT NULL,
        "lastSeenTimestamp" TIMESTAMPTZ NOT NULL,
        "lastStatus" TEXT,
        "lastValidatedAt" TIMESTAMPTZ,
        "lastError" TEXT,
        "clockTamperDetected" BOOLEAN NOT NULL DEFAULT false,
        "lastLicenceJti" TEXT,
        "lastLicenceIat" INTEGER,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Per-factory (per-Plant) module allocation, within the licence ceiling.
    """
    CREATE TABLE IF NOT EXISTS "FactoryModuleEntitlement" (
        "id" TEXT PRIMARY KEY,
        "plantId" TEXT NOT NULL,
        "moduleCode" TEXT NOT NULL,
        "enabled" BOOLEAN NOT NULL DEFAULT true,
        "updatedBy" TEXT,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    'CREATE UNIQUE INDEX IF NOT EXISTS "FactoryModuleEntitlement_plant_module_key" '
    'ON "FactoryModuleEntitlement" ("plantId", "moduleCode")',
    # Time-window columns (added later — idempotent ALTERs for existing installs).
    'ALTER TABLE "FactoryModuleEntitlement" ADD COLUMN IF NOT EXISTS "validFrom" TIMESTAMPTZ',
    'ALTER TABLE "FactoryModuleEntitlement" ADD COLUMN IF NOT EXISTS "validUntil" TIMESTAMPTZ',
]

TABLES = ["LicenceInstallation", "FactoryModuleEntitlement"]


def _existing(s: Session) -> set[str]:
    rows = s.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(:names)"
        ),
        {"names": TABLES},
    ).scalars().all()
    return set(rows)


def main() -> int:
    settings = get_settings()
    engine = create_engine(settings.sync_database_url, future=True)
    with Session(engine) as s:
        before = _existing(s)
        print(f"Before: {sorted(before) or 'LicenceInstallation does not exist yet'}")
        for stmt in DDL:
            s.execute(text(stmt))
        s.commit()
        after = _existing(s)
        print(f"After : {sorted(after)}")
        if set(TABLES) - after:
            print(f"!! MISSING: {sorted(set(TABLES) - after)}")
            return 1
        print("Done. LicenceInstallation present.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
