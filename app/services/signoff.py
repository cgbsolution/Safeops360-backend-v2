"""WP-41 - engagement sign-off and signature capture.

docs/cams/09 §3.1.

`AuditReport.signOffs` has existed since the audit build and was never written
to: the column shipped, the report rendered "Awaiting sign-off", and nothing
could ever change that. This module is the missing half.

**Sign-off is a closure gate, not a formality.** `close_audit` already enforces
`_finalizability` (every checkpoint terminal); this adds the second condition a
certification body expects - that a named lead auditor and a named auditee owner
have each accepted the result. Without it, "closed" means only "the auditor
stopped typing".

**Signature model.** Drawn signatures are stored as a PNG data URI captured on a
touch device; where drawing is impractical the fallback is a typed name plus an
authenticated timestamp and the signer's user id. Both are legally weak on their
own and neither pretends otherwise - what makes the record defensible is that
the signer was authenticated, the time is server-side, and the payload is frozen
into the report snapshot alongside the integrity hash.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.user import User

# Roles a sign-off can be recorded against. Per-discipline sign-off uses
# DISCIPLINE_AUDITOR with a disciplineCode.
SIGNOFF_ROLES = (
    "LEAD_AUDITOR",
    "AUDITEE_OWNER",
    "DISCIPLINE_AUDITOR",
    "PLANT_MANAGER",
    "EXTERNAL_OBSERVER",
)

# The two that gate closure. Everything else is supplementary.
REQUIRED_FOR_CLOSURE = ("LEAD_AUDITOR", "AUDITEE_OWNER")

SIGNATURE_KINDS = ("DRAWN", "TYPED")

# A drawn signature is a PNG data URI. Cap it so a pathological canvas cannot
# bloat the snapshot - 256 KiB is far beyond any real signature.
MAX_SIGNATURE_BYTES = 256 * 1024


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def validate_signature(kind: str, payload: str | None, typed_name: str | None) -> None:
    """Reject a signature that would not stand up. Pure."""
    if kind not in SIGNATURE_KINDS:
        raise ValueError(f"signatureKind must be one of {', '.join(SIGNATURE_KINDS)}")
    if kind == "DRAWN":
        if not payload or not payload.startswith("data:image/"):
            raise ValueError("A drawn signature must be an image data URI.")
        if len(payload) > MAX_SIGNATURE_BYTES:
            raise ValueError("Signature image is too large.")
    else:
        if not (typed_name or "").strip():
            raise ValueError(
                "A typed signature needs the signer's full name as they would write it."
            )


async def signoff_status(db: AsyncSession, audit: ComplianceAudit) -> dict[str, Any]:
    """What is signed, what is outstanding, and whether closure is permitted.

    Read from `ComplianceAudit.signOffs` (the live working set). The report
    snapshot freezes a copy at generation; this is the mutable source.
    """
    signs = list(audit.signOffs or [])
    have = {s.get("role") for s in signs if s.get("role")}
    missing = [r for r in REQUIRED_FOR_CLOSURE if r not in have]

    # A per-discipline sign-off is expected from each auditor who actually held
    # allocated checkpoints - not from everyone merely named on the team.
    rows = (
        await db.execute(
            select(
                AuditCheckpointResponse.categoryId,
                AuditCheckpointResponse.categoryName,
                AuditCheckpointResponse.assignedAuditorId,
            ).where(AuditCheckpointResponse.auditId == audit.id)
        )
    ).all()
    per_disc: dict[str, dict[str, Any]] = {}
    for cat, cat_name, auditor in rows:
        if not cat:
            continue
        e = per_disc.setdefault(
            cat, {"disciplineCode": cat, "disciplineLabel": cat_name or cat,
                  "auditorIds": set(), "signed": False}
        )
        if auditor:
            e["auditorIds"].add(auditor)
    for s in signs:
        if s.get("role") == "DISCIPLINE_AUDITOR" and s.get("disciplineCode") in per_disc:
            per_disc[s["disciplineCode"]]["signed"] = True

    return {
        "signOffs": signs,
        "signedRoles": sorted(have),
        "missingRequiredRoles": missing,
        "canClose": not missing,
        "disciplines": [
            {**v, "auditorIds": sorted(v["auditorIds"])} for v in per_disc.values()
        ],
        "disciplinesSigned": sum(1 for v in per_disc.values() if v["signed"]),
        "disciplinesTotal": len(per_disc),
        "statement": (
            "All required sign-offs recorded."
            if not missing
            else "Awaiting sign-off from: "
            + ", ".join(r.replace("_", " ").lower() for r in missing)
        ),
    }


async def record_signoff(
    db: AsyncSession,
    *,
    audit: ComplianceAudit,
    user: User,
    role: str,
    signature_kind: str,
    signature_payload: str | None = None,
    typed_name: str | None = None,
    discipline_code: str | None = None,
    statement: str | None = None,
) -> dict[str, Any]:
    """Append a sign-off. Idempotent per (role, discipline, signer).

    Re-signing the same slot REPLACES the prior entry rather than stacking, so a
    corrected signature does not leave two conflicting records - but the audit
    trail of the replacement lives in the interaction log, not here.
    """
    role = (role or "").upper()
    if role not in SIGNOFF_ROLES:
        raise ValueError(f"role must be one of {', '.join(SIGNOFF_ROLES)}")
    if role == "DISCIPLINE_AUDITOR" and not discipline_code:
        raise ValueError("A per-discipline sign-off needs the discipline it covers.")
    if audit.status in ("closed", "cancelled"):
        raise ValueError(f"Audit is {audit.status}; sign-off is locked.")

    validate_signature(signature_kind, signature_payload, typed_name)

    # The lead auditor's sign-off must come from the lead auditor. Anything else
    # is a signature in someone else's name, which is worse than no signature.
    if role == "LEAD_AUDITOR" and user.id != audit.leadAuditorUserId:
        raise ValueError("Only the assigned lead auditor can record the lead auditor sign-off.")

    entry = {
        "role": role,
        "userId": user.id,
        "name": user.name,
        "designation": user.designation,
        "disciplineCode": discipline_code,
        "signatureKind": signature_kind,
        # A drawn signature keeps its image; a typed one keeps the typed name.
        "signatureImage": signature_payload if signature_kind == "DRAWN" else None,
        "typedName": (typed_name or "").strip() or None,
        "statement": (statement or "").strip() or None,
        # Server-side timestamp: a client clock is not evidence.
        "signedAt": _utcnow().isoformat(),
    }

    signs = [
        s
        for s in (audit.signOffs or [])
        if not (
            s.get("role") == role
            and s.get("disciplineCode") == discipline_code
            and s.get("userId") == user.id
        )
    ]
    signs.append(entry)
    audit.signOffs = signs
    await db.flush()
    return {"ok": True, "signOffCount": len(signs), "role": role}


async def revoke_signoff(
    db: AsyncSession, *, audit: ComplianceAudit, user: User, role: str,
    discipline_code: str | None = None,
) -> dict[str, Any]:
    """Withdraw a sign-off. Only the signer may withdraw their own."""
    if audit.status in ("closed", "cancelled"):
        raise ValueError(f"Audit is {audit.status}; sign-off is locked.")
    before = list(audit.signOffs or [])
    after = [
        s
        for s in before
        if not (
            s.get("role") == role.upper()
            and s.get("disciplineCode") == discipline_code
            and s.get("userId") == user.id
        )
    ]
    if len(after) == len(before):
        raise ValueError("No sign-off of yours matches that role and discipline.")
    audit.signOffs = after
    await db.flush()
    return {"ok": True, "signOffCount": len(after)}


__all__ = [
    "SIGNOFF_ROLES",
    "REQUIRED_FOR_CLOSURE",
    "SIGNATURE_KINDS",
    "validate_signature",
    "signoff_status",
    "record_signoff",
    "revoke_signoff",
]
