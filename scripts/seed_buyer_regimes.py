"""WP-53 - seed the buyer-regime template skeletons (Q7 yes, Q19 self-design).

**Read this before running - it deliberately does LESS than it could.**

Q7 asked for SMETA / amfori BSCI / WRAP / Higg FEM / SLCP to be seeded, and Q19
said to self-design the structures. So this seeds the STRUCTURE of each regime:
its sections, its severity taxonomy, its result scale and its scoring style,
all authored by SafeOps360 and labelled as such.

**It does NOT invent checkpoint question text.** Writing plausible-sounding
questions and shipping them under a regime's name would produce something a
customer could mistake for the licensed measurement criteria - and an auditor
conducting a real SMETA audit against invented questions would be doing
something worse than useless. Each seeded library therefore arrives with its
sections in place and **zero checkpoints**, plus an explicit instruction to load
the licensed content.

That is the honest reading of "self-design the structure": the shape is ours to
build, the criteria are the regime owner's to license.

Idempotent. Dry run by default.

    .venv/Scripts/python.exe scripts/seed_buyer_regimes.py            # dry run
    .venv/Scripts/python.exe scripts/seed_buyer_regimes.py --commit

WARNING: the backend .env points at PRODUCTION.
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.audit_compliance import AuditCheckpointLibrary
from app.services.regimes import AUTHORSHIP_DISCLAIMER, REGIMES

# Colour per regime so the discipline chips are distinguishable at a glance.
COLOURS = ["#7C3AED", "#0891B2", "#DC2626", "#059669", "#D97706",
           "#4F46E5", "#DB2777", "#65A30D", "#0EA5E9", "#EA580C",
           "#14B8A6", "#F59E0B"]


def build_categories(spec) -> list[dict]:
    """Sections -> library categories, with NO invented checkpoints.

    `checkpoints: []` is the point. The category structure is the deliverable;
    the questions inside it are licensed content the customer supplies.
    """
    out = []
    for i, section in enumerate(spec.sections):
        code = "".join(ch for ch in section.upper() if ch.isalnum() or ch == " ").replace(" ", "-")
        out.append(
            {
                "category_code": f"{spec.code[:4]}-{code[:24]}",
                "category_name": section,
                "category_color": COLOURS[i % len(COLOURS)],
                "category_icon": "shield",
                "sequence": i + 1,
                "checkpoints": [],
                # Carried per-category so the template builder can render the
                # right vocabulary without re-deriving it from the regime code.
                "regimeCode": spec.code,
            }
        )
    return out


def main(commit: bool) -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    created = updated = 0

    with Session(engine) as s:
        print("-- buyer-regime skeletons ---------------------------")
        for spec in REGIMES.values():
            industry_code = f"REGIME_{spec.code}"
            cats = build_categories(spec)
            existing = s.execute(
                select(AuditCheckpointLibrary).where(
                    AuditCheckpointLibrary.industryCode == industry_code
                )
            ).scalars().first()

            label = f"{spec.name}"
            if existing is not None:
                print(f"   ~ {industry_code:<28} exists ({len(existing.categories or [])} sections)")
                updated += 1
                if commit:
                    existing.industryName = label
                    existing.categories = cats
                    # checkpointCount stays 0 until licensed content is loaded -
                    # it must never imply the regime is ready to run.
                    existing.checkpointCount = 0
                    # INACTIVE until licensed content is loaded. `isActive` is
                    # the Schedule picker's only visibility gate, and offering a
                    # 0-checkpoint library there produces "the selected scope
                    # produced no checkpoints" on submit - a dead end dressed up
                    # as a feature. The skeleton is still visible under Templates.
                    existing.isActive = False
                continue

            print(f"   + {industry_code:<28} {len(cats)} sections, 0 checkpoints "
                  f"({spec.scoringStyle.lower()})")
            created += 1
            if commit:
                s.add(
                    AuditCheckpointLibrary(
                        industryCode=industry_code,
                        industryName=label,
                        version="2026.1-skeleton",
                        categories=cats,
                        checkpointCount=0,
                        # See above: inactive until content is loaded.
                        isActive=False,
                    )
                )

        if commit:
            s.commit()

        print(f"\n   {created} created, {updated} refreshed")
        print("\n-- what was NOT seeded ------------------------------")
        print("   Checkpoint question text. Every library above has 0 checkpoints.")
        print("   Invented questions shipped under a regime's name would be")
        print("   mistakable for its licensed criteria, and auditing against them")
        print("   would be worse than not auditing at all.")
        print("\n   Each skeleton is INACTIVE, so it does not appear in the Schedule")
        print("   picker (where a 0-checkpoint library would dead-end on submit).")
        print("   Load licensed content via Templates > Import library, then activate.")
        print(f"\n   Disclaimer stamped on every regime: {AUTHORSHIP_DISCLAIMER[:72]}...")

        # Verification: a skeleton must never look ready to run.
        print("\n-- verification -------------------------------------")
        rows = s.execute(
            select(
                AuditCheckpointLibrary.industryCode,
                AuditCheckpointLibrary.checkpointCount,
            ).where(AuditCheckpointLibrary.industryCode.like("REGIME_%"))
        ).all()
        bad = [c for c, n in rows if n]
        print(f"   {len(rows)} regime skeleton(s) present")
        print(f"   {len(bad)} with a non-zero checkpoint count (must be 0 until content is loaded)")

    print("\nCOMMITTED." if commit else "\nDRY RUN - nothing written. Re-run with --commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--commit" in sys.argv))
