"""Assurance integrity router — independence, competence, meetings, integrity.

Implements Part 2 of [docs/cams/09-module-completion.md](../../../docs/cams/09-module-completion.md).

Permission codes (existing CAMS set — no new codes, deliberately):
  CAMS.READ            read verdicts, two-hat summaries, meeting records, errata
  CAMS.SCHEDULE        pre-flight an assignment; record meetings
  CAMS.CLOSE           grant/revoke an independence waiver; reopen; add erratum
  CAMS.TYPE_CONFIG     maintain discipline ownership

Reusing the CAMS codes means a tenant that has already granted CAMS rights does
not need an RBAC migration to get these surfaces, and the seeds stay untouched.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.assurance import DisciplineOwner, IndependenceEvent, IndependenceWaiver
from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.cams import CamsEngagement
from app.models.user import User
from app.services import assurance as asvc
from app.services import independence as ind
from app.services import independence_events as inde
from app.services import regimes as regimes_svc
from app.services import signoff as signoff_svc
from app.services.permissions import PermissionContext, can
from app.services.plant_directory import resolve_plant_names

router = APIRouter(prefix="/api/assurance", tags=["assurance"])


async def _require(db: AsyncSession, user: User, code: str, *, plant_id=None) -> None:
    res = await can(db, user.id, code, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission {code}")


async def _require_signoff_access(
    db: AsyncSession, user: User, audit: ComplianceAudit
) -> None:
    """Who may record or withdraw a sign-off on this audit.

    Entitlement comes from the audit's own team first: if you are named on it,
    you may sign the seat you hold (which seat, `signoff.record_signoff`
    enforces). Only someone NOT on the team - a CAMS admin, an external
    observer - falls through to the module-level CAMS.EXECUTE grant.

    This is deliberately not `CAMS.EXECUTE` for everyone. AUDITEE is a
    read-only role in CAMS so that an auditee can never conduct an audit, but
    the auditee owner is one of the two signatures that gate closure. Requiring
    EXECUTE here made that signature unobtainable and left every audit stuck
    short of Closed.
    """
    if signoff_svc.is_named_on_audit(audit, user.id):
        return
    await _require(db, user, "CAMS.EXECUTE", plant_id=audit.plantId)


async def _scope(
    db: AsyncSession, kind: str, engagement_id: str
) -> tuple[ind.EngagementScope, str | None]:
    """Resolve an engagement of either kind to the normalised scope + its site."""
    kind = (kind or "").upper()
    if kind == "AUDIT":
        audit = await db.get(ComplianceAudit, engagement_id)
        if audit is None or audit.isDeleted:
            raise HTTPException(404, "Audit not found")
        return await ind.scope_for_audit(db, audit), audit.plantId
    if kind == "INSPECTION":
        eng = await db.get(CamsEngagement, engagement_id)
        if eng is None or eng.isDeleted:
            raise HTTPException(404, "Engagement not found")
        return ind.scope_for_engagement(eng), eng.siteId
    raise HTTPException(400, "engagementKind must be AUDIT or INSPECTION")


# ─────────────────────────────────────────────────────────────────────
# §2.1 — independence
# ─────────────────────────────────────────────────────────────────────


class PreflightBody(BaseModel):
    """Pre-flight an assignment before committing to it.

    This is what the team-assignment step of the scheduling wizard calls, and
    what the candidate picker calls for its whole visible list, so a conflict is
    shown inline next to the person rather than as a submit-time failure that
    costs the user the whole form.

    **Why there is no separate `/preflight-batch`.** This body already carries
    `userIds: list[str]` and the full prospective scope, and `check_many` already
    takes a candidate list — a second endpoint would have an identical request
    shape, an identical response shape and identical semantics. The only real
    difference between "check the two people I picked" and "check all 59
    candidates" is whether the call is an ATTEMPT worth recording in the
    enforcement log. That is one field, not one endpoint.
    """

    engagementKind: Literal["AUDIT", "INSPECTION"] = "AUDIT"
    engagementId: str | None = None
    userIds: list[str] = Field(default_factory=list)
    assigningAs: Literal["AUDITOR", "AUDITEE"] = "AUDITOR"
    # ATTEMPT — someone selected these people and is heading for submit. Blocks
    #           and warnings are recorded; that is the evidence the Independence
    #           Register is built from.
    # SURVEY  — the picker asking "who is eligible?" about everyone visible.
    #           Nobody has attempted anything, so nothing is recorded. Logging
    #           these would write ~8 "blocked attempts" against people nobody
    #           chose, every time a scheduler changed a discipline checkbox, and
    #           the register's central claim — that a blocked attempt is real
    #           evidence of enforcement — would stop being true.
    mode: Literal["ATTEMPT", "SURVEY"] = "ATTEMPT"
    # For an engagement that does not exist yet (the create path), the caller
    # supplies the prospective scope directly.
    siteId: str | None = None
    disciplineCodes: list[str] = Field(default_factory=list)
    areaIds: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    leadAuditorId: str | None = None
    teamAuditorIds: list[str] = Field(default_factory=list)
    auditeeUserIds: list[str] = Field(default_factory=list)
    # WP-45 — set by the scheduling wizard when the subject is a supplier, so
    # the relationship-owner conflict is caught in the picker rather than at
    # submit. For an existing engagement it is derived from the link instead.
    vendorProfileId: str | None = None


@router.post("/independence/preflight")
async def independence_preflight(
    body: PreflightBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if body.engagementId:
        scope, plant_id = await _scope(db, body.engagementKind, body.engagementId)
    else:
        scope = ind.EngagementScope(
            kind=body.engagementKind,
            id=None,
            siteId=body.siteId,
            disciplineCodes=body.disciplineCodes,
            areaIds=body.areaIds,
            departments=body.departments,
            leadAuditorId=body.leadAuditorId,
            teamAuditorIds=body.teamAuditorIds,
            auditeeUserIds=body.auditeeUserIds,
            vendorProfileId=body.vendorProfileId,
        )
        plant_id = body.siteId
    await _require(db, user, "CAMS.SCHEDULE", plant_id=plant_id)

    verdicts = await ind.check_many(
        db, user_ids=body.userIds, scope=scope, assigning_as=body.assigningAs
    )
    # An attempt is evidence even when it never becomes an engagement — in fact
    # especially then. Deduplicated inside the service, because this endpoint
    # fires on every render of the team-assignment step. A SURVEY is not an
    # attempt and writes nothing (see `mode`).
    if body.mode == "ATTEMPT":
        await inde.record_verdicts(
            verdicts=verdicts,
            engagement_kind=body.engagementKind,
            origin="PREFLIGHT",
            attempted_by_user_id=user.id,
            engagement_id=body.engagementId,
            site_id=plant_id,
        )
    names = {}
    if body.userIds:
        rows = (
            await db.execute(select(User.id, User.name).where(User.id.in_(body.userIds)))
        ).all()
        names = {r[0]: r[1] for r in rows}
    return {
        "results": [
            {"userId": uid, "userName": names.get(uid), **v.as_dict()}
            for uid, v in verdicts.items()
        ],
        "blockedCount": sum(1 for v in verdicts.values() if v.blocking and not v.waived),
    }


@router.get("/independence/two-hat/{user_id}")
async def two_hat(
    user_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Rule 3 made visible — every engagement this person touches, and which hat.

    The screen to open when a client asks whether the same person can audit here
    and be audited there. The answer is a list, not an assertion.
    """
    await _require(db, user, "CAMS.READ")
    summary = await ind.two_hat_summary(db, user_id=user_id)
    subject = await db.get(User, user_id)
    summary["userName"] = subject.name if subject else user_id
    summary["designation"] = subject.designation if subject else None
    return summary


