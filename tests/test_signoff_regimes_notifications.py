"""WP-41 sign-off · WP-43 notifications · WP-47 buyer regimes.

Design: [docs/cams/09-module-completion.md](../../docs/cams/09-module-completion.md)
§3.1, §3.3, §3.7.

Pure cores only — the house style. The behaviours pinned here are the ones where
a wrong default would be silently harmful rather than obviously broken.
"""

from __future__ import annotations

import pytest

from app.services.cams_notifications import (
    CATALOGUE,
    DEFAULT_FREQUENCY,
    IMMEDIATE_EMAIL,
    deep_link,
    render_digest_text,
)
from app.services.regimes import (
    AUTHORSHIP_DISCLAIMER,
    REGIMES,
    get_regime,
    list_regimes,
    native_bucket,
    native_criticality,
    regime_ready,
)
from app.services.signoff import (
    REQUIRED_FOR_CLOSURE,
    SIGNOFF_ROLES,
    validate_signature,
)

# ── WP-41 sign-off ───────────────────────────────────────────────────


def test_closure_requires_both_an_auditor_and_an_auditee_signature():
    """One-sided sign-off is not acceptance. A lead auditor signing their own
    work proves nothing about whether the site agreed."""
    assert set(REQUIRED_FOR_CLOSURE) == {"LEAD_AUDITOR", "AUDITEE_OWNER"}
    assert all(r in SIGNOFF_ROLES for r in REQUIRED_FOR_CLOSURE)


def test_drawn_signature_must_be_an_image_data_uri():
    validate_signature("DRAWN", "data:image/png;base64,AAAA", None)
    with pytest.raises(ValueError):
        validate_signature("DRAWN", "not-an-image", None)
    with pytest.raises(ValueError):
        validate_signature("DRAWN", None, "A Name")


def test_oversized_signature_is_rejected():
    """A pathological canvas would otherwise bloat the immutable snapshot."""
    with pytest.raises(ValueError):
        validate_signature("DRAWN", "data:image/png;base64," + "A" * 300_000, None)


def test_typed_signature_requires_an_actual_name():
    validate_signature("TYPED", None, "Devendra Kulkarni")
    for empty in (None, "", "   "):
        with pytest.raises(ValueError):
            validate_signature("TYPED", None, empty)


def test_unknown_signature_kind_is_rejected():
    with pytest.raises(ValueError):
        validate_signature("SCANNED", "x", "y")


# ── WP-43 notifications ──────────────────────────────────────────────


def test_catalogue_covers_the_briefs_minimum_events():
    """The brief lists eleven minimum event types; assignment, execution, CAPA,
    review, sign-off and programme must each be represented."""
    for code in [
        "AUDITOR_ASSIGNED", "AUDITEE_ASSIGNED", "CHECKPOINTS_ALLOCATED",
        "ENGAGEMENT_STARTING_INCOMPLETE_TEAM", "FINDING_ROUTED", "RESPONSE_RECEIVED",
        "CAPA_DUE", "CAPA_OVERDUE", "REVIEW_REQUESTED", "SIGNOFF_REQUESTED",
        "SLOT_WINDOW_OPENING", "COVERAGE_GAP_ESCALATION", "DEFERRAL_PENDING_APPROVAL",
    ]:
        assert code in CATALOGUE, code


def test_only_genuinely_urgent_events_bypass_the_digest():
    """Digest batching is what stops a 1,500-checkpoint engagement generating a
    mail storm. Immediate email is reserved for events where a delay changes the
    outcome, not merely the mood."""
    assert IMMEDIATE_EMAIL == {
        "ENGAGEMENT_STARTING_INCOMPLETE_TEAM", "CAPA_OVERDUE", "COVERAGE_GAP_ESCALATION",
    }
    assert len(IMMEDIATE_EMAIL) < len(CATALOGUE) / 3


def test_default_frequency_keeps_people_subscribed():
    assert DEFAULT_FREQUENCY == "DAILY"


def test_every_event_has_a_valid_severity():
    assert {e.severity for e in CATALOGUE.values()} <= {"INFO", "WARNING", "CRITICAL"}


def test_deep_links_open_the_record_not_its_list():
    """A notification that lands on a register the user must then search is one
    they learn to ignore."""
    assert deep_link("ComplianceAudit", "a1") == "/cams/audits/a1"
    assert deep_link("ComplianceAudit", "a1", checkpoint_id="c9") == "/cams/audits/a1?checkpoint=c9"
    assert deep_link("CamsFinding", "f1") == "/cams/findings/f1"
    assert deep_link("ProgrammeCycle", "cy1") == "/cams/programme?cycle=cy1"


