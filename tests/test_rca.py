"""ERM Cross-Domain RCA — pure unit tests for the causal-analytics core + helpers.

These cover the computational heart of the client's ask (occurrences / risk_reach /
domain_spread / category rollup / recurring-driver) with no DB, so they're fast and
deterministic. DB-backed behaviours (origination Paths A/B/C, contributing-causes,
domain-filter, tenant isolation, raise-CAPA, soft-delete, audit) are proven against
the seeded database by verify_rca.py (run during verification).

Test-id map:
  RCA-T04 → test_single_origin_guard
  RCA-T05 → test_subcause_requires_parent_category
  RCA-T06 → test_domain_scoped_subcause_picker
  RCA-T07 → test_cross_domain_subcauses_roll_up_to_same_category
  RCA-T08 → test_occurrences_and_risk_reach
  RCA-T09 → test_domain_spread
  RCA-T10 → test_category_rollup_sums
  RCA-T12 → test_recurring_driver_threshold
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.schemas.rca import SubCauseUpsert
from app.services.rca import generate_rca_summary, is_empty_rca_data, normalise_rca_method
from app.services.rca_analytics import RECURRING_DRIVER_THRESHOLD, aggregate_cause_metrics
from app.services.rca_core import CATEGORY_CODE_TO_DOMAIN, assert_single_origin
from app.services.rca_taxonomy import filter_subcauses_by_domain


# ── builders ──────────────────────────────────────────────────────────────────
def _rca(rid, domain, causes, risk_ids):
    """causes: list of (subCauseId, enterpriseCategoryId); risk_ids: list of risk ids."""
    return SimpleNamespace(
        id=rid,
        primaryDomain=domain,
        identifiedCauses=[SimpleNamespace(subCauseId=s, enterpriseCategoryId=c) for s, c in causes],
        riskLinks=[SimpleNamespace(riskId=r) for r in risk_ids],
    )


def _cats(*codes):
    return {f"cat-{c}": SimpleNamespace(code=c, name=f"{c} category", colorHex="#123456") for c in codes}


def _subs(mapping):
    # mapping: subId -> categoryId
    return {s: SimpleNamespace(code=s.upper(), name=s, categoryId=c) for s, c in mapping.items()}


# ── RCA-T04 ─────────────────────────────────────────────────────────────────
def test_single_origin_guard():
    # exactly one → ok
    assert_single_origin("RISK", None, "risk-1", None)
    assert_single_origin("LOSS_EVENT", None, None, "loss-1")
    assert_single_origin("EVENT", "inc-1", None, None)
    # zero set → reject
    with pytest.raises(ValueError):
        assert_single_origin("RISK", None, None, None)
    # two set → reject
    with pytest.raises(ValueError):
        assert_single_origin("RISK", None, "risk-1", "loss-1")
    # mismatch between originType and the set reference → reject
    with pytest.raises(ValueError):
        assert_single_origin("RISK", "inc-1", None, None)


# ── RCA-T05 ─────────────────────────────────────────────────────────────────
def test_subcause_requires_parent_category():
    # a sub-cause with a category is valid
    ok = SubCauseUpsert(categoryId="cat-GOV", code="GOV-X", name="X")
    assert ok.categoryId == "cat-GOV"
    # a sub-cause with NO parent category is rejected at the schema boundary
    with pytest.raises(Exception):
        SubCauseUpsert(code="GOV-X", name="X")  # type: ignore[call-arg]


# ── RCA-T06 ─────────────────────────────────────────────────────────────────
def test_domain_scoped_subcause_picker():
    rows = [
        SimpleNamespace(code="LOTO", applicableDomains=["OPERATIONAL"]),
        SimpleNamespace(code="HEDGE", applicableDomains=["FINANCIAL"]),
        SimpleNamespace(code="GOV", applicableDomains=[]),  # universal
    ]
    fin = {s.code for s in filter_subcauses_by_domain(rows, "FINANCIAL")}
    assert "HEDGE" in fin and "GOV" in fin and "LOTO" not in fin
    ops = {s.code for s in filter_subcauses_by_domain(rows, "OPERATIONAL")}
    assert "LOTO" in ops and "GOV" in ops and "HEDGE" not in ops


# ── RCA-T07 ─────────────────────────────────────────────────────────────────
def test_cross_domain_subcauses_roll_up_to_same_category():
    # an operational sub-cause and a compliance sub-cause both under GOV category
    cats = _cats("GOV")
    subs = _subs({"ops-sub": "cat-GOV", "cmp-sub": "cat-GOV"})
    rcas = [
        _rca("r1", "OPERATIONAL", [("ops-sub", "cat-GOV")], ["risk-1"]),
        _rca("r2", "COMPLIANCE", [("cmp-sub", "cat-GOV")], ["risk-2"]),
    ]
    _, categories = aggregate_cause_metrics(rcas, cats, subs)
    gov = next(c for c in categories if c["enterpriseCategoryId"] == "cat-GOV")
    assert gov["subCauseCount"] == 2
    assert gov["domainSpread"] == 2  # operational + compliance under one category
    assert gov["riskReach"] == 2


# ── RCA-T08 ─────────────────────────────────────────────────────────────────
def test_occurrences_and_risk_reach():
    cats = _cats("GOV")
    subs = _subs({"iso": "cat-GOV"})
    # 7 RCAs each cite "iso"; collectively they link to 3 distinct risks
    risk_rotation = [["risk-1"], ["risk-2"], ["risk-3"], ["risk-1", "risk-2"],
                     ["risk-2", "risk-3"], ["risk-1"], ["risk-3"]]
    rcas = [_rca(f"r{i}", "OPERATIONAL", [("iso", "cat-GOV")], risk_rotation[i]) for i in range(7)]
    causes, _ = aggregate_cause_metrics(rcas, cats, subs)
    iso = next(c for c in causes if c["subCauseId"] == "iso")
    assert iso["occurrences"] == 7
    assert iso["riskReach"] == 3
    assert iso["rcaCount"] == 7


# ── RCA-T09 ─────────────────────────────────────────────────────────────────
def test_domain_spread():
    cats = _cats("GOV")
    subs = _subs({"gov": "cat-GOV"})
    rcas = [
        _rca("r1", "OPERATIONAL", [("gov", "cat-GOV")], ["risk-1"]),
        _rca("r2", "COMPLIANCE", [("gov", "cat-GOV")], ["risk-2"]),
        _rca("r3", "FINANCIAL", [("gov", "cat-GOV")], ["risk-3"]),
    ]
    causes, _ = aggregate_cause_metrics(rcas, cats, subs)
    gov = next(c for c in causes if c["subCauseId"] == "gov")
    assert gov["domainSpread"] == 3
    assert set(gov["domains"]) == {"OPERATIONAL", "COMPLIANCE", "FINANCIAL"}


# ── RCA-T10 ─────────────────────────────────────────────────────────────────
def test_category_rollup_sums():
    cats = _cats("GOV")
    subs = _subs({"a": "cat-GOV", "b": "cat-GOV"})
    rcas = [
        _rca("r1", "OPERATIONAL", [("a", "cat-GOV")], ["risk-1", "risk-2"]),
        _rca("r2", "FINANCIAL", [("b", "cat-GOV")], ["risk-2", "risk-3"]),
        _rca("r3", "FINANCIAL", [("a", "cat-GOV")], ["risk-3"]),
    ]
    causes, categories = aggregate_cause_metrics(rcas, cats, subs)
    gov = next(c for c in categories if c["enterpriseCategoryId"] == "cat-GOV")
    # occurrences sum across sub-causes = 3 citations (a×2, b×1)
    assert gov["occurrences"] == 3
    # distinct risks across the category = {1,2,3}
    assert gov["riskReach"] == 3
    assert gov["subCauseCount"] == 2


# ── RCA-T12 ─────────────────────────────────────────────────────────────────
def test_recurring_driver_threshold():
    cats = _cats("GOV")
    subs = _subs({"hot": "cat-GOV", "single": "cat-GOV", "narrow": "cat-GOV"})
    rcas = [
        # "hot": reach 2, occurrences 2 → recurring
        _rca("r1", "OPERATIONAL", [("hot", "cat-GOV")], ["risk-1"]),
        _rca("r2", "OPERATIONAL", [("hot", "cat-GOV")], ["risk-2"]),
        # "single": occurrences 1 → not recurring even if it reaches 2 risks
        _rca("r3", "OPERATIONAL", [("single", "cat-GOV")], ["risk-1", "risk-2"]),
        # "narrow": occurrences 3 but only 1 distinct risk → not recurring
        _rca("r4", "OPERATIONAL", [("narrow", "cat-GOV")], ["risk-9"]),
        _rca("r5", "OPERATIONAL", [("narrow", "cat-GOV")], ["risk-9"]),
        _rca("r6", "OPERATIONAL", [("narrow", "cat-GOV")], ["risk-9"]),
    ]
    causes, _ = aggregate_cause_metrics(rcas, cats, subs, threshold=RECURRING_DRIVER_THRESHOLD)
    by = {c["subCauseId"]: c for c in causes}
    assert by["hot"]["isRecurringDriver"] is True
    assert by["single"]["isRecurringDriver"] is False
    assert by["narrow"]["isRecurringDriver"] is False


# ── supporting helpers ─────────────────────────────────────────────────────
def test_category_code_to_domain_mapping():
    assert CATEGORY_CODE_TO_DOMAIN["OPS"] == "OPERATIONAL"
    assert CATEGORY_CODE_TO_DOMAIN["FIN"] == "FINANCIAL"
    assert CATEGORY_CODE_TO_DOMAIN["CMP"] == "COMPLIANCE"
    assert CATEGORY_CODE_TO_DOMAIN["TEC"] == "CYBER"
    # the 8 canonical domains are all reachable from the 10 seeded category codes
    assert set(CATEGORY_CODE_TO_DOMAIN.values()) >= {
        "OPERATIONAL", "FINANCIAL", "COMPLIANCE", "EXTERNAL",
        "REPUTATIONAL", "CYBER", "STRATEGIC", "ESG",
    }


def test_narrative_methodology_supported():
    assert normalise_rca_method("Narrative") == "NARRATIVE"
    assert normalise_rca_method("NARRATIVE") == "NARRATIVE"
    empty = {"summary": "", "factors": []}
    full = {"summary": "Brand exposure escalated on social media.",
            "factors": [{"description": "No social-listening control"}]}
    assert is_empty_rca_data("NARRATIVE", empty) is True
    assert is_empty_rca_data("NARRATIVE", full) is False
    summary = generate_rca_summary("NARRATIVE", full)
    assert summary and "1 contributing factor" in summary
