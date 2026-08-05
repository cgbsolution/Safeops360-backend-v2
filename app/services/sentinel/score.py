"""Brief Priority Score — the deterministic ranking model behind the Executive
Sentinel (Daily Brief upgrade, spec §1.2).

Pure functions, no I/O — unit-testable with plain dicts. Rank every collected
card by a computed score so the single most consequential item is card #1
regardless of which module produced it. The weighting is the heart of the
upgrade:

  * predicted seriousness (PSI/SIF)  — highest weight; a fatal-potential source
    dominates raw lateness.
  * overdue magnitude                — strictly secondary (capped).
  * recurrence / cluster size        — a finding backed by N records outranks a
    single-record one of equal severity.
  * escalation state                 — a not-yet-notified critical is more urgent
    to surface than one already escalated.
  * confidence                       — low-confidence is down-weighted and can
    never sit in the CRITICAL tier (it lands in WATCH as an 'early signal').

Every component is returned alongside the card so 'why is this ranked here' is
inspectable — the same no-black-box principle as the list screens (spec §1.2).
Fully deterministic; no external model call anywhere.
"""

from __future__ import annotations

from typing import Any

# ── weights ──────────────────────────────────────────────────────────────────
# Serious potential alone (60) must outrank the maximum any overdue+severity+
# cluster combination can contribute, so a 6-day-overdue fatal-potential item
# always beats a 45-day-overdue minor one (acceptance §7, line 1).
W_SERIOUS = 60
_OVERDUE_CAP_DAYS = 60
_OVERDUE_PER_DAY = 0.5          # → max 30
_CLUSTER_CAP = 9               # records beyond the first that still add weight
_CLUSTER_PER = 2               # → max 18 (a single-record finding adds 0)
W_FRESH = 5                    # a not-yet-escalated critical is more urgent to push
# Both insight severities (critical|high|watch|info) and alert severities
# (critical|attention|info) map here — 'high' and 'attention' are equivalent.
_SEV_W = {"critical": 25, "high": 18, "attention": 18, "watch": 6, "info": 3}
_CONF_W = {"high": 10, "medium": 6, "low": 2}
_DEFAULT_CONF = "medium"

# Executive lens only keeps CRITICAL + high-magnitude ATTENTION (spec §3).
EXEC_ATTENTION_MIN_SCORE = 40

TIERS = ("critical", "attention", "watch")


def score_components(
    *,
    serious_potential: bool,
    severity: str,
    overdue_days: int | None,
    cluster_size: int | None,
    escalated: bool,
    confidence: str,
) -> dict[str, int]:
    """The Brief Priority Score, broken into its inspectable components. Sum for
    the total. Every input traces to a real, counted field — nothing guessed."""
    serious = W_SERIOUS if serious_potential else 0
    overdue = round(min(max(overdue_days or 0, 0), _OVERDUE_CAP_DAYS) * _OVERDUE_PER_DAY)
    cluster = min(max((cluster_size or 1) - 1, 0), _CLUSTER_CAP) * _CLUSTER_PER
    freshness = 0 if escalated else W_FRESH
    sev = _SEV_W.get((severity or "").lower(), 0)
    conf = _CONF_W.get((confidence or "").lower(), _CONF_W[_DEFAULT_CONF])
    return {
        "seriousPotential": serious,
        "overdue": overdue,
        "cluster": cluster,
        "severity": sev,
        "freshness": freshness,
        "confidence": conf,
    }


def brief_priority_score(**kw: Any) -> int:
    return sum(score_components(**kw).values())


def tier_for(*, serious_potential: bool, severity: str, confidence: str) -> str:
    """CRITICAL / ATTENTION / WATCH (spec §1.3).

    Low-confidence findings never sit in CRITICAL — they drop to WATCH with the
    'early signal' tag, even on a serious source, so a thin signal can't top the
    critical section (spec §1.2 / acceptance §8)."""
    sev = (severity or "").lower()
    if (confidence or "").lower() == "low":
        return "watch"
    if serious_potential or sev == "critical":
        return "critical"
    if sev in ("high", "attention"):
        return "attention"
    return "watch"


