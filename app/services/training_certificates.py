"""Training certificate lifecycle service.

Owns the certificate state machine and issuance logic. Designed so the
schedule-complete handler, the daily refresh job, and admin actions all
go through the same code path.

State machine:

    ACTIVE ──(validTo - 30d)──► EXPIRING_SOON ──(validTo)──► EXPIRED
                                                               │
                                                  (validTo + grace) ▼
                                                            LAPSED
                                                               │
                                                       (admin)  ▼
        ACTIVE / EXPIRING_SOON / EXPIRED ──(admin)──► REVOKED

Auto-issue: called from training schedule.complete and from the
assessment.submit handler when a learner passes. Idempotent — never
issues a duplicate certificate for the same registration.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plant import Plant
from app.models.training import (
    TrainingCertificate,
    TrainingProgram,
    TrainingRegistration,
    TrainingSchedule,
)


# ─── Number generation ───────────────────────────────────────────────


async def _next_certificate_number(db: AsyncSession, plant_code: str) -> str:
    year = datetime.now(timezone.utc).year
    count = (
        await db.execute(select(func.count()).select_from(TrainingCertificate))
    ).scalar_one()
    return f"CERT-{year}-{plant_code}-{count + 1:05d}"


def _generate_qr_payload(certificate_number: str) -> str:
    """Returns a verification URL that encodes into a QR. The public
    verification page reads ?cert= from the URL."""
    return f"/verify/training/{certificate_number}"


def _generate_signature(certificate_number: str, holder_id: str, issued_at: datetime) -> str:
    """Best-effort tamper-detection hash. NOT cryptographic — just
    catches accidental DB edits. A future commit can swap this for an
    HMAC keyed off JWT_SECRET."""
    payload = f"{certificate_number}|{holder_id}|{issued_at.isoformat()}|{secrets.token_hex(8)}"
    import hashlib

    return hashlib.sha256(payload.encode()).hexdigest()[:32]


# ─── Auto-issue ──────────────────────────────────────────────────────


async def issue_certificate_if_eligible(
    db: AsyncSession,
    *,
    registration_id: str,
    issuer_user_id: str | None = None,
) -> TrainingCertificate | None:
    """Idempotent. Issues a certificate when:
      • The registration passed assessment (registration.passed = True)
      • The program issuesCertificate = True
      • No existing certificate for this registration

    Returns the certificate (newly created or already existing).
    Returns None if not eligible (didn't pass / program doesn't issue
    certificates).
    """
    reg = await db.get(TrainingRegistration, registration_id)
    if reg is None:
        return None
    if reg.passed is not True:
        return None

    # Already issued?
    if reg.certificateId:
        existing = await db.get(TrainingCertificate, reg.certificateId)
        if existing is not None:
            return existing

    schedule = await db.get(TrainingSchedule, reg.scheduleId)
    if schedule is None:
        return None
    program = await db.get(TrainingProgram, schedule.programId)
    if program is None or not program.issuesCertificate:
        return None
    plant = await db.get(Plant, schedule.plantId)
    plant_code = plant.code if plant else "PLANT"

    # Compute validity window
    now = datetime.now(timezone.utc)
    valid_from = schedule.endDate or now
    valid_to: datetime | None = None
    months = program.certificateValidityMonths or program.validityMonths
    if months and months < 999:
        # 30-day months — clients only care about month-precision
        valid_to = valid_from + timedelta(days=30 * months)

    cert_number = await _next_certificate_number(db, plant_code)
    qr_payload = _generate_qr_payload(cert_number)
    signature = _generate_signature(cert_number, reg.userId, now)

    cert = TrainingCertificate(
        certificateNumber=cert_number,
        programId=program.id,
        userId=reg.userId,
        registrationId=reg.id,
        issuedById=issuer_user_id,
        finalAssessmentScore=reg.assessmentScore,
        attendancePercent=reg.attendancePercent,
        validFrom=valid_from,
        validTo=valid_to,
        status="ACTIVE",
        certificateQrCode=qr_payload,
        digitalSignature=signature,
    )
    db.add(cert)
    await db.flush()

    reg.certificateId = cert.id
    await db.flush()
    return cert


async def issue_certificates_for_schedule(
    db: AsyncSession, *, schedule_id: str, issuer_user_id: str | None = None
) -> list[TrainingCertificate]:
    """Bulk issue for all PASSED registrations on a schedule. Called by
    the schedule-complete handler. Skips already-issued ones."""
    regs = (
        await db.execute(
            select(TrainingRegistration).where(
                TrainingRegistration.scheduleId == schedule_id,
                TrainingRegistration.passed.is_(True),
            )
        )
    ).scalars().all()
    issued: list[TrainingCertificate] = []
    for reg in regs:
        cert = await issue_certificate_if_eligible(
            db, registration_id=reg.id, issuer_user_id=issuer_user_id
        )
        if cert is not None:
            issued.append(cert)
    return issued


# ─── State refresh ───────────────────────────────────────────────────


async def refresh_certificate_states(db: AsyncSession) -> dict[str, int]:
    """Walks every non-final certificate and updates status based on
    today's date relative to validTo + grace period. Idempotent —
    safe to run nightly OR on-demand from a button.

    State transitions managed:
      ACTIVE         → EXPIRING_SOON (within 30 days of validTo)
      EXPIRING_SOON  → EXPIRED        (validTo passed)
      EXPIRED        → LAPSED         (validTo + grace days passed)

    Does NOT touch REVOKED / LAPSED (terminal-ish for state machine
    purposes — LAPSED requires re-certification but state cannot
    auto-progress further).

    Returns a dict of counts per transition for logging.
    """
    now = datetime.now(timezone.utc)
    transitions = {"to_expiring_soon": 0, "to_expired": 0, "to_lapsed": 0}

    # ACTIVE → EXPIRING_SOON
    soon_threshold = now + timedelta(days=30)
    rows = (
        await db.execute(
            select(TrainingCertificate).where(
                TrainingCertificate.status == "ACTIVE",
                TrainingCertificate.validTo.isnot(None),
                TrainingCertificate.validTo <= soon_threshold,
                TrainingCertificate.validTo > now,
            )
        )
    ).scalars().all()
    for c in rows:
        c.status = "EXPIRING_SOON"
        transitions["to_expiring_soon"] += 1

    # ACTIVE / EXPIRING_SOON → EXPIRED
    rows = (
        await db.execute(
            select(TrainingCertificate).where(
                TrainingCertificate.status.in_(["ACTIVE", "EXPIRING_SOON"]),
                TrainingCertificate.validTo.isnot(None),
                TrainingCertificate.validTo <= now,
            )
        )
    ).scalars().all()
    for c in rows:
        c.status = "EXPIRED"
        transitions["to_expired"] += 1

    # EXPIRED → LAPSED — need to read program for grace period
    rows = (
        await db.execute(
            select(TrainingCertificate).where(
                TrainingCertificate.status == "EXPIRED",
                TrainingCertificate.validTo.isnot(None),
            )
        )
    ).scalars().all()
    for c in rows:
        program = await db.get(TrainingProgram, c.programId)
        if program is None:
            continue
        grace_end = c.validTo + timedelta(days=program.certificateExpiryGracePeriodDays)  # type: ignore
        if now > grace_end:
            c.status = "LAPSED"
            transitions["to_lapsed"] += 1

    await db.flush()
    return transitions


# ─── Admin actions ───────────────────────────────────────────────────


async def revoke_certificate(
    db: AsyncSession,
    *,
    certificate_id: str,
    revoker_user_id: str,
    reason: str,
    details: str,
) -> TrainingCertificate:
    """Hard revocation — terminates the certificate immediately.
    Caller must do RBAC check beforehand."""
    cert = await db.get(TrainingCertificate, certificate_id)
    if cert is None:
        raise ValueError("Certificate not found")
    if cert.status == "REVOKED":
        raise ValueError("Certificate already revoked")

    cert.status = "REVOKED"
    cert.revokedAt = datetime.now(timezone.utc)
    cert.revokedById = revoker_user_id
    cert.revocationReason = reason
    cert.revocationDetails = details
    await db.flush()
    return cert


async def record_effectiveness_review(
    db: AsyncSession,
    *,
    certificate_id: str,
    reviewer_user_id: str,
    rating: int,
    notes: str | None,
) -> TrainingCertificate:
    cert = await db.get(TrainingCertificate, certificate_id)
    if cert is None:
        raise ValueError("Certificate not found")
    if cert.status not in ("ACTIVE", "EXPIRING_SOON"):
        raise ValueError(
            f"Effectiveness review only valid on ACTIVE / EXPIRING_SOON certificates "
            f"(current: {cert.status})."
        )
    cert.effectivenessReviewedAt = datetime.now(timezone.utc)
    cert.effectivenessReviewedById = reviewer_user_id
    cert.effectivenessRating = rating
    cert.effectivenessNotes = notes
    await db.flush()
    return cert
