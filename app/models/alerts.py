"""Event-driven daily alert dashboard — models.

``DomainEvent`` is the append-only outbox: every state-changing service path
emits one row *in the same transaction* as the entity write (see
app.services.events.emit — service layer only, never controllers). The
impact-resolver scheduler job consumes unprocessed events through the rule
registry (app/services/alerts/rules/) and materialises ``Alert`` rows, so the
daily-brief API never computes impacts at read time.

Schema is owned here (SQLAlchemy) and applied by hand-DDL
(``prisma/apply-capture-ddl.ts``) — NEVER via ``prisma db push``. Mirrored in
schema.prisma for seed-script access. ``siteId`` carries Plant.id (the spec's
naming; QueryScope.apply falls back to siteId automatically).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin, SoftDeleteMixin, TimestampMixin


class DomainEvent(Base, IdMixin):
    """Append-only domain event (outbox pattern). ``processedAt`` is stamped by
    the impact resolver after every matching rule ran; ``processingError``
    keeps the last failure so a poisoned event never wedges the queue."""

    __tablename__ = "DomainEvent"

    tenantId: Mapped[str] = mapped_column(String, nullable=False, default="default")
    siteId: Mapped[str | None] = mapped_column(String, index=True)  # Plant.id
    # e.g. rca.completed, rca.reopened, ptw.suspended, ptw.modified,
    # ptw.expiring, capa.overdue, observation.triaged_high, hira.control_failed
    eventType: Mapped[str] = mapped_column(String, nullable=False, index=True)
    entityType: Mapped[str] = mapped_column(String, nullable=False)
    entityId: Mapped[str] = mapped_column(String, nullable=False)
    entityRef: Mapped[str | None] = mapped_column(String)  # human number, e.g. RCA-2026-0104
    actorId: Mapped[str | None] = mapped_column(String)
    # {from, to, fields, ...} — rule inputs; keep it small and denormalised
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    occurredAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    processedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    processingError: Mapped[str | None] = mapped_column(Text)
    correlationId: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        Index("ix_DomainEvent_entity", "entityType", "entityId"),
    )


class Alert(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """A prioritised "what changed and why it matters" card. Produced only by
    impact rules; deduped on ``dedupeKey`` within 24h (count increments and
    updatedAt bumps instead of inserting). CRITICAL alerts cannot be muted —
    enforced in the router."""

    __tablename__ = "Alert"

    tenantId: Mapped[str] = mapped_column(String, nullable=False, default="default")
    siteId: Mapped[str | None] = mapped_column(String, index=True)  # Plant.id
    # critical | attention | info
    severity: Mapped[str] = mapped_column(String, nullable=False, default="info", index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)  # what changed
    # why it matters — template key + params so the digest renderer and the
    # feed UI share one vocabulary
    bodyTemplateKey: Mapped[str | None] = mapped_column(String)
    bodyParams: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    bodyText: Mapped[str] = mapped_column(Text, nullable=False, default="")  # rendered impact line

    sourceEventType: Mapped[str | None] = mapped_column(String)
    sourceEntityType: Mapped[str | None] = mapped_column(String)
    sourceEntityId: Mapped[str | None] = mapped_column(String)

    # [{type, id, ref, label, href}] — the deep-linkable entity pills
    impactedEntities: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    deepLink: Mapped[str | None] = mapped_column(String)

    dedupeKey: Mapped[str] = mapped_column(String, nullable=False, index=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # new | acknowledged | resolved | muted
    status: Mapped[str] = mapped_column(String, nullable=False, default="new", index=True)
    ackBy: Mapped[str | None] = mapped_column(String)
    ackAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mutedUntil: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    audienceRoles: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    audienceSiteIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    expiresAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_Alert_site_status_created", "tenantId", "siteId", "status", "createdAt"),
    )


class AlertSubscription(Base, IdMixin, TimestampMixin):
    """Daily-digest subscription: which role at which site gets the 06:00
    site-local digest, over which channels, from which minimum severity.
    ``lastSentOn`` (YYYY-MM-DD) dedupes the interval-scheduled job to one
    send per day."""

    __tablename__ = "AlertSubscription"

    tenantId: Mapped[str] = mapped_column(String, nullable=False, default="default")
    roleCode: Mapped[str] = mapped_column(String, nullable=False)
    siteId: Mapped[str | None] = mapped_column(String)  # null = all sites in scope
    channels: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # ["inapp","email"]
    # info | attention | critical
    minSeverity: Mapped[str] = mapped_column(String, nullable=False, default="attention")
    # IANA zone for the 06:00 window; default = the platform's home zone
    timezone: Mapped[str] = mapped_column(String, nullable=False, default="Asia/Kolkata")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    lastSentOn: Mapped[str | None] = mapped_column(String)  # YYYY-MM-DD (in `timezone`)


__all__ = ["DomainEvent", "Alert", "AlertSubscription"]
