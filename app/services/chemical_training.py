"""Hazmat handling training trigger (spec §4.8 / §6).

The build spec asks to "add HAZMAT_HANDLING as a trigger type, same pattern as
Fire & Life Safety's MISSED_DRILL / CRITICAL_DEFECT". Two corrections found while
implementing it, both of which make the work smaller rather than larger:

  1. Those two Fire & Life Safety trigger types do not exist in this codebase.
     Grepping `MISSED_DRILL` and `CRITICAL_DEFECT` across the Python backend,
     the Next.js app and the Prisma schema returns nothing. The pattern being
     referred to is presumably from a different document or a plan that was not
     built; there is no precedent here to mirror.

  2. There is no trigger-type enum to add to. The Training & Competency engine
     is fully data-driven: `HazardToSkillMapping` rows match
     (sourceModule, classificationField, classificationValue) → competencyId,
     and `TrainingTriggerEvent` carries an arbitrary `classification` blob.
     Adding a new trigger source is a matter of emitting the event with a new
     `sourceModule` and seeding mapping rows — no schema change, no enum, no
     engine change.

So this module emits `sourceModule="CHEMICAL"` trigger events whose
classification carries the hazard classes a person was exposed to, and ships the
default hazard-class → competency mapping seeds. If a hazard class has no
mapping row, that is reported rather than silently producing no assignment — an
exposure that generates no training because a config row is missing is exactly
the kind of quiet nothing this build brief is about.
"""

from __future__ import annotations

import logging
from typing import Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chemical import ChemicalInventoryItem, ChemicalMaster
from app.models.training_engine import HazardToSkillMapping, TrainingTriggerEvent

logger = logging.getLogger(__name__)

SOURCE_MODULE = "CHEMICAL"
#: The classification key `HazardToSkillMapping.classificationField` matches on.
CLASSIFICATION_FIELD = "hazardClass"

#: Event types this module emits. Plain strings — `TrainingTriggerEvent.eventType`
#: is a String column with no enum, deliberately (see module docstring).
EVENT_HAZMAT_HANDLING = "hazmat_handling"
EVENT_HAZMAT_DISPOSAL = "hazmat_disposal"


async def emit_hazmat_training_trigger(
    db: AsyncSession,
    *,
    plant_id: str,
    person_user_ids: Sequence[str],
    chemical: ChemicalMaster,
    source_record_id: str,
    source_record_ref: str | None = None,
    event_type: str = EVENT_HAZMAT_HANDLING,
    activity: str | None = None,
) -> TrainingTriggerEvent | None:
    """Stage a training trigger for people handling a hazardous chemical.

    Staged in the caller's session so it commits with the movement that caused
    it — a training obligation that survives a rolled-back receipt is noise.

    Returns the event, or None when the chemical carries no hazard classes (in
    which case there is genuinely nothing to train on).
    """
    hazard_classes = [str(c) for c in (chemical.hazardClasses or [])]
    if not hazard_classes:
        return None

    unmapped = await unmapped_hazard_classes(db, hazard_classes, plant_id=plant_id)
    if unmapped:
        # Not an error — a tenant may legitimately not train on IRRITANT. But it
        # must be visible, because the alternative is an exposure that quietly
        # assigns nothing and looks identical to one that assigned correctly.
        logger.warning(
            "[chemical_training] no HazardToSkillMapping for %s at plant %s — "
            "no training will be assigned for those classes (chemical %s).",
            ", ".join(unmapped), plant_id, chemical.name,
        )

    ev = TrainingTriggerEvent(
        plantId=plant_id,
        sourceModule=SOURCE_MODULE,
        sourceRecordId=source_record_id,
        sourceRecordRef=source_record_ref,
        eventType=event_type,
        classification={
            "plantId": plant_id,
            # The engine's mapping matcher reads a single value per field, so the
            # primary class drives the match and the full list is carried for
            # provenance and for multi-class mappings added later.
            CLASSIFICATION_FIELD: hazard_classes[0],
            "hazardClasses": hazard_classes,
            "chemicalId": chemical.id,
            "chemicalName": chemical.name,
            "casNumber": chemical.casNumber,
            "physicalState": chemical.physicalState,
            "activity": activity or event_type,
            "personUserIds": list(person_user_ids),
            "unmappedHazardClasses": unmapped,
        },
    )
    db.add(ev)
    return ev


async def unmapped_hazard_classes(
    db: AsyncSession, hazard_classes: Sequence[str], *, plant_id: str | None = None
) -> list[str]:
    """Hazard classes with no competency mapping — the visible config gap."""
    if not hazard_classes:
        return []
    rows = (
        await db.execute(
            select(HazardToSkillMapping.classificationValue)
            .where(HazardToSkillMapping.isActive.is_(True))
            .where(HazardToSkillMapping.isDeleted.is_(False))
            .where(HazardToSkillMapping.classificationField == CLASSIFICATION_FIELD)
            .where(HazardToSkillMapping.sourceModule.in_([SOURCE_MODULE, "ANY"]))
            .where(
                or_(
                    HazardToSkillMapping.plantId.is_(None),
                    HazardToSkillMapping.plantId == plant_id,
                )
            )
            .where(HazardToSkillMapping.classificationValue.in_(list(hazard_classes)))
        )
    ).scalars().all()
    mapped = {str(v) for v in rows}
    return [c for c in hazard_classes if c not in mapped]


async def trigger_for_inventory_movement(
    db: AsyncSession,
    *,
    item: ChemicalInventoryItem,
    person_user_ids: Sequence[str],
    activity: str,
    source_record_id: str,
) -> TrainingTriggerEvent | None:
    """Convenience wrapper for the ledger: exposure implied by a movement."""
    chem = await db.get(ChemicalMaster, item.chemicalId)
    if chem is None:
        return None
    return await emit_hazmat_training_trigger(
        db,
        plant_id=item.plantId,
        person_user_ids=person_user_ids,
        chemical=chem,
        source_record_id=source_record_id,
        source_record_ref=item.batchLotNumber,
        activity=activity,
    )


#: Default hazard-class → competency mapping, applied by
#: prisma/seed-chemical-training-mappings.ts. Competency codes are resolved at
#: seed time; a code that does not exist is reported by the seed rather than
#: inserted as a dangling id.
DEFAULT_COMPETENCY_MAPPINGS: dict[str, str] = {
    "FLAMMABLE": "CHEM_FLAMMABLE_HANDLING",
    "CORROSIVE": "CHEM_CORROSIVE_HANDLING",
    "TOXIC": "CHEM_TOXIC_HANDLING",
    "OXIDIZER": "CHEM_OXIDISER_HANDLING",
    "REACTIVE": "CHEM_REACTIVE_HANDLING",
    "CARCINOGEN": "CHEM_CARCINOGEN_CONTROL",
    "COMPRESSED_GAS": "CHEM_GAS_CYLINDER_HANDLING",
    "EXPLOSIVE": "CHEM_EXPLOSIVES_HANDLING",
    "PYROPHORIC": "CHEM_PYROPHORIC_HANDLING",
    "WATER_REACTIVE": "CHEM_WATER_REACTIVE_HANDLING",
}


__all__ = [
    "SOURCE_MODULE",
    "CLASSIFICATION_FIELD",
    "EVENT_HAZMAT_HANDLING",
    "EVENT_HAZMAT_DISPOSAL",
    "DEFAULT_COMPETENCY_MAPPINGS",
    "emit_hazmat_training_trigger",
    "unmapped_hazard_classes",
    "trigger_for_inventory_movement",
]
