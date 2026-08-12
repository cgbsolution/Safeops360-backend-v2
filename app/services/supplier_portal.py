"""Minimal supplier portal — issue, resolve, read, submit.

Scope is deliberately one audit per token. Everything the portal returns is
derived from the token row, never from a parameter the caller supplies, so
there is no path by which a valid token reaches a second audit's data. That is
the security property worth stating plainly, because it is the one an external
surface is judged on.

Explicitly NOT here (these are the full Stage-2 portal, per the build spec):
supplier login, cross-audit history, supplier-initiated scheduling, supplier
dashboards.
"""

from __future__ import annotations

import hashlib
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.cams_completion import SupplierAuditLink
from app.models.supplier_portal import (
    SupplierPortalAccessLog,
    SupplierPortalSubmission,
    SupplierPortalToken,
)

DEFAULT_TTL_DAYS = 30
MAX_TTL_DAYS = 180

# Rate limit per (token prefix, ip). Reads are cheap, writes are not.
_READ_LIMIT = 60          # per window
_WRITE_LIMIT = 20         # per window
_WINDOW_SECONDS = 300     # 5 minutes

# In-process sliding window. Deliberately not Redis: this surface sees a
# handful of suppliers, and adding infrastructure for it would be the more
# expensive mistake. The honest limitation — with N uvicorn workers the
# effective ceiling is N× these numbers — is why `SupplierPortalAccessLog` is
# the real forensic record and every attempt is written to it.
_hits: dict[tuple[str, str, bool], list[float]] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def hash_token(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def _prefix(raw: str) -> str:
    return (raw or "")[:8]


def _rate_limited(prefix: str, ip: str | None, *, is_write: bool) -> bool:
    key = (prefix, ip or "-", is_write)
    now = time.monotonic()
    window = _hits.setdefault(key, [])
    cutoff = now - _WINDOW_SECONDS
    # Trim in place so the dict does not grow without bound for a chatty caller.
    while window and window[0] < cutoff:
        window.pop(0)
    limit = _WRITE_LIMIT if is_write else _READ_LIMIT
    if len(window) >= limit:
        return True
    window.append(now)
    return False


async def _log(
    db: AsyncSession,
    *,
    outcome: str,
    action: str,
    is_write: bool,
    token: SupplierPortalToken | None = None,
    prefix: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    db.add(
        SupplierPortalAccessLog(
            tokenId=token.id if token else None,
            tokenPrefix=(token.tokenPrefix if token else prefix),
            auditId=token.auditId if token else None,
            outcome=outcome,
            action=action,
            ipAddress=(ip or "")[:64] or None,
            userAgent=(user_agent or "")[:400] or None,
            isWrite=is_write,
        )
    )
    await db.flush()


# ─────────────────────────────────────────────────────────────────────
# Issue / revoke
# ─────────────────────────────────────────────────────────────────────


@dataclass
class IssuedToken:
    token: SupplierPortalToken
    rawToken: str
    portalPath: str


#: Who an external link can be issued to. Not interchangeable — see the model.
PORTAL_ROLES = ("SUPPLIER_MANAGER", "CO_AUDITOR", "AUDITEE")


async def issue_token(
    db: AsyncSession,
    *,
    audit_id: str,
    contact_email: str,
    contact_name: str | None = None,
    role: str = "SUPPLIER_MANAGER",
    discipline_codes: list[str] | None = None,
    vendor_profile_id: str | None = None,
    actor_id: str | None = None,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> IssuedToken:
    """Mint a token for one PERSON on one audit, revoking their previous one.

    Re-issuing revokes the predecessor rather than running two live credentials
    for the same person: two valid tokens means revoking the leaked one does not
    actually close access.

    That rule used to be scoped to the whole AUDIT, which made a second
    recipient impossible — issuing a co-auditor's link silently killed the
    supplier manager's. It is now scoped to (audit, email, role), which is the
    narrowest key that still guarantees one live credential per person. The
    partial unique index enforces the same thing at the database, so a caller
    that forgets cannot create a second live link behind our back.
    """
    role = (role or "SUPPLIER_MANAGER").upper()
    if role not in PORTAL_ROLES:
        raise ValueError(f"Unknown portal role '{role}'. Expected one of {', '.join(PORTAL_ROLES)}.")
    email = (contact_email or "").strip()
    if not email:
        raise ValueError("A portal token needs an email address — it is the holder's identity.")

    ttl = max(1, min(int(ttl_days or DEFAULT_TTL_DAYS), MAX_TTL_DAYS))
    now = _utcnow()

    for prior in (
        await db.execute(
            select(SupplierPortalToken).where(
                SupplierPortalToken.auditId == audit_id,
                func.lower(SupplierPortalToken.supplierContactEmail) == email.lower(),
                SupplierPortalToken.role == role,
                SupplierPortalToken.revokedAt.is_(None),
            )
        )
    ).scalars().all():
        prior.revokedAt = now
        prior.revokedById = actor_id
        prior.revokedReason = "Superseded by a newly issued token for the same recipient"

    raw = secrets.token_urlsafe(32)
    row = SupplierPortalToken(
        engagementKind="AUDIT",
        auditId=audit_id,
        vendorProfileId=vendor_profile_id,
        tokenHash=hash_token(raw),
        tokenPrefix=_prefix(raw),
        supplierContactEmail=email,
        supplierContactName=contact_name,
        role=role,
        disciplineCodes=list(discipline_codes or []),
        expiresAt=now + timedelta(days=ttl),
        createdById=actor_id,
    )
    db.add(row)
    await db.flush()
    return IssuedToken(token=row, rawToken=raw, portalPath=f"/supplier/{raw}")


async def issue_tokens(
    db: AsyncSession,
    *,
    audit_id: str,
    recipients: list[dict[str, Any]],
    vendor_profile_id: str | None = None,
    actor_id: str | None = None,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> list[IssuedToken]:
    """Issue one link per recipient, skipping duplicates within the batch.

    `recipients` are `{email, name?, role?, disciplineCodes?}`. De-duplicated on
    (email, role) BEFORE issuing, because the same pair twice in one batch would
    otherwise have the second revoke the first and return a link the caller then
    reports alongside a dead one.

    A bad address fails that recipient only. Scheduling an audit must not be lost
    because someone mistyped one co-auditor's email, and a partially-issued batch
    is visible in the result — the caller reports which links went out.
    """
    out: list[IssuedToken] = []
    seen: set[tuple[str, str]] = set()
    for r in recipients or []:
        email = (r.get("email") or "").strip()
        role = (r.get("role") or "SUPPLIER_MANAGER").upper()
        if not email:
            continue
        key = (email.lower(), role)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            await issue_token(
                db,
                audit_id=audit_id,
                contact_email=email,
                contact_name=r.get("name"),
                role=role,
                discipline_codes=r.get("disciplineCodes"),
                vendor_profile_id=vendor_profile_id,
                actor_id=actor_id,
                ttl_days=ttl_days,
            )
        )
    return out


async def revoke_token(
    db: AsyncSession, *, token_id: str, actor_id: str | None = None, reason: str = ""
) -> bool:
    row = await db.get(SupplierPortalToken, token_id)
    if row is None or row.revokedAt is not None:
        return False
    row.revokedAt = _utcnow()
    row.revokedById = actor_id
    row.revokedReason = reason or "Revoked by an internal user"
    await db.flush()
    return True


# ─────────────────────────────────────────────────────────────────────
# Resolve — the single gate every portal request passes through
# ─────────────────────────────────────────────────────────────────────


class PortalError(Exception):
    """Carries the HTTP status the router should return.

    Every failure mode answers with the same generic message. Distinguishing
    "expired" from "never existed" to an anonymous caller is free reconnaissance
    for anyone probing tokens; the precise outcome goes to the access log where
    it is useful to us and invisible to them.
    """

    def __init__(self, status_code: int, outcome: str):
        self.status_code = status_code
        self.outcome = outcome
        super().__init__(outcome)


async def resolve_token(
    db: AsyncSession,
    raw_token: str,
    *,
    action: str,
    is_write: bool = False,
    ip: str | None = None,
    user_agent: str | None = None,
) -> SupplierPortalToken:
    """Token -> the row, or raise. Logs every attempt, successful or not."""
    prefix = _prefix(raw_token or "")

    if _rate_limited(prefix, ip, is_write=is_write):
        await _log(db, outcome="RATE_LIMITED", action=action, is_write=is_write,
                   prefix=prefix, ip=ip, user_agent=user_agent)
        raise PortalError(429, "RATE_LIMITED")

    row = (
        await db.execute(
            select(SupplierPortalToken).where(
                SupplierPortalToken.tokenHash == hash_token(raw_token or "")
            )
        )
    ).scalars().first()

    if row is None:
        await _log(db, outcome="NOT_FOUND", action=action, is_write=is_write,
                   prefix=prefix, ip=ip, user_agent=user_agent)
        raise PortalError(404, "NOT_FOUND")

    if row.revokedAt is not None:
        await _log(db, outcome="REVOKED", action=action, is_write=is_write,
                   token=row, ip=ip, user_agent=user_agent)
        raise PortalError(404, "REVOKED")

    if _aware(row.expiresAt) is not None and _aware(row.expiresAt) < _utcnow():
        await _log(db, outcome="EXPIRED", action=action, is_write=is_write,
                   token=row, ip=ip, user_agent=user_agent)
        raise PortalError(404, "EXPIRED")

    row.lastAccessedAt = _utcnow()
    row.accessCount = (row.accessCount or 0) + 1
    await _log(db, outcome="OK", action=action, is_write=is_write,
               token=row, ip=ip, user_agent=user_agent)
    return row


# ─────────────────────────────────────────────────────────────────────
# Read — what the supplier is allowed to see
# ─────────────────────────────────────────────────────────────────────

# Fields on a checkpoint that are INTERNAL and must never cross the boundary:
#   auditorNote            — the auditor's private note, distinct from the
#                            factual `observation` the supplier is answering.
#   plantManagerReview     — internal review commentary.
#   interactions           — the internal multi-round thread.
#   assigned*/routedTo*    — internal user ids.
#   auditorEvidenceIds     — our evidence, not theirs.
# The allow-list below is therefore explicit rather than a deny-list: a field
# added to the model later is invisible to the portal until someone chooses to
# expose it, which is the correct default for an external surface.


def _finding_for_supplier(r: AuditCheckpointResponse) -> dict[str, Any]:
    capa = r.capa or {}
    return {
        "id": r.id,
        "checkpointCode": r.checkpointCode,
        "question": r.checkpointQuestion,
        "discipline": r.categoryName,
        "criticality": r.criticality,
        "requirementReference": r.requirementReference,
        "standard": r.standard,
        "assessmentStatus": r.assessmentStatus,
        # The auditor's factual observation of the non-conformance — this IS
        # the thing the supplier is being asked to correct.
        "observation": r.observation or "",
        "capaNumber": capa.get("capa_number"),
        "capaStatus": capa.get("capa_status"),
        "capaDueDate": capa.get("capa_due_date"),
        "capaId": capa.get("capa_id"),
    }


async def portal_view(db: AsyncSession, token: SupplierPortalToken) -> dict[str, Any]:
    """Everything the portal page renders, scoped to the token's audit."""
    audit = await db.get(ComplianceAudit, token.auditId)
    if audit is None or audit.isDeleted:
        raise PortalError(404, "AUDIT_MISSING")

    rows = (
        await db.execute(
            select(AuditCheckpointResponse)
            .where(AuditCheckpointResponse.auditId == audit.id)
            .order_by(AuditCheckpointResponse.categoryId, AuditCheckpointResponse.sequence)
        )
    ).scalars().all()

    # Only non-conformances. A supplier does not need — and should not receive —
    # the full checkpoint register of an audit of their factory; they need the
    # things they have to fix.
    findings = [
        _finding_for_supplier(r)
        for r in rows
        if (r.auditorResponse or {}).get("value") in ("fail", "partial")
        or r.assessmentStatus in ("FAIL", "PARTIAL")
    ]

    subs = (
        await db.execute(
            select(SupplierPortalSubmission)
            .where(SupplierPortalSubmission.auditId == audit.id)
            .order_by(SupplierPortalSubmission.submittedAt.desc())
        )
    ).scalars().all()

    by_finding: dict[str, list[dict[str, Any]]] = {}
    for s in subs:
        by_finding.setdefault(s.checkpointResponseId or "", []).append({
            "id": s.id,
            "kind": s.kind,
            "body": s.body,
            "fileName": s.fileName,
            "submittedAt": _aware(s.submittedAt).isoformat() if s.submittedAt else None,
            "submittedByName": s.submittedByName,
            "acknowledged": s.acknowledgedAt is not None,
        })

    link = (
        await db.execute(
            select(SupplierAuditLink).where(
                SupplierAuditLink.engagementKind == "AUDIT",
                SupplierAuditLink.engagementId == audit.id,
            )
        )
    ).scalars().first()

    return {
        "audit": {
            "auditNumber": audit.auditNumber,
            "title": audit.title,
            "status": audit.status,
            "scheduledDate": _aware(audit.scheduledDate).isoformat() if audit.scheduledDate else None,
            "closedAt": _aware(audit.closedAt).isoformat() if audit.closedAt else None,
            "overallCompliancePct": audit.overallCompliancePct,
            "criticalFailureCount": audit.criticalFailureCount,
        },
        "supplier": {
            "legalName": None,  # filled by the router via the vendor boundary
            "contactName": token.supplierContactName,
            "contactEmail": token.supplierContactEmail,
            "vendorSiteRef": link.vendorSiteRef if link else None,
        },
        "findings": findings,
        "findingCount": len(findings),
        "submissions": by_finding,
        "expiresAt": _aware(token.expiresAt).isoformat() if token.expiresAt else None,
    }


# ─────────────────────────────────────────────────────────────────────
# Write — comment + evidence
# ─────────────────────────────────────────────────────────────────────


async def _validated_finding(
    db: AsyncSession, token: SupplierPortalToken, checkpoint_response_id: str
) -> AuditCheckpointResponse:
    """A submission may only target a checkpoint on the token's OWN audit.

    Checked against `auditId` from the token row rather than trusting the id in
    the request — without this a valid token could write onto any checkpoint in
    the database by guessing an id.
    """
    r = await db.get(AuditCheckpointResponse, checkpoint_response_id)
    if r is None or r.auditId != token.auditId:
        raise PortalError(404, "NOT_FOUND")
    return r


async def add_comment(
    db: AsyncSession,
    token: SupplierPortalToken,
    *,
    checkpoint_response_id: str,
    body: str,
) -> SupplierPortalSubmission:
    text = (body or "").strip()
    if not text:
        raise PortalError(400, "EMPTY_COMMENT")
    r = await _validated_finding(db, token, checkpoint_response_id)
    capa = r.capa or {}
    sub = SupplierPortalSubmission(
        tokenId=token.id,
        auditId=token.auditId,
        kind="COMMENT",
        checkpointResponseId=r.id,
        capaId=capa.get("capa_id"),
        body=text[:8000],
        submittedByEmail=token.supplierContactEmail,
        submittedByName=token.supplierContactName,
    )
    db.add(sub)
    await db.flush()
    return sub


async def add_evidence(
    db: AsyncSession,
    token: SupplierPortalToken,
    *,
    checkpoint_response_id: str,
    file_name: str,
    storage_path: str,
    file_size: int | None = None,
    mime_type: str | None = None,
    caption: str = "",
) -> SupplierPortalSubmission:
    r = await _validated_finding(db, token, checkpoint_response_id)
    capa = r.capa or {}
    sub = SupplierPortalSubmission(
        tokenId=token.id,
        auditId=token.auditId,
        kind="EVIDENCE",
        checkpointResponseId=r.id,
        capaId=capa.get("capa_id"),
        body=(caption or "")[:2000],
        fileName=file_name[:200],
        storagePath=storage_path,
        fileSize=file_size,
        mimeType=mime_type,
        submittedByEmail=token.supplierContactEmail,
        submittedByName=token.supplierContactName,
    )
    db.add(sub)
    await db.flush()
    return sub


# ─────────────────────────────────────────────────────────────────────
# Internal-side reads
# ─────────────────────────────────────────────────────────────────────


def _no_channel(note: str, **extra: Any) -> dict[str, Any]:
    return {"responseChannel": "OUT_OF_BAND", "responseChannelNote": note, **extra}


async def channel_for_engagement(
    db: AsyncSession, *, engagement_kind: str, engagement_id: str
) -> dict[str, Any]:
    """Does this engagement actually have a live supplier response channel?

    Read, not assumed. Telling an internal user "the supplier can respond" when
    no token was ever issued — or when it expired last month — is exactly the
    kind of confident-and-wrong status this module is supposed to avoid.

    **Never raises.** The portal is an OPTIONAL layer over the supplier link,
    and this is called from `get_audit` — the audit detail screen. When the
    portal tables were not yet created, the `UndefinedTableError` propagated out
    of `get_audit`, the page's `backendFetch(...).catch(() => null)` turned it
    into `notFound()`, and **every supplier audit 404'd**. An optional feature
    must not be able to take down the record it decorates, so a failure here
    degrades to "no portal" exactly like the not-yet-issued case.

    The query runs inside a SAVEPOINT because a failed statement aborts the
    enclosing asyncpg transaction — without one, catching the error would leave
    the caller with a poisoned session and the next query would fail anyway.
    """
    if (engagement_kind or "").upper() != "AUDIT":
        return _no_channel(
            "Inspections have no supplier portal. Findings are communicated to "
            "the contact above and recorded by the internal owner."
        )

    try:
        async with db.begin_nested():
            return await _channel_for_audit(db, engagement_id)
    except Exception as e:  # noqa: BLE001
        print(f"Supplier portal channel lookup failed for {engagement_id}: {e}", file=sys.stderr)
        return _no_channel(
            "Supplier portal status is unavailable — findings are communicated to "
            "the contact above and recorded by the internal owner.",
            portalUnavailable=True,
        )


async def _channel_for_audit(db: AsyncSession, engagement_id: str) -> dict[str, Any]:
    row = (
        await db.execute(
            select(SupplierPortalToken)
            .where(
                SupplierPortalToken.auditId == engagement_id,
                SupplierPortalToken.revokedAt.is_(None),
            )
            .order_by(SupplierPortalToken.createdAt.desc())
        )
    ).scalars().first()

    if row is None:
        return _no_channel(
            "No supplier portal access has been issued for this audit. Findings "
            "are communicated to the contact above and the corrective action is "
            "recorded by the internal owner on the supplier's behalf.",
            portalTokenIssued=False,
        )

    expired = _aware(row.expiresAt) is not None and _aware(row.expiresAt) < _utcnow()
    n_subs = (
        await db.execute(
            select(func.count(SupplierPortalSubmission.id)).where(
                SupplierPortalSubmission.auditId == engagement_id
            )
        )
    ).scalar_one()

    if expired:
        return _no_channel(
            f"Supplier portal access expired on "
            f"{_aware(row.expiresAt).date().isoformat()}. Re-issue it to let the "
            "supplier respond directly.",
            portalTokenIssued=True,
            portalExpired=True,
            portalSubmissionCount=int(n_subs),
        )

    return {
        "responseChannel": "PORTAL",
        "responseChannelNote": (
            f"The supplier can respond directly until "
            f"{_aware(row.expiresAt).date().isoformat()}."
        ),
        "portalTokenIssued": True,
        "portalExpired": False,
        "portalTokenId": row.id,
        "portalExpiresAt": _aware(row.expiresAt).isoformat(),
        "portalContactEmail": row.supplierContactEmail,
        "portalLastAccessedAt": (
            _aware(row.lastAccessedAt).isoformat() if row.lastAccessedAt else None
        ),
        "portalSubmissionCount": int(n_subs),
    }


async def submissions_for_audit(
    db: AsyncSession, audit_id: str
) -> list[dict[str, Any]]:
    """External submissions, for the internal CAPA/finding view.

    Every row carries `actorType: "SUPPLIER"` so the internal UI can render it
    distinctly without inferring anything from the absence of a user id.
    """
    rows = (
        await db.execute(
            select(SupplierPortalSubmission)
            .where(SupplierPortalSubmission.auditId == audit_id)
            .order_by(SupplierPortalSubmission.submittedAt.desc())
        )
    ).scalars().all()
    return [
        {
            "id": s.id,
            "actorType": "SUPPLIER",
            "kind": s.kind,
            "checkpointResponseId": s.checkpointResponseId,
            "capaId": s.capaId,
            "body": s.body,
            "fileName": s.fileName,
            "storagePath": s.storagePath,
            "mimeType": s.mimeType,
            "fileSize": s.fileSize,
            "submittedByName": s.submittedByName,
            "submittedByEmail": s.submittedByEmail,
            "submittedAt": _aware(s.submittedAt).isoformat() if s.submittedAt else None,
            "acknowledgedAt": (
                _aware(s.acknowledgedAt).isoformat() if s.acknowledgedAt else None
            ),
            "acknowledgedById": s.acknowledgedById,
        }
        for s in rows
    ]


__all__ = [
    "DEFAULT_TTL_DAYS",
    "IssuedToken",
    "PortalError",
    "add_comment",
    "add_evidence",
    "channel_for_engagement",
    "hash_token",
    "issue_token",
    "portal_view",
    "resolve_token",
    "revoke_token",
    "submissions_for_audit",
]
