"""Assurance integrity services — competence, meetings, report integrity.

Companion to `app.services.independence` (§2.1). Together these implement Part 2
of [docs/cams/09-module-completion.md](../../../docs/cams/09-module-completion.md).

  §2.2  competence linkage      — check_competence / capture_competence_snapshot
  §2.3  meeting records         — upsert_meeting / meetings_for
  §2.5  report integrity        — verify_report_integrity / reopen_audit / add_erratum

The through-line is that each of these turns something the report *asserted*
into something the product can *evidence*. A report that says a closing meeting
happened, or that an auditor was qualified, should be reading a row — not a
template string.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assurance import (
    EngagementCompetenceSnapshot,
    EngagementMeeting,
    IndependenceWaiver,
    ReportErratum,
)
from app.models.audit_compliance import (
    AuditCheckpointResponse,
    AuditReport,
    CheckpointInteraction,
    ComplianceAudit,
)
from app.models.cams import CamsAuditType, CamsEngagement
from app.models.competency_matrix import Competency, CompetencyRecord
from app.models.user import User

# A competency expiring inside this window warns even though it is still held —
# an auditor whose certification lapses mid-engagement is a finding waiting to
# happen.
EXPIRY_WARNING_DAYS = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """Postgres TIMESTAMPTZ round-trips as aware, but SQLite (tests) does not."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────
# §2.2 — competence
# ─────────────────────────────────────────────────────────────────────


async def required_competencies(
    db: AsyncSession, *, audit_type_id: str | None
) -> list[Competency]:
    """Resolve `CamsAuditType.requiresAuditorCompetency` to real rows.

    That column has existed since the CAMS build and has never been read by any
    guard — the admin UI even hard-coded it to `[]`. It accepts either competency
    ids or competency codes, because both are natural things for an administrator
    to type and neither was validated before.

    `audit_type_id` may be a `CamsAuditType.id` **or** a `typeCode`: the audit
    engine stores a bare `auditType` string on `ComplianceAudit` while the
    inspection engine carries a real `auditTypeId`, and this is the one place
    that difference has to be absorbed rather than pushed onto callers.
    """
    if not audit_type_id:
        return []
    at = await db.get(CamsAuditType, audit_type_id)
    if at is None:
        at = (
            await db.execute(
                select(CamsAuditType).where(
                    CamsAuditType.typeCode == audit_type_id,
                    CamsAuditType.isDeleted.is_(False),
                )
            )
        ).scalars().first()
    if at is None:
        return []
    keys = [k for k in (at.requiresAuditorCompetency or []) if k]
    if not keys:
        return []
    q = select(Competency).where(
        Competency.isActive.is_(True),
        (Competency.id.in_(keys)) | (Competency.code.in_(keys)),
    )
    return list((await db.execute(q)).scalars().all())


async def check_competence(
    db: AsyncSession, *, user_id: str, audit_type_id: str | None
) -> dict[str, Any]:
    """Does this user hold what the audit type requires?

    Returns `{ok, missing[], expiring[], held[]}` rather than raising, so the
    caller decides between warn and block per audit type. `validUntil` in the
    past counts as **missing**, not held — a lapsed certificate is not a
    certificate.
    """
    required = await required_competencies(db, audit_type_id=audit_type_id)
    if not required:
        return {"ok": True, "missing": [], "expiring": [], "held": [], "required": 0}

    by_id = {c.id: c for c in required}
    recs = (
        await db.execute(
            select(CompetencyRecord).where(
                CompetencyRecord.personUserId == user_id,
                CompetencyRecord.competencyId.in_(list(by_id)),
            )
        )
    ).scalars().all()
    rec_by_comp = {r.competencyId: r for r in recs}

    now = _utcnow()
    soon = now + timedelta(days=EXPIRY_WARNING_DAYS)
    missing: list[dict[str, Any]] = []
    expiring: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []

    for comp in required:
        rec = rec_by_comp.get(comp.id)
        entry = {"competencyId": comp.id, "code": comp.code, "name": comp.name}
        valid_until = _aware(rec.validUntil) if rec else None
        is_held = bool(
            rec
            and rec.state in ("validated", "competent", "current")
            and (valid_until is None or valid_until >= now)
        )
        if not is_held:
            missing.append(
                {
                    **entry,
                    "reason": (
                        "no competency record"
                        if rec is None
                        else ("expired" if valid_until and valid_until < now else f"state={rec.state}")
                    ),
                }
            )
            continue
        held.append({**entry, "validUntil": valid_until.isoformat() if valid_until else None})
        if valid_until and valid_until <= soon:
            expiring.append({**entry, "validUntil": valid_until.isoformat()})

    return {
        "ok": not missing,
        "missing": missing,
        "expiring": expiring,
        "held": held,
        "required": len(required),
        "summary": (
            ""
            if not missing
            else (
                f"Missing {', '.join(m['name'] for m in missing[:3])}"
                + (f" + {len(missing) - 3} more" if len(missing) > 3 else "")
                + " (required by this audit type)"
            )
        ),
    }