# Insight severity vocab (critical|high|watch|info) → Alert severity vocab
# (critical|attention|info). The tier is the authority; this is only the
# fallback when tiering isn't applied.
INSIGHT_TO_ALERT_SEV = {"critical": "critical", "high": "attention", "watch": "info", "info": "info"}
TIER_TO_ALERT_SEV = {"critical": "critical", "attention": "attention", "watch": "info"}


def score_insight(insight: Any, *, escalated: bool = False) -> dict[str, Any]:
    """Score one engine `Insight` (used by the scanner at materialisation time).
    Returns score + components + tier + earlySignal + the alert severity the
    materialised card should carry."""
    serious = bool(getattr(insight, "seriousPotential", False))
    severity = getattr(insight, "severity", "info")
    confidence = getattr(insight, "confidence", _DEFAULT_CONF)
    cluster_size = len(getattr(insight, "recordRefs", []) or []) or 1
    overdue_days = getattr(insight, "overdueDays", None)
    comps = score_components(
        serious_potential=serious,
        severity=severity,
        overdue_days=overdue_days,
        cluster_size=cluster_size,
        escalated=escalated,
        confidence=confidence,
    )
    tier = tier_for(serious_potential=serious, severity=severity, confidence=confidence)
    return {
        "score": sum(comps.values()),
        "components": comps,
        "tier": tier,
        "earlySignal": (confidence or "").lower() == "low",
        "alertSeverity": TIER_TO_ALERT_SEV.get(tier, "info"),
        "seriousPotential": serious,
        "overdueDays": overdue_days,
        "clusterSize": cluster_size,
        "confidence": confidence,
        "escalated": escalated,
    }


def score_alert(alert: dict[str, Any]) -> dict[str, Any]:
    """Score a materialised Alert row dict at READ time — for BOTH the sentinel
    insight cards (rich ranking metadata stored in bodyParams by the scanner) and
    the reactive event cards (sensible defaults derived from what they carry), so
    the two rank together in one unified feed (spec §1.2).

    Returns {score, components, tier, earlySignal} added to the card."""
    bp = alert.get("bodyParams") or {}
    severity = alert.get("severity") or "info"  # alert vocab
    # Event cards don't set seriousPotential; a critical event card is treated as
    # serious-potential (capa_overdue already escalates fatal-potential to critical).
    serious = bool(bp.get("seriousPotential", severity == "critical"))
    overdue_days = bp.get("overdueDays")
    cluster_size = (
        bp.get("clusterSize")
        or len(alert.get("impactedEntities") or [])
        or alert.get("count")
        or 1
    )
    escalated = bool(bp.get("escalated", False))
    confidence = bp.get("confidence", _DEFAULT_CONF)
    comps = score_components(
        serious_potential=serious,
        severity=severity,
        overdue_days=overdue_days,
        cluster_size=cluster_size,
        escalated=escalated,
        confidence=confidence,
    )
    return {
        "score": sum(comps.values()),
        "components": comps,
        "tier": tier_for(serious_potential=serious, severity=severity, confidence=confidence),
        "earlySignal": (confidence or "").lower() == "low",
    }


def role_lens_keep(card: dict[str, Any], role: str) -> bool:
    """Role-based visibility (spec §3). `card` must already carry `tier` and
    `score` (from score_alert). Same underlying pool, three lenses.

    * executive  — cross-site rollup: only CRITICAL + high-magnitude ATTENTION.
                   Routine single-record WATCH items are suppressed.
    * hse_manager — the working view: everything, all tiers incl WATCH.
    * site_lead   — narrowest: CRITICAL site-wide safety flags + actionable
                    ATTENTION items; register/governance WATCH noise filtered out.
    """
    tier = card.get("tier", "watch")
    score = card.get("score", 0)
    if role == "executive":
        return tier == "critical" or (tier == "attention" and score >= EXEC_ATTENTION_MIN_SCORE)
    if role == "site_lead":
        if tier == "critical":
            return True
        if tier == "attention":
            # actionable = has deep-linkable source records to act on
            return bool(card.get("impactedEntities"))
        return False
    # hse_manager (default working view) — keep everything
    return True
