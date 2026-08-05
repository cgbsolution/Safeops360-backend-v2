"""Import the AI-drafted clause citations into the checkpoint libraries.

Source: `docs/cams/_report_samples/sheet/clause-mapping-worksheet-DRAFTED.csv`
(127 rows — the gap set measured in docs/cams/11 §B2).

**What these citations are.** Drafted by an AI against general knowledge of
ISO 45001/14001, SA8000, EU GMP Annex 1, 21 CFR 211/Part 11 and Indian statutory
instruments. They are NOT sourced by a compliance professional and are NOT
verified against the cited instruments. Every imported row is therefore stamped
`citation_status = UNVERIFIED_AI_DRAFT`, and the report says so.

**The safety rule this script will not break: it never overwrites an existing
citation.** The 25 already-populated rows were authored in the seed, and a
drafted citation silently replacing a sourced one is the single worst outcome
available here. A row with a non-empty `requirement_reference` is skipped and
counted, not updated — even if the CSV names it.

Idempotent. Dry run by default.

    .venv/Scripts/python.exe scripts/import_clause_citations.py
    .venv/Scripts/python.exe scripts/import_clause_citations.py --commit

WARNING: the backend .env points at PRODUCTION.
"""

from __future__ import annotations

import argparse
import copy
import csv
import io
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.audit_compliance import AuditCheckpointLibrary
from app.services import citations as cit

CSV_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs" / "cams" / "_report_samples" / "sheet"
    / "clause-mapping-worksheet-DRAFTED.csv"
)

# Provenance stamped on every imported row, so a reviewer a year from now can
# tell where the text came from without asking anyone.
SOURCE_TAG = "AI-drafted 2026-07-29 (clause-mapping-worksheet-DRAFTED.csv)"


def load_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        print(f"ERROR: source CSV not found at {path}", file=sys.stderr)
        raise SystemExit(2)
    # utf-8-sig: the worksheet carries a BOM so Excel renders § and — correctly.
    with io.open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def revert(commit: bool) -> int:
    """Undo the import: clear every citation this script wrote.

    Exists because the alternative — an unverified citation that cannot be
    cleanly removed — would make the import a one-way door into a compliance
    library. Only rows stamped `UNVERIFIED_AI_DRAFT` are touched, so a citation
    a human has since verified (`HUMAN_VERIFIED`) survives, as does every
    ORIGINAL row.
    """
    engine = create_engine(get_settings().sync_database_url, future=True)
    cleared = kept_verified = 0
    with Session(engine) as s:
        libs = s.execute(select(AuditCheckpointLibrary)).scalars().all()
        for lib in libs:
            # Deep copy for the same reason as the import path — see the note
            # there. Mutating the loaded list in place makes the reassignment a
            # no-op and the revert would silently do nothing.
            cats = copy.deepcopy(lib.categories or [])
            changed = False
            for cat in cats:
                for cp in cat.get("checkpoints") or []:
                    st = cp.get(cit.KEY_STATUS)
                    if st == cit.HUMAN_VERIFIED:
                        kept_verified += 1
                        continue
                    if st != cit.UNVERIFIED_AI_DRAFT:
                        continue
                    cp["requirement_reference"] = ""
                    for k in (cit.KEY_STATUS, cit.KEY_CONFIDENCE, cit.KEY_PRIORITY,
                              cit.KEY_NOTE, cit.KEY_SOURCE):
                        cp.pop(k, None)
                    cleared += 1
                    changed = True
            if changed and commit:
                lib.categories = list(cats)
        if commit:
            s.commit()
            print(f"REVERTED — cleared {cleared} drafted citation(s); "
                  f"kept {kept_verified} human-verified.")
        else:
            s.rollback()
            print(f"DRY RUN — would clear {cleared} drafted citation(s); "
                  f"would keep {kept_verified} human-verified. Pass --commit.")
    return 0


