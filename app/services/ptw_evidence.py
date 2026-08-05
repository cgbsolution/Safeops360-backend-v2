"""PTW action evidence — closed-loop field proof for every permit action.

Every safety-relevant transition in the permit lifecycle (approve, accept,
suspend/resume/extend, work-completed declaration, handback inspection,
closure, cancellation, isolation verification) must carry field evidence:

  • GPS coordinates of the actor's device (+ accuracy when available)
  • a drawn signature (data-URL PNG)
  • one or more onsite photos, uploaded first via the PermitAttachment
    two-phase signed-URL flow and referenced by id
  • an optional declaration the actor confirms

The per-action requirements are POLICY, not hardcode — `EVIDENCE_POLICY`
below is the single place that says which action needs what. Validation
raises `EvidenceError` (routers convert to HTTP 422) so a client that
forgot GPS/photo/signature gets a precise, actionable message.

`record_action_evidence` persists the PermitActionEvidence row and links
the uploaded photos to it. Rows join the tamper-evident audit hash-chain
(registered in app.main) and are rendered as the evidence timeline in the
close-out report.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.permit import (
    Permit,
    PermitActionEvidence,
    PermitAttachment,
    PermitEvidenceAction,
)


class EvidenceError(Exception):
    """Evidence missing/invalid for a permit lifecycle action. Routers
    convert to HTTP 422 so the client can re-prompt for the field."""


@dataclass(frozen=True)
class EvidenceRule:
    gps: bool = True
    signature: bool = True
    photo: bool = False


# ─── Policy: which action requires what. Photo is mandatory at the field
#     actions (approvals, acceptance, work-completed, handback); GPS +
#     signature are mandatory everywhere. Edit HERE to change policy. ───
EVIDENCE_POLICY: dict[PermitEvidenceAction, EvidenceRule] = {
    PermitEvidenceAction.APPROVE_ISSUER: EvidenceRule(photo=True),
    PermitEvidenceAction.APPROVE_SAFETY: EvidenceRule(photo=True),
    PermitEvidenceAction.APPROVE_PLANT_HEAD: EvidenceRule(photo=True),
    PermitEvidenceAction.APPROVE: EvidenceRule(photo=True),
    PermitEvidenceAction.ISSUE: EvidenceRule(),
    PermitEvidenceAction.ACCEPT: EvidenceRule(photo=True),
    PermitEvidenceAction.ISOLATION_VERIFY: EvidenceRule(),
    PermitEvidenceAction.SUSPEND: EvidenceRule(),
    PermitEvidenceAction.RESUME: EvidenceRule(),
    PermitEvidenceAction.EXTEND: EvidenceRule(),
    PermitEvidenceAction.WORK_COMPLETED_DECLARE: EvidenceRule(photo=True),
    PermitEvidenceAction.HANDBACK_INSPECT: EvidenceRule(photo=True),
    PermitEvidenceAction.CLOSE: EvidenceRule(),
    PermitEvidenceAction.CANCEL: EvidenceRule(),
    # Rejection evidence is optional in full — an approver may reject from
    # their desk; the reason field is the mandatory part (engine enforces).
    PermitEvidenceAction.REJECT: EvidenceRule(gps=False, signature=False),
}


def _rule_for(action: PermitEvidenceAction) -> EvidenceRule:
    return EVIDENCE_POLICY.get(action, EvidenceRule())


def validate_evidence(
    action: PermitEvidenceAction,
    *,
    gps_latitude: float | None,
    gps_longitude: float | None,
    signature_image: str | None,
    photo_attachment_ids: list[str] | None,
) -> None:
    """Raise EvidenceError listing EVERY missing element at once (same
    all-at-once philosophy as the activation gate)."""
    rule = _rule_for(action)
    missing: list[str] = []
    if rule.gps and (gps_latitude is None or gps_longitude is None):
        missing.append("GPS coordinates (enable location on your device)")
    if gps_latitude is not None and not (-90.0 <= gps_latitude <= 90.0):
        raise EvidenceError("GPS latitude out of range.")
    if gps_longitude is not None and not (-180.0 <= gps_longitude <= 180.0):
        raise EvidenceError("GPS longitude out of range.")
    if rule.signature and not (signature_image and signature_image.strip()):
        missing.append("a drawn signature")
    if rule.photo and not photo_attachment_ids:
        missing.append("at least one onsite photo")
    if missing:
        raise EvidenceError(
            f"This action requires field evidence — missing: {', '.join(missing)}."
        )
    if signature_image and len(signature_image) > 500_000:
        raise EvidenceError("Signature image is too large (500 KB max).")


async def record_action_evidence(
    db: AsyncSession,
    *,
    permit: Permit,
    action: PermitEvidenceAction,
    actor_id: str,
    gps_latitude: float | None = None,
    gps_longitude: float | None = None,
    gps_accuracy_meters: float | None = None,
    signature_image: str | None = None,
    declaration_text: str | None = None,
    comments: str | None = None,
    photo_attachment_ids: list[str] | None = None,
    enforce: bool = True,
) -> PermitActionEvidence:
    """Validate (unless enforce=False for system actions), persist the
    evidence row, and link the uploaded photos to it. Photos must already
    exist as PermitAttachment rows on THIS permit (uploaded via the
    two-phase attachment flow) — dangling/foreign ids are rejected."""
    if enforce:
        validate_evidence(
            action,
            gps_latitude=gps_latitude,
            gps_longitude=gps_longitude,
            signature_image=signature_image,
            photo_attachment_ids=photo_attachment_ids,
        )

    row = PermitActionEvidence(
        permitId=permit.id,
        action=action,
        actorId=actor_id,
        gpsLatitude=gps_latitude,
        gpsLongitude=gps_longitude,
        gpsAccuracyMeters=gps_accuracy_meters,
        signatureImageBase64=signature_image,
        declarationText=declaration_text,
        comments=comments,
    )
    db.add(row)
    await db.flush()

    if photo_attachment_ids:
        atts = (
            await db.execute(
                select(PermitAttachment)
                .where(PermitAttachment.id.in_(photo_attachment_ids))
                .where(PermitAttachment.permitId == permit.id)
                .where(PermitAttachment.deletedAt.is_(None))
            )
        ).scalars().all()
        found = {a.id for a in atts}
        dangling = [i for i in photo_attachment_ids if i not in found]
        if dangling:
            raise EvidenceError(
                "Photo attachment(s) not found on this permit: "
                + ", ".join(dangling)
                + ". Upload via POST /api/ptw/{id}/attachments first."
            )
        for a in atts:
            a.actionEvidenceId = row.id
        await db.flush()

    return row


def evidence_out(row: PermitActionEvidence) -> dict:
    """Serializer used by the detail endpoint + report builders."""
    return {
        "id": row.id,
        "action": row.action.value if hasattr(row.action, "value") else str(row.action),
        "actorId": row.actorId,
        "gpsLatitude": row.gpsLatitude,
        "gpsLongitude": row.gpsLongitude,
        "gpsAccuracyMeters": row.gpsAccuracyMeters,
        "capturedAt": row.capturedAt.isoformat() if row.capturedAt else None,
        "hasSignature": bool(row.signatureImageBase64),
        "declarationText": row.declarationText,
        "comments": row.comments,
        "photoAttachmentIds": [p.id for p in (row.photos or [])],
    }
