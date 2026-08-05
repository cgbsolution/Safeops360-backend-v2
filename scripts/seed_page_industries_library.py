"""Seed the PAGE_INDUSTRIES internal-audit checkpoint library.

The demo instance audits ONE checklist: the Page Industries internal audit
workbook, whose three sheets — HR, EHS and Production — become the three
disciplines, 40 checkpoints each.

The questions in `app/seed/data/page_industries_checkpoints.json` are verbatim
from the customer's workbook (column B). Two fields are NOT in the workbook's
data and were derived here:

  • `requirement_type` (column I) — the workbook leaves it for the auditor to
    pick per row, but it is a property of the requirement, not of the audit: a
    Factories Act licence is statutory whichever auditor looks at it. It is
    therefore set once, here, and rendered read-only during conduct.
  • `criticality` — the engine's own severity axis, which gates auto-CAPA and
    the critical-failure rule. Distinct from the auditor's Risk Grade (column H),
    which is their assessment of what they actually found.

Both are editable in Configuration → Checkpoint libraries if Page want to move
a line between statutory and internal.

Idempotent: upserts by industryCode, so re-running refreshes the content
without touching audits already materialised from it (those hold their own
snapshot rows).

Run from the backend root:
    python scripts/seed_page_industries_library.py
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine, text

from app.core.config import get_settings

INDUSTRY_CODE = "PAGE_INDUSTRIES"
INDUSTRY_NAME = "Page Industries — Internal Audit"
VERSION = "2026.1"

DATA = Path(__file__).resolve().parents[1] / "app" / "seed" / "data" / "page_industries_checkpoints.json"


def _enrich(categories: list[dict]) -> list[dict]:
    """Fill the per-checkpoint rules the engine reads at materialization.

    Photo-on-fail and auto-CAPA follow the checkpoint's criticality, matching
    the rule the other libraries already use: a critical finding must carry
    evidence and raises a corrective action on its own.
    """
    for cat in categories:
        for cp in cat["checkpoints"]:
            crit = cp.get("criticality", "major")
            cp.setdefault("guidance", "")
            cp.setdefault("requirement_reference", "")
            cp.setdefault("standard", "Page Industries Internal Audit")
            cp.setdefault("response_type", "page_grading")
            cp.setdefault("requires_photo_on_fail", crit in ("critical", "major"))
            cp.setdefault("auto_trigger_capa_on_fail", crit == "critical")
            cp.setdefault(
                "capa_severity_if_triggered",
                "critical" if crit == "critical" else "major" if crit == "major" else "minor",
            )
            cp.setdefault("linked_safeops_module", None)
    return categories


def main() -> None:
    categories = _enrich(json.loads(DATA.read_text(encoding="utf-8")))
    count = sum(len(c["checkpoints"]) for c in categories)

    settings = get_settings()
    engine = create_engine(settings.sync_database_url, pool_pre_ping=True)
    with engine.begin() as conn:
        existing = conn.execute(
            text('SELECT id FROM "AuditCheckpointLibrary" WHERE "industryCode" = :c'),
            {"c": INDUSTRY_CODE},
        ).scalar()
        params = {
            "code": INDUSTRY_CODE,
            "name": INDUSTRY_NAME,
            "version": VERSION,
            "cats": json.dumps(categories, ensure_ascii=False),
            "count": count,
        }
        if existing:
            conn.execute(
                text(
                    'UPDATE "AuditCheckpointLibrary" SET "industryName" = :name, '
                    '"version" = :version, "categories" = CAST(:cats AS jsonb), '
                    '"checkpointCount" = :count, "isActive" = true, "updatedAt" = now() '
                    'WHERE "industryCode" = :code'
                ),
                params,
            )
            print(f"Updated {INDUSTRY_CODE} — {count} checkpoints across {len(categories)} disciplines.")
        else:
            conn.execute(
                text(
                    'INSERT INTO "AuditCheckpointLibrary" '
                    '("id", "industryCode", "industryName", "version", "categories", '
                    ' "checkpointCount", "isActive", "createdAt", "updatedAt") '
                    "VALUES (gen_random_uuid()::text, :code, :name, :version, "
                    "CAST(:cats AS jsonb), :count, true, now(), now())"
                ),
                params,
            )
            print(f"Created {INDUSTRY_CODE} — {count} checkpoints across {len(categories)} disciplines.")

        for cat in categories:
            statutory = sum(
                1 for cp in cat["checkpoints"] if cp["requirement_type"] == "STATUTORY_REGULATORY"
            )
            print(
                f"  {cat['category_name']:<32} {len(cat['checkpoints']):>3} checkpoints "
                f"({statutory} statutory / {len(cat['checkpoints']) - statutory} internal)"
            )


if __name__ == "__main__":
    main()
