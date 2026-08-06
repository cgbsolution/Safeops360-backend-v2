"""Co-storage incompatibility engine (build spec §4.4).

Two layers, deliberately:

  * **This service** resolves the applicable rules and returns them, so the UI
    can show a conflict on the Storage Location Map *before* anyone presses
    save (§7 #4 — "surfaced visually, not just on save") and so the API can
    return a useful 409 naming the conflicting drum.
  * **A deferred constraint trigger** in the database rejects a BLOCK conflict
    at COMMIT regardless of which code path produced it (see
    prisma/apply-chemical-ddl.ts).

The second layer is not redundancy for its own sake. Business rule §4 says BLOCK
is "a hard constraint at save time, not a UI-only warning", and a service-layer
check is still a UI-only warning as far as a bulk import, a data-fix script or a
future endpoint is concerned. The service check exists to produce a good error
message; the trigger exists to make the rule true.

Rule precedence: a specific chemical pair overrides the hazard-class pair, and a
tenant rule overrides the platform default. Both are "most specific wins", which
is the only precedence order an operator can predict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chemical import (
    ChemicalIncompatibilityRule,
    ChemicalInventoryItem,
    ChemicalMaster,
)


@dataclass
class CoStorageConflict:
    severity: str  # BLOCK | WARN
    rule_id: str
    this_chemical_id: str
    this_chemical_name: str
    other_item_id: str
    other_chemical_id: str
    other_chemical_name: str
    other_batch: str
    hazard_pair: tuple[str | None, str | None]
    regulatory_reference: str | None
    rationale: str | None

    def message(self) -> str:
        base = (
            f"{self.this_chemical_name} is incompatible with "
            f"{self.other_chemical_name} (batch {self.other_batch}) already in this location"
        )
        if self.regulatory_reference:
            base += f" — {self.regulatory_reference}"
        return base + "."


def _specificity(rule: ChemicalIncompatibilityRule) -> tuple[int, int]:
    """Higher sorts first. Specific-chemical pairs beat class pairs; tenant rows
    beat platform defaults."""
    return (
        1 if (rule.chemicalIdA and rule.chemicalIdB) else 0,
        1 if rule.tenantId else 0,
    )


async def _load_rules(db: AsyncSession, tenant_id: str) -> list[ChemicalIncompatibilityRule]:
    rows = (
        await db.execute(
            select(ChemicalIncompatibilityRule)
            .where(ChemicalIncompatibilityRule.isActive.is_(True))
            .where(ChemicalIncompatibilityRule.isDeleted.is_(False))
            .where(
                or_(
                    ChemicalIncompatibilityRule.tenantId.is_(None),
                    ChemicalIncompatibilityRule.tenantId == tenant_id,
                )
            )
        )
    ).scalars().all()
    return sorted(rows, key=_specificity, reverse=True)


def _matches(
    rule: ChemicalIncompatibilityRule,
    a_id: str,
    a_classes: Sequence[str],
    b_id: str,
    b_classes: Sequence[str],
) -> tuple[str | None, str | None] | None:
    """Return the matched hazard pair, or None. Both directions are checked —
    an incompatibility is symmetric and storing it twice invites the two rows to
    drift apart."""
    if rule.chemicalIdA and rule.chemicalIdB:
        if {rule.chemicalIdA, rule.chemicalIdB} == {a_id, b_id} and a_id != b_id:
            return (None, None)
        return None
    ca, cb = rule.hazardClassA, rule.hazardClassB
    if not ca or not cb:
        return None
    if ca in a_classes and cb in b_classes:
        return (ca, cb)
    if cb in a_classes and ca in b_classes:
        return (cb, ca)
    return None


async def check_co_storage(
    db: AsyncSession,
    *,
    tenant_id: str,
    storage_location_id: str,
    chemical_id: str,
    exclude_item_id: str | None = None,
) -> list[CoStorageConflict]:
    """Conflicts between `chemical_id` and everything already stored at
    `storage_location_id`.

    Only stock that is actually present counts: an emptied drum whose row still
    exists is not a co-storage hazard, and treating it as one makes a store
    un-loadable for a reason nobody can see on the shelf. This mirrors the same
    `quantityLedger > 0` predicate the database trigger uses — the two must
    agree, or the UI shows a conflict the database will not enforce (or worse,
    the reverse).
    """
    chemical = await db.get(ChemicalMaster, chemical_id)
    if chemical is None:
        return []
    this_classes = [str(c) for c in (chemical.hazardClasses or [])]

    stmt = (
        select(ChemicalInventoryItem, ChemicalMaster)
        .join(ChemicalMaster, ChemicalMaster.id == ChemicalInventoryItem.chemicalId)
        .where(ChemicalInventoryItem.storageLocationId == storage_location_id)
        .where(ChemicalInventoryItem.isDeleted.is_(False))
        .where(ChemicalInventoryItem.quantityLedger > 0)
    )
    if exclude_item_id:
        stmt = stmt.where(ChemicalInventoryItem.id != exclude_item_id)
    occupants = (await db.execute(stmt)).all()
    if not occupants:
        return []

    rules = await _load_rules(db, tenant_id)
    conflicts: list[CoStorageConflict] = []
    # One conflict per occupying item — the most specific matching rule wins, so
    # a named-pair WARN exception correctly overrides a class-level BLOCK.
    for item, other_chem in occupants:
        if other_chem.id == chemical_id:
            continue  # same substance, different batch: never self-incompatible
        other_classes = [str(c) for c in (other_chem.hazardClasses or [])]
        for rule in rules:
            pair = _matches(rule, chemical_id, this_classes, other_chem.id, other_classes)
            if pair is None:
                continue
            conflicts.append(
                CoStorageConflict(
                    severity=rule.severity,
                    rule_id=rule.id,
                    this_chemical_id=chemical_id,
                    this_chemical_name=chemical.name,
                    other_item_id=item.id,
                    other_chemical_id=other_chem.id,
                    other_chemical_name=other_chem.name,
                    other_batch=item.batchLotNumber,
                    hazard_pair=pair,
                    regulatory_reference=rule.regulatoryReference,
                    rationale=rule.rationale,
                )
            )
            break  # most specific rule for this pair has spoken

    # BLOCK first: the caller usually renders the first conflict, and a WARN
    # shown above a BLOCK reads as "proceed with a reason" when it is not.
    conflicts.sort(key=lambda c: 0 if c.severity == "BLOCK" else 1)
    return conflicts


def blocking(conflicts: Iterable[CoStorageConflict]) -> list[CoStorageConflict]:
    return [c for c in conflicts if c.severity == "BLOCK"]


def warnings(conflicts: Iterable[CoStorageConflict]) -> list[CoStorageConflict]:
    return [c for c in conflicts if c.severity == "WARN"]


__all__ = ["CoStorageConflict", "check_co_storage", "blocking", "warnings"]
