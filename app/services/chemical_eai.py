"""EAI Register linkage for disposal and storage data (spec §4.8 / §6).

DESIGN DECISION worth reading before changing this
──────────────────────────────────────────────────
The spec says disposal and storage data "feed environmental aspect/impact
entries". The obvious reading is "auto-create an EaiEntry per disposal". This
module deliberately does NOT do that, and the reason is not effort:

`EaiEntry` requires `initialLikelihoodId`, `initialLikelihoodScore`,
`initialMagnitudeId`, `initialMagnitudeScore`, `initialImpactScore` and
`initialImpactLevel` — all NOT NULL. Those are a competent person's assessment
of environmental consequence against the tenant's impact matrix. There is no
defensible way to derive them from "we disposed of 200 kg of solvent through a
licensed vendor". Auto-populating them would put invented risk scores into the
register an ISO 14001 auditor reads, and a register full of machine-invented
scores is worse than one with a visible gap: the gap prompts a human, the
invented score prevents one.

So the linkage is:

  * **Attach to an existing entry when one already covers this activity.** An
    EaiEntry whose study covers the plant and whose activity is hazardous-waste
    disposal gets the disposal appended to `materialsUsed` and its
    `triggeredByRecordId` set. The quantification is real data added to a human's
    assessment — which is what "feeds the register" should mean.
  * **Otherwise report the gap.** Return None with a reason. `eaiEntryId` stays
    null on the DisposalRecord, and the Daily Brief surfaces the count of
    disposals not represented in the EAI Register so an environmental engineer
    opens a study rather than the platform pretending one exists.

Never raises: the caller treats linkage as best-effort because the DisposalRecord
is the statutory artefact and must survive the EAI module being disabled.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chemical import ChemicalDisposalRecord, ChemicalMaster
from app.models.eai import EaiEntry, EaiStudy

logger = logging.getLogger(__name__)

#: Activity descriptions that indicate an entry already covers waste disposal.
#: Matched case-insensitively as substrings — intentionally loose, because a
#: false positive attaches real quantification data to a related entry (mildly
#: untidy) while a false negative silently drops the linkage (the failure mode
#: this module exists to avoid).
_DISPOSAL_ACTIVITY_HINTS = (
    "hazardous waste",
    "waste disposal",
    "chemical disposal",
    "waste handling",
    "effluent",
)


async def find_covering_entry(
    db: AsyncSession, *, plant_id: str, chemical_name: str
) -> EaiEntry | None:
    """An ACTIVE/APPROVED EaiEntry at this plant that already covers disposal."""
    stmt = (
        select(EaiEntry)
        .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
        .where(EaiStudy.plantId == plant_id)
        .where(EaiStudy.status.in_(["ACTIVE", "APPROVED"]))
        .where(EaiEntry.isCurrentVersion.is_(True))
        .where(EaiEntry.status.in_(["APPROVED", "ACTIVE"]))
        .where(
            or_(
                *[
                    func.lower(EaiEntry.activityDescription).like(f"%{hint}%")
                    for hint in _DISPOSAL_ACTIVITY_HINTS
                ],
                func.lower(EaiEntry.activityDescription).like(f"%{chemical_name.lower()}%"),
            )
        )
        .order_by(EaiEntry.sequenceNumber)
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def link_disposal_to_eai(
    db: AsyncSession, record: ChemicalDisposalRecord
) -> str | None:
    """Append a disposal's quantification to a covering EaiEntry.

    Returns the entry id, or None when no entry covers this activity — which is
    a reportable gap, not an error.
    """
    chem = await db.get(ChemicalMaster, record.chemicalId)
    chem_name = chem.name if chem else "chemical"

    entry = await find_covering_entry(db, plant_id=record.plantId, chemical_name=chem_name)
    if entry is None:
        logger.info(
            "[chemical_eai] no covering EAI entry at plant %s for disposal %s (%s). "
            "Recorded as an unrepresented disposal for the Daily Brief.",
            record.plantId, record.id, chem_name,
        )
        return None

    materials: list[Any] = list(entry.materialsUsed or [])
    materials.append(
        {
            "source": "CHEMICAL_DISPOSAL",
            "disposalRecordId": record.id,
            "chemicalId": record.chemicalId,
            "chemicalName": chem_name,
            "quantity": record.quantity,
            "unit": record.unit,
            "disposalDate": record.disposalDate.isoformat() if record.disposalDate else None,
            "manifestReference": record.manifestReference,
            "disposalVendor": record.disposalVendor,
            "wasteCategory": record.wasteCategory,
            "hazardClasses": list(chem.hazardClasses or []) if chem else [],
            "linkedAt": datetime.now(timezone.utc).isoformat(),
        }
    )
    # Re-assign rather than mutate: SQLAlchemy does not track in-place mutation
    # of a plain JSON column, so an append alone would never be persisted.
    entry.materialsUsed = materials
    entry.triggeredByRecordId = record.id
    await db.flush()
    return entry.id


async def unrepresented_disposal_count(
    db: AsyncSession, *, tenant_id: str, plant_id: str | None = None, days: int = 90
) -> int:
    """Disposals with no EAI entry in the last `days` — the Daily Brief signal
    that the environmental register has drifted behind operations."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = (
        select(func.count(ChemicalDisposalRecord.id))
        .where(ChemicalDisposalRecord.tenantId == tenant_id)
        .where(ChemicalDisposalRecord.isDeleted.is_(False))
        .where(ChemicalDisposalRecord.eaiEntryId.is_(None))
        .where(ChemicalDisposalRecord.disposalDate >= since)
    )
    if plant_id:
        stmt = stmt.where(ChemicalDisposalRecord.plantId == plant_id)
    return int((await db.execute(stmt)).scalar() or 0)


__all__ = ["link_disposal_to_eai", "find_covering_entry", "unrepresented_disposal_count"]
