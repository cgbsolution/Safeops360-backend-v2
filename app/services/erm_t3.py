"""ERM Tier 3 engines — controls effectiveness/segregation, dual-lens vendor
scoring, insurance status/renewal/coverage-gap. Pure + DB helpers; on-demand
(no scheduler), consistent with prior phases."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capa import Capa
from app.models.erm_t3 import (
    Control,
    ControlDeficiency,
    ControlTest,
    ControlTestPlan,
    VendorAssessment,
)
from app.models.user import User

_OPEN_CAPA = ("DRAFT", "SUBMITTED", "UNDER_RCA", "ACTIONS_PLANNED", "ACTIONS_IN_PROGRESS", "PENDING_VERIFICATION")
DEFICIENT_CONCLUSIONS = ("DEFICIENT", "SIGNIFICANT_DEFICIENCY", "MATERIAL_WEAKNESS")
CAPA_REQUIRED_SEVERITY = ("SIGNIFICANT_DEFICIENCY", "MATERIAL_WEAKNESS")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


async def user_name_map(db: AsyncSession, ids) -> dict[str, str]:
    ids = [i for i in set(ids) if i and i != "SYSTEM"]
    if not ids:
        return {}
    rows = (await db.execute(select(User.id, User.name).where(User.id.in_(ids)))).all()
    return {r[0]: r[1] for r in rows}


# ── Controls ───────────────────────────────────────────────────────────────────
def segregation_ok(tester_id: str, owner_id: str) -> bool:
    """A control cannot be tested by its owner (the auditor's first check)."""
    return bool(tester_id) and tester_id != owner_id


def requires_capa(severity: str) -> bool:
    return severity in CAPA_REQUIRED_SEVERITY


def _conclusion_to_rating(conclusion: str) -> str:
    return "EFFECTIVE" if conclusion == "EFFECTIVE" else "DEFICIENT"


async def recompute_control_ratings(db: AsyncSession, control: Control) -> None:
    """design rating = latest DESIGN test; operating = latest OPERATING test;
    lastTestDate = max test date; nextTestDueDate from the most-frequent plan."""
    tests = (await db.execute(select(ControlTest).where(ControlTest.controlId == control.id))).scalars().all()
    design = [t for t in tests if t.testType == "DESIGN"]
    operating = [t for t in tests if t.testType == "OPERATING"]
    if design:
        latest_d = max(design, key=lambda t: _aware(t.testDate))
        control.currentDesignRating = _conclusion_to_rating(latest_d.conclusion)
    elif control.currentDesignRating is None:
        control.currentDesignRating = "NOT_ASSESSED"
    if operating:
        latest_o = max(operating, key=lambda t: _aware(t.testDate))
        control.currentOperatingRating = _conclusion_to_rating(latest_o.conclusion)
    elif control.currentOperatingRating is None:
        control.currentOperatingRating = "NOT_ASSESSED"
    if tests:
        control.lastTestDate = max(_aware(t.testDate) for t in tests)
    # next due from plans (soonest interval)
    plans = (await db.execute(select(ControlTestPlan).where(ControlTestPlan.controlId == control.id))).scalars().all()
    if plans and control.lastTestDate:
        max_freq = max(p.testFrequencyPerYear for p in plans)
        interval_days = max(1, round(365 / max_freq))
        control.nextTestDueDate = _aware(control.lastTestDate) + timedelta(days=interval_days)
    elif plans and not control.lastTestDate:
        control.nextTestDueDate = min(_aware(p.scheduledDate) for p in plans)


def test_overdue(control: Control, now: datetime | None = None) -> bool:
    now = now or _now()
    if not control.isKeyControl or not control.isActive:
        return False
    return control.nextTestDueDate is not None and _aware(control.nextTestDueDate) < now


async def open_deficiency_count(db: AsyncSession, control_id: str) -> int:
    defs = (await db.execute(
        select(ControlDeficiency).where(ControlDeficiency.controlId == control_id).where(ControlDeficiency.isDeleted.is_(False))
    )).scalars().all()
    return sum(1 for d in defs if d.status != "CLOSED")


async def deficiency_capa_state(db: AsyncSession, capa_id: str | None) -> str | None:
    if not capa_id:
        return None
    c = await db.get(Capa, capa_id)
    return c.state if c else None


async def has_passing_retest_after(db: AsyncSession, control_id: str, after: datetime) -> bool:
    """CLOSED requires a later OPERATING test concluding EFFECTIVE (the retest)."""
    tests = (await db.execute(select(ControlTest).where(ControlTest.controlId == control_id))).scalars().all()
    return any(
        t.testType == "OPERATING" and t.conclusion == "EFFECTIVE" and _aware(t.testDate) > _aware(after)
        for t in tests
    )


# ── Vendor dual-lens scoring ────────────────────────────────────────────────────
def compute_weighted_score(domain_scores: list[dict]) -> float:
    """sum(rawScore × weightPct) / 5, clamped 0–100. Weights sum to 100 → range
    20 (all 1s) to 100 (all 5s). RISK lens: higher = riskier; ESG: higher = better."""
    total = sum(float(d.get("rawScore", 0)) * float(d.get("weightPct", 0)) for d in domain_scores or [])
    return round(max(0.0, min(100.0, total / 5.0)), 1)


def band_for(thresholds: list[dict], score: float) -> str:
    for b in thresholds or []:
        if float(b.get("minScore", 0)) <= score <= float(b.get("maxScore", 100)):
            return str(b.get("band"))
    return str(thresholds[-1].get("band")) if thresholds else "UNKNOWN"


def open_critical_gaps(assessments: list[VendorAssessment]) -> int:
    """Open CRITICAL_GAP findings across CURRENT assessments. A linked remediation
    CAPA does NOT close the gap — only a clean re-assessment (a new current
    assessment without the gap) does. So approval of a strategic/critical vendor
    stays blocked (→ CONDITIONAL) while any current CRITICAL_GAP exists."""
    n = 0
    for a in assessments:
        if not a.isCurrent:
            continue
        for f in a.findings or []:
            if f.get("severity") == "CRITICAL_GAP":
                n += 1
    return n


def vendor_review_overdue(vendor, now: datetime | None = None) -> bool:
    now = now or _now()
    return vendor.nextReviewDate is not None and _aware(vendor.nextReviewDate) < now


async def recompute_vendor_scores(db: AsyncSession, vendor) -> None:
    """Denormalise current RISK + ESG composite/band + soonest validUntil onto the profile."""
    rows = (await db.execute(
        select(VendorAssessment).where(VendorAssessment.vendorId == vendor.id).where(VendorAssessment.isDeleted.is_(False))
    )).scalars().all()
    valid_dates: list[datetime] = []
    for lens, score_attr, band_attr in (("RISK", "currentRiskScore", "currentRiskBand"), ("ESG", "currentEsgScore", "currentEsgBand")):
        current = [a for a in rows if a.lens == lens and a.isCurrent]
        if current:
            latest = max(current, key=lambda a: _aware(a.assessmentDate))
            setattr(vendor, score_attr, latest.weightedScore)
            setattr(vendor, band_attr, latest.band)
            valid_dates.append(_aware(latest.validUntil))
    if valid_dates:
        vendor.nextReviewDate = min(valid_dates)


# ── Insurance ────────────────────────────────────────────────────────────────────
def days_to_expiry(policy, now: datetime | None = None) -> int | None:
    now = now or _now()
    if not policy.coverageEndDate:
        return None
    return (_aware(policy.coverageEndDate) - now).days


def policy_status(policy, now: datetime | None = None) -> str:
    """Derive status from dates; manual LAPSED / UNDER_RENEWAL are sticky."""
    now = now or _now()
    if policy.status in ("LAPSED", "UNDER_RENEWAL"):
        return policy.status
    if not policy.isActive:
        return "LAPSED"
    end = _aware(policy.coverageEndDate)
    if end is None:
        return "ACTIVE"
    if end < now:
        return "EXPIRED"
    if end <= now + timedelta(days=policy.renewalLeadDays or 45):
        return "EXPIRING_SOON"
    return "ACTIVE"


async def open_claims_value(db: AsyncSession, policy_id: str) -> tuple[int, float]:
    from app.models.erm_t3 import InsuranceClaim
    claims = (await db.execute(
        select(InsuranceClaim).where(InsuranceClaim.policyId == policy_id).where(InsuranceClaim.isDeleted.is_(False))
    )).scalars().all()
    open_states = ("INTIMATED", "SURVEYOR_APPOINTED", "UNDER_ASSESSMENT", "APPROVED", "PARTIALLY_SETTLED")
    open_c = [c for c in claims if c.status in open_states]
    return len(open_c), sum(c.claimedAmountInr or 0 for c in open_c)
