"""Tool: check_crew_training_currency.

For each crew member on the source permit, classifies their training
certificate currency relative to the permit's validity window:

  • validThroughout — every active certificate's validTo is after permit.validTo
  • expiresDuringPermit — at least one active cert expires inside
    [permit.validFrom, permit.validTo] — this is the high-signal case
    the rules engine often misses (rules typically check "is it valid
    today", not "is it valid for the whole window")
  • expiredBeforePermit — at least one cert is already expired at permit.validFrom
  • noActiveCertificates — crew member has no active certs at all

Used to surface the prompt's `crew_competency` pattern with category
`high` (training expiry crosses the work window).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.permit import Permit
from app.models.training import TrainingCertificate, TrainingProgram
from app.models.user import User


DEFINITION: dict[str, Any] = {
    "name": "check_crew_training_currency",
    "description": (
        "Check each crew member's training certificate currency against the "
        "permit's validity window. Returns a classification per person: "
        "validThroughout / expiresDuringPermit / expiredBeforePermit / "
        "noActiveCertificates. expiresDuringPermit is the high-signal "
        "finding — the rules engine usually checks 'valid today', not "
        "'valid for the full work window'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "programCodes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of TrainingProgram.code values to filter "
                    "to. When set, only certificates for these programs are "
                    "considered. Useful when you only care about specific "
                    "certifications (e.g. ['HOT_WORK', 'FIRE_WATCH'] for a "
                    "hot-work permit)."
                ),
            },
        },
        "required": [],
    },
}


async def handle(
    input: dict[str, Any],  # noqa: A002
    *,
    db: AsyncSession,
    source_record_id: str,
    source_module: str,
) -> dict[str, Any]:
    if source_module != "PTW":
        raise ValueError(
            f"check_crew_training_currency expects source_module=PTW, got {source_module!r}"
        )

    permit = await db.get(Permit, source_record_id)
    if permit is None:
        return {"crew": [], "note": "Source permit not found"}

    valid_from = _ensure_aware(permit.validFrom)
    valid_to = _ensure_aware(permit.validTo)

    # Pull crew via separate query; we don't selectinload on the permit
    # because the caller may invoke this tool any number of times during
    # the loop and the snapshot can drift.
    from app.models.permit import PermitCrewMember  # local import — avoids cycle

    crew_stmt = select(PermitCrewMember).where(PermitCrewMember.permitId == permit.id)
    crew_rows = (await db.execute(crew_stmt)).scalars().all()
    if not crew_rows:
        return {
            "permitNumber": permit.number,
            "validFrom": _iso(valid_from),
            "validTo": _iso(valid_to),
            "crew": [],
            "note": "No crew members on this permit",
        }

    # Resolve user names in one batch
    user_ids = [c.userId for c in crew_rows if c.userId]
    users_by_id: dict[str, User] = {}
    if user_ids:
        users = (
            (await db.execute(select(User).where(User.id.in_(user_ids))))
            .scalars()
            .all()
        )
        users_by_id = {u.id: u for u in users}

    # Resolve program filter (if any) to program IDs once
    program_filter_ids: set[str] | None = None
    if program_codes := input.get("programCodes"):
        program_filter_ids = set(
            (
                await db.execute(
                    select(TrainingProgram.id).where(
                        TrainingProgram.code.in_(program_codes)
                    )
                )
            ).scalars().all()
        )

    results: list[dict[str, Any]] = []
    for member in crew_rows:
        cert_stmt = select(TrainingCertificate).where(
            TrainingCertificate.userId == member.userId
        )
        if program_filter_ids is not None:
            if not program_filter_ids:
                # programCodes filter resolved to zero programs — no point querying
                certs: list[TrainingCertificate] = []
            else:
                cert_stmt = cert_stmt.where(
                    TrainingCertificate.programId.in_(program_filter_ids)
                )
                certs = list((await db.execute(cert_stmt)).scalars().all())
        else:
            certs = list((await db.execute(cert_stmt)).scalars().all())

        # Resolve program codes/names for surfacing
        prog_ids_in_certs = {c.programId for c in certs}
        prog_lookup: dict[str, tuple[str, str | None]] = {}
        if prog_ids_in_certs:
            prog_lookup = {
                pid: (code, name)
                for pid, code, name in (
                    await db.execute(
                        select(
                            TrainingProgram.id,
                            TrainingProgram.code,
                            TrainingProgram.name,
                        ).where(TrainingProgram.id.in_(prog_ids_in_certs))
                    )
                ).all()
            }

        active_certs = [
            c for c in certs if (c.status or "").upper() == "ACTIVE" and c.revokedAt is None
        ]

        classification, expiring_certs = _classify(active_certs, valid_from, valid_to)
        user = users_by_id.get(member.userId)
        results.append(
            {
                "userId": member.userId,
                "name": user.name if user else None,
                "role": member.role,
                "trainingValidAtIssuance": member.trainingValidAtIssuance,
                "classification": classification,
                "expiringCertificates": [
                    {
                        "certificateNumber": c.certificateNumber,
                        "programCode": prog_lookup.get(c.programId, (None, None))[0],
                        "programName": prog_lookup.get(c.programId, (None, None))[1],
                        "validFrom": _iso(c.validFrom),
                        "validTo": _iso(c.validTo),
                    }
                    for c in expiring_certs
                ],
                "activeCertificateCount": len(active_certs),
            }
        )

    return {
        "permitNumber": permit.number,
        "validFrom": _iso(valid_from),
        "validTo": _iso(valid_to),
        "programCodesFiltered": input.get("programCodes") or None,
        "crew": results,
        "_note": (
            "expiresDuringPermit is the high-signal case: the certificate "
            "is valid at issuance but lapses inside the work window. The "
            "rules engine commonly misses this."
        ),
    }


def _classify(
    active_certs: list[TrainingCertificate],
    valid_from: datetime,
    valid_to: datetime,
) -> tuple[str, list[TrainingCertificate]]:
    """Return (classification, certs-that-expire-during-window). The
    expiring list is empty unless classification == 'expiresDuringPermit'."""
    if not active_certs:
        return "noActiveCertificates", []

    expiring: list[TrainingCertificate] = []
    expired_before: list[TrainingCertificate] = []
    for c in active_certs:
        if c.validTo is None:
            continue
        cert_to = _ensure_aware(c.validTo)
        if cert_to < valid_from:
            expired_before.append(c)
        elif valid_from <= cert_to <= valid_to:
            expiring.append(c)

    if expired_before:
        return "expiredBeforePermit", []
    if expiring:
        return "expiresDuringPermit", expiring
    return "validThroughout", []


def _ensure_aware(v: datetime) -> datetime:
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None