def main(commit: bool) -> int:
    rows = load_rows(CSV_PATH)
    by_code: dict[str, dict[str, str]] = {}
    dupes: list[str] = []
    for r in rows:
        code = (r.get("checkpointCode") or "").strip()
        if not code:
            continue
        if code in by_code:
            dupes.append(code)
        by_code[code] = r

    print(f"-- source: {CSV_PATH.name}")
    print(f"   {len(rows)} row(s), {len(by_code)} distinct checkpoint code(s)")
    if dupes:
        print(f"   WARNING duplicate codes collapsed: {sorted(set(dupes))}")
    conf = Counter((r.get("confidence") or "").upper() for r in rows)
    print(f"   confidence: HIGH {conf['HIGH']} · MEDIUM {conf['MEDIUM']} · LOW {conf['LOW']}")
    print()

    engine = create_engine(get_settings().sync_database_url, future=True)
    now = datetime.now(timezone.utc).isoformat()

    applied = skipped_existing = not_found_in_lib = 0
    priority_rows: list[str] = []
    matched_codes: set[str] = set()

    with Session(engine) as s:
        libs = s.execute(select(AuditCheckpointLibrary)).scalars().all()
        unmatched: list[str] = []
        # industryCode -> the post-import categories, for the measurement below.
        measured: dict[str, list[dict[str, Any]]] = {}
        print("-- libraries -------------------------------------------------")

        for lib in libs:
            # DEEP COPY before mutating — this is load-bearing, not tidiness.
            #
            # `lib.categories` is a plain `JSON` column with no `Mutable`
            # extension, so SQLAlchemy tracks changes by comparing the attribute
            # against its loaded value. Aliasing the list and mutating in place
            # mutates that loaded value too, so the later reassignment compares
            # EQUAL and nothing is written — while every in-memory read (including
            # this script's own "post-import" measurement) shows the new data and
            # reports success. The first version of this script did exactly that
            # and printed "COMMITTED · 0 gaps, 127 unverified" having written
            # nothing at all.
            cats = copy.deepcopy(lib.categories or [])
            lib_applied = lib_skipped = 0
            changed = False

            for cat in cats:
                for cp in cat.get("checkpoints") or []:
                    code = (cp.get("code") or "").strip()
                    if not code:
                        continue
                    src = by_code.get(code)
                    if src is None:
                        continue
                    matched_codes.add(code)

                    existing = (cp.get("requirement_reference") or "").strip()
                    if existing:
                        # The rule at the top of this file. A sourced citation is
                        # never replaced by a drafted one.
                        lib_skipped += 1
                        skipped_existing += 1
                        continue

                    combined = cit.combined_reference(
                        src.get("draftClauseCitation"), src.get("draftStatutoryReference")
                    )
                    if not combined:
                        continue

                    confidence = (src.get("confidence") or "").upper()
                    cp["requirement_reference"] = combined
                    cp[cit.KEY_STATUS] = cit.UNVERIFIED_AI_DRAFT
                    cp[cit.KEY_CONFIDENCE] = confidence or "UNKNOWN"
                    # LOW confidence is the drafter's own flag that it could not
                    # place the citation. Those four are the first rows a human
                    # should look at, so they carry a distinct priority rather
                    # than sitting in the same bucket as the 78 HIGH rows.
                    cp[cit.KEY_PRIORITY] = (
                        cit.PRIORITY if confidence == "LOW" else cit.NORMAL
                    )
                    cp[cit.KEY_NOTE] = (src.get("sourcingNote") or "").strip()
                    cp[cit.KEY_SOURCE] = SOURCE_TAG
                    if confidence == "LOW":
                        priority_rows.append(code)
                    lib_applied += 1
                    applied += 1
                    changed = True

            # Stamp the pre-existing citations too, so "sourced" is an explicit
            # value rather than the absence of a flag — the distinction the
            # import exists to create has to be readable from both sides.
            for cat in cats:
                for cp in cat.get("checkpoints") or []:
                    ref = (cp.get("requirement_reference") or "").strip()
                    if ref and not cp.get(cit.KEY_STATUS):
                        cp[cit.KEY_STATUS] = cit.ORIGINAL
                        cp[cit.KEY_SOURCE] = "Authored in seed-audit-compliance.ts"
                        changed = True

            summary = cit.summarise(cats)
            measured[lib.industryCode] = cats
            print(
                f"  {lib.industryCode:<24} {summary['total']:>4} cp · "
                f"+{lib_applied:>3} drafted · {lib_skipped:>2} kept sourced · "
                f"{summary['uncited']:>3} still uncited"
            )

            if changed and commit:
                # Safe now: `cats` is a deep copy, so this genuinely differs
                # from the loaded value and the attribute is marked dirty.
                lib.categories = cats

        unmatched = sorted(set(by_code) - matched_codes)
        not_found_in_lib = len(unmatched)

        # ── The gap measurement, re-run ────────────────────────────────
        #
        # Computed from the IN-MEMORY categories, before commit/rollback. On a
        # dry run a rollback expires these objects and they re-read as the
        # pre-import state — which would print "127 gaps, 0 unverified" and read
        # as though the import had changed nothing. Measuring here means the dry
        # run shows what the commit would actually produce.
        g_total = g_cited = g_unver = g_prio = 0
        for code, cats in measured.items():
            # SCALE_DEMO_1500 is a synthetic fixture, excluded from the "real
            # library" figure the same way docs/cams/11 §B2 excluded it.
            if code.startswith("SCALE_DEMO"):
                continue
            sm = cit.summarise(cats)
            g_total += sm["total"]
            g_cited += sm["cited"]
            g_unver += sm["unverified"]
            g_prio += sm["priorityReview"]

        if commit:
            s.commit()
            print("\n  COMMITTED")
        else:
            s.rollback()
            print("\n  DRY RUN — nothing written (pass --commit to apply)")

        label = "post-import" if commit else "PROJECTED (dry run)"
        print(f"\n-- gap measurement, {label} ---------------------------")

        gaps = g_total - g_cited
        print(f"  real library checkpoints      : {g_total}")
        print(f"  with a clause citation        : {g_cited}")
        print(f"  MISSING a citation (gaps)     : {gaps}")
        print(f"  of the cited, UNVERIFIED      : {g_unver}")
        print(f"  flagged for PRIORITY review   : {g_prio}")
        pct = round(g_unver / g_cited * 100, 1) if g_cited else 0.0
        print()
        print(f"  >> {gaps} gaps, {g_unver} unverified "
              f"({pct}% of citations are AI drafts, not sourced fact)")
        print("     Full coverage is NOT full confidence — do not report the gap "
              "count without the unverified count beside it.")

    print("\n-- import summary --------------------------------------------")
    print(f"  applied (blank -> drafted citation) : {applied}")
    print(f"  skipped (already had a citation)    : {skipped_existing}")
    print(f"  CSV codes not found in any library  : {not_found_in_lib}")
    if unmatched:
        print(f"    {unmatched}")
    if priority_rows:
        print(f"  LOW-confidence, priority review     : {sorted(priority_rows)}")

    if not commit:
        return 0

    # ── Prove it landed, from a NEW session ───────────────────────────
    #
    # The first version of this script printed "COMMITTED" and the correct
    # projected totals while writing nothing, because every number it reported
    # came from the same in-memory objects it had mutated. Re-reading through a
    # fresh session is the only check that distinguishes "written" from
    # "believed to be written", so it is not optional.
    with Session(engine) as s2:
        v_total = v_unver = v_prio = v_cited = 0
        for lib in s2.execute(select(AuditCheckpointLibrary)).scalars().all():
            if lib.industryCode.startswith("SCALE_DEMO"):
                continue
            sm = cit.summarise(lib.categories or [])
            v_total += sm["total"]
            v_cited += sm["cited"]
            v_unver += sm["unverified"]
            v_prio += sm["priorityReview"]

    print("\n-- persistence check (fresh session) -------------------------")
    print(f"  cited      : {v_cited}/{v_total}")
    print(f"  unverified : {v_unver}")
    print(f"  priority   : {v_prio}")
    ok = v_unver == applied and v_cited == v_total
    print(f"  {'VERIFIED — the write landed.' if ok else 'MISMATCH — the write did NOT land.'}")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="apply (default is a dry run)")
    ap.add_argument("--revert", action="store_true",
                    help="clear every UNVERIFIED_AI_DRAFT citation this script wrote")
    a = ap.parse_args()
    raise SystemExit(revert(a.commit) if a.revert else main(a.commit))
