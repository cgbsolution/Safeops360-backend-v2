"""Statutory obligations — the read interface CAMS consumes.

docs/cams/07 WP-52 · docs/cams/04-target.md §7.3 · open question Q8 (answered:
**yes, Statutory Registers is the system of record**).

**The problem this fixes.** `app/services/cams.py` imported
`app.models.erm_p2.LegalObligation` directly, inside a bare `try/except` that
set the model to `None` on any failure. `compute_compliance` then returned

    {"totalObligations": 0, "verifiedPct": 0, "rows": [], ...}

So a broken dependency — a renamed module, an import cycle, a missing table —
rendered as **"you have no statutory obligations and 0% assurance"**, which is
indistinguishable from a genuinely empty register and reads as *good news* on a
compliance dashboard. That is F-48, and it is the same failure shape as F-29:
a silent default standing in for an unknown.

**What replaces it.** One module owns the obligation read path. It reports its
own availability explicitly, and a caller that cannot get data gets
`ObligationsUnavailable` — never zeros. The Compliance Tracker then renders
"the obligations register could not be read" instead of a confident 0%.

Keeping it a service (not a direct model import) also means the eventual move of
Statutory Registers behind an API, or into a separate deployment, is a change to
this file alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class ObligationsUnavailable(RuntimeError):
    """The obligations register could not be read.

    Deliberately an exception rather than an empty list: the two are different
    facts, and the whole point of this module is to stop them being conflated.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ObligationSummary:
    """The obligation shape CAMS needs — not the full ERM row.

    Narrowing here is what keeps the boundary meaningful: CAMS reads seven
    fields, so CAMS depends on seven fields, and ERM stays free to change the
    rest.
    """

    id: str
    obligationCode: str
    title: str
    siteId: str | None
    status: str
    statuteReference: str | None
    validUntil: datetime | None
    renewalLeadDays: int | None
    regulatorName: str | None = None
    criticality: str | None = None

    @property
    def renewalDueAt(self) -> datetime | None:
        """`validUntil − renewalLeadDays`.

        The Compliance Tracker showed rows reading OVERDUE beside a future
        expiry date, which looked like a bug and was not: the obligation is
        overdue *for renewal*, and this is the date that makes the row make
        sense (F-49).
        """
        if self.validUntil is None:
            return None
        return self.validUntil - timedelta(days=self.renewalLeadDays or 0)


def _model():
    """Resolve the backing model, or explain precisely why it is unavailable.

    The import failure is surfaced verbatim — "no module named erm_p2" and "table
    LegalObligation does not exist" need different fixes, and a generic
    "unavailable" hides which one you have.
    """
    try:
        from app.models.erm_p2 import LegalObligation

        return LegalObligation
    except Exception as e:  # noqa: BLE001
        raise ObligationsUnavailable(
            f"The statutory obligations register (Statutory Registers) is not available: {e}"
        ) from e


def is_available() -> bool:
    """Cheap probe for callers that want to degrade gracefully and SAY SO."""
    try:
        _model()
        return True
    except ObligationsUnavailable:
        return False


async def list_obligations(
    db: AsyncSession, *, site_id: str | None = None
) -> list[ObligationSummary]:
    """Every live obligation, narrowed to what CAMS consumes.

    Raises `ObligationsUnavailable` rather than returning `[]` when the register
    cannot be read.
    """
    Model = _model()
    q = select(Model).where(Model.isDeleted.is_(False))
    if site_id:
        q = q.where(Model.siteId == site_id)
    try:
        rows = (await db.execute(q)).scalars().all()
    except Exception as e:  # noqa: BLE001 — table missing, permissions, etc.
        raise ObligationsUnavailable(
            f"The statutory obligations register could not be queried: {e}"
        ) from e

    return [
        ObligationSummary(
            id=o.id,
            obligationCode=getattr(o, "obligationCode", "") or "",
            title=getattr(o, "title", "") or "",
            siteId=getattr(o, "siteId", None),
            status=getattr(o, "status", "") or "",
            statuteReference=getattr(o, "statuteReference", None),
            validUntil=getattr(o, "validUntil", None),
            renewalLeadDays=getattr(o, "renewalLeadDays", None),
            regulatorName=getattr(o, "regulatorName", None),
            criticality=getattr(o, "criticality", None),
        )
        for o in rows
    ]


async def obligation_count(db: AsyncSession, *, site_id: str | None = None) -> int:
    return len(await list_obligations(db, site_id=site_id))


def unavailable_payload(reason: str) -> dict[str, Any]:
    """The response shape a consumer renders when the register cannot be read.

    Note what is NOT here: no zeros. `totalObligations` is `None`, not 0, so a
    UI cannot accidentally display "0 obligations · 100% compliant" over a
    failed dependency.
    """
    return {
        "available": False,
        "unavailableReason": reason,
        "totalObligations": None,
        "verifiedByAuditCount": None,
        "verifiedPct": None,
        "openNcCount": None,
        "statusCounts": {},
        "rows": [],
    }


__all__ = [
    "ObligationsUnavailable",
    "ObligationSummary",
    "is_available",
    "list_obligations",
    "obligation_count",
    "unavailable_payload",
]
