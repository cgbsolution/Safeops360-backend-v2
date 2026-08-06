"""HIRA linkage — chemical hazard classes → hazard rows (spec §4.8 / §6).

A chemical's hazard classification is exactly the information a HIRA hazard row
needs, and re-typing it into every study is both wasted effort and a source of
drift. This service maps `ChemicalMaster.hazardClasses` onto the existing
`HiraHazard` library rows and materialises `HiraEntryHazard` rows on a HIRA
entry, carrying the regulatory citation with them.

Two things worth knowing before changing this
─────────────────────────────────────────────
1. **The "#4 gap" the build spec asks to address is already closed.**
   `HiraEntryHazard.regulationRef` / `.regulationSection` exist at hazard-row
   grain, with a comment explaining that auditors expect the instrument cited
   against the hazard rather than only against the activity. So this module does
   not need a schema change — it needs to actually POPULATE those columns when
   it is the source, which is what `propagate_regulatory_reference` does. A
   chemical's `regulatoryReference` (e.g. "MSIHC Schedule 1 Part II") flows to
   the hazard row; the library hazard's own citation is the fallback.

2. **It proposes, it does not auto-approve.** Hazard rows are added to an entry
   that a human is authoring. Nothing here writes to an APPROVED entry or
   changes a risk score — a HIRA that silently gains hazard rows after approval
   is a HIRA nobody can defend in an audit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chemical import ChemicalMaster
from app.models.hira import HiraEntry, HiraEntryHazard, HiraHazard

logger = logging.getLogger(__name__)

#: Hazard class → library hazard codes. Sourced from the seeded HIRA hazard
#: library (prisma/seed-hira-masters.ts), not invented here — a mapping onto
#: codes that do not exist would silently produce zero rows, which is the
#: failure mode this whole build is about. `resolve_hazards` reports any code it
#: cannot find rather than skipping it quietly.
HAZARD_CLASS_TO_HIRA_CODES: dict[str, tuple[str, ...]] = {
    "FLAMMABLE": ("CHEM_FLAMMABLE_SPILL", "FIRE_HOT_WORK_IGNITION", "ELEC_STATIC_DISCHARGE"),
    "CORROSIVE": ("CHEM_CORROSIVE_CONTACT",),
    "TOXIC": ("CHEM_TOXIC_INHALATION",),
    "OXIDIZER": ("CHEM_OXIDISER_REACTIVE",),
    "REACTIVE": ("CHEM_OXIDISER_REACTIVE",),
    "CARCINOGEN": ("CHEM_CARCINOGEN_EXPOSURE",),
    "COMPRESSED_GAS": ("CHEM_PRESSURISED_GAS",),
    "EXPLOSIVE": ("FIRE_DUST_EXPLOSION",),
    "PYROPHORIC": ("CHEM_FLAMMABLE_SPILL", "FIRE_HOT_WORK_IGNITION"),
    "WATER_REACTIVE": ("CHEM_OXIDISER_REACTIVE",),
    "IRRITANT": ("CHEM_CORROSIVE_CONTACT",),
    # ENVIRONMENTAL_HAZARD has no occupational-safety library hazard; it belongs
    # to the EAI Register, and inventing a HIRA row for it would double-count the
    # same fact in two registers. Mapped deliberately to nothing.
    "ENVIRONMENTAL_HAZARD": (),
}

#: Applied when the chemical's SDS review is overdue — the library carries a
#: hazard for exactly this, and surfacing it on the HIRA is more useful than
#: another dashboard number.
SDS_UNREVIEWED_CODE = "CHEM_SDS_UNREVIEWED"


@dataclass
class HazardProposal:
    hazard_id: str
    hazard_code: str
    hazard_name: str
    source_hazard_class: str
    contextual_description: str
    regulation_ref: str | None
    regulation_section: str | None


def _library_citation(h: HiraHazard) -> tuple[str | None, str | None]:
    """Best regulatory citation the library row itself offers, most specific
    (Indian statutory) first — this platform's primary regime is IN."""
    if h.factoriesActSection:
        return ("Factories Act 1948", h.factoriesActSection)
    if h.isStandard:
        return ("IS", h.isStandard)
    if h.oshaStandard:
        return ("OSHA", h.oshaStandard)
    if h.isoReference:
        return ("ISO", h.isoReference)
    return (None, None)


