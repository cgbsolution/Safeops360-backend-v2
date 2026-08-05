"""Severity suggestion engine — offline unit tests (house no-DB style).

Covers the pure layers (the ladder, the tier modifier, the divergence gate, the
calibration arithmetic) and — through a fake AsyncSession — the resolver and the
tier precedence chain.

The properties under test are the ones the feature's correctness rests on:

  1. A missing rule yields NO suggestion, NO requirement and NO log row. That is
     the graceful-degradation contract: unconfigured policy must never block a
     submission or demand a justification for something nobody rated.
  2. The tier modifier is a clamped step on an ordered ladder, and Elevated
     applies ONLY from LOW — the two places an off-by-one would silently inflate
     or deflate every observation in an area.
  3. Agreement never writes a row, so an override *rate* is meaningful.
"""

from __future__ import annotations

import pytest

from app.models.observation_severity import (
    MIN_OVERRIDE_REASON_CHARS,
    OVERRIDE_SOURCE_CAPTURE_CONVERSION,
    OVERRIDE_SOURCE_OBSERVER_FORM,
    SEVERITY_LADDER,
    TIER_ELEVATED,
    TIER_HIGH_HAZARD,
    TIER_STANDARD,
    AreaHazardTier,
    SeverityMatrixRule,
)
from app.services import observation_severity as sev


# ─── Fakes ──────────────────────────────────────────────────────────────────
# The seed shape that matters: one HIGH rule on each axis, plus a LOW one for
# the Elevated-tier boundary. Mirrors prisma/seed-severity-matrix.ts.

_RULES = [
    ("ACT", "PROCEDURES", "PR_PERMIT_LOTO_BYPASSED", "CRITICAL", "LOTO bypass."),
    ("ACT", "PPE", "PPE_NOT_WORN", "MEDIUM", "PPE not worn."),
    ("ACT", "HOUSEKEEPING", "HK_NOT_CLEAN_AS_YOU_GO", "LOW", "Clean as you go."),
    ("CONDITION", "TOOLS_EQUIPMENT", "TE_GUARD_MISSING", "HIGH", "Guard missing."),
]


def _rule(tup, active=True):
    r = SeverityMatrixRule(
        observationType=tup[0],
        category=tup[1],
        subCategory=tup[2],
        baseSeverity=tup[3],
        rationale=tup[4],
        isActive=active,
    )
    r.id = f"rule:{tup[0]}:{tup[2]}"
    return r


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    """Replays the two query shapes this service issues by reading the literals
    SQLAlchemy compiled into the WHERE clause. Every filter here is a simple
    `column == literal`, so pulling the params back out is enough."""

    def __init__(self, rules=None, tiers=None):
        self.rules = [_rule(r) for r in (rules if rules is not None else _RULES)]
        self.tiers = list(tiers or [])
        self.added: list = []

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0].get("entity")
        params = stmt.compile().params
        wanted = {k.rsplit("_", 1)[0]: v for k, v in params.items()}

        if entity is SeverityMatrixRule:
            return _Result(
                [
                    r
                    for r in self.rules
                    if r.isActive
                    and wanted.get("observationType") in (None, r.observationType)
                    and wanted.get("category") in (None, r.category)
                    and wanted.get("subCategory") in (None, r.subCategory)
                ]
            )
        if entity is AreaHazardTier:
            return _Result(
                [t for t in self.tiers if t.isActive and t.plantId == wanted.get("plantId")]
            )
        raise AssertionError(f"unexpected query against {entity}")


def _tier(plant_id, area_id, tier):
    row = AreaHazardTier(plantId=plant_id, areaId=area_id, hazardTier=tier, isActive=True)
    row.id = f"tier:{plant_id}:{area_id or 'default'}"
    return row


class _Obs:
    """Just the attributes log_override reads."""

    def __init__(self, plant_id="P1", area_id="A1"):
        self.id = "OBS-1"
        self.plantId = plant_id
        self.areaId = area_id


# ─── The ladder + tier modifier ─────────────────────────────────────────────


def test_ladder_is_ordered_low_to_critical():
    assert SEVERITY_LADDER == ("LOW", "MEDIUM", "HIGH", "CRITICAL")
    assert sev.severity_index("LOW") < sev.severity_index("CRITICAL")


