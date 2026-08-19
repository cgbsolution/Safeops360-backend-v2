"""Fire Safety & Emergency Response (P1-4, extended by the Fire & Life Safety build).

Equipment lifecycle, assembly points, emergency plans, drills, and the
incident→crisis link. Fire equipment INSPECTIONS are not stored here — they are
CAMS engagements (sourceModule='FIRE', sourceEntityId=equipment.id): one engine,
no parallel checklist store. Emergency plans optionally link to a BCM
ContinuityPlan; a CRITICAL fire incident escalates to an ERM-P3 CrisisEvent.

Four things the Fire & Life Safety spec adds, and why each lands where it does:

  • **FireZone** — a fire *detection/suppression* zone (a panel loop, a sprinkler
    grid), which is NOT the same thing as a `Building` or an `Area`. It hangs off
    the existing Factory Profile hierarchy (`buildingId` → Building, `plantId` →
    Plant) rather than becoming a parallel facility model, and optionally points
    at an `Area` so the audit independence guard's area-owner signal still
    resolves for zone-scoped work.

  • **InspectionFrequencyMaster** — inspection frequency was an `int` column on
    each equipment row with a hardcoded 30-day default. That is exactly what
    makes a regulatory remap (IN → GCC) a code change instead of a data change.
    Frequency now resolves from config, most-specific-wins; the equipment column
    survives only as a per-asset override.

  • **FireAmcContract** — `FireEquipment.maintenanceContractor` was free text, so
    "is this asset under a live AMC?" was unanswerable. AMC lapse is
    informational (spec §4.4): it never flips compliance status on its own.

  • **FireAssetCertificate** — asset-level certificates only (hydrostatic test per
    cylinder, refill certs). SITE-level statutory certificates (Fire NOC, PESO
    licence) deliberately do NOT live here — `factory_ext.RegulatoryRegistration`
    already owns those, with expiry status, alert thresholds and the canonical
    `legalObligationId` link. Adding a second site-level certificate table would
    have created precisely the duplicate source of truth the spec forbids.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin, SoftDeleteMixin


def _c():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _u():
    return mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)


class FireEquipment(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "FireEquipment"
    equipmentCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    make: Mapped[str | None] = mapped_column(String)
    model: Mapped[str | None] = mapped_column(String)
    serialNo: Mapped[str | None] = mapped_column(String)
    location: Mapped[str] = mapped_column(String, nullable=False)
    buildingId: Mapped[str | None] = mapped_column(String)
    plantId: Mapped[str] = mapped_column(String, nullable=False)  # == siteId
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    floorLevel: Mapped[int | None] = mapped_column(Integer)
    installationDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastInspectionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nextInspectionDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inspectionFrequencyDays: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVE")  # computed
    capacitySpec: Mapped[str | None] = mapped_column(String)
    maintenanceContractor: Mapped[str | None] = mapped_column(String)
    qrCode: Mapped[str | None] = mapped_column(String)
    outOfServiceReason: Mapped[str | None] = mapped_column(Text)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Fire & Life Safety extensions ────────────────────────────────────────
    # Which detection/suppression zone this asset serves. Nullable: the P1-4
    # register predates zones and unzoned assets must keep working.
    zoneId: Mapped[str | None] = mapped_column(String)
    # e.g. CO2 / DCP / FOAM / WATER for an extinguisher. Free-text by design —
    # subtype vocabularies differ per client and per region, so this is config,
    # not an enum that needs a migration to extend.
    assetSubtype: Mapped[str | None] = mapped_column(String)
    amcContractId: Mapped[str | None] = mapped_column(String)

    # `inspectionFrequencyDays` above is now an OVERRIDE, not the source of
    # truth. `frequencyMasterId` records which InspectionFrequencyMaster row was
    # applied at the last recompute, so a regulator can be shown *why* an asset
    # is on a 90-day cycle. `frequencyOverrideReason` is required whenever an
    # asset departs from its resolved config frequency.
    frequencyMasterId: Mapped[str | None] = mapped_column(String)
    frequencyOverrideReason: Mapped[str | None] = mapped_column(Text)

    # Spec §5.2 — status auto-recalculates nightly and MUST NOT be silently
    # overridable. A manual override records who/when/why; the override is also
    # written to the tamper-evident audit chain by services/fire_safety.py.
    # `statusOverride` non-null is what makes `compute_status` sticky.
    statusOverride: Mapped[str | None] = mapped_column(String)
    statusOverrideReason: Mapped[str | None] = mapped_column(Text)
    statusOverriddenBy: Mapped[str | None] = mapped_column(String)
    statusOverriddenAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── Register of Fire Extinguishers — PIL/EHSD/CL/028-R1 ──────────────────
    #
    # The client's FE register is a sixteen-column sheet. Twelve of those columns
    # already existed here (serial, type, capacity, make, location, ...), so this
    # is the remainder — not a second register model. A `FireExtinguisherAsset`
    # table alongside `FireEquipment` would have split the extinguisher estate in
    # two: the fire dashboard, the zone/hot-work guard and the CAMS inspection
    # link all read `FireEquipment`, and none of them would have seen it.
    #
    # `allottedSerialNo` is the client's own asset tag (sheet column "Alloted
    # Serial No."), distinct from the manufacturer's `serialNo` and the
    # platform's `equipmentCode`. It is what is stencilled on the cylinder and
    # what the inspector reads off it, so it is the field the FE Inspection
    # screen searches on.
    allottedSerialNo: Mapped[str | None] = mapped_column(String)
    yearOfManufacture: Mapped[int | None] = mapped_column(Integer)
    # Cylinder life expiry — the sheet's "Expiry Date", e.g. manufactured 2021,
    # expires 2031. NOT the refill or hydrostatic-test due date: those are
    # certificate lifecycles and live on FireAssetCertificate (see below).
    expiryDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dateOfDischarge: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    weightKg: Mapped[float | None] = mapped_column(Float)
    registerRemarks: Mapped[str | None] = mapped_column(Text)

    # DELIBERATELY ABSENT: hpTestedOn / hpTestDueDate / refilledOn /
    # dueForRefilling. The sheet prints them as four flat columns, but each pair
    # is the issue and expiry of a *certificate*, and `FireAssetCertificate`
    # already models exactly that — with HYDROSTATIC_TEST and REFILL types, a
    # computed VALID/EXPIRING_SOON/EXPIRED status, escalation tiers and the
    # attached document. Duplicating them here would give the register two
    # sources of truth for "is this cylinder due", which is the failure mode the
    # certificate table was added to prevent. The register API projects the four
    # columns from the latest certificate of each type; see
    # services/fire_register.py.

    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    __table_args__ = (
        Index("ix_FireEquipment_plant_status", "plantId", "status"),
        Index("ix_FireEquipment_type", "type"),
        Index("ix_FireEquipment_due", "nextInspectionDueDate"),
        # The hot-work PTW guard (spec §4.6) asks "every asset in zone Z whose
        # status is not compliant" on every permit approval screen — that query
        # must not seq-scan the register.
        Index("ix_FireEquipment_zone_status", "zoneId", "status"),
        Index("ix_FireEquipment_amc", "amcContractId"),
        # The FE Inspection screen's asset picker searches on the tag stencilled
        # on the cylinder, not on the platform code.
        Index("ix_FireEquipment_allotted", "plantId", "allottedSerialNo"),
        # The register's cylinder-life badge sorts and filters on this.
        Index("ix_FireEquipment_expiry", "expiryDate"),
    )


class AssemblyPoint(Base, IdMixin):
    __tablename__ = "AssemblyPoint"
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    buildingIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    capacity: Mapped[int | None] = mapped_column(Integer)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    wardenUserId: Mapped[str | None] = mapped_column(String)
    alternateWardenUserId: Mapped[str | None] = mapped_column(String)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    createdAt: Mapped[datetime] = _c()
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_AssemblyPoint_plant", "plantId"),)


class FireEmergencyPlan(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "FireEmergencyPlan"
    planCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    continuityPlanId: Mapped[str | None] = mapped_column(String)  # BCM ContinuityPlan link
    fireTypes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    commandStructure: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    callTree: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    assemblyPointIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    criticalEquipmentShutdownSequence: Mapped[str | None] = mapped_column(Text)
    hazmatLocations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    externalContacts: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    lastReviewDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nextReviewDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    __table_args__ = (Index("ix_FireEmergencyPlan_plant", "plantId"),)


class FireDrill(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "FireDrill"
    drillCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    drillType: Mapped[str] = mapped_column(String, nullable=False)
    planId: Mapped[str | None] = mapped_column(String)
    scheduledDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    conductedDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, nullable=False, default="PLANNED")
    outcome: Mapped[str | None] = mapped_column(String)
    facilitatorId: Mapped[str | None] = mapped_column(String)
    participantCount: Mapped[int | None] = mapped_column(Integer)
    evacuationTimeMinutes: Mapped[float | None] = mapped_column(Float)
    evacuationTargetMinutes: Mapped[float | None] = mapped_column(Float)
    assemblyPointVerified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unaccountedPersons: Mapped[int | None] = mapped_column(Integer)
    reportRichText: Mapped[str | None] = mapped_column(Text)
    isAnnualMandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_FireDrill_plant_status", "plantId", "status"),)


class FireDrillFinding(Base, IdMixin):
    __tablename__ = "FireDrillFinding"
    drillId: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)  # OBSERVATION|MINOR_GAP|MAJOR_GAP
    description: Mapped[str] = mapped_column(Text, nullable=False)
    capaId: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    __table_args__ = (Index("ix_FireDrillFinding_drill", "drillId"),)


class FireIncidentLink(Base, IdMixin):
    __tablename__ = "FireIncidentLink"
    incidentId: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str | None] = mapped_column(String)
    affectedEquipmentIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    crisisEventId: Mapped[str | None] = mapped_column(String)
    evacuationOrdered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fireServiceCalled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    estimatedPropertyDamageInr: Mapped[float | None] = mapped_column(Float)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    __table_args__ = (Index("ix_FireIncidentLink_incident", "incidentId"),)


# ── Fire zones (child of the Factory Profile hierarchy) ──────────────────────
class FireZone(Base, IdMixin, SoftDeleteMixin):
    """A fire detection / suppression zone.

    Deliberately NOT a `Building` and NOT an `Area`: a zone is a panel loop or a
    sprinkler grid and routinely spans part of a floor or crosses two rooms. It
    is a *child* of the existing hierarchy (`plantId` → Plant, `buildingId` →
    Building), never a parallel facility model.

    `areaId` is optional and exists for one reason: the audit independence guard
    resolves an `AREA_OWNER` conflict signal from `Area.ownerUserId`. Without it,
    zone-scoped inspection work would degrade the guard to "no signal", which the
    guard treats as *no conflict* — the silent-failure mode worth avoiding.
    """

    __tablename__ = "FireZone"
    zoneCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    buildingId: Mapped[str | None] = mapped_column(String)
    areaId: Mapped[str | None] = mapped_column(String)
    parentZoneId: Mapped[str | None] = mapped_column(String)
    floor: Mapped[str | None] = mapped_column(String)
    areaSqm: Mapped[float | None] = mapped_column(Float)
    coverageType: Mapped[str] = mapped_column(String, nullable=False, default="BOTH")  # DETECTION|SUPPRESSION|BOTH

    # Drives the hot-work PTW guard's warn-vs-block decision (spec §4.6). CRITICAL
    # zones block a permit on non-compliant suppression cover; everything else
    # warns. Mirrors the ALARP CRITICAL-band mandatory-action-path rule from HIRA
    # rather than inventing a second escalation vocabulary.
    criticality: Mapped[str] = mapped_column(String, nullable=False, default="STANDARD")  # CRITICAL|HIGH|STANDARD
    # Asset types this zone must have in working order for hot work to be safe.
    # Empty list = fall back to the platform default set.
    requiredAssetTypes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    panelAssetId: Mapped[str | None] = mapped_column(String)  # FireEquipment of type PANEL covering this zone
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    __table_args__ = (
        Index("ix_FireZone_plant", "plantId"),
        Index("ix_FireZone_building", "buildingId"),
        Index("ix_FireZone_parent", "parentZoneId"),
    )


# ── Inspection frequency master (config, never hardcoded) ────────────────────
class InspectionFrequencyMaster(Base, IdMixin):
    """Statutory inspection cadence per asset type — spec §5.1.

    This table is the whole reason the GCC regulatory remap is a data change.
    Resolution is most-specific-wins (see `services/fire_frequency.resolve`):

        plantId + assetType + assetSubtype   ← site-specific, subtype-specific
        plantId + assetType
        region  + assetType + assetSubtype
        region  + assetType                  ← the seeded NBC 2016 defaults
        (nothing)                            → PLATFORM_FALLBACK_DAYS

    `plantId` NULL means "platform default for this region". `regulatoryReference`
    is free text and carries the citation a regulator asks for ("NBC 2016 Part 4
    Table 24"); it is rendered next to the due date, not just stored.

    `auditTypeId` / `checklistTemplateId` point at the CAMS engine's own config
    (`CamsAuditType` / `CamsTemplate`). Fire inspections are CAMS engagements, so
    the checklist lives in the CAMS template library — this column selects one,
    it does not define one.
    """

    __tablename__ = "InspectionFrequencyMaster"
    plantId: Mapped[str | None] = mapped_column(String)  # NULL = platform default
    region: Mapped[str] = mapped_column(String, nullable=False, default="IN")
    assetType: Mapped[str] = mapped_column(String, nullable=False)
    assetSubtype: Mapped[str | None] = mapped_column(String)  # NULL = all subtypes
    frequency: Mapped[str] = mapped_column(String, nullable=False)  # WEEKLY|MONTHLY|QUARTERLY|HALF_YEARLY|ANNUAL|CUSTOM
    # Only consulted when frequency == 'CUSTOM'. Everything else derives its day
    # count from the enum so two rows claiming "QUARTERLY" can never mean
    # different intervals.
    customIntervalDays: Mapped[int | None] = mapped_column(Integer)
    regulatoryReference: Mapped[str | None] = mapped_column(String)
    checklistTemplateId: Mapped[str | None] = mapped_column(String)  # → CamsTemplate
    auditTypeId: Mapped[str | None] = mapped_column(String)  # → CamsAuditType
    # Lead time for pre-generating the CAMS engagement, mirroring CamsRecurrence.
    leadTimeDays: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (
        Index("ix_IFM_lookup", "region", "assetType", "assetSubtype"),
        Index("ix_IFM_plant", "plantId", "assetType"),
    )


# ── AMC / vendor contracts ───────────────────────────────────────────────────
class FireAmcContract(Base, IdMixin, SoftDeleteMixin):
    """An annual maintenance contract covering one or more fire assets.

    Spec §4.4: lapse is **informational**. A dead AMC flips `amcCoverageLapsed`
    on the linked assets and raises reminders, but it never moves an asset's
    compliance `status` — an extinguisher inspected on time is compliant whether
    or not its service contract is current, and conflating the two would make the
    overdue-inspection count untrustworthy.

    Assets link *to* the contract (`FireEquipment.amcContractId`) rather than the
    contract holding an id array, so re-assigning one asset is a single-row
    update and the "assets on this contract" query uses an index.
    """

    __tablename__ = "FireAmcContract"
    contractCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    vendorName: Mapped[str] = mapped_column(String, nullable=False)
    vendorContactId: Mapped[str | None] = mapped_column(String)
    vendorEmail: Mapped[str | None] = mapped_column(String)
    vendorPhone: Mapped[str | None] = mapped_column(String)
    scopeSummary: Mapped[str | None] = mapped_column(Text)
    startDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    endDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Per-contract override of the tenant default (spec §4.4: 90/60/30/7).
    renewalReminderDays: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Highest tier already notified, so the nightly job is idempotent and does
    # not re-send the 90-day reminder every night for 30 days.
    lastReminderTierSent: Mapped[int | None] = mapped_column(Integer)
    escalatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    contractDocumentIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVE")  # ACTIVE|EXPIRING_SOON|LAPSED|RENEWED|CANCELLED
    annualValueInr: Mapped[float | None] = mapped_column(Float)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    __table_args__ = (
        Index("ix_FireAmcContract_plant_status", "plantId", "status"),
        Index("ix_FireAmcContract_end", "endDate"),
    )


# ── Asset-level certificates ─────────────────────────────────────────────────
class FireAssetCertificate(Base, IdMixin, SoftDeleteMixin):
    """A certificate attached to a single asset — hydrostatic test, refill, calibration.

    SITE-level statutory certificates (Fire NOC, PESO licence) are NOT stored
    here. `factory_ext.RegulatoryRegistration` already holds those with expiry
    status, alert thresholds and the canonical `legalObligationId`; a second
    site-level table would be the duplicate source of truth spec §6 rules out.
    What that table cannot hold is a per-cylinder hydrostatic test date, because
    it is scoped to a FactoryProfile — hence this one, scoped to an asset.

    `status` is computed, never entered (see `services/fire_certificates.py`).
    """

    __tablename__ = "FireAssetCertificate"
    assetId: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    certificateType: Mapped[str] = mapped_column(String, nullable=False)  # HYDROSTATIC_TEST|REFILL|CALIBRATION|OTHER
    certificateNo: Mapped[str | None] = mapped_column(String)
    issuingAuthority: Mapped[str | None] = mapped_column(String)
    issueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiryDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, nullable=False, default="VALID")  # VALID|EXPIRING_SOON|EXPIRED
    # Per-certificate override of the tenant escalation tiers; empty = tenant default.
    escalationTierDays: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    lastReminderTierSent: Mapped[int | None] = mapped_column(Integer)
    documentIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    notes: Mapped[str | None] = mapped_column(Text)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    __table_args__ = (
        Index("ix_FireAssetCertificate_asset", "assetId"),
        Index("ix_FireAssetCertificate_expiry", "expiryDate"),
        Index("ix_FireAssetCertificate_plant_status", "plantId", "status"),
    )


# ── False alarm log ──────────────────────────────────────────────────────────
class FireFalseAlarmLog(Base, IdMixin):
    """A false activation on a detection panel.

    Append-only. False-alarm rate per panel is the leading indicator that a
    detector head needs cleaning or resiting, and it is the number a regulator
    asks for when occupants have started ignoring the alarm.
    """

    __tablename__ = "FireFalseAlarmLog"
    panelAssetId: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    zoneId: Mapped[str | None] = mapped_column(String)
    occurredAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cause: Mapped[str] = mapped_column(String, nullable=False)  # DUST|STEAM|COOKING|TESTING|FAULT|UNKNOWN|OTHER
    causeNotes: Mapped[str | None] = mapped_column(Text)
    correctiveAction: Mapped[str | None] = mapped_column(Text)
    evacuationTriggered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fireServiceCalled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reportedBy: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    __table_args__ = (
        Index("ix_FireFalseAlarmLog_panel", "panelAssetId", "occurredAt"),
        Index("ix_FireFalseAlarmLog_plant", "plantId", "occurredAt"),
    )


# ── Non-working days (temporary — see the class docstring) ───────────────────
class PlantNonWorkingDay(Base, IdMixin):
    """A date the plant does not run, greyed out on the daily checklist grids.

    THIS IS A DOCUMENTED STOPGAP, not a design. The client's daily sheets
    pre-print SUNDAY and HOLIDAY across the date columns, so the grid has to know
    which days are excluded or a compliance report reads "8 missed inspections"
    for a week that contained a factory shutdown.

    The build spec's first open item asks whether a platform holiday calendar
    already exists to wire into. It does not — there is no holiday, calendar-of-
    non-working-days or shutdown model anywhere in the backend or the Prisma
    schema (searched). So this is the spec's own stated fallback: a manual
    per-date flag, scoped to a plant.

    Two consequences worth being explicit about:

      • Sundays are NOT stored here. A Sunday is derivable from the date, and
        writing 52 rows a year per plant to record something `weekday() == 6`
        already knows would be a calendar that can drift out of step with the
        actual calendar. `services/fire_checklists.non_working_days` computes
        Sundays and unions them with the rows in this table.

      • When a real platform holiday calendar lands (Facilities is the natural
        home), this table should be READ-migrated into it and dropped, not kept
        in parallel. It is deliberately minimal — plant, date, label — so that
        migration is a straight copy with nothing to reconcile.
    """

    __tablename__ = "PlantNonWorkingDay"
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    # Date only. Stored as a timestamp for consistency with every other date on
    # this platform; normalised to midnight UTC on write so equality works.
    day: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False, default="HOLIDAY")
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    __table_args__ = (Index("ix_PlantNonWorkingDay_plant_day", "plantId", "day", unique=True),)


__all__ = [
    "FireEquipment", "AssemblyPoint", "FireEmergencyPlan",
    "FireDrill", "FireDrillFinding", "FireIncidentLink",
    "FireZone", "InspectionFrequencyMaster", "FireAmcContract",
    "FireAssetCertificate", "FireFalseAlarmLog", "PlantNonWorkingDay",
]
