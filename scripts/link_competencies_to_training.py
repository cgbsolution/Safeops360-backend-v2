"""Wire safety competencies to the training programs that build them.

The competency library shipped with `relatedTrainingProgramIds` empty, so the
training receiver had nothing to act on. This sets that field (FK-by-value to
TrainingProgram.code) for the unambiguous safety-critical competencies, so
"training feeds competency" actually flows. Conservative — only clear 1:1
matches; competencies with no corresponding course are left empty (they're
validated by assessment / endorsement, not training).

Idempotent. Prints what it linked.

    .venv/Scripts/python.exe scripts/link_competencies_to_training.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text

from app.core.config import get_settings

# competency.code -> [TrainingProgram.code, ...]
MAPPING: dict[str, list[str]] = {
    "CRANE-OP-MOBILE": ["CRANE_OPERATOR"],
    "CRANE-OP-OVERHEAD": ["CRANE_OPERATOR"],
    "CS-ATTENDANT": ["CONFINED_SPACE_STANDBY"],
    "CS-ENTRANT-L1": ["PTW_CONFINED_SPACE_HOLDER"],
    "CS-ENTRANT-L2": ["PTW_CONFINED_SPACE_HOLDER"],
    "ELEC-AUTH-HT": ["ELECTRICAL_HT"],
    "ELEC-AUTH-LV": ["PTW_ELECTRICAL_HOLDER"],
    "EMERGENCY-RESPONSE-MGT": ["EMERGENCY_RESPONSE"],
    "FIRE-WARDEN": ["FIRE_SAFETY"],
    "FIRST-AIDER": ["FIRST_AID"],
    "FORKLIFT-OP": ["FORKLIFT_OPERATOR"],
}


def main() -> int:
    eng = create_engine(get_settings().sync_database_url, future=True)
    with eng.begin() as c:
        # Validate the target program codes exist.
        all_codes = sorted({code for codes in MAPPING.values() for code in codes})
        present = set(
            c.execute(
                text('SELECT code FROM "TrainingProgram" WHERE code = ANY(:codes)'),
                {"codes": all_codes},
            ).scalars().all()
        )
        missing = [c2 for c2 in all_codes if c2 not in present]
        if missing:
            print(f"!! these program codes do not exist, skipping them: {missing}")

        linked = 0
        for comp_code, prog_codes in MAPPING.items():
            valid = [p for p in prog_codes if p in present]
            if not valid:
                continue
            res = c.execute(
                text(
                    'UPDATE "Competency" SET "relatedTrainingProgramIds" = :ids '
                    'WHERE code = :cc'
                ),
                {"ids": valid, "cc": comp_code},
            )
            if res.rowcount:
                linked += 1
                print(f"  {comp_code:<24} -> {valid}")
            else:
                print(f"  {comp_code:<24} (competency not found, skipped)")

        print(f"\nLinked {linked} competencies to their training programs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
