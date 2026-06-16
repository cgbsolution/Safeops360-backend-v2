"""Competency-state service — the training receiver (Phase B, decision D1).

"Training feeds competency": when a person's training certificates change, the
matching `CompetencyRecord` is advanced through its lifecycle. This module
recomputes a record's `state` from current training evidence, delegating the
*training validity* judgement to `competency.check_user_competencies` (so the
certificate state machine + legacy `TrainingRecord` fallback are reused, not
duplicated) and the *state decision* to `competency_states`.

Public entry points:
  - sync_record_from_training(...)   — one record
  - sync_person_from_training(...)   — one person's records in a plant
  - sync_plant_from_training(...)    — every record in a plant (backfill / on-demand)

Each state change writes a `CompetencyRecordVersion` audit row and is
idempotent: a second run with unchanged training produces no writes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competency_matrix import (
    Competency,
    CompetencyRecord,
    CompetencyRecordVersion,
)
from app.models.training import TrainingCertificate, TrainingProgram
from app.services import competency_states as st
from app.services.competency import check_user_competencies

_ACTIVE_CERT_STATES = ("ACTIVE", "EXPIRING_SOON")


async def _required_programs(db: AsyncSession, competency: Competency) -> list[TrainingProgram]:
    """Resolve a competency's `relatedTrainingProgramIds` (FK-by-value, which
    may be program ids OR codes) to TrainingProgram rows."""
    ids = competency.relatedTrainingProgramIds or []
    if not ids:
        return []
    rows = (
        await db.execute(
            select(TrainingProgram).where(
                or_(
                    TrainingProgram.id.in_(ids),
                    TrainingProgram.code.in_(ids),
                    TrainingProgram.programCode.in_(ids),
                )
            )
        )
    ).scalars().all()
    return list(rows)


async def _active_certs(
    db: AsyncSession, user_id: str, programs: list[TrainingProgram]
) -> list[TrainingCertificate]:
    if not programs:
        return []
    prog_ids = [p.id for p in programs]
    rows = (
        await db.execute(
            select(TrainingCertificate)
            .where(TrainingCertificate.userId == user_id)
            .where(TrainingCertificate.programId.in_(prog_ids))
            .where(TrainingCertificate.status.in_(_ACTIVE_CERT_STATES))
        )
    ).scalars().all()
    return list(rows)


def _validity_window(
    certs: list[TrainingCertificate], competency: Competency, now: datetime
) -> tuple[datetime, datetime | None]:
    """The competency is valid only as long as its soonest-expiring required
    training certificate. Falls back to the competency's default validity when
    no certificate carries an expiry (e.g. legacy records)."""
    valid_from = now
    froms = [c.validFrom for c in certs if c.validFrom is not None]
    if froms:
        valid_from = max(froms)

    tos = [c.validTo for c in certs if c.validTo is not None]
    if tos:
        valid_to: datetime | None = min(tos)
    else:
        months = competency.defaultValidityMonths or 12
        valid_to = valid_from + timedelta(days=30 * months)
    return valid_from, valid_to


async def sync_record_from_training(
    db: AsyncSession,
    *,
    record: CompetencyRecord,
    competency: Competency,
    actor_user_id: str,
    trigger: str = "TRAINING_RECEIVER",
) -> bool:
    """Recompute one record's state from training. Returns True if it changed.

    No-op (returns False) for competencies that declare no related training
    programs — their state is owned by assessment / external proof, not here.
    """
    programs = await _required_programs(db, competency)
    required_count = len(programs)
    if required_count == 0:
        return False

    result = await check_user_competencies(db, record.personUserId, programs)
    satisfied_count = len(set(result.satisfied))
    has_expiring = any(getattr(w, "code", "") == "EXPIRING_SOON" for w in result.warnings)
    assessment_required = st.requires_assessment_beyond_training(competency)

    target = st.determine_training_state(
        required_count=required_count,
        satisfied_count=satisfied_count,
        has_expiring=has_expiring,
        assessment_required=assessment_required,
        current_state=record.state,
        current_validation_method=record.currentValidationMethod,
    )
    if target is None or target == record.state:
        return False

    now = datetime.now(timezone.utc)
    old_state = record.state
    certs = await _active_certs(db, record.personUserId, programs)

    record.state = target
    record.lastProgressEventAt = now
    record.updatedByUserId = actor_user_id
    record.requiredValidationsTotal = required_count
    record.requiredValidationsCompleted = satisfied_count
    cert_ids = [c.id for c in certs]
    record.relatedTrainingRecords = sorted(set((record.relatedTrainingRecords or []) + cert_ids))

    if target in (st.VALIDATED_ACTIVE, st.EXPIRING_SOON):
        valid_from, valid_to = _validity_window(certs, competency, now)
        record.currentValidatedAt = now
        record.currentValidatedByUserId = actor_user_id
        record.currentValidationMethod = "training_completion"
        record.validFrom = valid_from
        record.validUntil = valid_to
        record.nextRevalidationDue = valid_to
    elif target in (st.NOT_YET_ATTEMPTED, st.EXPIRED_REVOKED):
        # No longer validated — clear the live validation snapshot.
        if old_state in st.VALIDATED_STATES:
            record.currentValidationMethod = None

    record.versionNumber = (record.versionNumber or 1) + 1
    db.add(
        CompetencyRecordVersion(
            recordId=record.id,
            versionNumber=record.versionNumber,
            snapshot={
                "state": target,
                "validUntil": record.validUntil.isoformat() if record.validUntil else None,
                "requiredValidationsCompleted": satisfied_count,
                "requiredValidationsTotal": required_count,
            },
            changes=[{"field": "state", "from": old_state, "to": target}],
            changeReason=(
                f"Training evidence: {satisfied_count}/{required_count} required "
                f"program(s) currently valid."
            ),
            changeTrigger=trigger,
            createdById=actor_user_id,
        )
    )
    return True


async def sync_person_from_training(
    db: AsyncSession, *, plant_id: str, person_user_id: str, actor_user_id: str
) -> int:
    """Recompute every existing record this person holds in the plant."""
    records = (
        await db.execute(
            select(CompetencyRecord)
            .where(CompetencyRecord.plantId == plant_id)
            .where(CompetencyRecord.personUserId == person_user_id)
        )
    ).scalars().all()
    return await _sync_records(db, records, actor_user_id=actor_user_id)


async def sync_plant_from_training(
    db: AsyncSession, *, plant_id: str, actor_user_id: str
) -> dict[str, Any]:
    """Recompute every record in the plant. Commits once at the end."""
    records = (
        await db.execute(select(CompetencyRecord).where(CompetencyRecord.plantId == plant_id))
    ).scalars().all()
    changed = await _sync_records(db, records, actor_user_id=actor_user_id)
    await db.commit()
    return {"recordsScanned": len(records), "recordsChanged": changed}


async def _sync_records(
    db: AsyncSession, records: list[CompetencyRecord], *, actor_user_id: str = "system"
) -> int:
    comp_cache: dict[str, Competency | None] = {}
    changed = 0
    for rec in records:
        comp = comp_cache.get(rec.competencyId)
        if comp is None and rec.competencyId not in comp_cache:
            comp = await db.get(Competency, rec.competencyId)
            comp_cache[rec.competencyId] = comp
        if comp is None:
            continue
        if await sync_record_from_training(
            db, record=rec, competency=comp, actor_user_id=actor_user_id
        ):
            changed += 1
    return changed
