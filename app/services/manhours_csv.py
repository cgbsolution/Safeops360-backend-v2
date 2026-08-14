"""CSV bulk import for Manhours category rows.

Port of `lib/manhours/csv.ts`. Python has `csv` in the stdlib, so the
hand-rolled tokeniser the TypeScript needed is unnecessary — `csv.reader`
already handles CRLF, quoted fields with embedded commas and newlines, and
doubled quotes.

Two behaviours are deliberately preserved from the original:

  * **Per-row errors accumulate, they don't abort.** A payroll export with one
    bad cell should still import the other 40 rows and tell you which one to
    fix, rather than rejecting the file wholesale.
  * **Import REPLACES all rows of that category type**, it does not merge.
    Re-importing is the intended way to correct a mistake, and merging would
    silently double every headcount on the second attempt.
"""

from __future__ import annotations

import csv as _csv
import io
from typing import Any, Literal

CategoryKind = Literal["PERMANENT", "CONTRACT", "TRAINEE"]

# PERMANENT/TRAINEE rows are keyed by department; CONTRACT rows by contractor.
KEY_HEADER: dict[str, tuple[str, str]] = {
    "PERMANENT": ("departmentCode", "department code"),
    "TRAINEE": ("departmentCode", "department code"),
    "CONTRACT": ("contractorCode", "contractor code"),
}

NUMERIC_HEADERS = (
    "averageHeadcount",
    "peakHeadcount",
    "endOfPeriodHeadcount",
    "regularHours",
    "overtimeHours",
)


def generate_template(kind: CategoryKind, codes: list[str] | None = None) -> str:
    """The downloadable template. Pre-filled with the plant's real codes so a
    user doesn't have to go and look them up."""
    key_header = KEY_HEADER[kind][0]
    out = io.StringIO()
    writer = _csv.writer(out, lineterminator="\n")
    writer.writerow([key_header, *NUMERIC_HEADERS, "notes"])
    for code in codes or []:
        writer.writerow([code, 0, 0, 0, 0, 0, ""])
    return out.getvalue()


def parse_category_csv(text: str, kind: CategoryKind) -> dict[str, Any]:
    """Parse into row dicts plus a list of per-row errors.

    Row numbers in errors are 1-indexed with the header as row 1, matching
    what the user sees in a spreadsheet.
    """
    errors: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    # Excel writes a UTF-8 BOM; left in place it corrupts the first header.
    stripped = text.lstrip("﻿")
    records = [r for r in _csv.reader(io.StringIO(stripped))]
    if not records:
        return {"rows": [], "errors": [{"row": 1, "message": "Empty CSV"}]}

    header = [h.strip() for h in records[0]]
    expected_key, key_label = KEY_HEADER[kind]
    if not header or header[0] != expected_key:
        got = header[0] if header else ""
        return {
            "rows": [],
            "errors": [
                {"row": 1, "message": f'First column must be "{expected_key}" (got "{got}")'}
            ],
        }

    missing = [h for h in NUMERIC_HEADERS if h not in header]
    if missing:
        return {
            "rows": [],
            "errors": [
                {"row": 1, "message": f'Missing required column "{h}"'} for h in missing
            ],
        }

    numeric_idx = [(h, header.index(h)) for h in NUMERIC_HEADERS]
    notes_idx = header.index("notes") if "notes" in header else -1

    for r, fields in enumerate(records[1:], start=2):
        # Excel appends trailing newlines; a wholly blank line is not an error.
        if all((f or "").strip() == "" for f in fields):
            continue

        key = (fields[0] if fields else "").strip()
        if not key:
            errors.append({"row": r, "message": f"Row missing {key_label}"})
            continue

        numbers: dict[str, float] = {}
        ok = True
        for col, idx in numeric_idx:
            raw = (fields[idx] if idx < len(fields) else "").strip()
            if raw == "":
                numbers[col] = 0.0
                continue
            try:
                value = float(raw)
            except ValueError:
                errors.append({"row": r, "message": f'Invalid number for "{col}": {raw}'})
                ok = False
                break
            # Negative headcounts and hours are always data errors, never a
            # legitimate correction — a reversal belongs in the deductions.
            if value < 0 or value != value or value in (float("inf"), float("-inf")):
                errors.append({"row": r, "message": f'Invalid number for "{col}": {raw}'})
                ok = False
                break
            numbers[col] = value
        if not ok:
            continue

        note = ""
        if notes_idx >= 0 and notes_idx < len(fields):
            note = (fields[notes_idx] or "").strip()

        rows.append(
            {
                "key": key,
                "averageHeadcount": int(numbers["averageHeadcount"]),
                "peakHeadcount": int(numbers["peakHeadcount"]),
                "endOfPeriodHeadcount": int(numbers["endOfPeriodHeadcount"]),
                "regularHours": numbers["regularHours"],
                "overtimeHours": numbers["overtimeHours"],
                "notes": note or None,
            }
        )

    return {"rows": rows, "errors": errors}
