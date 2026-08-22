"""Conduct guards: who may work an audit, and which checkpoints.

Both guards exist because a permission scope cannot express them. AUDITOR and
LEAD_AUDITOR hold AUDIT_COMPLIANCE.EXECUTE at ALL_PLANTS deliberately — audit
independence seats auditors away from their home site — and an ALL_PLANTS grant
satisfies `can()` before it reads the record the conduct endpoints pass. So
scope alone let any auditor grade any audit at any plant, in any discipline.
"""

from types import SimpleNamespace

from app.services.audit_compliance import (
    checkpoint_conduct_block_reason,
    conduct_party_block_reason,
)

LEAD = "u-lead"
CO = "u-co"
OTHER_CO = "u-co-2"
CREATOR = "u-scheduler"
STRANGER = "u-stranger"


def _audit(**over):
    base = dict(
        leadAuditorUserId=LEAD,
        createdByUserId=CREATOR,
        coAuditors=[{"userId": CO, "disciplineIds": ["HR"]},
                    {"userId": OTHER_CO, "disciplineIds": []}],
    )
    base.update(over)
    return SimpleNamespace(**base)


def _row(auditor, category="HR"):
    return SimpleNamespace(
        assignedAuditorId=auditor, categoryId=category, categoryName=category
    )


# ── Party check ───────────────────────────────────────────────────────────

def test_lead_co_auditor_and_creator_are_on_the_team():
    a = _audit()
    for uid in (LEAD, CO, OTHER_CO, CREATOR):
        assert conduct_party_block_reason(a, uid) is None


def test_stranger_is_refused_even_though_they_hold_execute_everywhere():
    assert conduct_party_block_reason(_audit(), STRANGER) is not None


def test_legacy_flat_coauditor_shape_still_counts_as_seated():
    """coAuditors predates the {userId, disciplineIds} shape; a flat list of ids
    must not read as an empty team and lock its own auditors out."""
    assert conduct_party_block_reason(_audit(coAuditors=[CO]), CO) is None


def test_no_coauditors_does_not_admit_everyone():
    a = _audit(coAuditors=[])
    assert conduct_party_block_reason(a, LEAD) is None
    assert conduct_party_block_reason(a, STRANGER) is not None


def test_null_seats_are_not_a_wildcard():
    """An audit with no plant manager and no creator must not let a caller whose
    own id resolves to None-ish slip in through the party set."""
    a = _audit(createdByUserId=None, coAuditors=[])
    assert conduct_party_block_reason(a, STRANGER) is not None


# ── Per-checkpoint allocation ─────────────────────────────────────────────

def test_auditor_may_grade_a_checkpoint_allocated_to_them():
    assert checkpoint_conduct_block_reason(_audit(), _row(CO), CO) is None


def test_auditor_may_not_grade_a_checkpoint_allocated_to_someone_else():
    reason = checkpoint_conduct_block_reason(_audit(), _row(LEAD), CO)
    assert reason is not None
    # The discipline is named, so "why can't I answer this" is answerable
    # without opening the allocation dialog.
    assert "HR" in reason


def test_unallocated_checkpoint_is_open():
    """assignedAuditorId is normally the lead by fallback, never null — but a
    row that predates allocation, or an ad-hoc one, must not be unanswerable."""
    assert checkpoint_conduct_block_reason(_audit(), _row(None), CO) is None


def test_co_auditor_with_nothing_allocated_conducts_nothing():
    """The rule read literally. The lead absorbs every unallocated discipline,
    so an empty allocation means the work is the lead's — not everyone's."""
    a = _audit()
    for row in (_row(LEAD, "HR"), _row(LEAD, "Administration")):
        assert checkpoint_conduct_block_reason(a, row, OTHER_CO) is not None
