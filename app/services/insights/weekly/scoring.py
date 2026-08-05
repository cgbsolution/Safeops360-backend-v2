"""Weekly Insight Engine — scoring (spec §5).

Four components, each normalised 0-100, then weighted. `scoreComponents` is
persisted on every snapshot (§5, §14) so tuning can see *why* something scored 82.
Deterministic arithmetic only — no model, no network.
"""

from __future__ import annotations

from app.services.insights.weekly.types import ScoreConfig, area_risk, category_risk

_VELOCITY_NEW_BASELINE = 40.0  # a brand-new insight has nothing to diff against


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def seriousness(cfg: ScoreConfig, category: str | None, area_name: str | None) -> float:
    """category risk × area risk (§5)."""
    return clamp(category_risk(cfg, category) * area_risk(area_name) * 100.0)


def ageing(oldest_days: float, median_closure_days: float | None) -> float:
    """Oldest record as a multiple of that category's median closure time (§5)."""
    median = median_closure_days if median_closure_days and median_closure_days > 0 else 30.0
    ratio = oldest_days / median
    return clamp(ratio * 33.0)


def ownership_decay(unowned_fraction: float, avg_unowned_days: float) -> float:
    """Proportion unowned, weighted by how long they've been unowned (§5)."""
    frac = clamp(unowned_fraction, 0.0, 1.0)
    duration_factor = min(1.0, 0.5 + avg_unowned_days / 60.0)
    return clamp(frac * 100.0 * duration_factor)


def velocity(magnitude_now: float, magnitude_prior: float | None) -> float:
    """Rate of change vs ~4 weeks ago (§5). Growth is the signal."""
    if magnitude_prior is None:
        return _VELOCITY_NEW_BASELINE
    return clamp((magnitude_now - magnitude_prior) * 10.0)


def finalize(cfg: ScoreConfig, components: dict[str, float]) -> float:
    w = cfg.weights
    return round(
        w.seriousness * components.get("seriousness", 0.0)
        + w.velocity * components.get("velocity", 0.0)
        + w.ageing * components.get("ageing", 0.0)
        + w.ownershipDecay * components.get("ownershipDecay", 0.0),
        1,
    )
