"""WP-02 - backfill the assessmentStatus desync (F-29). The module's worst defect.

READ THIS BEFORE RUNNING.

The audit-lifecycle v2 migration added `AuditCheckpointResponse.assessmentStatus`
as the first-class verdict column but **never backfilled it**, so rows answered
before the migration still read `NOT_ASSESSED` while their answer sits in
`auditorResponse->>'value'`. Four read paths then disagreed about one fact:

    _compute_score        reads auditorResponse JSON   -> 78.9%
    _discipline_rollup    reads assessmentStatus       -> 0 of 82
    answeredCheckpoints   reads overallStatus
    _is_terminal          reads workflowState + assessmentStatus

That is how `RPT-AUD-GT-2026-NW-0003-*` came to report **78.9% over 0-of-82
assessed with 82 "open items" on a CLOSED audit** - the worst artefact in the
module, and the reason the coverage engine under-reports until this runs.

**Precedence** (docs/cams/04-target.md §9): keep `assessmentStatus` when it is
already set; otherwise derive from `auditorResponse.value`; otherwise fall back
to the legacy `overallStatus`. This script only ever fills NOT_ASSESSED rows -
it never overwrites an existing verdict.

Idempotent. Dry run by default.

    .venv/Scripts/python.exe scripts/backfill_assessment_status.py            # dry run
    .venv/Scripts/python.exe scripts/backfill_assessment_status.py --commit

WARNING: the backend .env points at PRODUCTION.
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.audit_compliance import _ASSESS_STATUS, _norm_value

# Legacy overallStatus -> scoring bucket, for rows with no JSON value at all.
_LEGACY = {
    "answered_pass": "pass",
    "answered_partial": "partial",
    "answered_fail": "fail",
    "answered_na": "na",
}

SELECT_DESYNCED = """
SELECT r.id, r."auditorResponse"->>'value' AS jsonval, r."overallStatus", a."auditNumber"
FROM "AuditCheckpointResponse" r
JOIN "ComplianceAudit" a ON a.id = r."auditId"
WHERE r."assessmentStatus" = 'NOT_ASSESSED'
  AND (r."auditorResponse"->>'value' IS NOT NULL OR r."overallStatus" <> 'not_answered')
"""

# The verification query the migration policy requires: must return 0 after.
VERIFY = """
SELECT count(*) FROM "AuditCheckpointResponse"
WHERE "auditorResponse"->>'value' IS NOT NULL AND "assessmentStatus" = 'NOT_ASSESSED'
"""


def resolve(jsonval: str | None, overall: str | None) -> str | None:
    """Pure: the single precedence rule. Returns an _ASSESS_STATUS value or None."""
    bucket = _norm_value(jsonval)
    if bucket is None:
        bucket = _LEGACY.get(overall or "")
    return _ASSESS_STATUS.get(bucket) if bucket else None


def main(commit: bool) -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        rows = s.execute(text(SELECT_DESYNCED)).all()
        print(f"-- {len(rows)} desynced row(s) --------------------------")

        by_audit: dict[str, dict[str, int]] = {}
        unresolvable: list[tuple[str, str | None, str | None]] = []
        updates: list[tuple[str, str]] = []

        for rid, jsonval, overall, code in rows:
            status = resolve(jsonval, overall)
            if status is None:
                unresolvable.append((code, jsonval, overall))
                continue
            updates.append((rid, status))
            by_audit.setdefault(code, {}).setdefault(status, 0)
            by_audit[code][status] += 1

        for code in sorted(by_audit):
            counts = ", ".join(f"{k}={v}" for k, v in sorted(by_audit[code].items()))
            print(f"   {code:<24} {counts}")

        if unresolvable:
            # Reported, never guessed. A row whose answer cannot be derived stays
            # NOT_ASSESSED, which is at least honest.
            print(f"\n   {len(unresolvable)} row(s) carry no derivable verdict and are LEFT ALONE:")
            for code, j, o in unresolvable[:5]:
                print(f"     {code}: value={j!r} overallStatus={o!r}")

        if commit and updates:
            for rid, status in updates:
                s.execute(
                    text(
                        'UPDATE "AuditCheckpointResponse" SET "assessmentStatus"=:st WHERE id=:id'
                    ),
                    {"st": status, "id": rid},
                )
            # Re-derive the audit-level answered counter from the column that is
            # now authoritative, so the header and the rollup agree.
            s.execute(
                text(
                    '''
                    UPDATE "ComplianceAudit" a SET "answeredCheckpoints" = sub.n
                    FROM (
                        SELECT "auditId", count(*) AS n FROM "AuditCheckpointResponse"
                        WHERE "assessmentStatus" <> 'NOT_ASSESSED' GROUP BY "auditId"
                    ) sub
                    WHERE a.id = sub."auditId"
                    '''
                )
            )
            s.commit()
            print(f"\n   {len(updates)} row(s) updated; answeredCheckpoints re-derived.")

        remaining = s.execute(text(VERIFY)).scalar_one()
        print(f"\n-- verification (must be 0) -------------------------")
        print(f"   {remaining}  rows answered in JSON but still NOT_ASSESSED")

    print("\nCOMMITTED." if commit else "\nDRY RUN - nothing written. Re-run with --commit.")
    return 0 if (not commit or remaining == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main("--commit" in sys.argv))
