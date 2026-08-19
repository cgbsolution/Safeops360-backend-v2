"""Signature capture on the fire checklist sign-off chain.

The earlier build recorded a userId and a timestamp at each of Prepared /
Reviewed / Approved and called that a sign-off. It is not one. The sheet being
reproduced prints a "Sign. & Date:" box under each of its three roles, and an
export with a name typed into that box by the system — rather than a mark made by
the person — is not the document an auditor was handed.

This is NOT a new signature mechanism. The platform already has exactly one, and
this is a second consumer of it:

    canvas          components/ui/signature-pad.tsx  (SignatureModal / SignatureField)
    validation      services/signoff.validate_signature
    record shape    ComplianceAudit.signOffs (WP-41)
                    [{role, userId, name, designation, signatureKind,
                      signatureImage, typedName, statement, signedAt}]

Same canvas, same validator, same DRAWN/TYPED vocabulary, same JSON shape, stored
on `CamsEngagement.signOffs`. A third canvas or a fire-specific column layout
would have been the mistake.

WHY TYPED IS ALLOWED
--------------------
Because the alternative is worse. Drawing on a shop-floor tablet with gloved
hands frequently does not work, and a system that only accepts a drawing gets
one inspector's signature used by five people. A typed signature is weaker
evidence and the record says so — `signatureKind` is stored and rendered, so
nobody has to guess which they are looking at.

WHY THE STAMP COLUMNS STAY
--------------------------
`reviewedBy` / `approvedBy` remain the queryable index. "Which sheets did this
person approve" must not be a JSON scan, and the signature is the evidence behind
the stamp rather than a replacement for it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models.cams import CamsEngagement
from app.models.user import User
from app.services.signoff import MAX_SIGNATURE_BYTES, validate_signature

# The three roles printed on every Page Industries fire sheet, keyed to the
# platform stages they correspond to. Held here rather than free text so the
# export, the screen and the record cannot disagree about what was signed.
STAGE_ROLE = {
    "SUBMITTED": "PREPARED_BY",
    "REVIEWED": "REVIEWED_BY",
    "APPROVED": "APPROVED_BY",
}

ROLE_LABEL = {
    "PREPARED_BY": "Prepared by: Person In-charge",
    "REVIEWED_BY": "Reviewed by: Intermediatory Head",
    "APPROVED_BY": "Approved by: HOD",
}

# What the signer is attesting to at each stage. Printed above the signature on
# screen and on the PDF: a signature against no statement is a mark, not an
# attestation, and "what did they actually certify?" is the first question asked
# of a signed record.
STATEMENT = {
    "PREPARED_BY": (
        "I certify that I carried out the checks recorded on this sheet and that the "
        "responses are a true record of what I observed."
    ),
    "REVIEWED_BY": (
        "I have reviewed this record for completeness and consistency, and any failed "
        "check has been raised for corrective action."
    ),
    "APPROVED_BY": (
        "I approve this record as the controlled inspection record for the period stated."
    ),
}


class SignatureRequired(ValueError):
    """A stage was advanced without the signature it requires."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def existing_roles(run: CamsEngagement) -> set[str]:
    return {s.get("role") for s in (run.signOffs or []) if s.get("role")}


def signer_identity(user: User) -> tuple[str, str]:
    """(userId, display name) resolved from a LIVE user row.

    Called by the router, which has a freshly-loaded user from `get_current_user`.
    Kept out of `build_entry` on purpose: attribute access on an expired ORM
    instance triggers a lazy refresh, and under asyncio that is a MissingGreenlet
    rather than a query. A service that does hidden I/O on a property read fails
    unpredictably depending on how far its caller is from the last commit, so the
    identity is resolved once at the edge and passed in as plain strings.
    """
    return user.id, (getattr(user, "name", None) or getattr(user, "email", None) or user.id)


