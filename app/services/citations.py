"""Clause-citation provenance — ONE definition, shared by every reader.

**The problem this exists to prevent.** `requirementReference` is a single
free-text string. Before this module, a citation drafted by an AI and a citation
sourced by a compliance professional were byte-identical in the database and
indistinguishable in the report. 127 of the library's 152 checkpoints had no
citation at all; filling them from a draft without recording *how* they were
filled would have converted "we know we have a gap" into "we appear to have
full coverage", which is strictly worse than the gap.

So provenance is recorded per checkpoint, and every surface that reports clause
coverage reads its vocabulary from here rather than re-deriving it.

**Where it lives.** On the checkpoint dict inside
`AuditCheckpointLibrary.categories` (JSON) — NOT as a column on
`AuditCheckpointResponse`.

Three reasons, in order of weight:

  1. A citation is *library content*, not per-audit response data. The library is
     where it is authored and where a reviewer will fix it.
  2. Adding a mapped column to `AuditCheckpointResponse` without its DDL 500s
     **every** query against that table — the omission that took down every CAMS
     screen once already. The backend `.env` points at production, so a model
     change here could not be applied and verified in the same pass.
  3. The report needs a COUNT at generation time, not a per-row join. One lookup
     against the library gives it, and the snapshot then freezes that count
     immutably, which is the correct semantic anyway.

When WP-20's `ClauseRef` catalogue lands, clause data becomes first-class and
this metadata belongs on that row. Until then the library is the system of
record and this module is its accessor.
"""

from __future__ import annotations

from typing import Any, Iterable

# ── Vocabulary ───────────────────────────────────────────────────────

# Authored in the seed before provenance was tracked. NOT a claim that anyone
# verified them — only that they predate the AI-drafted import and were written
# by a human authoring the library. Named ORIGINAL rather than VERIFIED for
# exactly that reason.
ORIGINAL = "ORIGINAL"

# Drafted by an AI against general knowledge of the standards and statutes.
# Plausible, unverified, and must never be presented as sourced fact.
UNVERIFIED_AI_DRAFT = "UNVERIFIED_AI_DRAFT"

# Shipped as demo starter content, explicitly flagged replaceable by whoever
# authored it. Distinct from an AI draft — a person wrote it — but equally
# unverified against the instrument, and it must not be counted as sourced just
# because a human typed it.
UNVERIFIED_STARTER_CONTENT = "UNVERIFIED_STARTER_CONTENT"

# A human with the relevant competence has checked the citation against the
# instrument. Nothing sets this yet — it is the target state of the review pass.
HUMAN_VERIFIED = "HUMAN_VERIFIED"

STATUSES = (
    ORIGINAL,
    UNVERIFIED_AI_DRAFT,
    UNVERIFIED_STARTER_CONTENT,
    HUMAN_VERIFIED,
)

# Membership, not a single constant — the report's "N unverified" figure must
# cover every unverified provenance. Adding a status without adding it here
# would silently shrink that count, which is the one number the whole module
# exists to keep honest.
UNVERIFIED_STATUSES = (UNVERIFIED_AI_DRAFT, UNVERIFIED_STARTER_CONTENT)

# Review priority, separate from status on purpose: an AI draft the drafter was
# confident about and one it explicitly flagged as uncertain share a status but
# are not the same review job. Collapsing them into one bucket would bury the
# four rows that most need a human.
PRIORITY = "PRIORITY"
NORMAL = "NORMAL"

# Keys written onto the checkpoint dict. Namespaced so they cannot collide with
# the library's existing checkpoint fields.
KEY_STATUS = "citation_status"
KEY_CONFIDENCE = "citation_confidence"
KEY_PRIORITY = "citation_review_priority"
KEY_NOTE = "citation_sourcing_note"
KEY_SOURCE = "citation_source"

# The sentence the report prints. Written once, here, so the PDF, the HTML view
# and any export cannot word it differently.
UNVERIFIED_STATEMENT = (
    "{n} of the {total} clause citations in this audit's checkpoint library are "
    "AI-drafted and have not been verified against the cited instrument. They are "
    "shown for navigation, not as an assurance that the clause reference is correct."
)


def is_unverified(cp: dict[str, Any]) -> bool:
    return cp.get(KEY_STATUS) in UNVERIFIED_STATUSES


def combined_reference(clause: str | None, statutory: str | None) -> str:
    """Draft clause + draft statute -> the library's existing citation format.

    The populated rows use a comma-separated list in one free-text field, most
    often clause first then statute (`SA8000:2014 Cl.1, Child Labour Act 1986`).
    A minority are statute-first (`Factories Act §21-27, ISO 45001 Cl.8.1`), so
    the order is a convention rather than a rule; clause-first matches the
    majority and the SA8000 exemplars.

    41 of the 127 drafted rows have no statutory instrument, and those emit the
    clause alone — the same shape as the existing single-citation rows
    (`IS 2190`).
    """
    parts = [p.strip() for p in (clause, statutory) if p and p.strip()]
    return ", ".join(parts)


def summarise(categories: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Citation provenance across a library. Pure — takes the categories JSON.

    This is the shape the report footnote and the gap measurement both read, so
    the two cannot disagree about how many citations are unverified.
    """
    total = cited = 0
    by_status: dict[str, int] = {}
    priority = 0
    uncited: list[str] = []

    for cat in categories or []:
        for cp in cat.get("checkpoints") or []:
            total += 1
            ref = (cp.get("requirement_reference") or "").strip()
            if ref:
                cited += 1
            else:
                uncited.append(cp.get("code") or "?")
            # A citation with no recorded status predates provenance tracking.
            st = cp.get(KEY_STATUS) or (ORIGINAL if ref else None)
            if st:
                by_status[st] = by_status.get(st, 0) + 1
            if cp.get(KEY_PRIORITY) == PRIORITY:
                priority += 1

    unverified = sum(by_status.get(s, 0) for s in UNVERIFIED_STATUSES)
    return {
        "total": total,
        "cited": cited,
        "uncited": total - cited,
        "uncitedCodes": uncited,
        "byStatus": by_status,
        "unverified": unverified,
        "priorityReview": priority,
        "verifiedPct": round((cited - unverified) / total * 100, 1) if total else 0.0,
        # The headline, phrased so full coverage is never mistaken for full
        # confidence.
        "statement": (
            f"{total - cited} gap(s), {unverified} unverified"
            if unverified
            else f"{total - cited} gap(s)"
        ),
    }


def report_footnote(summary: dict[str, Any]) -> dict[str, Any] | None:
    """The block a report renders, or None when there is nothing to declare."""
    n = summary.get("unverified", 0)
    if not n:
        return None
    return {
        "unverifiedCount": n,
        "totalCitations": summary.get("cited", 0),
        "priorityReviewCount": summary.get("priorityReview", 0),
        "statement": UNVERIFIED_STATEMENT.format(n=n, total=summary.get("cited", 0)),
    }


__all__ = [
    "HUMAN_VERIFIED",
    "KEY_CONFIDENCE",
    "KEY_NOTE",
    "KEY_PRIORITY",
    "KEY_SOURCE",
    "KEY_STATUS",
    "NORMAL",
    "ORIGINAL",
    "PRIORITY",
    "STATUSES",
    "UNVERIFIED_AI_DRAFT",
    "UNVERIFIED_STATEMENT",
    "combined_reference",
    "is_unverified",
    "report_footnote",
    "summarise",
]
