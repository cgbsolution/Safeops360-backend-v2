"""Supplier audits (WP-45) — the decision cores.

Same house style as `test_independence.py`: the logic that must not be wrong is
factored into pure functions taking already-loaded rows, so it is covered
directly with stand-ins and no async-DB harness.

Four things are worth pinning here, because each one is a way this feature can
be quietly wrong rather than loudly broken:

  1. A supplier with a good percentage but critical non-conformances must not
     score as LOW risk.
  2. An audit must not invent scores for the five vendor-risk domains it never
     looked at.
  3. The procurement owner of the audited vendor must be blocked from auditing
     it — and owning a *different* vendor must not block them.
  4. A portal token must be unguessable from what is stored, and expired or
     revoked tokens must be indistinguishable from non-existent ones.
"""

from __future__ import annotations

from app.services.audit_compliance import library_subject_scope
from app.services.independence import (
    EngagementScope,
    OwnedThing,
    vendor_relationship_conflicts,
    verdict_for,
)
from app.services import supplier_portal as sp
from app.services.supplier_portal import hash_token
from app.services.vendors import (
    AUDIT_EVIDENCED_DOMAIN,
    audit_pct_to_raw_score,
    build_domain_scores,
)

# ── The vendor RISK domains as the Meridian tenant configures them ───
RISK_DOMAINS = [
    {"domainKey": "financial_stability", "weightPct": 25},
    {"domainKey": "operational_capacity", "weightPct": 20},
    {"domainKey": "delivery_reliability", "weightPct": 15},
    {"domainKey": "compliance_legal", "weightPct": 15},
    {"domainKey": "cyber_infosec", "weightPct": 10},
    {"domainKey": "concentration_dependency", "weightPct": 15},
]


def _scope(**over):
    base = dict(
        kind="AUDIT",
        id="aud-1",
        siteId="plant-nw",
        disciplineCodes=["FS"],
        areaIds=[],
        departments=[],
        leadAuditorId="u-lead",
        teamAuditorIds=[],
        auditeeUserIds=[],
        vendorProfileId="ven-1",
    )
    base.update(over)
    return EngagementScope(**base)


def _owner_of(vendor_id: str, name: str = "Sunrise Knits Pvt Ltd") -> OwnedThing:
    return OwnedThing(
        source="VENDOR_RELATIONSHIP_OWNER",
        label=name,
        siteId="plant-nw",
        detail={"vendorProfileId": vendor_id, "vendorCode": "VEN-004"},
    )


# ─────────────────────────────────────────────────────────────────────
# 1. Compliance % -> RISK raw score
# ─────────────────────────────────────────────────────────────────────


def test_full_compliance_is_lowest_risk_and_zero_is_highest():
    # RISK runs higher = riskier, so the mapping inverts.
    assert audit_pct_to_raw_score(100) == 1.0
    assert audit_pct_to_raw_score(0) == 5.0
    assert audit_pct_to_raw_score(50) == 3.0


def test_critical_failures_floor_the_risk_score():
    """The defect this floor exists to prevent.

    96% compliance maps linearly to raw 1.16 — "LOW risk" — for a supplier with
    three critical non-conformances. A critical NC is precisely the finding that
    is supposed to stop that read.
    """
    assert audit_pct_to_raw_score(96) < 1.5
    floored = audit_pct_to_raw_score(96, critical_failures=3)
    assert floored == 3.5
    # raw 3.5 at full weight is 70/100 — HIGH on the seeded RISK bands.
    assert floored * 20 == 70


def test_failed_gate_floors_the_risk_score():
    assert audit_pct_to_raw_score(92, audit_passed=False) == 3.0


def test_floors_only_ever_raise_never_lower():
    """A reduction veto, not a rewrite: a genuinely bad audit stays bad."""
    bad = audit_pct_to_raw_score(10)
    assert audit_pct_to_raw_score(10, critical_failures=5, audit_passed=False) == bad
    assert bad == 4.6


def test_score_is_clamped_to_the_one_to_five_scale():
    assert audit_pct_to_raw_score(150) == 1.0
    assert audit_pct_to_raw_score(-20) == 5.0


# ─────────────────────────────────────────────────────────────────────
# 2. Domain scores — nothing invented
# ─────────────────────────────────────────────────────────────────────


