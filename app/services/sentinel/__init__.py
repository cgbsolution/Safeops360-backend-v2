"""Executive Sentinel — the cross-module, severity-ranked rollup that upgrades
the Daily Brief from a reactive feed into a proactive sentinel (spec §0–§4).

It is NOT a new intelligence engine: ``scanner`` consumes the existing
deterministic AI Insights engine and materialises the predictive cards as Alert
rows; ``score`` is the deterministic, inspectable Brief Priority Score used to
rank the unified feed and apply the role lenses. No external model calls.
"""

from __future__ import annotations

from app.services.sentinel.score import (
    EXEC_ATTENTION_MIN_SCORE,
    brief_priority_score,
    role_lens_keep,
    score_alert,
    score_components,
    score_insight,
    tier_for,
)
from app.services.sentinel.scanner import run_sentinel_scan

__all__ = [
    "run_sentinel_scan",
    "score_alert",
    "score_insight",
    "score_components",
    "brief_priority_score",
    "tier_for",
    "role_lens_keep",
    "EXEC_ATTENTION_MIN_SCORE",
]
