"""P3-1 — BBS observation quality gate + scoring.

A 'good' observation names a specific unsafe act, a specific location, and a
specific person/role (3/3 → actionable). The gate rejects vague at-risk
submissions; the score surfaces specificity. At-risk MEDIUM/HIGH observations
recommend a corrective action.
"""

from __future__ import annotations

AT_RISK_TYPES = {"UNSAFE_ACT", "UNSAFE_CONDITION"}
# Length no longer gates submission. It was 50 chars on at-risk types, which is
# every type the form now offers — so the gate had gone from "reject the vague
# ones" to "reject the terse ones", and a real observation typed one-handed on a
# shop floor ("guard missing on knitting m/c 4") is short, not vague. Length
# still feeds `quality_score`, so specificity is measured and reported; it is
# just not a wall. Set to 0 rather than deleted because the score thresholds and
# the wording below are the only other consumers.
MIN_AT_RISK_DESCRIPTION = 0


def is_at_risk(obs_type: str) -> bool:
    return obs_type in AT_RISK_TYPES


def quality_score(
    description: str,
    area_id: str | None,
    responsible_id: str | None,
    location: str | None = None,
) -> int:
    """0..3 specificity: named act (≥40 chars of specifics) + named location +
    named person/role.

    The location point is scored from EITHER a structured area or the free-text
    `location` the form now collects. Keying it on `area_id` alone would have
    docked every observation filed since the Area dropdown was replaced, which
    would read as a site-wide collapse in reporting quality that never happened.
    """
    score = 0
    if description and len(description.strip()) >= 40:
        score += 1
    if area_id or (location or "").strip():
        score += 1
    if responsible_id:
        score += 1
    return score


def quality_label(score: int) -> str:
    return {0: "too vague", 1: "vague", 2: "adequate", 3: "actionable"}.get(score, "unknown")


def validate_quality(obs_type: str, description: str) -> str | None:
    """Return an error message if the description is unusable, else None.

    Only emptiness is rejected now. A minimum length is a poor proxy for
    specificity — it blocked terse-but-clear reports while passing 50
    characters of padding — so the judgement moved entirely to
    `quality_score`, which measures and reports rather than refusing.
    """
    if not (description or "").strip():
        return "Describe what was observed."
    if MIN_AT_RISK_DESCRIPTION and is_at_risk(obs_type) and (
        len(description.strip()) < MIN_AT_RISK_DESCRIPTION
    ):
        return (
            f"At-risk observations need a specific description (≥{MIN_AT_RISK_DESCRIPTION} chars): "
            "name the unsafe act/condition, where, and who — so it can be actioned."
        )
    return None


def capa_recommended(obs_type: str, severity: str) -> bool:
    return is_at_risk(obs_type) and (severity or "").upper() in ("MEDIUM", "HIGH", "CRITICAL")
