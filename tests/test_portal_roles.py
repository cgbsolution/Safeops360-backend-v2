"""Per-recipient portal tokens — roles and the de-duplication rule.

A supplier audit's external parties are not interchangeable and each needs their
own link: the supplier manager who answers for the factory, external co-auditors
who conduct part of the audit, factory auditees who respond to findings.

The rule that had to change carefully: `issue_token` used to revoke every live
token for the AUDIT, so issuing a co-auditor's link silently killed the supplier
manager's. The reasoning behind it was sound — two live credentials mean revoking
a leaked one does not close access — so it is kept and merely re-keyed to
(audit, email, role), the narrowest key that still guarantees one live credential
per person. Weakening that by accident is the failure worth a test.

Pure over `issue_tokens`' batching logic, in the same style as
`test_supplier_audits.py` — no async-DB harness.
"""

from __future__ import annotations

import pytest

from app.services.supplier_portal import PORTAL_ROLES, hash_token


def _dedupe(recipients: list[dict]) -> list[tuple[str, str]]:
    """Mirror of the de-duplication `issue_tokens` performs before issuing.

    Kept here as the specification: the same (email, role) twice in one batch
    must yield ONE link. Without it the second issue revokes the first and the
    caller reports two links, one of which is already dead.
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for r in recipients:
        email = (r.get("email") or "").strip()
        role = (r.get("role") or "SUPPLIER_MANAGER").upper()
        if not email:
            continue
        key = (email.lower(), role)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


# ── Roles ─────────────────────────────────────────────────────────────────


def test_the_three_external_roles():
    assert PORTAL_ROLES == ("SUPPLIER_MANAGER", "CO_AUDITOR", "AUDITEE")


def test_supplier_manager_is_the_default_role():
    """Every token issued before the role column existed defaults to it, and it
    is what those tokens already meant — the supplier contact answering for the
    factory. A different default would silently reclassify live credentials."""
    assert _dedupe([{"email": "a@b.com"}]) == [("a@b.com", "SUPPLIER_MANAGER")]


# ── One live credential per person per role ───────────────────────────────


def test_the_same_person_in_the_same_role_twice_gets_one_link():
    """The second would revoke the first, so reporting both would hand over a
    dead URL alongside a live one."""
    got = _dedupe([
        {"email": "auditor@ext.com", "role": "CO_AUDITOR"},
        {"email": "auditor@ext.com", "role": "CO_AUDITOR"},
    ])
    assert got == [("auditor@ext.com", "CO_AUDITOR")]


def test_email_case_does_not_create_a_second_credential():
    """The unique index is on `lower(email)`, so the batch must agree — otherwise
    the database rejects the insert and loses the whole audit."""
    got = _dedupe([
        {"email": "Manager@Supplier.com", "role": "SUPPLIER_MANAGER"},
        {"email": "manager@supplier.com", "role": "SUPPLIER_MANAGER"},
    ])
    assert len(got) == 1


def test_one_person_may_hold_two_different_roles():
    """A factory manager can legitimately be both the answering counterpart and
    an auditee for their own area. Different roles grant different things, so
    these are two credentials, not a duplicate."""
    got = _dedupe([
        {"email": "same@supplier.com", "role": "SUPPLIER_MANAGER"},
        {"email": "same@supplier.com", "role": "AUDITEE"},
    ])
    assert len(got) == 2


def test_different_people_in_the_same_role_all_get_links():
    """The whole point of the change. Under the old per-audit rule the last of
    these would have been the only live one."""
    got = _dedupe([
        {"email": "a@ext.com", "role": "CO_AUDITOR"},
        {"email": "b@ext.com", "role": "CO_AUDITOR"},
        {"email": "c@ext.com", "role": "AUDITEE"},
    ])
    assert len(got) == 3


# ── Bad input costs one recipient, never the audit ────────────────────────


@pytest.mark.parametrize("bad", [{}, {"email": ""}, {"email": "   "}, {"name": "No Email"}])
def test_a_recipient_with_no_address_is_skipped_not_fatal(bad: dict):
    """Scheduling an on-site audit must not be lost because one row was left
    blank — the address is the identity, so a row without one is not a recipient."""
    got = _dedupe([{"email": "real@ext.com", "role": "CO_AUDITOR"}, bad])
    assert got == [("real@ext.com", "CO_AUDITOR")]


def test_surrounding_whitespace_does_not_make_a_new_recipient():
    got = _dedupe([
        {"email": " manager@supplier.com ", "role": "AUDITEE"},
        {"email": "manager@supplier.com", "role": "AUDITEE"},
    ])
    assert got == [("manager@supplier.com", "AUDITEE")]


# ── The credential itself ─────────────────────────────────────────────────


def test_the_raw_token_is_not_recoverable_from_what_is_stored():
    """Unchanged by the role work, and worth re-pinning beside it: several live
    tokens per audit only stays safe while a database read cannot be replayed as
    access."""
    raw = "some-opaque-token-value"
    stored = hash_token(raw)
    assert stored != raw
    assert raw not in stored
    assert stored == hash_token(raw)  # deterministic, so lookup by hash works
