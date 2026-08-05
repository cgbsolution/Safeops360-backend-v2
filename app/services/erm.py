"""ERM domain services — scoring, banding, rollup engine, escalation,
review-cycle math, snapshots, and shared query helpers.

Pure functions where possible; DB-touching helpers take an AsyncSession.
"""

from __future__ import annotations

import math
import random
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

# ─────────────────────────────────────────────────────────────────────
# ADVANCED quantification — monetary expected loss + control-derived residual
# ─────────────────────────────────────────────────────────────────────
# Ordinal likelihood 1..5 → default annualised probability (%). Used when an
# assessor scores on the 5×5 grid without entering an explicit probability.
_LIKELIHOOD_PCT = {1: 2.0, 2: 10.0, 3: 30.0, 4: 60.0, 5: 90.0}

# Control effectiveness model (barriers-in-series), CALIBRATED from evidence.
# Per-control effectiveness = strength-ceiling × design-factor × operating-factor:
#   • strength-ceiling — the most a control of this role can reduce risk (design intent)
#   • design-factor    — is it adequately DESIGNED (latest DESIGN test rating)
#   • operating-factor — is it OPERATING effectively, BACK-TESTED from the latest
#                        OPERATING test's exception rate (1 − exceptions/sample)
# An untested control earns no operating credit and an asserted-but-unevidenced one
# is penalised — a CRO will not let an unproven control reduce residual.
CONTROL_STRENGTH_CEILING = {"PRIMARY": 0.50, "SECONDARY": 0.25, "COMPENSATING": 0.15}
CONTROL_DESIGN_FACTOR = {"EFFECTIVE": 1.0, "DEFICIENT": 0.30, "NOT_ASSESSED": 0.0}
# Operating fallback when NO operating test exists (no exception data to back-test).
_OPERATING_FALLBACK = {"EFFECTIVE": 0.85, "DEFICIENT": 0.20, "NOT_ASSESSED": 0.0}
# Severe operating conclusions cap the back-tested operating factor regardless of sample.
_OPERATING_CONCLUSION_CAP = {"SIGNIFICANT_DEFICIENCY": 0.30, "MATERIAL_WEAKNESS": 0.10, "DEFICIENT": 0.50}
# Preventive/directive controls reduce LIKELIHOOD; detective/corrective reduce IMPACT.
_PREVENTIVE_TYPES = {"PREVENTIVE", "DIRECTIVE"}
# Controls never eliminate risk — cap the reduction on each axis (residual floor).
MAX_AXIS_REDUCTION = 0.80
# A residual asserted this many score-points BELOW the control-derived residual is a
# material optimistic override — it needs approver authority + a written rationale.
OVERRIDE_TOLERANCE = 4
# Back-compat aliases (older seed/QA referenced these names).
_STRENGTH_W = CONTROL_STRENGTH_CEILING
_RATING_F = {"EFFECTIVE": 1.0, "DEFICIENT": 0.20, "NOT_ASSESSED": 0.0}


def _aware_dt(d: "datetime | None") -> "datetime | None":
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def operating_factor_from_test(conclusion: str, sample_size: int | None, exceptions: int | None) -> float:
    """Back-test a control's operating effectiveness from its test exception rate:
    1 − exceptions/sample, then capped by the conclusion's severity. This replaces a
    flat 1.0-for-EFFECTIVE with evidence — the CRO's 'prove it works' answer."""
    ss = max(1, int(sample_size or 1))
    ex = max(0, int(exceptions or 0))
    factor = 1.0 - min(1.0, ex / ss)
    cap = _OPERATING_CONCLUSION_CAP.get(conclusion)
    if cap is not None:
        factor = min(factor, cap)
    return round(max(0.0, factor), 3)


def default_likelihood_pct(likelihood: int | None) -> float:
    return _LIKELIHOOD_PCT.get(int(likelihood or 0), 30.0)


def expected_loss(likelihood_pct: float | None, financial_expected: float | None) -> float | None:
    if likelihood_pct is None or financial_expected is None:
        return None
    return round(likelihood_pct / 100.0 * financial_expected)


def unexpected_loss(
    likelihood_pct: float | None, financial_expected: float | None, financial_worst: float | None
) -> float | None:
    """Tail proxy — annualised exposure to the worst-case beyond expected."""
    if likelihood_pct is None or financial_expected is None or financial_worst is None:
        return None
    return round(likelihood_pct / 100.0 * max(0.0, financial_worst - financial_expected))


def _series(terms: list[float]) -> float:
    """Combine independent barrier effectivenesses in series: 1 − Π(1 − eᵢ),
    capped at MAX_AXIS_REDUCTION. Diminishing returns — two 50% controls give
    75%, not 100%."""
    prod = 1.0
    for e in terms:
        prod *= 1.0 - max(0.0, min(1.0, e))
    return round(min(MAX_AXIS_REDUCTION, 1.0 - prod), 4)


