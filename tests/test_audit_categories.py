"""Audit categories — the chain from category to checkpoints.

The scheduler now picks a CATEGORY first (Internal / QMS-EMS-OHS / Social
Compliance) and the category decides which disciplines are on offer. Same house
style as `test_supplier_audits.py`: the classifier is a pure function over
already-loaded rows, so it is covered directly with no async-DB harness.

Four things are worth pinning, because each is a way this can be quietly wrong
rather than loudly broken:

  1. Every category in the menu resolves to a library that is actually seeded —
     a category with no checklist is a promise the instance cannot keep.
  2. Supplier checklists carry NO category. Category and subject are different
     axes, and collapsing them is how a supplier audit ends up scoped against a
     plant checklist.
  3. The seeded content matches the source workbooks: a discipline that silently
     loses its checkpoints still renders as a tickable chip that materialises
     nothing.
  4. The audit FORMAT is standardised — all three categories grade on
     `page_grading`, so one conduct screen and one score rollup serve all of
     them. This is the requirement that the categories be interchangeable in
     everything except which questions they ask.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.audit_compliance import (
    AUDIT_CATEGORIES,
    library_audit_category,
    library_subject_scope,
    list_audit_categories,
)

DATA = Path(__file__).resolve().parents[1] / "app" / "seed" / "data"

# The library JSON behind each category, and what the source workbook holds.
# The counts are asserted rather than derived: they are the whole point of the
# extraction, and a mapping bug (a clause column read as the wrong standard, a
# section banner counted as a checkpoint) shows up here as a number that moved.
SEEDED = {
    "INTERNAL": ("page_industries_checkpoints.json", 120, ["HR", "EHS", "PRODUCTION"]),
    "MANAGEMENT_SYSTEMS": ("page_ims_checkpoints.json", 125, ["QMS", "EMS", "OHS", "ENMS"]),
    "SOCIAL_COMPLIANCE": (
        "page_social_compliance_checkpoints.json",
        45,
        ["LAWS", "HOURS", "WAGES", "HS", "FOA", "CHILD", "DISCRIM", "FORCED", "ENV"],
    ),
}


def _load(name: str) -> list[dict]:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


# ── 1. The menu ───────────────────────────────────────────────────────────


def test_every_category_maps_to_a_library_and_back():
    """The map is a bijection. A category pointing at a library that does not
    classify back to it would offer the right name and materialise the wrong
    checklist — silently, because both halves would look correct alone."""
    for cat in AUDIT_CATEGORIES:
        assert library_audit_category(cat["industryCode"], []) == cat["code"]


def test_the_three_categories_are_the_menu_in_order():
    codes = [c["code"] for c in list_audit_categories()]
    assert codes == ["INTERNAL", "MANAGEMENT_SYSTEMS", "SOCIAL_COMPLIANCE"]


def test_list_audit_categories_hands_out_copies():
    """The wizard receives this straight from the router. A caller mutating a
    row must not edit the module-level menu for every later request."""
    menu = list_audit_categories()
    menu[0]["label"] = "mutated"
    assert list_audit_categories()[0]["label"] == "Internal"


def test_every_category_library_is_seeded():
    for cat in AUDIT_CATEGORIES:
        name, _count, _discs = SEEDED[cat["code"]]
        assert (DATA / name).exists(), f"{cat['code']} has no seed data"


# ── 2. Category and subject are different axes ────────────────────────────


def test_supplier_checklists_carry_no_category():
    """None, not a fourth category. A supplier checklist is reached through the
    audit SUBJECT; treating it as a category would put it in the own-facility
    picker, which is exactly the mismatch `library_subject_scope` exists to
    prevent."""
    assert library_audit_category("SUPPLIER_COC", []) is None
    assert library_audit_category("REGIME_SMETA_LIKE", [{"regimeCode": "SMETA"}]) is None


def test_retired_industry_libraries_carry_no_category():
    assert library_audit_category("CEMENT", [{"category_code": "KILN"}]) is None
    assert library_audit_category("GARMENTS_TEXTILE", []) is None


def test_all_three_categories_are_own_facility():
    """A category audits OUR site. If one classified as VENDOR it would vanish
    from the own-facility picker it was written for."""
    for cat in AUDIT_CATEGORIES:
        assert library_subject_scope(cat["industryCode"], []) == "OWN_SITE"


def test_an_imported_library_may_declare_its_own_category():
    """The explicit hook, so a library loaded later needs no code change."""
    cats = [{"category_code": "X", "audit_category": "social_compliance"}]
    assert library_audit_category("ANYTHING", cats) == "SOCIAL_COMPLIANCE"


# ── 3. The extracted content matches the workbooks ────────────────────────


@pytest.mark.parametrize("code", sorted(SEEDED))
def test_seeded_disciplines_and_counts(code: str):
    name, expected_total, expected_discs = SEEDED[code]
    cats = _load(name)
    assert [c["category_code"] for c in cats] == expected_discs
    assert sum(len(c["checkpoints"]) for c in cats) == expected_total


@pytest.mark.parametrize("code", sorted(SEEDED))
def test_no_discipline_is_empty(code: str):
    """An empty discipline is worse than a missing one: it renders as a
    tickable chip and materialises nothing."""
    name, _total, _discs = SEEDED[code]
    for cat in _load(name):
        assert cat["checkpoints"], f"{code}/{cat['category_code']} has no checkpoints"


@pytest.mark.parametrize("code", sorted(SEEDED))
def test_checkpoint_codes_are_unique_and_questions_non_empty(code: str):
    name, _total, _discs = SEEDED[code]
    cats = _load(name)
    codes = [cp["code"] for c in cats for cp in c["checkpoints"]]
    assert len(codes) == len(set(codes))
    for c in cats:
        for cp in c["checkpoints"]:
            assert cp["question"].strip(), f"{cp['code']} has no question"


def test_ims_disciplines_cite_their_own_standard():
    """The workbook's three clause columns ARE the discipline split. A row lands
    in QMS because column F carries a clause, so every QMS checkpoint must cite
    ISO 9001 and carry a clause reference — a row citing the wrong standard means
    the columns were read in the wrong order."""
    expected = {
        "QMS": "ISO 9001:2015",
        "EMS": "ISO 14001:2015",
        "OHS": "ISO 45001:2018",
        "ENMS": "ISO 50001:2018",
    }
    for cat in _load("page_ims_checkpoints.json"):
        for cp in cat["checkpoints"]:
            assert cp["standard"] == expected[cat["category_code"]]
            assert cp["requirement_reference"].startswith("Clause ")


def test_ims_carries_the_workbooks_audit_reference_as_guidance():
    """Column C is the auditor's method ("i. … ii. … iii. …"). Losing it would
    leave a one-line question where the workbook gave a procedure."""
    cps = [cp for c in _load("page_ims_checkpoints.json") for cp in c["checkpoints"]]
    with_guidance = [cp for cp in cps if cp["guidance"].strip()]
    assert len(with_guidance) == len(cps)


def test_social_compliance_is_statutory_throughout():
    """Every section of Annexure-2 is law-backed. An INTERNAL_REQUIREMENT row
    here would understate a legal exposure on the report."""
    for cat in _load("page_social_compliance_checkpoints.json"):
        for cp in cat["checkpoints"]:
            assert cp["requirement_type"] == "STATUTORY_REGULATORY", cp["code"]


def test_zero_tolerance_sections_are_critical():
    """Child labour, forced labour, discrimination and life safety cannot grade
    as a paperwork gap — criticality is what gates auto-CAPA."""
    by_code = {c["category_code"]: c for c in _load("page_social_compliance_checkpoints.json")}
    for code in ("CHILD", "FORCED", "DISCRIM", "HS"):
        for cp in by_code[code]["checkpoints"]:
            assert cp["criticality"] == "critical", cp["code"]


# ── 4. One standardised format across every category ──────────────────────


@pytest.mark.parametrize("code", sorted(SEEDED))
def test_criticality_is_from_the_engines_vocabulary(code: str):
    """`criticality` gates auto-CAPA and the critical-failure rule. An unknown
    value would not raise — it would just never trigger either."""
    name, _total, _discs = SEEDED[code]
    for cat in _load(name):
        for cp in cat["checkpoints"]:
            assert cp["criticality"] in ("critical", "major", "minor"), cp["code"]


@pytest.mark.parametrize("code", sorted(SEEDED))
def test_requirement_type_is_from_the_grading_vocabulary(code: str):
    name, _total, _discs = SEEDED[code]
    for cat in _load(name):
        for cp in cat["checkpoints"]:
            assert cp["requirement_type"] in (
                "STATUTORY_REGULATORY",
                "INTERNAL_REQUIREMENT",
            ), cp["code"]


def test_audit_type_is_resolvable_from_a_category_for_every_category():
    """`create_audit` falls back to the category's audit type when no caller
    supplied one, so a programme-materialised QMS audit does not read
    "compliance_audit" while a hand-scheduled one reads
    "management_system_audit". This pins the lookup that fallback performs."""
    by_code = {c["code"]: c["auditType"] for c in AUDIT_CATEGORIES}
    assert by_code == {
        "INTERNAL": "internal_audit",
        "MANAGEMENT_SYSTEMS": "management_system_audit",
        "SOCIAL_COMPLIANCE": "social_compliance_audit",
    }
    # An uncategorised library (supplier / retired) finds nothing and keeps the
    # generic type — the fallback must not invent a category for it.
    assert library_audit_category("SUPPLIER_COC", []) not in by_code


def test_discipline_codes_do_not_collide_across_categories():
    """The programme resolves a slot's library by which one covers the most
    planned discipline codes (`programme/materialise._library_for`). Two
    categories sharing a code would make that resolution ambiguous, and a slot
    could materialise against the wrong checklist."""
    seen: dict[str, str] = {}
    for code, (name, _total, _discs) in SEEDED.items():
        for cat in _load(name):
            dup = seen.get(cat["category_code"])
            assert dup is None, f"{cat['category_code']} is in both {dup} and {code}"
            seen[cat["category_code"]] = code


def test_every_category_grades_in_the_internal_audit_format():
    """The standardisation requirement, asserted rather than assumed.

    The seed scripts default `response_type` to `page_grading`, so all three
    categories share one conduct screen, one Grade/Compliance/Risk vocabulary
    and one score rollup. A library declaring a different response type would
    fork the conduct UI for that category alone.
    """
    from scripts.seed_page_audit_category_libraries import _enrich

    for name in ("page_ims_checkpoints.json", "page_social_compliance_checkpoints.json"):
        for cat in _enrich(_load(name), "test"):
            for cp in cat["checkpoints"]:
                assert cp["response_type"] == "page_grading", cp["code"]
                # Escalation must also match: a critical finding carries evidence
                # and raises a CAPA on its own, in every category.
                assert cp["requires_photo_on_fail"] is (cp["criticality"] in ("critical", "major"))
                assert cp["auto_trigger_capa_on_fail"] is (cp["criticality"] == "critical")
