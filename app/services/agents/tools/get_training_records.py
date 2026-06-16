"""Tool: get_training_records.

Returns training currency for a specific person — active certificates,
expired-but-relevant certificates, and any training records from the
legacy TrainingRecord table that the new TrainingCertificate model
hasn't superseded yet.

Used to test the "operator wasn't trained" hypothesis. The agent's
job is to AVOID jumping to "blame the worker" — but when training IS
the actual gap, the data should make that visible. The result includes
both active and expired-within-180d certs so the agent can spot
"certification lapsed shortly before the incident" patterns.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.training import TrainingCertificate, TrainingProgram, TrainingRecord
from app.models.user import User


DEFINITION: dict[str, Any] = {
    "name": "get_training_records",
    "description": (
        "Get training certificate currency for a specific person. Returns "
        "active certificates (valid as of the incident date), recently-expired "
        "certificates (expired within 180 days before the incident — these are "
        "the high-signal cases where competency may have lapsed), and a list "
        "of program codes the person has NEVER held a certificate for. Used to "
        "test competency-related hypotheses. CAUTION: a held certificate does "
        "not prove competence — it only confirms training was delivered. Don't "
        "treat absence-of-cert as 'operator at fault'; treat it as a system "
        "question ('why was an uncertified operator on this work?')."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "userId": {
                "type": "string",
                "description": (
                    "User.id of the person to check. From the incident's "
                    "personsInvolved list."
                ),
            },
            "asOfDate": {
                "type": "string",
                "description": (
                    "ISO datetime to evaluate currency against. Defaults to the "
                    "incident's occurredAt. Useful when checking 'were they "
                    "certified at the time of the incident?' explicitly."
                ),
            },
            "programCodes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of TrainingProgram.code values to filter to. "
                    "When set, only certificates for these programs are returned. "
                    "Useful when you suspect a specific training gap "
                    "(e.g. ['ISOLATION_VERIFICATION', 'LOTO_OPERATOR'])."
                ),
            },
        },
        "required": ["userId"],
    },
}


async def handle(
    input: dict[str, Any],  # noqa: A002
    *,
    db: AsyncSession,
    source_record_id: str,
    source_module: str,
) -> dict[str, Any]:
    user_id = input["userId"]
    user = await db.get(User, user_id)
    if user is None:
        return {"note": f"User {user_id!r} not found", "person": None}

    as_of_raw = input.get("asOfDate")
    if as_of_raw:
        try:
            as_of = datetime.fromisoformat(as_of_raw.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError(f"Invalid asOfDate {as_of_raw!r}: {e}") from e
    else:
        as_of = datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    expired_window_start = as_of - timedelta(days=180)

    cert_stmt = select(TrainingCertificate).where(TrainingCertificate.userId == user_id)
    if program_codes := input.get("programCodes"):
        # Join on program to filter by code
        program_subq = select(TrainingProgram.id).where(TrainingProgram.code.in_(program_codes))
        cert_stmt = cert_stmt.where(TrainingCertificate.programId.in_(program_subq))

    cert_stmt = cert_stmt.order_by(TrainingCertificate.validTo.desc().nulls_last())
    certs = (await db.execute(cert_stmt)).scalars().all()

    # Pull program names in a follow-up query so we can label cert rows
    program_ids = {c.programId for c in certs}
    programs_by_id: dict[str, tuple[str, str | None]] = {
        pid: (code, name)
        for pid, code, name in (
            await db.execute(
                select(TrainingProgram.id, TrainingProgram.code, TrainingProgram.name).where(
                    TrainingProgram.id.in_(program_ids)
                )
            )
        ).all()
    } if program_ids else {}

    active: list[dict[str, Any]] = []
    recently_expired: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for c in certs:
        prog_code, prog_name = programs_by_id.get(c.programId, (None, None))
        row = {
            "certificateNumber": c.certificateNumber,
            "programCode": prog_code,
            "programName": prog_name,
            "validFrom": _iso(c.validFrom),
            "validTo": _iso(c.validTo),
            "status": c.status,
            "revokedAt": _iso(c.revokedAt),
            "revocationReason": c.revocationReason,
        }
        valid_to = c.validTo
        if valid_to is not None and valid_to.tzinfo is None:
            valid_to = valid_to.replace(tzinfo=timezone.utc)
        if c.status == "ACTIVE" and (valid_to is None or valid_to >= as_of):
            active.append(row)
        elif valid_to is not None and expired_window_start <= valid_to < as_of:
            row["daysExpiredAtIncident"] = (as_of - valid_to).days
            recently_expired.append(row)
        else:
            other.append(row)

    # Legacy TrainingRecord fallback (PTW/FLRA crew validation still
    # reads these). Surface a count + the most recent few so the model
    # can corroborate the certificate picture.
    legacy_stmt = (
        select(TrainingRecord)
        .where(TrainingRecord.employeeId == user_id)
        .order_by(TrainingRecord.date.desc())
        .limit(5)
    )
    legacy = (await db.execute(legacy_stmt)).scalars().all()

    return {
        "person": {
            "userId": user.id,
            "name": user.name,
            "role": user.role,
            "designation": user.designation,
        },
        "asOfDate": _iso(as_of),
        "activeCertificates": active,
        "recentlyExpiredCertificates": recently_expired,
        "otherCertificates": other,
        "legacyTrainingRecords": [
            {
                "programId": r.programId,
                "date": _iso(r.date),
                "passed": r.passed,
                "validUntil": _iso(r.validUntil),
                "score": r.score,
            }
            for r in legacy
        ],
        "_disclaimer": (
            "A held certificate confirms training was delivered, not that the "
            "person is currently competent. Treat absence of a cert as a "
            "system question, not as operator fault."
        ),
    }


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None
