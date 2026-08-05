"""Audit number generation — the collision that took Schedule Audit down.

`_next_number` used `COUNT(*) + 1`. Two facts make that unsafe, and together
they turned every "Schedule audit" click into a 500:

  1. `ComplianceAudit.auditNumber` is UNIQUE across the PHYSICAL table, and a
     soft-delete leaves the row (and its number) behind.
  2. `ComplianceAudit` is a governed entity, so the global soft-delete filter in
     `app.core.soft_delete` rewrites every ORM SELECT to `isDeleted = false`.
     COUNT therefore saw 9 of 18 real rows and re-proposed
     `AUD-GT-2026-NW-0010` — an existing LIVE audit.

It could not self-heal: the count only advances when a create succeeds, and no
create could succeed. So these tests pin the two properties that matter rather
than the happy path — that the query reads MAX, and that it opts out of the
soft-delete filter.

House style: no async-DB harness in this suite, so the session is a stand-in
that records the statement it was handed (mirrors tests/test_independence.py).
"""

from __future__ import annotations

import asyncio

from app.services.audit_compliance import _industry_short, _next_number


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _RecordingSession:
    """Captures the statement so we can assert on the SQL that was built."""

    def __init__(self, value):
        self._value = value
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Result(self._value)


def _run(max_suffix, industry="GARMENTS_TEXTILE", plant="NW"):
    db = _RecordingSession(max_suffix)
    number = asyncio.run(_next_number(db, industry, plant))
    return number, db.statement


# ── The regression itself ────────────────────────────────────────────────────


def test_uses_max_plus_one_not_row_count():
    """18 rows exist, the highest suffix is 18 -> next is 0019, never 0010."""
    number, _ = _run(18)
    assert number.endswith("-0019")


def test_next_number_ignores_the_soft_delete_filter():
    """The whole bug. Without include_deleted the statement is silently
    rewritten to live rows only, and the number collides with a soft-deleted
    (or, as in production, a still-live) row."""
    _, stmt = _run(18)
    assert stmt.get_execution_options().get("include_deleted") is True


def test_query_selects_max_not_count():
    _, stmt = _run(18)
    sql = str(stmt).lower()
    assert "max(" in sql
    assert "count(" not in sql


def test_reads_from_complianceaudit():
    _, stmt = _run(18)
    assert "complianceaudit" in str(stmt).lower()


# ── Format ───────────────────────────────────────────────────────────────────


def test_format_is_stable():
    number, _ = _run(18, industry="GARMENTS_TEXTILE", plant="NW")
    head, tail = number.rsplit("-", 1)
    assert head == f"AUD-GT-{_year_of(number)}-NW"
    assert tail == "0019"
    assert len(tail) == 4


def test_empty_table_starts_at_0001():
    """`scalar()` returns None on an empty table; that must not crash."""
    number, _ = _run(None)
    assert number.endswith("-0001")


def test_sequence_is_zero_padded_to_four():
    assert _run(8)[0].endswith("-0009")
    assert _run(999)[0].endswith("-1000")


def test_sequence_survives_passing_four_digits():
    """Padding is a minimum, not a truncation — 10000 must not become 0000."""
    assert _run(9999)[0].endswith("-10000")


def test_industry_short_matches_the_numbers_already_in_the_tenant():
    assert _industry_short("GARMENTS_TEXTILE") == "GT"
    assert _industry_short("CHEMICAL_PROCESS") == "CP"
    assert _industry_short("PHARMA_LIFE_SCIENCES") == "PLS"
    assert _industry_short("") == "AC"  # fallback, never an empty segment


def _year_of(number: str) -> str:
    return number.split("-")[2]
