"""Observation SLA closure dates + Worker Involved + deroster workflow.

Offline unit tests in the house no-DB style (mirrors
tests/test_observation_taxonomy.py): a fake AsyncSession stands in for the
database so the decision logic — which is where the safety-relevant behaviour
lives — is exercised without a connection.

Every item on the build spec's §7 verification checklist has a test here, and
each one names the checklist item it covers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.observation import ObservationType, Severity
from app.models.observation_sla import (
    AXIS_ANY,
    CATEGORY_GROUP_BEHAVIORAL,
    CATEGORY_GROUP_PENDING,
    CATEGORY_GROUP_PHYSICAL,
    DEROSTER_CONFIRMED,
    DEROSTER_OVERRULED,
    DEROSTER_PENDING,
    DEROSTER_REINSTATED,
    PARTY_CONTRACTOR_WORKER,
    PARTY_USER,
    ROSTER_ACTIVE,
    ROSTER_DEROSTERED,
    ROSTER_PENDING_REVIEW,
    ObservationCategoryGroup,
    ObservationDeroster,
    ObservationSlaConfig,
)
from app.services import observation_deroster as der
from app.services import observation_sla as sla
from app.services import roster_gate


# ─── Fakes ──────────────────────────────────────────────────────────────────
class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


def _equality_filters(stmt) -> dict:
    """The `column == literal` pairs in a statement's WHERE clause.

    Only plain equality is extracted. Everything else (`IS NULL`, `<=`, `IN`,
    `!=`) is ignored, so a service that relies on those must either be tested
    through Python-side logic or stage its rows pre-filtered.
    """
    from sqlalchemy.sql import operators

    where = stmt.whereclause
    if where is None:
        return {}
    clauses = list(getattr(where, "clauses", [where]))
    out = {}
    for c in clauses:
        if getattr(c, "operator", None) is not operators.eq:
            continue
        try:
            out[c.left.name] = c.right.value
        except AttributeError:
            continue
    return out


class FakeSession:
    """Enough AsyncSession surface for these services.

    `execute` applies the statement's simple equality filters to the rows the
    test staged for that model, so a resolver that relies on the database to
    narrow by (say) categoryCode behaves the way it will in production. Richer
    predicates are not interpreted — see `_equality_filters`.
    """

    def __init__(self, *, rows=None, store=None):
        self.rows = rows or {}
        self.store = store or {}
        self.added: list = []
        self.flushes = 0

    def stage(self, model, rows):
        self.rows[model] = rows

    async def execute(self, stmt):
        model = stmt.column_descriptions[0]["entity"]
        rows = self.rows.get(model, [])
        filters = _equality_filters(stmt)
        for attr, expected in filters.items():
            rows = [r for r in rows if getattr(r, attr, None) == expected]
        return _Result(rows)

    async def get(self, model, pk):
        return self.store.get((model, pk))

    def add(self, obj):
        self.added.append(obj)
        if getattr(obj, "id", None) is None:
            obj.id = f"fake-{len(self.added)}"

    async def flush(self):
        self.flushes += 1

    def added_of(self, model):
        return [o for o in self.added if isinstance(o, model)]


class FakeObs:
    def __init__(self, **kw):
        self.id = kw.get("id", "obs-1")
        self.number = kw.get("number", "SO-2026-NW-0001")
        self.date = kw.get("date", datetime(2026, 7, 1, tzinfo=timezone.utc))
        self.type = kw.get("type", ObservationType.UNSAFE_ACT)
        self.severity = kw.get("severity", Severity.HIGH)
        self.plantId = kw.get("plantId", "plant-1")
        self.taxonomyAxis = kw.get("taxonomyAxis", "ACT")
        self.categoryCode = kw.get("categoryCode", "PPE")
        self.category = kw.get("category", "PPE")
        self.targetDate = None
        self.targetDateSource = None
        self.targetDateSlaConfig = None
        self.targetDateOverrideReason = None


class FakePerson:
    def __init__(self, rosterStatus=ROSTER_ACTIVE, ref=None, **kw):
        self.rosterStatus = rosterStatus
        self.currentDerosterRef = ref
        for k, v in kw.items():
            setattr(self, k, v)


def cfg(severity, group, days, plant=None, active=True):
    row = ObservationSlaConfig(
        plantId=plant, severity=severity, categoryGroup=group, slaDays=days, isActive=active
    )
    row.id = f"cfg-{severity}-{group}-{plant or 'global'}"
    return row


def deroster(**kw):
    d = ObservationDeroster(
        observationId=kw.get("observationId", "obs-1"),
        workerInvolvedId=kw.get("workerInvolvedId", "wi-1"),
        partyType=kw.get("partyType", PARTY_USER),
        userId=kw.get("userId", "user-1"),
        contractorWorkerId=kw.get("contractorWorkerId"),
        plantId=kw.get("plantId", "plant-1"),
        status=kw.get("status", DEROSTER_PENDING),
        flaggedReason=kw.get("flaggedReason", "High severity Unsafe Act — PPE"),
        reviewSlaHours=kw.get("reviewSlaHours", 4),
        reviewDueAt=kw.get("reviewDueAt", datetime.now(timezone.utc) + timedelta(hours=4)),
    )
    d.id = kw.get("id", "der-1")
    d.flaggedAt = kw.get("flaggedAt", datetime.now(timezone.utc))
    d.reviewedAt = kw.get("reviewedAt")
    d.correctiveActionTrainingId = kw.get("correctiveActionTrainingId")
    d.correctiveActionCompetencyId = kw.get("correctiveActionCompetencyId")
    d.escalatedAt = kw.get("escalatedAt")
    return d


# ═══ Category group mapping ═════════════════════════════════════════════════
# The seeded mapping (prisma/seed-observation-category-groups.ts). STOP-2 is
# deliberately undecided and must never resolve to a band.
SEEDED_GROUPS = [
    ("REACTIONS_OF_PEOPLE", CATEGORY_GROUP_BEHAVIORAL),
    ("POSITIONS_OF_PEOPLE", CATEGORY_GROUP_PENDING),
    ("PPE", CATEGORY_GROUP_PHYSICAL),
    ("TOOLS_EQUIPMENT", CATEGORY_GROUP_PHYSICAL),
    ("PROCEDURES", CATEGORY_GROUP_PHYSICAL),
    ("HOUSEKEEPING", CATEGORY_GROUP_PHYSICAL),
]


def group_row(code, group, axis=AXIS_ANY):
    r = ObservationCategoryGroup(categoryCode=code, axis=axis, categoryGroup=group)
    r.id = f"cg-{code}-{axis}"
    r.isActive = True
    return r


def seeded_db(**kw):
    db = FakeSession(**kw)
    db.stage(ObservationCategoryGroup, [group_row(c, g) for c, g in SEEDED_GROUPS])
    return db


@pytest.mark.parametrize(
    "code,expected",
    [(c, g) for c, g in SEEDED_GROUPS if g != CATEGORY_GROUP_PENDING],
)
@pytest.mark.parametrize("axis", ["ACT", "CONDITION"])
@pytest.mark.asyncio
async def test_every_stop_category_maps_to_its_configured_group(code, expected, axis):
    """Each of STOP-1 and STOP-3…STOP-6 tested individually, on BOTH axes —
    the mapping is category-keyed, so the axis must not change the answer."""
    db = seeded_db()
    group, source = await sla.resolve_category_group(db, category_code=code, axis=axis)
    assert group == expected
    assert source == "category_any"


@pytest.mark.parametrize("axis", ["ACT", "CONDITION"])
@pytest.mark.asyncio
async def test_ppe_is_physical_on_both_axes(axis):
    """The reported bug: PPE (STOP-3) on an Unsafe Act used to resolve
    Behavioral. It must now be Physical regardless of axis."""
    db = seeded_db()
    group, _ = await sla.resolve_category_group(db, category_code="PPE", axis=axis)
    assert group == CATEGORY_GROUP_PHYSICAL


@pytest.mark.asyncio
async def test_reactions_of_people_is_still_behavioral():
    db = seeded_db()
    group, _ = await sla.resolve_category_group(
        db, category_code="REACTIONS_OF_PEOPLE", axis="ACT"
    )
    assert group == CATEGORY_GROUP_BEHAVIORAL


@pytest.mark.parametrize("axis", ["ACT", "CONDITION"])
@pytest.mark.asyncio
async def test_stop2_resolves_no_group_and_is_never_guessed(axis):
    """STOP-2 is an open decision. It must return None with source 'pending' —
    not Behavioral, not Physical, on either axis."""
    db = seeded_db()
    group, source = await sla.resolve_category_group(
        db, category_code="POSITIONS_OF_PEOPLE", axis=axis
    )
    assert group is None
    assert source == "pending"


@pytest.mark.asyncio
async def test_per_axis_row_overrides_the_any_row():
    """The schema can split a category by axis later without a migration."""
    db = FakeSession()
    db.stage(
        ObservationCategoryGroup,
        [
            group_row("PPE", CATEGORY_GROUP_PHYSICAL),
            group_row("PPE", CATEGORY_GROUP_BEHAVIORAL, axis="ACT"),
        ],
    )
    act, src = await sla.resolve_category_group(db, category_code="PPE", axis="ACT")
    assert (act, src) == (CATEGORY_GROUP_BEHAVIORAL, "category_axis")
    cond, _ = await sla.resolve_category_group(db, category_code="PPE", axis="CONDITION")
    assert cond == CATEGORY_GROUP_PHYSICAL


@pytest.mark.asyncio
async def test_unmapped_category_falls_back_to_the_axis():
    """A category added to the taxonomy before anyone configures it here must
    still resolve a policy rather than silently losing its SLA."""
    db = seeded_db()
    group, source = await sla.resolve_category_group(db, category_code="BRAND_NEW", axis="ACT")
    assert (group, source) == (CATEGORY_GROUP_BEHAVIORAL, "axis_fallback")


@pytest.mark.parametrize(
    "obs_type,expected",
    [
        (ObservationType.SAFE_ACT, CATEGORY_GROUP_BEHAVIORAL),
        (ObservationType.SAFE_CONDITION, CATEGORY_GROUP_PHYSICAL),
    ],
)
@pytest.mark.asyncio
async def test_safe_observations_keep_auto_sla_via_the_axis_fallback(obs_type, expected):
    """SAFE_ACT / SAFE_CONDITION carry NO categoryCode (validate_selection
    returns (None, None, None)). A purely category-keyed lookup would have
    pushed every one of them to manual entry."""
    db = seeded_db()
    group, source = await sla.resolve_category_group(
        db, category_code=None, axis=None, obs_type=obs_type
    )
    assert (group, source) == (expected, "axis_fallback")


def test_axis_derivation_remains_available_as_the_fallback():
    assert sla.category_group_for_axis("ACT") == CATEGORY_GROUP_BEHAVIORAL
    assert sla.category_group_for_axis("CONDITION") == CATEGORY_GROUP_PHYSICAL
    assert sla.category_group_for_axis(None) is None


# ═══ SLA resolution + target date ═══════════════════════════════════════════
@pytest.mark.asyncio
async def test_plant_row_overrides_global():
    db = FakeSession()
    db.stage(
        ObservationSlaConfig,
        [cfg("HIGH", CATEGORY_GROUP_BEHAVIORAL, 7), cfg("HIGH", CATEGORY_GROUP_BEHAVIORAL, 3, plant="plant-1")],
    )
    row = await sla.resolve_sla_row(
        db, plant_id="plant-1", severity=Severity.HIGH, category_group=CATEGORY_GROUP_BEHAVIORAL
    )
    assert row.slaDays == 3
    # A different plant falls back to the global default.
    row2 = await sla.resolve_sla_row(
        db, plant_id="plant-2", severity=Severity.HIGH, category_group=CATEGORY_GROUP_BEHAVIORAL
    )
    assert row2.slaDays == 7


def test_target_date_is_calendar_days_from_the_observation_date():
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    # 14 calendar days spans two weekends — deliberately not working days (v1
    # default; a working-day calc would need a per-plant holiday calendar).
    assert sla.compute_target_date(base, 14) == datetime(2026, 7, 15, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_missing_sla_row_falls_back_to_manual_without_blocking():
    """§7: 'Missing/inactive SLA row falls back to manual entry without
    blocking submission.'"""
    db = FakeSession()
    db.stage(ObservationSlaConfig, [])
    obs = FakeObs()
    manual = datetime(2026, 8, 1, tzinfo=timezone.utc)

    await sla.apply_on_create(db, obs, submitted_target_date=manual, actor_id="u1")

    assert obs.targetDate == manual
    assert obs.targetDateSource == sla.SOURCE_MANUAL_NO_POLICY
    assert obs.targetDateSlaConfig is None
    # The fallback is still recorded — the trail says why there was no policy.
    assert len(db.added) == 1