async def control_effectiveness(db: AsyncSession, risk_id: str) -> dict[str, Any]:
    """Aggregate the effectiveness of controls MAPPED to this risk, split into a
    preventive component (cuts likelihood) and a mitigating component (cuts impact),
    plus a combined factor for monetary expected-loss attenuation.

    Returns: {preventive, mitigating, combined, mappedCount, ratedCount, contributing[]}.
    Pure read; no writes. Imports the Tier-3 controls register lazily to avoid a
    hard module dependency when Tier 3 isn't present.
    """
    from app.models.erm_t3 import Control, ControlTest, RiskControlMapping

    maps = (
        await db.execute(select(RiskControlMapping).where(RiskControlMapping.riskId == risk_id))
    ).scalars().all()
    prev_terms: list[float] = []
    mit_terms: list[float] = []
    contributing: list[dict[str, Any]] = []
    rated = back_tested = 0
    for m in maps:
        ctrl = await db.get(Control, m.controlId)
        if not ctrl or not ctrl.isActive or getattr(ctrl, "isDeleted", False):
            continue
        # Design adequacy from the latest DESIGN test rating.
        design_rating = ctrl.currentDesignRating or "NOT_ASSESSED"
        design_f = CONTROL_DESIGN_FACTOR.get(design_rating, 0.0)
        # Operating effectiveness — BACK-TESTED from the latest OPERATING test's
        # exception rate; fall back to the rating label (penalised) if untested.
        op_tests = (
            await db.execute(
                select(ControlTest)
                .where(ControlTest.controlId == ctrl.id)
                .where(ControlTest.testType == "OPERATING")
            )
        ).scalars().all()
        if op_tests:
            latest = max(op_tests, key=lambda t: _aware_dt(t.testDate) or datetime.min.replace(tzinfo=timezone.utc))
            op_f = operating_factor_from_test(latest.conclusion, latest.sampleSize, latest.exceptionsFound)
            op_basis = f"{latest.exceptionsFound}/{latest.sampleSize} exceptions ({latest.conclusion})"
            is_back_tested = True
        else:
            op_rating = ctrl.currentOperatingRating or "NOT_ASSESSED"
            op_f = _OPERATING_FALLBACK.get(op_rating, 0.0)
            op_basis = f"no operating test — fallback ({op_rating})"
            is_back_tested = False
        ceiling = CONTROL_STRENGTH_CEILING.get(m.mitigationStrength, 0.15)
        e = ceiling * design_f * op_f
        is_preventive = ctrl.controlType in _PREVENTIVE_TYPES
        if e > 0:
            rated += 1
            if is_back_tested:
                back_tested += 1
            (prev_terms if is_preventive else mit_terms).append(e)
        contributing.append(
            {
                "controlId": ctrl.id,
                "controlCode": ctrl.controlCode,
                "name": ctrl.name,
                "controlType": ctrl.controlType,
                "mitigationStrength": m.mitigationStrength,
                "rating": ctrl.currentOperatingRating or ctrl.currentDesignRating or "NOT_ASSESSED",
                "designFactor": round(design_f, 3),
                "operatingFactor": round(op_f, 3),
                "operatingBasis": op_basis,
                "backTested": is_back_tested,
                "axis": "LIKELIHOOD" if is_preventive else "IMPACT",
                "contribution": round(e, 3),
            }
        )
    preventive = _series(prev_terms)
    mitigating = _series(mit_terms)
    combined = round(1.0 - (1.0 - preventive) * (1.0 - mitigating), 4)
    return {
        "preventive": preventive,
        "mitigating": mitigating,
        "combined": combined,
        "mappedCount": len(maps),
        "ratedCount": rated,
        "backTestedCount": back_tested,
        "contributing": contributing,
    }


