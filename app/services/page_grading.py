"""Page Industries internal-audit grading vocabulary.

The engine's native verdict is a four-value bucket (pass / partial / fail / na)
scored as a pass-ratio. Page Industries grade their internal audits on a
seven-column model instead — the one their auditors already fill in on the
`Test - Audit Checklist` workbook, columns C–I:

    C  Grade Awarded    Unsatisfactory | Major Improvement Needed |
                        Some Improvement Needed | Effective | N/A
    D  Score Allotted   NA | 3
    E  Score Obtained   3 | 2 | 1 | 0 | -1
    F  Status           Complied | Non Compliance | Repeated Non Compliance |
                        New Observation | Repeated Observation | N/A | MAS (N/A)
    G  Audit Findings   free text (the auditor's comment)
    H  Risk Grade       High | Medium | Low
    I  Requirement Type Statutory/Regulatory | Internal Requirement

This module is the single home for that vocabulary and the rules that connect
it back to the engine, so no caller has to hard-code a label or re-derive a
mapping. Pure functions — the callers load, these decide.

Three connections matter:

1. **Grade drives the engine bucket.** Everything downstream of the verdict
   (finding routing, auto-CAPA, the discipline navigator, workflow states) keys
   off pass/partial/fail/na and continues to work untouched. The Page grade is
   the control the auditor sees; the bucket is what the engine reads.

2. **Grade drives the score, and the score is overridable.** Effective=3 down
   to Unsatisfactory=0 is a straight ladder, but the workbook's -1 has no grade
   of its own — it is the penalty a REPEAT finding carries. So the suggestion
   is computed from grade *and* status, and the auditor can still override it.

3. **Requirement Type is checkpoint master data, not a verdict.** A factory
   licence IS statutory; that does not change because of who audited it. It is
   carried on the library checkpoint and denormalised onto the response row at
   materialisation, and rendered read-only. Risk Grade is the opposite — it is
   the auditor's assessment of what they actually found, so it is captured per
   response.

Scoring is points-based, NOT the pass-ratio: percent = Σ score_obtained /
Σ score_allotted. That is a genuinely different number from the engine's
native `(passed + 0.5·partial) / assessable`, and it is the one Page reconcile
against their own sheet — a repeat non-compliance costs 4 points out of 3, so
a discipline can legitimately score below zero. `compute_points_score` is
allowed to return a negative percentage for exactly that reason; clamping it
would hide the penalty the -1 exists to apply.
"""

from __future__ import annotations

from typing import Any

# ── Column C — Grade Awarded ────────────────────────────────────────────
GRADE_EFFECTIVE = "EFFECTIVE"
GRADE_SOME_IMPROVEMENT = "SOME_IMPROVEMENT_NEEDED"
GRADE_MAJOR_IMPROVEMENT = "MAJOR_IMPROVEMENT_NEEDED"
GRADE_UNSATISFACTORY = "UNSATISFACTORY"
GRADE_NA = "NA"

GRADES: tuple[str, ...] = (
    GRADE_UNSATISFACTORY,
    GRADE_MAJOR_IMPROVEMENT,
    GRADE_SOME_IMPROVEMENT,
    GRADE_EFFECTIVE,
    GRADE_NA,
)

GRADE_LABEL: dict[str, str] = {
    GRADE_UNSATISFACTORY: "Unsatisfactory",
    GRADE_MAJOR_IMPROVEMENT: "Major Improvement Needed",
    GRADE_SOME_IMPROVEMENT: "Some Improvement Needed",
    GRADE_EFFECTIVE: "Effective",
    GRADE_NA: "N/A",
}

# ── Column F — Status ───────────────────────────────────────────────────
STATUS_COMPLIED = "COMPLIED"
STATUS_NON_COMPLIANCE = "NON_COMPLIANCE"
STATUS_REPEATED_NON_COMPLIANCE = "REPEATED_NON_COMPLIANCE"
STATUS_NEW_OBSERVATION = "NEW_OBSERVATION"
STATUS_REPEATED_OBSERVATION = "REPEATED_OBSERVATION"
STATUS_NA = "NA"
STATUS_MAS_NA = "MAS_NA"

STATUSES: tuple[str, ...] = (
    STATUS_COMPLIED,
    STATUS_NON_COMPLIANCE,
    STATUS_REPEATED_NON_COMPLIANCE,
    STATUS_NEW_OBSERVATION,
    STATUS_REPEATED_OBSERVATION,
    STATUS_NA,
    STATUS_MAS_NA,
)

