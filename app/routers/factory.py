"""Facilities — Factory Profile Master router.

A descriptive/compliance layer 1:1 with the existing Plant (= "Site"). Owns
profile attributes + the building register; the consolidated dashboard reads
live operational metrics per siteId from the existing engines (Phase D). All
endpoints plant-scoped + RBAC-enforced.

Permission codes (seeded in seed-rbac.ts):
  FACILITY.READ    view profiles / buildings / consolidated dashboard
  FACILITY.CREATE  create a factory profile (1:1 with a Site)
  FACILITY.UPDATE  edit profile + manage buildings / floors / floor activities
  FACILITY.DELETE  archive a factory profile (soft delete)
  FACILITY.PROFILE_APPROVE_UNIT        Plant Head sign-off on a profile edit
  FACILITY.PROFILE_APPROVE_COMPLIANCE  Lead Auditor sign-off on a profile edit
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.factory import (
    Building,
    BuildingFloor,
    BuildingFloorActivity,
    FactoryCertification,
    FactoryComplianceSnapshot,
    FactoryContact,
    FactoryProfile,
    ProductionProcess,
    SocialComplianceProfile,
    WorkforceComposition,
)
from app.models.factory_ext import (
    FactoryEquipment,
    FactoryEquipmentInspection,
    FactoryLifecycleEvent,
    FactoryProfileChangeRequest,
    HazardousMaterial,
    RegulatoryRegistration,
)
from app.models.user import User
from app.schemas import factory as S
from app.schemas import factory_ext as Sx
from app.services import factory as svc
from app.services import factory_ext as svc_ext
from app.services import factory_snapshot as snap_svc
from app.services.permissions import PermissionContext, can, get_accessible_plants

router = APIRouter(prefix="/api/factory", tags=["factory"])


async def _require(db: AsyncSession, user: User, code: str, *, plant_id=None) -> None:
    res = await can(db, user.id, code, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission {code}")


async def _read_profile(db: AsyncSession, user: User, profile_id: str) -> FactoryProfile:
    """Fetch a profile and enforce site-scoped FACILITY.READ. `can()` is permissive
    when no plant_id is supplied (pages are expected to filter), so reads MUST pass
    the profile's siteId — otherwise a plant-scoped Factory Manager could read any
    site's data."""
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.READ", plant_id=p.siteId)
    return p


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── serialisation ────────────────────────────────────────────────────────────
def _profile_out(p: FactoryProfile, plants: dict[str, str]) -> S.FactoryProfileOut:
    o = S.FactoryProfileOut.model_validate(p)
    o.siteName = plants.get(p.siteId)
    return o


def _workforce_out(w: WorkforceComposition) -> S.WorkforceCompositionOut:
    o = S.WorkforceCompositionOut.model_validate(w)
    o.genderTotal = w.maleCount + w.femaleCount + w.otherGenderCount
    o.genderMismatch = o.genderTotal != w.totalCount
    o.childLabourFlag = svc.child_labour_flag(w.youngestWorkerAge, w.workersUnder18Count, w.minHiringAgePolicy)
    return o


def _social_out(s: SocialComplianceProfile) -> S.SocialComplianceProfileOut:
    return S.SocialComplianceProfileOut.model_validate(s)


def _building_out(b: Building, floors_by_building: dict[str, list[S.BuildingFloorOut]]) -> S.BuildingOut:
    # `BuildingOut.floorRegister` is a declared field, so Pydantic's
    # from_attributes reads `b.floorRegister` — an unloaded lazy relationship,
    # which emits IO outside the async greenlet (MissingGreenlet → 500).
    # Assigning after model_validate() is too late; the attribute must already
    # be resolved when validation runs. set_committed_value() marks it loaded
    # without any IO, then the real register (queried in one batch) is attached.
    set_committed_value(b, "floorRegister", [])
    o = S.BuildingOut.model_validate(b)
    o.floorRegister = floors_by_building.get(b.id, [])
    return o


async def _building_detail(db: AsyncSession, b: Building) -> S.BuildingOut:
    """One building with its floor register attached (single-row equivalent of
    the batched `_floors_by_building` used by the profile detail page)."""
    return _building_out(b, await _floors_by_building(db, b.factoryProfileId))


def _cert_out(c: FactoryCertification) -> S.FactoryCertificationOut:
    o = S.FactoryCertificationOut.model_validate(c)
    o.status = svc.compute_cert_status(c.expiryDate, c.renewalLeadDays, c.status)
    o.daysToExpiry = svc.cert_days_to_expiry(c.expiryDate)
    return o


async def _floors_by_building(db: AsyncSession, profile_id: str) -> dict[str, list[S.BuildingFloorOut]]:
    """Floor register + mapped activities, grouped by buildingId.

    Loaded with explicit selects (never the lazy relationship, which would fire
    outside the async greenlet) and assembled in Python so the whole detail page
    costs two queries regardless of how many buildings a factory has.
    """
    floors = (
        await db.execute(
            select(BuildingFloor)
            .where(BuildingFloor.factoryProfileId == profile_id)
            .where(BuildingFloor.isDeleted.is_(False))
            .order_by(BuildingFloor.floorLevel.asc(), BuildingFloor.floorLabel.asc())
        )
    ).scalars().all()
    if not floors:
        return {}
    activities = (
        await db.execute(
            select(BuildingFloorActivity)
            .where(BuildingFloorActivity.factoryProfileId == profile_id)
            .where(BuildingFloorActivity.isDeleted.is_(False))
            .order_by(
                BuildingFloorActivity.sequenceOrder.asc().nulls_last(),
                BuildingFloorActivity.activityName.asc(),
            )
        )
    ).scalars().all()
    acts_by_floor: dict[str, list[S.FloorActivityOut]] = {}
    for a in activities:
        acts_by_floor.setdefault(a.floorId, []).append(S.FloorActivityOut.model_validate(a))

    out: dict[str, list[S.BuildingFloorOut]] = {}
    for f in floors:
        # Same lazy-relationship trap as _building_out(): `activities` is a
        # declared field, so validation touches f.activities before the line
        # below can set it. Mark it loaded (no IO) first.
        set_committed_value(f, "activities", [])
        floor_out = S.BuildingFloorOut.model_validate(f)
        floor_out.activities = acts_by_floor.get(f.id, [])
        out.setdefault(f.buildingId, []).append(floor_out)
    return out


async def _profile_detail(db: AsyncSession, p: FactoryProfile) -> S.FactoryProfileDetail:
    plants = await svc.plant_name_map(db, [p.siteId])
    buildings = (
        await db.execute(
            select(Building)
            .where(Building.factoryProfileId == p.id)
            .where(Building.isDeleted.is_(False))
            .order_by(Building.buildingName.asc())
        )
    ).scalars().all()
    floors_by_building = await _floors_by_building(db, p.id)
    workforce = (
        await db.execute(
            select(WorkforceComposition)
            .where(WorkforceComposition.factoryProfileId == p.id)
            .where(WorkforceComposition.isDeleted.is_(False))
            .order_by(WorkforceComposition.asOfDate.desc())
        )
    ).scalars().all()
    processes = (
        await db.execute(
            select(ProductionProcess)
            .where(ProductionProcess.factoryProfileId == p.id)
            .where(ProductionProcess.isDeleted.is_(False))
            .order_by(ProductionProcess.sequenceOrder.asc().nulls_last(), ProductionProcess.processName.asc())
        )
    ).scalars().all()
    certs = (
        await db.execute(
            select(FactoryCertification)
            .where(FactoryCertification.factoryProfileId == p.id)
            .where(FactoryCertification.isDeleted.is_(False))
            .order_by(FactoryCertification.certificationType.asc())
        )
    ).scalars().all()
    contacts = (
        await db.execute(
            select(FactoryContact)
            .where(FactoryContact.factoryProfileId == p.id)
            .where(FactoryContact.isDeleted.is_(False))
            .order_by(FactoryContact.isPrimary.desc(), FactoryContact.name.asc())
        )
    ).scalars().all()
    social = (
        await db.execute(
            select(SocialComplianceProfile)
            .where(SocialComplianceProfile.factoryProfileId == p.id)
            .where(SocialComplianceProfile.isDeleted.is_(False))
        )
    ).scalars().first()
    # Validate the scalar base first (avoids Pydantic touching lazy
    # relationships → async lazy-load outside the greenlet), then attach the
    # explicitly-queried children.
    base = _profile_out(p, plants)
    cert_outs = [_cert_out(c) for c in certs]
    base.certCount = len(cert_outs)
    base.certsExpiringCount = sum(1 for c in cert_outs if svc.cert_is_expiring(c.status))
    current = next((w for w in workforce if w.isCurrent), None)
    extras = await svc_ext.load_profile_extras(db, p.id)
    return S.FactoryProfileDetail(
        **base.model_dump(),
        buildings=[_building_out(b, floors_by_building) for b in buildings],
        currentWorkforce=_workforce_out(current) if current else None,
        workforceHistory=[_workforce_out(w) for w in workforce],
        processes=[S.ProductionProcessOut.model_validate(pr) for pr in processes],
        certifications=cert_outs,
        contacts=[S.FactoryContactOut.model_validate(ct) for ct in contacts],
        socialCompliance=_social_out(social) if social else None,
        equipment=extras["equipment"],
        hazardousMaterials=extras["hazardousMaterials"],
        regulatoryRegistrations=extras["regulatoryRegistrations"],
        lifecycleEvents=extras["lifecycleEvents"],
        changeRequests=extras["changeRequests"],
        pendingChangeRequest=extras["pendingChangeRequest"],
        editRequiresApproval=svc_ext.profile_edit_is_governed(p.lifecycleStage),
    )