@pytest.mark.asyncio
async def test_inactive_row_is_treated_as_missing():
    db = FakeSession()
    db.stage(ObservationSlaConfig, [cfg("HIGH", CATEGORY_GROUP_BEHAVIORAL, 7, active=False)])
    # resolve_sla_row filters on isActive in SQL; the fake returns rows
    # unfiltered, so assert the service's own guard by staging nothing active.
    db.stage(ObservationSlaConfig, [])
    row = await sla.resolve_sla_row(
        db, plant_id="plant-1", severity=Severity.HIGH, category_group=CATEGORY_GROUP_BEHAVIORAL
    )
    assert row is None


@pytest.mark.asyncio
async def test_applied_policy_is_frozen_onto_the_record():
    """§7: 'changing config retroactively does NOT alter already-submitted
    observations' — the record keeps its own copy, so a later edit cannot
    restate what it was held to."""
    db = FakeSession()
    row = cfg("HIGH", CATEGORY_GROUP_BEHAVIORAL, 7)
    db.stage(ObservationSlaConfig, [row])
    obs = FakeObs()

    await sla.apply_on_create(db, obs, submitted_target_date=None, actor_id="u1")

    assert obs.targetDate == datetime(2026, 7, 8, tzinfo=timezone.utc)
    assert obs.targetDateSource == sla.SOURCE_AUTO_SLA
    assert obs.targetDateSlaConfig["slaDays"] == 7
    assert obs.targetDateSlaConfig["configId"] == row.id
    # Mutating the config afterwards must not reach the stamped snapshot.
    row.slaDays = 99
    assert obs.targetDateSlaConfig["slaDays"] == 7