@pytest.mark.parametrize("junk", [None, "", "SEVERE", "medium ", "Medium"])
def test_severity_index_only_accepts_ladder_values(junk):
    # "Medium" / "medium " normalise; genuine junk must return None rather than
    # a rung, because a bogus index would silently shift a bump.
    expected = 1 if junk and junk.strip().upper() == "MEDIUM" else None
    assert sev.severity_index(junk) == expected


@pytest.mark.parametrize(
    "base,expected",
    [("LOW", "MEDIUM"), ("MEDIUM", "HIGH"), ("HIGH", "CRITICAL"), ("CRITICAL", "CRITICAL")],
)
def test_high_hazard_bumps_one_rung_and_clamps(base, expected):
    assert sev.apply_tier_modifier(base, TIER_HIGH_HAZARD) == expected


@pytest.mark.parametrize(
    "base,expected",
    [("LOW", "MEDIUM"), ("MEDIUM", "MEDIUM"), ("HIGH", "HIGH"), ("CRITICAL", "CRITICAL")],
)
def test_elevated_bumps_only_from_low(base, expected):
    """The spec's 'Elevated tier: bump only Low->Medium'. An Elevated area must
    not quietly turn every Medium finding into a High one."""
    assert sev.apply_tier_modifier(base, TIER_ELEVATED) == expected


@pytest.mark.parametrize("base", SEVERITY_LADDER)
def test_standard_tier_is_a_no_op(base):
    assert sev.apply_tier_modifier(base, TIER_STANDARD) == base
    assert sev.apply_tier_modifier(base, None) == base


@pytest.mark.parametrize("base", SEVERITY_LADDER)
def test_unknown_tier_is_treated_as_standard(base):
    """A typo in the tier column must not inflate every observation in an area."""
    assert sev.apply_tier_modifier(base, "HIGHHAZARD") == base
    assert sev.apply_tier_modifier(base, "extreme") == base


# ─── Tier precedence ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_area_row_beats_plant_default():
    db = _FakeDB(tiers=[_tier("P1", None, TIER_ELEVATED), _tier("P1", "A1", TIER_HIGH_HAZARD)])
    assert await sev.get_area_hazard_tier(db, plant_id="P1", area_id="A1") == (
        TIER_HIGH_HAZARD,
        "area",
    )


@pytest.mark.asyncio
async def test_plant_default_applies_to_an_area_with_no_row():
    db = _FakeDB(tiers=[_tier("P1", None, TIER_ELEVATED), _tier("P1", "A1", TIER_HIGH_HAZARD)])
    assert await sev.get_area_hazard_tier(db, plant_id="P1", area_id="A9") == (
        TIER_ELEVATED,
        "plant",
    )


@pytest.mark.asyncio
async def test_no_rows_at_all_falls_back_to_standard():
    db = _FakeDB(tiers=[])
    assert await sev.get_area_hazard_tier(db, plant_id="P1", area_id="A1") == (
        TIER_STANDARD,
        "default",
    )
    # And with no plant context at all — the resolver must not query blind.
    assert await sev.get_area_hazard_tier(db, plant_id=None, area_id="A1") == (
        TIER_STANDARD,
        "default",
    )


@pytest.mark.asyncio
async def test_inactive_tier_row_is_ignored():
    row = _tier("P1", "A1", TIER_HIGH_HAZARD)
    row.isActive = False
    db = _FakeDB(tiers=[row])
    assert (await sev.get_area_hazard_tier(db, plant_id="P1", area_id="A1"))[0] == TIER_STANDARD


# ─── The resolver ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_returns_base_severity_on_a_standard_area():
    db = _FakeDB()
    out = await sev.resolve(
        db,
        observation_type="UNSAFE_ACT",
        category="PPE",
        sub_category="PPE_NOT_WORN",
        plant_id="P1",
        area_id="A1",
    )
    assert out["suggested"] == "MEDIUM"
    assert out["baseSeverity"] == "MEDIUM"
    assert out["tierApplied"] == TIER_STANDARD
    assert out["tierUplifted"] is False
    assert out["rationale"] == "PPE not worn."


