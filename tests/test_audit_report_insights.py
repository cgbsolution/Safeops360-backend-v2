"""Audit report Section 1 insight layer — offline unit tests (house no-DB style).

The layer is a pure function of a report snapshot, so all of it is testable
without a database. What these tests actually protect:

  * **The gate is not re-implemented.** The dial VISUALISES the critical-fail
    rule; it must never become a second copy of it that can disagree with
    `scoring_rules.evaluate`.
  * **Repeat NCs come from the structured Column F verdict**, not from scanning
    observation prose for "previously raised" — the fragile approach the build
    spec proposed before the schema was checked.
  * **Determinism.** The block is hashed into an immutable report. If it were
    not a pure function of its input, a regenerated report would carry a
    different digest and integrity verification would report tampering.
  * **Nothing is invented on thin data**, and an empty pattern list says why.
"""

from __future__ import annotations

import ast
import inspect
import json
import re

from app.services.insights import rules_audit_report as rai
from app.services.insights.rules_audit_report import (
    compute_report_insights,
    resolve_owner_names,
)


# ─── fixtures ───────────────────────────────────────────────────────────────

def _finding(code, *, severity="major", status="FAIL", discipline="EHS", owner="u1",
             observation="Records were incomplete for two of the six months sampled.",
             compliance="NON_COMPLIANCE", req_type="INTERNAL_REQUIREMENT",
             capa=None, state="RESOLVED", rnd=1, repeat=False):
    return {
        "checkpointCode": code, "discipline": discipline, "severity": severity,
        "assessmentStatus": status, "workflowState": state, "round": rnd,
        "ownerId": owner, "question": f"Requirement for {code}?", "observation": observation,
        "standard": "Factories Act 1948", "requirementReference": "S.21",
        "requirementType": req_type, "complianceStatus": compliance, "isRepeat": repeat,
        "capaNumber": capa, "capaStatus": "IN_PROGRESS" if capa else None, "isAdHoc": False,
    }


# Categories carry the POINTS score the product is scored on — three points per
# assessed checkpoint, N/A allotted nothing. The pass-ratio numbers these would
# produce are deliberately DIFFERENT (see
# `test_category_chart_uses_points_not_the_pass_ratio`) so a regression back to
# the ratio cannot pass silently.
_CATEGORIES = [
    # 39 assessed x 3 = 117 allotted; 102 earned -> 87.2%. Pass-ratio would say
    # (31 + 0.5*6) / 39 = 87.2% too — so EHS alone cannot distinguish them.
    {"category_id": "EHS", "category_name": "EHS", "total": 40,
     "passed": 31, "partial": 6, "failed": 2, "na": 1,
     "score_obtained": 102, "score_allotted": 117, "score_pct": 87.2},
    # Nothing assessed: no points allotted at all -> no score, neutral bar.
    {"category_id": "HR", "category_name": "HR", "total": 10,
     "passed": 0, "partial": 0, "failed": 0, "na": 10,
     "score_obtained": 0, "score_allotted": 0, "score_pct": 0.0},
    # 112/117 = 95.7% on points; the pass-ratio would say (38 + 0.5)/39 = 98.7%.
    # That gap is the regression guard.
    {"category_id": "PR", "category_name": "PRODUCTION", "total": 40,
     "passed": 38, "partial": 1, "failed": 0, "na": 1,
     "score_obtained": 112, "score_allotted": 117, "score_pct": 95.7},
]


def _snapshot(findings, *, critical=0, pct=88.0, show_grade=True, passed=False):
    return {
        "overallScorePct": pct, "overallResult": "CRITICAL_NC" if critical else "MAJOR_NC",
        "scoreObtained": 214, "scoreAllotted": 234,
        "criticalFailures": critical, "majorFailures": len(findings), "minorFailures": 0,
        "checkpointsTotal": 120, "checkpointsAssessed": 120,
        "grade": {"showGrade": show_grade, "assessed": 117, "applicable": 117,
                  "assessedPct": 100.0, "threshold": 20, "label": "Graded"},
        "gate": {"band": "CRITICAL_NC", "passed": passed, "explanation": "Gate sentence.",
                 "rules": {}},
        "capaSummary": {"total": 2, "open": 1, "overdue": 1},
        "categoryScores": [dict(c) for c in _CATEGORIES],
        "findings": findings,
    }


