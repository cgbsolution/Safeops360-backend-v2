"""Seed CAMS analytics snapshots + run repeat-finding detection (§5.2 / §10.6).

The TypeScript seed (prisma/seed-cams.ts) creates the engagements and findings;
the analytics precompute lives in the Python backend, so this companion script
finishes the Phase-1 seed:

  1. runs the Analytics engine's repeat-finding detection (authoritative — proves
     the 5 South-Works recurrences are *computed*, not just hand-flagged), and
  2. precomputes the FY26-Q3 / FY26-Q4 / FY27-Q1 snapshots so QoQ trend lines and
     the board pack render with movement.

Idempotent: detection recomputes from scratch and snapshots upsert per
(periodLabel + scope). Safe to re-run.

Run from the backend root (AFTER `npx tsx prisma/seed-cams.ts`):
    venv/Scripts/python.exe scripts/seed_cams_snapshots.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

# Ensure the backend root (parent of scripts/) wins on sys.path, so `app`
# resolves to the live source tree rather than any stale editable-install copy.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.services import cams as svc  # noqa: E402

# Fiscal year = Apr–Mar (so FY27-Q1 = Apr–Jun 2026, which contains the seed's NOW).
PERIODS: list[tuple[str, datetime, datetime]] = [
    ("FY26-Q3", datetime(2025, 10, 1, tzinfo=timezone.utc), datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc)),
    ("FY26-Q4", datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 3, 31, 23, 59, 59, tzinfo=timezone.utc)),
    ("FY27-Q1", datetime(2026, 4, 1, tzinfo=timezone.utc), datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc)),
]


async def main() -> None:
    async with AsyncSessionLocal() as db:
        det = await svc.detect_repeat_findings(db)
        await db.commit()
        print(f"  repeat-finding detection: {det['flagged']} flagged of {det['scanned']} scanned (window {det['windowDays']}d)")

        for label, start, end in PERIODS:
            snap = await svc.precompute_snapshot(db, period_label=label, period_start=start, period_end=end)
            await db.commit()
            m = snap.metrics or {}
            prog = m.get("programme", {})
            print(
                f"  snapshot {label}: total={prog.get('total')} "
                f"completion={prog.get('completionRatePct')}% "
                f"repeatRate={m.get('repeatFindingRatePct')}% "
                f"hash={(snap.snapshotHash or '')[:8]}"
            )
    print("CAMS analytics snapshots seeded.")


if __name__ == "__main__":
    asyncio.run(main())
