"""DuPont STOP observation taxonomy — offline unit tests (house no-DB style).

Covers the pure layers: axis derivation, at-risk gating, the legacy dual-write
map, the capture hazard mapping, and — through a fake AsyncSession — the
validation choke point every write path funnels into.

The property under test throughout is the one the demo bug turned on: a
category is eligible for an axis ONLY if the seed data puts a sub-category
there, so "Reactions of People" / "Positions of People" are unreachable under
CONDITION with no exclusion list anywhere in the code.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.models.observation import ObservationType, TaxonomyAxis
from app.services import observation_taxonomy as tax


# ─── Fake DB ────────────────────────────────────────────────────────────────
# The seed shape that matters: PPE has rows on both axes, POSITIONS_OF_PEOPLE
# only on ACT. Mirrors prisma/seed-observation-taxonomy.ts.

_ROWS = [
    ("POSITIONS_OF_PEOPLE", "Positions of People", "STOP-2", "ACT", "PP_STRUCK_BY", "Struck by", 201),
    ("POSITIONS_OF_PEOPLE", "Positions of People", "STOP-2", "ACT", "PP_OVEREXERTION", "Overexertion", 208),
    ("PPE", "Personal Protective Equipment", "STOP-3", "ACT", "PPE_NOT_WORN", "PPE not worn", 300),
    ("PPE", "Personal Protective Equipment", "STOP-3", "CONDITION", "PPE_NOT_AVAILABLE", "PPE not available", 303),
    ("HOUSEKEEPING", "Housekeeping / Orderliness", "STOP-6", "CONDITION", "HK_POOR_LIGHTING", "Poor lighting", 602),
]


class _Row:
    def __init__(self, tup):
        (
            self.categoryCode, self.categoryLabel, self.stopReferenceCode,
            self.observationType, self.subCategoryCode, self.subCategoryLabel,
            self.displayOrder,
        ) = tup
        self.id = f"{self.categoryCode}:{self.subCategoryCode}:{self.observationType}"
        self.isActive = True


class _FakeDB:
    """Answers the three query shapes observation_taxonomy issues, by reading
    the filters SQLAlchemy recorded on the statement rather than executing SQL."""

    def __init__(self, rows=_ROWS):
        self.rows = [_Row(r) for r in rows]

    async def execute(self, stmt):
        # Recover the literal values bound into the WHERE clause. Every filter
        # in this module is a simple `column == literal`, so pulling the
        # compiled params back out is enough to replay them in Python.
        params = stmt.compile().params
        wanted = {k.rsplit("_", 1)[0]: v for k, v in params.items()}

        matched = [
            r for r in self.rows
            if (wanted.get("observationType") in (None, r.observationType))
            and (wanted.get("categoryCode") in (None, r.categoryCode))
            and (wanted.get("subCategoryCode") in (None, r.subCategoryCode))
            and r.isActive
        ]
        matched.sort(key=lambda r: (r.displayOrder, r.subCategoryCode))

        # `select(ObservationTaxonomy)` → .scalars(); column selects → .all().
        entities = [d.get("entity") or d.get("expr") for d in stmt.column_descriptions]
        whole_entity = len(stmt.column_descriptions) == 1 and hasattr(entities[0], "__tablename__")
        return _FakeResult(matched, whole_entity, stmt)


class _FakeResult:
    def __init__(self, rows, whole_entity, stmt):
        self._rows = rows
        self._whole = whole_entity
        self._stmt = stmt

    def scalars(self):
        return self

    def all(self):
        if self._whole:
            return self._rows
        keys = [c["name"] for c in self._stmt.column_descriptions]
        return [tuple(getattr(r, k) for k in keys) for r in self._rows]

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


@pytest.fixture
def db():
    return _FakeDB()


# ─── Axis derivation ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "obs_type,expected",
    [
        ("SAFE_ACT", "ACT"),
        ("UNSAFE_ACT", "ACT"),
        ("SAFE_CONDITION", "CONDITION"),
        ("UNSAFE_CONDITION", "CONDITION"),
        ("NONSENSE", None),
    ],
)
def test_axis_for_type(obs_type, expected):
    assert tax.axis_for_type(obs_type) == expected


def test_axis_for_type_accepts_enum_not_just_string():
    assert tax.axis_for_type(ObservationType.UNSAFE_CONDITION) == TaxonomyAxis.CONDITION.value


def test_only_at_risk_types_carry_the_taxonomy():
    # The seeded sub-category labels are deviation-phrased, so a Safe Act must
    # not be forced into them.
    assert tax.requires_taxonomy("UNSAFE_ACT")
    assert tax.requires_taxonomy("UNSAFE_CONDITION")
    assert not tax.requires_taxonomy("SAFE_ACT")
    assert not tax.requires_taxonomy("SAFE_CONDITION")


def test_normalise_axis_accepts_either_vocabulary():
    assert tax.normalise_axis("ACT") == "ACT"
    assert tax.normalise_axis("unsafe_condition") == "CONDITION"
    assert tax.normalise_axis("") is None
    assert tax.normalise_axis("CONDITIONS") is None  # typo must not silently pass


# ─── Derived category eligibility (the actual fix) ───────────────────────────

@pytest.mark.asyncio
async def test_condition_categories_exclude_act_only_categories(db):
    codes = {c["categoryCode"] for c in await tax.list_categories(db, "CONDITION")}
    assert "POSITIONS_OF_PEOPLE" not in codes
    assert "REACTIONS_OF_PEOPLE" not in codes
    assert {"PPE", "HOUSEKEEPING"} <= codes


@pytest.mark.asyncio
async def test_act_categories_include_act_only_categories(db):
    codes = {c["categoryCode"] for c in await tax.list_categories(db, "ACT")}
    assert "POSITIONS_OF_PEOPLE" in codes


@pytest.mark.asyncio
async def test_categories_are_deduplicated_and_ordered(db):
    cats = await tax.list_categories(db, "ACT")
    codes = [c["categoryCode"] for c in cats]
    assert len(codes) == len(set(codes)), "one entry per category, not one per sub-category"
    assert codes == sorted(codes, key=lambda c: [x["displayOrder"] for x in cats if x["categoryCode"] == c][0])


@pytest.mark.asyncio
async def test_subcategories_are_scoped_to_the_axis(db):
    act = {s["subCategoryCode"] for s in await tax.list_subcategories(db, "ACT", "PPE")}
    cond = {s["subCategoryCode"] for s in await tax.list_subcategories(db, "CONDITION", "PPE")}
    assert act == {"PPE_NOT_WORN"}
    assert cond == {"PPE_NOT_AVAILABLE"}
    assert not (act & cond), "the two lists must not share entries — that was the bug"


# ─── Validation ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_valid_pair_returns_normalised_triple(db):
    assert await tax.validate_selection(db, "UNSAFE_ACT", "PPE", "PPE_NOT_WORN") == (
        "PPE", "PPE_NOT_WORN", "ACT",
    )


@pytest.mark.asyncio
async def test_safe_types_clear_the_taxonomy_rather_than_keeping_a_stale_pair(db):
    assert await tax.validate_selection(db, "SAFE_ACT", "PPE", "PPE_NOT_WORN") == (None, None, None)


@pytest.mark.asyncio
async def test_act_only_category_on_a_condition_is_rejected(db):
    # The headline case: "Unsafe Condition + Positions of People".
    with pytest.raises(HTTPException) as e:
        await tax.validate_selection(db, "UNSAFE_CONDITION", "POSITIONS_OF_PEOPLE", "PP_STRUCK_BY")
    assert e.value.status_code == 400
    assert "only observable as act" in e.value.detail.lower()


@pytest.mark.asyncio
async def test_subcategory_from_the_wrong_axis_is_rejected(db):
    # Right category, but the ACT sub-category under a CONDITION.
    with pytest.raises(HTTPException) as e:
        await tax.validate_selection(db, "UNSAFE_CONDITION", "PPE", "PPE_NOT_WORN")
    assert e.value.status_code == 400
    assert "PPE_NOT_AVAILABLE" in e.value.detail, "the error should name what IS valid"


@pytest.mark.asyncio
async def test_unknown_category_is_rejected(db):
    with pytest.raises(HTTPException) as e:
        await tax.validate_selection(db, "UNSAFE_ACT", "NOT_A_CATEGORY", "PPE_NOT_WORN")
    assert e.value.status_code == 400
    assert "unknown category" in e.value.detail.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("cat,sub", [(None, None), ("PPE", None), (None, "PPE_NOT_WORN"), ("", "")])
async def test_at_risk_requires_both_halves(db, cat, sub):
    with pytest.raises(HTTPException) as e:
        await tax.validate_selection(db, "UNSAFE_ACT", cat, sub)
    assert e.value.status_code == 400


# ─── Legacy dual-write ──────────────────────────────────────────────────────

def test_stop_codes_map_onto_the_legacy_category_column():
    # Downstream group-by-category consumers keep working because the legacy
    # enum was extended with the STOP codes rather than remapped.
    for code in ("PPE", "HOUSEKEEPING", "TOOLS_EQUIPMENT", "PROCEDURES",
                 "REACTIONS_OF_PEOPLE", "POSITIONS_OF_PEOPLE"):
        assert tax.legacy_category_for(code) == code


def test_non_stop_input_leaves_the_legacy_category_to_the_caller():
    # Safe observations pick their own hazard category; nothing is derived.
    assert tax.legacy_category_for(None) is None
    assert tax.legacy_category_for("WORK_AT_HEIGHT") is None


# ─── Capture hazard mapping ─────────────────────────────────────────────────

def test_capture_hazard_maps_per_axis():
    assert tax.stop_pair_for_hazard("ppe", "ACT") == ("PPE", "PPE_NOT_WORN")
    assert tax.stop_pair_for_hazard("ppe", "CONDITION") == ("PPE", "PPE_NOT_AVAILABLE")


def test_unknown_capture_hazard_falls_back_within_the_axis():
    cat, sub = tax.stop_pair_for_hazard("no_such_hazard", "CONDITION")
    assert (cat, sub) == tax.HAZARD_FALLBACK["CONDITION"]


def test_every_capture_hazard_resolves_on_both_axes():
    # A converted field report is always at-risk, so an unresolvable hazard
    # would 400 the triager's conversion.
    for code in tax.HAZARD_TO_STOP:
        for axis in ("ACT", "CONDITION"):
            cat, sub = tax.stop_pair_for_hazard(code, axis)
            assert cat and sub


def test_capture_act_mappings_never_point_at_condition_only_subcategories():
    # A mapping that resolved to the wrong axis would fail validation at
    # conversion time, which is exactly what this guards.
    condition_only_prefixes = ("PPE_NOT_AVAILABLE", "PPE_DAMAGED", "PPE_WRONG_SPEC",
                               "TE_DAMAGED", "TE_UNSAFE_CONDITION", "TE_GUARD_MISSING",
                               "TE_OVERDUE", "PR_INADEQUATE", "PR_NOT_AVAILABLE",
                               "PR_NOT_UNDERSTOOD", "HK_SPILL", "HK_BLOCKED",
                               "HK_POOR_LIGHTING", "HK_CLUTTER", "HK_DAMAGED_FLOORING")
    for code, byaxis in tax.HAZARD_TO_STOP.items():
        _, sub = byaxis["ACT"]
        assert not sub.startswith(condition_only_prefixes), f"{code} ACT → {sub} is condition-only"


def test_legacy_category_bridge_stays_on_axis():
    assert tax.stop_pair_for_legacy_category("PPE", "CONDITION") == ("PPE", "PPE_NOT_AVAILABLE")
    assert tax.stop_pair_for_legacy_category("PPE", "ACT") == ("PPE", "PPE_NOT_WORN")
    # Anything without a 1:1 meaning takes the axis fallback rather than a guess.
    assert tax.stop_pair_for_legacy_category("HOT_WORK", "CONDITION") == tax.HAZARD_FALLBACK["CONDITION"]