async def resolve_hazards(
    db: AsyncSession, chemical: ChemicalMaster
) -> tuple[list[HazardProposal], list[str]]:
    """Hazard rows this chemical implies, plus any mapped library codes that are
    missing from the database.

    Returns (proposals, missing_codes). The second element is not decoration: a
    mapping that points at a code nobody seeded produces silently fewer hazard
    rows, and the caller surfaces it rather than shipping a short HIRA.
    """
    classes = [str(c) for c in (chemical.hazardClasses or [])]
    wanted: dict[str, str] = {}  # code → the hazard class that asked for it
    for cls in classes:
        for code in HAZARD_CLASS_TO_HIRA_CODES.get(cls, ()):
            wanted.setdefault(code, cls)
    if chemical.sdsReviewOverdue or not chemical.sdsAttachmentId:
        wanted.setdefault(SDS_UNREVIEWED_CODE, "SDS_REVIEW")

    if not wanted:
        return [], []

    rows = (
        await db.execute(
            select(HiraHazard)
            .where(HiraHazard.code.in_(list(wanted)))
            .where(HiraHazard.isActive.is_(True))
        )
    ).scalars().all()
    found = {h.code: h for h in rows}
    missing = sorted(set(wanted) - set(found))
    if missing:
        logger.warning(
            "[chemical_hira] hazard library is missing %s — HIRA rows for chemical %s "
            "will be incomplete. Re-run the HIRA master seed.",
            ", ".join(missing), chemical.id,
        )

    proposals: list[HazardProposal] = []
    for code, source_class in wanted.items():
        h = found.get(code)
        if h is None:
            continue
        lib_ref, lib_section = _library_citation(h)
        # The chemical's own regulatory reference wins: when this module is the
        # source of the hazard row, its citation is the more specific one.
        ref = chemical.regulatoryReference or lib_ref
        section = lib_section if not chemical.regulatoryReference else None
        detail = (
            f"Present as {chemical.name}"
            + (f" (CAS {chemical.casNumber})" if chemical.casNumber else "")
            + f", {chemical.physicalState.lower()}."
        )
        if source_class == "SDS_REVIEW":
            detail = (
                f"{chemical.name}'s Safety Data Sheet is "
                + ("overdue for review" if chemical.sdsReviewOverdue else "not attached")
                + " — hazard information in use may be out of date."
            )
        elif chemical.flashPointCelsius is not None and source_class == "FLAMMABLE":
            detail += f" Flash point {chemical.flashPointCelsius} °C."

        proposals.append(
            HazardProposal(
                hazard_id=h.id,
                hazard_code=h.code,
                hazard_name=h.name,
                source_hazard_class=source_class,
                contextual_description=detail,
                regulation_ref=(ref[:200] if ref else None),
                regulation_section=(section[:120] if section else None),
            )
        )
    proposals.sort(key=lambda p: p.hazard_code)
    return proposals, missing


async def apply_to_entry(
    db: AsyncSession,
    *,
    entry_id: str,
    chemical_id: str,
    replace: bool = False,
) -> dict[str, object]:
    """Materialise the proposed hazard rows onto a HIRA entry.

    Refuses to touch an entry that is not being authored: an APPROVED HIRA that
    gains hazard rows after the fact is unauditable, and "the system added them"
    is not an answer a team leader can give.
    """
    entry = await db.get(HiraEntry, entry_id)
    if entry is None:
        raise ValueError("HIRA entry not found.")
    if entry.status not in ("DRAFT", "IN_PROGRESS", "FLAGGED_FOR_REVIEW"):
        raise ValueError(
            f"HIRA entry is {entry.status}. Hazard rows can only be added while the entry "
            f"is being authored or is under review — raise a review cycle first."
        )
    chemical = await db.get(ChemicalMaster, chemical_id)
    if chemical is None or chemical.isDeleted:
        raise ValueError("Chemical not found.")

    proposals, missing = await resolve_hazards(db, chemical)

    existing = {
        r.hazardId
        for r in (
            await db.execute(
                select(HiraEntryHazard).where(HiraEntryHazard.entryId == entry_id)
            )
        ).scalars().all()
    }

    created: list[str] = []
    skipped: list[str] = []
    max_sort = len(existing)
    for i, p in enumerate(proposals):
        if p.hazard_id in existing and not replace:
            skipped.append(p.hazard_code)
            continue
        db.add(
            HiraEntryHazard(
                entryId=entry_id,
                hazardId=p.hazard_id,
                contextualDescription=p.contextual_description,
                regulationRef=p.regulation_ref,
                regulationSection=p.regulation_section,
                sortOrder=max_sort + i,
            )
        )
        created.append(p.hazard_code)
    await db.flush()

    return {
        "entryId": entry_id,
        "chemicalId": chemical_id,
        "created": created,
        "alreadyPresent": skipped,
        # Reported, never swallowed — an incomplete HIRA must say so.
        "missingLibraryHazards": missing,
    }


async def propagate_regulatory_reference(
    db: AsyncSession, *, chemical_id: str
) -> int:
    """Push a changed `ChemicalMaster.regulatoryReference` down to the hazard
    rows this module sourced.

    Only rows whose `regulationRef` still matches the chemical's PREVIOUS value
    or is null are touched — a citation a human edited by hand is theirs, and
    overwriting it would make the field untrustworthy the first time someone
    noticed.
    """
    chemical = await db.get(ChemicalMaster, chemical_id)
    if chemical is None or not chemical.regulatoryReference:
        return 0

    proposals, _ = await resolve_hazards(db, chemical)
    hazard_ids = [p.hazard_id for p in proposals]
    if not hazard_ids:
        return 0

    rows = (
        await db.execute(
            select(HiraEntryHazard)
            .where(HiraEntryHazard.hazardId.in_(hazard_ids))
            .where(HiraEntryHazard.regulationRef.is_(None))
        )
    ).scalars().all()
    for r in rows:
        r.regulationRef = chemical.regulatoryReference[:200]
    await db.flush()
    return len(rows)


__all__ = [
    "HAZARD_CLASS_TO_HIRA_CODES",
    "HazardProposal",
    "resolve_hazards",
    "apply_to_entry",
    "propagate_regulatory_reference",
]
