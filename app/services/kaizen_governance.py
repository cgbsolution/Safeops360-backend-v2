"""Kaizen Wall governance + AI agents (SCI-01 §7.3 / §8).

- Committee assignment (KaizenCommitteeRotation): 3 engaged workers from
  different departments, excluding management and the previous month's members;
  HSE-Manager seat fallback when the pool is too small.
- KWP-01 pre-screen: duplicate detection, profanity/irrelevance filter,
  category suggestion (deterministic stand-ins for the semantic model).
- LDA-01 lessons distribution: high-scoring approved posts are distributed
  cross-plant with a one-time +15 bonus and loop prevention.

These run inside the request flow; they're deterministic so they're fully
testable without a live model call (the semantic upgrade is a drop-in later).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kaizen import KaizenCommitteeRotation, KaizenPost
from app.models.plant import Plant
from app.models.sci import SciLedgerEntry
from app.models.user import User

# Roles that are supervisor-and-above — excluded from the committee pool.
_MGMT_ROLES = {
    "HSE_MANAGER", "PLANT_HEAD", "ADMIN", "SYSTEM_ADMIN", "CORPORATE_HSE",
    "SAFETY_OFFICER", "MAINTENANCE_HEAD", "DEPARTMENT_HEAD", "SUPERVISOR",
}
_PROFANITY = {"damn", "hell", "stupid", "idiot", "crap", "useless"}  # minimal demo list
_STOP = {"the", "a", "an", "in", "on", "at", "of", "to", "and", "is", "for", "near", "with", "from", "by"}


def _period(now: datetime) -> str:
    return now.strftime("%Y-%m")


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP and len(w) > 2}


async def get_or_assign_committee(db: AsyncSession, plant_id: str, *, now: datetime | None = None) -> KaizenCommitteeRotation:
    now = now or datetime.now(timezone.utc)
    period = _period(now)
    existing = (
        await db.execute(
            select(KaizenCommitteeRotation).where(KaizenCommitteeRotation.plantId == plant_id).where(KaizenCommitteeRotation.periodMonth == period)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    # Previous month's members (no consecutive same-combination).
    prev = (
        await db.execute(
            select(KaizenCommitteeRotation).where(KaizenCommitteeRotation.plantId == plant_id).order_by(KaizenCommitteeRotation.createdAt.desc()).limit(1)
        )
    ).scalar_one_or_none()
    prev_members = set(prev.memberUserIds) if prev else set()

    # Engaged pool: users at the plant with SCI activity, ranked by points.
    rows = (
        await db.execute(
            select(SciLedgerEntry.userId, func.sum(SciLedgerEntry.finalPoints))
            .where(SciLedgerEntry.plantId == plant_id)
            .group_by(SciLedgerEntry.userId)
            .order_by(func.sum(SciLedgerEntry.finalPoints).desc())
        )
    ).all()
    engaged_order = [uid for uid, _ in rows]
    users = {
        u.id: u
        for u in ((await db.execute(select(User).where(User.id.in_(engaged_order)))).scalars().all() if engaged_order else [])
    }
    candidates = [
        users[uid] for uid in engaged_order
        if uid in users and (users[uid].role or "").upper() not in _MGMT_ROLES
    ]

    selected: list[str] = []
    seen_depts: set[str] = set()
    for u in candidates:
        if u.id in prev_members:
            continue
        dept = u.department or u.id  # treat missing dept as unique
        if dept in seen_depts:
            continue
        selected.append(u.id)
        seen_depts.add(dept)
        if len(selected) == 3:
            break

    hse_seat = False
    if len(selected) < 3:
        hse_seat = True  # small plant / too few depts → HSE Manager takes a seat
        for u in candidates:
            if u.id not in selected and u.id not in prev_members:
                selected.append(u.id)
                if len(selected) == 3:
                    break

    rotation = KaizenCommitteeRotation(plantId=plant_id, periodMonth=period, memberUserIds=selected, hseManagerSeat=hse_seat)
    db.add(rotation)
    await db.flush()
    return rotation


# ── KWP-01 pre-screen ─────────────────────────────────────────────────
_CATEGORY_HINTS = [
    ("UNSAFE_CONDITION", ("spill", "leak", "exposed", "broken", "damaged", "missing guard", "wet floor", "trip", "obstruct")),
    ("UNSAFE_ACT", ("without ppe", "no helmet", "no harness", "bypass", "shortcut", "running", "smoking")),
    ("NEAR_MISS", ("almost", "nearly", "close call", "near miss", "could have")),
    ("GOOD_PRACTICE", ("good", "well done", "best practice", "exemplary")),
    ("IMPROVEMENT_SUGGESTION", ("suggest", "improve", "could", "propose", "recommend")),
]


async def prescreen_post(db: AsyncSession, post: KaizenPost) -> dict:
    """Mutates `post` with AI pre-screen results. Returns the result dict."""
    desc = post.description or ""
    flagged = any(w in desc.lower() for w in _PROFANITY)

    # Duplicate detection vs approved posts at this plant in the last 30 days.
    cutoff = (post.createdAt or datetime.now(timezone.utc)) - timedelta(days=30)
    recent = (
        await db.execute(
            select(KaizenPost).where(KaizenPost.plantId == post.plantId).where(KaizenPost.status == "APPROVED").where(KaizenPost.createdAt >= cutoff)
        )
    ).scalars().all()
    my_tokens = _tokens(desc)
    dup_id = None
    for r in recent:
        if r.id == post.id:
            continue
        rt = _tokens(r.description)
        if not my_tokens or not rt:
            continue
        overlap = len(my_tokens & rt) / len(my_tokens | rt)
        if overlap >= 0.5:
            dup_id = r.id
            break

    # Category suggestion.
    suggestion = post.category
    low = desc.lower()
    for cat, hints in _CATEGORY_HINTS:
        if any(h in low for h in hints):
            suggestion = cat
            break

    post.aiDuplicateFlag = dup_id is not None
    post.aiLinkedTransactionId = dup_id
    post.aiCategorySuggestion = suggestion
    if flagged:
        post.aiFlagReason = "Contains profanity / non-safety content"
        post.status = "FLAGGED"
    return {"flagged": flagged, "duplicateOf": dup_id, "categorySuggestion": suggestion}


# ── LDA-01 lessons distribution ───────────────────────────────────────
async def distribute_lessons(db: AsyncSession, post: KaizenPost, *, now: datetime) -> dict:
    """High-scoring approved posts distribute cross-plant with a one-time +15
    bonus to the submitter. Idempotent (loop/duplicate prevention)."""
    if post.crossPlantDistributed:
        return {"distributed": False, "reason": "already distributed"}
    # Composite is sum of 3 dims (3..15); avg dim > 3.5 ⇒ composite > 10.5.
    if (post.finalCommitteeScore or 0) <= 10.5:
        return {"distributed": False, "reason": "below relevance threshold"}
    other_plants = (
        await db.execute(select(distinct(Plant.id)).where(Plant.id != post.plantId))
    ).scalars().all()
    if not other_plants:
        return {"distributed": False, "reason": "no other plants"}

    post.crossPlantDistributed = True
    db.add(SciLedgerEntry(
        userId=post.submitterUserId, plantId=post.plantId, sourceModule="KAIZEN_WALL",
        sourceTransactionId=f"{post.id}:lda-bonus", eventType="Cross-Plant Lessons Distributed",
        basePoints=15, multiplier=1.0, finalPoints=15, scoringPeriod=now.strftime("%Y-%m"),
        auditTrail=[{"at": now.isoformat(), "by": "LDA-01", "action": "AWARDED", "plants": len(other_plants)}],
    ))
    return {"distributed": True, "plants": len(other_plants), "bonus": 15}
