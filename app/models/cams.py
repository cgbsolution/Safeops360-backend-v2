"""CAMS — Compliance & Audit Management System SQLAlchemy models.

Mirrors the `Cams*` Prisma family in schema.prisma (section "CAMS — Compliance &
Audit Management System"). Schema is owned by Prisma (db push). camelCase columns
to match the DB. References to existing tables (User / Plant / Capa / Equipment /
EnterpriseRisk / CamsAuditType / CamsTemplate) are plain String columns — no FKs
to those; only the intra-module parent→child links use ForeignKey.

The engine serves both "audits" and "inspections" — they differ only by
`engagementType` + AuditType config, never by code path.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


def _created():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated():
    return mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)


# ── Shared Service ③ (types) — Audit Type config (was "Inspection Types") ────
class CamsAuditType(Base, IdMixin):
    __tablename__ = "CamsAuditType"

    typeCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    engagementType: Mapped[str] = mapped_column(String, nullable=False)
    defaultTemplateId: Mapped[str | None] = mapped_column(String)
    defaultRecurrence: Mapped[str | None] = mapped_column(String)
    requiresAssetRef: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requiresAuditorCompetency: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # ── WP-49: the audit type is the configuration home ──────────────────
    #
    # `MINIMUM_PASS_SCORE = 80.0` was a module-level constant in
    # `services/audit_compliance.py` applied to every audit of every type (F-22),
    # while `AuditTemplate.scoring` sat unused. Scoring policy belongs to the
    # TYPE — a fire-equipment inspection and an SA8000 social audit do not share
    # a pass mark or a critical-failure tolerance.
    #
    # {minimumPassScore, criticalGateThreshold, partialCredit, naHandling}
    # Null falls back to the historic constant, so existing types are unchanged.
    scoringRules: Mapped[dict | None] = mapped_column(JSON)

    # WP-47: which buyer regime's severity/result vocabulary this type renders
    # (SMETA_LIKE, BSCI_LIKE, ...). Null = the engine's native taxonomy.
    regimeCode: Mapped[str | None] = mapped_column(String, index=True)

    # Warn vs block when an assignee lacks a required competency. ISO 19011
    # cl.7 wants competence assured, but a hard block on day one would strand
    # tenants whose Skill Matrix is still being populated — so the default is
    # WARN and tightening it is a deliberate act.
    competenceEnforcement: Mapped[str] = mapped_column(
        String, nullable=False, default="WARN"
    )
    standardRefs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_CamsAuditType_engagementType", "engagementType"),
        Index("ix_CamsAuditType_isActive", "isActive"),
    )


# ── Shared Service ① — Audit/Inspection Engine ──────────────────────────────
class CamsEngagement(Base, IdMixin):
    __tablename__ = "CamsEngagement"

    engagementCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    engagementType: Mapped[str] = mapped_column(String, nullable=False)
    auditTypeId: Mapped[str | None] = mapped_column(String)
    standardRefs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    siteId: Mapped[str | None] = mapped_column(String)
    areaOrAssetRef: Mapped[str | None] = mapped_column(String)
    scopeStatement: Mapped[str] = mapped_column(Text, nullable=False, default="")
    leadAuditorId: Mapped[str] = mapped_column(String, nullable=False)
    auditTeamIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    auditeeOwnerId: Mapped[str | None] = mapped_column(String)
    plannedDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scheduledStart: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduledEnd: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    conductedDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    templateId: Mapped[str | None] = mapped_column(String)
    templateVersionUsed: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PLANNED")
    riskBasis: Mapped[str | None] = mapped_column(String)
    triggeringRiskId: Mapped[str | None] = mapped_column(String)
    overallResult: Mapped[str | None] = mapped_column(String)
    scorePercent: Mapped[float | None] = mapped_column(Float)
    reportAttachmentIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    nextScheduledDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sourceModule: Mapped[str | None] = mapped_column(String)
    sourceEntityId: Mapped[str | None] = mapped_column(String)  # entity this engagement inspects (e.g. FireEquipment.id)
    recurrenceId: Mapped[str | None] = mapped_column(String)

    # ── Periodic-record support (Fire & Life Safety checklists) ──────────────
    #
    # A controlled EHS checklist is a *periodic* record: exactly one Daily Fire
    # Alarm sheet exists for panel P on 19 Aug 2026, and touching that screen
    # twice must find the same record, not make a second one. `plannedDate`
    # cannot carry that identity — it is a timestamp, so "same day" is a range
    # query and two clients racing the first touch both see nothing and both
    # insert.
    #
    # `periodLabel` is the occurrence key, granularity-encoded so one column
    # serves every cadence:
    #
    #     DAILY      "2026-08-19"      MONTHLY  "2026-08"
    #     QUARTERLY  "2026-Q3"         ANNUAL   "2026"
    #
    # Uniqueness is enforced in the database by a PARTIAL unique index
    # (templateId, sourceEntityId, periodLabel) scoped to rows that set it — see
    # prisma/apply-firechecklists-ddl.ts. Partial, because every CAMS engagement
    # predating this build has periodLabel NULL and a plain unique index would
    # be fine on NULLs but would also silently start constraining ad-hoc audits
    # the day someone set the column for an unrelated reason.
    periodLabel: Mapped[str | None] = mapped_column(String)

    # Prepared / Reviewed / Approved — the sign-off block printed at the foot of
    # every Page Industries checklist. "Prepared by" already exists as
    # `CamsResponse.completedBy/completedAt` (the person who filled the sheet),
    # so only the two later stages are new here.
    #
    # These are stamps, not signatures: userId + timestamp, the same evidence
    # every other approval on this platform records. No e-signature capture
    # exists anywhere in the codebase (checked), so inventing one for this module
    # alone would have been a second mechanism, not a reused one. Stage ORDER is
    # enforced by the existing `_TRANSITIONS` state machine, not by these
    # columns — they record who, the state machine decides whether they may.
    reviewedBy: Mapped[str | None] = mapped_column(String)
    reviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approvedBy: Mapped[str | None] = mapped_column(String)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── Captured signatures ──────────────────────────────────────────────────
    #
    # A userId and a timestamp record WHO the system believes acted. They do not
    # record a person putting their name to a statement, and the paper sheet this
    # reproduces has a "Sign. & Date:" box under each of its three roles — an
    # export with a blank box next to a name is not the document the auditor was
    # handed.
    #
    # Shape is copied EXACTLY from `ComplianceAudit.signOffs` (WP-41), the
    # platform's established sign-off record:
    #
    #   [{role, userId, name, designation, signatureKind, signatureImage,
    #     typedName, statement, signedAt}]
    #
    # Same JSON shape, same DRAWN/TYPED vocabulary, same `SignatureModal` canvas
    # on the front end, same `services/signoff.validate_signature` guard. This is
    # deliberately not a fire-specific signature mechanism: there is one on this
    # platform and this is a second consumer of it.
    #
    # The `reviewedBy`/`approvedBy` columns above stay as the queryable index —
    # "which sheets has this person approved" must not be a JSON scan — and the
    # signature is the evidence behind the stamp, not a replacement for it.
    signOffs: Mapped[list | None] = mapped_column(JSON)

    responses: Mapped[list["CamsResponse"]] = relationship(back_populates="engagement", cascade="all, delete-orphan")
    findings: Mapped[list["CamsFinding"]] = relationship(back_populates="engagement", cascade="all, delete-orphan")

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_CamsEngagement_site_status", "siteId", "status"),
        Index("ix_CamsEngagement_status", "status"),
        Index("ix_CamsEngagement_type", "engagementType"),
        Index("ix_CamsEngagement_lead", "leadAuditorId"),
        Index("ix_CamsEngagement_planned", "plannedDate"),
        Index("ix_CamsEngagement_source", "sourceModule"),
        Index("ix_CamsEngagement_auditType", "auditTypeId"),
        # The grid screens ask "every period of template T for asset A" on every
        # render — a month of daily rows is 31 of these, a year of the FE sheet
        # is 12. Without this it is a seq-scan of the whole engagement table per
        # page view.
        Index("ix_CamsEngagement_period", "sourceEntityId", "templateId", "periodLabel"),
    )


class CamsRecurrence(Base, IdMixin):
    __tablename__ = "CamsRecurrence"

    auditTypeId: Mapped[str | None] = mapped_column(String)
    templateId: Mapped[str | None] = mapped_column(String)
    siteScope: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    frequency: Mapped[str] = mapped_column(String, nullable=False)
    customIntervalDays: Mapped[int | None] = mapped_column(Integer)
    leadTimeDays: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    defaultLeadAuditorId: Mapped[str | None] = mapped_column(String)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    lastGeneratedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_CamsRecurrence_isActive", "isActive"),
        Index("ix_CamsRecurrence_auditType", "auditTypeId"),
    )


# ── Shared Service ② — Template / Checklist Engine ──────────────────────────
class CamsTemplate(Base, IdMixin):
    __tablename__ = "CamsTemplate"

    templateCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    applicableEngagementTypes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    standardRefs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    approvedBy: Mapped[str | None] = mapped_column(String)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parentTemplateId: Mapped[str | None] = mapped_column(String)
    scoringConfig: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    ownerId: Mapped[str] = mapped_column(String, nullable=False)
    isGlobal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    siteId: Mapped[str | None] = mapped_column(String)

    # ── Controlled-document provenance ───────────────────────────────────────
    #
    # A template that reproduces a client's controlled document has to carry that
    # document's own identity — number, revision, supersedes, effective/review
    # dates, source sheet — because the auditor's first question is "which
    # revision is this?" and the answer must come off the record, not off a
    # developer's memory of which XLSX was imported.
    #
    # `version` above is the platform's edit counter; it is NOT the client's
    # revision. PIL/EHSD/CL/026-R2 is R2 whether this is the first import or the
    # fifth re-seed of it, so conflating the two would misreport the document.
    #
    # JSON rather than eight columns because the shape is document-family
    # specific (a Page Industries sheet has "supersedesNo"; an ISO 45001
    # checklist would not) and nothing queries inside it — it is read whole, by
    # the screen header and the PDF header. Also carries `layout`, which tells
    # the renderer how to pivot a set of periodic engagements into the grid the
    # paper original prints. Empty {} on every pre-existing template.
    documentMeta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    sections: Mapped[list["CamsTemplateSection"]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_CamsTemplate_status", "status"),
        Index("ix_CamsTemplate_isGlobal", "isGlobal"),
        Index("ix_CamsTemplate_parent", "parentTemplateId"),
    )


class CamsTemplateSection(Base, IdMixin):
    __tablename__ = "CamsTemplateSection"

    templateId: Mapped[str] = mapped_column(ForeignKey("CamsTemplate.id", ondelete="CASCADE"), nullable=False)
    template: Mapped[CamsTemplate] = relationship(back_populates="sections")
    orderIndex: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(String, nullable=False)
    weightPct: Mapped[float | None] = mapped_column(Float)

    questions: Mapped[list["CamsTemplateQuestion"]] = relationship(
        back_populates="section", cascade="all, delete-orphan"
    )

    createdAt: Mapped[datetime] = _created()
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (Index("ix_CamsTemplateSection_tpl_order", "templateId", "orderIndex"),)


class CamsTemplateQuestion(Base, IdMixin):
    __tablename__ = "CamsTemplateQuestion"

    sectionId: Mapped[str] = mapped_column(ForeignKey("CamsTemplateSection.id", ondelete="CASCADE"), nullable=False)
    section: Mapped[CamsTemplateSection] = relationship(back_populates="questions")
    orderIndex: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    questionType: Mapped[str] = mapped_column(String, nullable=False, default="CONFORM_NC_NA")
    isMandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    standardClauseRef: Mapped[str | None] = mapped_column(String)
    guidance: Mapped[str | None] = mapped_column(Text)
    weight: Mapped[float | None] = mapped_column(Float)
    ncTriggersFinding: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    evidenceRequiredOnNc: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    options: Mapped[list | None] = mapped_column(JSON)

    createdAt: Mapped[datetime] = _created()
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (
        Index("ix_CamsTemplateQuestion_sec_order", "sectionId", "orderIndex"),
        Index("ix_CamsTemplateQuestion_clause", "standardClauseRef"),
    )


class CamsResponse(Base, IdMixin):
    __tablename__ = "CamsResponse"

    engagementId: Mapped[str] = mapped_column(
        ForeignKey("CamsEngagement.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    engagement: Mapped[CamsEngagement] = relationship(back_populates="responses")
    templateVersionUsed: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    answers: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    sectionScores: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    completedBy: Mapped[str | None] = mapped_column(String)
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = _created()
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (Index("ix_CamsResponse_engagement", "engagementId"),)


# ── Shared Service ③ — Findings ─────────────────────────────────────────────
class CamsFinding(Base, IdMixin):
    __tablename__ = "CamsFinding"

    findingCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    engagementId: Mapped[str] = mapped_column(ForeignKey("CamsEngagement.id", ondelete="CASCADE"), nullable=False)
    engagement: Mapped[CamsEngagement] = relationship(back_populates="findings")
    sourceQuestionId: Mapped[str | None] = mapped_column(String)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    severity: Mapped[str] = mapped_column(String, nullable=False, default="MINOR_NC")
    standardClauseRef: Mapped[str | None] = mapped_column(String)
    siteId: Mapped[str | None] = mapped_column(String)
    areaOrAssetRef: Mapped[str | None] = mapped_column(String)
    ownerId: Mapped[str | None] = mapped_column(String)
    rootCauseMethod: Mapped[str | None] = mapped_column(String)
    rootCauseSummary: Mapped[str | None] = mapped_column(Text)
    capaId: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN")
    isRepeatFinding: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    repeatOfFindingId: Mapped[str | None] = mapped_column(String)

    # ── Re-observation of an OPEN finding ────────────────────────────────────
    #
    # `isRepeatFinding` / `repeatOfFindingId` describe a defect that came BACK
    # after being closed. These three describe one that never went away: the same
    # check failing on the same asset day after day while the CAPA is still open.
    #
    # That distinction is what lets a routine daily checklist raise CAPAs at all.
    # Without it, a power lamp dead for three weeks is either 21 CAPAs (a register
    # nobody reads) or none (a failure nobody acts on). With it, it is one CAPA
    # that says "observed 21 times, last on 2026-08-24" — which is the sentence a
    # CAPA owner can act on and an auditor can check.
    #
    # Real columns rather than a JSON blob because "which defects recur most" is a
    # question worth being able to ORDER BY, and because the first attempt at this
    # wrote the count into a `sourceMetadata` attribute that does not exist on this
    # model — every write silently did nothing and the count was permanently stuck
    # at 2. A typed column fails loudly instead.
    occurrenceCount: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lastObservedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The period labels it was seen in ("2026-08-24", "2026-Q3"), newest last and
    # capped by the writer — a year of daily observations is 365 labels and the
    # useful facts are the count, the first and the last.
    observedPeriods: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    dueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closedBy: Mapped[str | None] = mapped_column(String)
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verificationNote: Mapped[str | None] = mapped_column(Text)
    evidenceAttachmentIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # ── Fire & Life Safety additions (generic on purpose) ─────────────────────
    #
    # A fire "Defect" in the build spec IS a CamsFinding — same lifecycle, same
    # CAPA link, same independence rules. Rather than fork a Defect table, the
    # two things the spec needs that findings lacked are added here, and both are
    # useful to every CAMS consumer:
    #
    # `requiresCapa` — opt-in hard constraint. Spec §5.4 wants "CRITICAL defect
    # ⇒ linked CAPA" enforced by the DB, not by the UI, so it cannot repeat the
    # HIRA gap where a column existed and nothing enforced it. A blanket CHECK on
    # `severity='CRITICAL_NC' ⇒ capaId IS NOT NULL` would retroactively invalidate
    # existing CAMS findings and break every audit path that writes a finding
    # before spawning its CAPA. Instead the fire path sets this flag, and a
    # DEFERRABLE INITIALLY DEFERRED constraint trigger validates it at COMMIT —
    # so the spawn-then-link ordering inside one transaction is legal, but a
    # transaction that ends with an unlinked required-CAPA finding cannot commit.
    # (Postgres will not accept a deferrable CHECK; a constraint trigger is the
    # only way to get a deferred assertion.)
    requiresCapa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # `verificationEngagementId` — spec §4.3: a defect cannot go OPEN → CLOSED
    # without a linked *verification inspection*. `verificationNote` above is
    # free text and proves nothing; this points at the CamsEngagement that
    # re-inspected the asset, mirroring the re-approval-on-edit lock decided for
    # HIRA. Enforced in services/fire_defects.py, not by a DB constraint: a
    # non-fire finding may legitimately close on documentary evidence alone.
    verificationEngagementId: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_CamsFinding_engagement", "engagementId"),
        Index("ix_CamsFinding_status_due", "status", "dueDate"),
        Index("ix_CamsFinding_severity", "severity"),
        Index("ix_CamsFinding_clause", "standardClauseRef"),
        Index("ix_CamsFinding_site", "siteId"),
        Index("ix_CamsFinding_repeat", "isRepeatFinding"),
        # The fire defect board filters by asset; without this the kanban
        # seq-scans every finding the tenant has ever raised.
        Index("ix_CamsFinding_assetRef", "areaOrAssetRef"),
    )


# ── Shared Service ⑤ — Compliance link (audit ↔ obligation) ─────────────────
class CamsComplianceLink(Base, IdMixin):
    __tablename__ = "CamsComplianceLink"

    engagementId: Mapped[str | None] = mapped_column(String)
    findingId: Mapped[str | None] = mapped_column(String)
    obligationId: Mapped[str] = mapped_column(String, nullable=False)
    linkType: Mapped[str] = mapped_column(String, nullable=False)  # VERIFIES | BREACHES | EVIDENCES
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_CamsComplianceLink_obligation", "obligationId"),
        Index("ix_CamsComplianceLink_engagement", "engagementId"),
        Index("ix_CamsComplianceLink_finding", "findingId"),
    )


__all__ = [
    "CamsAuditType",
    "CamsEngagement",
    "CamsRecurrence",
    "CamsTemplate",
    "CamsTemplateSection",
    "CamsTemplateQuestion",
    "CamsResponse",
    "CamsFinding",
    "CamsComplianceLink",
]