def test_unknown_entity_falls_back_to_a_real_page():
    assert deep_link("Whatever", "x").startswith("/cams")


def test_empty_digest_renders_nothing():
    """An empty digest is how people learn to filter you — the caller must send
    nothing, so the renderer returns nothing."""
    assert render_digest_text({"empty": True}) == ""


def test_digest_text_includes_titles_and_links():
    txt = render_digest_text({
        "empty": False, "userName": "A", "criticalCount": 1,
        "sections": [{"severity": "CRITICAL", "items": [
            {"title": "CAPA overdue", "body": "b", "linkUrl": "/capa/1"}]}],
    })
    assert "CAPA overdue" in txt and "/capa/1" in txt
    assert "immediate attention" in txt


# ── WP-47 buyer regimes (Q7 / Q19) ───────────────────────────────────


def test_all_five_named_regimes_are_modelled():
    """Q7 named SMETA/Sedex, amfori BSCI, WRAP, Higg FEM and SLCP."""
    assert set(REGIMES) == {
        "SMETA_LIKE", "BSCI_LIKE", "WRAP_LIKE", "HIGG_FEM_LIKE", "SLCP_LIKE",
    }


def test_every_regime_is_labelled_as_safeops_authored():
    """Q19 says self-design. That makes labelling non-negotiable: these are the
    engineering SHAPE of each regime, not the owner's licensed criteria, and a
    reader must never be able to mistake one for the other."""
    for r in list_regimes():
        assert r["authored"] == "SafeOps360"
        assert r["disclaimer"] == AUTHORSHIP_DISCLAIMER
        assert "not the regime owner's official" in r["disclaimer"]


def test_every_regime_severity_maps_onto_a_native_criticality():
    """The mapping is what lets a regime be added without touching the scorer,
    the CAPA severity map or the critical-failure gate."""
    valid = {"critical", "major", "minor", "observation"}
    for spec in REGIMES.values():
        for s in spec.severities:
            assert s.nativeCriticality in valid, (spec.code, s.code)


def test_every_regime_result_maps_onto_a_scoring_bucket():
    valid = {"pass", "partial", "fail", "na"}
    for spec in REGIMES.values():
        for r in spec.results:
            assert r.nativeBucket in valid, (spec.code, r.code)


def test_zero_tolerance_severities_demand_immediate_action():
    """Forced labour and imminent danger cannot be a routine finding."""
    assert native_criticality("SMETA_LIKE", "BUSINESS_CRITICAL") == "critical"
    smeta = get_regime("SMETA_LIKE")
    bc = next(s for s in smeta.severities if s.code == "BUSINESS_CRITICAL")
    assert bc.requiresImmediateAction is True


def test_unknown_severity_fails_safe_to_major_not_observation():
    """An unmapped severity that quietly became `observation` would understate a
    real problem. Failing upward is the only safe direction here."""
    assert native_criticality("SMETA_LIKE", "NOT_A_REAL_CODE") == "major"
    assert native_criticality("NO_SUCH_REGIME", "ANYTHING") == "major"


def test_maturity_scale_spreads_across_buckets_rather_than_pass_fail():
    """The point of a maturity module is that 'we measure it' and 'we improve on
    it' are different achievements."""
    assert native_bucket("HIGG_FEM_LIKE", "LEVEL_0") == "fail"
    assert native_bucket("HIGG_FEM_LIKE", "LEVEL_1") == "partial"
    assert native_bucket("HIGG_FEM_LIKE", "LEVEL_3") == "pass"
    weights = {r.code: r.weight for r in get_regime("HIGG_FEM_LIKE").results}
    assert weights["LEVEL_0"] == 0.0 and weights["LEVEL_3"] == 1.0


def test_binary_regime_offers_no_partial_option():
    """WRAP-shaped certification is binary by design; a middle option would let
    an auditor dodge the actual call."""
    buckets = {r.nativeBucket for r in get_regime("WRAP_LIKE").results}
    assert "partial" not in buckets


def test_unknown_result_code_returns_none_rather_than_guessing():
    assert native_bucket("SMETA_LIKE", "NONSENSE") is None


def test_readiness_reports_coverage_and_says_what_it_does_not_mean():
    out = regime_ready("SMETA_LIKE", ["Health & Safety", "Environment"])
    assert out["known"] is True
    assert out["coveragePct"] == 50.0
    assert set(out["missingSections"]) == {"Labour Standards", "Business Ethics"}
    # The caveat is the load-bearing part: scoped != would pass.
    assert "not whether the facility would pass" in out["caveat"]


def test_readiness_on_an_unknown_regime_is_reported_not_faked():
    assert regime_ready("MADE_UP", ["x"])["known"] is False