def build_entry(
    *,
    stage: str,
    user_id: str,
    user_name: str,
    signature_kind: str,
    signature_payload: str | None,
    typed_name: str | None,
    designation: str | None = None,
) -> dict[str, Any]:
    """Validate and shape one signature entry.

    The signer is ALWAYS the authenticated user — `user_id` comes from the session,
    never from the request body. A signature recorded in someone else's name is
    worse than no signature at all, because it manufactures evidence.
    """
    role = STAGE_ROLE.get(stage)
    if role is None:
        raise ValueError(f"'{stage}' is not a signable stage.")

    kind = (signature_kind or "DRAWN").upper()
    if kind not in ("DRAWN", "TYPED"):
        raise ValueError("signatureKind must be DRAWN or TYPED.")
    # Platform validator: data-URI shape and the shared size ceiling. Not
    # re-implemented here, so a change to the platform rule reaches this too.
    validate_signature(kind, signature_payload, typed_name)

    name = user_name or user_id
    return {
        "role": role,
        "roleLabel": ROLE_LABEL[role],
        "userId": user_id,
        "name": name,
        "designation": (designation or "").strip() or None,
        "signatureKind": kind,
        "signatureImage": signature_payload if kind == "DRAWN" else None,
        "typedName": (typed_name or "").strip() or (name if kind == "TYPED" else None),
        "statement": STATEMENT[role],
        "signedAt": _now().isoformat(),
    }


def record(run: CamsEngagement, entry: dict[str, Any]) -> None:
    """Append a signature, replacing any prior one for the same role.

    Replace rather than accumulate: a stage is signed once, and two PREPARED_BY
    entries on one record is not a history, it is an ambiguity about who prepared
    it. Re-signing is only reachable by an authorised stage transition anyway.
    """
    kept = [s for s in (run.signOffs or []) if s.get("role") != entry["role"]]
    run.signOffs = kept + [entry]


def require_for_stage(
    run: CamsEngagement,
    stage: str,
    *,
    signature_kind: str | None,
    signature_payload: str | None,
    typed_name: str | None,
    enforce: bool,
) -> dict[str, Any] | None:
    """Return the entry to record, or raise if a required signature is missing.

    `enforce` is per template, not global. A daily round on a shop-floor tablet is
    signed once a month on the paper original — demanding 31 drawn signatures for
    31 daily records would get the tablet handed round and one person signing for
    everyone, which is worse evidence than the userId stamp alone. Monthly,
    quarterly and annual sheets DO carry a per-record signature block, and those
    enforce.
    """
    if signature_payload or (typed_name or "").strip():
        # Offered: always validate and record it, enforced or not.
        return {"kind": signature_kind or "DRAWN", "payload": signature_payload, "typed": typed_name}
    if enforce:
        raise SignatureRequired(
            f"{ROLE_LABEL[STAGE_ROLE[stage]]} requires a signature. "
            "Draw it, or type your full name as you would write it."
        )
    return None


def out(run: CamsEngagement) -> list[dict[str, Any]]:
    """Signatures for the API, newest per role, image included.

    The image is returned because the screen and the PDF both render it. It is a
    base64 PNG capped by MAX_SIGNATURE_BYTES, so a sheet with three signatures
    carries at most ~768 KB — acceptable for a single record view, and the reason
    the grid endpoints do NOT return signatures for 31 records at once.
    """
    return list(run.signOffs or [])


def summary(run: CamsEngagement) -> list[dict[str, Any]]:
    """Signature presence WITHOUT the images — for list and grid responses."""
    return [
        {k: v for k, v in s.items() if k != "signatureImage"} | {"hasImage": bool(s.get("signatureImage"))}
        for s in (run.signOffs or [])
    ]


__all__ = [
    "signer_identity", "STAGE_ROLE", "ROLE_LABEL", "STATEMENT", "MAX_SIGNATURE_BYTES",
    "SignatureRequired", "build_entry", "record", "require_for_stage", "out", "summary",
    "existing_roles",
]