def test_unevidenced_domains_are_carried_forward_not_invented():
    prior = [
        {"domainKey": "financial_stability", "rawScore": 2, "weightPct": 25},
        {"domainKey": "operational_capacity", "rawScore": 3, "weightPct": 20},
        {"domainKey": "delivery_reliability", "rawScore": 2, "weightPct": 15},
        {"domainKey": "compliance_legal", "rawScore": 2, "weightPct": 15},
        {"domainKey": "cyber_infosec", "rawScore": 4, "weightPct": 10},
        {"domainKey": "concentration_dependency", "rawScore": 3, "weightPct": 15},
    ]
    out = build_domain_scores(
        RISK_DOMAINS, prior, raw_score=3.5, evidence_note="On-site audit AUD-1"
    )
    assert len(out) == 6
    audited = next(d for d in out if d["domainKey"] == AUDIT_EVIDENCED_DOMAIN)
    assert audited["rawScore"] == 3.5
    assert not audited.get("carriedForward")
    assert "AUD-1" in audited["evidenceNotes"]

    # Every other domain keeps its prior number AND says where it came from.
    others = [d for d in out if d["domainKey"] != AUDIT_EVIDENCED_DOMAIN]
    assert all(d["carriedForward"] for d in others)
    assert all("Carried forward" in d["evidenceNotes"] for d in others)
    assert {d["domainKey"]: d["rawScore"] for d in others} == {
        "financial_stability": 2, "operational_capacity": 3,
        "delivery_reliability": 2, "cyber_infosec": 4,
        "concentration_dependency": 3,
    }
    # Weights still sum to 100, so the composite stays comparable.
    assert sum(d["weightPct"] for d in out) == 100


def test_first_ever_assessment_renormalises_instead_of_scoring_zeros():
    """With no prior there is nothing to carry.

    Emitting the other five domains at rawScore 0 would compute a LOW risk
    composite — on the RISK lens zero reads as "no risk", which is the opposite
    of "we have not looked".
    """
    out = build_domain_scores(
        RISK_DOMAINS, None, raw_score=4.0, evidence_note="On-site audit AUD-2"
    )
    assert len(out) == 1
    assert out[0]["domainKey"] == AUDIT_EVIDENCED_DOMAIN
    assert out[0]["weightPct"] == 100.0
    assert "renormalised" in out[0]["evidenceNotes"]
    # raw 4.0 at 100% weight = 80/100 -> CRITICAL band, as intended.
    assert out[0]["rawScore"] * 100 / 5 == 80.0


def test_domain_absent_from_prior_is_omitted_not_zeroed():
    partial_prior = [{"domainKey": "financial_stability", "rawScore": 2, "weightPct": 25}]
    out = build_domain_scores(
        RISK_DOMAINS, partial_prior, raw_score=2.0, evidence_note="n"
    )
    keys = {d["domainKey"] for d in out}
    assert keys == {"financial_stability", AUDIT_EVIDENCED_DOMAIN}
    assert "cyber_infosec" not in keys


# ─────────────────────────────────────────────────────────────────────
# 3. Independence — procurement cannot audit its own vendor
# ─────────────────────────────────────────────────────────────────────


def test_relationship_owner_of_the_audited_vendor_is_blocked():
    conflicts = vendor_relationship_conflicts([_owner_of("ven-1")], _scope())
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.severity == "BLOCK"
    assert c.source == "VENDOR_RELATIONSHIP_OWNER"
    assert c.rule == "OWN_WORK"
    # The reason must cite the standard and name the supplier — a guard that
    # says "denied" without either is one people route around.
    assert "ISO 19011 §7.2.3" in c.reason
    assert "Sunrise Knits" in c.reason


def test_owning_a_different_vendor_is_not_a_conflict():
    """Scoped like every other own-work source: owning something OUT of scope
    is not a conflict, or a category manager could audit nothing."""
    assert vendor_relationship_conflicts([_owner_of("ven-OTHER")], _scope()) == []


def test_own_facility_audit_never_raises_a_vendor_conflict():
    assert vendor_relationship_conflicts(
        [_owner_of("ven-1")], _scope(vendorProfileId=None)
    ) == []


def test_verdict_blocks_the_relationship_owner_as_auditor():
    v = verdict_for(
        "u-procurement",
        _scope(),
        assigning_as="AUDITOR",
        owned=[_owner_of("ven-1")],
    )
    assert v.allowed is False
    assert len(v.blocking) == 1
    assert v.as_dict()["summary"].startswith("They are the relationship owner")


def test_relationship_owner_may_still_be_the_internal_auditee():
    """AUDITEE assignment applies no own-work rule — the relationship owner is
    exactly who should answer for the supplier's corrective actions."""
    v = verdict_for(
        "u-procurement",
        _scope(),
        assigning_as="AUDITEE",
        owned=[_owner_of("ven-1")],
    )
    assert v.allowed is True


def test_a_waiver_allows_but_keeps_the_conflict_visible():
    waiver = type("W", (), {"id": "wv-1"})()
    v = verdict_for(
        "u-procurement", _scope(), assigning_as="AUDITOR",
        owned=[_owner_of("ven-1")], waiver=waiver,
    )
    assert v.allowed is True
    assert v.waived is True
    assert len(v.blocking) == 1  # still reported, never erased


# ─────────────────────────────────────────────────────────────────────
# 4. Portal token handling
# ─────────────────────────────────────────────────────────────────────


