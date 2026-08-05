"""Executive Sentinel (Daily Brief upgrade) — pure-function unit tests.

House style: no DB, no TestClient, no conftest — deterministic functions driven
by SimpleNamespace fakes (mirrors tests/test_alert_rules.py / test_insights_
engine.py). Covers the acceptance checklist items that don't need the live stack:
  * Brief Priority Score ranks predicted seriousness first, lateness second
  * score components are inspectable; every input traces to a real field
  * CRITICAL/ATTENTION/WATCH tiering + low-confidence 'early signal' rule
  * role lenses produce materially different briefs
  * the incident-cluster 'days' slot-fill bug is fixed
  * the four predictive card types are constructed correctly (§2)
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services.insights.common import keywords
from app.services.insights.rules_capa import _near_breach_insight
from app.services.insights.rules_combined_risk import _Row, _not_active_tracked_insight
from app.services.insights.rules_incident import _cluster_insights
from app.services.insights.rules_nearmiss import _critical_uninvestigated
from app.services.sentinel.score import (
    brief_priority_score,
    role_lens_keep,
    score_alert,
    score_components,
    score_insight,
    tier_for,
)

# ── Brief Priority Score ─────────────────────────────────────────────────────


def test_seriousness_outranks_lateness():
    """Acceptance §7 line 1: a 6-day-overdue fatal-potential item must outrank a
    45-day-overdue minor one."""
    fatal_6d = brief_priority_score(
        serious_potential=True, severity="critical", overdue_days=6,
        cluster_size=2, escalated=False, confidence="medium",
    )
    minor_45d = brief_priority_score(
        serious_potential=False, severity="attention", overdue_days=45,
        cluster_size=1, escalated=False, confidence="medium",
    )
    assert fatal_6d > minor_45d
    # even if the minor one is escalated to 'critical' severity, seriousness wins
    minor_45d_crit = brief_priority_score(
        serious_potential=False, severity="critical", overdue_days=45,
        cluster_size=1, escalated=False, confidence="high",
    )
    assert fatal_6d > minor_45d_crit


def test_score_components_are_inspectable_and_trace_to_fields():
    comps = score_components(
        serious_potential=True, severity="high", overdue_days=10,
        cluster_size=4, escalated=False, confidence="high",
    )
    assert set(comps) == {"seriousPotential", "overdue", "cluster", "severity", "freshness", "confidence"}
    assert comps["seriousPotential"] == 60
    assert comps["overdue"] == 5           # 10 * 0.5
    assert comps["cluster"] == 6           # (4-1) * 2
    assert comps["freshness"] == 5         # not yet escalated
    assert sum(comps.values()) == brief_priority_score(
        serious_potential=True, severity="high", overdue_days=10,
        cluster_size=4, escalated=False, confidence="high",
    )


def test_cluster_size_breaks_severity_ties():
    single = brief_priority_score(serious_potential=False, severity="high", overdue_days=0,
                                  cluster_size=1, escalated=False, confidence="medium")
    many = brief_priority_score(serious_potential=False, severity="high", overdue_days=0,
                                cluster_size=6, escalated=False, confidence="medium")
    assert many > single


def test_escalated_card_is_less_urgent_than_fresh_equivalent():
    fresh = brief_priority_score(serious_potential=True, severity="critical", overdue_days=0,
                                 cluster_size=1, escalated=False, confidence="high")
    escalated = brief_priority_score(serious_potential=True, severity="critical", overdue_days=0,
                                     cluster_size=1, escalated=True, confidence="high")
    assert fresh > escalated


# ── Tiering ──────────────────────────────────────────────────────────────────


def test_tiering_rules():
    assert tier_for(serious_potential=True, severity="high", confidence="high") == "critical"
    assert tier_for(serious_potential=False, severity="critical", confidence="high") == "critical"
    assert tier_for(serious_potential=False, severity="high", confidence="high") == "attention"
    assert tier_for(serious_potential=False, severity="watch", confidence="high") == "watch"


def test_low_confidence_never_tops_critical():
    """Low-confidence lands in WATCH as an 'early signal', even on a serious source
    (spec §1.2 / acceptance §8)."""
    assert tier_for(serious_potential=True, severity="critical", confidence="low") == "watch"


# ── score_alert derivation (unified event + insight feed) ────────────────────


def test_score_alert_reads_sentinel_bodyparams():
    card = {
        "severity": "critical",
        "count": 3,
        "impactedEntities": [1, 2, 3],
        "bodyParams": {"seriousPotential": True, "overdueDays": 2, "confidence": "medium", "clusterSize": 3},
    }
    out = score_alert(card)
    assert out["tier"] == "critical"
    assert out["components"]["seriousPotential"] == 60
    assert out["earlySignal"] is False


def test_score_alert_derives_serious_from_critical_event_card():
    """A reactive event card carries no seriousPotential flag; a critical one is
    treated as serious-potential so it ranks with the sentinel criticals."""
    event_card = {"severity": "critical", "count": 1, "impactedEntities": [1], "bodyParams": {}}
    out = score_alert(event_card)
    assert out["components"]["seriousPotential"] == 60
    assert out["tier"] == "critical"


# ── Role lenses (spec §3) ────────────────────────────────────────────────────


def _card(tier, score, actionable=True):
    return {"tier": tier, "score": score, "impactedEntities": [1] if actionable else []}


def test_role_lenses_differ_materially():
    crit = _card("critical", 100)
    hi_attention = _card("attention", 55)
    lo_attention = _card("attention", 25)
    watch = _card("watch", 40)

    # executive: only CRITICAL + high-magnitude ATTENTION
    assert role_lens_keep(crit, "executive")
    assert role_lens_keep(hi_attention, "executive")
    assert not role_lens_keep(lo_attention, "executive")   # below the magnitude floor
    assert not role_lens_keep(watch, "executive")

    # hse_manager: the working view — everything, incl WATCH
    assert all(role_lens_keep(c, "hse_manager") for c in (crit, hi_attention, lo_attention, watch))

    # site_lead: critical site-wide + actionable attention; not register/watch noise
    assert role_lens_keep(crit, "site_lead")
    assert role_lens_keep(hi_attention, "site_lead")
    assert not role_lens_keep(_card("attention", 55, actionable=False), "site_lead")
    assert not role_lens_keep(watch, "site_lead")


# ── score_insight on an engine Insight ───────────────────────────────────────


def test_score_insight_maps_tier_to_alert_severity():
    ins = SimpleNamespace(
        severity="critical", confidence="high", seriousPotential=True,
        overdueDays=None, recordRefs=["NM-1", "NM-2", "NM-3"],
    )
    scored = score_insight(ins)
    assert scored["tier"] == "critical"
    assert scored["alertSeverity"] == "critical"
    assert scored["clusterSize"] == 3


# ── §2 blocker: the incident-cluster 'days' slot-fill bug is fixed ───────────


def test_keywords_drops_duration_words():
    toks = keywords("worker off 5 days", "guard missing on press")
    assert "days" not in toks
    assert "day" not in toks
    assert "guard" in toks and "press" in toks


def test_incident_cluster_keyword_is_a_real_cause_not_days():
    now = datetime.utcnow()
    rows = [
        SimpleNamespace(
            id=f"i{i}", number=f"INC-{i}", plantId="p1", type="LTI", severity="HIGH",
            immediateCauses=["machine guard removed"], rootCauses=["guard interlock bypassed"],
            # description narrates lost time — the old bug's source of "days"
            description=f"operator off work {i + 3} days after the event",
        )
        for i in range(3)
    ]
    insights = _cluster_insights(rows, {"p1": "Meridian North"})
    assert len(insights) == 1
    card = insights[0]
    assert "days" not in card.headline           # the bug is fixed
    # keyword is a real shared CAUSE term (from the structured cause arrays), not
    # a duration word leaked from the free-text description.
    assert any(t in card.headline for t in ("guard", "bypassed", "interlock", "machine", "removed"))
    assert card.seriousPotential is True          # LTI in the cluster (PSI/SIF)
    assert card.kind == "cluster"


# ── §2 card types: the four predictive insights ─────────────────────────────


def test_nearmiss_critical_uninvestigated_is_serious_potential():
    old = datetime.utcnow() - timedelta(days=10)
    rows = [
        SimpleNamespace(id=f"n{i}", number=f"NM-{i}", severity="CRITICAL", status="REPORTED", createdAt=old)
        for i in range(3)
    ]
    card = _critical_uninvestigated(rows)
    assert card is not None
    assert card.kind == "predictive_risk"
    assert card.seriousPotential is True
    assert card.severity == "critical"           # >= 3 stale


def test_capa_near_breach_only_flags_serious_unstarted_pre_target():
    now = datetime.utcnow()
    rows = [
        # HIT: serious, planned (not started), due in 3d
        SimpleNamespace(severity="CRITICAL", state="ACTIONS_PLANNED",
                        closureTargetDate=now + timedelta(days=3), capaNumber="CAPA-A"),
        # miss: already started
        SimpleNamespace(severity="HIGH", state="ACTIONS_IN_PROGRESS",
                        closureTargetDate=now + timedelta(days=2), capaNumber="CAPA-B"),
        # miss: minor severity
        SimpleNamespace(severity="LOW", state="ACTIONS_PLANNED",
                        closureTargetDate=now + timedelta(days=2), capaNumber="CAPA-C"),
        # miss: already past target (that's overdue, not near-breach)
        SimpleNamespace(severity="CRITICAL", state="ACTIONS_PLANNED",
                        closureTargetDate=now - timedelta(days=1), capaNumber="CAPA-D"),
    ]
    card = _near_breach_insight(rows, now)
    assert card is not None
    assert card.kind == "predictive_risk"
    assert card.seriousPotential is True
    assert card.recordRefs == ["CAPA-A"]


def test_capa_near_breach_none_when_no_serious_unstarted():
    now = datetime.utcnow()
    rows = [SimpleNamespace(severity="LOW", state="ACTIONS_PLANNED",
                            closureTargetDate=now + timedelta(days=2), capaNumber="CAPA-X")]
    assert _near_breach_insight(rows, now) is None


def test_combined_risk_not_active_tracked_bar_insight():
    rows = [
        _Row("e1", "HIRA-1#1", "HIRA", "a1", "CRITICAL", "HIGH", "DRAFT", False),
        _Row("e2", "EAI-2#3", "EAI", "a2", "CRITICAL", "HIGH", "APPROVED", True),
        _Row("e3", "HIRA-3#2", "HIRA", "a3", "HIGH", "HIGH", "ACTIVE", True),  # not a hit
    ]
    card = _not_active_tracked_insight(rows)
    assert card is not None
    assert card.kind == "anomaly"
    assert card.seriousPotential is True
    assert set(card.recordRefs) == {"HIRA-1#1", "EAI-2#3"}
    assert card.severity == "high"                # >= 2 hits
