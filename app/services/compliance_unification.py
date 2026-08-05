"""P2-8 — Compliance single source of truth.

LegalObligation (ERM Phase 2, governed, with tasks + attestation) is the canonical
register. RegulatoryRegistration (Facilities) becomes a display alias that points to
its LegalObligation via legalObligationId. `link_registrations_to_obligations`
backfills the link (creating an obligation where none matches). `statutory_view`
returns the obligations of statutory types — the Statutory Registers nav is then a
filtered view over the one source, not a second store.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.erm_p2 import LegalObligation
from app.models.factory_ext import RegulatoryRegistration

# RegulatoryRegistration.registrationType → LegalObligation.obligationType
_TYPE_MAP = {
    "FACTORY_ACT": "LICENCE", "FIRE_LICENSE": "LICENCE", "BOILER": "LICENCE",
    "PCB": "CONSENT", "BUILDING_CERT": "REGISTRATION", "ESI": "REGISTRATION",
    "PF": "REGISTRATION", "GST": "REGISTRATION", "OTHER": "REGISTRATION",
}
STATUTORY_TYPES = ("LICENCE", "CONSENT", "REGISTRATION", "RETURN_FILING")
_FREQ_MAP = {"ANNUAL": "ANNUAL", "BIENNIAL": "PERIODIC_RENEWAL", "TRIENNIAL": "PERIODIC_RENEWAL",
             "ONEOFF": "PERIODIC_RENEWAL", "ONGOING": "PERIODIC_RENEWAL"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _next_code(db: AsyncSession) -> str:
    n = (await db.execute(select(func.count()).select_from(LegalObligation))).scalar() or 0
    return f"LO-{_now().year}-{n + 1:04d}"


async def link_registrations_to_obligations(db: AsyncSession, actor_id: str | None = None) -> dict[str, Any]:
    """Backfill: each RegulatoryRegistration → its canonical LegalObligation
    (match on site + type + regulator/name; create if none). Idempotent — already
    linked rows are skipped. Caller commits."""
    regs = (await db.execute(select(RegulatoryRegistration).where(RegulatoryRegistration.isDeleted.is_(False)))).scalars().all()
    obls = (await db.execute(select(LegalObligation).where(LegalObligation.isDeleted.is_(False)))).scalars().all()
    linked = created = 0
    for reg in regs:
        if reg.legalObligationId:
            continue
        otype = _TYPE_MAP.get(reg.registrationType, "REGISTRATION")
        match = next(
            (o for o in obls if o.siteId == reg.siteId and o.obligationType == otype
             and (o.title.lower() == reg.registrationName.lower()
                  or (reg.issuingAuthority and o.regulatorName.lower() == (reg.issuingAuthority or "").lower()))),
            None,
        )
        if match is None:
            match = LegalObligation(
                obligationCode=await _next_code(db), title=reg.registrationName, obligationType=otype,
                statuteReference=reg.registrationType, regulatorName=reg.issuingAuthority or "—",
                siteId=reg.siteId, ownerId=actor_id or "SYSTEM", frequency=_FREQ_MAP.get(reg.renewalFrequency, "ANNUAL"),
                validFrom=reg.issueDate, validUntil=reg.expiryDate, renewalLeadDays=reg.alertThresholdDays or 60,
                status="COMPLIANT", isActive=(reg.status == "VALID"),
            )
            db.add(match)
            await db.flush()
            obls.append(match)
            created += 1
        reg.legalObligationId = match.id
        linked += 1
    await db.flush()
    return {"registrations": len(regs), "linked": linked, "obligationsCreated": created}


async def statutory_view(db: AsyncSession, plant_ids: list[str] | None) -> dict[str, Any]:
    """The Statutory Registers view = LegalObligations of statutory types. Single
    source of truth; same data the CAMS Compliance Tracker surfaces."""
    q = (
        select(LegalObligation).where(LegalObligation.isDeleted.is_(False))
        .where(LegalObligation.obligationType.in_(STATUTORY_TYPES))
    )
    if plant_ids is not None:
        q = q.where(LegalObligation.siteId.in_(plant_ids or ["__none__"]))
    obls = (await db.execute(q.order_by(LegalObligation.validUntil.asc().nulls_last()))).scalars().all()
    now = _now()
    rows = []
    for o in obls:
        vu = o.validUntil.replace(tzinfo=timezone.utc) if o.validUntil and o.validUntil.tzinfo is None else o.validUntil
        days_left = (vu - now).days if vu else None
        rows.append({
            "id": o.id, "obligationCode": o.obligationCode, "title": o.title, "obligationType": o.obligationType,
            "regulatorName": o.regulatorName, "siteId": o.siteId, "status": o.status,
            "validUntil": o.validUntil.isoformat() if o.validUntil else None, "daysToExpiry": days_left,
            "statuteReference": o.statuteReference,
        })
    return {"items": rows, "total": len(rows), "source": "LegalObligation"}
