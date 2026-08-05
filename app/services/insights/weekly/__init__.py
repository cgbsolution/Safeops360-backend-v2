"""Weekly Insight Engine (spec: SafeOps360 — Weekly Insight Engine, Hero Slot &
Dynamic Secondary Row).

A weekly-recomputed engine that generates candidate insights of several types,
scores them (§5), tracks each identity's lifecycle across weeks (§6), promotes one
to a "This week's focus" hero and the next three to a secondary row (§9), and
escalates to a meta-insight when a signal keeps worsening without resolution (§7).

100% deterministic (spec §0): every score and every sentence is rules + slot-
filled templates. No model calls, no network egress anywhere in this package.
"""

from __future__ import annotations

from app.services.insights.weekly.engine import compute_weekly, get_current_week_view

__all__ = ["compute_weekly", "get_current_week_view"]
