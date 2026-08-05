"""15-minute in-process TTL cache for insight responses (spec §1.1).

Insight computation scans and scores a module's record set, which is wasted work
on every keystroke in a list screen's search box. The frontend loader caches
nothing (server components re-run per navigation), so the TTL lives here, keyed
by module + plant + a hash of the filter args.

Deliberately in-process (single-worker dev/demo backend) — no Redis dependency
and no network, keeping the airgap guarantee. A restart clears it, which is
fine: these are derived, recomputable views.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

_TTL_SECONDS = 15 * 60
_store: dict[str, tuple[float, Any]] = {}


def make_key(module: str, plant: str | None, filters: dict[str, Any]) -> str:
    payload = json.dumps({"m": module, "p": plant, "f": filters}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def get(key: str) -> Any | None:
    hit = _store.get(key)
    if hit is None:
        return None
    expiry, value = hit
    if time.time() >= expiry:
        _store.pop(key, None)
        return None
    return value


def put(key: str, value: Any) -> None:
    _store[key] = (time.time() + _TTL_SECONDS, value)


def clear() -> None:
    """Test hook / manual flush."""
    _store.clear()