# ════════════════════════════════════════════════════════════════════════════
# Profiles  (F-01 list / F-02 detail / F-03 create)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/profiles", response_model=S.FactoryProfileListResponse)
async def list_profiles(
    state: str | None = Query(None),
    pstatus: str | None = Query(None, alias="status"),
    profileStatus: str | None = Query(None),
    siteId: str | None = Query(None),
    q: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require(db, user, "FACILITY.READ")
    # Plant-scope the dashboard: a Factory Manager (OWN_PLANT) sees only their
    # site(s); a group manager (ALL_PLANTS) gets None ⇒ unrestricted.
    accessible = await get_accessible_plants(db, user.id)
    if accessible is not None and not accessible:
        return S.FactoryProfileListResponse(items=[], total=0)
    stmt = select(FactoryProfile).where(FactoryProfile.isDeleted.is_(False))
    if accessible is not None:
        stmt = stmt.where(FactoryProfile.siteId.in_(accessible))
    if state:
        stmt = stmt.where(FactoryProfile.state == state)
    if pstatus:
        stmt = stmt.where(FactoryProfile.status == pstatus)
    if profileStatus:
        stmt = stmt.where(FactoryProfile.profileStatus == profileStatus)
    if siteId:
        stmt = stmt.where(FactoryProfile.siteId == siteId)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(FactoryProfile.factoryName.ilike(like), FactoryProfile.factoryCode.ilike(like), FactoryProfile.city.ilike(like)))
    # Newest-created first — platform-wide register convention.
    rows = (
        await db.execute(
            stmt.order_by(FactoryProfile.createdAt.desc(), FactoryProfile.id.desc())
        )
    ).scalars().all()

    plants = await svc.plant_name_map(db, [p.siteId for p in rows])

    # Batch-load certs for the listed profiles → per-profile + group cert counts.
    profile_ids = [p.id for p in rows]
    cert_by_profile: dict[str, list[FactoryCertification]] = {}
    if profile_ids:
        cert_rows = (
            await db.execute(
                select(FactoryCertification)
                .where(FactoryCertification.factoryProfileId.in_(profile_ids))
                .where(FactoryCertification.isDeleted.is_(False))
            )
        ).scalars().all()
        for c in cert_rows:
            cert_by_profile.setdefault(c.factoryProfileId, []).append(c)

    # Batch-load the LIVE compliance snapshot per profile (precompute cache).
    snap_by_profile: dict[str, FactoryComplianceSnapshot] = {}
    if profile_ids:
        snap_rows = (
            await db.execute(
                select(FactoryComplianceSnapshot)
                .where(FactoryComplianceSnapshot.factoryProfileId.in_(profile_ids))
                .where(FactoryComplianceSnapshot.periodLabel == "LIVE")
                .where(FactoryComplianceSnapshot.isDeleted.is_(False))
            )
        ).scalars().all()
        snap_by_profile = {s.factoryProfileId: s for s in snap_rows}

    # Lazy-populate: any factory without a LIVE snapshot gets one computed now
    # (self-healing for newly-created factories; existing rows use the cache).
    missing = [p for p in rows if p.id not in snap_by_profile]
    if missing:
        for p in missing:
            snap_by_profile[p.id] = await snap_svc.recompute_snapshot(db, p, user.id)
        await db.commit()

    items: list[S.FactoryProfileOut] = []
    group_expiring = 0
    score_sum, score_n, group_open_capas, group_overdue_capas = 0.0, 0, 0, 0
    status_counts: dict[str, int] = {}
    state_counts: dict[str, int] = {}
    for p in rows:
        o = _profile_out(p, plants)
        certs = cert_by_profile.get(p.id, [])
        o.certCount = len(certs)
        o.certsExpiringCount = sum(
            1 for c in certs if svc.cert_is_expiring(svc.compute_cert_status(c.expiryDate, c.renewalLeadDays, c.status))
        )
        group_expiring += o.certsExpiringCount
        sn = snap_by_profile.get(p.id)
        if sn is not None:
            o.metrics = S.SnapshotMetrics(
                auditComplianceScorePct=sn.auditComplianceScorePct, openFindings=sn.openFindings,
                criticalFindings=sn.criticalFindings, openCapas=sn.openCapas, overdueCapas=sn.overdueCapas,
                openObligations=sn.openObligations, overdueObligations=sn.overdueObligations,
                certsExpiringCount=sn.certsExpiringCount, incidentCount12m=sn.incidentCount12m,
                lastAuditDate=sn.lastAuditDate, computedAt=sn.computedAt,
            )
            if sn.auditComplianceScorePct is not None:
                score_sum += sn.auditComplianceScorePct
                score_n += 1
            group_open_capas += sn.openCapas
            group_overdue_capas += sn.overdueCapas
        items.append(o)
        status_counts[p.status] = status_counts.get(p.status, 0) + 1
        if p.state:
            state_counts[p.state] = state_counts.get(p.state, 0) + 1

    return S.FactoryProfileListResponse(
        items=items,
        total=len(items),
        totalBuildings=sum(p.buildingCount for p in rows),
        totalEmployees=sum(p.totalEmployees for p in rows),
        certsExpiring=group_expiring,
        groupComplianceScore=round(score_sum / score_n, 1) if score_n else None,
        groupOpenCapas=group_open_capas,
        groupOverdueCapas=group_overdue_capas,
        statusCounts=status_counts,
        stateCounts=state_counts,
    )


