#!/usr/bin/env python
"""Mint an opaque QR token for every fire asset that has none.

    python scripts/fire_qr_backfill.py            # dry run
    python scripts/fire_qr_backfill.py --commit

Run this immediately after prisma/apply-fire-qr-token-ddl.ts. Until it has run,
every label-producing endpoint refuses (409) rather than printing a sticker that
encodes nothing.

NEVER re-mints an asset that already has a token. A second run that rotated
everything would silently invalidate labels printed after the first run — which
is the one thing a backfill must not do. Reissuing a single asset is
`POST /api/fire/assets/{id}/qr-token/rotate`, deliberate and one at a time.

This does NOT touch `qrCode`, the legacy derived value. That string is what the
stickers currently on the cylinders encode, and resolution still honours it
until the reprint pass finishes and FIRE_QR_LEGACY_SCAN is turned off.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.services import fire_qr as qrsvc  # noqa: E402


async def main(commit: bool) -> int:
    async with AsyncSessionLocal() as db:
        result = await qrsvc.backfill_tokens(db, dry_run=not commit)
        minted = result["minted"]
        print(f"assets              {result['total']}")
        print(f"already had a token {result['alreadyHadToken']}")
        print(f"to mint             {len(minted)}")
        for code in minted[:20]:
            print(f"  + {code}")
        if len(minted) > 20:
            print(f"  … and {len(minted) - 20} more")

        if commit:
            await db.commit()
            print("\nCommitted.")
            if minted:
                print(
                    "Every minted asset now has a token that has NEVER been printed, so its\n"
                    "field sticker is still the old derived one. Next:\n"
                    "  1. GET /api/fire/assets/qr-sheet.pdf  (marks them printed)\n"
                    "  2. apply the new labels\n"
                    "  3. python scripts/fire_qr_reprint_status.py\n"
                    "  4. only then set FIRE_QR_LEGACY_SCAN=0"
                )
        else:
            print("\nDry run — nothing written. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="apply; otherwise dry run")
    raise SystemExit(asyncio.run(main(ap.parse_args().commit)))