def derive_residual_from_controls(
    inherent_likelihood: int | None,
    inherent_impact: int | None,
    eff: dict[str, Any],
    bands: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Residual implied PURELY by mapped control effectiveness: preventive controls
    attenuate likelihood, mitigating controls attenuate impact. Floors each axis at 1."""
    il = int(inherent_likelihood or 1)
    ii = int(inherent_impact or 1)
    rl = max(1, round(il * (1.0 - eff.get("preventive", 0.0))))
    ri = max(1, round(ii * (1.0 - eff.get("mitigating", 0.0))))
    score = rl * ri
    return {
        "likelihood": rl,
        "impact": ri,
        "score": score,
        "band": band_for_score(score, bands or DEFAULT_BANDS),
    }


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
    onto the risk row for fast register / heat-map rendering. Also denormalises
    monetary exposure (₹ expected / worst loss) and the control-DERIVED residual
    (residual implied purely by mapped control effectiveness) so the register can
    prove residual is derived, not guessed."""
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
    risk.inherentExpectedLossInr = risk.inherentWorstLossInr = None
    risk.residualExpectedLossInr = risk.residualWorstLossInr = None
    for a in rows:
        if a.assessmentType == "INHERENT":
            risk.inherentLikelihood = a.likelihood
            risk.inherentImpact = a.overallImpact
            risk.inherentScore = a.totalScore
            risk.inherentBand = a.ratingBand
            risk.inherentExpectedLossInr = a.expectedLossInr
            risk.inherentWorstLossInr = a.financialWorstInr
        elif a.assessmentType == "RESIDUAL":
            risk.residualLikelihood = a.likelihood
            risk.residualImpact = a.overallImpact
            risk.residualScore = a.totalScore
            risk.residualBand = band_for_score(a.totalScore, bands)
            risk.residualExpectedLossInr = a.expectedLossInr
            risk.residualWorstLossInr = a.financialWorstInr

    # Control-DERIVED residual (independent of the asserted residual) + override
    # variance — this is what lets the register prove residual falls out of controls.
    eff = await control_effectiveness(db, risk.id)
    risk.controlEffectivenessPct = round(eff["combined"] * 100.0, 1)
    if risk.inherentLikelihood and risk.inherentImpact:
        d = derive_residual_from_controls(risk.inherentLikelihood, risk.inherentImpact, eff, bands)
        risk.derivedResidualScore = d["score"]
        risk.derivedResidualBand = d["band"]
        if risk.residualScore is not None:
            risk.residualOverrideVariance = risk.residualScore - d["score"]
            risk.residualIsOverride = abs(risk.residualOverrideVariance) >= 1
    else:
        risk.derivedResidualScore = risk.derivedResidualBand = None
        risk.residualOverrideVariance = None
        risk.residualIsOverride = False


def set_target(
    risk: EnterpriseRisk,
    likelihood: int,
    impact: int,
    bands: list[dict[str, Any]] | None = None,
    target_date: datetime | None = None,
    rationale: str | None = None,
    financial_expected_inr: float | None = None,
    likelihood_pct: float | None = None,
) -> None:
    """Set the target risk level (where the org intends to steer the risk)."""
    score = int(likelihood) * int(impact)
    risk.targetLikelihood = int(likelihood)
    risk.targetImpact = int(impact)
    risk.targetScore = score
    risk.targetBand = band_for_score(score, bands or DEFAULT_BANDS)
    if target_date is not None:
        risk.targetDate = target_date
    if rationale is not None:
        risk.targetRationale = rationale
    lp = likelihood_pct if likelihood_pct is not None else default_likelihood_pct(likelihood)
    if financial_expected_inr is not None:
        risk.targetExpectedLossInr = expected_loss(lp, financial_expected_inr)


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


# Risk velocity (how fast a risk materialises) compresses the review cadence — a
# VERY_FAST critical risk is reviewed far more often than a SLOW one at the same band.
_VELOCITY_FACTOR = {"SLOW": 1.25, "MODERATE": 1.0, "FAST": 0.6, "VERY_FAST": 0.35}


def velocity_factor(velocity: str | None) -> float:
    return _VELOCITY_FACTOR.get((velocity or "MODERATE").upper(), 1.0)


async def next_review_date_for_band(
    db: AsyncSession, band: str | None, from_date: datetime | None = None, velocity: str | None = None
) -> datetime:
    """Resolve the review cadence for a band (optionally compressed by risk velocity)
    and return the next review date."""
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
    days = max(7, round(days * velocity_factor(velocity)))
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
# Correlation & interdependency — propagation + correlated exposure
# ─────────────────────────────────────────────────────────────────────
async def risk_propagation(db: AsyncSession, risk_id: str) -> dict[str, Any]:
    """If this risk materialises, which linked risks move and by how much ₹?
    Each outgoing linkage propagates impactFactor × source expected-loss onto the
    target, scaled by correlation strength. Models correlations, not just draws them."""
    from app.models.erm import RiskLinkage

    src = await db.get(EnterpriseRisk, risk_id)
    if not src:
        return {"sourceRiskId": risk_id, "sourceRiskCode": "", "sourceExpectedLossInr": 0.0, "directTargets": [], "totalAddedExpectedLossInr": 0.0, "affectedCount": 0}
    src_el = src.residualExpectedLossInr or 0.0
    links = (
        await db.execute(select(RiskLinkage).where(RiskLinkage.sourceRiskId == risk_id))
    ).scalars().all()
    targets: list[dict[str, Any]] = []
    total_added = 0.0
    for l in links:
        tgt = await db.get(EnterpriseRisk, l.targetRiskId)
        if not tgt or tgt.isDeleted:
            continue
        base_el = tgt.residualExpectedLossInr or 0.0
        added = round(src_el * (l.impactFactor or 0.0) * (l.correlationStrength or 0.0))
        total_added += added
        targets.append({
            "riskId": tgt.id, "riskCode": tgt.riskCode, "title": tgt.title,
            "linkageType": l.linkageType, "correlationStrength": l.correlationStrength or 0.0,
            "impactFactor": l.impactFactor or 0.0, "baseResidualExpectedLossInr": base_el,
            "addedExpectedLossInr": added, "stressedExpectedLossInr": round(base_el + added),
        })
    targets.sort(key=lambda t: t["addedExpectedLossInr"], reverse=True)
    return {
        "sourceRiskId": src.id, "sourceRiskCode": src.riskCode, "sourceExpectedLossInr": src_el,
        "directTargets": targets, "totalAddedExpectedLossInr": round(total_added), "affectedCount": len(targets),
    }


async def correlated_exposure(db: AsyncSession) -> dict[str, Any]:
    """Portfolio exposure WITH interdependencies vs the naive independent sum. The
    gap between them is the contagion exposure a naive Σ hides — what a CRO means
    by 'if X fires, what does it do to the portfolio'.

    P2-10: bulk-loaded — TWO queries total (risks + linkages), propagation computed
    in memory. O(N + E), not the previous O(N + N·E) per-risk/per-link round-trips."""
    from app.models.erm import RiskLinkage

    risks = (
        await db.execute(
            select(EnterpriseRisk).where(EnterpriseRisk.isDeleted.is_(False)).where(EnterpriseRisk.lifecycleState != "CLOSED")
        )
    ).scalars().all()
    risk_by_id = {r.id: r for r in risks}
    standalone = sum(r.residualExpectedLossInr or 0.0 for r in risks)

    links = (await db.execute(select(RiskLinkage))).scalars().all()  # single query
    adjacency: dict[str, list[RiskLinkage]] = {}
    for l in links:
        adjacency.setdefault(l.sourceRiskId, []).append(l)

    contagion: list[dict[str, Any]] = []
    total_added = 0.0
    for r in risks:
        src_el = r.residualExpectedLossInr or 0.0
        if not src_el:
            continue
        targets, added_sum = [], 0.0
        for l in adjacency.get(r.id, []):
            tgt = risk_by_id.get(l.targetRiskId)
            if not tgt:  # closed/deleted targets aren't in the in-memory map
                continue
            added = round(src_el * (l.impactFactor or 0.0) * (l.correlationStrength or 0.0))
            if added <= 0:
                continue
            added_sum += added
            targets.append({
                "riskId": tgt.id, "riskCode": tgt.riskCode, "title": tgt.title, "linkageType": l.linkageType,
                "correlationStrength": l.correlationStrength or 0.0, "impactFactor": l.impactFactor or 0.0,
                "baseResidualExpectedLossInr": tgt.residualExpectedLossInr or 0.0,
                "addedExpectedLossInr": added, "stressedExpectedLossInr": round((tgt.residualExpectedLossInr or 0.0) + added),
            })
        if added_sum > 0:
            targets.sort(key=lambda t: t["addedExpectedLossInr"], reverse=True)
            total_added += added_sum
            contagion.append({
                "sourceRiskId": r.id, "sourceRiskCode": r.riskCode, "sourceExpectedLossInr": src_el,
                "directTargets": targets, "totalAddedExpectedLossInr": round(added_sum), "affectedCount": len(targets),
            })
    contagion.sort(key=lambda p: p["totalAddedExpectedLossInr"], reverse=True)
    return {
        "standaloneExpectedLossInr": round(standalone),
        "correlatedExpectedLossInr": round(standalone + total_added),
        "diversificationGapInr": round(total_added),
        "topContagionSources": contagion[:5],
        "linkageCount": len(links),
    }


# ─────────────────────────────────────────────────────────────────────
# Probabilistic — Monte Carlo loss distribution / VaR + reverse stress
# ─────────────────────────────────────────────────────────────────────
def _triangular_sample(best: float, expected: float, worst: float) -> float:
    lo, mode, hi = sorted([best or 0.0, expected or 0.0, worst or 0.0])
    if hi <= lo:
        return lo
    return random.triangular(lo, hi, mode)


async def _residual_loss_params(db: AsyncSession, plant_id: str | None = None) -> list[dict[str, Any]]:
    """Per-active-risk Monte-Carlo inputs from the current RESIDUAL assessment:
    annualised probability + triangular(best, expected, worst) ₹ severity."""
    q = (
        select(EnterpriseRisk, RiskAssessment)
        .join(RiskAssessment, RiskAssessment.riskId == EnterpriseRisk.id)
        .where(EnterpriseRisk.isDeleted.is_(False))
        .where(EnterpriseRisk.lifecycleState != "CLOSED")
        .where(RiskAssessment.assessmentType == "RESIDUAL")
        .where(RiskAssessment.isCurrent.is_(True))
    )
    if plant_id:
        q = q.where(EnterpriseRisk.plantId == plant_id)
    out: list[dict[str, Any]] = []
    for risk, a in (await db.execute(q)).all():
        if not a.financialExpectedInr:
            continue
        out.append({
            "riskId": risk.id, "riskCode": risk.riskCode,
            "p": (a.likelihoodPct if a.likelihoodPct is not None else default_likelihood_pct(a.likelihood)) / 100.0,
            "best": a.financialBestInr or a.financialExpectedInr,
            "expected": a.financialExpectedInr,
            "worst": a.financialWorstInr or a.financialExpectedInr,
        })
    return out


async def _load_linkage_map(db: AsyncSession) -> dict[str, list[tuple[str, float, float]]]:
    """sourceRiskId → [(targetRiskId, correlationStrength, impactFactor)]."""
    from app.models.erm import RiskLinkage

    links = (await db.execute(select(RiskLinkage))).scalars().all()
    by_src: dict[str, list[tuple[str, float, float]]] = {}
    for l in links:
        by_src.setdefault(l.sourceRiskId, []).append(
            (l.targetRiskId, l.correlationStrength or 0.0, l.impactFactor or 0.0)
        )
    return by_src


def _simulate_trial(
    params: list[dict[str, Any]],
    param_by_id: dict[str, dict[str, Any]],
    link_map: dict[str, list[tuple[str, float, float]]],
    stressed_probs: dict[str, float] | None,
    correlate: bool,
) -> float:
    """One Monte-Carlo trial. When correlate=True, a fired risk can INDUCE its
    linked risks (probability = correlationStrength) and AMPLIFY already-fired ones
    (add impactFactor × source severity) — contagion that fattens the tail."""
    fired: dict[str, float] = {}
    total = 0.0
    for p in params:
        prob = (stressed_probs or {}).get(p["riskId"], p["p"])
        if random.random() < prob:
            sev = _triangular_sample(p["best"], p["expected"], p["worst"])
            fired[p["riskId"]] = sev
            total += sev
    if correlate and link_map:
        queue = list(fired.keys())
        while queue:
            src = queue.pop()
            src_sev = fired.get(src, 0.0)
            for tgt, corr, impact in link_map.get(src, []):
                tp = param_by_id.get(tgt)
                if tp is None:
                    continue
                if tgt not in fired:
                    if random.random() < corr:  # induced firing (contagion)
                        sev = _triangular_sample(tp["best"], tp["expected"], tp["worst"])
                        fired[tgt] = sev
                        total += sev
                        queue.append(tgt)
                elif impact > 0:  # amplification spillover onto an already-fired risk
                    total += impact * src_sev
    return total


def _percentiles(totals: list[float]) -> dict[str, float]:
    n = len(totals)
    def q(pct: float) -> float:
        return round(totals[min(n - 1, int(pct * n))])
    return {"p50": q(0.50), "p90": q(0.90), "p95": q(0.95), "p99": q(0.99), "max": round(totals[-1]), "mean": round(sum(totals) / n)}


async def monte_carlo_portfolio(
    db: AsyncSession, iterations: int = 10000, seed: int = 42, plant_id: str | None = None,
    stressed_probs: dict[str, float] | None = None, correlate: bool = True,
) -> dict[str, Any]:
    """Simulate annual aggregate loss: each risk fires with its annualised probability;
    severity ~ triangular(best,expected,worst). When correlate=True the RiskLinkage
    graph drives contagion (induced firing + amplification) INSIDE the simulation, so
    VaR reflects correlation — not an independence assumption. Returns the loss
    distribution + VaR P90/P95/P99, plus the independent-vs-correlated tail delta.
    stressed_probs overrides probabilities for a multi-factor scenario."""
    params = await _residual_loss_params(db, plant_id)
    if not params:
        return {"iterations": 0, "riskCount": 0, "meanLossInr": 0.0, "p50LossInr": 0.0,
                "p90LossInr": 0.0, "p95LossInr": 0.0, "p99LossInr": 0.0, "maxLossInr": 0.0,
                "expectedLossInr": 0.0, "correlated": False, "independentP99LossInr": 0.0,
                "contagionTailUpliftInr": 0.0, "distribution": []}
    iterations = max(1000, min(100000, iterations))
    param_by_id = {p["riskId"]: p for p in params}
    link_map = await _load_linkage_map(db) if correlate else {}

    random.seed(seed)
    totals = sorted(_simulate_trial(params, param_by_id, link_map, stressed_probs, correlate) for _ in range(iterations))

    # Independent baseline (same seed) → quantify the contagion tail uplift.
    independent_p99 = 0.0
    if correlate and link_map:
        random.seed(seed)
        ind = sorted(_simulate_trial(params, param_by_id, {}, stressed_probs, False) for _ in range(iterations))
        independent_p99 = round(ind[min(len(ind) - 1, int(0.99 * len(ind)))])

    n = len(totals)
    pc = _percentiles(totals)
    expected_loss_sum = round(sum(p["p"] * p["expected"] for p in params))
    hi = totals[min(n - 1, int(0.99 * n))] or 1.0
    buckets = 12
    width = hi / buckets
    hist = [0] * (buckets + 1)
    for t in totals:
        idx = min(buckets, int(t / width)) if width else 0
        hist[idx] += 1
    distribution = [
        {"bucketFromInr": round(i * width), "bucketToInr": round((i + 1) * width) if i < buckets else None,
         "count": hist[i], "pct": round(hist[i] * 100.0 / n, 1)}
        for i in range(buckets + 1)
    ]
    return {
        "iterations": iterations, "riskCount": len(params),
        "meanLossInr": pc["mean"], "p50LossInr": pc["p50"], "p90LossInr": pc["p90"],
        "p95LossInr": pc["p95"], "p99LossInr": pc["p99"], "maxLossInr": pc["max"],
        "expectedLossInr": expected_loss_sum,
        "correlated": bool(correlate and link_map),
        "linkageCount": sum(len(v) for v in link_map.values()),
        "independentP99LossInr": independent_p99,
        "contagionTailUpliftInr": round(pc["p99"] - independent_p99) if independent_p99 else 0.0,
        "distribution": distribution,
    }


async def reverse_stress(db: AsyncSession, threshold_inr: float, plant_id: str | None = None) -> dict[str, Any]:
    """Reverse stress test — the smallest set of simultaneously-materialising risks
    (by worst-case ₹) whose combined loss breaches the threshold. Answers 'what
    combination breaks us'."""
    risks = (
        await db.execute(
            select(EnterpriseRisk).where(EnterpriseRisk.isDeleted.is_(False)).where(EnterpriseRisk.lifecycleState != "CLOSED")
        )
    ).scalars().all()
    scored = sorted(
        [r for r in risks if (r.residualWorstLossInr or r.residualExpectedLossInr or 0) > 0],
        key=lambda r: (r.residualWorstLossInr or r.residualExpectedLossInr or 0), reverse=True,
    )
    if plant_id:
        scored = [r for r in scored if r.plantId == plant_id]
    combo: list[dict[str, Any]] = []
    cum = 0.0
    for r in scored:
        loss = r.residualWorstLossInr or r.residualExpectedLossInr or 0
        combo.append({"riskId": r.id, "riskCode": r.riskCode, "title": r.title, "worstLossInr": loss, "residualBand": r.residualBand})
        cum += loss
        if cum >= threshold_inr:
            break
    total_worst = sum((r.residualWorstLossInr or r.residualExpectedLossInr or 0) for r in scored)
    return {
        "thresholdInr": round(threshold_inr),
        "breached": cum >= threshold_inr,
        "minRisksToBreach": len(combo) if cum >= threshold_inr else None,
        "combinedWorstLossInr": round(cum),
        "breakingCombination": combo if cum >= threshold_inr else [],
        "portfolioWorstCaseInr": round(total_worst),
        "headroomInr": round(total_worst - threshold_inr),
    }


# ─────────────────────────────────────────────────────────────────────
# Closed-loop mitigation — treatment reconciliation + control alerts
# ─────────────────────────────────────────────────────────────────────
_CLOSED_CAPA_STATES = ("CLOSED", "VERIFIED")


def achieved_reduction(meta: dict[str, Any], current_residual: int | None) -> int | None:
    """Residual points actually removed since the treatment baseline was captured."""
    base = meta.get("baselineResidualScore")
    if base is None or current_residual is None:
        return None
    return base - current_residual


async def reconcile_treatment_closures(db: AsyncSession) -> dict[str, Any]:
    """For every CLOSED risk-treatment CAPA, measure the residual reduction ACTUALLY
    achieved vs what was expected, and the ₹ exposure removed per ₹ spent. Persists
    the result into the CAPA's sourceMetadata so the loop is provably closed."""
    from app.models.capa import Capa

    now = datetime.now(timezone.utc)
    capas = (
        await db.execute(
            select(Capa)
            .where(Capa.sourceTypeCode == "RISK_TREATMENT")
            .where(Capa.state.in_(_CLOSED_CAPA_STATES))
        )
    ).scalars().all()
    reconciled = 0
    for c in capas:
        meta = dict(c.sourceMetadata or {})
        risk = await db.get(EnterpriseRisk, c.sourceReferenceId) if c.sourceReferenceId else None
        if not risk or meta.get("baselineResidualScore") is None or risk.residualScore is None:
            continue
        achieved = meta["baselineResidualScore"] - risk.residualScore
        meta["achievedResidualReduction"] = achieved
        meta["reconciledAt"] = now.isoformat()
        expected = meta.get("expectedResidualReduction")
        if expected is not None:
            meta["reductionVsExpected"] = achieved - expected
            meta["reductionShortfall"] = bool(achieved < expected)
        base_el = meta.get("baselineResidualExpectedLossInr")
        if base_el is not None and risk.residualExpectedLossInr is not None:
            el_red = base_el - risk.residualExpectedLossInr
            meta["expectedLossReductionInr"] = el_red
            cost = meta.get("costInr")
            if cost:
                meta["riskReductionPerRupee"] = round(el_red / cost, 2)
        c.sourceMetadata = meta  # reassign so SQLAlchemy tracks the JSON mutation
        reconciled += 1
    await db.flush()
    return {"reconciled": reconciled}


_TREATMENT_OPEN_STATES = ("DRAFT", "SUBMITTED", "UNDER_RCA", "ACTIONS_PLANNED", "ACTIONS_IN_PROGRESS", "PENDING_VERIFICATION")
# Escalation ladder for an OVERDUE treatment, by days overdue and parent severity.
_ESCALATION_TIERS = [
    {"level": "CRO", "minDaysOverdue": 14, "bands": ("CRITICAL", "HIGH")},
    {"level": "CHAMPION", "minDaysOverdue": 7, "bands": ("CRITICAL", "HIGH", "MEDIUM")},
    {"level": "OWNER", "minDaysOverdue": 0, "bands": ("CRITICAL", "HIGH", "MEDIUM", "LOW")},
]


def _escalation_level(days_overdue: int, residual_band: str | None) -> str | None:
    for tier in _ESCALATION_TIERS:
        if days_overdue >= tier["minDaysOverdue"] and (residual_band or "LOW") in tier["bands"]:
            return tier["level"]
    return None


async def escalate_overdue_treatments(db: AsyncSession) -> dict[str, Any]:
    """Overdue risk-treatments don't just sit overdue — they escalate up a ladder by
    how long they're overdue AND the parent risk's severity (OWNER → CHAMPION → CRO).
    Persists the escalation level/time on the CAPA metadata and notifies. On-demand."""
    from app.models.capa import Capa

    now = datetime.now(timezone.utc)
    capas = (
        await db.execute(
            select(Capa).where(Capa.sourceTypeCode == "RISK_TREATMENT").where(Capa.state.in_(_TREATMENT_OPEN_STATES))
        )
    ).scalars().all()
    escalated = 0
    by_level: dict[str, int] = {}
    for c in capas:
        if not c.closureTargetDate:
            continue
        due = c.closureTargetDate.replace(tzinfo=timezone.utc) if c.closureTargetDate.tzinfo is None else c.closureTargetDate
        if due >= now:
            continue
        days_overdue = (now - due).days
        risk = await db.get(EnterpriseRisk, c.sourceReferenceId) if c.sourceReferenceId else None
        band = risk.residualBand if risk else None
        level = _escalation_level(days_overdue, band)
        if not level:
            continue
        meta = dict(c.sourceMetadata or {})
        if meta.get("escalationLevel") == level:
            continue  # already at this tier — no duplicate escalation
        meta["escalationLevel"] = level
        meta["escalatedAt"] = now.isoformat()
        meta["daysOverdueAtEscalation"] = days_overdue
        c.sourceMetadata = meta
        escalated += 1
        by_level[level] = by_level.get(level, 0) + 1
        if level == "CRO" and risk:
            await _notify_treatment_escalation(db, c, risk, days_overdue)
    await db.flush()
    return {"escalated": escalated, "byLevel": by_level, "evaluated": len(capas)}


async def _notify_treatment_escalation(db: AsyncSession, capa, risk: EnterpriseRisk, days_overdue: int) -> None:
    """Best-effort CRO notification for a critically-overdue treatment. Never raises."""
    try:
        from app.models.user import Role, UserRole
        from app.services.notifications import send_email

        cro = (
            await db.execute(
                select(User.email).join(UserRole, UserRole.userId == User.id).join(Role, Role.id == UserRole.roleId).where(Role.code == "CRO")
            )
        ).scalars().all()
        recip = [e for e in cro if e]
        if recip:
            await send_email(
                recip,
                subject=f"[ERM] Treatment escalated to CRO: {capa.capaNumber} ({days_overdue}d overdue)",
                body=f"Treatment {capa.capaNumber} on {risk.residualBand} risk {risk.riskCode} '{risk.title}' is {days_overdue} days overdue and has escalated to CRO level. RMC review required.",
            )
    except Exception:
        return


async def sync_kri_alerts(db: AsyncSession) -> dict[str, Any]:
    """A RED KRI must push its linked enterprise risks into reassessment (early
    warning → action), not just notify. Sets kriAlert on every risk linked to a RED
    KRI; clears it when no linked KRI is RED."""
    from app.models.erm_p2 import KriDefinition

    now = datetime.now(timezone.utc)
    kris = (
        await db.execute(select(KriDefinition).where(KriDefinition.isActive.is_(True)).where(KriDefinition.isDeleted.is_(False)))
    ).scalars().all()
    red_risk_ids: set[str] = set()
    for k in kris:
        if k.currentStatus == "RED":
            red_risk_ids.update(k.linkedRiskIds or [])
    raised = cleared = 0
    risks = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.isDeleted.is_(False)))).scalars().all()
    for r in risks:
        should = r.id in red_risk_ids
        if should and not r.kriAlert:
            r.kriAlert = True
            r.kriAlertAt = now
            raised += 1
        elif not should and r.kriAlert:
            r.kriAlert = False
            r.kriAlertAt = None
            cleared += 1
    await db.flush()
    return {"alertsRaised": raised, "alertsCleared": cleared}