async def capture_competence_snapshot(
    db: AsyncSession,
    *,
    engagement_kind: str,
    engagement_id: str,
    user_id: str,
    audit_type_id: str | None,
    captured_by: str | None = None,
    waived_gap: bool = False,
) -> list[EngagementCompetenceSnapshot]:
    """Freeze what the Skill Matrix says right now.

    Same principle as the template snapshot: a live read cannot answer "was this
    person qualified when the audit was conducted?" after a revalidation. Replaces
    any prior snapshot for this (engagement, user) so re-assignment is idempotent.
    """
    required = await required_competencies(db, audit_type_id=audit_type_id)
    if not required:
        return []

    existing = (
        await db.execute(
            select(EngagementCompetenceSnapshot).where(
                EngagementCompetenceSnapshot.engagementKind == engagement_kind,
                EngagementCompetenceSnapshot.engagementId == engagement_id,
                EngagementCompetenceSnapshot.userId == user_id,
            )
        )
    ).scalars().all()
    for row in existing:
        await db.delete(row)

    recs = (
        await db.execute(
            select(CompetencyRecord).where(
                CompetencyRecord.personUserId == user_id,
                CompetencyRecord.competencyId.in_([c.id for c in required]),
            )
        )
    ).scalars().all()
    rec_by_comp = {r.competencyId: r for r in recs}

    now = _utcnow()
    out: list[EngagementCompetenceSnapshot] = []
    for comp in required:
        rec = rec_by_comp.get(comp.id)
        valid_until = _aware(rec.validUntil) if rec else None
        snap = EngagementCompetenceSnapshot(
            engagementKind=engagement_kind,
            engagementId=engagement_id,
            userId=user_id,
            competencyId=comp.id,
            competencyCode=comp.code,
            competencyName=comp.name,
            state=rec.state if rec else None,
            validUntil=rec.validUntil if rec else None,
            externalCertificateReference=(rec.externalCertificateReference if rec else None),
            held=bool(
                rec
                and rec.state in ("validated", "competent", "current")
                and (valid_until is None or valid_until >= now)
            ),
            waivedGap=waived_gap,
            capturedByUserId=captured_by,
        )
        db.add(snap)
        out.append(snap)
    return out


async def competence_snapshots_for(
    db: AsyncSession, *, engagement_kind: str, engagement_id: str
) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(EngagementCompetenceSnapshot)
            .where(
                EngagementCompetenceSnapshot.engagementKind == engagement_kind,
                EngagementCompetenceSnapshot.engagementId == engagement_id,
            )
            .order_by(EngagementCompetenceSnapshot.userId)
        )
    ).scalars().all()
    names = await _user_names(db, [r.userId for r in rows])
    return [
        {
            "userId": r.userId,
            "userName": names.get(r.userId),
            "competencyCode": r.competencyCode,
            "competencyName": r.competencyName,
            "state": r.state,
            "held": r.held,
            "waivedGap": r.waivedGap,
            "validUntil": r.validUntil.isoformat() if r.validUntil else None,
            "externalCertificateReference": r.externalCertificateReference,
            "capturedAt": r.capturedAt.isoformat() if r.capturedAt else None,
        }
        for r in rows
    ]


async def _user_names(db: AsyncSession, ids: Iterable[str | None]) -> dict[str, str]:
    clean = {i for i in ids if i}
    if not clean:
        return {}
    rows = (await db.execute(select(User.id, User.name).where(User.id.in_(clean)))).all()
    return {r[0]: r[1] for r in rows}


# ─────────────────────────────────────────────────────────────────────
# §2.3 — meeting records
# ─────────────────────────────────────────────────────────────────────


