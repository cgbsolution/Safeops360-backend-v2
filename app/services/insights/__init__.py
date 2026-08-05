"""Deterministic AI Insights engine (spec Stream A).

A single shared engine that computes summary-level "bar" insights and row-level
"signals" for the platform's list screens, from real records only. No external
model calls anywhere — clustering, overdue math, duplicate/fuzzy detection and
threshold breaches are plain Python; the natural-language phrasing is a
template + slot-filling layer (`templates.py`). This is the whole engine, not a
phase-1 subset — it runs correctly with all outbound network access blocked
(airgapped tenants).

Public surface:
  * `compute(db, module, *, plant, date_from, date_to, filters)` → InsightResponse
  * `SUPPORTED_MODULES` — the module keys the engine knows how to answer
"""

from __future__ import annotations

from app.services.insights.engine import SUPPORTED_MODULES, compute

__all__ = ["compute", "SUPPORTED_MODULES"]
