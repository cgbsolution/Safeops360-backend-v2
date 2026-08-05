"""Time-based event scans (build spec Part 2.1 trigger table).

* ``run_ptw_expiry_scan`` — T-24h warning events for active permits, and the
  server-side expiry flip. Until now permits only expired when someone opened
  /inbox (the lazy TS sweep, DECISIONS.md D8) — this makes both the alert and
  the state change autonomous.

* ``run_capa_overdue_scan`` — CAPA overdue is a query-time predicate on this
  platform (no persisted state): the scan emits one capa.overdue event per
  overdue CAPA per run; the alert layer's 24h dedupe collapses repeats into a
  counter. Source severity rides along so the rule can escalate CRITICAL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import DomainEvent
from app.models.capa import Capa
from app.models.permit import Permit, PermitStatus
from app.services import events

EXPIRY_WARN_HOURS = 24
OPEN_CAPA_STATES = (
    "DRAFT", "SUBMITTED", "UNDER_RCA", "ACTIONS_PLANNED", "ACTIONS_IN_PROGRESS", "PENDING_VERIFICATION",
)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def _already_emitted_today(db: AsyncSession, event_type: str, entity_id: str) -> bool:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    row = (
        await db.execute(
            select(DomainEvent.id)
            .where(DomainEvent.eventType == event_type)
            .where(DomainEvent.entityId == entity_id)
            .where(DomainEvent.occurredAt >= since)
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def run_ptw_expiry_scan(db: AsyncSession) -> dict:
    now = datetime.now(timezone.utc)
    warn_until = now + timedelta(hours=EXPIRY_WARN_HOURS)
    # Closed-loop rebuild: an ISSUED permit that was never accepted also
    # dies at its validTo — otherwise it lingers acceptable forever.
    active = (
        await db.execute(
            select(Permit).where(
                Permit.status.in_((PermitStatus.ACTIVE, PermitStatus.ISSUED))
            )
        )
    ).scalars().all()

    warned = expired = 0
    for permit in active:
        valid_to = _aware(permit.validTo)
        if valid_to is None:
            continue
        if valid_to <= now:
            # server-side expiry flip (complements the lazy /inbox sweep)
            permit.status = PermitStatus.EXPIRED
            permit.expiredAt = now
            permit.expirationReason = permit.expirationReason or "VALIDITY_END"
            events.emit(
                db,
                event_type=events.PTW_EXPIRED,
                entity_type="Permit",
                entity_id=permit.id,
                entity_ref=permit.number,
                site_id=permit.plantId,
                payload={"validTo": valid_to.isoformat()},
            )
            expired += 1
        elif valid_to <= warn_until:
            if await _already_emitted_today(db, events.PTW_EXPIRING, permit.id):
                continue
            hours_left = max(1, int((valid_to - now).total_seconds() // 3600))
            events.emit(
                db,
                event_type=events.PTW_EXPIRING,
                entity_type="Permit",
                entity_id=permit.id,
                entity_ref=permit.number,
                site_id=permit.plantId,
                payload={"hoursLeft": hours_left, "validTo": valid_to.isoformat()},
            )
            warned += 1

    await db.commit()
    return {"activeScanned": len(active), "expiring": warned, "expiredFlipped": expired}


_SOURCE_HREF = {
    "ENTERPRISE_RCA": "/erm/rca/{id}",
    "SAFETY_INCIDENT": "/incidents/{id}",
    "SAFETY_OBSERVATION": "/observations/{id}",
    "NEAR_MISS": "/near-miss/{id}",
}


async def run_capa_overdue_scan(db: AsyncSession) -> dict:
    now = datetime.now(timezone.utc)
    overdue_rows = (
        await db.execute(
            select(Capa)
            .where(Capa.state.in_(OPEN_CAPA_STATES))
            .where(Capa.closureTargetDate.is_not(None))
            .where(Capa.closureTargetDate < now)
            .limit(200)
        )
    ).scalars().all()

    emitted = 0
    for capa in overdue_rows:
        if await _already_emitted_today(db, events.CAPA_OVERDUE, capa.id):
            continue
        due = _aware(capa.closureTargetDate) or now
        days = max(1, (now - due).days)

        # source enrichment — the rule inherits severity from the source record
        # (falls back to the CAPA's own severity when the source can't say)
        source_ref = None
        source_href = None
        source_severity: str | None = capa.severity if capa.severity in ("CRITICAL",) else None
        if capa.sourceTypeCode == "ENTERPRISE_RCA" and capa.sourceReferenceId:
            from app.models.incident import Incident
            from app.models.rca import RootCauseAnalysis

            rca = await db.get(RootCauseAnalysis, capa.sourceReferenceId)
            if rca is not None:
                source_ref = rca.rcaCode
                if rca.sourceEventId:  # fatal-potential check via the origin incident
                    incident = await db.get(Incident, rca.sourceEventId)
                    if incident is not None and (incident.severity or "").upper() == "CRITICAL":
                        source_severity = "CRITICAL"
        elif capa.sourceTypeCode == "SAFETY_INCIDENT" and capa.sourceReferenceId:
            from app.models.incident import Incident

            incident = await db.get(Incident, capa.sourceReferenceId)
            if incident is not None:
                source_ref = incident.number
                source_severity = incident.severity or source_severity
        if source_ref is None and capa.sourceReferenceSummary:
            source_ref = capa.sourceReferenceSummary[:60]
        if capa.sourceTypeCode in _SOURCE_HREF and capa.sourceReferenceId:
            source_href = _SOURCE_HREF[capa.sourceTypeCode].format(id=capa.sourceReferenceId)

        owner_name = None
        if capa.raisedByUserId:
            from app.models.user import User

            owner = await db.get(User, capa.raisedByUserId)
            owner_name = owner.name if owner else None

        events.emit(
            db,
            event_type=events.CAPA_OVERDUE,
            entity_type="Capa",
            entity_id=capa.id,
            entity_ref=capa.capaNumber,
            site_id=capa.plantId,
            payload={
                "daysOverdue": days,
                "dueDate": due.date().isoformat(),
                "title": capa.title,
                "ownerName": owner_name,
                "sourceType": capa.sourceTypeCode,
                "sourceId": capa.sourceReferenceId,
                "sourceRef": source_ref,
                "sourceHref": source_href,
                "sourceSeverity": source_severity,
            },
        )
        emitted += 1

    await db.commit()
    return {"overdue": len(overdue_rows), "eventsEmitted": emitted}
