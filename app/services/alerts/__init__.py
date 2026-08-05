"""Event-driven daily alert engine (build spec Part 2).

DomainEvent rows (the outbox written by app.services.events.emit) are consumed
here by ``resolve_pending_events`` — the ``alerts_impact_resolver`` scheduler
job — through a registry of impact rules (one file per rule under
``app/services/alerts/rules/``). Rules are pure functions over a narrow
``RuleContext`` protocol so they unit-test with a fake context and no DB
(tests/test_alert_rules.py).

Alerts are MATERIALISED (never computed at read time — spec performance
budget): ``materialise`` dedupes on ``dedupeKey`` within 24h by incrementing
``count`` + bumping ``updatedAt`` instead of inserting a new card.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import Alert, DomainEvent

DEDUPE_WINDOW_HOURS = 24


# ── draft (what a rule returns) ───────────────────────────────────────────────
@dataclass
class ImpactedEntity:
    type: str  # RCA | CAPA | PTW | Observation | CaptureSubmission | HiraControl
    id: str
    ref: str  # human number, e.g. CAPA-S-2026-NW-088
    label: str
    href: str


@dataclass
class AlertDraft:
    severity: str  # critical | attention | info
    title: str  # WHAT changed
    body_text: str  # WHY it matters (the impact line)
    dedupe_key: str
    site_id: str | None = None
    body_template_key: str | None = None
    body_params: dict[str, Any] = field(default_factory=dict)
    impacted: list[ImpactedEntity] = field(default_factory=list)
    deep_link: str | None = None
    audience_roles: list[str] = field(default_factory=list)
    expires_at: datetime | None = None


# ── narrow data-access surface rules are allowed to touch ────────────────────
class RuleContext(Protocol):
    """Everything a rule may ask of the platform. Implemented for real by
    DbRuleContext below; tests pass a hand-rolled fake."""

    async def capas_for_source(self, source_type_code: str, ref_id: str) -> list[Any]: ...
    async def active_permits(
        self, plant_id: str, area_id: str | None = None, exclude_id: str | None = None
    ) -> list[Any]: ...
    async def permit(self, permit_id: str) -> Any | None: ...
    async def rca_origin_area(self, rca_id: str) -> tuple[str | None, str | None]: ...
    async def area_name(self, area_id: str | None) -> str | None: ...
    async def count_high_submissions(
        self, plant_id: str, area_id: str | None, category_l1_code: str | None, days: int
    ) -> int: ...
    async def permits_citing_hira_control(self, plant_id: str | None, control_name: str) -> list[Any]: ...


@dataclass
class ImpactRule:
    key: str
    event_types: tuple[str, ...]
    resolve: Any  # async (event, ctx: RuleContext) -> list[AlertDraft]


# ── registry ──────────────────────────────────────────────────────────────────
def rule_registry() -> list[ImpactRule]:
    """Import-on-call so a broken rule module can't take the app down at boot;
    new rules are additive — drop a file in rules/ and list it here."""
    from app.services.alerts.rules import (
        capa_overdue,
        hira_control_failed,
        observation_cluster,
        ptw_changed,
        ptw_expiring,
        rca_completed,
        rca_reopened,
    )

    return [
        rca_completed.RULE,
        rca_reopened.RULE,
        ptw_changed.RULE,
        ptw_expiring.RULE,
        capa_overdue.RULE,
        observation_cluster.RULE,
        hira_control_failed.RULE,
    ]


# ── materialisation (dedup) ───────────────────────────────────────────────────
async def materialise(db: AsyncSession, draft: AlertDraft, source_event: DomainEvent | None = None) -> Alert:
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=DEDUPE_WINDOW_HOURS)
    existing = (
        await db.execute(
            select(Alert)
            .where(Alert.dedupeKey == draft.dedupe_key)
            .where(Alert.isDeleted.is_(False))
            .where(Alert.createdAt >= window_start)
            .order_by(Alert.createdAt.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.count += 1
        existing.title = draft.title
        existing.bodyText = draft.body_text
        existing.bodyParams = draft.body_params
        existing.impactedEntities = [vars(e) for e in draft.impacted]
        if existing.status in ("resolved", "muted") and draft.severity == "critical":
            existing.status = "new"  # a re-fired critical resurfaces
        existing.updatedAt = now
        return existing

    alert = Alert(
        siteId=draft.site_id,
        severity=draft.severity,
        title=draft.title,
        bodyTemplateKey=draft.body_template_key,
        bodyParams=draft.body_params,
        bodyText=draft.body_text,
        sourceEventType=source_event.eventType if source_event else None,
        sourceEntityType=source_event.entityType if source_event else None,
        sourceEntityId=source_event.entityId if source_event else None,
        impactedEntities=[vars(e) for e in draft.impacted],
        deepLink=draft.deep_link,
        dedupeKey=draft.dedupe_key,
        audienceRoles=draft.audience_roles,
        audienceSiteIds=[draft.site_id] if draft.site_id else [],
        expiresAt=draft.expires_at,
    )
    db.add(alert)
    return alert


# ── the resolver job body ─────────────────────────────────────────────────────
async def resolve_pending_events(db: AsyncSession, batch: int = 100) -> dict:
    from app.services.alerts.db_context import DbRuleContext

    ctx = DbRuleContext(db)
    registry = rule_registry()
    events = (
        await db.execute(
            select(DomainEvent)
            .where(DomainEvent.processedAt.is_(None))
            .order_by(DomainEvent.occurredAt.asc())
            .limit(batch)
        )
    ).scalars().all()

    produced = 0
    errors = 0
    for event in events:
        try:
            for rule in registry:
                if event.eventType not in rule.event_types:
                    continue
                drafts = await rule.resolve(event, ctx)
                for draft in drafts or []:
                    await materialise(db, draft, event)
                    produced += 1
            event.processedAt = datetime.now(timezone.utc)
            event.processingError = None
        except Exception as e:  # noqa: BLE001 — a poisoned event must not wedge the queue
            print(f"[alerts] rule failure on event {event.id} ({event.eventType}): {e}", file=sys.stderr)
            event.processedAt = datetime.now(timezone.utc)
            event.processingError = str(e)[:500]
            errors += 1

    await db.commit()
    return {"events": len(events), "alerts": produced, "errors": errors}
