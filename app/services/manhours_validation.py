"""Manhours submission validator.

Port of `lib/manhours/validation.ts`. This is the gate between a draft and a
filed statutory return, so the levels mean something specific:

  FAIL — blocks submission. The numbers are internally inconsistent or
         impossible; filing them would put a wrong figure on the record.
  WARN — allowed, but the submitter must explain it in the notes. Reserved for
         things that are plausible but unusual (a shutdown month, a new
         contractor), where the right response is context, not correction.
  INFO — observation only.

Thresholds are the ones the TypeScript used, unchanged. They encode real
operational judgement — 176 hrs/employee is 22 working days × 8 hrs, and the
5-15% deduction band is what a normal month's leave looks like — so they are
named constants rather than inline magic numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Float tolerance for "these two totals agree". Hours are entered to 2dp, so
# anything under a hundredth of an hour is rounding, not a discrepancy.
EPS = 0.01

# Deductions outside 5-15% of gross are unusual but legitimate (commissioning
# months, festival shutdowns) — hence WARN rather than FAIL.
DEDUCTION_PCT_MIN = 5.0
DEDUCTION_PCT_MAX = 15.0

# 22 working days × 8 hrs = 176; overtime typically brings it to ~190.
HOURS_PER_HEAD_NORMAL_MIN = 150.0
HOURS_PER_HEAD_NORMAL_MAX = 220.0
# Outside this, it is a data-entry error rather than an unusual month.
HOURS_PER_HEAD_ABSURD_MIN = 50.0
HOURS_PER_HEAD_ABSURD_MAX = 400.0

# Month-on-month swing that needs explaining.
PRIOR_DEVIATION_PCT = 30.0

Level = Literal["INFO", "WARN", "FAIL"]


@dataclass
class ValidationIssue:
    level: Level
    code: str
    message: str
    details: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)
    canSubmit: bool = True
    summary: dict[str, int] = field(default_factory=lambda: {"info": 0, "warn": 0, "fail": 0})

    def to_dict(self) -> dict[str, Any]:
        return {
            "issues": [i.to_dict() for i in self.issues],
            "canSubmit": self.canSubmit,
            "summary": self.summary,
        }


def _sum_hours(categories: list[dict[str, Any]], kind: str) -> float:
    return sum(float(c.get("totalHours") or 0) for c in categories if c.get("categoryType") == kind)


def validate_submission(
    submission: dict[str, Any],
    categories: list[dict[str, Any]],
    prior_months: list[dict[str, Any]],
) -> ValidationReport:
    """Run every check. `submission` is the roll-up row, `categories` its rows,
    `prior_months` the recent LOCKED returns for the same plant."""
    issues: list[ValidationIssue] = []

    def f(key: str) -> float:
        return float(submission.get(key) or 0)

    # ── Declared totals must equal the sum of their rows ──────────────
    for kind, code, label in (
        ("PERMANENT", "TOTAL_MATCH_PERMANENT", "Permanent total doesn't match sum of department rows"),
        ("CONTRACT", "TOTAL_MATCH_CONTRACT", "Contract total doesn't match sum of contractor company rows"),
        ("TRAINEE", "TOTAL_MATCH_TRAINEE", "Trainee total doesn't match sum of trainee rows"),
    ):
        declared = f(f"totalManhours{kind.capitalize()}")
        rows_sum = _sum_hours(categories, kind)
        if abs(declared - rows_sum) > EPS:
            issues.append(
                ValidationIssue(
                    "FAIL", code, label,
                    f"Declared {declared:.2f} hrs vs rows sum to {rows_sum:.2f} hrs",
                )
            )

    computed_all = f("totalManhoursPermanent") + f("totalManhoursContract") + f("totalManhoursTrainee")
    if abs(f("totalManhoursAll") - computed_all) > EPS:
        issues.append(
            ValidationIssue(
                "FAIL", "TOTAL_MATCH_ALL",
                "Grand total doesn't equal permanent + contract + trainee",
                f"Declared {f('totalManhoursAll'):.2f} hrs vs computed {computed_all:.2f} hrs",
            )
        )

    # ── Deductions ───────────────────────────────────────────────────
    deduction_fields = (
        ("Annual leave", "hoursAnnualLeave"),
        ("Sick leave", "hoursSickLeave"),
        ("Off-job training", "hoursTraining"),
        ("Maternity leave", "hoursMaternityLeave"),
        ("Other", "hoursOther"),
    )
    negative = [label for label, key in deduction_fields if f(key) < 0]
    if negative:
        issues.append(
            ValidationIssue(
                "FAIL", "DEDUCTIONS_NEGATIVE", "Deduction values cannot be negative",
                f"Negative: {', '.join(negative)}",
            )
        )

    deduction_sum = sum(f(key) for _label, key in deduction_fields)
    if abs(f("hoursDeductionsTotal") - deduction_sum) > EPS:
        issues.append(
            ValidationIssue(
                "FAIL", "DEDUCTIONS_TOTAL_MISMATCH",
                "Deduction total doesn't match sum of individual deductions",
                f"Stored {f('hoursDeductionsTotal'):.2f} vs computed {deduction_sum:.2f}",
            )
        )

    gross = f("totalManhoursAll")
    if gross > 0:
        pct = (f("hoursDeductionsTotal") / gross) * 100
        if not (DEDUCTION_PCT_MIN <= pct <= DEDUCTION_PCT_MAX):
            issues.append(
                ValidationIssue(
                    "WARN", "DEDUCTIONS_REASONABLE",
                    "Deductions unusually low (<5%)" if pct < DEDUCTION_PCT_MIN
                    else "Deductions unusually high (>15%)",
                    f"Deductions are {pct:.1f}% of gross hours; typical range is 5-15%",
                )
            )

    # ── Hours per head ───────────────────────────────────────────────
    strength = f("totalEmployeeStrength")
    if strength > 0 and f("totalManhoursPermanent") > 0:
        per_head = f("totalManhoursPermanent") / strength
        if per_head < HOURS_PER_HEAD_ABSURD_MIN or per_head > HOURS_PER_HEAD_ABSURD_MAX:
            issues.append(
                ValidationIssue(
                    "FAIL", "HOURS_PER_EMPLOYEE_ABSURD",
                    "Hours per employee is outside any plausible range",
                    f"{per_head:.1f} hrs/employee for {int(strength)} permanent staff "
                    "— check headcount or hours entry",
                )
            )
        elif per_head < HOURS_PER_HEAD_NORMAL_MIN or per_head > HOURS_PER_HEAD_NORMAL_MAX:
            issues.append(
                ValidationIssue(
                    "WARN", "HOURS_PER_EMPLOYEE_REASONABLE",
                    "Hours per employee outside normal range",
                    f"{per_head:.1f} hrs/employee; typical month = 175-200 hrs",
                )
            )

    # ── Declared strength without supporting rows ────────────────────
    kinds = {c.get("categoryType") for c in categories}
    if f("totalEmployeeStrength") > 0 and "PERMANENT" not in kinds:
        issues.append(
            ValidationIssue(
                "FAIL", "MISSING_PERMANENT_ROWS",
                "Permanent strength declared but no department rows entered",
                f"{int(f('totalEmployeeStrength'))} permanent staff declared in Step 1; "
                "add at least one department row in Step 2",
            )
        )
    if f("totalContractorStrength") > 0 and "CONTRACT" not in kinds:
        issues.append(
            ValidationIssue(
                "FAIL", "MISSING_CONTRACT_ROWS",
                "Contractor strength declared but no contractor company rows entered",
                f"{int(f('totalContractorStrength'))} contract staff declared in Step 1; "
                "add at least one contractor row in Step 3",
            )
        )
    # Trainees are optional — only a problem if hours exist without rows.
    if f("totalManhoursTrainee") > 0 and "TRAINEE" not in kinds:
        issues.append(
            ValidationIssue(
                "FAIL", "MISSING_TRAINEE_ROWS",
                "Trainee hours declared but no trainee rows entered",
                "Add per-department breakdown in Step 4 or set trainee total to 0",
            )
        )

    # ── Net exposure: the KPI denominator ────────────────────────────
    if f("netExposureHours") <= 0:
        issues.append(
            ValidationIssue(
                "FAIL", "NET_EXPOSURE_NONPOSITIVE",
                "Net exposure hours must be greater than zero",
                "All KPIs (LTIFR, TRIR, Severity) divide by this number — "
                "submitting zero would make them undefined.",
            )
        )
    expected_net = f("totalManhoursAll") - f("hoursDeductionsTotal")
    if abs(f("netExposureHours") - expected_net) > EPS:
        issues.append(
            ValidationIssue(
                "FAIL", "NET_EXPOSURE_FORMULA_MISMATCH",
                "Net exposure hours don't match (gross − deductions)",
                f"Stored {f('netExposureHours'):.2f} vs computed {expected_net:.2f} "
                "— re-save Steps 2-6 to refresh totals",
            )
        )

    # ── Comparison against recent months ─────────────────────────────
    if prior_months:
        mean_prior = sum(float(p.get("netExposureHours") or 0) for p in prior_months) / len(prior_months)
        if mean_prior > 0:
            delta = ((f("netExposureHours") - mean_prior) / mean_prior) * 100
            if abs(delta) > PRIOR_DEVIATION_PCT:
                issues.append(
                    ValidationIssue(
                        "WARN", "PRIOR_DEVIATION",
                        "Net exposure hours markedly higher than recent months" if delta > 0
                        else "Net exposure hours markedly lower than recent months",
                        f"{'+' if delta > 0 else ''}{delta:.1f}% vs prior {len(prior_months)}-month "
                        f"average ({mean_prior:.0f} hrs). Add notes if this reflects a real "
                        "operational change.",
                    )
                )

        seen_departments = {n for p in prior_months for n in (p.get("departmentNames") or [])}
        current_departments = {
            c.get("departmentName") for c in categories if c.get("departmentName")
        }
        novel_departments = current_departments - seen_departments
        if novel_departments and seen_departments:
            issues.append(
                ValidationIssue(
                    "WARN", "NEW_DEPARTMENT",
                    f"{len(novel_departments)} department"
                    f"{'' if len(novel_departments) == 1 else 's'} not seen in recent months",
                    ", ".join(sorted(str(d) for d in novel_departments)),
                )
            )

        seen_contractors = {n for p in prior_months for n in (p.get("contractorNames") or [])}
        current_contractors = {
            c.get("contractorName") for c in categories if c.get("contractorName")
        }
        novel_contractors = current_contractors - seen_contractors
        if novel_contractors and seen_contractors:
            issues.append(
                ValidationIssue(
                    "WARN", "NEW_CONTRACTOR",
                    f"{len(novel_contractors)} contractor"
                    f"{'' if len(novel_contractors) == 1 else 's'} not seen in recent months",
                    ", ".join(sorted(str(c) for c in novel_contractors)),
                )
            )

    # ── Cross-cut, runs last so it can see everything above ──────────
    # A WARN is "unusual but possibly correct". The submitter has to say which.
    if any(i.level == "WARN" for i in issues) and not (submission.get("submissionNotes") or "").strip():
        issues.append(
            ValidationIssue(
                "FAIL", "NOTES_REQUIRED_ON_DEVIATIONS",
                "Add submission notes explaining the flagged deviations",
                "One or more warnings were raised; notes are mandatory so the reviewer "
                "knows whether this is a real operational change.",
            )
        )

    summary = {
        "info": sum(1 for i in issues if i.level == "INFO"),
        "warn": sum(1 for i in issues if i.level == "WARN"),
        "fail": sum(1 for i in issues if i.level == "FAIL"),
    }
    return ValidationReport(issues=issues, canSubmit=summary["fail"] == 0, summary=summary)
