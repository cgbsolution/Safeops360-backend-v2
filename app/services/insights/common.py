"""Shared helpers for the deterministic insight rules.

Confidence gating, keyword tokenisation (for clustering / fuzzy grouping), and
the naive-datetime handling the rest of the read path uses (Prisma writes most
date columns tz-naive; comparisons stay naive throughout, mirroring
app/routers/dashboard.py)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# Bar suppression floor — below this many records in scope, show nothing rather
# than a low-confidence card on thin data (spec §1.4 / acceptance §8).
MIN_RECORDS = 5

# A finding built on this many supporting records or more is not "early".
_MEDIUM_FLOOR = 5
_HIGH_FLOOR = 15

_TOKEN_RE = re.compile(r"[a-z][a-z0-9]{2,}")

# Words that carry no clustering signal — dropped before keyword grouping so a
# cluster is grounded in a real shared term, not "the" / "and" / "was".
_STOPWORDS = frozenset(
    """
    the and for with was were had has have that this from onto into out off due
    not but are you your they them their our its his her him she who whom which
    when where what while during after before near over under above below then
    than been being does did done doing will would could should shall might may
    can cant cannot dont didnt wasnt werent isnt arent about above across also
    all any both each few more most other some such only own same very just too
    incident incidents nearmiss near miss report reported occurred happened
    worker operator employee area plant site unit line shift day night morning
    days week weeks month months year years hour hours minute minutes second
    seconds lost time times ago approx approximately around later duration
    """.split()
)


def now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_naive(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.replace(tzinfo=None) if d.tzinfo is not None else d


def age_days(d: datetime | None, *, ref: datetime | None = None) -> int | None:
    """Whole days between `d` and `ref` (default now). None-safe."""
    dd = as_naive(d)
    if dd is None:
        return None
    return (as_naive(ref) or now_naive()).__sub__(dd).days


def confidence_for(supporting: int) -> str:
    """Confidence is a function of sample size, never vibes (spec §1.1)."""
    if supporting >= _HIGH_FLOOR:
        return "high"
    if supporting >= _MEDIUM_FLOOR:
        return "medium"
    return "low"


def keywords(*values: Any) -> list[str]:
    """Significant, de-stopworded tokens from free-text / array cause fields,
    preserving first-seen order. Used for keyword clustering."""
    seen: dict[str, None] = {}
    for v in values:
        if not v:
            continue
        items = v if isinstance(v, (list, tuple, set)) else [v]
        for item in items:
            if not item:
                continue
            for t in _TOKEN_RE.findall(str(item).lower()):
                if t in _STOPWORDS:
                    continue
                seen.setdefault(t, None)
    return list(seen.keys())


def refs_str(refs: list[str], *, limit: int = 5) -> str:
    """Compact, human-readable record-ref list for an evidence line."""
    shown = refs[:limit]
    extra = len(refs) - len(shown)
    tail = f" +{extra} more" if extra > 0 else ""
    return ", ".join(shown) + tail
