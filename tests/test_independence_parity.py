"""The register and the guard must not disagree about who owns what.

**The defect this locks down.** `two_hat_summary` — which powers the
Independence Register — derived its auditee set from `ComplianceAudit.auditees`
and `plantManagerUserId` alone. `check_assignment` reads four sources:
declared-auditee, checkpoint-level ownership, `Area.ownerUserId` and
`DisciplineOwner`. Three of the four were invisible to the register.

On live data that produced the worst possible failure for an evidence screen:
Rohit Kumar, Imran Solanki and Nikhil Desai each own audit checkpoints, so the
scheduler refused to assign them — and the Independence tab rendered all three
as "0 engagements, wears both hats: no". A register that certifies someone as
clear while the product refuses to schedule them is not merely incomplete; it
is wrong in the direction that matters.

Both now read `resolve_ownership_sources`. The tests below assert the property
that keeps them honest: **anything the guard blocks on must be visible to the
register**, per source. They are written against hand-built facts rather than a
database because the suite has no async-DB harness — the parity being asserted
is between two pure decision layers over one resolution, which is exactly where
the divergence lived.
"""

from __future__ import annotations

import pytest

from app.services.independence import (
    AuditorRole,
    EngagementScope,
    OwnedThing,
    OwnershipSources,
    declared_auditee_conflicts,
    ownership_of_record_conflicts,
    summarise_two_hats,
)

SITE = "site-nw"


def _scope(**over) -> EngagementScope:
    base = dict(
        kind="AUDIT",
        id="aud-target",
        siteId=SITE,
        disciplineCodes=["FIRE", "ELEC"],
        areaIds=["area-1"],
        departments=[],
        leadAuditorId=None,
        teamAuditorIds=[],
        auditeeUserIds=[],
    )
    base.update(over)
    return EngagementScope(**base)


def _declared(engagement_id="aud-other", disciplines=("FIRE",), site=SITE) -> OwnedThing:
    return OwnedThing(
        source="DECLARED_AUDITEE",
        label="Q2 Fire Safety Audit",
        siteId=site,
        disciplineCodes=list(disciplines),
        engagementKind="AUDIT",
        engagementId=engagement_id,
        engagementCode="AUD-0002",
        detail={"status": "closed"},
    )


def _checkpoint(engagement_id="aud-other", discipline="FIRE", site=SITE) -> OwnedThing:
    return OwnedThing(
        source="CHECKPOINT_OWNER",
        label="Fire & Life Safety",
        siteId=site,
        disciplineCodes=[discipline],
        engagementKind="AUDIT",
        engagementId=engagement_id,
        engagementCode="AUD-0002",
        detail={"checkpointCount": 12},
    )


def _area(area_id="area-1") -> OwnedThing:
    return OwnedThing(
        source="AREA_OWNER", label="Cutting Floor", siteId=SITE,
        detail={"areaId": area_id, "areaName": "Cutting Floor"},
    )


def _discipline(code="FIRE", site=SITE) -> OwnedThing:
    return OwnedThing(
        source="DISCIPLINE_OWNER", label="Fire Safety", siteId=site,
        disciplineCodes=[code],
        detail={"disciplineCode": code, "ownershipType": "ACCOUNTABLE",
                "estateWide": site is None},
    )


# ── The property: nothing the guard blocks on is invisible to the register ──

ALL_FOUR = [
    pytest.param(_declared(), id="DECLARED_AUDITEE"),
    pytest.param(_checkpoint(), id="CHECKPOINT_OWNER"),
    pytest.param(_area(), id="AREA_OWNER"),
    pytest.param(_discipline(), id="DISCIPLINE_OWNER"),
]


@pytest.mark.parametrize("fact", ALL_FOUR)
def test_every_blocking_source_is_visible_to_the_register(fact: OwnedThing):
    """The parity assertion, one source at a time.

    Parametrised deliberately: a single test over all four would have passed on
    DECLARED_AUDITEE alone, which is exactly the state the product shipped in.
    """
    scope = _scope()
    conflicts = declared_auditee_conflicts([fact], scope) + ownership_of_record_conflicts(
        [fact], scope
    )
    assert conflicts, f"{fact.source} produced no conflict — the guard would not block"

    view = summarise_two_hats(OwnershipSources(userId="u1", owns=[fact]))
    assert fact.source in view["sources"], (
        f"{fact.source} blocks in the guard but is absent from the register's sources"
    )
    surfaced = view["asAuditee"] + view["ownershipOfRecord"]
    assert surfaced, f"{fact.source} blocks in the guard but renders nothing on the register"


def test_the_three_live_users_are_no_longer_invisible():
    """Rohit Kumar, Imran Solanki, Nikhil Desai — the confirmed live divergence.

    Each owns checkpoints on an audit and is named nowhere in
    `ComplianceAudit.auditees`. Under the old derivation the register reported
    0/0 and `wearsBothHats=False` for all three while the guard blocked them.
    """
    for name in ("rohit", "imran", "nikhil"):
        sources = OwnershipSources(
            userId=name,
            owns=[_checkpoint(engagement_id=f"aud-{name}", discipline="FIRE")],
        )
        view = summarise_two_hats(sources)
        assert view["auditeeCount"] == 1, f"{name} still renders as clear"
        assert "CHECKPOINT_OWNER" in view["sources"]
        # And the guard agrees there is something here.
        assert declared_auditee_conflicts(sources.owns, _scope())