STATUS_LABEL: dict[str, str] = {
    STATUS_COMPLIED: "Complied",
    STATUS_NON_COMPLIANCE: "Non Compliance",
    STATUS_REPEATED_NON_COMPLIANCE: "Repeated Non Compliance",
    STATUS_NEW_OBSERVATION: "New Observation",
    STATUS_REPEATED_OBSERVATION: "Repeated Observation",
    STATUS_NA: "N/A",
    STATUS_MAS_NA: "MAS (N/A)",
}

# The two statuses that mean "we have seen this before". They are the ONLY
# source of the workbook's -1, and they are also what `isRepeatFinding` on a
# CamsFinding means — so the repeat flag and the score penalty cannot disagree.
REPEAT_STATUSES: frozenset[str] = frozenset(
    {STATUS_REPEATED_NON_COMPLIANCE, STATUS_REPEATED_OBSERVATION}
)

# Statuses that take the checkpoint out of the scored population entirely.
# MAS (N/A) is "Management Assessment System — not applicable": scoped out by
# the programme rather than by the site, but scored identically.
NOT_APPLICABLE_STATUSES: frozenset[str] = frozenset({STATUS_NA, STATUS_MAS_NA})

# ── Conformance mode — how many verdicts a checkpoint offers ────────────
#
# Page's IMS/EnMS department checklist records ONE thing per checkpoint:
# Conformance, Non-Conformance or Observation. That is the header of column E
# on both sheets, verbatim. The seven-value status ladder above is the internal
# audit's vocabulary and asking for it here would be asking auditors to answer a
# question their own form does not put to them.
#
# So the mode is declared per LIBRARY CATEGORY (`conformance_mode` on the
# category dict) and snapshotted onto the response row at materialisation, in
# the same way and for the same reason as `requirement_type` — an audit must
# keep being read against the vocabulary it was conducted under, whatever the
# library is later changed to say.
#
# TRISTATE is a NARROWING, not a second state machine: each of the three
# parameters resolves to a grade and a status from the ladders above, so the
# score, the CAPA routing, the discipline rollup and both reports keep working
# with no branch of their own. Three things are unreachable in this mode, and
# that is the deliberate cost of matching the customer's form:
#
#   • N/A            — every checkpoint stays in the score denominator.
#   • the repeat −1  — REPEATED_* cannot be selected, so a repeat finding
#                      scores the same as a first one.
#   • MAS (N/A)      — same as N/A.
CONFORMANCE_FULL = "FULL"
CONFORMANCE_TRISTATE = "TRISTATE"
CONFORMANCE_MODES: tuple[str, ...] = (CONFORMANCE_FULL, CONFORMANCE_TRISTATE)

TRI_CONFORMANCE = "CONFORMANCE"
TRI_NON_CONFORMANCE = "NON_CONFORMANCE"
TRI_OBSERVATION = "OBSERVATION"

TRISTATE_VERDICTS: tuple[str, ...] = (
    TRI_CONFORMANCE,
    TRI_NON_CONFORMANCE,
    TRI_OBSERVATION,
)

TRISTATE_LABEL: dict[str, str] = {
    TRI_CONFORMANCE: "Conformance",
    TRI_NON_CONFORMANCE: "Non-Conformance",
    TRI_OBSERVATION: "Observation",
}

# Tristate verdict -> (grade, status). The status side is what makes a tristate
# audit legible to every existing reader: a report, an export or an API client
# that knows nothing about this mode still sees COMPLIED / NON_COMPLIANCE /
# NEW_OBSERVATION and reads it correctly.
TRISTATE_TO_GRADE_STATUS: dict[str, tuple[str, str]] = {
    TRI_CONFORMANCE: (GRADE_EFFECTIVE, STATUS_COMPLIED),
    TRI_NON_CONFORMANCE: (GRADE_MAJOR_IMPROVEMENT, STATUS_NON_COMPLIANCE),
    TRI_OBSERVATION: (GRADE_SOME_IMPROVEMENT, STATUS_NEW_OBSERVATION),
}

# The inverse, for rendering a row the tristate control did not write — a
# checkpoint graded before the mode was introduced, or one bulk-marked through
# the "mark department compliant" fast path. Anything that maps to no tristate
# verdict renders as unanswered rather than as a wrong one.
_STATUS_TO_TRISTATE: dict[str, str] = {
    STATUS_COMPLIED: TRI_CONFORMANCE,
    STATUS_NON_COMPLIANCE: TRI_NON_CONFORMANCE,
    STATUS_REPEATED_NON_COMPLIANCE: TRI_NON_CONFORMANCE,
    STATUS_NEW_OBSERVATION: TRI_OBSERVATION,
    STATUS_REPEATED_OBSERVATION: TRI_OBSERVATION,
}