@pytest.mark.asyncio
async def test_resolve_applies_the_area_uplift():
    db = _FakeDB(tiers=[_tier("P1", "A1", TIER_HIGH_HAZARD)])
    out = await sev.resolve(
        db,
        observation_type="UNSAFE_ACT",
        category="PPE",
        sub_category="PPE_NOT_WORN",
        plant_id="P1",
        area_id="A1",
    )
    assert out["baseSeverity"] == "MEDIUM"
    assert out["suggested"] == "HIGH"
    assert out["tierUplifted"] is True
    assert out["tierSource"] == "area"


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["ACT", "UNSAFE_ACT", "unsafe_act", "  Act  "])
async def test_axis_accepts_every_spelling_the_api_edge_may_receive(spelling):
    """The build spec writes the key as `unsafe_act`; the taxonomy stores `ACT`;
    the form sends `UNSAFE_ACT`. All three must reach the same rule."""
    db = _FakeDB()
    out = await sev.resolve(
        db, observation_type=spelling, category="PPE", sub_category="PPE_NOT_WORN"
    )
    assert out["suggested"] == "MEDIUM"


@pytest.mark.asyncio
async def test_no_rule_yields_no_suggestion_rather_than_an_error():
    db = _FakeDB()
    out = await sev.resolve(
        db, observation_type="ACT", category="PPE", sub_category="PPE_INVENTED_CODE"
    )
    assert out["suggested"] is None
    assert out["baseSeverity"] is None
    assert out["rationale"] == sev.NO_RULE_MESSAGE


@pytest.mark.asyncio
async def test_safe_observation_carries_no_taxonomy_and_gets_no_suggestion():
    """SAFE_ACT / SAFE_CONDITION resolve (None, None, None) from
    validate_selection, so there is nothing to look up — and nothing to justify."""
    db = _FakeDB()
    out = await sev.resolve(db, observation_type="SAFE_ACT", category=None, sub_category=None)
    assert out["suggested"] is None


@pytest.mark.asyncio
async def test_a_rule_on_the_other_axis_does_not_match():
    """TE_GUARD_MISSING is a CONDITION rule. Asking for it on the ACT axis must
    miss rather than borrow the other axis's severity."""
    db = _FakeDB()
    out = await sev.resolve(
        db, observation_type="ACT", category="TOOLS_EQUIPMENT", sub_category="TE_GUARD_MISSING"
    )
    assert out["suggested"] is None

    out = await sev.resolve(
        db,
        observation_type="UNSAFE_CONDITION",
        category="TOOLS_EQUIPMENT",
        sub_category="TE_GUARD_MISSING",
    )
    assert out["suggested"] == "HIGH"


@pytest.mark.asyncio
async def test_retired_rule_is_not_used():
    db = _FakeDB()
    for r in db.rules:
        r.isActive = False
    out = await sev.resolve(
        db, observation_type="ACT", category="PPE", sub_category="PPE_NOT_WORN"
    )
    assert out["suggested"] is None


# ─── The override gate ──────────────────────────────────────────────────────


def _suggestion(suggested="HIGH"):
    return {
        "suggested": suggested,
        "baseSeverity": "MEDIUM",
        "tierApplied": TIER_HIGH_HAZARD,
        "tierSource": "area",
        "tierUplifted": True,
        "rationale": "x",
        "matrixRuleId": "rule:1",
        "observationType": "ACT",
        "categoryCode": "PPE",
        "subCategoryCode": "PPE_NOT_WORN",
    }


def test_matching_severity_is_not_a_divergence():
    assert sev.diverges(_suggestion("HIGH"), "HIGH") is False
    sev.require_reason(_suggestion("HIGH"), "HIGH", None)  # must not raise


def test_no_suggestion_is_never_a_divergence():
    """With no rule seeded there is nothing to disagree with — demanding a
    reason would be asking the observer to justify unconfigured policy."""
    assert sev.diverges(_suggestion(None), "CRITICAL") is False
    sev.require_reason(_suggestion(None), "CRITICAL", None)  # must not raise


def test_divergence_without_a_reason_is_rejected():
    with pytest.raises(sev.SeverityOverrideError):
        sev.require_reason(_suggestion("HIGH"), "LOW", None)