def _bowtie_has_failed_barrier(bowtie: dict[str, Any] | None) -> bool:
    """A FAILED or ABSENT bow-tie barrier is a control gap that must flag the risk."""
    if not bowtie:
        return False
    for threat in bowtie.get("threats", []) or []:
        for b in threat.get("preventiveBarriers", []) or []:
            if b.get("status") in ("FAILED", "ABSENT"):
                return True
    for cons in bowtie.get("consequences", []) or []:
        for b in cons.get("mitigatingBarriers", []) or []:
            if b.get("status") in ("FAILED", "ABSENT"):
                return True
    return False


async def sync_control_alerts(db: AsyncSession) -> dict[str, Any]:
    """Flag every risk whose mapped controls include a currently-DEFICIENT control OR
    whose bow-tie has a FAILED/ABSENT barrier — a degraded control forces the linked
    risk back into reassessment; clear the alert when no control gap remains."""
    from app.models.erm_t3 import Control, RiskControlMapping

    now = datetime.now(timezone.utc)
    maps = (
        await db.execute(select(RiskControlMapping).where(RiskControlMapping.riskId.is_not(None)))
    ).scalars().all()
    controls_by_risk: dict[str, list[str]] = {}
    for m in maps:
        controls_by_risk.setdefault(m.riskId, []).append(m.controlId)

    raised = cleared = barrier_flagged = 0
    risks = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.isDeleted.is_(False)))).scalars().all()
    for risk in risks:
        gap = False
        for cid in controls_by_risk.get(risk.id, []):
            ctrl = await db.get(Control, cid)
            if not ctrl or not ctrl.isActive:
                continue
            if (ctrl.currentOperatingRating or ctrl.currentDesignRating) == "DEFICIENT":
                gap = True
                break
        if not gap and _bowtie_has_failed_barrier(risk.bowtie):
            gap = True
            barrier_flagged += 1
        if gap and not risk.controlAlert:
            risk.controlAlert = True
            risk.controlAlertAt = now
            raised += 1
        elif not gap and risk.controlAlert:
            risk.controlAlert = False
            risk.controlAlertAt = None
            cleared += 1
    await db.flush()
    return {"alertsRaised": raised, "alertsCleared": cleared, "barrierFlagged": barrier_flagged}


