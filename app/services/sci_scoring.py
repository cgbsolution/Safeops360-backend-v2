"""SCI scoring engine — points from verified operational events.

Every point is earned from a verified system event (no self-report, no manual
award). This backfill scores existing records across the source modules and
writes immutable ledger entries (idempotent via a unique
(userId, sourceModule, sourceTransactionId) key — a point can never be
double-awarded). Real-time event-bus consumers will later call the same
per-source scorers. See SCI_Kaizen_Build_Prompt §4.2 / §9.

Sources scored: Safety Observation, Near Miss, FLRA, PTW, Incident, Training,
Inspection, CAPA. (HIRA review, Kaizen Wall, streaks are Phase 2.)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capa import Capa
from app.models.equipment import Inspection
from app.models.flra import FLRA, FLRACrewSignature
from app.models.incident import Incident
from app.models.near_miss import NearMiss
from app.models.observation import Observation
from app.models.permit import Permit, PermitCrewMember
from app.models.sci import SciLedgerEntry
from app.models.training import TrainingCertificate
from app.models.user import User

_SEVERITY_MULT = {"LOW": 1.0, "MEDIUM": 2.0, "HIGH": 3.0, "CRITICAL": 3.0}


def _ev(v: Any) -> str:
    return v.value if hasattr(v, "value") else str(v)


def _period(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m") if dt else "unknown"


async def sync_all(db: AsyncSession, *, plant_id: str, actor: str = "SYSTEM") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    existing = {
        (e.userId, e.sourceModule, e.sourceTransactionId)
        for e in (await db.execute(select(SciLedgerEntry).where(SciLedgerEntry.plantId == plant_id))).scalars().all()
    }
    stats: dict[str, int] = {}

    def award(*, user_id: str | None, module: str, txn: str, event: str, base: int, mult: float, when: datetime | None) -> None:
        if not user_id:
            return
        key = (user_id, module, txn)
        if key in existing:
            return
        existing.add(key)
        final = round(base * mult)
        db.add(SciLedgerEntry(
            userId=user_id, plantId=plant_id, sourceModule=module, sourceTransactionId=txn,
            eventType=event, basePoints=base, multiplier=mult, finalPoints=final,
            scoringPeriod=_period(when), auditTrail=[{"at": now.isoformat(), "by": actor, "action": "AWARDED", "event": event}],
        ))
        stats[module] = stats.get(module, 0) + 1

    # Safety Observation — full workflow (CLOSED). base 10 × severity.
    for o in (await db.execute(select(Observation).where(Observation.plantId == plant_id).where(Observation.status == "CLOSED"))).scalars().all():
        mult = _SEVERITY_MULT.get(_ev(o.severity), 1.0)
        award(user_id=o.observerId, module="SAFETY_OBS", txn=o.id, event="Safety Observation Closed", base=10, mult=mult, when=o.closedAt)

    # Near Miss — reported and routed to workflow.
    for nm in (await db.execute(select(NearMiss).where(NearMiss.plantId == plant_id).where(NearMiss.status.in_(["UNDER_REVIEW", "ACTION_ASSIGNED", "CLOSED"])))).scalars().all():
        award(user_id=nm.reporterId, module="NEAR_MISS", txn=nm.id, event="Near Miss Reported & Routed", base=20, mult=1.0, when=nm.closedAt)

    # FLRA — signed by individual on own device (per signature). base 5.
    flras = (await db.execute(select(FLRA).where(FLRA.plantId == plant_id).where(FLRA.status == "COMPLETED"))).scalars().all()
    flra_ids = [f.id for f in flras]
    if flra_ids:
        sigs = (await db.execute(select(FLRACrewSignature).where(FLRACrewSignature.flraId.in_(flra_ids)))).scalars().all()
        for s in sigs:
            if s.signedAt is not None:
                award(user_id=s.userId, module="FLRA", txn=s.flraId, event="FLRA Signed", base=5, mult=1.0, when=s.signedAt)

    # Incident — first responder reports within SLA. base 25.
    for inc in (await db.execute(select(Incident).where(Incident.plantId == plant_id))).scalars().all():
        within_sla = inc.initialReportSlaTargetAt is None or (inc.reportedAt is not None and inc.reportedAt <= inc.initialReportSlaTargetAt)
        if within_sla:
            award(user_id=inc.reporterId, module="INCIDENT", txn=inc.id, event="Incident First Report (within SLA)", base=25, mult=1.0, when=inc.reportedAt or inc.date)

    # Training — module passed at/above threshold (active certificate).
    plant_user_ids = [u.id for u in (await db.execute(select(User).where(User.plantId == plant_id))).scalars().all()]
    if plant_user_ids:
        for c in (await db.execute(select(TrainingCertificate).where(TrainingCertificate.userId.in_(plant_user_ids)).where(TrainingCertificate.status.in_(["ACTIVE", "EXPIRING_SOON"])))).scalars().all():
            award(user_id=c.userId, module="TRAINING", txn=c.id, event="Training Passed", base=15, mult=1.0, when=c.issuedAt)

    # Inspection — closed with no overdue. base 12.
    for ins in (await db.execute(select(Inspection).where(Inspection.plantId == plant_id).where(Inspection.status == "COMPLETED"))).scalars().all():
        award(user_id=ins.inspectorId, module="INSPECTION", txn=ins.id, event="Inspection Closed", base=12, mult=1.0, when=ins.completedDate or ins.scheduledDate)

    # CAPA — owner closes with verified evidence. base 10.
    for capa in (await db.execute(select(Capa).where(Capa.plantId == plant_id).where(Capa.state == "VERIFIED"))).scalars().all():
        award(user_id=capa.primaryOwnerUserId, module="CAPA", txn=capa.id, event="CAPA Closed with Evidence", base=10, mult=1.0, when=capa.verificationCompletedAt or capa.createdAt)

    # PTW — crew member completes permit lifecycle (CLOSED). base 8 per crew member.
    closed = (await db.execute(select(Permit).where(Permit.plantId == plant_id).where(Permit.status == "CLOSED"))).scalars().all()
    permit_ids = [p.id for p in closed]
    permit_when = {p.id: p.closedAt or p.validTo for p in closed}
    if permit_ids:
        for cm in (await db.execute(select(PermitCrewMember).where(PermitCrewMember.permitId.in_(permit_ids)))).scalars().all():
            award(user_id=cm.userId, module="PTW", txn=cm.permitId, event="PTW Crew Lifecycle Complete", base=8, mult=1.0, when=permit_when.get(cm.permitId))

    await db.commit()
    total = sum(stats.values())
    return {"plantId": plant_id, "created": total, "byModule": stats}
