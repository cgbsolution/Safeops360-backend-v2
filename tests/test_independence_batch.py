"""The batched guard: one decision, one fetch, many candidates.

**What this replaced.** `check_many` looped `check_assignment`, and each call
issued ~6 queries of its own. Measured against this tenant's 59-candidate site
pool: **362 queries, 52.9 seconds.** Fine for "check the two people I picked";
unusable for a picker that wants status on everyone before anyone is picked.

The fix moved the FETCH, not the RULES. `verdict_for` is pure and every caller
reaches it: `check_assignment` is now a one-element `check_many`, so the
single-candidate and list paths cannot produce different answers. The tests
below pin that equivalence and the per-candidate isolation that makes a batch
safe — a batch that leaked one candidate's facts into another's verdict would
be far worse than a slow loop.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.independence import (
    Conflict,
    EngagementScope,
    OwnedThing,
    check_many,
    verdict_for,
)

SITE = "site-nw"


def _scope(**over) -> EngagementScope:
    base = dict(
        kind="AUDIT", id=None, siteId=SITE,
        disciplineCodes=["FIRE"], areaIds=[], departments=[],
        leadAuditorId=None, teamAuditorIds=[], auditeeUserIds=[],
    )
    base.update(over)
    return EngagementScope(**base)


def _checkpoint(uid_engagement="aud-1") -> OwnedThing:
    return OwnedThing(
        source="CHECKPOINT_OWNER", label="Fire & Life Safety", siteId=SITE,
        disciplineCodes=["FIRE"], engagementKind="AUDIT",
        engagementId=uid_engagement, engagementCode="AUD-0001",
        detail={"checkpointCount": 9},
    )


def _role(scope_type="PLANT", value=SITE):
    return SimpleNamespace(scopeType=scope_type, scopeValue=value, validFrom=None, validTo=None)


# ── verdict_for: the whole decision, pure ────────────────────────────


def test_a_clean_candidate_is_allowed():
    v = verdict_for("u1", _scope(), assigning_as="AUDITOR", owned=[])
    assert v.allowed and not v.conflicts


def test_ownership_blocks():
    v = verdict_for("u1", _scope(), assigning_as="AUDITOR", owned=[_checkpoint()])
    assert not v.allowed
    assert v.blocking[0].source == "CHECKPOINT_OWNER"


def test_a_site_role_warns_but_does_not_block():
    """Site access is not ownership. Blocking on it once left zero eligible
    auditors at every site in the tenant."""
    v = verdict_for("u1", _scope(), assigning_as="AUDITOR", owned=[], user_roles=[_role()])
    assert v.allowed
    assert v.warnings and v.warnings[0].source == "ROLE_SCOPE"
    assert not v.blocking


def test_a_waiver_turns_a_block_into_an_allowed_waived_verdict():
    waiver = SimpleNamespace(id="w1")
    v = verdict_for(
        "u1", _scope(), assigning_as="AUDITOR", owned=[_checkpoint()], waiver=waiver
    )
    assert v.allowed and v.waived and v.waiverId == "w1"
    # The conflict is NOT erased — a waiver makes an exception visible, it does
    # not make it disappear.
    assert v.blocking


def test_a_waiver_on_a_clean_candidate_does_not_mark_them_waived():
    v = verdict_for("u1", _scope(), assigning_as="AUDITOR", owned=[],
                    waiver=SimpleNamespace(id="w1"))
    assert v.allowed and not v.waived


def test_assigning_as_auditee_skips_the_own_work_rules():
    """You cannot fail the own-work guard by being made the auditee of your own
    area — that is the correct assignment."""
    v = verdict_for("u1", _scope(), assigning_as="AUDITEE", owned=[_checkpoint()],
                    user_roles=[_role()])
    assert v.allowed and not v.conflicts


def test_rule_two_applies_in_both_directions():
    as_auditor = verdict_for(
        "u1", _scope(auditeeUserIds=["u1"]), assigning_as="AUDITOR", owned=[]
    )
    as_auditee = verdict_for(
        "u1", _scope(leadAuditorId="u1"), assigning_as="AUDITEE", owned=[]
    )
    for v in (as_auditor, as_auditee):
        assert not v.allowed
        assert v.blocking[0].rule == "SAME_ENGAGEMENT_DUAL_ROLE"


# ── check_many: fetch once, decide per candidate ─────────────────────


class _BatchDb:
    """Records what was asked for, and answers with per-table fixtures.

    The point of the assertions below is the SHAPE of the traffic: one query per
    table for the whole candidate list, never one per candidate.
    """

    def __init__(self, *, roles=(), waivers=(), users=()):
        self.roles = list(roles)
        self.waivers = list(waivers)
        self.users = list(users)
        self.queries: list[str] = []

    async def execute(self, stmt):
        text = str(stmt)
        if '"UserRole"' in text:
            rows = self.roles
        elif '"IndependenceWaiver"' in text:
            rows = self.waivers
        elif '"User"' in text:
            rows = self.users
        else:  # pragma: no cover - the resolver is stubbed out in these tests
            rows = []
        self.queries.append(text.split("\n")[0][:60])
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows),
            all=lambda: rows,
        )

    async def flush(self):
        pass


@pytest.fixture()
def no_ownership(monkeypatch):
    """Stub the ownership resolver — its own behaviour is covered by
    `test_independence_parity`; what matters here is that it is called ONCE."""
    calls = {"n": 0, "user_ids": None}

    async def _fake(db, **kw):
        calls["n"] += 1
        calls["user_ids"] = list(kw.get("user_ids") or [])
        return {}

    monkeypatch.setattr("app.services.independence.resolve_ownership_sources", _fake)
    return calls


def test_the_ownership_resolver_is_called_once_for_the_whole_list(no_ownership):
    db = _BatchDb()
    ids = [f"u{i}" for i in range(50)]
    asyncio.run(check_many(db, user_ids=ids, scope=_scope(), assigning_as="AUDITOR"))
    assert no_ownership["n"] == 1, "resolver called per candidate — the fan-out is back"
    assert sorted(no_ownership["user_ids"]) == sorted(ids)


def test_query_count_does_not_grow_with_the_candidate_list(no_ownership):
    """The property the whole rewrite exists for."""
    small = _BatchDb()
    asyncio.run(check_many(small, user_ids=["u1"], scope=_scope()))
    large = _BatchDb()
    asyncio.run(check_many(large, user_ids=[f"u{i}" for i in range(150)], scope=_scope()))
    assert len(small.queries) == len(large.queries)


def test_every_candidate_gets_a_verdict(no_ownership):
    db = _BatchDb()
    ids = [f"u{i}" for i in range(20)]
    out = asyncio.run(check_many(db, user_ids=ids, scope=_scope()))
    assert set(out) == set(ids)


def test_duplicate_and_empty_ids_are_collapsed(no_ownership):
    db = _BatchDb()
    out = asyncio.run(check_many(db, user_ids=["u1", "u1", "", None, "u2"], scope=_scope()))
    assert set(out) == {"u1", "u2"}


def test_an_empty_candidate_list_issues_no_queries(no_ownership):
    db = _BatchDb()
    assert asyncio.run(check_many(db, user_ids=[], scope=_scope())) == {}
    assert db.queries == []
    assert no_ownership["n"] == 0


def test_one_candidates_role_does_not_leak_into_anothers_verdict(no_ownership):
    """The isolation a batch has to earn. A shared-fetch implementation that
    mixed candidates up would be worse than the slow loop it replaced."""
    db = _BatchDb(roles=[SimpleNamespace(
        userId="u1", scopeType="PLANT", scopeValue=SITE, validFrom=None, validTo=None,
    )])
    out = asyncio.run(check_many(db, user_ids=["u1", "u2"], scope=_scope()))
    assert out["u1"].warnings and out["u1"].warnings[0].source == "ROLE_SCOPE"
    assert not out["u2"].conflicts


def test_a_waiver_is_matched_to_its_own_subject(no_ownership):
    db = _BatchDb(waivers=[SimpleNamespace(id="w1", subjectUserId="u1")])
    scope = _scope(id="aud-1", auditeeUserIds=["u1", "u2"])
    out = asyncio.run(check_many(db, user_ids=["u1", "u2"], scope=scope))
    assert out["u1"].waived and out["u1"].waiverId == "w1"
    assert not out["u2"].waived and not out["u2"].allowed


def test_no_waiver_query_when_the_engagement_does_not_exist_yet(no_ownership):
    """The picker's case: an audit being composed has no id, so no waiver can
    exist against it and the round trip is skipped."""
    db = _BatchDb()
    asyncio.run(check_many(db, user_ids=["u1"], scope=_scope(id=None)))
    assert not any("IndependenceWaiver" in q for q in db.queries)


def test_the_profile_query_is_skipped_when_no_department_is_in_scope(no_ownership):
    """Profile affinity reads nothing without departments, so loading the users
    to ignore them was a round trip for nothing."""
    db = _BatchDb()
    asyncio.run(check_many(db, user_ids=["u1"], scope=_scope(departments=[])))
    assert not any(q.startswith("SELECT \"User\"") for q in db.queries)


def test_auditee_mode_issues_no_ownership_queries(no_ownership):
    db = _BatchDb()
    asyncio.run(check_many(db, user_ids=["u1"], scope=_scope(), assigning_as="AUDITEE"))
    assert no_ownership["n"] == 0
    assert not any('"UserRole"' in q for q in db.queries)


def test_check_assignment_is_a_one_element_check_many(no_ownership, monkeypatch):
    """Asserted directly, because "one implementation" is the standing principle
    this module is held to and a wrapper is the easy place to lose it."""
    from app.services import independence as ind

    seen = {}
    real = ind.check_many

    async def _spy(db, *, user_ids, scope, assigning_as="AUDITOR"):
        seen["ids"] = list(user_ids)
        return await real(db, user_ids=user_ids, scope=scope, assigning_as=assigning_as)

    monkeypatch.setattr(ind, "check_many", _spy)
    v = asyncio.run(ind.check_assignment(_BatchDb(), user_id="u9", scope=_scope()))
    assert seen["ids"] == ["u9"]
    assert v.allowed