# Sort key for the dual-role register. A genuine cross-engagement dual role on
# two OPEN engagements is the row a certification body asks about; standing
# ownership with no engagement attached is reference material. Sorting
# alphabetically would bury the first behind the second.
_OPEN_AUDIT = ("scheduled", "in_progress", "submitted_pending_response",
               "response_in_progress", "under_review")
_OPEN_INSPECTION = ("PLANNED", "SCHEDULED", "IN_PROGRESS", "FIELDWORK_COMPLETE",
                    "FINDINGS_REVIEW")


def _is_open(row: dict[str, Any]) -> bool:
    st = row.get("status") or ""
    return st in _OPEN_AUDIT or st in _OPEN_INSPECTION


@router.get("/independence/register")
async def independence_register(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Section 1 — everyone carrying a dual role or an ownership of record.

    Computed once, server-side, from `resolve_ownership_sources` — the same
    resolution `check_assignment` applies its rules to. That is the point: this
    screen used to run a narrower query of its own and told the truth about
    fewer people than the guard did.

    This is a register, not a lookup. The old screen made you already know whose
    name to type, which is the one thing a reader of an impartiality register
    does not know.
    """
    await _require(db, user, "CAMS.READ")
    resolved = await ind.resolve_ownership_sources(db, include_auditor_roles=True)

    rows: list[dict[str, Any]] = []
    for uid, sources in resolved.items():
        s = ind.summarise_two_hats(sources)
        if not s["asAuditor"] and not s["asAuditee"] and not s["ownershipOfRecord"]:
            continue
        open_auditor = [r for r in s["asAuditor"] if _is_open(r)]
        open_auditee = [r for r in s["asAuditee"] if _is_open(r)]
        # Rank 0 is the row worth opening the screen for: auditor on one live
        # engagement, auditee on a DIFFERENT live one.
        cross_open = bool(open_auditor and open_auditee) and bool(
            {r["engagementId"] for r in open_auditor}
            ^ {r["engagementId"] for r in open_auditee}
        )
        if cross_open:
            rank, status = 0, "DUAL_ROLE_OPEN"
        elif s["wearsBothHats"]:
            rank, status = 1, "DUAL_ROLE"
        else:
            rank, status = 2, "OWNER_OF_RECORD"
        rows.append({**s, "rank": rank, "status": status,
                     "openAuditorCount": len(open_auditor),
                     "openAuditeeCount": len(open_auditee)})

    waived = {
        w.subjectUserId
        for w in (
            await db.execute(
                select(IndependenceWaiver).where(IndependenceWaiver.revokedAt.is_(None))
            )
        ).scalars().all()
    }
    for r in rows:
        if r["userId"] in waived:
            r["status"] = "WAIVED"

    ids = [r["userId"] for r in rows]
    people = {}
    if ids:
        people = {
            u.id: {"name": u.name, "designation": u.designation, "plantId": u.plantId}
            for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()
        }
    for r in rows:
        p = people.get(r["userId"]) or {}
        r["userName"] = p.get("name")
        r["designation"] = p.get("designation")
        r["homePlantId"] = p.get("plantId")

    rows.sort(key=lambda r: (r["rank"], -(r["auditorCount"] + r["auditeeCount"]),
                             r["userName"] or r["userId"]))
    return {
        "items": rows,
        "total": len(rows),
        "dualRoleOpenCount": sum(1 for r in rows if r["rank"] == 0),
        "dualRoleCount": sum(1 for r in rows if r["rank"] <= 1),
        "ownerOfRecordCount": sum(1 for r in rows if r["rank"] == 2),
    }


@router.get("/independence/events")
async def independence_events(
    outcome: str | None = Query(None),
    subjectUserId: str | None = Query(None),
    limit: int = Query(200, le=1000),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Section 2 — every verdict the guard reached, waived or not.

    `IndependenceEvent LEFT JOIN IndependenceWaiver`: rule fired → who attempted
    it → approver and justification if it was waived → the engagement it belongs
    to. A blocked attempt that was never overridden is the strongest evidence in
    here, which is why BLOCKED rows are not hidden once they are resolved.
    """
    await _require(db, user, "CAMS.READ")
    q = select(IndependenceEvent).order_by(IndependenceEvent.occurredAt.desc())
    if outcome:
        q = q.where(IndependenceEvent.outcome == outcome.upper())
    if subjectUserId:
        q = q.where(IndependenceEvent.subjectUserId == subjectUserId)
    events = list((await db.execute(q.limit(limit))).scalars().all())

    waiver_ids = [e.waiverId for e in events if e.waiverId]
    waivers = {}
    if waiver_ids:
        waivers = {
            w.id: w
            for w in (
                await db.execute(
                    select(IndependenceWaiver).where(IndependenceWaiver.id.in_(waiver_ids))
                )
            ).scalars().all()
        }
    uids = {
        u for e in events
        for u in (e.subjectUserId, e.attemptedByUserId)
        if u
    } | {w.approvedByUserId for w in waivers.values()}
    names = {}
    if uids:
        names = {
            u.id: u.name
            for u in (await db.execute(select(User).where(User.id.in_(list(uids))))).scalars().all()
        }

    items = []
    for e in events:
        w = waivers.get(e.waiverId) if e.waiverId else None
        items.append({
            "id": e.id,
            "occurredAt": e.occurredAt.isoformat() if e.occurredAt else None,
            "outcome": e.outcome,
            "origin": e.origin,
            "rule": e.rule,
            "source": e.source,
            "reason": e.reason,
            "siteId": e.siteId,
            "engagementKind": e.engagementKind,
            "engagementId": e.engagementId,
            "engagementCode": e.engagementCode,
            "subjectUserId": e.subjectUserId,
            "subjectUserName": names.get(e.subjectUserId),
            "attemptedByUserId": e.attemptedByUserId,
            "attemptedByUserName": names.get(e.attemptedByUserId),
            "conflictDetail": e.conflictDetail,
            "waiver": (
                {
                    "id": w.id,
                    "justification": w.justification,
                    "approvedByUserId": w.approvedByUserId,
                    "approvedByUserName": names.get(w.approvedByUserId),
                    "approvedAt": w.approvedAt.isoformat() if w.approvedAt else None,
                    "revokedAt": w.revokedAt.isoformat() if w.revokedAt else None,
                }
                if w
                else None
            ),
        })

    counts: dict[str, int] = {}
    for e in events:
        counts[e.outcome] = counts.get(e.outcome, 0) + 1
    return {
        "items": items,
        "total": len(items),
        "counts": counts,
        # The headline claim: blocks that stood, versus blocks that were
        # overridden. Both are governance; only one is enforcement.
        "blockedStanding": counts.get("BLOCKED", 0),
        "waivedCount": counts.get("WAIVED", 0),
    }


class WaiverBody(BaseModel):
    engagementKind: Literal["AUDIT", "INSPECTION"] = "AUDIT"
    engagementId: str
    subjectUserId: str
    ruleViolated: Literal["OWN_WORK", "SAME_ENGAGEMENT_DUAL_ROLE"] = "OWN_WORK"
    justification: str = Field(min_length=20)
    approvedByUserId: str
    scope: Literal["ENGAGEMENT", "CHECKPOINT_SET"] = "ENGAGEMENT"
    checkpointCodes: list[str] = Field(default_factory=list)


@router.post("/independence/waivers", status_code=status.HTTP_201_CREATED)
async def create_waiver(
    body: WaiverBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Grant a governed independence waiver.

    ISO 19011 acknowledges proportionality — small sites sometimes cannot staff
    full independence. What matters is that the exception is visible, so this
    requires a justification, a named approver who is not the subject, and it is
    rendered in the engagement's report.
    """
    scope, plant_id = await _scope(db, body.engagementKind, body.engagementId)
    await _require(db, user, "CAMS.CLOSE", plant_id=plant_id)

    if not ind.segregation_ok(body.approvedByUserId, body.subjectUserId):
        raise HTTPException(400, "A waiver cannot be approved by the person it exempts.")

    verdict = await ind.check_assignment(
        db, user_id=body.subjectUserId, scope=scope, assigning_as="AUDITOR"
    )
    if not verdict.blocking:
        raise HTTPException(
            400, "No blocking independence conflict exists for this person on this engagement."
        )

    row = IndependenceWaiver(
        engagementKind=body.engagementKind,
        engagementId=body.engagementId,
        subjectUserId=body.subjectUserId,
        ruleViolated=body.ruleViolated,
        conflictDetail=verdict.blocking[0].as_dict(),
        justification=body.justification.strip(),
        approvedByUserId=body.approvedByUserId,
        scope=body.scope,
        checkpointCodes=body.checkpointCodes,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    # The override, on the same timeline as the block it overrides. A register
    # showing only waivers would show every exception and no enforcement.
    await inde.record_event(
        subject_user_id=body.subjectUserId,
        engagement_kind=body.engagementKind,
        engagement_id=body.engagementId,
        site_id=plant_id,
        outcome="WAIVED",
        origin="WAIVER_GRANT",
        attempted_by_user_id=user.id,
        rule=body.ruleViolated,
        source=verdict.blocking[0].source,
        reason=body.justification.strip(),
        conflict_detail=row.conflictDetail,
        waiver_id=row.id,
        dedupe=False,
    )
    return {"id": row.id, "ok": True, "conflict": row.conflictDetail}


@router.delete("/independence/waivers/{waiver_id}")
async def revoke_waiver(
    waiver_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.CLOSE")
    row = await db.get(IndependenceWaiver, waiver_id)
    if row is None:
        raise HTTPException(404, "Waiver not found")
    row.revokedAt = asvc._utcnow()
    row.revokedByUserId = user.id
    await db.commit()
    # Revocation restores the block, so it is a BLOCKED event, not a deletion.
    # Nothing in this table is ever updated or removed — the waiver row keeps its
    # `revokedAt`, and the register reads both.
    await inde.record_event(
        subject_user_id=row.subjectUserId,
        engagement_kind=row.engagementKind,
        engagement_id=row.engagementId,
        outcome="BLOCKED",
        origin="WAIVER_REVOKE",
        attempted_by_user_id=user.id,
        rule=row.ruleViolated,
        source=(row.conflictDetail or {}).get("source"),
        reason="The independence waiver was revoked; the original conflict applies again.",
        conflict_detail=row.conflictDetail,
        waiver_id=row.id,
        dedupe=False,
    )
    return {"ok": True}


@router.get("/independence/waivers")
async def list_waivers(
    engagementKind: str = Query("AUDIT"),
    engagementId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.READ")
    return await asvc.waiver_block_for(
        db, engagement_kind=engagementKind.upper(), engagement_id=engagementId
    )


# ─────────────────────────────────────────────────────────────────────
# Q17 — discipline ownership
# ─────────────────────────────────────────────────────────────────────


class DisciplineOwnerBody(BaseModel):
    plantId: str | None = None
    disciplineCode: str
    disciplineLabel: str = ""
    ownerUserId: str
    ownershipType: Literal["ACCOUNTABLE", "RESPONSIBLE"] = "ACCOUNTABLE"


@router.get("/discipline-owners")
async def list_discipline_owners(
    plantId: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.READ", plant_id=plantId)
    q = select(DisciplineOwner).where(DisciplineOwner.isActive.is_(True))
    if plantId:
        q = q.where(
            (DisciplineOwner.plantId == plantId) | (DisciplineOwner.plantId.is_(None))
        )
    rows = list((await db.execute(q)).scalars().all())
    names = await asvc._user_names(db, [r.ownerUserId for r in rows])
    # The scope chip on the ownership register printed `plantId` — a cuid where
    # the reader needs to know WHICH site this ownership covers.
    plants = await resolve_plant_names(db, [r.plantId for r in rows])
    return {
        "items": [
            {
                "id": r.id,
                "plantId": r.plantId,
                "plantName": plants.get(r.plantId) if r.plantId else None,
                "disciplineCode": r.disciplineCode,
                "disciplineLabel": r.disciplineLabel,
                "ownerUserId": r.ownerUserId,
                "ownerName": names.get(r.ownerUserId),
                "ownershipType": r.ownershipType,
                "estateWide": r.plantId is None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.post("/discipline-owners", status_code=status.HTTP_201_CREATED)
async def upsert_discipline_owner(
    body: DisciplineOwnerBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.TYPE_CONFIG", plant_id=body.plantId)
    existing = (
        await db.execute(
            select(DisciplineOwner).where(
                DisciplineOwner.plantId.is_(None)
                if body.plantId is None
                else DisciplineOwner.plantId == body.plantId,
                DisciplineOwner.disciplineCode == body.disciplineCode,
                DisciplineOwner.ownerUserId == body.ownerUserId,
            )
        )
    ).scalars().first()
    if existing is not None:
        existing.isActive = True
        existing.ownershipType = body.ownershipType
        existing.disciplineLabel = body.disciplineLabel or existing.disciplineLabel
        await db.commit()
        return {"id": existing.id, "ok": True, "created": False}

    row = DisciplineOwner(
        plantId=body.plantId,
        disciplineCode=body.disciplineCode,
        disciplineLabel=body.disciplineLabel,
        ownerUserId=body.ownerUserId,
        ownershipType=body.ownershipType,
        createdBy=user.id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"id": row.id, "ok": True, "created": True}


@router.delete("/discipline-owners/{owner_id}")
async def deactivate_discipline_owner(
    owner_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.TYPE_CONFIG")
    row = await db.get(DisciplineOwner, owner_id)
    if row is None:
        raise HTTPException(404, "Not found")
    row.isActive = False
    await db.commit()
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────
# §2.2 — competence
# ─────────────────────────────────────────────────────────────────────


@router.get("/competence/check")
async def competence_check(
    userId: str = Query(...),
    auditTypeId: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.READ")
    return await asvc.check_competence(db, user_id=userId, audit_type_id=auditTypeId)


@router.get("/competence/snapshots")
async def competence_snapshots(
    engagementKind: str = Query("AUDIT"),
    engagementId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.READ")
    items = await asvc.competence_snapshots_for(
        db, engagement_kind=engagementKind.upper(), engagement_id=engagementId
    )
    return {"items": items, "total": len(items)}


# ─────────────────────────────────────────────────────────────────────
# §2.3 — meetings
# ─────────────────────────────────────────────────────────────────────


class MeetingBody(BaseModel):
    engagementKind: Literal["AUDIT", "INSPECTION"] = "AUDIT"
    engagementId: str
    meetingType: Literal["OPENING", "CLOSING"]
    heldAt: str | None = None
    attendees: list[dict[str, Any]] = Field(default_factory=list)
    scopeConfirmed: bool = False
    findingsSummaryPresented: str | None = None
    auditeeAcknowledged: bool = False
    auditeeAcknowledgedByUserId: str | None = None
    notes: str | None = None
    # Carry these attendees onto the engagement's calendar bookings. Default
    # False so an API caller that has not thought about it cannot mail invitations
    # by omission; the form sends True, because a recorder who has just typed a
    # list of people wants those people on the closing invite.
    addToCalendar: bool = False


@router.post("/meetings")
async def record_meeting(
    body: MeetingBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _, plant_id = await _scope(db, body.engagementKind, body.engagementId)
    await _require(db, user, "CAMS.SCHEDULE", plant_id=plant_id)
    try:
        row = await asvc.upsert_meeting(
            db,
            engagement_kind=body.engagementKind,
            engagement_id=body.engagementId,
            meeting_type=body.meetingType,
            payload=body.model_dump(),
            user=user,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    # The minute is now part of the calendar's desired state, so recompute it.
    # Unconditional rather than only when addToCalendar is on: turning the flag
    # OFF has to withdraw the people it previously added, and that only happens
    # if the sync runs on the way down as well as on the way up.
    #
    # Best-effort by contract (`sync_engagement` never raises) — a mail server
    # having a bad afternoon must not lose the meeting record.
    from app.services import calendar_booking as _cal

    cal = await _cal.sync_engagement(
        db,
        engagement_kind=body.engagementKind,
        engagement_id=body.engagementId,
        actor_id=user.id,
    )
    await db.commit()
    return {"id": row.id, "ok": True, "calendar": cal}


@router.get("/meetings")
async def get_meetings(
    engagementKind: str = Query("AUDIT"),
    engagementId: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.READ")
    return await asvc.meetings_for(
        db, engagement_kind=engagementKind.upper(), engagement_id=engagementId
    )


# ─────────────────────────────────────────────────────────────────────
# §3.7 — buyer-regime support (WP-47, Q7/Q19)
# ─────────────────────────────────────────────────────────────────────


@router.get("/regimes")
async def list_regimes(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """The buyer-regime structures the engine can render.

    Each carries `authored: "SafeOps360"` and a disclaimer: these are the
    engineering SHAPE of each regime (severity taxonomy, result scale, section
    structure), not the regime owner's licensed measurement criteria.
    """
    await _require(db, user, "CAMS.READ")
    items = regimes_svc.list_regimes()
    return {"items": items, "total": len(items),
            "disclaimer": regimes_svc.AUTHORSHIP_DISCLAIMER}


@router.get("/regimes/{regime_code}/readiness")
async def regime_readiness(
    regime_code: str,
    auditId: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """"Are we SMETA-ready?" — which regime sections the scope covers.

    Section-name matching only, and the response says so: it answers what has
    been SCOPED against the regime, not whether the facility would pass it.
    """
    await _require(db, user, "CAMS.READ")
    disciplines: list[str] = []
    if auditId:
        audit = await db.get(ComplianceAudit, auditId)
        if audit is None or audit.isDeleted:
            raise HTTPException(404, "Audit not found")
        rows = (
            await db.execute(
                select(AuditCheckpointResponse.categoryName)
                .where(AuditCheckpointResponse.auditId == auditId)
                .distinct()
            )
        ).all()
        disciplines = [r[0] for r in rows if r[0]]
    out = regimes_svc.regime_ready(regime_code, disciplines)
    if not out.get("known"):
        raise HTTPException(404, f"Unknown regime {regime_code}")
    return out


# ─────────────────────────────────────────────────────────────────────
# §3.1 — sign-off & signature capture (WP-41)
# ─────────────────────────────────────────────────────────────────────


class SignOffBody(BaseModel):
    role: Literal[
        "LEAD_AUDITOR", "AUDITEE_OWNER", "DISCIPLINE_AUDITOR",
        "PLANT_MANAGER", "EXTERNAL_OBSERVER",
    ]
    signatureKind: Literal["DRAWN", "TYPED"] = "DRAWN"
    # PNG data URI for DRAWN; ignored for TYPED.
    signaturePayload: str | None = None
    typedName: str | None = None
    disciplineCode: str | None = None
    statement: str | None = None


@router.get("/audits/{audit_id}/signoff")
async def get_signoff(
    audit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """What is signed, what is outstanding, and whether closure is permitted."""
    audit = await db.get(ComplianceAudit, audit_id)
    if audit is None or audit.isDeleted:
        raise HTTPException(404, "Audit not found")
    await _require(db, user, "CAMS.READ", plant_id=audit.plantId)
    return await signoff_svc.signoff_status(db, audit)


@router.post("/audits/{audit_id}/signoff")
async def create_signoff(
    audit_id: str,
    body: SignOffBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Record a sign-off. The signer is always the authenticated user — a
    signature recorded in someone else's name is worse than none."""
    audit = await db.get(ComplianceAudit, audit_id)
    if audit is None or audit.isDeleted:
        raise HTTPException(404, "Audit not found")
    await _require_signoff_access(db, user, audit)
    try:
        out = await signoff_svc.record_signoff(
            db,
            audit=audit,
            user=user,
            role=body.role,
            signature_kind=body.signatureKind,
            signature_payload=body.signaturePayload,
            typed_name=body.typedName,
            discipline_code=body.disciplineCode,
            statement=body.statement,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.commit()
    return out


@router.delete("/audits/{audit_id}/signoff")
async def delete_signoff(
    audit_id: str,
    role: str = Query(...),
    disciplineCode: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Withdraw your own sign-off. You cannot withdraw someone else's."""
    audit = await db.get(ComplianceAudit, audit_id)
    if audit is None or audit.isDeleted:
        raise HTTPException(404, "Audit not found")
    await _require_signoff_access(db, user, audit)
    try:
        out = await signoff_svc.revoke_signoff(
            db, audit=audit, user=user, role=role, discipline_code=disciplineCode
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.commit()
    return out


# ─────────────────────────────────────────────────────────────────────
# §2.5 — report integrity
# ─────────────────────────────────────────────────────────────────────


@router.get("/reports/{report_id}/verify")
async def verify_report(
    report_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Recompute the snapshot hash. Answers "has this record changed since it
    was issued?" with something better than a stored string nobody checks."""
    await _require(db, user, "CAMS.READ")
    try:
        return await asvc.verify_report_integrity(db, report_id=report_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


class ReopenBody(BaseModel):
    reason: str = Field(min_length=10)
    approvedByUserId: str


@router.post("/audits/{audit_id}/reopen")
async def reopen(
    audit_id: str,
    body: ReopenBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    audit = await db.get(ComplianceAudit, audit_id)
    if audit is None or audit.isDeleted:
        raise HTTPException(404, "Audit not found")
    await _require(db, user, "CAMS.CLOSE", plant_id=audit.plantId)
    try:
        out = await asvc.reopen_audit(
            db,
            user=user,
            audit_id=audit_id,
            reason=body.reason,
            approver_id=body.approvedByUserId,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.commit()
    return out


class ErratumBody(BaseModel):
    text: str = Field(min_length=10)
    approvedByUserId: str


@router.post("/reports/{report_id}/errata", status_code=status.HTTP_201_CREATED)
async def add_erratum(
    report_id: str,
    body: ErratumBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Correct an issued report without touching its snapshot or its hash."""
    await _require(db, user, "CAMS.CLOSE")
    try:
        row = await asvc.add_erratum(
            db,
            report_id=report_id,
            text_body=body.text,
            raised_by=user.id,
            approved_by=body.approvedByUserId,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.commit()
    return {"id": row.id, "sequence": row.sequence, "ok": True}


@router.get("/reports/{report_id}/errata")
async def list_errata(
    report_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "CAMS.READ")
    items = await asvc.errata_for(db, report_id=report_id)
    return {"items": items, "total": len(items)}