def test_portal_failure_never_breaks_the_audit_detail_screen():
    """The regression this pins actually shipped.

    `get_audit` calls through to the portal for the supplier block. With the
    portal tables not yet created, the `UndefinedTableError` propagated out of
    `get_audit`; the detail page's `backendFetch(...).catch(() => null)` turned
    that into `notFound()`, and **every supplier audit rendered as a 404**. An
    optional decoration must not be able to delete the record it decorates.
    """
    import asyncio

    class _Boom:
        """A session whose every statement fails, like a missing table."""

        def begin_nested(self):
            class _Ctx:
                async def __aenter__(self_inner):
                    return None

                async def __aexit__(self_inner, *exc):
                    return False

            return _Ctx()

        async def execute(self, *_a, **_kw):
            raise RuntimeError('relation "SupplierPortalToken" does not exist')

    out = asyncio.run(
        sp.channel_for_engagement(_Boom(), engagement_kind="AUDIT", engagement_id="aud-1")
    )
    # Degrades rather than raising, and says so honestly.
    assert out["responseChannel"] == "OUT_OF_BAND"
    assert out["portalUnavailable"] is True
    # Crucially it must NOT claim the supplier can respond.
    assert "unavailable" in out["responseChannelNote"].lower()


def test_inspections_report_no_portal_without_touching_the_database():
    """The non-AUDIT branch returns before any query, so an inspection cannot be
    broken by the portal tables either."""
    import asyncio

    class _Explode:
        def begin_nested(self):  # pragma: no cover - must never be reached
            raise AssertionError("should not have touched the database")

    out = asyncio.run(
        sp.channel_for_engagement(
            _Explode(), engagement_kind="INSPECTION", engagement_id="eng-1"
        )
    )
    assert out["responseChannel"] == "OUT_OF_BAND"


def test_token_hash_is_deterministic_and_not_the_token():
    raw = "abcdef123456"
    assert hash_token(raw) == hash_token(raw)
    assert hash_token(raw) != raw
    assert len(hash_token(raw)) == 64
    assert hash_token("a") != hash_token("b")


def test_rate_limiter_trips_then_recovers_after_the_window():
    import app.services.supplier_portal as sp

    sp._hits.clear()
    # Writes are the tighter limit.
    for _ in range(sp._WRITE_LIMIT):
        assert sp._rate_limited("tok12345", "1.2.3.4", is_write=True) is False
    assert sp._rate_limited("tok12345", "1.2.3.4", is_write=True) is True

    # A different caller is unaffected — the limit is per (token, ip).
    assert sp._rate_limited("tok12345", "9.9.9.9", is_write=True) is False

    # Ageing the recorded hits past the window frees the original caller.
    sp._hits[("tok12345", "1.2.3.4", True)] = [
        t - (sp._WINDOW_SECONDS + 1) for t in sp._hits[("tok12345", "1.2.3.4", True)]
    ]
    assert sp._rate_limited("tok12345", "1.2.3.4", is_write=True) is False
    sp._hits.clear()


# ─────────────────────────────────────────────────────────────────────
# 5. Checklist scoping — a supplier is never audited against our plant
# ─────────────────────────────────────────────────────────────────────


def test_industry_libraries_are_own_facility():
    """"Are the kiln refractory inspections within validity" is a question about
    OUR plant. If these ever classified as VENDOR, a supplier audit would
    materialise plant checkpoints and the report would read as an internal
    inspection of someone else's factory."""
    assert library_subject_scope("GARMENTS_TEXTILE", [{"category_code": "FIRE"}]) == "OWN_SITE"
    assert library_subject_scope("CEMENT", [{"category_code": "KILN"}]) == "OWN_SITE"
    assert library_subject_scope("PHARMA_LIFE_SCIENCES", []) == "OWN_SITE"


def test_buyer_regimes_are_vendor_scoped_via_regime_code():
    """`seed_buyer_regimes.py` stamps `regimeCode` on every category, so the
    classification is data-driven rather than a name match."""
    cats = [{"category_code": "LABOUR", "regimeCode": "SMETA_LIKE"}]
    assert library_subject_scope("REGIME_SMETA_LIKE", cats) == "VENDOR"


def test_regime_prefix_alone_is_enough():
    assert library_subject_scope("REGIME_WRAP_LIKE", []) == "VENDOR"


def test_explicit_subject_scope_wins_for_imported_libraries():
    """The hook a customer's own Supplier Code of Conduct import uses — it will
    not carry `regimeCode` and will not be named `REGIME_*`."""
    cats = [{"category_code": "COC", "subject_scope": "VENDOR"}]
    assert library_subject_scope("PAGE_SUPPLIER_COC", cats) == "VENDOR"
    assert library_subject_scope("X", [{"subject_scope": "BOTH"}]) == "BOTH"


def test_unknown_library_defaults_to_own_facility():
    """Fails toward "missing from the supplier picker" (visible, fixable) rather
    than toward offering plant checklists for supplier audits (silent, wrong)."""
    assert library_subject_scope("SOMETHING_NEW", [{"category_code": "A"}]) == "OWN_SITE"


def test_reads_and_writes_have_separate_budgets():
    """Exhausting the write budget must not lock the supplier out of READING
    their own findings — they would have no way to see what to fix."""
    import app.services.supplier_portal as sp

    sp._hits.clear()
    for _ in range(sp._WRITE_LIMIT + 1):
        sp._rate_limited("tok9", "1.1.1.1", is_write=True)
    assert sp._rate_limited("tok9", "1.1.1.1", is_write=False) is False
    sp._hits.clear()