# ─────────────────────────────────────────────────────────────────────
# Regulatory framework alignment (ISO 31000 · COSO ERM 2017 · SEBI LODR Reg 21)
# ─────────────────────────────────────────────────────────────────────
# Static capability→clause mapping. status reflects what the ADVANCED build now
# delivers; "evidence" names the endpoint/feature a CRO can be shown.
_FRAMEWORK_MATRIX: list[dict[str, Any]] = [
    {"framework": "ISO 31000:2018", "version": "2018", "clauses": [
        {"clause": "6.3.4", "title": "Risk identification", "capability": "Multi-category register + live HIRA/EAI rollup", "status": "MET", "evidence": "/api/erm/risks, rollup engine"},
        {"clause": "6.4.2", "title": "Risk analysis (likelihood & consequence)", "capability": "5-dimension impact, MAX aggregation, ₹ expected loss", "status": "MET", "evidence": "_record_assessment, expected_loss"},
        {"clause": "6.4.3", "title": "Risk evaluation vs criteria", "capability": "Appetite bands + breach engine", "status": "MET", "evidence": "/api/erm/appetite, evaluate_appetite"},
        {"clause": "6.5", "title": "Risk treatment (residual & target)", "capability": "Control-derived residual + target level + 4 strategies", "status": "MET", "evidence": "derived-residual, /target, /treatments"},
        {"clause": "6.6", "title": "Monitoring & review", "capability": "Velocity-driven cadence, KRIs, closed-loop reconcile", "status": "MET", "evidence": "/treatments/reconcile, KRI engine"},
    ]},
    {"framework": "COSO ERM 2017", "version": "2017", "clauses": [
        {"clause": "Principle 10", "title": "Identifies & assesses risk", "capability": "Inherent→residual→target with derivation", "status": "MET", "evidence": "derived-residual"},
        {"clause": "Principle 11", "title": "Selects risk responses", "capability": "Treat/Tolerate/Transfer/Terminate + cost-benefit", "status": "MET", "evidence": "/treatments, riskReductionPerRupee"},
        {"clause": "Principle 13", "title": "Portfolio view", "capability": "₹ exposure, concentration HHI, correlated exposure, Monte Carlo VaR", "status": "MET", "evidence": "/exposure, /portfolio/*"},
        {"clause": "Principle 15", "title": "Assesses substantial change", "capability": "Control-alert + KRI-alert auto-reassessment", "status": "MET", "evidence": "control-alerts/sync, kri-alerts/sync"},
        {"clause": "3 Lines of Defence", "title": "Roles & accountability", "capability": "1st/2nd/3rd line owners per risk", "status": "MET", "evidence": "/risks/{id}/three-lines"},
    ]},
    {"framework": "SEBI LODR Reg 21", "version": "2015 (amended)", "clauses": [
        {"clause": "21(4)", "title": "Risk Management Committee oversight", "capability": "RMC workflow, escalation, board pack", "status": "MET", "evidence": "escalation, board pack"},
        {"clause": "21", "title": "Cyber/ESG/credit risk coverage", "capability": "Vendor ESG dual-lens + insurance + BCM", "status": "MET", "evidence": "Tier 3 + Phase 3 modules"},
        {"clause": "21", "title": "Quantified exposure to the board", "capability": "₹ enterprise exposure + Monte-Carlo VaR embedded in the board pack", "status": "MET", "evidence": "board-packs/{id}/render → exposure + monteCarlo"},
    ]},
]


