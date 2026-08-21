"""Facilities service layer — shared by the factory router.

Owns the genuinely-shared behaviour:
  • sequential factory-code generation (FAC-0001, mirrors the CAMS convention)
  • buildingCount sync (recompute from active Building rows; manual when none)
  • DRAFT→ACTIVE completeness check (the ≥1-workforce gate lands in Phase B)

Cross-module references (Plant) are plain ids with no hard FK, so absence
degrades to an empty field rather than an error.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.factory import Building, FactoryProfile, WorkforceComposition
from app.models.plant import Plant
from app.models.user import UserRole
from app.services.permissions import invalidate_user_permissions

# Re-use the CAMS batch name helper — same DB, same Plant table.
from app.services.cams import plant_name_map  # noqa: F401  (re-exported for the router)


# ── code generation (tenant-scoped sequential) ──────────────────────────────
async def next_factory_code(db: AsyncSession) -> str:
    n = (await db.execute(select(func.count()).select_from(FactoryProfile))).scalar() or 0
    return f"FAC-{(n + 1):04d}"


# ── Site (Plant) auto-provisioning for in-house factories ───────────────────
def _slug_code(source: str) -> str:
    """A Plant.code-safe token from a factory code/name (A-Z0-9 and dashes)."""
    token = re.sub(r"[^A-Za-z0-9]+", "-", source).strip("-").upper()
    # Strip AFTER the truncation too: cutting at 20 can land mid-separator and
    # leave a trailing dash ("(Unit-1)_Bommanahalli_Blr" -> "UNIT-1-BOMMANAHALLI-"),
    # which then shows up verbatim in every site dropdown.
    return (token[:20].strip("-") or "SITE")


async def _unique_plant_code(db: AsyncSession, base: str) -> str:
    """`base`, or base-2, base-3 … — Plant.code is unique."""
    code = base
    for n in range(2, 100):
        exists = (await db.execute(select(Plant.id).where(Plant.code == code))).scalars().first()
        if not exists:
            return code
        code = f"{base[:17]}-{n}"
    raise ValueError(f"Could not derive a free Plant code from '{base}'")


async def ensure_site_for_profile(
    db: AsyncSession,
    *,
    site_id: str | None,
    factory_name: str,
    factory_code: str,
    city: str,
    state: str,
    primary_industry: str,
) -> str:
    """Resolve the Site (Plant) a new factory profile links to.

    A supplier factory is mapped onto a Site that already exists — pass its id
    and it is validated and returned. A Page-owned in-house factory usually has
    no separate Site concept in the operator's head, so `site_id=None` means
    "this factory IS the site": a Plant row is provisioned from the factory's
    own identity and its id returned.

    The 1:1 FactoryProfile↔Plant invariant is therefore never relaxed — only the
    data-entry burden is. Everything downstream (RBAC plant scoping, the
    building/workforce/certification registers, the live compliance rollups)
    keeps working unchanged because it all keys off a real siteId.
    """
    if site_id:
        plant = await db.get(Plant, site_id)
        if plant is None:
            raise ValueError("The selected Site no longer exists.")
        return plant.id

    code = await _unique_plant_code(db, _slug_code(factory_code or factory_name))
    plant = Plant(
        code=code,
        name=factory_name,
        location=city or "—",
        state=state or "—",
        # Plant.unitType is a free-text descriptor elsewhere in the platform;
        # the factory's own industry is the most honest value available here.
        unitType=primary_industry or "Factory",
    )
    db.add(plant)
    await db.flush()  # assign plant.id
    return plant.id


async def grant_site_access(db: AsyncSession, *, plant_id: str, created_by: str) -> int:
    """Give a freshly-provisioned Site to the people who must be able to see it.

    Provisioning a Plant row is only half of "the factory exists". Every
    plant-scoped surface on the platform — the audit Owning-site dropdown, the
    plant switcher, the registers — resolves through `UserRole(scopeType='PLANT')`
    plus `User.plantId`, and a brand-new Plant is on nobody's list. Before this,
    an HSE Manager could add a factory, land back on the Facilities dashboard
    seeing it, and then find it absent from the Owning-site dropdown one click
    later — because `AUDIT_COMPLIANCE.READ` is OWN_PLANT for that role and the
    new site was in no one's plant set. The site was real and invisible.

    Two grants are written, both narrow on purpose:

      * the creator, under each role they already hold. This widens *reach*, not
        *authority* — they keep exactly the permissions that role already
        carried, now applicable to the site they just created.
      * anyone who already holds a PLANT grant on EVERY pre-existing site. That
        is the platform's existing way of spelling "this user covers the whole
        estate" (the RBAC seed writes one row per plant rather than an
        ALL_PLANTS scope), so a new site joining the estate must join their set
        too or their coverage silently develops a hole.

    A user with ALL_PLANTS needs nothing — `get_accessible_plants_for` returns
    None for them and the new site is already included.

    Returns the number of UserRole rows written.
    """
    # Every other site — "covers the whole estate" is measured against these.
    other_sites = {
        r[0] for r in (await db.execute(select(Plant.id).where(Plant.id != plant_id))).all()
    }

    existing = (
        await db.execute(
            select(UserRole.userId, UserRole.roleId).where(
                UserRole.scopeType == "PLANT", UserRole.scopeValue == plant_id
            )
        )
    ).all()
    already = {(u, r) for u, r in existing}

    # (userId, roleId) pairs to grant. The creator first.
    wanted: set[tuple[str, str]] = {
        (created_by, r[0])
        for r in (
            await db.execute(select(UserRole.roleId).where(UserRole.userId == created_by))
        ).all()
    }

    # Then the estate-wide users. Group each user's PLANT grants by role and
    # keep the (user, role) pairs whose plant set already covers every other
    # site — a partial-coverage role is a deliberate subset and is left alone.
    if other_sites:
        rows = (
            await db.execute(
                select(UserRole.userId, UserRole.roleId, UserRole.scopeValue).where(
                    UserRole.scopeType == "PLANT", UserRole.scopeValue.is_not(None)
                )
            )
        ).all()
        coverage: dict[tuple[str, str], set[str]] = {}
        for user_id, role_id, site in rows:
            coverage.setdefault((user_id, role_id), set()).add(site)
        wanted |= {pair for pair, sites in coverage.items() if other_sites <= sites}

    written = 0
    for user_id, role_id in sorted(wanted - already):
        db.add(
            UserRole(
                userId=user_id,
                roleId=role_id,
                scopeType="PLANT",
                scopeValue=plant_id,
                assignedById=created_by,
            )
        )
        written += 1
        # The permission snapshot is cached for 5 minutes; without this the
        # creator would not see their own new site until it expired.
        invalidate_user_permissions(user_id)
    if written:
        await db.flush()
    return written


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