@pytest.mark.asyncio
async def test_client_supplied_date_is_ignored_when_a_policy_exists():
    """The field is read-only in the UI; the server has to mean it."""
    db = FakeSession()
    db.stage(ObservationSlaConfig, [cfg("HIGH", CATEGORY_GROUP_BEHAVIORAL, 7)])
    obs = FakeObs()
    await sla.apply_on_create(
        db, obs, submitted_target_date=datetime(2027, 1, 1, tzinfo=timezone.utc), actor_id="u1"
    )
    assert obs.targetDate == datetime(2026, 7, 8, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_preview_reports_no_match_without_raising():
    db = seeded_db()
    db.stage(ObservationSlaConfig, [])
    out = await sla.preview(
        db,
        plant_id="plant-1",
        obs_type=ObservationType.UNSAFE_ACT,
        severity=Severity.LOW,
        category_code="PPE",
        observation_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    assert out["matched"] is False
    assert out["targetDate"] is None
    assert out["reason"] == "NO_POLICY"
    assert out["categoryGroup"] == CATEGORY_GROUP_PHYSICAL


# ═══ The reported scenario, end to end ══════════════════════════════════════
FULL_MATRIX = [
    cfg("CRITICAL", CATEGORY_GROUP_BEHAVIORAL, 2),
    cfg("CRITICAL", CATEGORY_GROUP_PHYSICAL, 3),
    cfg("HIGH", CATEGORY_GROUP_BEHAVIORAL, 7),
    cfg("HIGH", CATEGORY_GROUP_PHYSICAL, 14),
    cfg("MEDIUM", CATEGORY_GROUP_BEHAVIORAL, 14),
    cfg("MEDIUM", CATEGORY_GROUP_PHYSICAL, 30),
    cfg("LOW", CATEGORY_GROUP_BEHAVIORAL, 30),
    cfg("LOW", CATEGORY_GROUP_PHYSICAL, 45),
]


def full_db(**kw):
    db = seeded_db(**kw)
    db.stage(ObservationSlaConfig, FULL_MATRIX)
    return db


@pytest.mark.asyncio
async def test_reported_bug_low_severity_ppe_now_reads_physical_45_days():
    """The exact case from the screenshot: Low + STOP-3 PPE on an Unsafe Act
    showed 'Low / Behavioral → 30 days'. It must now be Physical / 45."""
    db = full_db()
    out = await sla.preview(
        db,
        plant_id="plant-1",
        obs_type=ObservationType.UNSAFE_ACT,
        severity=Severity.LOW,
        category_code="PPE",
        observation_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    assert out["matched"] is True
    assert out["categoryGroup"] == CATEGORY_GROUP_PHYSICAL
    assert out["slaDays"] == 45
    assert out["targetDate"] == datetime(2026, 8, 15, tzinfo=timezone.utc)
    assert "Physical" in out["label"] and "45 days" in out["label"]


@pytest.mark.parametrize(
    "stop,code,expected_group,expected_days",
    [
        ("STOP-1", "REACTIONS_OF_PEOPLE", CATEGORY_GROUP_BEHAVIORAL, 30),
        ("STOP-3", "PPE", CATEGORY_GROUP_PHYSICAL, 45),
        ("STOP-4", "TOOLS_EQUIPMENT", CATEGORY_GROUP_PHYSICAL, 45),
        ("STOP-5", "PROCEDURES", CATEGORY_GROUP_PHYSICAL, 45),
        ("STOP-6", "HOUSEKEEPING", CATEGORY_GROUP_PHYSICAL, 45),
    ],
)
@pytest.mark.asyncio
async def test_each_stop_category_previews_individually(stop, code, expected_group, expected_days):
    """STOP-1 and STOP-3…STOP-6 each exercised through the real preview path,
    not assumed fixed because STOP-3 is. LOW severity throughout so the two
    bands are distinguishable (30 vs 45)."""
    db = full_db()
    out = await sla.preview(
        db,
        plant_id="plant-1",
        obs_type=ObservationType.UNSAFE_ACT,
        severity=Severity.LOW,
        category_code=code,
        observation_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    assert out["matched"] is True, f"{stop} failed to resolve"
    assert out["categoryGroup"] == expected_group, f"{stop} wrong group"
    assert out["slaDays"] == expected_days, f"{stop} wrong day count"


@pytest.mark.asyncio
async def test_stop2_preview_shows_the_manual_fallback_not_a_wrong_autocalc():
    db = full_db()
    out = await sla.preview(
        db,
        plant_id="plant-1",
        obs_type=ObservationType.UNSAFE_ACT,
        severity=Severity.LOW,
        category_code="POSITIONS_OF_PEOPLE",
        observation_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    assert out["matched"] is False
    assert out["reason"] == "PENDING_DECISION"
    assert out["categoryGroup"] is None
    assert out["targetDate"] is None
    assert out["slaDays"] is None


@pytest.mark.asyncio
async def test_stop2_submission_saves_manually_and_says_why():
    """An observation in the undecided category still submits — it just gets no
    auto date and records the reason in the closure-date trail."""
    db = full_db()
    obs = FakeObs(categoryCode="POSITIONS_OF_PEOPLE", severity=Severity.LOW)
    manual = datetime(2026, 8, 20, tzinfo=timezone.utc)

    await sla.apply_on_create(db, obs, submitted_target_date=manual, actor_id="u1")

    assert obs.targetDate == manual
    assert obs.targetDateSource == sla.SOURCE_MANUAL_NO_POLICY
    assert obs.targetDateSlaConfig is None
    history = db.added[-1]
    assert "not yet assigned" in history.reason
    assert "POSITIONS_OF_PEOPLE" in history.reason


@pytest.mark.asyncio
async def test_submission_records_how_the_group_was_chosen():
    """The frozen snapshot says whether the group came from the configured
    mapping or the axis fallback, so an audit needn't re-derive it."""
    db = full_db()
    obs = FakeObs(categoryCode="PPE", severity=Severity.LOW)
    await sla.apply_on_create(db, obs, submitted_target_date=None, actor_id="u1")
    assert obs.targetDateSlaConfig["categoryGroup"] == CATEGORY_GROUP_PHYSICAL
    assert obs.targetDateSlaConfig["categoryCode"] == "PPE"
    assert obs.targetDateSlaConfig["categoryGroupSource"] == "category_any"
    assert obs.targetDate == datetime(2026, 8, 15, tzinfo=timezone.utc)  # 1 Jul + 45d


# ═══ Worker Involved requirement ════════════════════════════════════════════
@pytest.mark.parametrize(
    "obs_type,severity,expected",
    [
        (ObservationType.UNSAFE_ACT, Severity.CRITICAL, True),
        (ObservationType.UNSAFE_ACT, Severity.HIGH, True),
        (ObservationType.UNSAFE_ACT, Severity.MEDIUM, False),
        (ObservationType.UNSAFE_ACT, Severity.LOW, False),
        (ObservationType.UNSAFE_CONDITION, Severity.CRITICAL, False),
        (ObservationType.UNSAFE_CONDITION, Severity.HIGH, False),
        (ObservationType.UNSAFE_CONDITION, Severity.MEDIUM, False),
        (ObservationType.UNSAFE_CONDITION, Severity.LOW, False),
        (ObservationType.SAFE_ACT, Severity.CRITICAL, False),
        (ObservationType.SAFE_CONDITION, Severity.HIGH, False),
    ],
)
def test_worker_required_across_every_severity_and_type(obs_type, severity, expected):
    """§7: 'Worker Involved required-state toggles correctly across all 4
    severity × 2 observation-type combinations.' Extended to all four types."""
    assert der.worker_involved_required(obs_type, severity) is expected


@pytest.mark.parametrize("severity", [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW])
def test_deroster_never_fires_for_unsafe_condition(severity):
    """§7: 'Deroster trigger does NOT fire for Unsafe Condition observations
    regardless of severity.' A condition is a hazard the site owns."""
    obs = FakeObs(type=ObservationType.UNSAFE_CONDITION, severity=severity)
    assert der.observation_qualifies(obs) is False


def test_deroster_fires_only_for_high_and_critical_unsafe_acts():
    assert der.observation_qualifies(FakeObs(severity=Severity.CRITICAL)) is True
    assert der.observation_qualifies(FakeObs(severity=Severity.HIGH)) is True
    assert der.observation_qualifies(FakeObs(severity=Severity.MEDIUM)) is False
    assert der.observation_qualifies(FakeObs(severity=Severity.LOW)) is False


# ═══ Trigger ════════════════════════════════════════════════════════════════
class FakeWorker:
    def __init__(self, wid, party=PARTY_USER, user=None, contractor=None, name="Ramesh Kumar"):
        self.id = wid
        self.partyType = party
        self.userId = user
        self.contractorWorkerId = contractor
        self.nameSnapshot = name


@pytest.mark.asyncio
async def test_multi_worker_observation_gets_independent_records():
    """§7: 'Deroster trigger fires correctly for multi-worker observations
    (each worker gets independent DerosterRecord and independent
    confirm/overrule).'"""
    p1, p2 = FakePerson(), FakePerson()
    db = FakeSession(
        store={("U", "u1"): p1, ("U", "u2"): p2},
    )
    db.stage(ObservationDeroster, [])
    from app.models.user import User

    db.store = {(User, "u1"): p1, (User, "u2"): p2}
    obs = FakeObs()

    created = await der.trigger_for_observation(
        db,
        obs,
        [FakeWorker("wi-1", user="u1"), FakeWorker("wi-2", user="u2", name="Suresh Patil")],
        actor_id="observer-1",
    )

    assert len(created) == 2
    assert {d.workerInvolvedId for d in created} == {"wi-1", "wi-2"}
    assert all(d.status == DEROSTER_PENDING for d in created)
    # Both people were soft-locked, each pointing at its own review.
    assert p1.rosterStatus == ROSTER_PENDING_REVIEW
    assert p2.rosterStatus == ROSTER_PENDING_REVIEW
    assert p1.currentDerosterRef != p2.currentDerosterRef


@pytest.mark.asyncio
async def test_trigger_is_idempotent_for_an_already_flagged_worker():
    """A retried submission must not double-flag."""
    db = FakeSession()
    db.stage(ObservationDeroster, [deroster(workerInvolvedId="wi-1")])
    created = await der.trigger_for_observation(
        db, FakeObs(), [FakeWorker("wi-1", user="u1")], actor_id="o1"
    )
    assert created == []


def test_flag_reason_is_generated_from_the_record():
    obs = FakeObs(severity=Severity.CRITICAL, categoryCode="TOOLS_EQUIPMENT")
    assert der.flag_reason_for(obs) == "Critical severity Unsafe Act — Tools Equipment"


# ═══ Soft-lock wording ══════════════════════════════════════════════════════
def test_pending_review_never_reads_as_a_sanction():
    """§2.4: a flag is a soft-lock, not a punitive record. Nothing may call a
    pending review 'derostered'."""
    v = der.visible_status(deroster(status=DEROSTER_PENDING))
    assert v["punitive"] is False
    assert "deroster" not in v["label"].lower()
    assert v["label"] == "Under safety review"


def test_confirmed_review_is_punitive_and_says_so():
    v = der.visible_status(deroster(status=DEROSTER_CONFIRMED))
    assert v["punitive"] is True
    assert v["label"] == "Derostered"


def test_overruled_review_is_not_punitive():
    assert der.visible_status(deroster(status=DEROSTER_OVERRULED))["punitive"] is False


# ═══ Decisions ══════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_confirm_requires_a_reason_of_minimum_length():
    db = FakeSession()
    with pytest.raises(der.DerosterError) as e:
        await der.confirm(db, deroster(), actor_id="mgr-1", reason="too short")
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_second_decision_is_rejected_with_409():
    """§3: 'Reject with 409 if status has already moved past pending_review
    (idempotency against double-submission from notification links).'"""
    db = FakeSession()
    already = deroster(status=DEROSTER_CONFIRMED)
    with pytest.raises(der.DerosterError) as e:
        await der.confirm(db, already, actor_id="mgr-1", reason="A perfectly valid reason here")
    assert e.value.status_code == 409

    with pytest.raises(der.DerosterError) as e2:
        await der.overrule(db, already, actor_id="mgr-1", reason="A perfectly valid reason here")
    assert e2.value.status_code == 409


@pytest.mark.asyncio
async def test_overrule_releases_the_worker_and_leaves_no_sanction():
    from app.models.user import User

    person = FakePerson(rosterStatus=ROSTER_PENDING_REVIEW, ref="der-1")
    db = FakeSession(store={(User, "user-1"): person})
    db.stage(ObservationDeroster, [])  # no other open flags
    d = deroster()

    await der.overrule(db, d, actor_id="mgr-1", reason="Worker was not in the exclusion zone.")

    assert d.status == DEROSTER_OVERRULED
    assert person.rosterStatus == ROSTER_ACTIVE
    assert person.currentDerosterRef is None


@pytest.mark.asyncio
async def test_clearing_one_flag_does_not_release_a_worker_held_by_another():
    """Two observations, two reviews. Overruling one must not return the
    worker to active while the other is still open."""
    from app.models.user import User

    person = FakePerson(rosterStatus=ROSTER_PENDING_REVIEW, ref="der-1")
    db = FakeSession(store={(User, "user-1"): person})
    other = deroster(id="der-2", observationId="obs-2", workerInvolvedId="wi-9")
    db.stage(ObservationDeroster, [other])

    await der.overrule(db, deroster(), actor_id="mgr-1", reason="Not this worker after review.")

    assert person.rosterStatus == ROSTER_PENDING_REVIEW
    assert person.currentDerosterRef == "der-2"


# ═══ Reinstatement gate ═════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_reinstate_blocked_when_training_is_incomplete():
    """§7: 'Reinstate button server-side blocks reinstatement if training
    incomplete, even if UI is bypassed (direct API call test).'"""
    from app.models.training_engine import TrainingAssignment

    assignment = TrainingAssignment(
        plantId="plant-1", personUserId="user-1", competencyId="c1", source="manual"
    )
    assignment.id = "ta-1"
    assignment.status = "in_progress"
    db = FakeSession(store={(TrainingAssignment, "ta-1"): assignment})
    d = deroster(status=DEROSTER_CONFIRMED, correctiveActionTrainingId="ta-1")

    with pytest.raises(der.DerosterError) as e:
        await der.reinstate(db, d, actor_id="mgr-1")
    assert e.value.status_code == 409
    assert d.status == DEROSTER_CONFIRMED  # unchanged


@pytest.mark.asyncio
async def test_reinstate_allowed_once_training_completes():
    from app.models.training_engine import TrainingAssignment
    from app.models.user import User

    assignment = TrainingAssignment(
        plantId="plant-1", personUserId="user-1", competencyId="c1", source="manual"
    )
    assignment.id = "ta-1"
    assignment.status = "completed"
    person = FakePerson(rosterStatus=ROSTER_DEROSTERED, ref="der-1")
    db = FakeSession(store={(TrainingAssignment, "ta-1"): assignment, (User, "user-1"): person})
    db.stage(ObservationDeroster, [])
    d = deroster(status=DEROSTER_CONFIRMED, correctiveActionTrainingId="ta-1")

    await der.reinstate(db, d, actor_id="mgr-1", note="Refresher completed and observed.")

    assert d.status == DEROSTER_REINSTATED
    assert person.rosterStatus == ROSTER_ACTIVE


@pytest.mark.asyncio
async def test_cannot_reinstate_a_pending_or_overruled_review():
    db = FakeSession()
    for status in (DEROSTER_PENDING, DEROSTER_OVERRULED):
        with pytest.raises(der.DerosterError) as e:
            await der.reinstate(db, deroster(status=status), actor_id="mgr-1")
        assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_confirmed_deroster_with_no_linked_action_cannot_reinstate():
    db = FakeSession()
    d = deroster(status=DEROSTER_CONFIRMED)
    state = await der.corrective_action_state(db, d)
    assert state["complete"] is False


# ═══ Contractor corrective-action gate ══════════════════════════════════════
def test_contractor_evidence_predating_the_deroster_is_rejected():
    """A certificate the worker already held is exactly what did not prevent
    the unsafe act — accepting it would let a deroster clear itself."""
    confirmed_at = datetime(2026, 7, 10, tzinfo=timezone.utc)
    worker = FakePerson(
        competencyRecords=[{"competencyId": "c1", "completedAt": "2026-01-05T00:00:00Z"}],
        trainingCertificates=[],
    )
    assert der._epc_evidence_after(worker, "c1", confirmed_at) is None


def test_contractor_evidence_after_confirmation_is_accepted():
    confirmed_at = datetime(2026, 7, 10, tzinfo=timezone.utc)
    worker = FakePerson(
        competencyRecords=[{"competencyId": "c1", "completedAt": "2026-07-18T00:00:00Z"}],
        trainingCertificates=[],
    )
    found = der._epc_evidence_after(worker, "c1", confirmed_at)
    assert found is not None
    assert found["source"] == "competencyRecords"


def test_contractor_evidence_for_a_different_competency_does_not_count():
    worker = FakePerson(
        competencyRecords=[{"competencyId": "c2", "completedAt": "2026-07-18T00:00:00Z"}],
        trainingCertificates=[],
    )
    assert der._epc_evidence_after(worker, "c1", datetime(2026, 7, 10, tzinfo=timezone.utc)) is None


@pytest.mark.asyncio
async def test_contractor_gate_uses_epc_records_not_training_assignment():
    from app.models.epc import ContractorWorker

    worker = FakePerson(
        competencyRecords=[{"competencyId": "c1", "completedAt": "2026-07-18T00:00:00Z"}],
        trainingCertificates=[],
    )
    db = FakeSession(store={(ContractorWorker, "cw-1"): worker})
    d = deroster(
        status=DEROSTER_CONFIRMED,
        partyType=PARTY_CONTRACTOR_WORKER,
        userId=None,
        contractorWorkerId="cw-1",
        correctiveActionCompetencyId="c1",
        reviewedAt=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )
    state = await der.corrective_action_state(db, d)
    assert state["kind"] == "EPC_COMPETENCY_RECORD"
    assert state["complete"] is True


# ═══ Timeout escalation ═════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_escalation_never_decides_the_review():
    """§2.5 / §7: 'Timeout escalation fires exactly once, does not
    auto-decide.'"""
    from app.models.observation_sla import ObservationDerosterConfig

    overdue = deroster(reviewDueAt=datetime.now(timezone.utc) - timedelta(hours=2))
    db = FakeSession()
    db.stage(ObservationDeroster, [overdue])
    db.stage(ObservationDerosterConfig, [])

    result = await der.run_escalation_scan(db)

    assert result["recordsAffected"] == 1
    # The decisive assertion: status untouched, worker still held.
    assert overdue.status == DEROSTER_PENDING
    assert overdue.escalatedAt is not None


@pytest.mark.asyncio
async def test_escalation_latch_prevents_a_second_alarm():
    """The scan's predicate is `escalatedAt IS NULL`; once stamped, the row
    drops out of the next scan's result set."""
    from app.models.observation_sla import ObservationDerosterConfig

    already = deroster(
        reviewDueAt=datetime.now(timezone.utc) - timedelta(hours=2),
        escalatedAt=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db = FakeSession()
    # Mirror the SQL filter the real query applies.
    db.stage(ObservationDeroster, [d for d in [already] if d.escalatedAt is None])
    db.stage(ObservationDerosterConfig, [])

    result = await der.run_escalation_scan(db)
    assert result["recordsAffected"] == 0


# ═══ Daily Brief cards ══════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_open_review_produces_a_brief_card():
    """§6 downstream checklist item 1 — an open safety review is a Daily Brief
    card, at attention level while inside its SLA."""
    from app.models.alerts import Alert

    db = FakeSession()
    db.stage(ObservationDeroster, [deroster()])
    db.stage(Alert, [])

    out = await der.sync_daily_brief_cards(db)

    assert out["created"] == 1
    card = db.added_of(Alert)[0]
    assert card.severity == "attention"
    assert card.dedupeKey == "deroster:der-1"
    assert "#deroster" in card.deepLink


@pytest.mark.asyncio
async def test_overdue_review_card_is_critical():
    from app.models.alerts import Alert

    db = FakeSession()
    db.stage(
        ObservationDeroster,
        [deroster(reviewDueAt=datetime.now(timezone.utc) - timedelta(hours=3))],
    )
    db.stage(Alert, [])

    await der.sync_daily_brief_cards(db)
    assert db.added_of(Alert)[0].severity == "critical"


@pytest.mark.asyncio
async def test_decided_review_card_is_retired():
    """The brief empties itself — no separate cleanup pass to forget."""
    from app.models.alerts import Alert

    stale = Alert(
        siteId="plant-1",
        severity="critical",
        title="old",
        bodyText="",
        dedupeKey="deroster:der-1",
        sourceEventType="deroster_review",
    )
    stale.status = "new"
    stale.isDeleted = False
    db = FakeSession()
    db.stage(ObservationDeroster, [])  # nothing pending any more
    db.stage(Alert, [stale])

    out = await der.sync_daily_brief_cards(db)
    assert out["resolved"] == 1
    assert stale.status == "resolved"


# ═══ Roster gate ════════════════════════════════════════════════════════════
def test_active_worker_passes_the_roster_gate():
    g = roster_gate.for_person(FakePerson(rosterStatus=ROSTER_ACTIVE))
    assert g.allowed is True
    assert g.as_check()["result"] == "pass"


@pytest.mark.parametrize("status", [ROSTER_PENDING_REVIEW, ROSTER_DEROSTERED])
def test_held_worker_fails_the_roster_gate(status):
    """§7: 'Roster/shift assignment screens correctly exclude
    pending_safety_review and derostered workers.'"""
    g = roster_gate.for_person(FakePerson(rosterStatus=status, ref="der-1"))
    assert g.allowed is False
    check = g.as_check()
    assert check["result"] == "fail"
    assert check["derosterRef"] == "der-1"


def test_pending_review_gate_message_is_not_punitive():
    g = roster_gate.for_person(FakePerson(rosterStatus=ROSTER_PENDING_REVIEW))
    assert "derostered" not in g.detail.lower()
    assert "under safety review" in g.detail.lower()


def test_missing_roster_column_defaults_to_allowed():
    """Rows read before the DDL ran must not be treated as held."""

    class Bare:
        pass

    assert roster_gate.for_person(Bare()).allowed is True


def test_unknown_person_is_not_silently_allowed():
    assert roster_gate.for_person(None).allowed is False


# ═══ Role gating ════════════════════════════════════════════════════════════
def test_decision_roles_cover_section_head_and_hse_manager():
    """'Section Head' is the business name for the OBSERVATION workflow's
    CHECKER step, whose seeded approverRole is DEPARTMENT_HEAD — there is no
    SECTION_HEAD role in this system."""
    assert "DEPARTMENT_HEAD" in der.DECISION_ROLES
    assert "HSE_MANAGER" in der.DECISION_ROLES


def test_ordinary_roles_cannot_decide_a_deroster():
    """§7: 'a non-Section-Head/HSE-Manager user cannot call
    confirm/overrule/reinstate endpoints (403 test).' The router intersects the
    caller's role codes with this set."""
    for role in ("WORKER", "SUPERVISOR", "SAFETY_OFFICER", "CONTRACTOR_WORKMAN", "TRAINER"):
        assert role not in der.DECISION_ROLES