def framework_coverage() -> dict[str, Any]:
    frameworks = []
    all_met = all_total = 0
    for fw in _FRAMEWORK_MATRIX:
        clauses = fw["clauses"]
        met = sum(1 for c in clauses if c["status"] == "MET")
        partial = sum(1 for c in clauses if c["status"] == "PARTIAL")
        gap = sum(1 for c in clauses if c["status"] == "GAP")
        total = len(clauses)
        # partial counts as half for the percentage
        pct = round((met + 0.5 * partial) * 100.0 / total, 1) if total else 0.0
        all_met += met + 0.5 * partial
        all_total += total
        frameworks.append({
            "framework": fw["framework"], "version": fw.get("version", ""),
            "metCount": met, "partialCount": partial, "gapCount": gap, "coveragePct": pct, "clauses": clauses,
        })
    return {"frameworks": frameworks, "overallCoveragePct": round(all_met * 100.0 / all_total, 1) if all_total else 0.0}


# ─────────────────────────────────────────────────────────────────────
# Time horizon — multi-year cumulative exposure
# ─────────────────────────────────────────────────────────────────────
def multi_year_loss(likelihood_pct: float | None, financial_expected: float | None, years: int) -> dict[str, Any] | None:
    """Cumulative probability of ≥1 occurrence and cumulative expected loss over N
    years from an annualised probability: P(≥1 in N) = 1 − (1−p)^N."""
    if likelihood_pct is None or financial_expected is None:
        return None
    p = max(0.0, min(1.0, likelihood_pct / 100.0))
    cum_p = 1.0 - (1.0 - p) ** years
    return {
        "years": years,
        "cumulativeProbabilityPct": round(cum_p * 100.0, 1),
        "cumulativeExpectedLossInr": round(p * financial_expected * years),
    }


