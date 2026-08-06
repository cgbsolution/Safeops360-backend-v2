"""SDS attachment, onboarding status transitions and the review cycle (§4.1/§4.5).

Scope boundary, restated because it is easy to erode
────────────────────────────────────────────────────
The SDS is EVIDENCE, not a data source. This module uses the shared
evidence-attachment layer's basic tier only: upload, store, view a PDF against
a record. Nothing here reads inside the file. Flash point, NFPA ratings and
hazard phrases are entered by a human who has read the sheet, and
`ChemicalMaster.hazardClassificationSource` is constrained to MANUAL|IMPORTED so
a future extraction feature cannot quietly overwrite that. AI/OCR extraction is
a separate airgapped commercial add-on and is out of scope (build spec §0/§8).

The two rules that shape this file:

  §1  A chemical cannot reach ACTIVE without a linked SDS — enforced by a CHECK
      constraint, so `activate()` cannot succeed without one even if a caller
      forgets to look. The check here exists to produce a readable error, not to
      be the enforcement.

  §6  SDS review overdue is a visible compliance signal, NOT an automatic
      deactivation. The nightly batch sets a flag and raises a Daily Brief card;
      it never touches `status`. Deactivating a chemical because its paperwork
      aged would stop production over a filing lapse, and the predictable result
      is that someone disables the batch job.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chemical import DEFAULT_SDS_VALIDITY_YEARS, ChemicalMaster

logger = logging.getLogger(__name__)


class SdsError(ValueError):
    """Operator-facing problem with an SDS or a status transition."""


def compute_review_due(
    revision_date: datetime, validity_years: int = DEFAULT_SDS_VALIDITY_YEARS
) -> datetime:
    """Review due date from the sheet's revision date.

    365.25 days per year rather than `replace(year=...)`: the latter raises on
    29 February, which is a real revision date on roughly one sheet in 1,460 and
    is not the failure anyone wants to debug.
    """
    return revision_date + timedelta(days=int(round(365.25 * validity_years)))


async def attach_sds(
    db: AsyncSession,
    *,
    chemical_id: str,
    attachment_id: str,
    revision_date: datetime,
    user_id: str,
    validity_years: int = DEFAULT_SDS_VALIDITY_YEARS,
) -> ChemicalMaster:
    """Link an uploaded SDS document to a chemical and set its review clock.

    Does NOT activate the chemical: §4.1 puts an HSE Manager review between
    "SDS present" and ACTIVE. Attaching a sheet is not the same as someone
    competent having read it.
    """
    chem = await db.get(ChemicalMaster, chemical_id)
    if chem is None or chem.isDeleted:
        raise SdsError("Chemical not found.")

    chem.sdsAttachmentId = attachment_id
    chem.sdsRevisionDate = revision_date
    chem.sdsReviewDueDate = compute_review_due(revision_date, validity_years)
    # A fresh sheet clears a previous overdue flag — the signal is about THIS
    # sheet's age, and leaving it set would keep a resolved finding on the brief.
    chem.sdsReviewOverdue = False
    chem.sdsReviewFlaggedAt = None
    chem.updatedBy = user_id
    await db.flush()
    return chem


async def activate(
    db: AsyncSession, *, chemical_id: str, user_id: str
) -> ChemicalMaster:
    """PENDING_SDS → ACTIVE after HSE Manager review (§4.1)."""
    chem = await db.get(ChemicalMaster, chemical_id)
    if chem is None or chem.isDeleted:
        raise SdsError("Chemical not found.")
    if not chem.sdsAttachmentId:
        # The database would reject this too (ck_ChemicalMaster_active_requires_sds).
        # Catching it here turns a 500 into a sentence someone can act on.
        raise SdsError(
            f"'{chem.name}' cannot be activated without a Safety Data Sheet attached. "
            f"Upload the SDS, then approve."
        )
    if not (chem.hazardClasses or []):
        raise SdsError(
            f"'{chem.name}' has no hazard classification. Enter the classification from "
            f"the SDS before activating — threshold and co-storage rules key off it, and "
            f"an unclassified chemical is invisible to both."
        )
    chem.status = "ACTIVE"
    chem.approvedByUserId = user_id
    chem.approvedAt = datetime.now(timezone.utc)
    chem.updatedBy = user_id
    await db.flush()
    return chem


async def set_status(
    db: AsyncSession,
    *,
    chemical_id: str,
    status: str,
    user_id: str,
    reason: str | None = None,
) -> ChemicalMaster:
    chem = await db.get(ChemicalMaster, chemical_id)
    if chem is None or chem.isDeleted:
        raise SdsError("Chemical not found.")
    if status == "ACTIVE":
        return await activate(db, chemical_id=chemical_id, user_id=user_id)
    if status == "RESTRICTED" and not (reason or "").strip():
        raise SdsError("A restriction reason is required so users understand the constraint.")
    chem.status = status
    if status == "RESTRICTED":
        chem.restrictionReason = reason
    chem.updatedBy = user_id
    await db.flush()
    return chem


# ── nightly batch (§4.5) ──────────────────────────────────────────────────────
async def flag_overdue_sds_reviews(
    db: AsyncSession, *, tenant_id: str | None = None
) -> dict[str, int]:
    """Nightly sweep: flag chemicals whose SDS review date has passed.

    Sets `sdsReviewOverdue` and stamps `sdsReviewFlaggedAt`. Deliberately does
    NOT change `status` — see the module docstring and business rule §6.

    Also clears the flag on chemicals whose sheet was renewed since the last
    run, so the Daily Brief count reflects reality rather than accumulating.
    Returns {flagged, cleared} for the job log.
    """
    now = datetime.now(timezone.utc)

    flag_stmt = (
        update(ChemicalMaster)
        .where(ChemicalMaster.isDeleted.is_(False))
        .where(ChemicalMaster.sdsReviewDueDate.isnot(None))
        .where(ChemicalMaster.sdsReviewDueDate < now)
        .where(ChemicalMaster.sdsReviewOverdue.is_(False))
        .values(sdsReviewOverdue=True, sdsReviewFlaggedAt=now)
    )
    clear_stmt = (
        update(ChemicalMaster)
        .where(ChemicalMaster.isDeleted.is_(False))
        .where(ChemicalMaster.sdsReviewOverdue.is_(True))
        .where(
            (ChemicalMaster.sdsReviewDueDate.is_(None))
            | (ChemicalMaster.sdsReviewDueDate >= now)
        )
        .values(sdsReviewOverdue=False, sdsReviewFlaggedAt=None)
    )
    if tenant_id:
        flag_stmt = flag_stmt.where(ChemicalMaster.tenantId == tenant_id)
        clear_stmt = clear_stmt.where(ChemicalMaster.tenantId == tenant_id)

    flagged = (await db.execute(flag_stmt)).rowcount or 0
    cleared = (await db.execute(clear_stmt)).rowcount or 0
    await db.flush()
    logger.info("[chemical_sds] SDS review sweep: %d flagged, %d cleared", flagged, cleared)
    return {"flagged": int(flagged), "cleared": int(cleared)}


async def overdue_sds(
    db: AsyncSession, *, tenant_id: str, limit: int = 50
) -> list[ChemicalMaster]:
    """Chemicals with an overdue SDS review, worst first — the Daily Brief card
    and the Command Centre widget both read this."""
    return list(
        (
            await db.execute(
                select(ChemicalMaster)
                .where(ChemicalMaster.tenantId == tenant_id)
                .where(ChemicalMaster.isDeleted.is_(False))
                .where(ChemicalMaster.sdsReviewOverdue.is_(True))
                .order_by(ChemicalMaster.sdsReviewDueDate)
                .limit(limit)
            )
        ).scalars().all()
    )


async def expiring_sds(
    db: AsyncSession, *, tenant_id: str, within_days: int = 60, limit: int = 50
) -> list[ChemicalMaster]:
    """Sheets due for review soon but not yet overdue — the actionable window."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=within_days)
    return list(
        (
            await db.execute(
                select(ChemicalMaster)
                .where(ChemicalMaster.tenantId == tenant_id)
                .where(ChemicalMaster.isDeleted.is_(False))
                .where(ChemicalMaster.sdsReviewOverdue.is_(False))
                .where(ChemicalMaster.sdsReviewDueDate.isnot(None))
                .where(ChemicalMaster.sdsReviewDueDate <= horizon)
                .where(ChemicalMaster.sdsReviewDueDate >= now)
                .order_by(ChemicalMaster.sdsReviewDueDate)
                .limit(limit)
            )
        ).scalars().all()
    )


__all__ = [
    "SdsError",
    "compute_review_due",
    "attach_sds",
    "activate",
    "set_status",
    "flag_overdue_sds_reviews",
    "overdue_sds",
    "expiring_sds",
]
