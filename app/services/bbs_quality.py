"""P3-1 — BBS observation quality gate + scoring.

A 'good' observation names a specific unsafe act, a specific location, and a
specific person/role (3/3 → actionable). The gate rejects vague at-risk
submissions; the score surfaces specificity. At-risk MEDIUM/HIGH observations
recommend a corrective action.
"""

from __future__ import annotations

AT_RISK_TYPES = {"UNSAFE_ACT", "UNSAFE_CONDITION"}
MIN_AT_RISK_DESCRIPTION = 50  # chars — a vague at-risk obs is not actionable


def is_at_risk(obs_type: str) -> bool:
    return obs_type in AT_RISK_TYPES


def quality_score(description: str, area_id: str | None, responsible_id: str | None) -> int:
    """0..3 specificity: named act (≥40 chars of specifics) + named location +
    named person/role."""
    score = 0
    if description and len(description.strip()) >= 40:
        score += 1
    if area_id:
        score += 1
    if responsible_id:
        score += 1
    return score


def quality_label(score: int) -> str:
    return {0: "too vague", 1: "vague", 2: "adequate", 3: "actionable"}.get(score, "unknown")


def validate_quality(obs_type: str, description: str) -> str | None:
    """Return an error message if an at-risk observation is too vague to action,
    else None. (Soft for safe observations — only at-risk submissions are gated.)"""
    if is_at_risk(obs_type) and len((description or "").strip()) < MIN_AT_RISK_DESCRIPTION:
        return (
            f"At-risk observations need a specific description (≥{MIN_AT_RISK_DESCRIPTION} chars): "
            "name the unsafe act/condition, where, and who — so it can be actioned."
        )
    return None


def capa_recommended(obs_type: str, severity: str) -> bool:
    return is_at_risk(obs_type) and (severity or "").upper() in ("MEDIUM", "HIGH", "CRITICAL")