async def upsert_meeting(
    db: AsyncSession,
    *,
    engagement_kind: str,
    engagement_id: str,
    meeting_type: str,
    payload: dict[str, Any],
    user: User,
) -> EngagementMeeting:
    meeting_type = (meeting_type or "").upper()
    if meeting_type not in ("OPENING", "CLOSING"):
        raise ValueError("meetingType must be OPENING or CLOSING")

    held_at = payload.get("heldAt")
    if isinstance(held_at, str):
        held_at = datetime.fromisoformat(held_at.replace("Z", "+00:00"))
    if held_at is None:
        held_at = _utcnow()

    attendees = payload.get("attendees") or []
    if not isinstance(attendees, list) or not attendees:
        raise ValueError("At least one attendee is required — a meeting record with no attendees is not a record")

    # An external attendee's address is what makes them invitable, so it is
    # normalised here rather than trusted as typed. An entry with a malformed
    # address keeps its place in the minute and simply loses the address: the
    # record of who attended must not be rejected over a calendar nicety.
    cleaned: list[dict[str, Any]] = []
    for a in attendees:
        if not isinstance(a, dict):
            continue
        email = (a.get("email") or "").strip().lower()
        if email and ("@" not in email or email.startswith("@") or email.endswith("@")):
            email = ""
        cleaned.append({**a, "email": email} if email else {k: v for k, v in a.items() if k != "email"})
    if not cleaned:
        raise ValueError("At least one attendee is required — a meeting record with no attendees is not a record")
    attendees = cleaned

    row = (
        await db.execute(
            select(EngagementMeeting).where(
                EngagementMeeting.engagementKind == engagement_kind,
                EngagementMeeting.engagementId == engagement_id,
                EngagementMeeting.meetingType == meeting_type,
            )
        )
    ).scalars().first()

    if row is None:
        row = EngagementMeeting(
            engagementKind=engagement_kind,
            engagementId=engagement_id,
            meetingType=meeting_type,
            heldAt=held_at,
            recordedByUserId=user.id,
        )
        db.add(row)

    row.heldAt = held_at
    row.attendees = attendees
    row.addToCalendar = bool(payload.get("addToCalendar"))
    row.notes = payload.get("notes")
    if meeting_type == "OPENING":
        row.scopeConfirmed = bool(payload.get("scopeConfirmed"))
    else:
        row.findingsSummaryPresented = payload.get("findingsSummaryPresented")
        if payload.get("auditeeAcknowledged"):
            row.auditeeAcknowledgedByUserId = (
                payload.get("auditeeAcknowledgedByUserId") or user.id
            )
            row.auditeeAcknowledgedAt = _utcnow()
    await db.flush()
    return row


async def meetings_for(
    db: AsyncSession, *, engagement_kind: str, engagement_id: str
) -> dict[str, Any]:
    """Report-shaped meeting block.

    Returns an explicit `recorded: False` when a meeting is absent so the report
    can print "No closing meeting was recorded" rather than asserting one. That
    single rule is the difference between a record and boilerplate.
    """
    rows = (
        await db.execute(
            select(EngagementMeeting).where(
                EngagementMeeting.engagementKind == engagement_kind,
                EngagementMeeting.engagementId == engagement_id,
            )
        )
    ).scalars().all()
    by_type = {r.meetingType: r for r in rows}

    uids: set[str] = set()
    for r in rows:
        for a in r.attendees or []:
            if isinstance(a, dict) and a.get("userId"):
                uids.add(a["userId"])
        if r.auditeeAcknowledgedByUserId:
            uids.add(r.auditeeAcknowledgedByUserId)
    names = await _user_names(db, uids)

    def render(t: str) -> dict[str, Any]:
        r = by_type.get(t)
        if r is None:
            return {"recorded": False, "meetingType": t}
        attendees = []
        for a in r.attendees or []:
            if isinstance(a, dict) and a.get("userId"):
                attendees.append(
                    {
                        "userId": a["userId"],
                        "name": names.get(a["userId"], a.get("name") or "Unknown"),
                        "organisation": a.get("organisation") or "Internal",
                        "role": a.get("role"),
                    }
                )
            elif isinstance(a, dict):
                attendees.append(
                    {
                        "name": a.get("name") or "Unnamed attendee",
                        "organisation": a.get("organisation") or "External",
                        "email": a.get("email") or None,
                        "role": a.get("role"),
                        "external": True,
                    }
                )
        # Who the calendar could actually reach. An external with no address is
        # in the minute but not in anyone's calendar, and the form says so rather
        # than leaving the recorder to assume the invitation went out.
        unreachable = sum(
            1 for a in attendees if a.get("external") and not a.get("email")
        )
        return {
            "recorded": True,
            "meetingType": t,
            "heldAt": r.heldAt.isoformat() if r.heldAt else None,
            "attendees": attendees,
            "attendeeCount": len(attendees),
            "addToCalendar": bool(getattr(r, "addToCalendar", False)),
            "unreachableCount": unreachable,
            "scopeConfirmed": r.scopeConfirmed,
            "findingsSummaryPresented": r.findingsSummaryPresented,
            "auditeeAcknowledged": bool(r.auditeeAcknowledgedByUserId),
            "auditeeAcknowledgedBy": names.get(r.auditeeAcknowledgedByUserId or ""),
            "auditeeAcknowledgedAt": (
                r.auditeeAcknowledgedAt.isoformat() if r.auditeeAcknowledgedAt else None
            ),
            "notes": r.notes,
        }

    return {"opening": render("OPENING"), "closing": render("CLOSING")}


