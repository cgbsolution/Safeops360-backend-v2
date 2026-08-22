"""Soft-delete audits that are duplicates of one another, keeping the first.

Why this exists
---------------
A POST that times out at the proxy is not a POST that failed. Before the retry
guard in `create_audit`, the backend kept working after the 25/30s ceiling,
committed the audit, and the scheduler was shown "Couldn't schedule audit" for
an audit that now existed. Clicking again is the only reasonable response to
that message, so one intent produced AUD-PI-2026-NW-0050, -0051 and -0052:
three identical 206-checkpoint audits at the same site on the same date.

The guard stops new ones. This clears the ones already made.

What counts as a duplicate
--------------------------
The same four fields that make an audit that audit — title, plant, scheduled
date, lead auditor — created within `WINDOW_MINUTES` of each other. Within a
group the EARLIEST by createdAt is kept and the rest are soft-deleted; the
first one is the one whose emails and calendar invites actually went out.

Refuses to touch a group where any member has been worked on. An audit that is
no longer `scheduled`, or that has a single answered checkpoint, is not a
duplicate anyone can safely delete — it is a record with evidence in it, and
deciding which of two part-conducted audits survives is not a script's call.

Soft delete only. `ComplianceAudit` is a governed entity: `app.core.soft_delete`
blocks hard deletion outright, and the row stays restorable within the restore
window. Nothing is destroyed.

    python scripts/soft_delete_duplicate_audits.py                    # dry run
    python scripts/soft_delete_duplicate_audits.py --commit
    python scripts/soft_delete_duplicate_audits.py --numbers AUD-PI-2026-NW-0051,AUD-PI-2026-NW-0052 --commit

`--numbers` deletes exactly those audit numbers and nothing else, still subject
to the untouched check. Use it when you have already decided which to remove.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.soft_delete import soft_delete  # noqa: E402
from app.models.audit_compliance import (  # noqa: E402
    AuditCheckpointResponse,
    ComplianceAudit,
)

# Two audits created further apart than this are two decisions, not one retry.
WINDOW_MINUTES = 30

REASON = (
    "Duplicate audit created by a retry after the schedule request timed out at "
    "the proxy while the backend had already committed it. The earliest audit in "
    "the group is retained; see scripts/soft_delete_duplicate_audits.py."
)


def _untouched(session: Session, audit: ComplianceAudit) -> tuple[bool, str]:
    """(safe_to_delete, why_not). Conservative on purpose."""
    if audit.status != "scheduled":
        return False, f"status is '{audit.status}', not 'scheduled'"
    answered = session.execute(
        select(AuditCheckpointResponse.id)
        .where(
            AuditCheckpointResponse.auditId == audit.id,
            AuditCheckpointResponse.overallStatus != "not_answered",
        )
        .limit(1)
    ).first()
    if answered is not None:
        return False, "has at least one answered checkpoint"
    return True, ""


def _groups(audits: list[ComplianceAudit]) -> list[list[ComplianceAudit]]:
    """Group by (title, plant, scheduled date, lead), split on the time window."""
    buckets: dict[tuple, list[ComplianceAudit]] = defaultdict(list)
    for a in audits:
        buckets[(a.title, a.plantId, a.scheduledDate, a.leadAuditorUserId)].append(a)

    out: list[list[ComplianceAudit]] = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda a: a.createdAt)
        run = [members[0]]
        for a in members[1:]:
            gap = (a.createdAt - run[-1].createdAt).total_seconds() / 60
            if gap <= WINDOW_MINUTES:
                run.append(a)
            else:
                if len(run) > 1:
                    out.append(run)
                run = [a]
        if len(run) > 1:
            out.append(run)
    return out


def main(commit: bool, only_numbers: set[str] | None) -> int:
    engine = create_engine(get_settings().sync_database_url, pool_pre_ping=True, future=True)
    with Session(engine) as session:
        live = list(
            session.execute(
                select(ComplianceAudit)
                .where(ComplianceAudit.isDeleted.is_(False))
                .order_by(ComplianceAudit.createdAt)
            ).scalars()
        )

        if only_numbers:
            targets = [a for a in live if a.auditNumber in only_numbers]
            missing = only_numbers - {a.auditNumber for a in targets}
            if missing:
                print(f"!! not found (or already deleted): {', '.join(sorted(missing))}")
                return 1
            plan = [(None, targets)]
        else:
            plan = [(g[0], g[1:]) for g in _groups(live)]

        if not plan or all(not victims for _, victims in plan):
            print("No duplicate audits found. Nothing to do.")
            return 0

        deleted = skipped = 0
        for keep, victims in plan:
            if keep is not None:
                print(f"\nGroup: “{keep.title}” @ {keep.scheduledDate:%d %b %Y}")
                print(f"   KEEP    {keep.auditNumber}  created {keep.createdAt:%Y-%m-%d %H:%M:%S}")
            for a in victims:
                safe, why = _untouched(session, a)
                if not safe:
                    print(f"   SKIP    {a.auditNumber}  — {why}")
                    skipped += 1
                    continue
                print(
                    f"   DELETE  {a.auditNumber}  created {a.createdAt:%Y-%m-%d %H:%M:%S}"
                    f"  ({a.totalCheckpoints} checkpoints)"
                )
                if commit:
                    soft_delete(a, actor_id=None, reason=REASON)
                deleted += 1

        print(
            f"\n{deleted} audit(s) {'soft-deleted' if commit else 'would be soft-deleted'}"
            f"{f', {skipped} skipped as not-untouched' if skipped else ''}."
        )
        if commit:
            session.commit()
            print("Committed. Restorable via app.core.soft_delete.restore().")
        else:
            print("Dry run — nothing written. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="apply; otherwise dry run")
    ap.add_argument(
        "--numbers",
        default="",
        help="comma-separated audit numbers to delete instead of auto-grouping",
    )
    args = ap.parse_args()
    nums = {n.strip() for n in args.numbers.split(",") if n.strip()} or None
    raise SystemExit(main(args.commit, nums))