def normalise_conformance_mode(value: Any) -> str:
    """Unknown / absent -> FULL, which is the engine's historic behaviour."""
    s = _squash(str(value or ""))
    for m in CONFORMANCE_MODES:
        if _squash(m) == s:
            return m
    return CONFORMANCE_FULL


def normalise_tristate(value: Any) -> str | None:
    return _normalise(value, TRISTATE_VERDICTS, TRISTATE_LABEL)


def tristate_for_status(status: str | None) -> str | None:
    """Which of the three parameters a stored status displays as."""
    return _STATUS_TO_TRISTATE.get(status or "")


def tristate_vocabulary() -> list[dict[str, Any]]:
    """The three controls, each carrying what it resolves to — so the client
    renders the customer's words and the server stores the engine's."""
    out = []
    for v in TRISTATE_VERDICTS:
        grade, status = TRISTATE_TO_GRADE_STATUS[v]
        out.append({
            "code": v,
            "label": TRISTATE_LABEL[v],
            "grade": grade,
            "status": status,
            "value": GRADE_TO_VALUE[grade],
            "score": GRADE_TO_SCORE[grade],
        })
    return out


# ── Column H — Risk Grade ───────────────────────────────────────────────
RISK_HIGH = "HIGH"
RISK_MEDIUM = "MEDIUM"
RISK_LOW = "LOW"

RISK_GRADES: tuple[str, ...] = (RISK_HIGH, RISK_MEDIUM, RISK_LOW)

RISK_GRADE_LABEL: dict[str, str] = {
    RISK_HIGH: "High",
    RISK_MEDIUM: "Medium",
    RISK_LOW: "Low",
}

# ── Column I — Requirement Type (checkpoint master data) ────────────────
REQ_STATUTORY = "STATUTORY_REGULATORY"
REQ_INTERNAL = "INTERNAL_REQUIREMENT"

REQUIREMENT_TYPES: tuple[str, ...] = (REQ_STATUTORY, REQ_INTERNAL)

REQUIREMENT_TYPE_LABEL: dict[str, str] = {
    REQ_STATUTORY: "Statutory/Regulatory",
    REQ_INTERNAL: "Internal Requirement",
}

# ── Column D — Score Allotted ───────────────────────────────────────────
# Every scored checkpoint is worth the same 3 points; an N/A checkpoint is
# worth nothing and leaves the denominator (the workbook's literal "NA").
FULL_SCORE = 3
SCORE_OBTAINED_CHOICES: tuple[int, ...] = (3, 2, 1, 0, -1)


# ── Rules ───────────────────────────────────────────────────────────────

# Grade -> the engine's native verdict bucket. This is the join between the
# Page vocabulary and every existing code path (routing, CAPA, rollup).
#
# Major Improvement Needed maps to `fail` rather than `partial`: it is a
# non-compliance that needs a corrective action, and mapping it to `partial`
# would let a genuine NC escape the critical-failure gate.
GRADE_TO_VALUE: dict[str, str] = {
    GRADE_EFFECTIVE: "pass",
    GRADE_SOME_IMPROVEMENT: "partial",
    GRADE_MAJOR_IMPROVEMENT: "fail",
    GRADE_UNSATISFACTORY: "fail",
    GRADE_NA: "na",
}

# Grade -> the points the workbook awards before any repeat penalty.
GRADE_TO_SCORE: dict[str, int | None] = {
    GRADE_EFFECTIVE: 3,
    GRADE_SOME_IMPROVEMENT: 2,
    GRADE_MAJOR_IMPROVEMENT: 1,
    GRADE_UNSATISFACTORY: 0,
    GRADE_NA: None,
}

# Grade -> the status an auditor most often pairs it with. A SUGGESTION only:
# the repeat variants can never be inferred from the grade, which is the whole
# reason status is a separate control.
GRADE_TO_STATUS: dict[str, str] = {
    GRADE_EFFECTIVE: STATUS_COMPLIED,
    GRADE_SOME_IMPROVEMENT: STATUS_NEW_OBSERVATION,
    GRADE_MAJOR_IMPROVEMENT: STATUS_NON_COMPLIANCE,
    GRADE_UNSATISFACTORY: STATUS_NON_COMPLIANCE,
    GRADE_NA: STATUS_NA,
}

