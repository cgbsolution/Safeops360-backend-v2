"""Kaizen Wall router (SCI-01 §7-8). Mounts at /api/sci/kaizen.

Full governed flow: submit → KWP-01 pre-screen → assigned 3-member committee
→ 2-of-3 quorum review (submitter identity withheld) → approve/decline. Approved
posts award SCI points (submitter + each reviewer) and run LDA-01 cross-plant
distribution. Committee members can't post during their rotation. Declined posts
are retained. Voiding a source transaction writes a compensating negative entry.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.kaizen import KaizenCommitteeRotation, KaizenPost
from app.models.sci import SciLedgerEntry
from app.models.user import User
from app.services.kaizen_governance import distribute_lessons, get_or_assign_committee, prescreen_post

router = APIRouter(prefix="/api/sci/kaizen", tags=["kaizen"])

_CATEGORIES = {"UNSAFE_ACT", "UNSAFE_CONDITION", "NEAR_MISS", "GOOD_PRACTICE", "IMPROVEMENT_SUGGESTION"}
_SEVERITY = {"LOW", "MEDIUM", "HIGH"}
_SEVERITY_MULT = {"LOW": 1.0, "MEDIUM": 1.5, "HIGH": 2.3}


class PostCreate(BaseModel):
    plantId: str
    category: str
    hazardSeveritySelf: str = "MEDIUM"
    description: str = Field(min_length=5, max_length=500)
    locationTag: str | None = None
    photoUrl: str | None = None
    isAnonymous: bool = False


class ReviewIn(BaseModel):
    hazardSig: int = Field(ge=1, le=5)
    learningVal: int = Field(ge=1, le=5)
    actionQual: int = Field(ge=1, le=5)
    decision: str  # APPROVE | DECLINE
    feedback: str | None = None


async def _award_once(db: AsyncSession, *, user_id: str, plant_id: str, module: str, txn: str, event: str, base: int, mult: float, when: datetime) -> None:
    exists = (
        await db.execute(
            select(SciLedgerEntry.id).where(SciLedgerEntry.userId == user_id).where(SciLedgerEntry.sourceModule == module).where(SciLedgerEntry.sourceTransactionId == txn)
        )
    ).first()
    if exists:
        return
    db.add(SciLedgerEntry(
        userId=user_id, plantId=plant_id, sourceModule=module, sourceTransactionId=txn, eventType=event,
        basePoints=base, multiplier=mult, finalPoints=round(base * mult), scoringPeriod=when.strftime("%Y-%m"),
        auditTrail=[{"at": when.isoformat(), "by": "SYSTEM", "action": "AWARDED", "event": event}],
    ))


async def _active_committee(db: AsyncSession, plant_id: str) -> KaizenCommitteeRotation:
    return await get_or_assign_committee(db, plant_id)


@router.get("/committee")
async def committee(plantId: str = Query(...), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    rot = await _active_committee(db, plantId)
    await db.commit()
    members = {
        u.id: u.name
        for u in ((await db.execute(select(User).where(User.id.in_(rot.memberUserIds)))).scalars().all() if rot.memberUserIds else [])
    }
    return {
        "period": rot.periodMonth,
        "members": [{"userId": uid, "name": members.get(uid, uid)} for uid in rot.memberUserIds],
        "hseManagerSeat": rot.hseManagerSeat,
        "iAmMember": user.id in rot.memberUserIds,
    }


@router.post("/posts", status_code=status.HTTP_201_CREATED)
async def submit_post(payload: PostCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    if payload.category not in _CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid category")
    rot = await _active_committee(db, payload.plantId)
    if user.id in rot.memberUserIds:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Committee members cannot submit wall posts during their rotation month.")

    sev = payload.hazardSeveritySelf if payload.hazardSeveritySelf in _SEVERITY else "MEDIUM"
    post = KaizenPost(
        submitterUserId=user.id, isAnonymous=payload.isAnonymous, plantId=payload.plantId, category=payload.category,
        hazardSeveritySelf=sev, photoUrl=payload.photoUrl, description=payload.description.strip(),
        locationTag=payload.locationTag, status="PENDING_AI_SCREEN",
    )
    db.add(post)
    await db.flush()

    # KWP-01 pre-screen.
    screen = await prescreen_post(db, post)
    if post.status != "FLAGGED":
        post.status = "PENDING_COMMITTEE"
        post.committeeRotationId = rot.id
    await db.commit()
    return {"id": post.id, "status": post.status, "prescreen": screen}


@router.get("/wall")
async def wall(plantId: str = Query(...), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    posts = (await db.execute(select(KaizenPost).where(KaizenPost.plantId == plantId).where(KaizenPost.status == "APPROVED").order_by(KaizenPost.approvedAt.desc()))).scalars().all()
    names = {u.id: u.name for u in ((await db.execute(select(User).where(User.id.in_([p.submitterUserId for p in posts if not p.isAnonymous])))).scalars().all() if posts else [])}
    return {
        "plantId": plantId,
        "posts": [
            {
                "id": p.id, "category": p.category, "hazardSeveritySelf": p.hazardSeveritySelf, "description": p.description,
                "locationTag": p.locationTag, "photoUrl": p.photoUrl, "submitter": "Anonymous" if p.isAnonymous else names.get(p.submitterUserId, "—"),
                "finalCommitteeScore": p.finalCommitteeScore, "pointsAwarded": p.pointsAwardedSubmitter, "reactionsCount": p.reactionsCount,
                "crossPlantDistributed": p.crossPlantDistributed, "approvedAt": p.approvedAt.isoformat() if p.approvedAt else None,
            }
            for p in posts
        ],
    }


@router.get("/my-posts")
async def my_posts(plantId: str = Query(...), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    posts = (await db.execute(select(KaizenPost).where(KaizenPost.submitterUserId == user.id).where(KaizenPost.plantId == plantId).order_by(KaizenPost.createdAt.desc()))).scalars().all()
    return {
        "posts": [
            {
                "id": p.id, "category": p.category, "hazardSeveritySelf": p.hazardSeveritySelf, "description": p.description, "status": p.status,
                "isAnonymous": p.isAnonymous, "pointsAwarded": p.pointsAwardedSubmitter, "declineFeedback": p.declineFeedback,
                "aiFlagReason": p.aiFlagReason, "aiDuplicateFlag": p.aiDuplicateFlag, "crossPlantDistributed": p.crossPlantDistributed,
                "createdAt": p.createdAt.isoformat() if p.createdAt else None,
            }
            for p in posts
        ],
    }


@router.get("/review-queue")
async def review_queue(plantId: str = Query(...), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """Committee view — only the assigned members see it; submitter identity withheld."""
    rot = await _active_committee(db, plantId)
    await db.commit()
    if user.id not in rot.memberUserIds:
        return {"posts": [], "onCommittee": False}
    posts = (
        await db.execute(
            select(KaizenPost).where(KaizenPost.plantId == plantId).where(KaizenPost.status == "PENDING_COMMITTEE")
            .where(KaizenPost.submitterUserId != user.id).order_by(KaizenPost.createdAt.asc())
        )
    ).scalars().all()
    out = []
    for p in posts:
        reviewed = any((s.get("reviewerId") == user.id) for s in (p.committeeScoresJson or []))
        out.append({
            # submitterUserId / name deliberately omitted.
            "id": p.id, "category": p.category, "hazardSeveritySelf": p.hazardSeveritySelf, "description": p.description,
            "locationTag": p.locationTag, "photoUrl": p.photoUrl, "aiCategorySuggestion": p.aiCategorySuggestion,
            "aiDuplicateFlag": p.aiDuplicateFlag, "reviewsSoFar": len(p.committeeScoresJson or []), "alreadyReviewed": reviewed,
            "createdAt": p.createdAt.isoformat() if p.createdAt else None,
        })
    return {"posts": out, "onCommittee": True}


@router.post("/posts/{post_id}/review")
async def review_post(post_id: str, payload: ReviewIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    post = await db.get(KaizenPost, post_id)
    if post is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Post not found")
    if post.status != "PENDING_COMMITTEE":
        raise HTTPException(status.HTTP_409_CONFLICT, "Post is no longer pending review")
    if post.submitterUserId == user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot review your own submission")

    rot = await db.get(KaizenCommitteeRotation, post.committeeRotationId) if post.committeeRotationId else None
    if rot is None or user.id not in rot.memberUserIds:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You are not on this month's Kaizen committee")

    scores = list(post.committeeScoresJson or [])
    if any(s.get("reviewerId") == user.id for s in scores):
        raise HTTPException(status.HTTP_409_CONFLICT, "You have already reviewed this post")

    now = datetime.now(timezone.utc)
    scores.append({
        "reviewerId": user.id, "hazardSig": payload.hazardSig, "learningVal": payload.learningVal,
        "actionQual": payload.actionQual, "decision": payload.decision, "feedback": payload.feedback, "at": now.isoformat(),
    })
    post.committeeScoresJson = scores
    post.reviewedByUserId = user.id
    post.reviewedAt = now

    approvals = sum(1 for s in scores if s["decision"] == "APPROVE")
    declines = sum(1 for s in scores if s["decision"] == "DECLINE")
    result: dict = {"id": post.id, "status": post.status, "reviewsSoFar": len(scores), "quorum": 2}

    # 2-of-3 majority quorum.
    if approvals >= 2:
        composites = [s["hazardSig"] + s["learningVal"] + s["actionQual"] for s in scores]
        avg_composite = sum(composites) / len(composites)
        mult = _SEVERITY_MULT.get(post.hazardSeveritySelf, 1.5)
        points = max(10, min(35, round(avg_composite * mult)))
        post.status = "APPROVED"
        post.approvedAt = now
        post.finalCommitteeScore = round(avg_composite, 2)
        post.pointsAwardedSubmitter = points
        await _award_once(db, user_id=post.submitterUserId, plant_id=post.plantId, module="KAIZEN_WALL", txn=post.id, event="Kaizen Wall Post Approved", base=points, mult=1.0, when=now)
        for s in scores:
            await _award_once(db, user_id=s["reviewerId"], plant_id=post.plantId, module="KAIZEN_COMMITTEE", txn=post.id, event="Kaizen Committee Review", base=5, mult=1.0, when=now)
        dist = await distribute_lessons(db, post, now=now)
        result.update({"status": "APPROVED", "pointsAwarded": points, "finalCommitteeScore": post.finalCommitteeScore, "distribution": dist})
    elif declines >= 2:
        fb = "; ".join(s["feedback"] for s in scores if s["decision"] == "DECLINE" and s.get("feedback")) or "Did not meet the committee's hazard/learning/action criteria."
        post.status = "DECLINED"
        post.declineFeedback = fb
        result.update({"status": "DECLINED"})
    else:
        result.update({"status": "PENDING_COMMITTEE", "note": "awaiting quorum"})

    await db.commit()
    return result


@router.post("/posts/{post_id}/react")
async def react(post_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    post = await db.get(KaizenPost, post_id)
    if post is None or post.status != "APPROVED":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Post not found")
    post.reactionsCount = (post.reactionsCount or 0) + 1
    await db.commit()
    return {"id": post.id, "reactionsCount": post.reactionsCount}


@router.post("/void-ledger/{entry_id}")
async def void_ledger(entry_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """Void a ledger entry (e.g. its source transaction was cancelled). Writes a
    compensating NEGATIVE entry so net points = 0. Never deletes (§4.1.9)."""
    e = await db.get(SciLedgerEntry, entry_id)
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ledger entry not found")
    if e.isVoided:
        raise HTTPException(status.HTTP_409_CONFLICT, "Entry already voided")
    now = datetime.now(timezone.utc)
    comp = SciLedgerEntry(
        userId=e.userId, plantId=e.plantId, sourceModule=e.sourceModule, sourceTransactionId=f"{e.sourceTransactionId}:void",
        eventType=f"VOID — {e.eventType}", basePoints=-e.basePoints, multiplier=e.multiplier, finalPoints=-e.finalPoints,
        isVoided=True, scoringPeriod=now.strftime("%Y-%m"),
        auditTrail=[{"at": now.isoformat(), "by": user.id, "action": "COMPENSATING_VOID", "of": e.id}],
    )
    db.add(comp)
    await db.flush()
    e.isVoided = True
    e.voidCompensatingEntryId = comp.id
    await db.commit()
    return {"voidedEntryId": e.id, "compensatingEntryId": comp.id, "netPoints": 0}
