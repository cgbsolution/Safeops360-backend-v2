#!/usr/bin/env python
"""Is it safe to turn off legacy QR scanning yet?

    python scripts/fire_qr_reprint_status.py
    python scripts/fire_qr_reprint_status.py --by-location

THE QUESTION THIS ANSWERS
-------------------------
Every sticker currently on a cylinder encodes the old derived value
(`safeops:fire-asset:<asset id>`). Setting `FIRE_QR_LEGACY_SCAN=0` makes those
stop resolving. Do it before the reprint pass is finished and the fire estate
goes unscannable — silently, and discovered by an inspector standing in front of
a cylinder rather than by anyone watching a dashboard.

So the cutover is gated on a count this reports: how many assets carry a token
whose label has actually been produced. Exit code 1 means NOT READY.

WHAT IT CANNOT TELL YOU
-----------------------
`qrLabelPrintedAt` records that a label was PRINTED, which is the only moment
the system can observe. It cannot know the label was applied to the right
cylinder, or applied at all. Treat a clean report as "the paperwork is done, go
confirm the walk happened" — not as authorisation on its own. The physical pass
has its own lead time, which is why it starts before the code ships.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.models.fire_safety import FireEquipment  # noqa: E402
from app.models.plant import Plant  # noqa: E402
from app.services import fire_qr as qrsvc  # noqa: E402


def rule(title: str) -> None:
    print(f"\n{title}\n" + "─" * max(len(title), 66))


async def main(by_location: bool) -> int:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(FireEquipment)
                .where(FireEquipment.isDeleted.is_(False))
                .order_by(FireEquipment.plantId, FireEquipment.location, FireEquipment.equipmentCode)
            )
        ).scalars().all()
        plants = {p.id: p for p in (await db.execute(select(Plant))).scalars().all()}

    total = len(rows)
    no_token = [e for e in rows if not e.qrToken]
    unprinted = [e for e in rows if e.qrToken and not e.qrLabelPrintedAt]
    ready = [e for e in rows if e.qrToken and e.qrLabelPrintedAt]
    rotated = [e for e in rows if (e.qrTokenRotations or 0) > 0]

    rule("FIRE QR REPRINT STATUS")
    print(f"  legacy scanning        {'ON — old stickers still resolve' if qrsvc.legacy_scan_enabled() else 'OFF — token only'}")
    print(f"  assets in the register {total}")
    print(f"  no token minted yet    {len(no_token)}")
    print(f"  token, not yet printed {len(unprinted)}")
    print(f"  printed and ready      {len(ready)}")
    print(f"  reissued at least once {len(rotated)}")

    if no_token:
        rule("NO TOKEN — cannot be printed at all")
        print("  Run: python scripts/fire_qr_backfill.py --commit")
        for e in no_token[:20]:
            print(f"    {e.equipmentCode:<22} {e.location or '—'}")
        if len(no_token) > 20:
            print(f"    … and {len(no_token) - 20} more")

    if unprinted:
        rule("AWAITING REPRINT — these go unscannable at cutover")
        for e in unprinted[:30]:
            why = "reissued" if (e.qrTokenRotations or 0) > 0 else "new token"
            print(f"    {e.equipmentCode:<22} {(e.location or '—')[:38]:<38} ({why})")
        if len(unprinted) > 30:
            print(f"    … and {len(unprinted) - 30} more")
        print("\n  Print a sheet for exactly these:")
        print("    GET /api/fire/assets/qr-sheet.pdf?ids=" + ",".join(e.id for e in unprinted[:8])
              + ("…" if len(unprinted) > 8 else ""))

    if by_location:
        rule("BY LOCATION — the order someone walks the site in")
        buckets: dict[tuple[str, str], list[FireEquipment]] = {}
        for e in rows:
            plant = plants.get(e.plantId)
            buckets.setdefault(((plant.name if plant else e.plantId), e.location or "—"), []).append(e)
        for (plant_name, location), items in sorted(buckets.items()):
            done = sum(1 for e in items if e.qrToken and e.qrLabelPrintedAt)
            flag = "" if done == len(items) else "   ← outstanding"
            print(f"  {plant_name[:22]:<22} {location[:34]:<34} {done}/{len(items)}{flag}")

    rule("VERDICT")
    blocked = len(no_token) + len(unprinted)
    if blocked == 0 and total:
        print("  Every asset carries a printed token.")
        print("  Confirm with whoever ran the physical replacement that the labels are")
        print("  actually ON the cylinders — this report can only see that they were")
        print("  printed. Then set FIRE_QR_LEGACY_SCAN=0 and restart the API.")
        return 0
    if not total:
        print("  No fire assets registered — nothing to reprint.")
        return 0
    print(f"  NOT READY: {blocked} of {total} asset(s) would stop scanning at cutover.")
    print("  Leave FIRE_QR_LEGACY_SCAN on (the default) until this reaches zero.")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--by-location", action="store_true",
                    help="group by plant and location, for planning the physical walk")
    raise SystemExit(asyncio.run(main(ap.parse_args().by_location)))