# Risk Grade -> CAPA severity, used when a finding spawns a corrective action.
# The auditor's assessment of the finding beats the checkpoint's inherent
# criticality here, because the criticality was set before anyone looked.
RISK_TO_CAPA_SEVERITY: dict[str, str] = {
    RISK_HIGH: "CRITICAL",
    RISK_MEDIUM: "HIGH",
    RISK_LOW: "MODERATE",
}


def normalise_grade(value: Any) -> str | None:
    """Accept a stored code, a workbook label, or None."""
    return _normalise(value, GRADES, GRADE_LABEL)


def normalise_status(value: Any) -> str | None:
    return _normalise(value, STATUSES, STATUS_LABEL)


def normalise_risk_grade(value: Any) -> str | None:
    return _normalise(value, RISK_GRADES, RISK_GRADE_LABEL)


def normalise_requirement_type(value: Any) -> str | None:
    return _normalise(value, REQUIREMENT_TYPES, REQUIREMENT_TYPE_LABEL)


def _normalise(value: Any, codes: tuple[str, ...], labels: dict[str, str]) -> str | None:
    """Map a code or a human label onto the canonical code. Unknown -> None.

    Tolerating the label matters because the workbook is the source anyone
    pastes from — "Repeated Non Compliance" has to land on
    REPEATED_NON_COMPLIANCE rather than silently becoming an unset field.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    upper = s.upper()
    if upper in codes:
        return upper
    # Label match, punctuation-insensitive: "MAS (N/A)" -> MAS_NA, "N/A" -> NA.
    squashed = _squash(s)
    for code, label in labels.items():
        if _squash(label) == squashed:
            return code
    # Last resort: the code written with spaces or hyphens instead of underscores.
    for code in codes:
        if _squash(code) == squashed:
            return code
    return None


def _squash(s: str) -> str:
    return "".join(ch for ch in s.upper() if ch.isalnum())


def value_for_grade(grade: str | None) -> str | None:
    """Grade -> engine verdict bucket (pass/partial/fail/na), or None if unset."""
    if grade is None:
        return None
    return GRADE_TO_VALUE.get(grade)


def allotted_for_grade(grade: str | None) -> int | None:
    """Column D. `None` is the workbook's "NA" — out of the denominator."""
    if grade is None:
        return None
    return None if grade == GRADE_NA else FULL_SCORE


def suggest_score(grade: str | None, status: str | None) -> int | None:
    """Column E, before any auditor override.

    The repeat penalty is applied on top of the grade ladder: a finding you
    already raised and that is still open costs a point rather than merely
    earning none. It is only applied to a grade that actually scored — a
    Repeated Observation against an `Effective` grade is a contradiction the
    auditor should resolve, not something to silently drive to -1.
    """
    if grade is None:
        return None
    base = GRADE_TO_SCORE.get(grade)
    if base is None:  # N/A — nothing to score
        return None
    if status in REPEAT_STATUSES and base < FULL_SCORE:
        return -1
    return base


def suggest_status(grade: str | None) -> str | None:
    if grade is None:
        return None
    return GRADE_TO_STATUS.get(grade)


def is_repeat(status: str | None) -> bool:
    return status in REPEAT_STATUSES


def carries_risk_grade(grade: str | None) -> bool:
    """Whether a risk grade is MEANINGFUL on this grade — i.e. the checkpoint is
    a finding at all. A grade outside this set has nothing to be risky about, so
    a stored risk grade is stale and gets cleared.

    Separate from `requires_risk_grade` because the two questions differ in
    TRISTATE mode: a risk grade there is optional (not required) but it is still
    meaningful, and collapsing the two would erase every risk grade an auditor
    deliberately set on a Non-Conformance.
    """
    return grade in (GRADE_UNSATISFACTORY, GRADE_MAJOR_IMPROVEMENT, GRADE_SOME_IMPROVEMENT)


def requires_risk_grade(grade: str | None, mode: str | None = None) -> bool:
    """A finding without an assessed risk cannot be prioritised, so anything
    that is not `Effective` or `N/A` has to carry one — and submission is
    blocked until it does.

    Not in TRISTATE mode. The customer's IMS/EnMS form asks for one verdict and
    a comment per line and carries no risk column, so gating submission on a
    field their auditors are not given would make the audit unsubmittable. Risk
    Grade stays capturable there — it is simply not a gate — and `capa_severity`
    already falls back to the checkpoint's own criticality when it is absent.
    """
    if normalise_conformance_mode(mode) == CONFORMANCE_TRISTATE:
        return False
    return carries_risk_grade(grade)


