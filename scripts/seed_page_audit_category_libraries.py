"""Seed the two checkpoint libraries behind the new audit categories.

The scheduler now picks an AUDIT CATEGORY first, and the category resolves the
checklist the disciplines come from:

  INTERNAL           -> PAGE_INDUSTRIES  (HR / EHS / Production)  — already seeded
                        by seed_page_industries_library.py
  MANAGEMENT_SYSTEMS -> PAGE_IMS         (QMS / EMS / OHS / EnMS)  — this script
  SOCIAL_COMPLIANCE  -> PAGE_SOCIAL      (9 sections of Annexure-2) — this script

Both are seeded here rather than in two scripts because they are one delivery:
neither category can be scheduled until its library exists, and a half-loaded
pair puts a category on screen that materialises nothing.

Content comes from app/seed/data/page_ims_checkpoints.json and
page_social_compliance_checkpoints.json, regenerated from the customer's
workbooks by scripts/extract_page_workbooks.py. The questions are verbatim from
column B (IMS) and column E (social); `guidance` is the workbook's own Audit
Reference column and `requirement_reference` its clause number, so an auditor
sees the same instruction they would have read off the sheet.

The audit FORMAT is deliberately identical to the internal audit's: every
checkpoint is `response_type = page_grading`, so all three categories share one
conduct screen, one Grade/Compliance/Risk vocabulary and one scoring rollup.
The category changes which questions are asked, never how they are answered.

Idempotent: upserts by industryCode. Audits already materialised from either
library are untouched — they hold their own snapshot rows.

Run from the backend root:
    python scripts/seed_page_audit_category_libraries.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Running a FILE puts `scripts/` on sys.path, not the backend root, so `app` is
# not importable however sensible the cwd is. Same bootstrap as
# scripts/seed_fire_frequency_master.py, so the command in the docstring above
# actually works instead of needing PYTHONPATH=. in front of it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import get_settings  # noqa: E402

DATA_DIR = Path(__file__).resolve().parents[1] / "app" / "seed" / "data"

VERSION = "2026.1"

LIBRARIES = [
    {
        "industry_code": "PAGE_IMS",
        "industry_name": "Page Industries — QMS, EMS & OHS",
        "data": DATA_DIR / "page_ims_checkpoints.json",
        "standard": "ISO 9001:2015 / ISO 14001:2015 / ISO 45001:2018 / ISO 50001:2018",
    },
    {
        "industry_code": "PAGE_SOCIAL",
        "industry_name": "Page Industries — Social Compliance",
        "data": DATA_DIR / "page_social_compliance_checkpoints.json",
        "standard": "PIL Social Compliance Audit Checklist (Annexure-2, v4)",
    },
]


def _enrich(categories: list[dict], default_standard: str) -> list[dict]:
    """Fill the per-checkpoint rules the engine reads at materialization.

    Identical to seed_page_industries_library._enrich — photo-on-fail and
    auto-CAPA follow criticality, so a critical finding carries evidence and
    raises a corrective action on its own. Kept in step deliberately: three
    categories that grade the same way must also escalate the same way.
    """
    for cat in categories:
        for cp in cat["checkpoints"]:
            crit = cp.get("criticality", "major")
            cp.setdefault("guidance", "")
            cp.setdefault("requirement_reference", "")
            cp.setdefault("standard", default_standard)
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
    settings = get_settings()
    engine = create_engine(settings.sync_database_url, pool_pre_ping=True)

    for spec in LIBRARIES:
        categories = _enrich(
            json.loads(spec["data"].read_text(encoding="utf-8")), spec["standard"]
        )
        count = sum(len(c["checkpoints"]) for c in categories)
        params = {
            "code": spec["industry_code"],
            "name": spec["industry_name"],
            "version": VERSION,
            "cats": json.dumps(categories, ensure_ascii=False),
            "count": count,
        }

        with engine.begin() as conn:
            existing = conn.execute(
                text('SELECT id FROM "AuditCheckpointLibrary" WHERE "industryCode" = :c'),
                {"c": spec["industry_code"]},
            ).scalar()
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
                verb = "Updated"
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
                verb = "Created"

        print(
            f"{verb} {spec['industry_code']} — {count} checkpoints across "
            f"{len(categories)} disciplines."
        )
        for cat in categories:
            statutory = sum(
                1 for cp in cat["checkpoints"]
                if cp.get("requirement_type") == "STATUTORY_REGULATORY"
            )
            print(
                f"  {cat['category_name'][:48]:<50} {len(cat['checkpoints']):>3} checkpoints "
                f"({statutory} statutory / {len(cat['checkpoints']) - statutory} internal)"
            )
        print()


if __name__ == "__main__":
    main()
