"""ISO compliance validators (P2-7).

  • HIRA control hierarchy (ISO 45001 §8.1.2): warn (soft, not block) when only
    administrative/PPE controls are selected without documenting that higher-order
    controls were considered.
  • EAI significance (ISO 14001 §6.1.3): a legal obligation makes an aspect
    SIGNIFICANT regardless of the likelihood×magnitude score.
"""

from __future__ import annotations

CONTROL_HIERARCHY = ["ELIMINATION", "SUBSTITUTION", "ENGINEERING", "ADMINISTRATIVE", "PPE"]
_HIGHER_ORDER = {"ELIMINATION", "SUBSTITUTION", "ENGINEERING"}
_LOWER_ORDER = {"ADMINISTRATIVE", "PPE"}


def validate_control_hierarchy(control_types: list[str], enforce: bool = True) -> list[str]:
    """ISO 45001 §8.1.2. Returns warnings (the form shows them; expert may still
    save after acknowledging). Empty list = no concern / enforcement off."""
    if not enforce:
        return []
    types = {(t or "").upper() for t in control_types}
    warnings: list[str] = []
    has_higher = bool(types & _HIGHER_ORDER)
    only_lower = bool(types) and types.issubset(_LOWER_ORDER)
    if only_lower and not has_higher:
        warnings.append(
            "ISO 45001 §8.1.2: only administrative/PPE controls selected. Document why "
            "elimination, substitution or engineering controls are not practicable before "
            "accepting lower-order controls."
        )
    return warnings


def eai_significance(likelihood_score: int, magnitude_score: int, has_legal_obligation: bool) -> tuple[str, bool]:
    """ISO 14001 §6.1.3. Returns (significanceBand, isLegallyMandated).
    A legal obligation forces SIGNIFICANT regardless of score."""
    if has_legal_obligation:
        return "SIGNIFICANT", True
    score = (likelihood_score or 0) * (magnitude_score or 0)
    if score >= 9:
        return "SIGNIFICANT", False
    if score >= 4:
        return "MODERATE", False
    return "LOW", False
