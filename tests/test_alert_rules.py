"""Impact rules — offline unit tests (spec Part 3: "unit tests for every
impact rule: given event + fixture graph → expected alerts").

Rules only touch the RuleContext protocol, so a hand-rolled fake context +
SimpleNamespace events cover them with no DB — the house test style.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.alerts import rule_registry
from app.services.alerts.rules import (
    capa_overdue,
    hira_control_failed,
    observation_cluster,
    ptw_changed,
    ptw_expiring,
    rca_completed,
    rca_reopened,
)


def _event(event_type: str, *, entity_id="e1", ref="REF-1", site="plant-1", payload=None):
    return SimpleNamespace(
        eventType=event_type, entityType="X", entityId=entity_id, entityRef=ref,
        siteId=site, payload=payload or {}, occurredAt=datetime.now(timezone.utc),
    )


def _capa(id="c1", number="CAPA-S-2026-NW-088", state="ACTIONS_PLANNED", due=None, title="Fix guard"):
    return SimpleNamespace(id=id, number=number, state=state, dueAt=due, title=title,
                           open=state not in ("CLOSED", "CLOSED_RECURRED", "CANCELLED"))


def _permit(id="p1", number="PTW-NW-2026-2231", type="HOT_WORK", area="area-1", status="ACTIVE"):
    return SimpleNamespace(id=id, number=number, type=type, plantId="plant-1", areaId=area,
                           validTo=datetime(2026, 7, 8, tzinfo=timezone.utc), status=status)


class FakeCtx:
    """Configurable RuleContext double."""

    def __init__(self, *, capas=None, permits=None, permit=None, origin=("plant-1", "area-1"),
                 area_names=None, high_count=0):
        self._capas = capas or []
        self._permits = permits or []
        self._permit = permit
        self._origin = origin
        self._area_names = area_names or {"area-1": "Cutting Hall"}
        self._high_count = high_count

    async def capas_for_source(self, source_type_code, ref_id):
        return self._capas

    async def active_permits(self, plant_id, area_id=None, exclude_id=None):
        rows = [p for p in self._permits if area_id is None or p.areaId == area_id]
        return [p for p in rows if p.id != exclude_id]

    async def permit(self, permit_id):
        return self._permit

    async def rca_origin_area(self, rca_id):
        return self._origin

    async def area_name(self, area_id):
        return self._area_names.get(area_id)

    async def count_high_submissions(self, plant_id, area_id, category_l1_code, days):
        return self._high_count

    async def permits_citing_hira_control(self, plant_id, control_name):
        return self._permits


# ── registry sanity ───────────────────────────────────────────────────────────
def test_registry_covers_every_spec_event_type():
    covered = {et for rule in rule_registry() for et in rule.event_types}
    for required in ("rca.completed", "rca.reopened", "ptw.suspended", "ptw.modified",
                     "ptw.expiring", "capa.overdue", "observation.triaged_high", "hira.control_failed"):
        assert required in covered, f"no rule handles {required}"


# ── rca.completed ─────────────────────────────────────────────────────────────
async def test_rca_completed_counts_open_capas_and_earliest_due():
    due = datetime(2026, 7, 20, tzinfo=timezone.utc)
    ctx = FakeCtx(capas=[
        _capa(id="c1", due=due), _capa(id="c2", number="CAPA-S-2026-NW-089"),
        _capa(id="c3", number="CAPA-S-2026-NW-090", state="CLOSED"),
    ])
    drafts = await rca_completed.RULE.resolve(_event("rca.completed", ref="RCA-2026-0104"), ctx)
    assert len(drafts) == 1
    d = drafts[0]
    assert d.severity == "attention"
    assert "RCA-2026-0104 closed → 2 corrective actions now active" in d.title
    assert "2026-07-20" in d.body_text
    assert len(d.impacted) == 2  # closed CAPA excluded


async def test_rca_completed_silent_without_open_capas():
    drafts = await rca_completed.RULE.resolve(_event("rca.completed"), FakeCtx(capas=[_capa(state="CLOSED")]))
    assert drafts == []


# ── rca.reopened ──────────────────────────────────────────────────────────────
async def test_rca_reopened_is_critical_and_flags_origin_area_permits():
    ctx = FakeCtx(permits=[_permit(), _permit(id="p2", number="PTW-NW-2026-0841")],
                  capas=[_capa()])
    drafts = await rca_reopened.RULE.resolve(_event("rca.reopened", ref="RCA-2026-0104"), ctx)
    assert len(drafts) == 1
    d = drafts[0]
    assert d.severity == "critical"
    assert "2 permits rely on its controls" in d.title
    assert "Cutting Hall" in d.body_text
    assert {e.type for e in d.impacted} == {"PTW", "CAPA"}


# ── ptw.* changes ─────────────────────────────────────────────────────────────
async def test_ptw_suspended_is_critical_with_overlap_count():
    ctx = FakeCtx(permit=_permit(), permits=[_permit(), _permit(id="p2", number="PTW-NW-2026-0841")])
    drafts = await ptw_changed.RULE.resolve(
        _event("ptw.suspended", entity_id="p1", payload={"reason": "Gas exceedance"}), ctx)
    d = drafts[0]
    assert d.severity == "critical"
    assert "PTW-NW-2026-2231 suspended → 1 overlapping permit flagged" in d.title
    assert "Gas exceedance" in d.body_text
    assert d.impacted[0].ref == "PTW-NW-2026-0841"  # the suspended permit itself excluded


async def test_ptw_modified_is_attention():
    ctx = FakeCtx(permit=_permit(), permits=[_permit()])
    drafts = await ptw_changed.RULE.resolve(_event("ptw.modified", entity_id="p1"), ctx)
    assert drafts[0].severity == "attention"


async def test_ptw_change_silent_when_permit_gone():
    drafts = await ptw_changed.RULE.resolve(_event("ptw.suspended"), FakeCtx(permit=None))
    assert drafts == []


# ── ptw.expiring ──────────────────────────────────────────────────────────────
async def test_ptw_expiring_reports_hours_left():
    ctx = FakeCtx(permit=_permit())
    drafts = await ptw_expiring.RULE.resolve(
        _event("ptw.expiring", entity_id="p1", payload={"hoursLeft": 6}), ctx)
    assert "expires in 6h — work status unconfirmed" in drafts[0].title
    assert drafts[0].severity == "attention"


async def test_ptw_expiring_silent_once_permit_left_active():
    ctx = FakeCtx(permit=_permit(status="CLOSED"))
    assert await ptw_expiring.RULE.resolve(_event("ptw.expiring", payload={"hoursLeft": 6}), ctx) == []


# ── capa.overdue ──────────────────────────────────────────────────────────────
async def test_capa_overdue_inherits_critical_from_source():
    ev = _event("capa.overdue", ref="CAPA-S-2026-NW-088", payload={
        "daysOverdue": 5, "sourceRef": "RCA-2026-0104", "sourceHref": "/erm/rca/r1",
        "sourceId": "r1", "sourceType": "RCA", "sourceSeverity": "CRITICAL",
        "dueDate": "2026-07-01", "ownerName": "Priya Nair",
    })
    d = (await capa_overdue.RULE.resolve(ev, FakeCtx()))[0]
    assert d.severity == "critical"
    assert "overdue 5 days → linked to RCA-2026-0104 (fatal-potential source)" in d.title
    assert "Priya Nair" in d.body_text
    assert len(d.impacted) == 2


async def test_capa_overdue_defaults_to_attention():
    ev = _event("capa.overdue", payload={"daysOverdue": 2})
    assert (await capa_overdue.RULE.resolve(ev, FakeCtx()))[0].severity == "attention"


# ── observation cluster ───────────────────────────────────────────────────────
async def test_cluster_fires_at_threshold():
    ev = _event("observation.triaged_high",
                payload={"categoryL1": "machine_guarding", "areaId": "area-1", "riskLevel": "HIGH"})
    drafts = await observation_cluster.RULE.resolve(ev, FakeCtx(high_count=3))
    assert len(drafts) == 1
    assert "Cluster: 3× machine guarding in Cutting Hall this week" in drafts[0].title
    assert drafts[0].severity == "attention"


async def test_cluster_critical_when_latest_is_critical():
    ev = _event("observation.triaged_high",
                payload={"categoryL1": "machine_guarding", "areaId": "area-1", "riskLevel": "CRITICAL"})
    assert (await observation_cluster.RULE.resolve(ev, FakeCtx(high_count=4)))[0].severity == "critical"


async def test_cluster_silent_below_threshold():
    ev = _event("observation.triaged_high", payload={"categoryL1": "ppe", "areaId": "area-1"})
    assert await observation_cluster.RULE.resolve(ev, FakeCtx(high_count=2)) == []


# ── hira.control_failed ───────────────────────────────────────────────────────
async def test_hira_control_failed_narrows_by_influenced_permit_types():
    ctx = FakeCtx(permits=[_permit(), _permit(id="p2", number="PTW-NW-2026-0842", type="CONFINED_SPACE")])
    ev = _event("hira.control_failed",
                payload={"controlName": "Hot-work fire watch", "influencedPermitTypes": ["HOT_WORK"]})
    d = (await hira_control_failed.RULE.resolve(ev, ctx))[0]
    assert d.severity == "critical"
    assert "1 active PTW cite" in d.title
    assert d.impacted[0].ref == "PTW-NW-2026-2231"
