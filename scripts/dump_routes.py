"""Dump the FastAPI route table as JSON.

Feeds `safeopsapp/scripts/audit-api-contract.ts`, which cross-checks every
endpoint the mobile registry can call against what the backend actually serves.
The mobile app is config-driven, so a renamed route shows up as an empty screen
rather than an error — this pair turns that into a build-time failure.

    venv/Scripts/python scripts/dump_routes.py routes.json
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import create_app  # noqa: E402


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "routes.json"
    app = create_app()
    rows = [
        {"path": r.path, "methods": sorted(getattr(r, "methods", []) or [])}
        for r in app.routes
    ]
    rows.sort(key=lambda x: x["path"])
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=0)
    print(f"routes dumped: {len(rows)} -> {out}")


if __name__ == "__main__":
    main()
