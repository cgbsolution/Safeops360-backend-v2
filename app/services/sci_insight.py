"""WIA-01 — SCI Worker Insight Agent (SCI-01 §8.3).

Personalised, encouraging insight cards on the My Score tab, computed from the
worker's own ledger + training data. No shaming, no named comparisons — only
relative ("top X% of your plant"). Deterministic so it's testable; the natural-
language polish is a drop-in later.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sci import SciLedgerEntry
from app.models.training import TrainingCertificate

_MODULE_LABEL = {
    "SAFETY_OBS": "safety observations", "NEAR_MISS": "near-miss reports", "FLRA": "FLRA sign-offs",
    "INCIDENT": "incident reports", "TRAINING": "training completions", "INSPECTION": "inspections",
    "CAPA": "CAPA closures", "KAIZEN_WALL": "Kaizen posts",
}


async def worker_insight(db: AsyncSession, *, user_id: str, plant_id: str) -> list[str]:
    cards: list[str] = []

    # Rank percentile in the plant.
    totals = (
        await db.execute(
            select(SciLedgerEntry.userId, func.sum(SciLedgerEntry.finalPoints))
            .where(SciLedgerEntry.plantId == plant_id)
            .group_by(SciLedgerEntry.userId)
            .order_by(func.sum(SciLedgerEntry.finalPoints).desc())
        )
    ).all()
    order = [uid for uid, _ in totals]
    if user_id in order and len(order) > 1:
        rank = order.index(user_id) + 1
        pct = round(rank / len(order) * 100)
        if pct <= 10:
            cards.append(f"You're in the top {pct}% of your plant for verified safety actions — outstanding.")
        else:
            cards.append(f"You're ranked #{rank} of {len(order)} at your plant — every verified action moves you up.")

    # Strongest source.
    by_mod = (
        await db.execute(
            select(SciLedgerEntry.sourceModule, func.count())
            .where(SciLedgerEntry.plantId == plant_id).where(SciLedgerEntry.userId == user_id)
            .group_by(SciLedgerEntry.sourceModule).order_by(func.count().desc()).limit(1)
        )
    ).first()
    if by_mod:
        mod, n = by_mod
        cards.append(f"Your {_MODULE_LABEL.get(mod, mod)} are your biggest contribution — {n} logged.")

    # Training currency.
    now = datetime.now(timezone.utc)
    cert = (
        await db.execute(
            select(TrainingCertificate).where(TrainingCertificate.userId == user_id)
            .where(TrainingCertificate.validTo.isnot(None)).order_by(TrainingCertificate.validTo.asc()).limit(1)
        )
    ).scalar_one_or_none()
    if cert and cert.validTo:
        vt = cert.validTo if cert.validTo.tzinfo else cert.validTo.replace(tzinfo=timezone.utc)
        days = (vt - now).days
        if days < 0:
            cards.append("One of your training certificates has expired — book a refresher to stay permit-eligible.")
        elif days <= 30:
            cards.append(f"A training certificate expires in {days} day(s) — renew it to keep your streak and eligibility.")
        else:
            cards.append(f"Your training is current — next certificate is valid for another {days} days.")

    return cards[:3]
