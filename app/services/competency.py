"""Canonical competency check.

Single source of truth for "does user X meet the training requirements
for situation Y". All SafeOps gates (PTW crew add, FLRA sign-off,
role assignment, contractor onboarding, equipment operator) call
through this service so the rules stay consistent.

Design:

  • Reads `TrainingCertificate` first (production-depth state machine).
    Falls back to `TrainingRecord` (legacy) when no certificate exists,
    so the migration window doesn't break existing PTW seeded data.
  • Returns a structured `CompetencyCheckResult` listing blockers
    AND warnings (e.g. "expires in 12 days"). Callers decide what to
    do with each — typically blockers fail the gate, warnings show
    a chip but allow.
  • Aggregates across multiple required programs. Hot Work, for
    instance, can require Hot Work Holder + Fire Watch + Basic Safety
    all simultaneously.

Lookup helpers:

  • `get_required_programs_for_permit_type(code)` reads
    `TrainingProgram.isMandatoryForPermitTypes` array contains
    the permit type code AND `blocksPtwIfMissing=true`.
  • `get_required_programs_for_role(code)` reads
    `TrainingProgram.isMandatoryForRoles` AND
    `blocksRoleAssignmentIfMissing=true`.
  • `get_required_programs_for_contractor()` reads
    `TrainingProgram.blocksContractorOnboardingIfMissing=true`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import any_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.training import TrainingCertificate, TrainingProgram, TrainingRecord


# ─── Result shapes ────────────────────────────────────────────────────


@dataclass
class CompetencyBlocker:
    """One reason the user fails the gate."""

    programCode: str
    programName: str
    code: str  # MISSING | EXPIRED | LAPSED | REVOKED | RECORD_FAILED
    message: str


@dataclass
class CompetencyWarning:
    """One soft warning — user passes but should know about it."""

    programCode: str
    programName: str
    code: str  # EXPIRING_SOON | NO_CERTIFICATE_LEGACY_ONLY
    message: str
    daysUntilExpiry: int | None = None


@dataclass
class CompetencyCheckResult:
    ok: bool
    blockers: list[CompetencyBlocker] = field(default_factory=list)
    warnings: list[CompetencyWarning] = field(default_factory=list)
    # Programs that the user IS competent in (for UI green-tick rendering)
    satisfied: list[str] = field(default_factory=list)


# ─── Lookup helpers ───────────────────────────────────────────────────


async def get_required_programs_for_permit_type(
    db: AsyncSession, permit_type_code: str
) -> list[TrainingProgram]:
    """Programs that are mandatory for this permit type AND have the
    PTW gate enabled. Postgres array contains."""
    rows = (
        await db.execute(
            select(TrainingProgram)
            .where(TrainingProgram.isActive == True)  # noqa: E712
            .where(TrainingProgram.approvalStatus == "APPROVED")
            .where(TrainingProgram.blocksPtwIfMissing == True)  # noqa: E712
            .where(permit_type_code == any_(TrainingProgram.isMandatoryForPermitTypes))
        )
    ).scalars().all()
    return list(rows)


async def get_required_programs_for_role(
    db: AsyncSession, role_code: str
) -> list[TrainingProgram]:
    rows = (
        await db.execute(
            select(TrainingProgram)
            .where(TrainingProgram.isActive == True)  # noqa: E712
            .where(TrainingProgram.approvalStatus == "APPROVED")
            .where(TrainingProgram.blocksRoleAssignmentIfMissing == True)  # noqa: E712
            .where(role_code == any_(TrainingProgram.isMandatoryForRoles))
        )
    ).scalars().all()
    return list(rows)


async def get_required_programs_for_contractor(db: AsyncSession) -> list[TrainingProgram]:
    """Programs that gate contractor gate-pass issuance."""
    rows = (
        await db.execute(
            select(TrainingProgram)
            .where(TrainingProgram.isActive == True)  # noqa: E712
            .where(TrainingProgram.approvalStatus == "APPROVED")
            .where(TrainingProgram.blocksContractorOnboardingIfMissing == True)  # noqa: E712
        )
    ).scalars().all()
    return list(rows)


# ─── Cert lookup with legacy fallback ─────────────────────────────────


async def _get_user_certificate_or_record(
    db: AsyncSession, user_id: str, program_id: str, program_code: str
) -> tuple[TrainingCertificate | None, TrainingRecord | None]:
    """Returns the most relevant proof of training for this (user, program)
    pair. Prefers TrainingCertificate (new); falls back to TrainingRecord
    (legacy seed data + pre-migration writes).

    Returns (cert, record) — at most one is non-null. cert takes priority."""
    cert = (
        await db.execute(
            select(TrainingCertificate)
            .where(TrainingCertificate.userId == user_id)
            .where(TrainingCertificate.programId == program_id)
            .order_by(TrainingCertificate.issuedAt.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if cert is not None:
        return cert, None

    # Legacy fallback — TrainingRecord by programId
    rec = (
        await db.execute(
            select(TrainingRecord)
            .where(TrainingRecord.employeeId == user_id)
            .where(TrainingRecord.programId == program_id)
            .order_by(TrainingRecord.validUntil.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return None, rec


# ─── Core check ───────────────────────────────────────────────────────


async def check_user_competencies(
    db: AsyncSession, user_id: str, required_programs: list[TrainingProgram]
) -> CompetencyCheckResult:
    """Walk every required program and aggregate result. Each program
    contributes either to satisfied / warning / blocker."""
    result = CompetencyCheckResult(ok=True)
    if not required_programs:
        return result

    now = datetime.now(timezone.utc)

    for program in required_programs:
        program_code = program.programCode or program.code
        program_name = program.programName or program.name

        cert, rec = await _get_user_certificate_or_record(
            db, user_id, program.id, program_code
        )

        if cert is not None:
            # New TrainingCertificate path — use the state machine.
            # Normalise case: certificates are written with status "ACTIVE"
            # by the app (model default) but some seed data persisted them
            # lowercase ("active"). Comparing case-sensitively dropped those
            # valid certs into the `else` branch below and wrongly blocked
            # competent receivers with "unknown state (active)".
            cert_status = (cert.status or "").upper()
            if cert_status == "ACTIVE":
                result.satisfied.append(program_code)
            elif cert_status == "EXPIRING_SOON":
                days_remaining: int | None = None
                if cert.validTo is not None:
                    valid_to = cert.validTo
                    if valid_to.tzinfo is None:
                        valid_to = valid_to.replace(tzinfo=timezone.utc)
                    delta = valid_to - now
                    days_remaining = max(0, delta.days)
                result.warnings.append(
                    CompetencyWarning(
                        programCode=program_code,
                        programName=program_name,
                        code="EXPIRING_SOON",
                        message=(
                            f'"{program_name}" certificate expires soon'
                            + (f" — {days_remaining} day(s)" if days_remaining is not None else "")
                            + "."
                        ),
                        daysUntilExpiry=days_remaining,
                    )
                )
                result.satisfied.append(program_code)
            elif cert_status == "EXPIRED":
                result.ok = False
                result.blockers.append(
                    CompetencyBlocker(
                        programCode=program_code,
                        programName=program_name,
                        code="EXPIRED",
                        message=f'"{program_name}" certificate expired on {cert.validTo.strftime("%d %b %Y") if cert.validTo else "—"}.',
                    )
                )
            elif cert_status == "LAPSED":
                result.ok = False
                result.blockers.append(
                    CompetencyBlocker(
                        programCode=program_code,
                        programName=program_name,
                        code="LAPSED",
                        message=(
                            f'"{program_name}" certificate has lapsed (past grace period). '
                            "Full re-certification required."
                        ),
                    )
                )
            elif cert_status == "REVOKED":
                result.ok = False
                result.blockers.append(
                    CompetencyBlocker(
                        programCode=program_code,
                        programName=program_name,
                        code="REVOKED",
                        message=f'"{program_name}" certificate has been revoked. Cannot proceed.',
                    )
                )
            else:
                # Unknown future status — be conservative and block
                result.ok = False
                result.blockers.append(
                    CompetencyBlocker(
                        programCode=program_code,
                        programName=program_name,
                        code="MISSING",
                        message=f'"{program_name}" certificate is in an unknown state ({cert.status}).',
                    )
                )
        elif rec is not None:
            # Legacy TrainingRecord path
            valid_until = rec.validUntil
            if valid_until.tzinfo is None:
                valid_until = valid_until.replace(tzinfo=timezone.utc)
            if not rec.passed:
                result.ok = False
                result.blockers.append(
                    CompetencyBlocker(
                        programCode=program_code,
                        programName=program_name,
                        code="RECORD_FAILED",
                        message=f'Latest "{program_name}" attempt was failed.',
                    )
                )
            elif valid_until < now:
                result.ok = False
                result.blockers.append(
                    CompetencyBlocker(
                        programCode=program_code,
                        programName=program_name,
                        code="EXPIRED",
                        message=f'"{program_name}" record expired on {valid_until.strftime("%d %b %Y")}.',
                    )
                )
            else:
                # Valid legacy record but no certificate — soft warning
                # so HSE knows the migration to certificates is pending.
                result.warnings.append(
                    CompetencyWarning(
                        programCode=program_code,
                        programName=program_name,
                        code="NO_CERTIFICATE_LEGACY_ONLY",
                        message=(
                            f'"{program_name}" passes via legacy training record. '
                            "A formal certificate has not yet been issued."
                        ),
                    )
                )
                result.satisfied.append(program_code)
        else:
            # Nothing at all — hard blocker
            result.ok = False
            result.blockers.append(
                CompetencyBlocker(
                    programCode=program_code,
                    programName=program_name,
                    code="MISSING",
                    message=f'No "{program_name}" certificate or record on file.',
                )
            )

    return result


# ─── High-level wrappers used by the gates ────────────────────────────


async def check_competency_for_permit_type(
    db: AsyncSession, user_id: str, permit_type_code: str
) -> CompetencyCheckResult:
    """Used by PTW crew add + FLRA sign-off."""
    required = await get_required_programs_for_permit_type(db, permit_type_code)
    return await check_user_competencies(db, user_id, required)


async def check_competency_for_role(
    db: AsyncSession, user_id: str, role_code: str
) -> CompetencyCheckResult:
    """Used by user-role assignment endpoint."""
    required = await get_required_programs_for_role(db, role_code)
    return await check_user_competencies(db, user_id, required)


async def check_competency_for_contractor_onboarding(
    db: AsyncSession, user_id: str
) -> CompetencyCheckResult:
    """Used by contractor gate-pass issuance."""
    required = await get_required_programs_for_contractor(db)
    return await check_user_competencies(db, user_id, required)
