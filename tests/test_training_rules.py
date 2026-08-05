"""Unit tests for the Training & Competency Engine rule core (spec §B).

The four rule types are pure functions (no DB/UI), so they test directly — the
house pattern for a deterministic rules engine (cf. tests/test_alert_rules.py,
tests/test_insights_engine.py). These lock the business-rule invariants:
  • role-scoping can NEVER produce a blanket site-wide assignment
  • severity-rule assignments are mandatory + non-dismissible + escalated
  • all thresholds/windows come from config, never hardcoded
  • the rule core is vendor/content agnostic (keys only on competencyId)
"""

from __future__ import annotations

import dataclasses

from app.services.training_engine.rules import (
    AssignmentDraft,
    PersonEventStat,
    RecordDueRef,
    RuleConfigView,
    apply_role_scoping,
    event_weight,
    person_involvement_rule,
    recert_rule,
    severity_rule,
    severity_rank,
    threshold_rule,
)

CFG = RuleConfigView.defaults()  # thresholdCount=3, window=90, severityThreshold=HIGH, ...


# ── RULE 3: role/exposure scoping — the "never blanket" guarantee ────────────
def test_scoping_filters_candidates_to_role_required_only():
    res = apply_role_scoping(candidate_worker_ids=["a", "b", "c"], requiring_worker_ids=["a", "c"])
    assert set(res.scoped_worker_ids) == {"a", "c"}
    assert res.rejected_count == 1  # "b" dropped — role doesn't require it
    assert res.scoping_failed is False


def test_scoping_blanket_none_collapses_to_role_required():
    # candidate=None models "the whole roster" — must collapse to the required set.
    res = apply_role_scoping(candidate_worker_ids=None, requiring_worker_ids=["a", "b"])
    assert set(res.scoped_worker_ids) == {"a", "b"}


def test_scoping_fails_when_no_role_requires_competency():
    res = apply_role_scoping(candidate_worker_ids=["a", "b"], requiring_worker_ids=[])
    assert res.scoping_failed is True
    assert res.scoped_worker_ids == []


# ── RULE 1: threshold ────────────────────────────────────────────────────────
def test_threshold_below_count_produces_nothing():
    out = threshold_rule(
        competency_id="C1", plant_id="P1", department_id=None,
        matched_record_count=2, requiring_worker_ids=["w1", "w2"],
        already_covered_ids=set(), config=CFG,
    )
    assert out.drafts == [] and out.flags == []


def test_threshold_at_count_assigns_role_required_workers_only():
    out = threshold_rule(
        competency_id="C1", plant_id="P1", department_id="D1",
        matched_record_count=3, requiring_worker_ids=["w1", "w2"],
        already_covered_ids={"w2"}, config=CFG, trigger_mapping_id="M1",
    )
    assert {d.personUserId for d in out.drafts} == {"w1"}  # w2 already covered
    d = out.drafts[0]
    assert d.source == "threshold_rule" and d.dismissible is True and d.isMandatory is False
    assert d.competencyId == "C1" and d.triggerMappingId == "M1"
    assert d.provenance["thresholdCount"] == 3 and d.provenance["matchedRecordCount"] == 3


def test_threshold_never_blankets_flags_instead():
    # Threshold reached but NO role requires it → a flag, never an assignment.
    out = threshold_rule(
        competency_id="C1", plant_id="P1", department_id=None,
        matched_record_count=9, requiring_worker_ids=[],
        already_covered_ids=set(), config=CFG,
    )
    assert out.drafts == []
    assert len(out.flags) == 1 and out.flags[0].reason == "scoping_failed"


def test_threshold_is_config_driven_not_hardcoded():
    strict = RuleConfigView(thresholdCount=5)
    out = threshold_rule(
        competency_id="C1", plant_id="P1", department_id=None,
        matched_record_count=3, requiring_worker_ids=["w1"],
        already_covered_ids=set(), config=strict,
    )
    assert out.drafts == []  # 3 < 5 under the stricter config


# ── RULE 2: severity (SIF-potential) ─────────────────────────────────────────
def _sev_cls(**over):
    base = {"severity": "MEDIUM", "sifPotential": False, "involvedUserIds": ["v1"]}
    base.update(over)
    return base


def test_severity_ignores_non_serious():
    out = severity_rule(
        classification=_sev_cls(severity="MEDIUM"), mapped_competency_ids=["C1"],
        plant_id="P1", source_module="INCIDENT", source_record_id="i1", source_record_ref="INC-1", config=CFG,
    )
    assert out.drafts == [] and out.flags == []


def test_severity_sif_creates_mandatory_nondismissible_escalated():
    out = severity_rule(
        classification=_sev_cls(sifPotential=True, involvedUserIds=["v1", "v2"]),
        mapped_competency_ids=["C1"], plant_id="P1", source_module="INCIDENT",
        source_record_id="i1", source_record_ref="INC-1", config=CFG,
        mapping_by_competency={"C1": "M9"},
    )
    assert {d.personUserId for d in out.drafts} == {"v1", "v2"}
    for d in out.drafts:
        assert d.source == "severity_rule"
        assert d.isMandatory is True and d.dismissible is False and d.escalationFlag is True
        assert d.triggerMappingId == "M9" and d.sourceRecordRef == "INC-1"


