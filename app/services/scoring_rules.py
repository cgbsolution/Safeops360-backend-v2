"""WP-49 / F-22 - per-audit-type scoring rules, and the rule RENDERED.

`MINIMUM_PASS_SCORE = 80.0` was a module-level constant in
`services/audit_compliance.py`, applied to every audit of every type, while
`AuditTemplate.scoring` sat unused. A fire-equipment inspection and an SA8000
social audit do not share a pass mark or a critical-failure tolerance.

Two things this fixes:

  1. **Configurable.** `CamsAuditType.scoringRules` overrides the default.
     NULL keeps the historic 80.0 / zero-critical behaviour, so nothing changes
     until someone deliberately configures a type.
  2. **Rendered.** The diagnosis found the gate invisible - an audit could read
     "99.5% overall" and still be a FAIL because of 8 critical failures, with
     nothing on screen explaining why. `describe_gate()` produces that sentence.

Pure functions: the caller loads the rules, these decide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The historic behaviour, preserved exactly as the fallback.
DEFAULT_MINIMUM_PASS_SCORE = 80.0
DEFAULT_CRITICAL_GATE = 0  # zero critical failures tolerated

# Below this fraction of the applicable population assessed, NO grade is shown.
#
# 04-target.md Appendix C requires a provisional score to carry its assessed
# fraction, but sets no floor — so a report could headline "100.0% CONFORMING"
# on 1 of 82 checkpoints with the caveat buried in the body. That is the same
# defect class as the 78.9%-over-0-of-82 report, just wearing a disclaimer
# nobody reading the cover will see. 20% is the floor; it is per-audit-type
# configurable because a 12-checkpoint inspection and a 1500-checkpoint social
# audit do not have the same "enough to say something" point.
DEFAULT_MIN_ASSESSED_PCT_FOR_GRADE = 20.0

NA_HANDLING = ("EXCLUDE", "COUNT_AS_PASS")


@dataclass(frozen=True)
class ScoringRules:
    minimumPassScore: float = DEFAULT_MINIMUM_PASS_SCORE
    # Max critical failures still allowed to pass. 0 = any critical fails it.
    criticalGateThreshold: int = DEFAULT_CRITICAL_GATE
    # Weight a PARTIAL contributes. 0.5 is what `_compute_score` already does.
    partialCredit: float = 0.5
    # N/A excluded from the denominator (current, correct) or counted as a pass.
    naHandling: str = "EXCLUDE"
    # Coverage floor below which the grade badge is suppressed entirely.
    minimumAssessedPctForGrade: float = DEFAULT_MIN_ASSESSED_PCT_FOR_GRADE

    def as_dict(self) -> dict[str, Any]:
        return {
            "minimumPassScore": self.minimumPassScore,
            "criticalGateThreshold": self.criticalGateThreshold,
            "partialCredit": self.partialCredit,
            "naHandling": self.naHandling,
            "minimumAssessedPctForGrade": self.minimumAssessedPctForGrade,
        }


def grade_visibility(
    *, assessed: int, applicable: int, rules: ScoringRules | None = None
) -> dict[str, Any]:
    """Is there enough coverage for a grade to mean anything?

    Returns the decision AND the honest label to show in its place, so no caller
    has to invent one. `applicable` is the post-N/A denominator — grading 1 of 82
    is dishonest whether or not the other 81 were excluded.

    This is deliberately a *visibility* decision, not a scoring one: the band and
    percentage are still computed and still stored in the snapshot. They are
    simply not rendered as a headline verdict until the audit has covered enough
    ground to support one.
    """
    r = rules or ScoringRules()
    pct = (assessed / applicable * 100) if applicable else 0.0
    return {
        "showGrade": applicable > 0 and pct >= r.minimumAssessedPctForGrade,
        "assessed": assessed,
        "applicable": applicable,
        "assessedPct": round(pct, 1),
        "threshold": r.minimumAssessedPctForGrade,
        # What replaces the badge below threshold. Same position, same weight.
        "label": f"{assessed} of {applicable} assessed",
    }


def rules_from(raw: dict[str, Any] | None) -> ScoringRules:
    """Parse stored rules, falling back per-field.

    Per-field rather than all-or-nothing: a type that sets only a pass mark
    should not silently lose the critical gate.
    """
    if not raw:
        return ScoringRules()
    try:
        pass_score = float(raw.get("minimumPassScore", DEFAULT_MINIMUM_PASS_SCORE))
        gate = int(raw.get("criticalGateThreshold", DEFAULT_CRITICAL_GATE))
        partial = float(raw.get("partialCredit", 0.5))
    except (TypeError, ValueError):
        # Malformed config must not silently score every audit as a pass.
        return ScoringRules()
    try:
        min_assessed = float(
            raw.get("minimumAssessedPctForGrade", DEFAULT_MIN_ASSESSED_PCT_FOR_GRADE)
        )
    except (TypeError, ValueError):
        min_assessed = DEFAULT_MIN_ASSESSED_PCT_FOR_GRADE
    na = raw.get("naHandling", "EXCLUDE")
    return ScoringRules(
        minimumPassScore=max(0.0, min(100.0, pass_score)),
        criticalGateThreshold=max(0, gate),
        partialCredit=max(0.0, min(1.0, partial)),
        naHandling=na if na in NA_HANDLING else "EXCLUDE",
        minimumAssessedPctForGrade=max(0.0, min(100.0, min_assessed)),
    )


def evaluate(
    *, overall_pct: float | None, critical_failures: int, rules: ScoringRules
) -> dict[str, Any]:
    """Apply the gate. Returns the verdict AND the sentence explaining it.

    The critical gate is checked independently of the percentage, which is the
    whole point: 99.5% with 8 critical failures is a FAIL, and the reader must
    be told which condition failed.
    """
    # Nothing assessable -> no verdict. Never a 0% that reads as total failure.
    if overall_pct is None:
        return {
            "passed": False,
            "band": "NOT_ASSESSED",
            "scoreMet": False,
            "gateMet": critical_failures <= rules.criticalGateThreshold,
            "explanation": "Nothing assessable — no score can be computed.",
            "rules": rules.as_dict(),
        }

    score_met = overall_pct >= rules.minimumPassScore
    gate_met = critical_failures <= rules.criticalGateThreshold
    passed = score_met and gate_met

    if passed:
        band = "CONFORMING"
    elif not gate_met:
        # A critical failure outranks the percentage — say so.
        band = "CRITICAL_NC"
    elif overall_pct >= rules.minimumPassScore - 10:
        band = "MINOR_NC"
    else:
        band = "MAJOR_NC"

    return {
        "passed": passed,
        "band": band,
        "scoreMet": score_met,
        "gateMet": gate_met,
        "explanation": describe_gate(
            overall_pct=overall_pct, critical_failures=critical_failures,
            rules=rules, passed=passed, score_met=score_met, gate_met=gate_met,
        ),
        "rules": rules.as_dict(),
    }


def describe_gate(
    *, overall_pct: float, critical_failures: int, rules: ScoringRules,
    passed: bool, score_met: bool, gate_met: bool,
) -> str:
    """The sentence the diagnosis said was missing (F-22).

    Target shape: "99.5% overall, but FAIL: 8 critical failures, threshold 0."
    A number without its rule is not a result.
    """
    pct = f"{overall_pct:g}%"
    if passed:
        return (
            f"{pct} overall — PASS: at or above the {rules.minimumPassScore:g}% pass mark, "
            f"with {critical_failures} critical failure(s) against a threshold of "
            f"{rules.criticalGateThreshold}."
        )
    if not gate_met:
        return (
            f"{pct} overall, but FAIL: {critical_failures} critical failure(s), "
            f"threshold {rules.criticalGateThreshold}. A critical failure fails the audit "
            "regardless of the percentage."
        )
    return (
        f"{pct} overall — FAIL: below the {rules.minimumPassScore:g}% pass mark "
        f"(critical-failure gate was met)."
    )


__all__ = [
    "DEFAULT_MINIMUM_PASS_SCORE",
    "DEFAULT_CRITICAL_GATE",
    "DEFAULT_MIN_ASSESSED_PCT_FOR_GRADE",
    "NA_HANDLING",
    "ScoringRules",
    "rules_from",
    "evaluate",
    "describe_gate",
    "grade_visibility",
]
