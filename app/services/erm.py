"""ERM domain services — scoring, banding, rollup engine, escalation,
review-cycle math, snapshots, and shared query helpers.

Pure functions where possible; DB-touching helpers take an AsyncSession.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.eai import EaiEntry, EaiStudy
from app.models.erm import (
    EnterpriseRisk,
    ReviewCycleConfig,
    RiskAssessment,
    RiskCategory,
    RollupLinkage,
    RollupRule,
    ScoringMatrixConfig,
)
from app.models.hira import HiraEntry, HiraStudy
from app.models.user import User

# ─────────────────────────────────────────────────────────────────────
# Banding — default Meridian Standard 5×5 thresholds. The active matrix's
# ratingBands override these when present.
# ─────────────────────────────────────────────────────────────────────
DEFAULT_BANDS = [
    {"name": "LOW", "minScore": 1, "maxScore": 4, "colorHex": "#2E8B57"},
    {"name": "MEDIUM", "minScore": 5, "maxScore": 9, "colorHex": "#E6A817"},
    {"name": "HIGH", "minScore": 10, "maxScore": 15, "colorHex": "#E67E22"},
    {"name": "CRITICAL", "minScore": 16, "maxScore": 25, "colorHex": "#C0392B"},
]

# Map foreign / HIRA-style level strings onto the ERM 4-band scale.
_LEVEL_ALIAS = {
    "LOW": "LOW",
    "MEDIUM": "MEDIUM",
    "MODERATE": "MEDIUM",
    "HIGH": "HIGH",
    "SIGNIFICANT": "HIGH",
    "CRITICAL": "CRITICAL",
    "MAJOR": "CRITICAL",
    "CATASTROPHIC": "CRITICAL",
}
_BAND_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

IMPACT_DIMENSIONS = [
    "FINANCIAL",
    "SAFETY",
    "REPUTATIONAL",
    "REGULATORY",
    "BUSINESS_INTERRUPTION",
]


def band_for_score(score: int, bands: list[dict[str, Any]] | None = None) -> str:
    bands = bands or DEFAULT_BANDS
    for b in bands:
        if b["minScore"] <= score <= b["maxScore"]:
            return b["name"]
    # Above the top band → highest defined band.
    return bands[-1]["name"] if bands else "CRITICAL"


def normalise_band(level: str | None) -> str | None:
    if not level:
        return None
    return _LEVEL_ALIAS.get(level.upper(), level.upper())


def dominant_dimension(impact_scores: list[dict[str, Any]]) -> tuple[str, int]:
    """Returns (dominantDimension, overallImpact=max level). Conservative: MAX,
    not average. Ties resolved by IMPACT_DIMENSIONS order (FINANCIAL first)."""
    if not impact_scores:
        return ("FINANCIAL", 1)
    best_dim = impact_scores[0]["dimension"]
    best_level = 0
    for dim in IMPACT_DIMENSIONS:
        for s in impact_scores:
            if s["dimension"] == dim and int(s["level"]) > best_level:
                best_level = int(s["level"])
                best_dim = dim
    if best_level == 0:  # dimensions outside the canonical list
        for s in impact_scores:
            if int(s["level"]) > best_level:
                best_level = int(s["level"])
                best_dim = s["dimension"]
    return (best_dim, best_level)


def factor_score(score: int) -> tuple[int, int]:
    """Factor a 1..25 total score into a plausible (likelihood, impact) pair on
    the 5×5 grid. Used to place rollup-derived risks on the heat map when only a
    composite child score is known. Prefers near-square factorisation."""
    score = max(1, min(25, score))
    best = (1, score if score <= 5 else 5)
    best_gap = 999
    for impact in range(1, 6):
        for likelihood in range(1, 6):
            prod = likelihood * impact
            gap = abs(prod - score)
            # Prefer exact product, then larger impact (conservative).
            rank = (gap, -impact)
            if rank < (best_gap, -best[1]):
                best_gap = gap
                best = (likelihood, impact)
    return best


async def get_active_matrix(db: AsyncSession) -> ScoringMatrixConfig | None:
    row = (
        await db.execute(
            select(ScoringMatrixConfig)
            .where(ScoringMatrixConfig.isActive.is_(True))
            .where(ScoringMatrixConfig.isDeleted.is_(False))
            .order_by(ScoringMatrixConfig.isDefault.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def bands_from_active_matrix(db: AsyncSession) -> list[dict[str, Any]]:
    m = await get_active_matrix(db)
    if m and m.ratingBands:
        return m.ratingBands
    return DEFAULT_BANDS


# ─────────────────────────────────────────────────────────────────────
# Assessment recompute — denormalise current scores onto EnterpriseRisk
# ─────────────────────────────────────────────────────────────────────
async def recompute_risk_scores(db: AsyncSession, risk: EnterpriseRisk) -> None:
    """Pull the current INHERENT / RESIDUAL assessments and cache their scores
    onto the risk row for fast register / heat-map rendering."""
    bands = await bands_from_active_matrix(db)
    rows = (
        await db.execute(
            select(RiskAssessment)
            .where(RiskAssessment.riskId == risk.id)
            .where(RiskAssessment.isCurrent.is_(True))
        )
    ).scalars().all()
    risk.inherentScore = risk.inherentBand = None
    risk.inherentLikelihood = risk.inherentImpact = None
    risk.residualScore = risk.residualBand = None
    risk.residualLikelihood = risk.residualImpact = None
    for a in rows:
        if a.assessmentType == "INHERENT":
            risk.inherentLikelihood = a.likelihood
            risk.inherentImpact = a.overallImpact
            risk.inherentScore = a.totalScore
            risk.inherentBand = a.ratingBand
        elif a.assessmentType == "RESIDUAL":
            risk.residualLikelihood = a.likelihood
            risk.residualImpact = a.overallImpact
            risk.residualScore = a.totalScore
            risk.residualBand = band_for_score(a.totalScore, bands)


def review_overdue_days(next_review: datetime | None, now: datetime | None = None) -> int:
    if not next_review:
        return 0
    now = now or datetime.now(timezone.utc)
    if next_review.tzinfo is None:
        next_review = next_review.replace(tzinfo=timezone.utc)
    delta = (now - next_review).days
    return max(0, delta)


def review_badge(overdue_days: int) -> str | None:
    if overdue_days > 30:
        return "RED"
    if overdue_days > 15:
        return "AMBER"
    return None


async def next_review_date_for_band(
    db: AsyncSession, band: str | None, from_date: datetime | None = None
) -> datetime:
    """Resolve the review cadence for a band and return the next review date."""
    from_date = from_date or datetime.now(timezone.utc)
    defaults = {"LOW": 365, "MEDIUM": 180, "HIGH": 90, "CRITICAL": 30}
    days = defaults.get((band or "MEDIUM").upper(), 180)
    if band:
        cfg = (
            await db.execute(
                select(ReviewCycleConfig).where(ReviewCycleConfig.ratingBand == band.upper())
            )
        ).scalar_one_or_none()
        if cfg:
            days = cfg.reviewFrequencyDays
    return from_date + timedelta(days=days)


# ─────────────────────────────────────────────────────────────────────
# Escalation
# ─────────────────────────────────────────────────────────────────────
async def maybe_escalate(db: AsyncSession, risk: EnterpriseRisk) -> bool:
    """Apply Phase-1 escalation rules after a (re)assessment. Returns True if the
    risk was escalated. Caller commits."""
    escalate = False
    if risk.residualBand == "CRITICAL":
        escalate = True
    if (
        risk.appetiteThreshold is not None
        and risk.residualScore is not None
        and risk.residualScore > risk.appetiteThreshold
    ):
        escalate = True
    if escalate and risk.lifecycleState not in ("ESCALATED", "CLOSED"):
        risk.lifecycleState = "ESCALATED"
        risk.escalatedAt = datetime.now(timezone.utc)
        await notify_escalation(db, risk)
    return escalate


async def notify_escalation(db: AsyncSession, risk: EnterpriseRisk) -> None:
    """Best-effort email notification to CRO + Risk Champion. Never raises —
    notification failure must not block the transaction."""
    try:
        from app.models.user import Role, UserRole
        from app.services.notifications import send_email

        emails: list[str] = []
        champ = await db.get(User, risk.riskChampionId)
        if champ and champ.email:
            emails.append(champ.email)
        cro_emails = (
            await db.execute(
                select(User.email)
                .join(UserRole, UserRole.userId == User.id)
                .join(Role, Role.id == UserRole.roleId)
                .where(Role.code == "CRO")
            )
        ).scalars().all()
        recipients = list({e for e in (emails + list(cro_emails)) if e})
        if recipients:
            await send_email(
                recipients,
                subject=f"[ERM] Risk escalated: {risk.riskCode} — {risk.title}",
                body=(
                    f"Risk {risk.riskCode} '{risk.title}' has breached the escalation "
                    f"threshold (residual band {risk.residualBand}, score "
                    f"{risk.residualScore}). Awaiting Risk Management Committee decision."
                ),
            )
    except Exception:
        return


# ─────────────────────────────────────────────────────────────────────
# Rollup engine
# ─────────────────────────────────────────────────────────────────────
async def run_rollup_rule(
    db: AsyncSession, rule: RollupRule, actor_id: str | None = None
) -> dict[str, int | list[str]]:
    """Execute one rollup rule against the live HIRA / EAI registers and keep the
    derived EnterpriseRisk(s) in sync. Returns a run summary."""
    crit = rule.filterCriteria or {}
    site_ids = crit.get("siteIds") or None
    min_band = crit.get("minRiskBand")  # HIGH | CRITICAL | None
    modules = crit.get("sourceModules") or ["HIRA", "EAI"]
    min_rank = _BAND_RANK.get((min_band or "").upper(), -1)

    matched: list[dict[str, Any]] = []

    if "HIRA" in modules:
        q = (
            select(HiraEntry, HiraStudy)
            .join(HiraStudy, HiraStudy.id == HiraEntry.studyId)
            .where(HiraEntry.isCurrentVersion.is_(True))
        )
        if site_ids:
            q = q.where(HiraStudy.plantId.in_(site_ids))
        for entry, study in (await db.execute(q)).all():
            nb = normalise_band(entry.residualRiskLevel)
            if min_rank >= 0 and _BAND_RANK.get(nb or "", -1) < min_rank:
                continue
            matched.append(
                {
                    "id": entry.id,
                    "module": "HIRA",
                    "plantId": study.plantId,
                    "activity": entry.activityDescription,
                    "residualScore": entry.residualRiskScore or 0,
                    "initialScore": entry.initialRiskScore or 0,
                    "band": nb,
                }
            )

    if "EAI" in modules:
        q = (
            select(EaiEntry, EaiStudy)
            .join(EaiStudy, EaiStudy.id == EaiEntry.studyId)
            .where(EaiEntry.isCurrentVersion.is_(True))
        )
        if site_ids:
            q = q.where(EaiStudy.plantId.in_(site_ids))
        for entry, study in (await db.execute(q)).all():
            nb = normalise_band(entry.residualImpactLevel)
            if min_rank >= 0 and _BAND_RANK.get(nb or "", -1) < min_rank:
                continue
            matched.append(
                {
                    "id": entry.id,
                    "module": "EAI",
                    "plantId": study.plantId,
                    "activity": entry.activityDescription,
                    "residualScore": entry.residualImpactScore or 0,
                    "initialScore": entry.initialImpactScore or 0,
                    "band": nb,
                }
            )

    created = updated = unlinked = 0
    touched: list[str] = []
    now = datetime.now(timezone.utc)

    if rule.aggregationMode == "GROUPED":
        groups: dict[str | None, list[dict[str, Any]]] = {None: matched}
        for key, entries in groups.items():
            er, was_created = await _sync_grouped_risk(db, rule, entries, actor_id, now)
            if er is None:
                continue
            touched.append(er.id)
            created += 1 if was_created else 0
            updated += 0 if was_created else 1
            unlinked += await _reconcile_linkages(db, rule, er, entries)
    else:  # ONE_TO_ONE
        for e in matched:
            er, was_created = await _sync_one_to_one_risk(db, rule, e, actor_id, now)
            if er is None:
                continue
            touched.append(er.id)
            created += 1 if was_created else 0
            updated += 0 if was_created else 1

    rule.lastRunAt = now
    rule.lastRunSummary = {
        "created": created,
        "updated": updated,
        "unlinked": unlinked,
        "matched": len(matched),
    }
    await db.flush()
    return {
        "created": created,
        "updated": updated,
        "unlinked": unlinked,
        "matched": len(matched),
        "enterpriseRiskIds": touched,
    }


async def _category_id_for_code(db: AsyncSession, code: str) -> str | None:
    return (
        await db.execute(select(RiskCategory.id).where(RiskCategory.code == code))
    ).scalar_one_or_none()


async def _next_risk_code(db: AsyncSession) -> str:
    year = datetime.now(timezone.utc).year
    count = (
        await db.execute(select(func.count(EnterpriseRisk.id)))
    ).scalar_one() or 0
    return f"ERM-{year}-{(count + 1):04d}"


async def _sync_grouped_risk(
    db: AsyncSession,
    rule: RollupRule,
    entries: list[dict[str, Any]],
    actor_id: str | None,
    now: datetime,
) -> tuple[EnterpriseRisk | None, bool]:
    bands = await bands_from_active_matrix(db)
    cat_id = await _category_id_for_code(db, rule.targetCategoryCode)
    if not cat_id:
        return None, False

    # Aggregate child scores → parent score.
    if rule.scoringMode == "WEIGHTED_AVERAGE" and entries:
        res_score = round(sum(e["residualScore"] for e in entries) / len(entries))
        init_score = round(sum(e["initialScore"] for e in entries) / len(entries))
    else:  # MAX (default)
        res_score = max((e["residualScore"] for e in entries), default=0)
        init_score = max((e["initialScore"] for e in entries), default=0)

    er = (
        await db.execute(
            select(EnterpriseRisk).where(EnterpriseRisk.rollupRuleId == rule.id)
        )
    ).scalar_one_or_none()
    was_created = er is None
    if er is None:
        er = EnterpriseRisk(
            riskCode=await _next_risk_code(db),
            title=f"Aggregated operational risk — {rule.name}",
            description=f"Auto-aggregated from the Combined Risk Register by rollup rule '{rule.name}'.",
            categoryId=cat_id,
            orgLevel="ENTERPRISE",
            riskOwnerId=actor_id or "SYSTEM",
            riskChampionId=actor_id or "SYSTEM",
            lifecycleState="MONITORING",
            velocity="MODERATE",
            sourceType="HSE_ROLLUP",
            rollupRuleId=rule.id,
            identifiedDate=now,
            nextReviewDate=now + timedelta(days=90),
            createdBy=actor_id,
        )
        db.add(er)
        await db.flush()

    if init_score:
        l, i = factor_score(init_score)
        er.inherentLikelihood, er.inherentImpact = l, i
        er.inherentScore = init_score
        er.inherentBand = band_for_score(init_score, bands)
    if res_score:
        l, i = factor_score(res_score)
        er.residualLikelihood, er.residualImpact = l, i
        er.residualScore = res_score
        er.residualBand = band_for_score(res_score, bands)
    er.updatedBy = actor_id
    await db.flush()
    return er, was_created


async def _sync_one_to_one_risk(db, rule, entry, actor_id, now):  # pragma: no cover (seldom used)
    bands = await bands_from_active_matrix(db)
    cat_id = await _category_id_for_code(db, rule.targetCategoryCode)
    if not cat_id:
        return None, False
    existing = (
        await db.execute(
            select(EnterpriseRisk)
            .join(RollupLinkage, RollupLinkage.enterpriseRiskId == EnterpriseRisk.id)
            .where(RollupLinkage.sourceRegisterEntryId == entry["id"])
            .where(EnterpriseRisk.rollupRuleId == rule.id)
        )
    ).scalar_one_or_none()
    was_created = existing is None
    if existing is None:
        existing = EnterpriseRisk(
            riskCode=await _next_risk_code(db),
            title=f"{entry['module']} risk — {entry['activity'][:120]}",
            description=entry["activity"],
            categoryId=cat_id,
            orgLevel="SITE",
            plantId=entry["plantId"],
            riskOwnerId=actor_id or "SYSTEM",
            riskChampionId=actor_id or "SYSTEM",
            lifecycleState="MONITORING",
            sourceType="HSE_ROLLUP",
            rollupRuleId=rule.id,
            identifiedDate=now,
            nextReviewDate=now + timedelta(days=90),
            createdBy=actor_id,
        )
        db.add(existing)
        await db.flush()
    rs = entry["residualScore"]
    if rs:
        l, i = factor_score(rs)
        existing.residualLikelihood, existing.residualImpact = l, i
        existing.residualScore = rs
        existing.residualBand = band_for_score(rs, bands)
    # link
    await _upsert_linkage(db, rule, existing, entry)
    return existing, was_created


async def _upsert_linkage(db, rule, er, entry) -> None:
    existing = (
        await db.execute(
            select(RollupLinkage)
            .where(RollupLinkage.enterpriseRiskId == er.id)
            .where(RollupLinkage.sourceRegisterEntryId == entry["id"])
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            RollupLinkage(
                enterpriseRiskId=er.id,
                rollupRuleId=rule.id,
                sourceRegisterEntryId=entry["id"],
                sourceModule=entry["module"],
                sourceRef=entry["activity"][:200],
                contributingScore=entry["residualScore"],
                contributingBand=entry["band"],
            )
        )
    else:
        existing.contributingScore = entry["residualScore"]
        existing.contributingBand = entry["band"]
        existing.sourceRef = entry["activity"][:200]


async def _reconcile_linkages(
    db: AsyncSession, rule: RollupRule, er: EnterpriseRisk, entries: list[dict[str, Any]]
) -> int:
    """Add linkages for matched entries; remove linkages whose source entry no
    longer matches. Returns count unlinked."""
    matched_ids = {e["id"] for e in entries}
    existing = (
        await db.execute(
            select(RollupLinkage).where(RollupLinkage.enterpriseRiskId == er.id)
        )
    ).scalars().all()
    existing_ids = {l.sourceRegisterEntryId for l in existing}
    unlinked = 0
    for l in existing:
        if l.sourceRegisterEntryId not in matched_ids:
            await db.delete(l)
            unlinked += 1
    for e in entries:
        if e["id"] not in existing_ids:
            await _upsert_linkage(db, rule, er, e)
        else:
            for l in existing:
                if l.sourceRegisterEntryId == e["id"]:
                    l.contributingScore = e["residualScore"]
                    l.contributingBand = e["band"]
    return unlinked


# ─────────────────────────────────────────────────────────────────────
# Snapshots
# ─────────────────────────────────────────────────────────────────────
async def take_snapshot(db: AsyncSession, quarter_label: str) -> int:
    """Persist a full register snapshot for the given quarter. Idempotent per
    (quarter, risk)."""
    from app.models.erm import ErmRiskSnapshot

    now = datetime.now(timezone.utc)
    cat_by_id = {
        c.id: c.code
        for c in (await db.execute(select(RiskCategory))).scalars().all()
    }
    risks = (
        await db.execute(
            select(EnterpriseRisk).where(EnterpriseRisk.isDeleted.is_(False))
        )
    ).scalars().all()
    count = 0
    for r in risks:
        existing = (
            await db.execute(
                select(ErmRiskSnapshot)
                .where(ErmRiskSnapshot.quarterLabel == quarter_label)
                .where(ErmRiskSnapshot.riskId == r.id)
            )
        ).scalar_one_or_none()
        if existing:
            continue
        db.add(
            ErmRiskSnapshot(
                quarterLabel=quarter_label,
                snapshotDate=now,
                riskId=r.id,
                riskCode=r.riskCode,
                categoryCode=cat_by_id.get(r.categoryId, ""),
                inherentScore=r.inherentScore,
                inherentBand=r.inherentBand,
                residualScore=r.residualScore,
                residualBand=r.residualBand,
                likelihood=r.residualLikelihood,
                overallImpact=r.residualImpact,
                lifecycleState=r.lifecycleState,
            )
        )
        count += 1
    await db.flush()
    return count


# ─────────────────────────────────────────────────────────────────────
# Name resolution helper
# ─────────────────────────────────────────────────────────────────────
async def user_name_map(db: AsyncSession, user_ids: Iterable[str]) -> dict[str, str]:
    ids = [u for u in set(user_ids) if u and u != "SYSTEM"]
    if not ids:
        return {}
    rows = (await db.execute(select(User.id, User.name).where(User.id.in_(ids)))).all()
    return {r[0]: r[1] for r in rows}


ACTIVE_STATES = (
    "DRAFT",
    "SUBMITTED",
    "ASSESSED",
    "TREATMENT_ACTIVE",
    "MONITORING",
    "ACCEPTED",
    "ESCALATED",
)
