"""Auditor independence — ISO 19011 §5.4.2 / §7.2.3 guard tests.

Design: [docs/cams/09-module-completion.md](../../docs/cams/09-module-completion.md) §2.1.

The guard logic is factored into pure decision cores that take already-loaded
rows, so SimpleNamespace stand-ins cover them with no DB — the house test style
(the suite has no async-DB harness).

The three rules under test:
  1. Own-work guard          — you may not audit what you own
  2. Same-engagement duality — never auditor AND auditee on ONE engagement
  3. Cross-engagement        — auditor at site A + auditee at site B is VALID

Rule 3 is the one Page Industries asked to see, and the tests assert it stays
*allowed* — a guard that blocked it would be wrong, not safe.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.independence import (
    Conflict,
    EngagementScope,
    IndependenceVerdict,
    _overlaps,
    area_owner_conflicts,
    discipline_owner_conflicts,
    profile_affinity_conflicts,
    role_scope_conflicts,
    same_engagement_conflict,
    segregation_ok,
)

NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)


def _scope(**over):
    base = dict(
        kind="AUDIT",
        id="aud-1",
        siteId="plant-nw",
        disciplineCodes=["FS", "EL"],
        areaIds=["area-1"],
        departments=["Utilities"],
        leadAuditorId="u-lead",
        teamAuditorIds=["u-co"],
        auditeeUserIds=["u-auditee"],
    )
    base.update(over)
    return EngagementScope(**base)


# ── segregation_ok — the shared primitive ────────────────────────────


def test_segregation_rejects_same_person():
    assert segregation_ok("u-1", "u-2") is True
    assert segregation_ok("u-1", "u-1") is False


def test_segregation_rejects_empty_actor():
    """An unset actor is not "independent by default" — it is unusable."""
    assert segregation_ok("", "u-1") is False
    assert segregation_ok(None, "u-1") is False


# ── Rule 2 — same-engagement exclusivity ─────────────────────────────


def test_rule2_blocks_auditor_who_is_already_auditee():
    c = same_engagement_conflict("u-auditee", _scope(), assigning_as="AUDITOR")
    assert c is not None
    assert c.rule == "SAME_ENGAGEMENT_DUAL_ROLE"
    assert c.severity == "BLOCK"


def test_rule2_blocks_auditee_who_is_already_lead_auditor():
    c = same_engagement_conflict("u-lead", _scope(), assigning_as="AUDITEE")
    assert c is not None and c.severity == "BLOCK"


def test_rule2_blocks_auditee_who_is_already_a_co_auditor():
    """The checkpoint-allocation path is the one that had no guard at all (F-36:
    an insurance manager owning 513 audit checkpoints). Co-auditors count."""
    c = same_engagement_conflict("u-co", _scope(), assigning_as="AUDITEE")
    assert c is not None and c.severity == "BLOCK"


def test_rule2_allows_an_unrelated_person():
    assert same_engagement_conflict("u-fresh", _scope(), assigning_as="AUDITOR") is None
    assert same_engagement_conflict("u-fresh", _scope(), assigning_as="AUDITEE") is None


# ── Rule 3 — cross-engagement dual-hatting stays LEGAL ───────────────


def test_rule3_auditee_elsewhere_is_not_a_same_engagement_conflict():
    """The headline requirement: the same person may audit engagement A while
    being an auditee on engagement B. Rule 2 must not fire across engagements."""
    other = _scope(id="aud-2", auditeeUserIds=["u-someone-else"])
    assert same_engagement_conflict("u-auditee", other, assigning_as="AUDITOR") is None


# ── Rule 1 — area ownership (Q17) ────────────────────────────────────


def test_area_owner_blocks():
    areas = [SimpleNamespace(id="area-1", name="Boiler House", ownerUserId="u-x")]
    out = area_owner_conflicts(areas, "u-x")
    assert len(out) == 1
    assert out[0].severity == "BLOCK"
    assert out[0].source == "AREA_OWNER"
    assert "Boiler House" in out[0].reason  # the reason names the thing


def test_area_owner_ignores_areas_owned_by_others():
    areas = [SimpleNamespace(id="area-1", name="Boiler House", ownerUserId="u-other")]
    assert area_owner_conflicts(areas, "u-x") == []


def test_unowned_area_yields_no_signal_not_a_clearance():
    """An area with no owner must produce NO conflict — and equally must not be
    read as "verified independent". Absence of data is absence of signal."""
    areas = [SimpleNamespace(id="area-1", name="Yard", ownerUserId=None)]
    assert area_owner_conflicts(areas, "u-x") == []


# ── Rule 1 — discipline ownership (Q17) ──────────────────────────────


def _owner(**over):
    base = dict(
        ownerUserId="u-x",
        isActive=True,
        plantId="plant-nw",
        disciplineCode="FS",
        disciplineLabel="Fire Safety",
        ownershipType="ACCOUNTABLE",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_discipline_owner_blocks_at_own_site():
    out = discipline_owner_conflicts([_owner()], "u-x", "plant-nw")
    assert len(out) == 1 and out[0].severity == "BLOCK"
    assert "Fire Safety" in out[0].reason and "at this site" in out[0].reason


def test_discipline_owner_at_a_different_site_does_not_block():
    assert discipline_owner_conflicts([_owner(plantId="plant-sw")], "u-x", "plant-nw") == []


def test_estate_wide_owner_blocks_everywhere():
    """plantId NULL = estate-wide. A group lead who owns Fire Safety across the
    estate should not audit Fire Safety at ANY site."""
    out = discipline_owner_conflicts([_owner(plantId=None)], "u-x", "plant-nw")
    assert len(out) == 1
    assert "across the estate" in out[0].reason


def test_inactive_ownership_row_does_not_block():
    assert discipline_owner_conflicts([_owner(isActive=False)], "u-x", "plant-nw") == []


def test_ownership_type_is_rendered_in_the_reason():
    out = discipline_owner_conflicts([_owner(ownershipType="RESPONSIBLE")], "u-x", "plant-nw")
    assert "responsible owner" in out[0].reason


# ── Rule 1 — role scope ──────────────────────────────────────────────


def _ur(**over):
    base = dict(scopeType="PLANT", scopeValue="plant-nw", validFrom=None, validTo=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_site_scoped_role_warns_but_does_not_block():
    """Site scope is membership, not ownership — WARN, never BLOCK.

    This asserted BLOCK until 2026-07-28. A live count showed the consequence:
    161 users in the tenant hold a PLANT-scoped role, so the rule blocked 59 of
    59 candidates at Meridian North Works and left **0 independent auditors at
    every site**. §2.1 scopes the own-work guard to a unit the user is
    responsible *for*; `UserRole(scopeType='PLANT')` only says they may act at
    the site. Ownership blocks via AREA_OWNER / DISCIPLINE_OWNER /
    DECLARED_AUDITEE, which are scope-unit-correct.
    """
    out = role_scope_conflicts([_ur()], _scope(), now=NOW)
    assert len(out) == 1 and out[0].severity == "WARN"
    assert not any(c.severity == "BLOCK" for c in out)


# ── Scope-unit overlap — the narrowing that fixed the over-block ─────


def test_overlap_disjoint_disciplines_do_not_conflict():
    """Owning Emergency Response does not compromise auditing Worker Welfare.
    This is the whole point of the narrowing."""
    assert _overlaps(["WORKER-WELFARE", "FIRE-LIFE-SAFETY"], ["EMERGENCY-RESPONSE"]) is False


def test_overlap_shared_discipline_conflicts():
    assert _overlaps(["WORKER-WELFARE", "PPE-COMPLIANCE"], ["WORKER-WELFARE"]) is True


def test_overlap_empty_is_full_library_sentinel_and_overlaps_everything():
    """`selectedDisciplineIds == []` means "full library", not "no scope".

    Reading empty as "nothing in scope" would disable the guard on exactly the
    widest audits — the dangerous direction to be wrong in.
    """
    assert _overlaps([], ["WORKER-WELFARE"]) is True
    assert _overlaps(["WORKER-WELFARE"], []) is True
    assert _overlaps([], []) is True


def test_overlap_ignores_none():
    assert _overlaps(None, ["X"]) is True
    assert _overlaps(["X"], None) is True


def test_department_scoped_role_only_warns():
    """Department scope is coarser than audit scope. Blocking on it would deny
    legitimate assignments, and a guard people route around is worse than none."""
    out = role_scope_conflicts(
        [_ur(scopeType="DEPARTMENT", scopeValue="Utilities")], _scope(), now=NOW
    )
    assert len(out) == 1 and out[0].severity == "WARN"


def test_expired_role_assignment_is_ignored():
    out = role_scope_conflicts([_ur(validTo=NOW - timedelta(days=1))], _scope(), now=NOW)
    assert out == []


def test_future_role_assignment_is_ignored():
    out = role_scope_conflicts([_ur(validFrom=NOW + timedelta(days=30))], _scope(), now=NOW)
    assert out == []


def test_naive_validity_dates_do_not_crash():
    """Test stand-ins and SQLite round-trips give naive datetimes; Postgres gives
    aware. The comparison must survive both rather than raising."""
    out = role_scope_conflicts([_ur(validTo=datetime(2026, 1, 1))], _scope(), now=NOW)
    assert out == []


def test_role_at_a_different_site_does_not_block():
    assert role_scope_conflicts([_ur(scopeValue="plant-sw")], _scope(), now=NOW) == []


# ── Rule 1 — profile affinity (weakest source) ───────────────────────


def test_profile_affinity_warns_never_blocks():
    """User.department is free text with no FK. A string match is not evidence."""
    u = SimpleNamespace(plantId="plant-nw", department="Utilities")
    out = profile_affinity_conflicts(u, _scope())
    assert len(out) == 1
    assert out[0].severity == "WARN"
    assert "not a declared ownership record" in out[0].reason


def test_profile_affinity_is_case_and_whitespace_insensitive():
    u = SimpleNamespace(plantId="plant-nw", department="  utilities ")
    assert len(profile_affinity_conflicts(u, _scope())) == 1


def test_profile_affinity_needs_both_site_and_department_match():
    assert profile_affinity_conflicts(
        SimpleNamespace(plantId="plant-sw", department="Utilities"), _scope()
    ) == []
    assert profile_affinity_conflicts(
        SimpleNamespace(plantId="plant-nw", department="Quality"), _scope()
    ) == []
    assert profile_affinity_conflicts(
        SimpleNamespace(plantId="plant-nw", department=None), _scope()
    ) == []


def test_profile_affinity_handles_a_missing_user():
    assert profile_affinity_conflicts(None, _scope()) == []


# ── Verdict aggregation ──────────────────────────────────────────────


def _c(sev):
    return Conflict(rule="OWN_WORK", severity=sev, source="ROLE_SCOPE", reason=f"{sev} reason")


def test_warnings_alone_do_not_block():
    v = IndependenceVerdict(allowed=True, conflicts=[_c("WARN")])
    assert v.allowed is True
    assert v.blocking == [] and len(v.warnings) == 1


def test_verdict_summary_prefers_the_blocking_reason():
    v = IndependenceVerdict(allowed=False, conflicts=[_c("WARN"), _c("BLOCK")])
    assert v.as_dict()["summary"] == "BLOCK reason"
    assert v.as_dict()["blockingCount"] == 1


def test_verdict_summary_falls_back_to_a_warning():
    assert IndependenceVerdict(allowed=True, conflicts=[_c("WARN")]).as_dict()["summary"] == (
        "WARN reason"
    )


def test_clean_verdict_has_an_empty_summary():
    d = IndependenceVerdict(allowed=True).as_dict()
    assert d["summary"] == "" and d["blockingCount"] == 0 and d["warningCount"] == 0


def test_waived_verdict_reports_both_the_waiver_and_the_conflict():
    """A waiver must not erase the conflict — the report has to render what was
    waived, so the conflict stays visible alongside `waived: True`."""
    v = IndependenceVerdict(allowed=True, conflicts=[_c("BLOCK")], waived=True, waiverId="w-1")
    d = v.as_dict()
    assert d["allowed"] is True and d["waived"] is True and d["waiverId"] == "w-1"
    assert d["blockingCount"] == 1