def _many(n=8, **kw):
    return [_finding(f"PI-EHS-{i:03d}", **kw) for i in range(1, n + 1)]


# ─── the gate is visualised, never re-derived ───────────────────────────────

def test_dial_is_painted_red_by_a_critical_fail_even_on_a_good_score():
    ins = compute_report_insights(_snapshot(_many(), critical=2, pct=88.0))
    g = ins["gauge"]
    # The BAND is what the percentage earns; displayBand is what gets painted.
    # Keeping them separate is what stops the visual override from silently
    # becoming a second scoring rule.
    assert g["band"] == "amber"
    assert g["displayBand"] == "red"
    assert g["criticalGate"] is True
    assert g["pct"] == 88.0


def test_verdict_fields_are_passed_through_from_scoring_rules_untouched():
    snap = _snapshot(_many(), critical=1)
    snap["gate"]["passed"] = False
    snap["gate"]["explanation"] = "A critical NC fails the audit regardless of percentage."
    g = compute_report_insights(snap)["gauge"]
    assert g["passed"] is False
    assert g["explanation"] == "A critical NC fails the audit regardless of percentage."
    assert g["result"] == "CRITICAL_NC"


def test_no_critical_fail_means_no_banner_rather_than_an_all_clear():
    # An all-clear banner in the same position trains readers to skim past the
    # real one.
    ins = compute_report_insights(_snapshot(_many(), critical=0))
    assert ins["criticalBanner"] is None
    assert ins["gauge"]["displayBand"] == "amber"


def test_banner_counts_and_names_the_critical_checkpoints():
    findings = _many(6) + [
        _finding("PI-EHS-011", severity="critical"),
        _finding("PI-PR-002", severity="critical", discipline="PRODUCTION"),
    ]
    b = compute_report_insights(_snapshot(findings, critical=2))["criticalBanner"]
    assert b["count"] == 2
    assert set(b["codes"]) == {"PI-EHS-011", "PI-PR-002"}


def test_suppressed_grade_hides_the_percentage_but_keeps_the_dial():
    ins = compute_report_insights(_snapshot(_many(), show_grade=False))
    assert ins["gauge"]["pct"] is None
    assert ins["gauge"]["coverageLabel"] == "Graded"


# ─── repeat NC comes from the structured verdict, not from prose ────────────

def test_repeat_is_detected_from_compliance_status_not_observation_text():
    findings = _many(6) + [
        _finding("PI-PR-002", discipline="PRODUCTION",
                 compliance="REPEATED_NON_COMPLIANCE", repeat=True,
                 # Deliberately says nothing about a previous audit: text
                 # matching would miss this, the Column F verdict cannot.
                 observation="Control not operating."),
    ]
    rep = compute_report_insights(_snapshot(findings))["repeats"]
    assert rep is not None and rep["count"] == 1
    assert [i["checkpointCode"] for i in rep["items"]] == ["PI-PR-002"]
    assert rep["items"][0]["statusLabel"] == "Repeated Non Compliance"


def test_prose_mentioning_a_previous_audit_is_not_treated_as_a_repeat():
    # The inverse failure of text matching: a finding that merely REFERS to an
    # earlier audit is not itself a repeat non-conformance.
    findings = _many(6) + [
        _finding("PI-HR-004",
                 observation="The corrective action previously raised at the last audit was "
                             "verified as implemented; a different gap is recorded here.")
    ]
    assert compute_report_insights(_snapshot(findings))["repeats"] is None


def test_no_repeats_yields_no_callout():
    assert compute_report_insights(_snapshot(_many()))["repeats"] is None


# ─── pattern rules ──────────────────────────────────────────────────────────