# ─────────────────────────────────────────────────────────────────────
# §2.5 — report integrity
# ─────────────────────────────────────────────────────────────────────


def canonical_hash(obj: Any, *, full: bool = True) -> str:
    """The one canonicalisation rule, documented in one place.

    Mirrors `audit_compliance._canonical_hash` exactly (`sort_keys=True`,
    `default=str`) so a hash produced there verifies here. `full=False` returns
    the legacy 16-char prefix that shipped inside existing snapshots.

    **Invariant that will otherwise be got wrong:** `snapshotHash` is computed
    over the snapshot and then *inserted into it*, so verification must remove
    both hash keys before rehashing. `verify_report_integrity` does that; do not
    re-implement it elsewhere.
    """
    digest = hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()
    return digest if full else digest[:16]


async def verify_report_integrity(db: AsyncSession, *, report_id: str) -> dict[str, Any]:
    """Recompute the snapshot hash and compare. Answers "has this changed?".

    Three outcomes, and the middle one matters: reports generated before the
    full-length hash landed carry only the 16-char prefix. Those verify as
    `LEGACY_TRUNCATED` — not as tampered. Reporting a pre-existing report as
    tampered because the product changed would be worse than useless.
    """
    rep = await db.get(AuditReport, report_id)
    if rep is None:
        raise ValueError("Report not found")

    snapshot = dict(rep.snapshot or {})
    stored_short = snapshot.pop("snapshotHash", None)
    snapshot.pop("snapshotHashFull", None)
    computed_full = canonical_hash(snapshot, full=True)
    computed_short = computed_full[:16]

    if rep.snapshotHashFull:
        valid = rep.snapshotHashFull == computed_full
        status = "VALID" if valid else "MISMATCH"
    elif stored_short:
        valid = stored_short == computed_short
        status = "LEGACY_TRUNCATED" if valid else "MISMATCH"
    else:
        valid = False
        status = "NO_HASH_STORED"

    return {
        "reportId": rep.id,
        "reportCode": rep.reportCode,
        "status": status,
        "valid": valid,
        "algorithm": "SHA-256 over JSON with sort_keys=True, default=str, hash keys removed",
        "storedHashFull": rep.snapshotHashFull,
        "storedHashShort": stored_short,
        "computedHashFull": computed_full,
        "generatedAt": rep.generatedAt.isoformat() if rep.generatedAt else None,
        "note": (
            "Generated before full-length hashing; the 16-character prefix matches. "
            "Regenerate the report to obtain a full-length digest."
            if status == "LEGACY_TRUNCATED"
            else ""
        ),
    }


async def reopen_audit(
    db: AsyncSession, *, user: User, audit_id: str, reason: str, approver_id: str
) -> dict[str, Any]:
    """Governed reopen — the alternative to a direct database write.

    Before this, `close_audit` was terminal and the only escape was editing the
    database, which leaves no trace *in the product*. A reopen that is logged,
    approved and counted is strictly safer than one that happens invisibly.
    """
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise ValueError("A reopen reason of at least 10 characters is required")
    if not approver_id:
        raise ValueError("A named approver is required to reopen a closed audit")

    audit = await db.get(ComplianceAudit, audit_id)
    if audit is None:
        raise ValueError("Audit not found")
    if audit.status != "closed":
        raise ValueError(f"Audit is '{audit.status}', not closed — nothing to reopen")

    now = _utcnow()
    rows = (
        await db.execute(
            select(AuditCheckpointResponse).where(AuditCheckpointResponse.auditId == audit_id)
        )
    ).scalars().all()

    # Unlock FINALIZED checkpoints back to a reviewable state and log every one.
    for r in rows:
        if r.workflowState == "FINALIZED":
            r.workflowState = "RESOLVED"
            r.finalizedAt = None
        db.add(
            CheckpointInteraction(
                checkpointInstanceId=r.id,
                auditId=audit_id,
                round=r.currentRound,
                actorId=user.id,
                actorRole="LEAD_AUDITOR" if user.id == audit.leadAuditorUserId else "AUDITOR",
                action="AUDIT_REOPENED",
                comment=reason,
                resultingState=r.workflowState,
                timestamp=now,
            )
        )

    # Every prior report becomes superseded — the column already existed.
    reports = (
        await db.execute(select(AuditReport).where(AuditReport.auditId == audit_id))
    ).scalars().all()
    for rep in reports:
        rep.isSuperseded = True

    audit.status = "under_review"
    audit.closedAt = None
    audit.reopenCount = (audit.reopenCount or 0) + 1
    audit.lastReopenedAt = now
    audit.lastReopenReason = reason
    await db.flush()

    return {
        "ok": True,
        "status": audit.status,
        "reopenCount": audit.reopenCount,
        "checkpointsUnlocked": sum(1 for r in rows if r.workflowState == "RESOLVED"),
        "reportsSuperseded": len(reports),
    }


