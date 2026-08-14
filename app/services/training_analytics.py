"""Training analytics — the competency dashboard's whole view model.

The web page assembled this from fourteen separate queries. Beyond the round
trips, the rollups it did in TypeScript (per-plant compliance, contractor
coverage, the expiry pipeline) each encoded a rule — what counts as "covered",
which statuses are still live — that nothing else could see or reuse.

Everything here is computed from the same instant (`now` is passed in), so the
30/60/90 buckets and the 12-month pipeline cannot disagree about where today is.

Contractor coverage is the odd one out: contractors are not Users, so their
training lives in `ContractorWorker.trainingCertificates` as JSON rather than in
the TrainingCertificate table. It is rolled up here anyway, because "are our
people trained" has to span employees AND contractors to mean anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.epc import ContractorCompany, ContractorWorker
from app.models.incident import Incident
from app.models.plant import Plant
from app.models.training import TrainingCertificate, TrainingProgram
from app.models.user import User

DAY = timedelta(days=1)

# Statuses that still represent a live certificate. Anything else is history.
LIVE_CERT_STATUSES = ("ACTIVE", "EXPIRING_SOON")
# Contractor JSON uses free-text status; these mean "no longer valid".
DEAD_CONTRACTOR_STATUSES = {"EXPIRED", "REVOKED", "LAPSED"}


def _month_key(dt: datetime) -> str:
    return f"{dt.year}-{dt.month:02d}"


async def training_analytics(db: AsyncSession, now: datetime) -> dict[str, Any]:
    in30, in60, in90 = now + 30 * DAY, now + 60 * DAY, now + 90 * DAY
    in365 = now + 365 * DAY

    # ── Certificate status mix ───────────────────────────────────────
    status_rows = (
        await db.execute(
            select(TrainingCertificate.status, func.count()).group_by(
                TrainingCertificate.status
            )
        )
    ).all()
    status_counts = {s: int(n) for s, n in status_rows}
    total_certs = sum(status_counts.values())

    statutory_active = int(
        (
            await db.execute(
                select(func.count())
                .select_from(TrainingCertificate)
                .join(TrainingProgram, TrainingProgram.id == TrainingCertificate.programId)
                .where(TrainingCertificate.status == "ACTIVE")
                .where(TrainingProgram.isStatutory.is_(True))
            )
        ).scalar_one()
    )

    async def _expiring_between(start: datetime, end: datetime) -> int:
        return int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(TrainingCertificate)
                    .where(TrainingCertificate.validTo >= start)
                    .where(TrainingCertificate.validTo < end)
                    .where(TrainingCertificate.status.in_(LIVE_CERT_STATUSES))
                )
            ).scalar_one()
        )

    expiring_30 = await _expiring_between(now, in30)
    expiring_60 = await _expiring_between(in30, in60)
    expiring_90 = await _expiring_between(in60, in90)

    # ── Per-plant compliance ─────────────────────────────────────────
    # Certificates carry a user, not a plant, so compliance is grouped through
    # the holder's plant. Aggregated in SQL rather than by pulling every
    # certificate row across the wire, which is what the page used to do.
    plants = (
        await db.execute(select(Plant.id, Plant.name, Plant.code).order_by(Plant.name))
    ).all()
    plant_rows = (
        await db.execute(
            select(User.plantId, TrainingCertificate.status, func.count())
            .select_from(TrainingCertificate)
            .join(User, User.id == TrainingCertificate.userId)
            .where(User.plantId.is_not(None))
            .group_by(User.plantId, TrainingCertificate.status)
        )
    ).all()
    plant_stats: dict[str, dict[str, int]] = {
        pid: {"active": 0, "total": 0} for pid, _n, _c in plants
    }
    for plant_id, cert_status, n in plant_rows:
        slot = plant_stats.setdefault(plant_id, {"active": 0, "total": 0})
        slot["total"] += int(n)
        if cert_status == "ACTIVE":
            slot["active"] += int(n)

    # ── Top programmes by issuance ───────────────────────────────────
    top_rows = (
        await db.execute(
            select(TrainingCertificate.programId, func.count().label("n"))
            .group_by(TrainingCertificate.programId)
            .order_by(func.count().desc())
            .limit(8)
        )
    ).all()

    programs = (
        await db.execute(
            select(TrainingProgram)
            .where(TrainingProgram.approvalStatus == "APPROVED")
            .where(TrainingProgram.isActive.is_(True))
        )
    ).scalars().all()
    program_by_id = {p.id: p for p in programs}

    top_programs = []
    for program_id, n in top_rows:
        p = program_by_id.get(program_id)
        if p is None:
            continue  # retired/unapproved programme — not shown on this screen
        top_programs.append(
            {
                "id": p.id,
                "name": p.programName or p.name,
                "code": p.programCode or p.code,
                "isStatutory": p.isStatutory,
                "gates": [
                    g
                    for g in (
                        "PTW" if p.blocksPtwIfMissing else None,
                        "Role" if p.blocksRoleAssignmentIfMissing else None,
                        "Contractor" if p.blocksContractorOnboardingIfMissing else None,
                    )
                    if g
                ],
                "count": int(n),
            }
        )

    # ── Effectiveness ────────────────────────────────────────────────
    eff = (
        await db.execute(
            select(
                func.count(TrainingCertificate.effectivenessRating),
                func.avg(TrainingCertificate.effectivenessRating),
            ).where(TrainingCertificate.effectivenessReviewedAt.is_not(None))
        )
    ).one()
    reviewed_count = int(eff[0] or 0)
    avg_rating = float(eff[1]) if eff[1] is not None else 0.0

    # ── Expiry pipeline, next 12 months ──────────────────────────────
    pipeline_rows = (
        await db.execute(
            select(TrainingCertificate.validTo)
            .where(TrainingCertificate.validTo >= now)
            .where(TrainingCertificate.validTo < in365)
            .where(TrainingCertificate.status.in_(LIVE_CERT_STATUSES))
        )
    ).all()
    buckets: dict[str, int] = {}
    for i in range(12):
        total = now.year * 12 + (now.month - 1) + i
        buckets[f"{total // 12}-{total % 12 + 1:02d}"] = 0
    for (valid_to,) in pipeline_rows:
        if valid_to is None:
            continue
        key = _month_key(valid_to)
        if key in buckets:
            buckets[key] += 1
    expiry_pipeline = [{"month": k, "count": v} for k, v in buckets.items()]

    # ── Incidents that triggered training ────────────────────────────
    incident_rows = (
        await db.execute(
            select(
                Incident.id,
                Incident.number,
                Incident.date,
                Incident.triggeredTrainingFor,
                Incident.triggeredTrainingKeywords,
            )
            .where(Incident.triggeredTrainingFor.is_not(None))
            .order_by(Incident.date.desc())
            .limit(5)
        )
    ).all()
    triggered_incidents = [
        {
            "id": i_id,
            "number": number,
            "date": date,
            "triggeredTrainingFor": trained_for or [],
            "triggeredTrainingKeywords": keywords or [],
        }
        for i_id, number, date, trained_for, keywords in incident_rows
        if trained_for
    ]

    # ── Contractor coverage ──────────────────────────────────────────
    worker_rows = (
        await db.execute(
            select(ContractorWorker.trainingCertificates, ContractorCompany.name)
            .outerjoin(
                ContractorCompany,
                ContractorCompany.id == ContractorWorker.contractorCompanyId,
            )
        )
    ).all()
    horizon = now + 30 * DAY
    coverage: dict[str, dict[str, Any]] = {}
    for certs_json, company_name in worker_rows:
        company = company_name or "Unknown"
        slot = coverage.setdefault(
            company, {"company": company, "total": 0, "covered": 0, "expiring": 0}
        )
        slot["total"] += 1
        certs = certs_json if isinstance(certs_json, list) else []
        valid = []
        for c in certs:
            if not isinstance(c, dict):
                continue
            if str(c.get("status", "")).upper() in DEAD_CONTRACTOR_STATUSES:
                continue
            valid_until = c.get("validUntil")
            if not valid_until:
                valid.append(c)  # no expiry recorded → treated as still valid
                continue
            try:
                parsed = datetime.fromisoformat(str(valid_until).replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=now.tzinfo)
            if parsed >= now:
                valid.append({**c, "_parsed": parsed})
        if valid:
            slot["covered"] += 1
        if any(
            isinstance(c, dict) and c.get("_parsed") and c["_parsed"] <= horizon
            for c in valid
        ):
            slot["expiring"] += 1
    contractor_coverage = sorted(
        ({k: v for k, v in c.items()} for c in coverage.values()),
        key=lambda c: c["total"],
        reverse=True,
    )
    contractor_total = sum(c["total"] for c in contractor_coverage)
    contractor_covered = sum(c["covered"] for c in contractor_coverage)

    return {
        "statusCounts": status_counts,
        "totalCerts": total_certs,
        "activePct": round(status_counts.get("ACTIVE", 0) / total_certs * 100) if total_certs else 0,
        "statutoryActive": statutory_active,
        "expiring30": expiring_30,
        "expiring60": expiring_60,
        "expiring90": expiring_90,
        "plants": [
            {
                "id": pid,
                "name": name,
                "code": code,
                "active": plant_stats.get(pid, {}).get("active", 0),
                "total": plant_stats.get(pid, {}).get("total", 0),
            }
            for pid, name, code in plants
        ],
        "topPrograms": top_programs,
        "effectiveness": {"reviewedCount": reviewed_count, "avgRating": round(avg_rating, 2)},
        "expiryPipeline": expiry_pipeline,
        "triggeredIncidents": triggered_incidents,
        "contractorCoverage": contractor_coverage,
        "contractorPct": round(contractor_covered / contractor_total * 100)
        if contractor_total
        else 0,
    }
