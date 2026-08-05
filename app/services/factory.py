"""Facilities service layer — shared by the factory router.

Owns the genuinely-shared behaviour:
  • sequential factory-code generation (FAC-0001, mirrors the CAMS convention)
  • buildingCount sync (recompute from active Building rows; manual when none)
  • DRAFT→ACTIVE completeness check (the ≥1-workforce gate lands in Phase B)

Cross-module references (Plant) are plain ids with no hard FK, so absence
degrades to an empty field rather than an error.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.factory import Building, FactoryProfile, WorkforceComposition

# Re-use the CAMS batch name helper — same DB, same Plant table.
from app.services.cams import plant_name_map  # noqa: F401  (re-exported for the router)


# ── code generation (tenant-scoped sequential) ──────────────────────────────
async def next_factory_code(db: AsyncSession) -> str:
    n = (await db.execute(select(func.count()).select_from(FactoryProfile))).scalar() or 0
    return f"FAC-{(n + 1):04d}"


# ── buildingCount sync ───────────────────────────────────────────────────────
async def recompute_building_count(db: AsyncSession, profile_id: str) -> int:
    """Recompute FactoryProfile.buildingCount from the count of active, non-deleted
    Building rows. The manual count is preserved ONLY for a profile that has never
    had any Building row (greenfield); once buildings have been managed via the
    register the count tracks the active total — including dropping to 0 when the
    last building is removed (TF-02)."""
    active = (
        await db.execute(
            select(func.count())
            .select_from(Building)
            .where(Building.factoryProfileId == profile_id)
            .where(Building.isActive.is_(True))
            .where(Building.isDeleted.is_(False))
        )
    ).scalar() or 0
    # any Building row ever attached (incl. soft-deleted) ⇒ register is in use
    ever = (
        await db.execute(
            select(func.count()).select_from(Building).where(Building.factoryProfileId == profile_id)
        )
    ).scalar() or 0
    profile = await db.get(FactoryProfile, profile_id)
    if profile and ever > 0:
        profile.buildingCount = active
    return active


# ── DRAFT → ACTIVE completeness ──────────────────────────────────────────────
async def compute_profile_status(db: AsyncSession, profile: FactoryProfile) -> str:
    """Completeness (build prompt F-03 §6): name + site link + location + ≥1
    current workforce record ⇒ ACTIVE, else DRAFT. A profile already flagged
    REVIEW_DUE is left as-is."""
    if profile.profileStatus == "REVIEW_DUE":
        return "REVIEW_DUE"
    has_location = bool((profile.state or "").strip() or (profile.city or "").strip() or (profile.addressLine or "").strip())
    # A *meaningful* current workforce record (>0 headcount) is required — an
    # all-zero record shouldn't promote a profile to ACTIVE.
    has_workforce = (
        await db.execute(
            select(func.count())
            .select_from(WorkforceComposition)
            .where(WorkforceComposition.factoryProfileId == profile.id)
            .where(WorkforceComposition.isCurrent.is_(True))
            .where(WorkforceComposition.isDeleted.is_(False))
            .where(WorkforceComposition.totalCount > 0)
        )
    ).scalar() or 0
    if profile.factoryName and profile.siteId and has_location and has_workforce > 0:
        return "ACTIVE"
    return "DRAFT"


# ── workforce reconciliation + history ───────────────────────────────────────
def reconcile_workforce(permanent: int, contract: int, apprentice: int, male: int, female: int, other: int) -> tuple[int, bool]:
    """Returns (totalCount, genderMismatch). totalCount is the authoritative sum
    of employment-type counts (so permanent+contract+apprentice = totalCount is
    enforced by construction). A gender split that doesn't reconcile to totalCount
    is a SOFT warning, not a block (data completeness varies)."""
    total = permanent + contract + apprentice
    gender_total = male + female + other
    return total, gender_total != total


# ── certification status engine (TF-04) ──────────────────────────────────────
def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compute_cert_status(expiry: datetime | None, renewal_lead_days: int | None, stored: str | None = None) -> str:
    """Effective cert status. UNDER_RENEWAL / SUSPENDED are manual overrides and
    pass through; VALID / EXPIRING_SOON / EXPIRED are always derived from the
    expiry date + renewalLeadDays (default 60), so the dashboard stays correct
    without a cron."""
    if stored in ("UNDER_RENEWAL", "SUSPENDED"):
        return stored
    if expiry is None:
        return "VALID"
    now = datetime.now(timezone.utc)
    exp = _aware(expiry)
    if exp < now:
        return "EXPIRED"
    # `0` is a valid lead ("alert only once expired") — don't fall back to 60.
    lead = 60 if renewal_lead_days is None else max(0, renewal_lead_days)
    if exp <= now + timedelta(days=lead):
        return "EXPIRING_SOON"
    return "VALID"


def cert_days_to_expiry(expiry: datetime | None) -> int | None:
    if expiry is None:
        return None
    return (_aware(expiry) - datetime.now(timezone.utc)).days


def cert_is_expiring(status: str) -> bool:
    return status in ("EXPIRING_SOON", "EXPIRED")


def workforce_derived_pcts(
    *, total: int, contract: int, female: int, gender_total: int, migrant: int | None
) -> tuple[float, float, float | None]:
    """Persisted register percentages. contract% / migrant% are a share of total
    headcount; female% is a share of the gender split (matches the SA8000 welfare
    lens on the Workforce tab)."""
    contract_pct = round(contract / total * 100, 1) if total else 0.0
    female_pct = round(female / gender_total * 100, 1) if gender_total else 0.0
    migrant_pct = round(migrant / total * 100, 1) if (migrant is not None and total) else None
    return contract_pct, female_pct, migrant_pct


def apply_workforce_derived(comp: WorkforceComposition) -> None:
    """Recompute + persist contractPct / femalePct / migrantPct on a composition
    from its counts (call after the counts are set)."""
    gender_total = comp.maleCount + comp.femaleCount + comp.otherGenderCount
    comp.contractPct, comp.femalePct, comp.migrantPct = workforce_derived_pcts(
        total=comp.totalCount, contract=comp.contractCount, female=comp.femaleCount,
        gender_total=gender_total, migrant=comp.migrantWorkerCount,
    )


def child_labour_flag(
    youngest_worker_age: int | None, workers_under_18_count: int | None, min_hiring_age_policy: int | None
) -> bool:
    """SA8000 Element 1. Raised when legally-young workers are present AND the
    youngest is below the factory's own minimum hiring-age policy — the single
    most scrutinised SA8000 item. Missing age/policy with under-18 present is
    flagged conservatively (an exception worth checking)."""
    if not workers_under_18_count or workers_under_18_count <= 0:
        return False
    if youngest_worker_age is None or min_hiring_age_policy is None:
        return True
    return youngest_worker_age < min_hiring_age_policy


# ── social-compliance flag engine (SA8000) ───────────────────────────────────
# Element ComplianceFlag fields that feed the overall worst-of computation.
SOCIAL_ELEMENT_FIELDS = (
    "minimumWageCompliant",
    "wagesPaidOnTime",
    "overtimeVoluntary",
    "weeklyRestDayProvided",
    "unionOrWorkerCommitteePresent",
    "noDepositOrDocumentRetention",
    "grievanceMechanismPresent",
    "antiDiscriminationPolicy",
)
_FLAG_RANK = {"NON_COMPLIANT": 3, "ATTENTION": 2, "COMPLIANT": 1, "NOT_ASSESSED": 0}
SA8000_OVERTIME_CAP = 12  # SA8000 guidance — max 12 OT hours/week


def worst_flag(flags) -> str:
    """Worst-of across element flags. NON_COMPLIANT > ATTENTION > COMPLIANT.
    NOT_ASSESSED contributes only when EVERY element is unassessed (so a single
    assessed COMPLIANT element doesn't get masked by unassessed siblings)."""
    assessed = [f for f in flags if f and f != "NOT_ASSESSED"]
    if not assessed:
        return "NOT_ASSESSED"
    return max(assessed, key=lambda f: _FLAG_RANK.get(f, 0))


def overtime_exceeds_cap(max_weekly_overtime_hours: int | None) -> bool:
    return max_weekly_overtime_hours is not None and max_weekly_overtime_hours > SA8000_OVERTIME_CAP


def compute_overall_social_flag(*, element_flags, max_weekly_overtime_hours: int | None) -> str:
    """Persisted overall flag = worst-of the element flags, with an OT-cap breach
    (>12h/week) folding in a Working-Hours ATTENTION. Child-labour is a
    workforce-driven signal layered on at the register/export level, not here."""
    flags = list(element_flags)
    if overtime_exceeds_cap(max_weekly_overtime_hours):
        flags.append("ATTENTION")
    return worst_flag(flags)


def overall_social_flag_for(profile) -> str:
    """Convenience: compute the overall flag from a SocialComplianceProfile row."""
    return compute_overall_social_flag(
        element_flags=[getattr(profile, f) for f in SOCIAL_ELEMENT_FIELDS],
        max_weekly_overtime_hours=profile.maxWeeklyOvertimeHours,
    )


def effective_social_flag(overall: str, child_labour: bool) -> str:
    """The chip shown on the register/export — escalates the persisted overall
    flag with the workforce-derived child-labour signal. Without child labour the
    overall flag passes through unchanged (so a factory with no social profile
    stays NOT_ASSESSED rather than being promoted to COMPLIANT)."""
    if child_labour:
        return worst_flag([overall, "ATTENTION"])
    return overall


async def make_workforce_current(db: AsyncSession, profile: FactoryProfile, comp: WorkforceComposition) -> None:
    """Flip every other composition for this profile to historical, mark `comp`
    current, and write the denormalised headcount onto the profile."""
    prior = (
        await db.execute(
            select(WorkforceComposition)
            .where(WorkforceComposition.factoryProfileId == profile.id)
            .where(WorkforceComposition.id != comp.id)
            .where(WorkforceComposition.isCurrent.is_(True))
        )
    ).scalars().all()
    for p in prior:
        p.isCurrent = False
    comp.isCurrent = True
    profile.totalEmployees = comp.totalCount