async def horizon_projection(db: AsyncSession, risk_id: str) -> dict[str, Any]:
    """Project the current residual exposure over 1/3/5-year horizons — risk is not
    static point-in-time. Reads the current RESIDUAL assessment's annual probability."""
    a = (
        await db.execute(
            select(RiskAssessment).where(RiskAssessment.riskId == risk_id)
            .where(RiskAssessment.assessmentType == "RESIDUAL").where(RiskAssessment.isCurrent.is_(True))
        )
    ).scalar_one_or_none()
    if not a or not a.financialExpectedInr:
        return {"riskId": risk_id, "available": False, "annualExpectedLossInr": None, "assessmentHorizon": None, "horizons": []}
    lp = a.likelihoodPct if a.likelihoodPct is not None else default_likelihood_pct(a.likelihood)
    return {
        "riskId": risk_id,
        "available": True,
        "annualExpectedLossInr": expected_loss(lp, a.financialExpectedInr),
        "assessmentHorizon": a.timeHorizon,
        "horizons": [multi_year_loss(lp, a.financialExpectedInr, y) for y in (1, 3, 5)],
    }


# ─────────────────────────────────────────────────────────────────────
# Longitudinal residual stability — did it fall AND stay down?
# ─────────────────────────────────────────────────────────────────────
async def residual_stability(db: AsyncSession, risk_id: str) -> dict[str, Any]:
    """Track residual across quarter-end snapshots: did it fall, did it stay down (or
    rebound), and is it at/below target? Mitigation effectiveness OVER TIME, not a
    point estimate."""
    from app.models.erm import ErmRiskSnapshot

    risk = await db.get(EnterpriseRisk, risk_id)
    snaps = (
        await db.execute(
            select(ErmRiskSnapshot).where(ErmRiskSnapshot.riskId == risk_id).order_by(ErmRiskSnapshot.snapshotDate)
        )
    ).scalars().all()
    series = [
        {"quarterLabel": s.quarterLabel, "residualScore": s.residualScore, "residualBand": s.residualBand}
        for s in snaps if s.residualScore is not None
    ]
    # Append the live current residual as the latest point.
    if risk and risk.residualScore is not None:
        series.append({"quarterLabel": "CURRENT", "residualScore": risk.residualScore, "residualBand": risk.residualBand})
    scores = [pt["residualScore"] for pt in series]
    peak = max(scores) if scores else None
    current = risk.residualScore if risk else None
    fell = bool(peak is not None and current is not None and current < peak)
    # rebound = a later point is higher than an earlier (post-reduction) low
    rebounded = False
    if len(scores) >= 2:
        running_min = scores[0]
        for s in scores[1:]:
            if s > running_min + 1:  # tolerance of 1 point
                rebounded = True
            running_min = min(running_min, s)
    target = risk.targetScore if risk else None
    return {
        "riskId": risk_id,
        "series": series,
        "currentResidualScore": current,
        "peakResidualScore": peak,
        "targetScore": target,
        "fellFromPeak": fell,
        "rebounded": rebounded,
        "atOrBelowTarget": (current <= target) if (current is not None and target is not None) else None,
        "stable": bool(fell and not rebounded),
        "dataPoints": len(series),
    }


