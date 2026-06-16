"""End-to-end check that the live backend serves the HIRA export endpoints
(after the HiraEntryHazard.hazard relationship fix). Mints a real token for
the study's team leader and calls the running server on :8000.

    .venv/Scripts/python.exe scripts/verify_hira_export.py <entryId>
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import create_access_token
from app.models.hira import HiraEntry, HiraStudy

BASE = "http://127.0.0.1:8000"
ENTRY_ID = sys.argv[1] if len(sys.argv) > 1 else "cmpwjajto0005ja27eellq3t0"


def call(path: str, token: str) -> tuple[int, str]:
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(400).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, repr(e)


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        entry = s.get(HiraEntry, ENTRY_ID)
        if entry is None:
            print(f"!! entry {ENTRY_ID} not found")
            return 1
        study = s.get(HiraStudy, entry.studyId)
        token = create_access_token(study.teamLeaderId)
        print(f"study={study.id} ({study.number})  user={study.teamLeaderId}\n")

    checks = [
        ("GET entry      ", f"/api/hira/entries/{ENTRY_ID}"),
        ("GET export.csv ", f"/api/hira/studies/{study.id}/export.csv"),
        ("GET detail     ", f"/api/hira/studies/{study.id}/detail"),
    ]
    worst = 0
    for label, path in checks:
        code, body = call(path, token)
        ok = code == 200
        worst = max(worst, 0 if ok else 1)
        snippet = body[:140].replace(chr(10), " ").encode("ascii", "replace").decode("ascii")
        print(f"{label} {path}")
        print(f"   -> {code} {'OK' if ok else 'FAIL'}  {snippet}\n")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
