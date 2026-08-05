"""Pydantic schemas for the deterministic AI Insights engine.

Two response types per module call (spec §1.1):
  * bar-level insights (0-3) → the Insight Bar at the top of a list screen
  * row-level signals (0-1 per record) → a Signal Chip on specific rows

The engine is 100% deterministic (rules + template slot-filling); there are no
model calls anywhere, so the same contract holds on airgapped deployments. The
`Insight` shape is kept a drop-in for a future local-model phrasing swap (spec
§1.1) — the phrasing layer is `app/services/insights/templates.py`, callers only
ever see this validated contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

InsightKind = Literal[
    "trend",
    "cluster",
    "anomaly",
    "predictive_risk",
    "next_best_action",
    "duplicate",
    "overdue_escalation",
]
InsightSeverity = Literal["info", "watch", "high", "critical"]
InsightConfidence = Literal["low", "medium", "high"]


class Insight(BaseModel):
    """A bar-level, summary finding grounded in real records."""

    id: str
    kind: InsightKind
    severity: InsightSeverity
    headline: str = Field(max_length=90)
    evidence: str
    recordRefs: list[str] = Field(default_factory=list)
    suggestedAction: str | None = None
    confidence: InsightConfidence

    # ── Brief-Sentinel ranking metadata (optional; default-safe) ──────────────
    # Populated by the predictive rules so the Daily Brief's Brief Priority Score
    # (app/services/sentinel/score.py) is a deterministic, inspectable function of
    # fields the rule already computed — never re-queried per the spec §1.2. The
    # eight list-screen consumers ignore these three; they are a backwards-
    # compatible superset of the shipped Insight contract (the InsightBar/
    # SignalChip components read only the fields above).
    seriousPotential: bool = False   # source carries fatal / serious-injury (PSI/SIF) potential
    overdueDays: int | None = None   # days past a target, when the finding is overdue-grounded
    escalated: bool = False          # escalation chain already notified for this finding


class Signal(BaseModel):
    """A row-level chip payload attached to one record."""

    recordId: str
    recordRef: str
    kind: InsightKind
    severity: InsightSeverity
    label: str = Field(max_length=24)
    evidence: str
    suggestedAction: str | None = None
    # Row-Level Insight Layer (Part 3): when set, clicking the chip narrows the
    # list to this signal's cluster/location. A query string the host list page
    # understands (e.g. "?cat=ELECTRICAL&area=<id>" or "?insight=<barId>"). Most
    # signals are per-record and leave this None (the chip just opens its
    # popover). Optional + default-safe — the other list screens ignore it.
    filterHref: str | None = None


class InsightResponse(BaseModel):
    module: str
    plant: str | None = None
    generatedAt: datetime
    # 0-3 bar insights; empty when nothing clears the confidence/severity bar or
    # the module has < MIN_RECORDS records for this scope (bar is suppressed).
    bar: list[Insight] = Field(default_factory=list)
    # 0-1 signal per record; only rows that earned a signal appear here.
    signals: list[Signal] = Field(default_factory=list)
    recordCount: int = 0
    # True when the bar was suppressed on thin data (< MIN_RECORDS). The UI shows
    # nothing rather than a low-confidence card on too few records (spec §1.4).
    suppressed: bool = False
    reason: str | None = None
    cached: bool = False