def test_severity_threshold_is_configurable():
    hi = _sev_cls(severity="HIGH", sifPotential=False)
    # default threshold HIGH → serious
    assert severity_rule(classification=hi, mapped_competency_ids=["C1"], plant_id="P1",
                         source_module="INCIDENT", source_record_id="i", source_record_ref="R", config=CFG).drafts
    # threshold raised to CRITICAL → HIGH no longer serious
    crit_cfg = RuleConfigView(severityThreshold="CRITICAL")
    assert not severity_rule(classification=hi, mapped_competency_ids=["C1"], plant_id="P1",
                            source_module="INCIDENT", source_record_id="i", source_record_ref="R", config=crit_cfg).drafts


def test_severity_disabled_by_config():
    off = RuleConfigView(severitySifImmediate=False)
    out = severity_rule(classification=_sev_cls(sifPotential=True), mapped_competency_ids=["C1"],
                        plant_id="P1", source_module="INCIDENT", source_record_id="i", source_record_ref="R", config=off)
    assert out.drafts == []


def test_severity_serious_but_no_involved_worker_flags_not_blankets():
    out = severity_rule(
        classification=_sev_cls(sifPotential=True, involvedUserIds=[]),
        mapped_competency_ids=["C1"], plant_id="P1", source_module="OBSERVATION",
        source_record_id="o1", source_record_ref="OBS-1", config=CFG,
    )
    assert out.drafts == []
    assert out.flags and out.flags[0].reason == "no_involved_workers"


def test_severity_serious_but_no_mapping_flags():
    out = severity_rule(
        classification=_sev_cls(sifPotential=True), mapped_competency_ids=[],
        plant_id="P1", source_module="INCIDENT", source_record_id="i1", source_record_ref="INC-1", config=CFG,
    )
    assert out.flags and out.flags[0].reason == "no_mapping"


def test_severity_rank_ordering():
    assert severity_rank("CRITICAL") > severity_rank("HIGH") > severity_rank("MEDIUM") > severity_rank("LOW")
    assert severity_rank(None) == 0


# ── RULE 4: recertification ──────────────────────────────────────────────────
def test_recert_assigns_due_records_and_dedupes():
    due = [
        RecordDueRef(personUserId="w1", competencyId="C1", plantId="P1"),
        RecordDueRef(personUserId="w2", competencyId="C1", plantId="P1"),
    ]
    out = recert_rule(records_due=due, already_covered_ids={("w2", "C1")}, config=CFG)
    assert {d.personUserId for d in out.drafts} == {"w1"}
    assert out.drafts[0].source == "recert_rule" and out.drafts[0].dismissible is True


# ── content/vendor decoupling: the rule core keys ONLY on competencyId ───────
def test_rule_core_is_content_and_vendor_agnostic():
    fields = {f.name for f in dataclasses.fields(AssignmentDraft)}
    # The draft (rule output) must not carry ANY content-type / vendor concept.
    assert "competencyId" in fields
    assert not (fields & {"contentType", "vendorId", "deliveryMode", "contentRef", "vendorName"})


# ── RULE 5: person-risk (repeat-involvement flag) ────────────────────────────
def _stat(**over):
    base = dict(personUserId="w1", plantId="P1")
    base.update(over)
    return PersonEventStat(**base)


def test_person_not_flagged_when_below_threshold_and_low_score():
    st = _stat(incidentCount=1, severityWeight=1.0)  # 1 event, threshold default 2
    res = person_involvement_rule(stats=st, config=CFG)
    assert res.flagged is False and res.riskBand == "none"


def test_person_flagged_on_repeat_involvement():
    # 2 incidents "against their name" → flagged by count (spec's core ask).
    st = _stat(incidentCount=2, severityWeight=3.0)
    res = person_involvement_rule(stats=st, config=CFG)
    assert res.flagged is True and res.totalEvents == 2
    assert any("2 events" in r for r in res.reasons)


def test_person_flagged_immediately_on_sif_even_with_one_event():
    st = _stat(incidentCount=1, sifCount=1, severityWeight=8.0)
    res = person_involvement_rule(stats=st, config=CFG)
    assert res.flagged is True and res.riskBand in ("high", "critical")


def test_person_risk_bands_are_config_driven():
    # score 6 → high (default cutoffs elevated=3, high=6, critical=10)
    assert person_involvement_rule(stats=_stat(nearMissCount=3, severityWeight=6.0), config=CFG).riskBand == "high"
    assert person_involvement_rule(stats=_stat(incidentCount=2, severityWeight=10.0), config=CFG).riskBand == "critical"
    # raise the critical cutoff → same score is only 'high'
    lax = RuleConfigView(personRiskCritical=99)
    assert person_involvement_rule(stats=_stat(incidentCount=2, severityWeight=10.0), config=lax).riskBand == "high"


def test_person_flagged_by_count_but_low_score_is_elevated_not_none():
    st = _stat(observationCount=2, severityWeight=1.0)  # 2 events, score below elevated cutoff
    res = person_involvement_rule(stats=st, config=CFG)
    assert res.flagged is True and res.riskBand == "elevated"


def test_event_weight_orders_incident_over_nearmiss_and_observation():
    # An incident weighs strictly more than a near miss / observation at the same
    # severity; near-miss and observation are the lighter tier (may tie).
    assert event_weight("INCIDENT", "HIGH") > event_weight("NEAR_MISS", "HIGH")
    assert event_weight("NEAR_MISS", "HIGH") >= event_weight("OBSERVATION", "HIGH")
    assert event_weight("INCIDENT", "CRITICAL") > event_weight("OBSERVATION", "CRITICAL")
    assert event_weight("INCIDENT", "HIGH", sif=True) == 2 * event_weight("INCIDENT", "HIGH")