async def add_erratum(
    db: AsyncSession, *, report_id: str, text_body: str, raised_by: str, approved_by: str
) -> ReportErratum:
    """Append a correction without disturbing the snapshot or its hash."""
    text_body = (text_body or "").strip()
    if len(text_body) < 10:
        raise ValueError("An erratum needs at least 10 characters of text")
    if not approved_by:
        raise ValueError("An erratum requires a named approver")

    rep = await db.get(AuditReport, report_id)
    if rep is None:
        raise ValueError("Report not found")

    nxt = (
        await db.execute(
            select(func.coalesce(func.max(ReportErratum.sequence), 0)).where(
                ReportErratum.reportId == report_id
            )
        )
    ).scalar_one() + 1

    row = ReportErratum(
        reportId=report_id,
        auditId=rep.auditId,
        sequence=nxt,
        text=text_body,
        raisedByUserId=raised_by,
        approvedByUserId=approved_by,
    )
    db.add(row)
    await db.flush()
    return row


async def errata_for(db: AsyncSession, *, report_id: str) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(ReportErratum)
            .where(ReportErratum.reportId == report_id)
            .order_by(ReportErratum.sequence)
        )
    ).scalars().all()
    names = await _user_names(
        db, [r.raisedByUserId for r in rows] + [r.approvedByUserId for r in rows]
    )
    return [
        {
            "id": r.id,
            "sequence": r.sequence,
            "text": r.text,
            "raisedBy": names.get(r.raisedByUserId, r.raisedByUserId),
            "approvedBy": names.get(r.approvedByUserId, r.approvedByUserId),
            "createdAt": r.createdAt.isoformat() if r.createdAt else None,
        }
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────────────
# Report block — waivers (§2.1.6)
# ─────────────────────────────────────────────────────────────────────


async def waiver_block_for(
    db: AsyncSession, *, engagement_kind: str, engagement_id: str
) -> dict[str, Any]:
    """The independence block the report renders.

    Asserts absence explicitly. A reader must be able to tell "no waivers were
    issued" from "this product does not track waivers", and only an explicit
    statement does that.
    """
    rows = (
        await db.execute(
            select(IndependenceWaiver).where(
                IndependenceWaiver.engagementKind == engagement_kind,
                IndependenceWaiver.engagementId == engagement_id,
                IndependenceWaiver.revokedAt.is_(None),
            )
        )
    ).scalars().all()
    names = await _user_names(
        db, [r.subjectUserId for r in rows] + [r.approvedByUserId for r in rows]
    )
    return {
        "count": len(rows),
        "statement": (
            "No independence waivers were issued for this engagement."
            if not rows
            else f"{len(rows)} independence waiver(s) were issued for this engagement."
        ),
        "waivers": [
            {
                "id": r.id,
                "subject": names.get(r.subjectUserId, r.subjectUserId),
                "ruleViolated": r.ruleViolated,
                "conflict": (r.conflictDetail or {}).get("reason"),
                "justification": r.justification,
                "approvedBy": names.get(r.approvedByUserId, r.approvedByUserId),
                "approvedAt": r.approvedAt.isoformat() if r.approvedAt else None,
                "scope": r.scope,
            }
            for r in rows
        ],
    }


__all__ = [
    "required_competencies",
    "check_competence",
    "capture_competence_snapshot",
    "competence_snapshots_for",
    "upsert_meeting",
    "meetings_for",
    "canonical_hash",
    "verify_report_integrity",
    "reopen_audit",
    "add_erratum",
    "errata_for",
    "waiver_block_for",
]