@pytest.mark.parametrize("reason", ["", "   ", "too short", "         "])
def test_divergence_with_an_unusable_reason_is_rejected(reason):
    assert sev.reason_is_usable(reason) is False
    with pytest.raises(sev.SeverityOverrideError):
        sev.require_reason(_suggestion("HIGH"), "LOW", reason)


def test_reason_length_is_measured_after_stripping():
    padded = " " * 20 + "short" + " " * 20
    assert sev.reason_is_usable(padded) is False


def test_divergence_with_a_usable_reason_is_accepted():
    reason = "x" * MIN_OVERRIDE_REASON_CHARS
    assert sev.reason_is_usable(reason) is True
    sev.require_reason(_suggestion("HIGH"), "LOW", reason)  # must not raise


def test_error_message_names_both_severities():
    """The observer has to be able to tell what the form is objecting to."""
    with pytest.raises(sev.SeverityOverrideError) as e:
        sev.require_reason(_suggestion("HIGH"), "LOW", None)
    assert "High" in str(e.value) and "LOW" in str(e.value)


# ─── The log ────────────────────────────────────────────────────────────────


def test_agreement_writes_no_row():
    db = _FakeDB()
    row = sev.log_override(
        db,
        observation=_Obs(),
        suggestion=_suggestion("HIGH"),
        final_severity="HIGH",
        reason=None,
        actor_id="U1",
    )
    assert row is None
    assert db.added == []


def test_no_suggestion_writes_no_row():
    db = _FakeDB()
    assert (
        sev.log_override(
            db,
            observation=_Obs(),
            suggestion=_suggestion(None),
            final_severity="CRITICAL",
            reason="a" * 20,
            actor_id="U1",
        )
        is None
    )
    assert db.added == []


def test_override_row_freezes_the_resolver_inputs():
    """Observation.categoryCode and .severity are both editable afterwards, so a
    report joining through them would restate history. The row carries its own
    copy."""
    db = _FakeDB()
    obs = _Obs()
    row = sev.log_override(
        db,
        observation=obs,
        suggestion=_suggestion("HIGH"),
        final_severity="LOW",
        reason="  Guard was already isolated at the panel.  ",
        actor_id="U1",
    )
    assert row is not None and db.added == [row]
    assert row.observationId == obs.id
    assert (row.suggestedSeverity, row.finalSeverity) == ("HIGH", "LOW")
    assert row.overrideReason == "Guard was already isolated at the panel."
    assert (row.categoryCode, row.subCategoryCode) == ("PPE", "PPE_NOT_WORN")
    assert row.baseSeverity == "MEDIUM"
    assert row.hazardTier == TIER_HIGH_HAZARD
    assert row.matrixRuleId == "rule:1"
    assert (row.plantId, row.areaId) == (obs.plantId, obs.areaId)
    assert row.source == OVERRIDE_SOURCE_OBSERVER_FORM
    assert row.overriddenById == "U1"


def test_source_is_recorded_so_calibration_can_exclude_non_observer_decisions():
    db = _FakeDB()
    row = sev.log_override(
        db,
        observation=_Obs(),
        suggestion=_suggestion("HIGH"),
        final_severity="MEDIUM",
        reason="Converted from a field report.",
        actor_id="U1",
        source=OVERRIDE_SOURCE_CAPTURE_CONVERSION,
    )
    assert row.source == OVERRIDE_SOURCE_CAPTURE_CONVERSION


# ─── Calibration arithmetic ─────────────────────────────────────────────────


def test_direction_is_computed_on_the_ladder_not_alphabetically():
    assert sev._direction("MEDIUM", "HIGH") == "up"
    assert sev._direction("CRITICAL", "LOW") == "down"
    assert sev._direction("HIGH", "HIGH") == "same"
    # Alphabetically "CRITICAL" < "LOW"; on the ladder it is the other way.
    assert sev._direction("LOW", "CRITICAL") == "up"


@pytest.mark.asyncio
async def test_calibration_report_is_empty_when_no_plants_are_accessible():
    """An empty accessible-plant list must yield no rows — never an unscoped
    report over every plant."""
    assert await sev.calibration_report(None, plant_ids=[]) == []