def _ids(ins):
    return {p["id"] for p in ins["patterns"]}


def test_statutory_exposure_is_ranked_critical():
    findings = _many(6, req_type="STATUTORY_REGULATORY")
    findings += [_finding("PI-HR-001", discipline="HR", req_type="STATUTORY_REGULATORY")]
    ins = compute_report_insights(_snapshot(findings))
    assert "statutory.exposure" in _ids(ins)
    p = next(p for p in ins["patterns"] if p["id"] == "statutory.exposure")
    assert p["severity"] == "critical"
    # Highest severity sorts to the top of page 1.
    assert ins["patterns"][0]["id"] == "statutory.exposure"


def test_owner_concentration_needs_both_a_majority_and_a_floor():
    # 5 of 6 on one owner clears both.
    findings = _many(5, owner="u_anjali") + [_finding("PI-EHS-099", owner="u_other")]
    ins = compute_report_insights(_snapshot(findings))
    assert "owner.major" in _ids(ins)

    # Evenly spread across three owners clears neither.
    spread = [_finding(f"PI-EHS-{i:03d}", owner=f"u{i % 3}") for i in range(1, 10)]
    assert "owner.major" not in _ids(compute_report_insights(_snapshot(spread)))


def test_owner_name_is_resolved_late_and_never_leaks_an_id():
    findings = _many(5, owner="u_anjali") + [_finding("PI-EHS-099", owner="u_other")]
    ins = compute_report_insights(_snapshot(findings))
    p = next(p for p in ins["patterns"] if p["id"] == "owner.major")
    assert "{owner}" in p["headline"]          # pure layer leaves the slot

    resolve_owner_names(ins, {"u_anjali": "Anjali Verma"})
    p = next(p for p in ins["patterns"] if p["id"] == "owner.major")
    assert "Anjali Verma" in p["headline"] and "{owner}" not in p["headline"]
    assert "u_anjali" not in p["headline"]


def test_unresolvable_owner_degrades_to_a_noun_not_a_cuid():
    findings = _many(5, owner="ckz9raw000000") + [_finding("PI-EHS-099", owner="u_other")]
    ins = compute_report_insights(_snapshot(findings))
    resolve_owner_names(ins, {})
    p = next(p for p in ins["patterns"] if p["id"] == "owner.major")
    assert "One owner" in p["headline"]
    assert "ckz9raw000000" not in p["headline"]


def test_capa_coverage_gap_only_counts_severe_failures_without_a_capa():
    findings = _many(4, severity="critical", status="FAIL", capa=None)
    findings += _many(4, severity="minor", status="FAIL", capa=None)
    ins = compute_report_insights(_snapshot(findings))
    p = next(p for p in ins["patterns"] if p["id"] == "capa.gap")
    assert p["refCount"] == 4  # the minors are not "severe"


def test_wording_cluster_requires_repeats_across_more_than_one_discipline():
    shared = ("Requirement only partially met. Evidence exists for the current month but the "
              "review cycle has slipped twice and no corrective action was recorded.")
    # Same wording, all in one discipline -> not a cross-cutting pattern.
    same_disc = [_finding(f"PI-EHS-{i:03d}", observation=shared) for i in range(1, 6)]
    assert not [p for p in compute_report_insights(_snapshot(same_disc))["patterns"]
                if p["id"].startswith("wording.")]

    # Same wording across three disciplines -> reported.
    cross = [_finding("PI-EHS-005", discipline="EHS", observation=shared),
             _finding("PI-HR-005", discipline="HR", observation=shared),
             _finding("PI-PR-005", discipline="PRODUCTION", observation=shared)]
    cross += _many(3, observation="Something else entirely, at sufficient length to be kept.")
    p = next(p for p in compute_report_insights(_snapshot(cross))["patterns"]
             if p["id"].startswith("wording."))
    # Flagged as text-derived so the UI can caveat it — freeform observation
    # text cannot support a claim about a shared ROOT CAUSE.
    assert p["basis"] == "observation_text"
    assert p["refCount"] == 3
    assert "identical observation wording" in p["headline"]


