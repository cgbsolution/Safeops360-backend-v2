"""Seed the two checkpoint libraries behind the new audit categories.

The scheduler now picks an AUDIT CATEGORY first, and the category resolves the
checklist the disciplines come from:

  INTERNAL           -> PAGE_INDUSTRIES  (HR / EHS / Production)  — already seeded
                        by seed_page_industries_library.py
  MANAGEMENT_SYSTEMS -> PAGE_IMS         (HR / Admin / OHC)         — this script
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

Every checkpoint is `response_type = page_grading`, so all three categories
share one conduct screen, one scoring rollup and one report shape.

PAGE_IMS is the one category that also differs in HOW it is answered, and both
differences are declared on its categories rather than coded anywhere:

  • `segregation: DEPARTMENT` — a category here is HR / Admin / OHC, not a
    management-system standard. Page conduct one audit per department and
    assess each against both source sheets.
  • `conformance_mode: TRISTATE` — Conformance / Non-Conformance / Observation,
    the header of column E on both sheets, in place of the seven-value status
    ladder. It resolves to the same grade + status underneath.

Each checkpoint also carries `stream` (IMS | ENMS — which of the two reports it
belongs to), `replication_key` (the same line in another department) and
`pair_key` (the same requirement on the other sheet). All three are read by the
runtime; none of them is inferred from the code.

Idempotent: upserts by industryCode. Audits already materialised from either
library are untouched — they hold their own snapshot rows.

Run from the backend root:
    python scripts/seed_page_audit_category_libraries.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
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
        "industry_name": "Page Industries — QMS, EMS, OHS by department",
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

        axis = (
            "departments"
            if any((c.get("segregation") or "").upper() == "DEPARTMENT" for c in categories)
            else "disciplines"
        )
        print(
            f"{verb} {spec['industry_code']} — {count} checkpoints across "
            f"{len(categories)} {axis}."
        )
        for cat in categories:
            cps = cat["checkpoints"]
            statutory = sum(
                1 for cp in cps if cp.get("requirement_type") == "STATUTORY_REGULATORY"
            )
            # The stream split is what the two reports are cut on, so a
            # department that silently lost one of them has to be visible here
            # rather than at the moment a report comes out empty.
            streams = ""
            if any(cp.get("stream") for cp in cps):
                by_stream = Counter(cp.get("stream") for cp in cps if cp.get("stream"))
                pairs = len({cp["pair_key"] for cp in cps if cp.get("pair_key")})
                streams = ("  [" + ", ".join(f"{k} {v}" for k, v in sorted(by_stream.items()))
                           + f", {pairs} paired]")
            print(
                f"  {cat['category_name'][:40]:<42} {len(cps):>3} checkpoints "
                f"({statutory} statutory / {len(cps) - statutory} internal){streams}"
            )
        print()


if __name__ == "__main__":
    main()