# ─────────────────────────────────────────────────────────────────────
# Consolidated on-demand jobs runner (scheduler substitute)
# ─────────────────────────────────────────────────────────────────────
async def run_all_jobs(db: AsyncSession, include_module_fed: bool = False) -> dict[str, Any]:
    """Run every on-demand ERM engine in one pass — the platform has no scheduler, so
    a cron/ops call to this single endpoint replaces nightly jobs. include_module_fed
    re-reads live KRI metrics (off by default so it doesn't overwrite curated demo
    KRI statuses)."""
    from app.services import erm_p2 as p2

    results: dict[str, Any] = {}
    if include_module_fed:
        results["kriModuleFed"] = await p2.run_module_fed(db)
    results["kriNoData"] = await p2.check_no_data(db)
    results["appetite"] = await p2.evaluate_appetite(db)
    results["complianceTasks"] = await p2.generate_tasks(db)
    results["complianceStatus"] = await p2.refresh_statuses(db)
    results["lossAutoFeed"] = await p2.auto_feed_incidents(db)
    results["treatmentReconcile"] = await reconcile_treatment_closures(db)
    results["treatmentEscalation"] = await escalate_overdue_treatments(db)
    results["controlAlerts"] = await sync_control_alerts(db)
    results["kriAlerts"] = await sync_kri_alerts(db)
    await db.flush()
    return results


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


# ─────────────────────────────────────────────────────────────────────
# I-04 — Incident → ERM risk auto-flag
# ─────────────────────────────────────────────────────────────────────
_INCIDENT_DONE = ("CAPA_ASSIGNED", "VERIFIED", "CLOSED")  # investigation complete onward


async def sync_incident_risk_alerts(db: AsyncSession, lookback_days: int = 120) -> dict[str, int]:
    """Flag OPS-category active EnterpriseRisks for review when an LTI/FATALITY (by
    type) or CRITICAL (by severity) incident at their site has completed
    investigation in the lookback window. Idempotent — re-running re-derives the
    flag from current incidents (clears it when no qualifying incident remains).
    Caller commits."""
    from app.models.incident import Incident

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=lookback_days)
    incidents = (
        await db.execute(
            select(Incident).where(Incident.status.in_(_INCIDENT_DONE)).where(Incident.createdAt >= since)
        )
    ).scalars().all()

    def _qualifies(i) -> bool:
        itype = getattr(i, "type", None)
        itype_s = itype.value if hasattr(itype, "value") else str(itype or "")
        return itype_s in ("LTI", "FATALITY") or (i.severity or "").upper() == "CRITICAL"

    # plant → most-recent qualifying incident
    by_plant: dict[str, Any] = {}
    for i in incidents:
        if not _qualifies(i) or not i.plantId:
            continue
        cur = by_plant.get(i.plantId)
        if cur is None or _aware_dt(i.createdAt) > _aware_dt(cur.createdAt):
            by_plant[i.plantId] = i

    ops_cat = (
        await db.execute(select(RiskCategory.id).where(RiskCategory.code == "OPS"))
    ).scalar_one_or_none()

    risks = (
        await db.execute(
            select(EnterpriseRisk)
            .where(EnterpriseRisk.isDeleted.is_(False))
            .where(EnterpriseRisk.lifecycleState.in_(ACTIVE_STATES))
        )
    ).scalars().all()
    flagged = cleared = 0
    for r in risks:
        if ops_cat is not None and r.categoryId != ops_cat:
            continue
        inc = by_plant.get(r.plantId) if r.plantId else None
        if inc is not None:
            code = getattr(inc, "incidentNumber", None) or getattr(inc, "incidentCode", None) or inc.id[:8]
            reason = f"LTI/Critical incident {code} at this site — review recommended"
            if not r.incidentAlert or r.incidentAlertReason != reason:
                r.incidentAlert = True
                r.incidentAlertReason = reason
                r.incidentAlertAt = now
                flagged += 1
        elif r.incidentAlert:
            r.incidentAlert = False
            r.incidentAlertReason = None
            r.incidentAlertAt = None
            cleared += 1
    await db.flush()
    return {"flagged": flagged, "cleared": cleared, "qualifyingPlants": len(by_plant)}


def _aware_dt(d: datetime | None) -> datetime:
    if d is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