def test_two_equal_sized_wording_groups_produce_one_card_not_two_identical_ones():
    # Two groups of the same size fill a headline template with the same slots,
    # so both cards read word-for-word alike — which a reader can only take as a
    # rendering fault. One card, and the rest counted in its evidence.
    a = "Requirement only partially met and the review cycle has slipped twice this quarter."
    b = "The procedure is defined but is not being followed in practice at two workstations."
    findings = []
    for text, n in ((a, 1), (b, 2)):
        for d in ("EHS", "HR", "PRODUCTION"):
            findings.append(_finding(f"PI-{d}-{n:03d}", discipline=d, observation=text))
    cards = [p for p in compute_report_insights(_snapshot(findings))["patterns"]
             if p["id"].startswith("wording.")]
    assert len(cards) == 1
    assert cards[0]["otherWordingGroups"] == 1
    assert "A further 1 group" in cards[0]["evidence"]


def test_short_boilerplate_observations_do_not_form_a_wording_cluster():
    boiler = [_finding(f"PI-{d}-{i:03d}", discipline=d, observation="Not met.")
              for i, d in enumerate(["EHS", "HR", "PRODUCTION", "EHS", "HR"], start=1)]
    assert not [p for p in compute_report_insights(_snapshot(boiler))["patterns"]
                if p["id"].startswith("wording.")]


def test_punctuation_and_case_do_not_split_a_wording_cluster():
    a = "Requirement only partially met and the review cycle has slipped twice this quarter."
    variants = [
        _finding("PI-EHS-005", discipline="EHS", observation=a),
        _finding("PI-HR-005", discipline="HR", observation=a.upper()),
        _finding("PI-PR-005", discipline="PRODUCTION", observation=a.replace(".", " !")),
    ] + _many(3, observation="A different observation, long enough not to be dropped as boilerplate.")
    p = next(p for p in compute_report_insights(_snapshot(variants))["patterns"]
             if p["id"].startswith("wording."))
    assert p["refCount"] == 3


# ─── thin data ──────────────────────────────────────────────────────────────

def test_thin_data_suppresses_patterns_but_keeps_the_counted_facts():
    # The dial, banner, chart and CAPA strip are re-presentations of counted
    # facts and are true at any n. Only INFERENCE is suppressed — blanking
    # page 1 of a small but valid report would be the worse failure.
    ins = compute_report_insights(_snapshot([_finding("PI-EHS-001")], critical=1))
    assert ins["suppressed"] is True
    assert ins["reason"] == "insufficient_findings"
    assert ins["patterns"] == []
    assert ins["gauge"]["pct"] == 88.0
    assert ins["criticalBanner"] is not None
    assert len(ins["categoryChart"]) == 3


def test_empty_pattern_list_explains_itself():
    ins = compute_report_insights(_snapshot([_finding("PI-EHS-001")]))
    assert ins["patternNote"] and str(rai.MIN_FINDINGS) in ins["patternNote"]


def test_pattern_list_is_capped_and_says_how_many_it_dropped():
    ins = compute_report_insights(_snapshot(
        _many(9, req_type="STATUTORY_REGULATORY", owner="u_anjali", capa=None,
              severity="critical", state="ESCALATED_PM", rnd=2)))
    assert len(ins["patterns"]) <= rai._MAX_PATTERNS
    assert "patternsSuppressedCount" in ins


# ─── charts re-present, they do not recompute ───────────────────────────────

def test_unassessed_discipline_charts_as_neutral_never_as_a_red_zero():
    chart = compute_report_insights(_snapshot(_many()))["categoryChart"]
    hr = next(c for c in chart if c["name"] == "HR")
    # score_pct is a literal 0.0 on this row; charting it would print a red 0%
    # for a discipline nobody assessed. Zero allotted points means no score.
    assert hr["pct"] is None and hr["band"] == "neutral"
    # …and it sorts LAST, not first: absent is not "worst".
    assert chart[-1]["name"] == "HR"


