"""Who may decide an escalated audit finding.

The observed failure (audit AUD-PI-2026-NW-0022, checkpoint PI-OHS-002): a
finding was escalated for the plant manager's decision and was accepted by the
OHS **co-auditor** who raised it. `plantManagerReview.reviewer_user_id` then
named him as the reviewing plant manager.

Nothing was broken in the escalate step — it notified the right person. The hole
was on the DECIDE step: the router gates `PM_*` on `AUDIT_COMPLIANCE.APPROVE`,
which every HSE manager at the site holds at OWN_PLANT scope, so record context
(`plantManagerUserId`) was never actually enforced. Escalation exists to reach
someone who did not raise the finding; permission alone cannot express that.

Pure over the decision rule, in the same style as `test_supplier_audits.py` —
the rule is factored so it can be checked without an async-DB harness.
"""

from __future__ import annotations

import pytest

from app.services.audit_compliance import _coauditor_ids, pm_decision_block_reason

LEAD = "u-lead"
CO_QMS_OHS = "u-lalit"
CO_EMS = "u-other-co"
PM = "u-ravi"
STRANGER = "u-somebody-else"


class _Audit:
    """Only the fields the rule reads."""

    def __init__(self, plant_manager=PM, co_auditors=None, lead=LEAD):
        self.leadAuditorUserId = lead
        self.plantManagerUserId = plant_manager
        self.coAuditors = co_auditors if co_auditors is not None else [
            {"userId": CO_QMS_OHS, "disciplineIds": ["QMS", "OHS"]},
            {"userId": CO_EMS, "disciplineIds": ["EMS", "ENMS"]},
        ]


# ── The reported bug ──────────────────────────────────────────────────────


def test_the_co_auditor_who_raised_it_cannot_decide_the_escalation():
    """The exact case from the report. This must fail closed."""
    reason = pm_decision_block_reason(_Audit(), CO_QMS_OHS)
    assert reason is not None
    assert "conducted this audit" in reason


def test_the_lead_auditor_cannot_decide_the_escalation_either():
    """Same principle — the lead is no more independent of the audit than a
    co-auditor is."""
    assert pm_decision_block_reason(_Audit(), LEAD) is not None


def test_the_designated_plant_manager_can_decide():
    assert pm_decision_block_reason(_Audit(), PM) is None


def test_another_approver_who_is_not_the_designated_reviewer_cannot():
    """Holding AUDIT_COMPLIANCE.APPROVE at the plant is not the same as being
    THIS audit's reviewer — which is precisely what the permission check alone
    could not distinguish."""
    reason = pm_decision_block_reason(_Audit(), STRANGER)
    assert reason is not None
    assert "assigned to this audit" in reason


# ── The rules must not fire where they shouldn't ──────────────────────────


def test_with_no_designated_reviewer_any_non_auditor_may_decide():
    """`plantManagerUserId` is optional on an audit. With nobody designated the
    identity rule has nothing to compare against, so it must fall back to the
    permission gate — but the independence rule still stands."""
    audit = _Audit(plant_manager=None)
    assert pm_decision_block_reason(audit, STRANGER) is None
    assert pm_decision_block_reason(audit, LEAD) is not None
    assert pm_decision_block_reason(audit, CO_QMS_OHS) is not None


def test_independence_is_checked_before_identity():
    """If the same person were somehow both auditor and designated reviewer, the
    answer is still no — and the reason must say WHY, not send them to reassign
    the reviewer to themselves."""
    audit = _Audit(plant_manager=LEAD)
    reason = pm_decision_block_reason(audit, LEAD)
    assert reason is not None
    assert "conducted this audit" in reason


def test_legacy_flat_coauditor_ids_are_still_recognised():
    """`coAuditors` may be a bare list of ids on older audits. A co-auditor who
    is invisible to the rule is a co-auditor who can review their own finding."""
    audit = _Audit(co_auditors=[CO_QMS_OHS, CO_EMS])
    assert _coauditor_ids(audit.coAuditors) == [CO_QMS_OHS, CO_EMS]
    assert pm_decision_block_reason(audit, CO_QMS_OHS) is not None


@pytest.mark.parametrize("co", [None, []])
def test_an_audit_with_no_co_auditors_is_handled(co):
    audit = _Audit(co_auditors=co if co is not None else [])
    assert pm_decision_block_reason(audit, PM) is None
    assert pm_decision_block_reason(audit, LEAD) is not None
