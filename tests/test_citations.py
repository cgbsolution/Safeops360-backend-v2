"""Clause-citation provenance — the distinction the import exists to create.

127 of the 152 real-library citations are AI drafts. The whole point of this
module is that a report can never present them as sourced fact, so these tests
pin the two ways that could silently break:

  1. `summarise` losing the unverified count (full coverage reported as full
     confidence — strictly worse than the original gap).
  2. `combined_reference` producing a format that does not match the citations
     authored by hand, which would make drafted rows visually identifiable only
     by accident rather than by their recorded status.
"""

from __future__ import annotations

from app.services import citations as cit


def _cp(code: str, ref: str = "", **extra):
    return {"code": code, "requirement_reference": ref, **extra}


def _lib(*checkpoints):
    return [{"category_code": "CAT", "checkpoints": list(checkpoints)}]


# ── combined_reference — must match the hand-authored format ────────


def test_clause_and_statute_join_clause_first():
    """Matches the dominant hand-authored shape: `SA8000:2014 Cl.1, Child
    Labour Act 1986`."""
    assert cit.combined_reference("SA8000:2014 Cl.1", "Child Labour Act 1986") == (
        "SA8000:2014 Cl.1, Child Labour Act 1986"
    )


def test_clause_alone_when_no_statute():
    """41 of the 127 drafted rows have no statutory instrument. They must emit
    the clause alone — the same shape as the existing `IS 2190` rows — not a
    dangling separator."""
    assert cit.combined_reference("ISO 45001 Cl.8.1.2", "") == "ISO 45001 Cl.8.1.2"
    assert cit.combined_reference("ISO 45001 Cl.8.1.2", None) == "ISO 45001 Cl.8.1.2"
    assert cit.combined_reference("ISO 45001 Cl.8.1.2", "   ") == "ISO 45001 Cl.8.1.2"


def test_statute_alone_when_no_clause():
    assert cit.combined_reference("", "Factories Act §38") == "Factories Act §38"


def test_both_empty_yields_empty_not_a_comma():
    assert cit.combined_reference("", "") == ""
    assert cit.combined_reference(None, None) == ""


# ── summarise — the count that keeps the report honest ──────────────


def test_unverified_count_survives_full_coverage():
    """The defect this guards: every checkpoint cited, so `uncited` is 0 — and
    a reader concludes the clause library is complete AND sound. It is complete
    and 2/3 unverified, and the summary has to keep saying so."""
    cats = _lib(
        _cp("A", "ISO 45001 Cl.8.1", **{cit.KEY_STATUS: cit.ORIGINAL}),
        _cp("B", "ISO 45001 Cl.7.2", **{cit.KEY_STATUS: cit.UNVERIFIED_AI_DRAFT}),
        _cp("C", "Factories Act §38", **{cit.KEY_STATUS: cit.UNVERIFIED_AI_DRAFT}),
    )
    s = cit.summarise(cats)
    assert s["total"] == 3
    assert s["cited"] == 3
    assert s["uncited"] == 0
    assert s["unverified"] == 2
    # The headline never states a gap count without the unverified count.
    assert s["statement"] == "0 gap(s), 2 unverified"


def test_statement_omits_unverified_only_when_there_are_none():
    cats = _lib(_cp("A", "ISO 45001 Cl.8.1", **{cit.KEY_STATUS: cit.ORIGINAL}))
    assert cit.summarise(cats)["statement"] == "0 gap(s)"


def test_a_citation_with_no_status_counts_as_original_not_unverified():
    """Rows authored before provenance tracking must not be swept into the
    unverified bucket — that would overstate the problem and make the real
    unverified count untrustworthy in the other direction."""
    cats = _lib(_cp("A", "IS 2190"))
    s = cit.summarise(cats)
    assert s["byStatus"] == {cit.ORIGINAL: 1}
    assert s["unverified"] == 0


def test_uncited_rows_are_named_not_just_counted():
    cats = _lib(_cp("A", "IS 2190"), _cp("B", ""), _cp("C", "   "))
    s = cit.summarise(cats)
    assert s["uncited"] == 2
    assert s["uncitedCodes"] == ["B", "C"]


def test_starter_content_counts_as_unverified_too():
    """The Supplier Code of Conduct library is human-written starter content —
    a person typed it, but nobody checked it against SA8000/SMETA. Counting it
    as sourced because it was not AI-generated would put 39 unverified citations
    behind a clean report."""
    cats = _lib(
        _cp("A", "SA8000 §5", **{cit.KEY_STATUS: cit.UNVERIFIED_STARTER_CONTENT}),
        _cp("B", "ISO 45001 Cl.7.2", **{cit.KEY_STATUS: cit.UNVERIFIED_AI_DRAFT}),
        _cp("C", "IS 2190", **{cit.KEY_STATUS: cit.ORIGINAL}),
    )
    s = cit.summarise(cats)
    # Both unverified provenances count; ORIGINAL does not.
    assert s["unverified"] == 2
    assert s["statement"] == "0 gap(s), 2 unverified"


def test_every_unverified_status_is_in_the_membership_tuple():
    """Guard against the drift this design is exposed to: adding a status
    without adding it to UNVERIFIED_STATUSES would silently shrink the one
    number the module exists to keep honest."""
    for st in cit.STATUSES:
        if st.startswith("UNVERIFIED"):
            assert st in cit.UNVERIFIED_STATUSES, f"{st} missing from UNVERIFIED_STATUSES"


def test_priority_review_is_counted_separately_from_status():
    """The 4 LOW-confidence rows share a status with the 78 HIGH ones but are a
    different review job — collapsing them would bury the rows that most need a
    human."""
    cats = _lib(
        _cp("A", "x", **{cit.KEY_STATUS: cit.UNVERIFIED_AI_DRAFT,
                         cit.KEY_PRIORITY: cit.PRIORITY}),
        _cp("B", "y", **{cit.KEY_STATUS: cit.UNVERIFIED_AI_DRAFT,
                         cit.KEY_PRIORITY: cit.NORMAL}),
    )
    s = cit.summarise(cats)
    assert s["unverified"] == 2
    assert s["priorityReview"] == 1


def test_empty_library_does_not_divide_by_zero():
    s = cit.summarise([])
    assert s["total"] == 0 and s["verifiedPct"] == 0.0


# ── report_footnote — present only when there is something to declare ──


def test_footnote_absent_when_nothing_is_unverified():
    cats = _lib(_cp("A", "IS 2190", **{cit.KEY_STATUS: cit.ORIGINAL}))
    assert cit.report_footnote(cit.summarise(cats)) is None


def test_footnote_states_the_count_and_that_it_is_not_assurance():
    cats = _lib(
        _cp("A", "x", **{cit.KEY_STATUS: cit.UNVERIFIED_AI_DRAFT}),
        _cp("B", "y", **{cit.KEY_STATUS: cit.ORIGINAL}),
    )
    f = cit.report_footnote(cit.summarise(cats))
    assert f is not None
    assert f["unverifiedCount"] == 1
    assert "1 of the 2" in f["statement"]
    assert "have not been verified" in f["statement"]
    # It must say what the citations are NOT — navigation, not assurance.
    assert "not as an assurance" in f["statement"]