def test_checkpoint_ownership_alone_makes_someone_an_auditee():
    """The specific hole: no declared-auditee row, yet the guard blocks."""
    fact = _checkpoint()
    view = summarise_two_hats(OwnershipSources(userId="u1", owns=[fact]))
    assert view["auditeeCount"] == 1
    assert view["asAuditee"][0]["hat"] == "CHECKPOINT_OWNER"


# ── Ownership of record is real, but it is not "two hats" ──────────────


def test_standing_ownership_is_reported_separately_from_two_hats():
    """A discipline owner on no engagement wears one hat, not two — and still blocks.

    Collapsing the two would either overstate the dual-role count or hide a
    blocking fact. The register reports them as distinct claims.
    """
    view = summarise_two_hats(OwnershipSources(userId="u1", owns=[_discipline()]))
    assert view["wearsBothHats"] is False
    assert view["hasOwnershipOfRecord"] is True
    assert view["ownershipCount"] == 1
    assert view["auditeeCount"] == 0
    assert ownership_of_record_conflicts([_discipline()], _scope())


def test_two_hats_needs_both_sides_on_engagements():
    sources = OwnershipSources(
        userId="u1",
        owns=[_declared(engagement_id="aud-a")],
        audits=[
            AuditorRole(
                hat="LEAD_AUDITOR", engagementKind="AUDIT", engagementId="aud-b",
                engagementCode="AUD-0003", title="Q3", siteId=SITE,
                status="in_progress", scheduledDate=None,
            )
        ],
    )
    view = summarise_two_hats(sources)
    assert view["wearsBothHats"] is True
    assert view["auditorCount"] == 1 and view["auditeeCount"] == 1


# ── Guard behaviour preserved through the refactor ─────────────────────


def test_the_engagement_under_assessment_is_not_its_own_rule_1_conflict():
    """Rule 2's territory, not rule 1's — asserted because the refactor moved
    the check from a query filter into the decision layer."""
    fact = _declared(engagement_id="aud-target")
    assert declared_auditee_conflicts([fact], _scope(id="aud-target")) == []


def test_a_non_overlapping_discipline_is_not_a_conflict():
    """Owning Emergency Response checkpoints does not compromise someone
    auditing Worker Welfare. Widening this to "auditee anywhere at this site"
    once left zero eligible auditors at every site in the tenant."""
    fact = _declared(disciplines=("EMERGENCY",))
    assert declared_auditee_conflicts([fact], _scope(disciplineCodes=["FIRE"])) == []


def test_an_empty_scope_is_the_full_library_sentinel_and_overlaps_everything():
    """`selectedDisciplineIds = []` means the whole library, so it must match.
    Reading it as "nothing in scope" disables the guard on the widest audits."""
    fact = _declared(disciplines=("ANYTHING",))
    assert declared_auditee_conflicts([fact], _scope(disciplineCodes=[]))


def test_another_site_is_not_a_conflict():
    assert declared_auditee_conflicts([_declared(site="site-sw")], _scope()) == []


def test_an_inspection_auditee_role_does_not_block_an_audit_assignment():
    """A deliberate narrowing, preserved from the pre-refactor guard.

    The register SHOWS inspection auditee roles; the guard has never blocked on
    them, and turning that on is a scheduling-policy change rather than a
    display fix. Asserted so the asymmetry is a decision, not an accident.
    """
    fact = _declared()
    fact.engagementKind = "INSPECTION"
    assert declared_auditee_conflicts([fact], _scope()) == []
    view = summarise_two_hats(OwnershipSources(userId="u1", owns=[fact]))
    assert view["auditeeCount"] == 1


def test_estate_wide_discipline_ownership_conflicts_at_every_site():
    """`plantId is None` — a group lead who owns Fire Safety everywhere should
    not audit it anywhere."""
    assert ownership_of_record_conflicts([_discipline(site=None)], _scope(siteId="any-site"))


def test_area_ownership_outside_the_engagement_scope_does_not_block():
    assert ownership_of_record_conflicts([_area(area_id="area-99")], _scope()) == []


def test_only_one_declared_auditee_conflict_is_emitted():
    """Two copies of the same message help nobody; the guard returned one."""
    facts = [_declared(engagement_id="a"), _declared(engagement_id="b")]
    assert len(declared_auditee_conflicts(facts, _scope())) == 1


def test_the_header_source_wins_over_the_checkpoint_source():
    """Checkpoint ownership is consulted only when the header found nothing —
    preserving the original precedence, which produced the more specific
    message first."""
    out = declared_auditee_conflicts([_checkpoint(), _declared()], _scope())
    assert len(out) == 1
    assert out[0].source == "DECLARED_AUDITEE"


def test_an_empty_resolution_produces_a_clean_register_row():
    """"0 engagements" must now mean "the guard has nothing on them"."""
    view = summarise_two_hats(OwnershipSources(userId="u1"))
    assert view["auditorCount"] == 0 and view["auditeeCount"] == 0
    assert view["sources"] == []
    assert view["wearsBothHats"] is False and view["hasOwnershipOfRecord"] is False
    assert declared_auditee_conflicts([], _scope()) == []
    assert ownership_of_record_conflicts([], _scope()) == []