def test_category_chart_is_worst_first():
    chart = compute_report_insights(_snapshot(_many()))["categoryChart"]
    assert [c["name"] for c in chart] == ["EHS", "PRODUCTION", "HR"]


def test_category_chart_uses_points_not_the_pass_ratio():
    """The chart must report the same formula as the headline percentage.

    Both formulas have always been computed and frozen into every snapshot, so
    reaching for the wrong key is a one-word mistake that renders a second,
    contradictory number for the same discipline — Production printed 85.0% on
    the bar and 88.3% in the table directly beneath it. This pins the choice.
    """
    chart = compute_report_insights(_snapshot(_many()))["categoryChart"]
    pr = next(c for c in chart if c["name"] == "PRODUCTION")

    assert pr["pct"] == 95.7                        # points: 112 / 117
    ratio = (pr["passed"] + 0.5 * pr["partial"]) / pr["assessed"] * 100
    assert round(ratio, 1) == 98.7                  # the superseded formula
    assert pr["pct"] != round(ratio, 1)


def test_category_chart_carries_the_arithmetic_behind_each_percentage():
    # A percentage a reader cannot check is a percentage they must trust.
    pr = next(c for c in compute_report_insights(_snapshot(_many()))["categoryChart"]
              if c["name"] == "PRODUCTION")
    assert (pr["scoreObtained"], pr["scoreAllotted"]) == (112, 117)
    assert round(pr["scoreObtained"] / pr["scoreAllotted"] * 100, 1) == pr["pct"]


def test_gauge_carries_the_arithmetic_behind_the_headline():
    g = compute_report_insights(_snapshot(_many()))["gauge"]
    assert (g["scoreObtained"], g["scoreAllotted"]) == (214, 234)


def test_full_outcome_split_is_available_to_renderers():
    # The count line used to print pass and fail only, so it never summed to the
    # total even though partials earn points toward the percentage beside it.
    ehs = next(c for c in compute_report_insights(_snapshot(_many()))["categoryChart"]
               if c["name"] == "EHS")
    assert ehs["passed"] + ehs["partial"] + ehs["failed"] + ehs["na"] == ehs["total"]


def test_capa_strip_totals_come_from_the_capa_summary_not_a_recount():
    # Recounting here would let the strip and Section 7 disagree.
    snap = _snapshot(_many(4, capa="CAPA-1"))
    snap["capaSummary"] = {"total": 9, "open": 4, "overdue": 2}
    strip = compute_report_insights(snap)["capaStrip"]
    assert (strip["total"], strip["open"], strip["overdue"]) == (9, 4, 2)


# ─── determinism / airgap ───────────────────────────────────────────────────

def test_block_is_byte_identical_across_recomputation():
    # The block is hashed into an immutable report. Non-determinism here would
    # make a regenerated report verify as tampered.
    snap = _snapshot(_many(9, owner="u_anjali", req_type="STATUTORY_REGULATORY"), critical=2)
    seen = set()
    for _ in range(10):
        ins = compute_report_insights(snap)
        resolve_owner_names(ins, {"u_anjali": "Anjali Verma"})
        seen.add(json.dumps(ins, sort_keys=True, default=str))
    assert len(seen) == 1


def test_rule_module_does_no_io_and_reads_no_clock():
    src = inspect.getsource(rai)
    tree = ast.parse(src)
    # Strip docstrings: the module docstring legitimately NAMES AsyncSession to
    # explain that it deliberately does not take one.
    body = [n for n in tree.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]
    code = ast.unparse(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])))

    assert not re.search(r"\b(requests|httpx|urllib|aiohttp|openai|anthropic|socket)\b", code)
    assert not re.search(r"datetime\.now|time\.time|utcnow", code)
    assert "AsyncSession" not in code
    assert not re.search(r"^\s*(from|import)\s+(sqlalchemy|app\.models)", src, re.M)