def capa_severity(risk_grade: str | None, fallback: str) -> str:
    if risk_grade and risk_grade in RISK_TO_CAPA_SEVERITY:
        return RISK_TO_CAPA_SEVERITY[risk_grade]
    return fallback


def compute_points_score(
    *, obtained: int | None, allotted: int | None
) -> float:
    """Σ obtained / Σ allotted as a percentage. Negative results are real."""
    if not allotted:
        return 0.0
    return round((obtained or 0) / allotted * 100, 1)


def band(percent: float, minimum_pass: float) -> str:
    """The label Page put on a score. Bands mirror the grade ladder so a 100%
    discipline reads `Effective` for the same reason a 100% checkpoint does."""
    if percent >= 90:
        return "EFFECTIVE"
    if percent >= minimum_pass:
        return "SOME_IMPROVEMENT_NEEDED"
    if percent >= 50:
        return "MAJOR_IMPROVEMENT_NEEDED"
    return "UNSATISFACTORY"


def vocabulary() -> dict[str, Any]:
    """The whole dropdown set, shaped for the UI so the options are defined in
    exactly one place and the client cannot drift from the server."""
    return {
        "grades": [
            {
                "code": g,
                "label": GRADE_LABEL[g],
                "score": GRADE_TO_SCORE[g],
                "value": GRADE_TO_VALUE[g],
            }
            for g in GRADES
        ],
        "statuses": [
            {
                "code": s,
                "label": STATUS_LABEL[s],
                "isRepeat": s in REPEAT_STATUSES,
                "isNotApplicable": s in NOT_APPLICABLE_STATUSES,
            }
            for s in STATUSES
        ],
        "riskGrades": [{"code": r, "label": RISK_GRADE_LABEL[r]} for r in RISK_GRADES],
        "requirementTypes": [
            {"code": r, "label": REQUIREMENT_TYPE_LABEL[r]} for r in REQUIREMENT_TYPES
        ],
        "scoreObtainedChoices": list(SCORE_OBTAINED_CHOICES),
        "fullScore": FULL_SCORE,
        # The narrowed vocabulary, served alongside the full one rather than
        # instead of it: one audit register can hold checkpoints of both modes
        # (an internal audit and an IMS audit at the same site), so the client
        # needs both sets present and picks per row on `conformanceMode`.
        "conformanceModes": list(CONFORMANCE_MODES),
        "tristate": tristate_vocabulary(),
    }


__all__ = [
    "GRADES", "GRADE_LABEL", "GRADE_EFFECTIVE", "GRADE_SOME_IMPROVEMENT",
    "GRADE_MAJOR_IMPROVEMENT", "GRADE_UNSATISFACTORY", "GRADE_NA",
    "STATUSES", "STATUS_LABEL", "STATUS_COMPLIED", "STATUS_NON_COMPLIANCE",
    "STATUS_REPEATED_NON_COMPLIANCE", "STATUS_NEW_OBSERVATION",
    "STATUS_REPEATED_OBSERVATION", "STATUS_NA", "STATUS_MAS_NA",
    "REPEAT_STATUSES", "NOT_APPLICABLE_STATUSES",
    "CONFORMANCE_MODES", "CONFORMANCE_FULL", "CONFORMANCE_TRISTATE",
    "TRISTATE_VERDICTS", "TRISTATE_LABEL", "TRISTATE_TO_GRADE_STATUS",
    "TRI_CONFORMANCE", "TRI_NON_CONFORMANCE", "TRI_OBSERVATION",
    "normalise_conformance_mode", "normalise_tristate", "tristate_for_status",
    "tristate_vocabulary",
    "RISK_GRADES", "RISK_GRADE_LABEL", "RISK_HIGH", "RISK_MEDIUM", "RISK_LOW",
    "REQUIREMENT_TYPES", "REQUIREMENT_TYPE_LABEL", "REQ_STATUTORY", "REQ_INTERNAL",
    "FULL_SCORE", "SCORE_OBTAINED_CHOICES",
    "GRADE_TO_VALUE", "GRADE_TO_SCORE", "GRADE_TO_STATUS", "RISK_TO_CAPA_SEVERITY",
    "normalise_grade", "normalise_status", "normalise_risk_grade",
    "normalise_requirement_type", "value_for_grade", "allotted_for_grade",
    "suggest_score", "suggest_status", "is_repeat",
    "carries_risk_grade", "requires_risk_grade",
    "capa_severity", "compute_points_score", "band", "vocabulary",
]