@router.post("/profiles", response_model=S.FactoryProfileDetail, status_code=201)
async def create_profile(body: S.FactoryProfileCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _require(db, user, "FACILITY.CREATE", plant_id=body.siteId)
    if body.siteId:
        # Attaching to an EXISTING Site is the supplier case: the factory is
        # being placed under a Site someone else already maintains, and that
        # mapping is what FACILITY.SITE_LINK governs. Only the compliance /
        # supplier lead auditor holds it, and the Add Factory wizard hides the
        # Site picker from everyone else — this is the boundary behind it, since
        # a hidden field stops nobody from posting a siteId by hand.
        #
        # This guard used to sit on the OPPOSITE branch, requiring SITE_LINK to
        # leave the Site blank, on the reasoning that `can()` is permissive with
        # no plant_id so a plant-scoped user could mint Sites outside their
        # scope. That made auto-provisioning the privileged act and attaching the
        # ordinary one — the reverse of how the two are actually used. A
        # provisioned Site is built from this factory's own name and location and
        # is reachable only through the profile that created it, so an ordinary
        # creator making one is just what "create a factory" means here.
        await _require(db, user, "FACILITY.SITE_LINK")

    if body.siteId:
        # Enforce the 1:1 site mapping (TF-01) — a Site carries one profile. The
        # DB unique constraint covers soft-deleted rows too, so check ANY existing
        # row (not just active) and return a clean 409 rather than a 500
        # unique-violation. An auto-provisioned Site is new, so it can't collide.
        existing = (
            await db.execute(select(FactoryProfile).where(FactoryProfile.siteId == body.siteId))
        ).scalars().first()
        if existing and not existing.isDeleted:
            raise HTTPException(status.HTTP_409_CONFLICT, "A factory profile already exists for this site.")
        if existing and existing.isDeleted:
            raise HTTPException(status.HTTP_409_CONFLICT, "A factory profile for this site was archived; restore it instead of creating a new one.")

    factory_code = body.factoryCode or await svc.next_factory_code(db)
    # Site selection is optional: supplier factories map onto an existing Site,
    # a Page-owned in-house factory gets one provisioned from its own identity.
    try:
        site_id = await svc.ensure_site_for_profile(
            db,
            site_id=body.siteId,
            factory_name=body.factoryName,
            factory_code=factory_code,
            city=body.city,
            state=body.state,
            primary_industry=body.primaryIndustry,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    if not body.siteId:
        # A Site was just provisioned, so it is on nobody's plant list. Without
        # this the factory is created, the dashboard shows it, and the audit
        # Owning-site dropdown does not — plant-scoped roles resolve sites
        # through UserRole(scopeType='PLANT'), and a new Plant has no rows.
        await svc.grant_site_access(db, plant_id=site_id, created_by=user.id)

    p = FactoryProfile(
        siteId=site_id,
        factoryCode=factory_code,
        factoryName=body.factoryName,
        status=body.status,
        ownershipType=body.ownershipType,
        addressLine=body.addressLine,
        city=body.city,
        state=body.state,
        pincode=body.pincode,
        latitude=body.latitude,
        longitude=body.longitude,
        establishedYear=body.establishedYear,
        factoryLicenseNo=body.factoryLicenseNo,
        factoryLicenseValidUntil=body.factoryLicenseValidUntil,
        registrationNos=[r.model_dump() for r in body.registrationNos],
        applicableActs=body.applicableActs,
        pollutionControlBoard=body.pollutionControlBoard,
        totalLandAreaSqm=body.totalLandAreaSqm,
        builtUpAreaSqm=body.builtUpAreaSqm,
        buildingCount=body.buildingCount or 0,
        primaryIndustry=body.primaryIndustry,
        profileStatus="DRAFT",
        createdBy=user.id,
    )
    db.add(p)
    await db.flush()  # assign p.id before attaching buildings

    for b in body.buildings:
        row = Building(
            factoryProfileId=p.id, siteId=p.siteId, buildingName=b.buildingName, buildingType=b.buildingType,
            floors=b.floors, areaSqm=b.areaSqm, maxOccupancy=b.maxOccupancy, currentOccupancy=b.currentOccupancy,
            yearBuilt=b.yearBuilt, assemblyPoint=b.assemblyPoint, emergencyExits=b.emergencyExits,
            occupancyCertificateNo=b.occupancyCertificateNo, isActive=b.isActive, createdBy=user.id,
        )
        db.add(row)
        await db.flush()  # assign row.id before its floor register
        # The wizard's floor count becomes real, mappable floors.
        await _seed_floor_register(db, row, user.id)
    await db.flush()
    await svc.recompute_building_count(db, p.id)

    # Initial workforce composition (optional) — reconcile + denormalise.
    if body.workforce is not None:
        w = body.workforce
        total, _mismatch = svc.reconcile_workforce(
            w.permanentCount, w.contractCount, w.apprenticeTraineeCount, w.maleCount, w.femaleCount, w.otherGenderCount
        )
        comp = WorkforceComposition(
            factoryProfileId=p.id, siteId=p.siteId, asOfDate=w.asOfDate or _now(), isCurrent=True,
            permanentCount=w.permanentCount, contractCount=w.contractCount, apprenticeTraineeCount=w.apprenticeTraineeCount,
            maleCount=w.maleCount, femaleCount=w.femaleCount, otherGenderCount=w.otherGenderCount,
            migrantWorkerCount=w.migrantWorkerCount, differentlyAbledCount=w.differentlyAbledCount,
            youngestWorkerAge=w.youngestWorkerAge, workersUnder18Count=w.workersUnder18Count,
            minHiringAgePolicy=w.minHiringAgePolicy,
            totalCount=total, notes=w.notes, createdBy=user.id,
        )
        svc.apply_workforce_derived(comp)
        db.add(comp)
        await db.flush()
        await svc.make_workforce_current(db, p, comp)

    # Initial production processes (optional).
    for idx, pr in enumerate(body.processes):
        db.add(ProductionProcess(
            factoryProfileId=p.id, siteId=p.siteId, processName=pr.processName, processCategory=pr.processCategory,
            description=pr.description, sequenceOrder=pr.sequenceOrder if pr.sequenceOrder is not None else idx + 1,
            shiftPattern=pr.shiftPattern, installedCapacity=pr.installedCapacity, keyHazards=pr.keyHazards,
            isActive=pr.isActive, createdBy=user.id,
        ))
    await db.flush()
    p.profileStatus = await svc.compute_profile_status(db, p)

    # Seed the lifecycle workflow at INITIATED with an opening event.
    p.lifecycleStageOwnerRole = svc_ext.stage_owner_role(p.lifecycleStage)
    p.lifecycleUpdatedAt = _now()
    db.add(FactoryLifecycleEvent(
        factoryProfileId=p.id, siteId=p.siteId, fromStage=None, toStage=p.lifecycleStage,
        action="INITIATE", performedBy=user.id, performedByRole=getattr(user, "role", None),
        comment="Factory profile created.", createdBy=user.id,
    ))

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "A factory profile already exists for this site.")
    await db.refresh(p)
    return await _profile_detail(db, p)


@router.get("/profiles/{profile_id}", response_model=S.FactoryProfileDetail)
async def get_profile(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await _read_profile(db, user, profile_id)
    return await _profile_detail(db, p)


@router.patch("/profiles/{profile_id}", response_model=S.FactoryProfileDetail)
async def update_profile(
    profile_id: str,
    body: S.FactoryProfileUpdate,
    reason: str | None = Query(None, description="Why the profile is being changed — recorded on the change request"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Edit a factory profile.

    Two paths, decided by the profile's own lifecycle stage:

    * **Still being drafted** (INITIATED / EXECUTION / VALIDATION) — the edit is
      written straight through, and recorded as an ``autoApplied`` change-request
      row so the version history has no gaps.
    * **Governed** (lifecycleStage ACTIVE) — nothing is written. A change request
      is opened instead and must be approved by the Plant Head at the Unit, then
      by the Compliance Team's Lead Auditor, before the values land. The response
      is the *unchanged* profile carrying ``pendingChangeRequest``.

    Either way an edit is never silent: every version is attributable.
    """
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.UPDATE", plant_id=p.siteId)

    data = body.model_dump(exclude_unset=True)
    if "registrationNos" in data and data["registrationNos"] is not None:
        data["registrationNos"] = [r if isinstance(r, dict) else r.model_dump() for r in body.registrationNos]

    diff = svc_ext.build_profile_diff(p, data)
    if not diff:
        # Nothing actually changed — don't open an approval nobody needs. The
        # completeness recompute still runs, since it also reflects child data
        # (workforce) that may have moved since the last write.
        if "profileStatus" not in data:
            p.profileStatus = await svc.compute_profile_status(db, p)
            await db.commit()
            await db.refresh(p)
        return await _profile_detail(db, p)

    governed = svc_ext.profile_edit_is_governed(p.lifecycleStage)
    if governed:
        open_cr = await svc_ext.open_change_request(db, p.id)
        if open_cr is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Change request v{open_cr.version} is already awaiting "
                f"{svc_ext.CR_STEP_ROLE_LABEL.get(open_cr.status, 'approval')}. "
                "Resolve or withdraw it before submitting another edit.",
            )
        cr = FactoryProfileChangeRequest(
            factoryProfileId=p.id,
            siteId=p.siteId,
            version=await svc_ext.next_change_request_version(db, p.id),
            changes=diff,
            reason=reason,
            status=svc_ext.CR_PENDING_UNIT,
            requestedBy=user.id,
            requestedByRole=getattr(user, "role", None),
            requestedAt=_now(),
            createdBy=user.id,
        )
        db.add(cr)
        await db.commit()
        await db.refresh(p)
        return await _profile_detail(db, p)

    # Pre-approval drafting — apply now, still record the version.
    for k, v in data.items():
        setattr(p, k, v)
    p.updatedBy = user.id
    # keep profileStatus honest unless the caller explicitly set it
    if "profileStatus" not in data:
        p.profileStatus = await svc.compute_profile_status(db, p)
    db.add(FactoryProfileChangeRequest(
        factoryProfileId=p.id,
        siteId=p.siteId,
        version=await svc_ext.next_change_request_version(db, p.id),
        changes=diff,
        reason=reason,
        status=svc_ext.CR_APPLIED,
        requestedBy=user.id,
        requestedByRole=getattr(user, "role", None),
        requestedAt=_now(),
        appliedAt=_now(),
        autoApplied=True,
        createdBy=user.id,
    ))

    await db.commit()
    await db.refresh(p)
    return await _profile_detail(db, p)


# ── Profile change requests (Plant Head → Compliance Lead Auditor) ───────────
@router.get("/profiles/{profile_id}/change-requests", response_model=list[Sx.ChangeRequestOut])
async def list_change_requests(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Full version history for a profile, newest first."""
    await _read_profile(db, user, profile_id)
    extras = await svc_ext.load_profile_extras(db, profile_id)
    return extras["changeRequests"]


async def _open_cr_or_404(db: AsyncSession, request_id: str) -> FactoryProfileChangeRequest:
    cr = await db.get(FactoryProfileChangeRequest, request_id)
    if not cr or cr.isDeleted:
        raise HTTPException(404, "Change request not found")
    return cr


@router.post("/change-requests/{request_id}/approve", response_model=S.FactoryProfileDetail)
async def approve_change_request(
    request_id: str,
    body: Sx.ChangeRequestDecision,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Approve the current step of a change request.

    PENDING_UNIT → PENDING_COMPLIANCE (Plant Head), then
    PENDING_COMPLIANCE → APPLIED (Lead Auditor), which is where the proposed
    values are finally written onto the profile.
    """
    cr = await _open_cr_or_404(db, request_id)
    if cr.status not in svc_ext.CR_OPEN_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Change request is already {cr.status}.")
    p = await db.get(FactoryProfile, cr.factoryProfileId)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, svc_ext.CR_STEP_PERMISSION[cr.status], plant_id=cr.siteId)

    # Segregation of duties: the person who asked for a change cannot be the one
    # who signs it off. An approval by the requester is not an approval.
    if cr.requestedBy and cr.requestedBy == user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You raised this change request — it must be approved by someone else.",
        )

    now = _now()
    if cr.status == svc_ext.CR_PENDING_UNIT:
        cr.unitApprovedBy = user.id
        cr.unitApprovedAt = now
        cr.unitApprovalComment = body.comment
        cr.status = svc_ext.CR_PENDING_COMPLIANCE
    else:
        if cr.unitApprovedBy and cr.unitApprovedBy == user.id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "You gave the Unit approval — the Compliance sign-off must be a second person.",
            )
        cr.complianceApprovedBy = user.id
        cr.complianceApprovedAt = now
        cr.complianceApprovalComment = body.comment
        cr.status = svc_ext.CR_APPLIED
        cr.appliedAt = now
        svc_ext.apply_change_request(p, cr)
        p.updatedBy = user.id
        p.lastReviewedAt = now
        p.profileStatus = await svc.compute_profile_status(db, p)
        db.add(FactoryLifecycleEvent(
            factoryProfileId=p.id, siteId=p.siteId, fromStage=p.lifecycleStage, toStage=p.lifecycleStage,
            action="ADVANCE", performedBy=user.id, performedByRole=getattr(user, "role", None),
            comment=f"Profile change request v{cr.version} approved and applied "
                    f"({len(cr.changes or [])} field(s)).",
            createdBy=user.id,
        ))
    cr.updatedBy = user.id
    await db.commit()
    await db.refresh(p)
    return await _profile_detail(db, p)


@router.post("/change-requests/{request_id}/reject", response_model=S.FactoryProfileDetail)
async def reject_change_request(
    request_id: str,
    body: Sx.ChangeRequestRejection,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reject at whichever step the request is sitting. The profile is untouched;
    the rejected row stays as history and the requester may submit a fresh one."""
    cr = await _open_cr_or_404(db, request_id)
    if cr.status not in svc_ext.CR_OPEN_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Change request is already {cr.status}.")
    p = await db.get(FactoryProfile, cr.factoryProfileId)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, svc_ext.CR_STEP_PERMISSION[cr.status], plant_id=cr.siteId)

    cr.rejectedAtStep = "UNIT" if cr.status == svc_ext.CR_PENDING_UNIT else "COMPLIANCE"
    cr.status = svc_ext.CR_REJECTED
    cr.rejectedBy = user.id
    cr.rejectedAt = _now()
    cr.rejectionReason = body.reason
    cr.updatedBy = user.id
    db.add(FactoryLifecycleEvent(
        factoryProfileId=p.id, siteId=p.siteId, fromStage=p.lifecycleStage, toStage=p.lifecycleStage,
        action="REJECT", performedBy=user.id, performedByRole=getattr(user, "role", None),
        comment=f"Profile change request v{cr.version} rejected at {cr.rejectedAtStep}: {body.reason}",
        createdBy=user.id,
    ))
    await db.commit()
    await db.refresh(p)
    return await _profile_detail(db, p)


@router.post("/change-requests/{request_id}/withdraw", response_model=S.FactoryProfileDetail)
async def withdraw_change_request(request_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """The requester (or anyone who may edit the profile) pulls a request back
    before it is decided — e.g. to correct it and resubmit."""
    cr = await _open_cr_or_404(db, request_id)
    if cr.status not in svc_ext.CR_OPEN_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Change request is already {cr.status}.")
    p = await db.get(FactoryProfile, cr.factoryProfileId)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.UPDATE", plant_id=cr.siteId)
    cr.status = svc_ext.CR_WITHDRAWN
    cr.updatedBy = user.id
    await db.commit()
    await db.refresh(p)
    return await _profile_detail(db, p)


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.DELETE", plant_id=p.siteId)
    p.isDeleted = True
    p.updatedBy = user.id
    # Cascade the soft-delete to children (the DB FK cascade only fires on a
    # HARD delete, so a soft-deleted profile would otherwise leave active orphans).
    for model in (
        Building, BuildingFloor, BuildingFloorActivity, WorkforceComposition, ProductionProcess,
        FactoryCertification, FactoryContact, SocialComplianceProfile, FactoryEquipment,
        FactoryEquipmentInspection, HazardousMaterial, RegulatoryRegistration,
        FactoryLifecycleEvent, FactoryProfileChangeRequest,
    ):
        await db.execute(
            update(model)
            .where(model.factoryProfileId == p.id)
            .where(model.isDeleted.is_(False))
            .values(isDeleted=True, updatedBy=user.id)
        )
    p.lifecycleStage = "ARCHIVED"
    await db.commit()


# ════════════════════════════════════════════════════════════════════════════
# Buildings  (F-02 Buildings tab) — each mutation re-syncs buildingCount (TF-02)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/profiles/{profile_id}/buildings", response_model=list[S.BuildingOut])
async def list_buildings(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _read_profile(db, user, profile_id)
    rows = (
        await db.execute(
            select(Building)
            .where(Building.factoryProfileId == profile_id)
            .where(Building.isDeleted.is_(False))
            .order_by(Building.buildingName.asc())
        )
    ).scalars().all()
    floors_by_building = await _floors_by_building(db, profile_id)
    return [_building_out(b, floors_by_building) for b in rows]


@router.post("/profiles/{profile_id}/buildings", response_model=S.BuildingOut, status_code=201)
async def create_building(profile_id: str, body: S.BuildingCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.UPDATE", plant_id=p.siteId)
    b = Building(
        factoryProfileId=p.id, siteId=p.siteId, buildingName=body.buildingName, buildingType=body.buildingType,
        floors=body.floors, areaSqm=body.areaSqm, maxOccupancy=body.maxOccupancy, currentOccupancy=body.currentOccupancy,
        yearBuilt=body.yearBuilt, assemblyPoint=body.assemblyPoint, emergencyExits=body.emergencyExits,
        occupancyCertificateNo=body.occupancyCertificateNo, isActive=body.isActive, createdBy=user.id,
    )
    db.add(b)
    await db.flush()
    await _seed_floor_register(db, b, user.id)
    await svc.recompute_building_count(db, p.id)
    await db.commit()
    await db.refresh(b)
    return await _building_detail(db, b)


@router.patch("/buildings/{building_id}", response_model=S.BuildingOut)
async def update_building(building_id: str, body: S.BuildingUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    b = await db.get(Building, building_id)
    if not b or b.isDeleted:
        raise HTTPException(404, "Building not found")
    p = await db.get(FactoryProfile, b.factoryProfileId)
    await _require(db, user, "FACILITY.UPDATE", plant_id=b.siteId)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(b, k, v)
    b.updatedBy = user.id
    await db.flush()
    # Raising the floor count opens up the new levels for mapping straight away.
    await _seed_floor_register(db, b, user.id)
    if p:
        await svc.recompute_building_count(db, p.id)
    await db.commit()
    await db.refresh(b)
    return await _building_detail(db, b)


@router.delete("/buildings/{building_id}", status_code=204)
async def delete_building(building_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    b = await db.get(Building, building_id)
    if not b or b.isDeleted:
        raise HTTPException(404, "Building not found")
    await _require(db, user, "FACILITY.UPDATE", plant_id=b.siteId)
    b.isDeleted = True
    b.updatedBy = user.id
    # Cascade to the floor register + its activity mapping (the DB FK cascade
    # only fires on a HARD delete, so these would otherwise be live orphans).
    for model in (BuildingFloor, BuildingFloorActivity):
        await db.execute(
            update(model)
            .where(model.buildingId == b.id)
            .where(model.isDeleted.is_(False))
            .values(isDeleted=True, updatedBy=user.id)
        )
    await db.flush()
    await svc.recompute_building_count(db, b.factoryProfileId)
    await db.commit()


# ════════════════════════════════════════════════════════════════════════════
# Floor register + per-floor activity mapping
# ────────────────────────────────────────────────────────────────────────────
# `Building.floors` is a count; these are the addressable levels, and each one
# carries as many activities as it actually hosts — Block A / Floor 1 / Sewing,
# Floor 2 / Packing, Floor 3 / Canteen, DG yard / 1250 kVA DG set + STP.
# Every measure has a fixed unit baked into its field name; there is no unit
# picker anywhere in this register by design.
# ════════════════════════════════════════════════════════════════════════════
async def _building_for_write(db: AsyncSession, user: User, building_id: str) -> Building:
    b = await db.get(Building, building_id)
    if not b or b.isDeleted:
        raise HTTPException(404, "Building not found")
    await _require(db, user, "FACILITY.UPDATE", plant_id=b.siteId)
    return b


async def _floor_for_write(db: AsyncSession, user: User, floor_id: str) -> BuildingFloor:
    f = await db.get(BuildingFloor, floor_id)
    if not f or f.isDeleted:
        raise HTTPException(404, "Floor not found")
    await _require(db, user, "FACILITY.UPDATE", plant_id=f.siteId)
    return f


async def _floor_out(db: AsyncSession, f: BuildingFloor) -> S.BuildingFloorOut:
    # See _building_out(): validating the ORM row would lazy-load `activities`
    # outside the greenlet. Mark it loaded first; the real rows are attached below.
    set_committed_value(f, "activities", [])
    o = S.BuildingFloorOut.model_validate(f)
    rows = (
        await db.execute(
            select(BuildingFloorActivity)
            .where(BuildingFloorActivity.floorId == f.id)
            .where(BuildingFloorActivity.isDeleted.is_(False))
            .order_by(
                BuildingFloorActivity.sequenceOrder.asc().nulls_last(),
                BuildingFloorActivity.activityName.asc(),
            )
        )
    ).scalars().all()
    o.activities = [S.FloorActivityOut.model_validate(a) for a in rows]
    return o


async def _assert_free_floor_level(db: AsyncSession, building_id: str, level: int, *, exclude_id: str | None = None) -> None:
    stmt = (
        select(BuildingFloor.id)
        .where(BuildingFloor.buildingId == building_id)
        .where(BuildingFloor.floorLevel == level)
        .where(BuildingFloor.isDeleted.is_(False))
    )
    if exclude_id:
        stmt = stmt.where(BuildingFloor.id != exclude_id)
    if (await db.execute(stmt)).scalars().first():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This building already has a floor at level {level}. "
            "Edit that floor instead, or give this one a different level.",
        )


@router.get("/buildings/{building_id}/floors", response_model=list[S.BuildingFloorOut])
async def list_floors(building_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    b = await db.get(Building, building_id)
    if not b or b.isDeleted:
        raise HTTPException(404, "Building not found")
    await _require(db, user, "FACILITY.READ", plant_id=b.siteId)
    floors = (
        await db.execute(
            select(BuildingFloor)
            .where(BuildingFloor.buildingId == building_id)
            .where(BuildingFloor.isDeleted.is_(False))
            .order_by(BuildingFloor.floorLevel.asc())
        )
    ).scalars().all()
    return [await _floor_out(db, f) for f in floors]


@router.post("/buildings/{building_id}/floors", response_model=S.BuildingFloorOut, status_code=201)
async def create_floor(building_id: str, body: S.BuildingFloorCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    b = await _building_for_write(db, user, building_id)
    await _assert_free_floor_level(db, b.id, body.floorLevel)
    f = BuildingFloor(
        factoryProfileId=b.factoryProfileId, buildingId=b.id, siteId=b.siteId,
        floorLabel=body.floorLabel, floorLevel=body.floorLevel, areaSqm=body.areaSqm,
        headroomM=body.headroomM, occupancyPersons=body.occupancyPersons, notes=body.notes,
        isActive=body.isActive, createdBy=user.id,
    )
    db.add(f)
    await db.flush()
    for idx, a in enumerate(body.activities):
        db.add(_new_activity(f, a, idx, user.id))
    # A floor register that outgrows the headline count keeps the count honest —
    # never shrinks it, since a building may have levels not yet registered.
    registered = len(await _active_floor_ids(db, b.id))
    if registered > b.floors:
        b.floors = registered
        b.updatedBy = user.id
    await db.commit()
    await db.refresh(f)
    return await _floor_out(db, f)


async def _active_floor_ids(db: AsyncSession, building_id: str) -> list[str]:
    return list(
        (
            await db.execute(
                select(BuildingFloor.id)
                .where(BuildingFloor.buildingId == building_id)
                .where(BuildingFloor.isDeleted.is_(False))
            )
        ).scalars().all()
    )


def _default_floor_label(level: int) -> str:
    if level == 0:
        return "Ground Floor"
    return f"Basement {-level}" if level < 0 else f"Floor {level}"


async def _seed_floor_register(db: AsyncSession, b: Building, user_id: str) -> None:
    """Give a building the floor rows its `floors` count implies.

    Saying "Block A has 4 floors" should be enough to start mapping what is on
    each of them — nobody should have to re-enter the same four levels by hand.
    Additive only: existing levels are left alone and a level is never removed,
    so lowering the count (or renaming a floor) is safe.
    """
    existing = (
        await db.execute(
            select(BuildingFloor.floorLevel)
            .where(BuildingFloor.buildingId == b.id)
            .where(BuildingFloor.isDeleted.is_(False))
        )
    ).scalars().all()
    have = set(existing)
    for level in range(max(b.floors, 1)):
        if level in have:
            continue
        db.add(BuildingFloor(
            factoryProfileId=b.factoryProfileId, buildingId=b.id, siteId=b.siteId,
            floorLabel=_default_floor_label(level), floorLevel=level, createdBy=user_id,
        ))


def _new_activity(f: BuildingFloor, a: S.FloorActivityCreate, idx: int, user_id: str) -> BuildingFloorActivity:
    return BuildingFloorActivity(
        factoryProfileId=f.factoryProfileId, buildingId=f.buildingId, floorId=f.id, siteId=f.siteId,
        activityType=a.activityType, activityName=a.activityName, processId=a.processId,
        description=a.description,
        sequenceOrder=a.sequenceOrder if a.sequenceOrder is not None else idx + 1,
        areaSqm=a.areaSqm, headcount=a.headcount,
        productionCapacityPcsPerDay=a.productionCapacityPcsPerDay,
        fabricConsumptionMPerDay=a.fabricConsumptionMPerDay,
        powerRatingKva=a.powerRatingKva, waterCapacityKld=a.waterCapacityKld,
        wasteGeneratedKgPerDay=a.wasteGeneratedKgPerDay,
        keyHazards=a.keyHazards, isActive=a.isActive, createdBy=user_id,
    )


@router.patch("/floors/{floor_id}", response_model=S.BuildingFloorOut)
async def update_floor(floor_id: str, body: S.BuildingFloorUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    f = await _floor_for_write(db, user, floor_id)
    data = body.model_dump(exclude_unset=True)
    if "floorLevel" in data and data["floorLevel"] is not None and data["floorLevel"] != f.floorLevel:
        await _assert_free_floor_level(db, f.buildingId, data["floorLevel"], exclude_id=f.id)
    for k, v in data.items():
        setattr(f, k, v)
    f.updatedBy = user.id
    await db.commit()
    await db.refresh(f)
    return await _floor_out(db, f)


@router.delete("/floors/{floor_id}", status_code=204)
async def delete_floor(floor_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    f = await _floor_for_write(db, user, floor_id)
    f.isDeleted = True
    f.updatedBy = user.id
    await db.execute(
        update(BuildingFloorActivity)
        .where(BuildingFloorActivity.floorId == f.id)
        .where(BuildingFloorActivity.isDeleted.is_(False))
        .values(isDeleted=True, updatedBy=user.id)
    )
    await db.commit()


@router.post("/floors/{floor_id}/activities", response_model=S.FloorActivityOut, status_code=201)
async def create_floor_activity(floor_id: str, body: S.FloorActivityCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    f = await _floor_for_write(db, user, floor_id)
    existing = len(
        (
            await db.execute(
                select(BuildingFloorActivity.id)
                .where(BuildingFloorActivity.floorId == f.id)
                .where(BuildingFloorActivity.isDeleted.is_(False))
            )
        ).scalars().all()
    )
    a = _new_activity(f, body, existing, user.id)
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return S.FloorActivityOut.model_validate(a)


@router.patch("/floor-activities/{activity_id}", response_model=S.FloorActivityOut)
async def update_floor_activity(activity_id: str, body: S.FloorActivityUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    a = await db.get(BuildingFloorActivity, activity_id)
    if not a or a.isDeleted:
        raise HTTPException(404, "Floor activity not found")
    await _require(db, user, "FACILITY.UPDATE", plant_id=a.siteId)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(a, k, v)
    a.updatedBy = user.id
    await db.commit()
    await db.refresh(a)
    return S.FloorActivityOut.model_validate(a)


@router.delete("/floor-activities/{activity_id}", status_code=204)
async def delete_floor_activity(activity_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    a = await db.get(BuildingFloorActivity, activity_id)
    if not a or a.isDeleted:
        raise HTTPException(404, "Floor activity not found")
    await _require(db, user, "FACILITY.UPDATE", plant_id=a.siteId)
    a.isDeleted = True
    a.updatedBy = user.id
    await db.commit()


# ════════════════════════════════════════════════════════════════════════════
# Workforce composition (F-02 Workforce tab) — SA8000 lens; history retained
# ════════════════════════════════════════════════════════════════════════════
@router.get("/profiles/{profile_id}/workforce", response_model=list[S.WorkforceCompositionOut])
async def list_workforce(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _read_profile(db, user, profile_id)
    rows = (
        await db.execute(
            select(WorkforceComposition)
            .where(WorkforceComposition.factoryProfileId == profile_id)
            .where(WorkforceComposition.isDeleted.is_(False))
            .order_by(WorkforceComposition.asOfDate.desc())
        )
    ).scalars().all()
    return [_workforce_out(w) for w in rows]


@router.post("/profiles/{profile_id}/workforce", response_model=S.WorkforceCompositionOut, status_code=201)
async def add_workforce(profile_id: str, body: S.WorkforceCompositionCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.WORKFORCE_UPDATE", plant_id=p.siteId)
    total, _mismatch = svc.reconcile_workforce(
        body.permanentCount, body.contractCount, body.apprenticeTraineeCount, body.maleCount, body.femaleCount, body.otherGenderCount
    )
    comp = WorkforceComposition(
        factoryProfileId=p.id, siteId=p.siteId, asOfDate=body.asOfDate or _now(), isCurrent=True,
        permanentCount=body.permanentCount, contractCount=body.contractCount, apprenticeTraineeCount=body.apprenticeTraineeCount,
        maleCount=body.maleCount, femaleCount=body.femaleCount, otherGenderCount=body.otherGenderCount,
        migrantWorkerCount=body.migrantWorkerCount, differentlyAbledCount=body.differentlyAbledCount,
        youngestWorkerAge=body.youngestWorkerAge, workersUnder18Count=body.workersUnder18Count,
        minHiringAgePolicy=body.minHiringAgePolicy,
        totalCount=total, notes=body.notes, createdBy=user.id,
    )
    svc.apply_workforce_derived(comp)
    db.add(comp)
    await db.flush()
    await svc.make_workforce_current(db, p, comp)  # flip prior → historical + write totalEmployees
    p.profileStatus = await svc.compute_profile_status(db, p)
    await db.commit()
    await db.refresh(comp)
    return _workforce_out(comp)


# ════════════════════════════════════════════════════════════════════════════
# Production processes (F-02 Production Processes tab)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/profiles/{profile_id}/processes", response_model=list[S.ProductionProcessOut])
async def list_processes(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _read_profile(db, user, profile_id)
    rows = (
        await db.execute(
            select(ProductionProcess)
            .where(ProductionProcess.factoryProfileId == profile_id)
            .where(ProductionProcess.isDeleted.is_(False))
            .order_by(ProductionProcess.sequenceOrder.asc().nulls_last(), ProductionProcess.processName.asc())
        )
    ).scalars().all()
    return [S.ProductionProcessOut.model_validate(pr) for pr in rows]


@router.post("/profiles/{profile_id}/processes", response_model=S.ProductionProcessOut, status_code=201)
async def create_process(profile_id: str, body: S.ProductionProcessCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.UPDATE", plant_id=p.siteId)
    pr = ProductionProcess(
        factoryProfileId=p.id, siteId=p.siteId, processName=body.processName, processCategory=body.processCategory,
        description=body.description, sequenceOrder=body.sequenceOrder, shiftPattern=body.shiftPattern,
        installedCapacity=body.installedCapacity, keyHazards=body.keyHazards, isActive=body.isActive, createdBy=user.id,
    )
    db.add(pr)
    await db.commit()
    await db.refresh(pr)
    return S.ProductionProcessOut.model_validate(pr)


@router.patch("/processes/{process_id}", response_model=S.ProductionProcessOut)
async def update_process(process_id: str, body: S.ProductionProcessUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    pr = await db.get(ProductionProcess, process_id)
    if not pr or pr.isDeleted:
        raise HTTPException(404, "Production process not found")
    await _require(db, user, "FACILITY.UPDATE", plant_id=pr.siteId)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(pr, k, v)
    pr.updatedBy = user.id
    await db.commit()
    await db.refresh(pr)
    return S.ProductionProcessOut.model_validate(pr)


@router.delete("/processes/{process_id}", status_code=204)
async def delete_process(process_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    pr = await db.get(ProductionProcess, process_id)
    if not pr or pr.isDeleted:
        raise HTTPException(404, "Production process not found")
    await _require(db, user, "FACILITY.UPDATE", plant_id=pr.siteId)
    pr.isDeleted = True
    pr.updatedBy = user.id
    await db.commit()


# ════════════════════════════════════════════════════════════════════════════
# Certifications (F-02 Certifications tab) — status engine (TF-04)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/profiles/{profile_id}/certifications", response_model=list[S.FactoryCertificationOut])
async def list_certifications(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _read_profile(db, user, profile_id)
    rows = (
        await db.execute(
            select(FactoryCertification)
            .where(FactoryCertification.factoryProfileId == profile_id)
            .where(FactoryCertification.isDeleted.is_(False))
            .order_by(FactoryCertification.certificationType.asc())
        )
    ).scalars().all()
    return [_cert_out(c) for c in rows]


@router.post("/profiles/{profile_id}/certifications", response_model=S.FactoryCertificationOut, status_code=201)
async def create_certification(profile_id: str, body: S.FactoryCertificationCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.CERT_MANAGE", plant_id=p.siteId)
    status_val = body.status or svc.compute_cert_status(body.expiryDate, body.renewalLeadDays, None)
    c = FactoryCertification(
        factoryProfileId=p.id, siteId=p.siteId, certificationType=body.certificationType, certificateNo=body.certificateNo,
        issuingBody=body.issuingBody, issueDate=body.issueDate, expiryDate=body.expiryDate, renewalLeadDays=body.renewalLeadDays,
        status=status_val, scopeNotes=body.scopeNotes, attachmentIds=body.attachmentIds, createdBy=user.id,
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return _cert_out(c)


@router.patch("/certifications/{cert_id}", response_model=S.FactoryCertificationOut)
async def update_certification(cert_id: str, body: S.FactoryCertificationUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    c = await db.get(FactoryCertification, cert_id)
    if not c or c.isDeleted:
        raise HTTPException(404, "Certification not found")
    await _require(db, user, "FACILITY.CERT_MANAGE", plant_id=c.siteId)
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(c, k, v)
    # Recompute the cached status from dates unless the caller set it explicitly.
    if "status" not in data:
        c.status = svc.compute_cert_status(c.expiryDate, c.renewalLeadDays, None)
    c.updatedBy = user.id
    await db.commit()
    await db.refresh(c)
    return _cert_out(c)


@router.delete("/certifications/{cert_id}", status_code=204)
async def delete_certification(cert_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    c = await db.get(FactoryCertification, cert_id)
    if not c or c.isDeleted:
        raise HTTPException(404, "Certification not found")
    await _require(db, user, "FACILITY.CERT_MANAGE", plant_id=c.siteId)
    c.isDeleted = True
    c.updatedBy = user.id
    await db.commit()


# ════════════════════════════════════════════════════════════════════════════
# Contacts (F-02 Contacts tab)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/profiles/{profile_id}/contacts", response_model=list[S.FactoryContactOut])
async def list_contacts(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _read_profile(db, user, profile_id)
    rows = (
        await db.execute(
            select(FactoryContact)
            .where(FactoryContact.factoryProfileId == profile_id)
            .where(FactoryContact.isDeleted.is_(False))
            .order_by(FactoryContact.isPrimary.desc(), FactoryContact.name.asc())
        )
    ).scalars().all()
    return [S.FactoryContactOut.model_validate(ct) for ct in rows]


@router.post("/profiles/{profile_id}/contacts", response_model=S.FactoryContactOut, status_code=201)
async def create_contact(profile_id: str, body: S.FactoryContactCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.CONTACT_MANAGE", plant_id=p.siteId)
    ct = FactoryContact(
        factoryProfileId=p.id, siteId=p.siteId, role=body.role, name=body.name,
        phone=body.phone, email=body.email, isPrimary=body.isPrimary, createdBy=user.id,
    )
    db.add(ct)
    await db.commit()
    await db.refresh(ct)
    return S.FactoryContactOut.model_validate(ct)


@router.patch("/contacts/{contact_id}", response_model=S.FactoryContactOut)
async def update_contact(contact_id: str, body: S.FactoryContactUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    ct = await db.get(FactoryContact, contact_id)
    if not ct or ct.isDeleted:
        raise HTTPException(404, "Contact not found")
    await _require(db, user, "FACILITY.CONTACT_MANAGE", plant_id=ct.siteId)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(ct, k, v)
    ct.updatedBy = user.id
    await db.commit()
    await db.refresh(ct)
    return S.FactoryContactOut.model_validate(ct)


@router.delete("/contacts/{contact_id}", status_code=204)
async def delete_contact(contact_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    ct = await db.get(FactoryContact, contact_id)
    if not ct or ct.isDeleted:
        raise HTTPException(404, "Contact not found")
    await _require(db, user, "FACILITY.CONTACT_MANAGE", plant_id=ct.siteId)
    ct.isDeleted = True
    ct.updatedBy = user.id
    await db.commit()


# ════════════════════════════════════════════════════════════════════════════
# Social-Compliance Profile (SA8000) — 1:1 with the factory; flag engine on write
# ════════════════════════════════════════════════════════════════════════════
@router.get("/profiles/{profile_id}/social-compliance", response_model=S.SocialComplianceProfileOut | None)
async def get_social_compliance(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _read_profile(db, user, profile_id)
    s = (
        await db.execute(
            select(SocialComplianceProfile)
            .where(SocialComplianceProfile.factoryProfileId == profile_id)
            .where(SocialComplianceProfile.isDeleted.is_(False))
        )
    ).scalars().first()
    return _social_out(s) if s else None


@router.post("/profiles/{profile_id}/social-compliance", response_model=S.SocialComplianceProfileOut)
async def upsert_social_compliance(profile_id: str, body: S.SocialComplianceProfileUpsert, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Create-or-update the (1:1) social-compliance profile. Only supplied fields
    are written; the overall flag is recomputed (worst-of elements + OT cap)."""
    p = await db.get(FactoryProfile, profile_id)
    if not p or p.isDeleted:
        raise HTTPException(404, "Factory profile not found")
    await _require(db, user, "FACILITY.SOCIAL_UPDATE", plant_id=p.siteId)
    s = (
        await db.execute(
            select(SocialComplianceProfile)
            .where(SocialComplianceProfile.factoryProfileId == profile_id)
            .where(SocialComplianceProfile.isDeleted.is_(False))
        )
    ).scalars().first()
    data = body.model_dump(exclude_unset=True)
    if s is None:
        s = SocialComplianceProfile(
            factoryProfileId=p.id, siteId=p.siteId, asOfDate=data.get("asOfDate") or _now(), createdBy=user.id,
        )
        db.add(s)
    for k, v in data.items():
        if k == "asOfDate" and v is None:
            continue
        setattr(s, k, v)
    if s.asOfDate is None:
        s.asOfDate = _now()
    s.overallSocialComplianceFlag = svc.overall_social_flag_for(s)
    s.updatedBy = user.id
    await db.commit()
    await db.refresh(s)
    return _social_out(s)


# ════════════════════════════════════════════════════════════════════════════
# Group registers (W-01 register view + the three Reports-tile CSV exports)
# ════════════════════════════════════════════════════════════════════════════
async def _scoped_profiles(db: AsyncSession, user: User, state: str | None):
    """Tenant-scoped, optionally state-filtered factory list (mirrors list_profiles
    scoping). Returns None when the user has no plant access (caller returns empty)."""
    accessible = await get_accessible_plants(db, user.id)
    if accessible is not None and not accessible:
        return None
    stmt = select(FactoryProfile).where(FactoryProfile.isDeleted.is_(False))
    if accessible is not None:
        stmt = stmt.where(FactoryProfile.siteId.in_(accessible))
    if state:
        stmt = stmt.where(FactoryProfile.state == state)
    return (await db.execute(stmt.order_by(FactoryProfile.factoryName.asc()))).scalars().all()


def _social_register_row(
    p: FactoryProfile, w: WorkforceComposition | None, s: SocialComplianceProfile | None
) -> S.SocialComplianceRegisterRow:
    total = w.totalCount if w else 0
    gender_total = (w.maleCount + w.femaleCount + w.otherGenderCount) if w else 0
    child_labour = (
        svc.child_labour_flag(w.youngestWorkerAge, w.workersUnder18Count, w.minHiringAgePolicy) if w else False
    )
    overall = svc.overall_social_flag_for(s) if s else "NOT_ASSESSED"
    wage_flag = bool(s) and (
        s.minimumWageCompliant in ("ATTENTION", "NON_COMPLIANT") or s.wagesPaidOnTime in ("ATTENTION", "NON_COMPLIANT")
    )
    foa_flag = bool(s) and s.unionOrWorkerCommitteePresent in ("ATTENTION", "NON_COMPLIANT")
    overtime_flag = bool(s) and svc.overtime_exceeds_cap(s.maxWeeklyOvertimeHours)
    return S.SocialComplianceRegisterRow(
        factoryProfileId=p.id, factoryCode=p.factoryCode, factoryName=p.factoryName, state=p.state, city=p.city,
        asOfDate=w.asOfDate if w else None,
        totalWorkforce=total,
        permanentCount=w.permanentCount if w else 0,
        permanentPct=round(w.permanentCount / total * 100, 1) if (w and total) else 0,
        contractCount=w.contractCount if w else 0,
        contractPct=w.contractPct if w else 0,
        apprenticeTraineeCount=w.apprenticeTraineeCount if w else 0,
        maleCount=w.maleCount if w else 0,
        femaleCount=w.femaleCount if w else 0,
        femalePct=w.femalePct if w else 0,
        otherGenderCount=w.otherGenderCount if w else 0,
        migrantWorkerCount=w.migrantWorkerCount if w else None,
        migrantPct=w.migrantPct if w else None,
        differentlyAbledCount=w.differentlyAbledCount if w else None,
        youngestWorkerAge=w.youngestWorkerAge if w else None,
        workersUnder18Count=w.workersUnder18Count if w else 0,
        minHiringAgePolicy=w.minHiringAgePolicy if w else None,
        childLabourFlag=child_labour,
        hasSocialProfile=bool(s),
        minimumWageCompliant=s.minimumWageCompliant if s else "NOT_ASSESSED",
        lowestMonthlyWageInr=s.lowestMonthlyWageInr if s else None,
        statutoryMinimumWageInr=s.statutoryMinimumWageInr if s else None,
        wagesPaidOnTime=s.wagesPaidOnTime if s else "NOT_ASSESSED",
        standardWeeklyHours=s.standardWeeklyHours if s else None,
        maxWeeklyOvertimeHours=s.maxWeeklyOvertimeHours if s else None,
        overtimeVoluntary=s.overtimeVoluntary if s else "NOT_ASSESSED",
        weeklyRestDayProvided=s.weeklyRestDayProvided if s else "NOT_ASSESSED",
        unionOrWorkerCommitteePresent=s.unionOrWorkerCommitteePresent if s else "NOT_ASSESSED",
        collectiveBargainingAgreement=s.collectiveBargainingAgreement if s else False,
        noDepositOrDocumentRetention=s.noDepositOrDocumentRetention if s else "NOT_ASSESSED",
        grievanceMechanismPresent=s.grievanceMechanismPresent if s else "NOT_ASSESSED",
        antiDiscriminationPolicy=s.antiDiscriminationPolicy if s else "NOT_ASSESSED",
        sa8000AwarenessTrainingPct=s.sa8000AwarenessTrainingPct if s else None,
        lastSocialAuditDate=s.lastSocialAuditDate if s else None,
        overallSocialComplianceFlag=overall,
        wageFlag=wage_flag,
        overtimeFlag=overtime_flag,
        foaFlag=foa_flag,
        effectiveFlag=svc.effective_social_flag(overall, child_labour),
    )


@router.get("/social-compliance/register", response_model=S.SocialComplianceRegisterResponse)
async def social_compliance_register(state: str | None = Query(None), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """W-01 — group workforce & social-compliance register + roll-ups. Powers the
    register view AND the Workforce/SA8000 CSV. Tenant-scoped; Factory Managers
    auto-scoped to their own site."""
    await _require(db, user, "FACILITY.READ")
    rows = await _scoped_profiles(db, user, state)
    if rows is None:
        return S.SocialComplianceRegisterResponse()
    profile_ids = [p.id for p in rows]
    wf_by_profile: dict[str, WorkforceComposition] = {}
    soc_by_profile: dict[str, SocialComplianceProfile] = {}
    if profile_ids:
        wf_rows = (
            await db.execute(
                select(WorkforceComposition)
                .where(WorkforceComposition.factoryProfileId.in_(profile_ids))
                .where(WorkforceComposition.isCurrent.is_(True))
                .where(WorkforceComposition.isDeleted.is_(False))
            )
        ).scalars().all()
        wf_by_profile = {w.factoryProfileId: w for w in wf_rows}
        soc_rows = (
            await db.execute(
                select(SocialComplianceProfile)
                .where(SocialComplianceProfile.factoryProfileId.in_(profile_ids))
                .where(SocialComplianceProfile.isDeleted.is_(False))
            )
        ).scalars().all()
        soc_by_profile = {s.factoryProfileId: s for s in soc_rows}

    items = [_social_register_row(p, wf_by_profile.get(p.id), soc_by_profile.get(p.id)) for p in rows]

    roll = S.SocialComplianceRollup(factoryCount=len(items))
    flag_counts: dict[str, int] = {}
    g_perm = g_contract = g_app = g_male = g_female = g_other = g_migrant = g_dab = 0
    g_gender_total = 0
    for r in items:
        roll.totalWorkforce += r.totalWorkforce
        g_perm += r.permanentCount
        g_contract += r.contractCount
        g_app += r.apprenticeTraineeCount
        g_male += r.maleCount
        g_female += r.femaleCount
        g_other += r.otherGenderCount
        g_gender_total += r.maleCount + r.femaleCount + r.otherGenderCount
        g_migrant += r.migrantWorkerCount or 0
        g_dab += r.differentlyAbledCount or 0
        flag_counts[r.effectiveFlag] = flag_counts.get(r.effectiveFlag, 0) + 1
        if r.childLabourFlag:
            roll.childLabourFlagCount += 1
        if r.overtimeFlag:
            roll.overtimeFlagCount += 1
        if r.wageFlag:
            roll.wageFlagCount += 1
        if r.foaFlag:
            roll.foaFlagCount += 1
    roll.permanentCount, roll.contractCount, roll.apprenticeTraineeCount = g_perm, g_contract, g_app
    roll.maleCount, roll.femaleCount, roll.otherGenderCount = g_male, g_female, g_other
    roll.migrantWorkerCount, roll.differentlyAbledCount = g_migrant, g_dab
    roll.contractPct = round(g_contract / roll.totalWorkforce * 100, 1) if roll.totalWorkforce else 0
    roll.femalePct = round(g_female / g_gender_total * 100, 1) if g_gender_total else 0
    roll.migrantPct = round(g_migrant / roll.totalWorkforce * 100, 1) if roll.totalWorkforce else 0
    roll.flagCounts = flag_counts
    return S.SocialComplianceRegisterResponse(items=items, rollup=roll)


@router.get("/buildings/register", response_model=S.BuildingRegisterResponse)
async def buildings_register(state: str | None = Query(None), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Group building register — every building across the (scoped) estate."""
    await _require(db, user, "FACILITY.READ")
    rows = await _scoped_profiles(db, user, state)
    if rows is None:
        return S.BuildingRegisterResponse()
    by_id = {p.id: p for p in rows}
    profile_ids = list(by_id.keys())
    items: list[S.BuildingRegisterRow] = []
    total_area = 0.0
    if profile_ids:
        b_rows = (
            await db.execute(
                select(Building)
                .where(Building.factoryProfileId.in_(profile_ids))
                .where(Building.isDeleted.is_(False))
                .order_by(Building.buildingName.asc())
            )
        ).scalars().all()
        # group by factory then building name for a readable register
        b_rows = sorted(b_rows, key=lambda b: (by_id[b.factoryProfileId].factoryCode, b.buildingName))
        for b in b_rows:
            p = by_id[b.factoryProfileId]
            total_area += b.areaSqm or 0
            items.append(S.BuildingRegisterRow(
                factoryCode=p.factoryCode, factoryName=p.factoryName, state=p.state,
                buildingName=b.buildingName, buildingType=b.buildingType, floors=b.floors,
                areaSqm=b.areaSqm, maxOccupancy=b.maxOccupancy, currentOccupancy=b.currentOccupancy,
                assemblyPoint=b.assemblyPoint, emergencyExits=b.emergencyExits, yearBuilt=b.yearBuilt,
                occupancyCertificateNo=b.occupancyCertificateNo,
            ))
    return S.BuildingRegisterResponse(items=items, buildingCount=len(items), totalAreaSqm=round(total_area, 1))


@router.get("/certifications/register", response_model=S.CertificationRegisterResponse)
async def certifications_register(state: str | None = Query(None), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Group certification register — sorted by expiry ascending so
    expiring/expired surface at the top; summary counts on the response."""
    await _require(db, user, "FACILITY.READ")
    rows = await _scoped_profiles(db, user, state)
    if rows is None:
        return S.CertificationRegisterResponse()
    by_id = {p.id: p for p in rows}
    profile_ids = list(by_id.keys())
    items: list[S.CertificationRegisterRow] = []
    expiring_90 = expired = 0
    if profile_ids:
        c_rows = (
            await db.execute(
                select(FactoryCertification)
                .where(FactoryCertification.factoryProfileId.in_(profile_ids))
                .where(FactoryCertification.isDeleted.is_(False))
            )
        ).scalars().all()
        built = []
        for c in c_rows:
            p = by_id[c.factoryProfileId]
            status = svc.compute_cert_status(c.expiryDate, c.renewalLeadDays, c.status)
            days = svc.cert_days_to_expiry(c.expiryDate)
            built.append((c, p, status, days))
            if status == "EXPIRED":
                expired += 1
            elif days is not None and 0 <= days <= 90:
                expiring_90 += 1
        # days-to-expiry ascending → expired (negative) then expiring then valid;
        # certs without an expiry date sort last.
        built.sort(key=lambda t: (t[3] is None, t[3] if t[3] is not None else 0))
        for c, p, status, days in built:
            items.append(S.CertificationRegisterRow(
                certId=c.id,
                factoryProfileId=p.id,
                factoryCode=p.factoryCode, factoryName=p.factoryName, state=p.state,
                certificationType=c.certificationType, certificateNo=c.certificateNo, issuingBody=c.issuingBody,
                issueDate=c.issueDate, expiryDate=c.expiryDate, status=status, daysToExpiry=days, scopeNotes=c.scopeNotes,
            ))
    return S.CertificationRegisterResponse(items=items, certCount=len(items), expiringWithin90Days=expiring_90, expiredCount=expired)


# ════════════════════════════════════════════════════════════════════════════
# Compliance snapshot (precompute) + live Compliance & Audit tab (Phase D)
# ════════════════════════════════════════════════════════════════════════════
@router.post("/snapshots/recompute")
async def recompute_snapshots(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Refresh the LIVE compliance snapshot for every accessible factory by
    reading the existing engines. Cheap at demo scale; powers F-01 fast at 100+."""
    await _require(db, user, "FACILITY.READ")
    accessible = await get_accessible_plants(db, user.id)
    stmt = select(FactoryProfile).where(FactoryProfile.isDeleted.is_(False))
    if accessible is not None:
        if not accessible:
            return {"recomputed": 0}
        stmt = stmt.where(FactoryProfile.siteId.in_(accessible))
    profiles = (await db.execute(stmt)).scalars().all()
    for p in profiles:
        await snap_svc.recompute_snapshot(db, p, user.id)
    await db.commit()
    return {"recomputed": len(profiles)}


@router.get("/profiles/{profile_id}/compliance", response_model=S.ComplianceTabResponse)
async def profile_compliance(
    profile_id: str,
    periodRef: str | None = Query(None, description="Quarter ref, e.g. 2026-Q2; defaults to current quarter"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """F-02 Compliance & Audit tab — live read from the existing engines (no
    duplicate store), each row drillable into its module. Extended with QoQ deltas
    (vs the prior-quarter snapshot) and the Environment / Training / Certifications
    rollup blocks. One slow/failing block degrades only itself (degraded=true)."""
    p = await _read_profile(db, user, profile_id)
    period_ref = periodRef or snap_svc.quarter_label(_now())
    prior_ref = snap_svc.prior_quarter_label(period_ref)

    m = await snap_svc.compute_site_metrics(db, p.siteId)
    certs_expiring = await snap_svc._certs_expiring(db, p.id)
    detail = await snap_svc.compliance_detail(db, p.siteId)
    metrics = S.SnapshotMetrics(
        auditComplianceScorePct=m["auditComplianceScorePct"], openFindings=m["openFindings"],
        criticalFindings=m["criticalFindings"], openCapas=m["openCapas"], overdueCapas=m["overdueCapas"],
        openObligations=m["openObligations"], overdueObligations=m["overdueObligations"],
        certsExpiringCount=certs_expiring, incidentCount12m=m["incidentCount12m"], lastAuditDate=m["lastAuditDate"],
    )

    degraded = False

    async def _safe(coro):
        nonlocal degraded
        try:
            return await coro
        except Exception:
            degraded = True
            return None

    prior = await _safe(snap_svc.prior_metrics(db, p.id, prior_ref))
    environment = await _safe(snap_svc.env_block(db, p.id, p.siteId, period_ref, prior_ref))
    training = await _safe(snap_svc.training_block(db, p.siteId))
    certifications = await _safe(snap_svc.certifications_block(db, p.id, p.siteId))
    # P2 — social-compliance is garment-gated (None ⇒ omitted); op-risk is live.
    social = await _safe(snap_svc.social_block(db, p, p.siteId))
    operational_risk = await _safe(snap_svc.operational_risk_block(db, p.siteId))

    return S.ComplianceTabResponse(
        metrics=metrics, priorMetrics=prior, periodRef=period_ref, priorPeriodRef=prior_ref,
        environment=environment, training=training, certifications=certifications,
        socialCompliance=social, operationalRisk=operational_risk, degraded=degraded,
        **detail,
    )
