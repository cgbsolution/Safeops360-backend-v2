"""Waves 3-5 completion models — WP-19, WP-40, WP-43, WP-45, WP-46.

Grouped in one module (and one DDL) because they are small, land together, and
share no dependencies beyond the CAMS engine.

  AuditFinding             WP-19 — Finding first-class on the AUDIT side
  EvidencePackJob          WP-40 — async certification evidence pack
  NotificationPreference   WP-43 — per-user, per-class digest frequency
  SupplierAuditLink        WP-45 — VendorProfile <-> engagement, no new entity
  CheckpointTranslation    WP-46 — per-language checkpoint text (Q18: en + hi)

Each carries a matching Prisma block; applied by
`scripts/add_cams_completion.py`, never `prisma db push`.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


# ── WP-19: Finding first-class on the audit side ─────────────────────


class AuditFinding(Base, IdMixin):
    """A finding raised from an audit checkpoint — as a ROW, not a property.

    Today an audit finding is an implicit property of a checkpoint
    (`assessmentStatus IN (FAIL, PARTIAL)`). That is why it cannot carry a due
    date, an owner distinct from the checkpoint's, a repeat chain, or a severity
    different from the checkpoint's criticality — and why the unified Findings
    Register shows blank Due on ~40% of its rows *structurally* (F-3, F-40).

    **This is additive, not a migration.** `AuditCheckpointResponse` keeps its
    verdict columns and the conduct screen is unchanged; a finding row is
    created alongside when a checkpoint goes adverse. Collapsing the two into
    one model is WP-18's job, and doing it here under time pressure is how a
    working module becomes a broken one.

    `observationOnly` is the answer to the 375 `criticality='observation'` rows
    the diagnosis found being dropped at the CamsFinding boundary: an
    observation is a real finding that simply is not a non-conformity.
    """

    __tablename__ = "AuditFinding"

    findingCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    auditId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    checkpointResponseId: Mapped[str | None] = mapped_column(String, index=True)
    checkpointCode: Mapped[str | None] = mapped_column(String)

    siteId: Mapped[str | None] = mapped_column(String, index=True)
    disciplineCode: Mapped[str | None] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Severity is the finding's OWN, seeded from checkpoint criticality but
    # independently adjustable — an auditor can downgrade a critical checkpoint's
    # finding on the evidence, and that decision should survive.
    severity: Mapped[str] = mapped_column(String, nullable=False, default="MINOR_NC")
    observationOnly: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    standard: Mapped[str | None] = mapped_column(String)
    clauseRef: Mapped[str | None] = mapped_column(String)

    ownerId: Mapped[str | None] = mapped_column(String, index=True)
    # The column whose absence made 40% of the register blank.
    dueDate: Mapped[date | None] = mapped_column(Date, index=True)

    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN")
    capaId: Mapped[str | None] = mapped_column(String, index=True)

    # Cross-engine repeat chain: this may point at an AuditFinding OR a
    # CamsFinding id, which is what makes a chain able to span both engines for
    # the first time.
    isRepeatFinding: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    repeatOfFindingId: Mapped[str | None] = mapped_column(String, index=True)
    repeatOfKind: Mapped[str | None] = mapped_column(String)  # AUDIT | INSPECTION

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    createdById: Mapped[str | None] = mapped_column(String)
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closedById: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    __table_args__ = (
        Index("ix_AuditFinding_audit_status", "auditId", "status"),
        Index("ix_AuditFinding_site_severity", "siteId", "severity"),
        Index("ix_AuditFinding_due", "dueDate", "status"),
    )


# ── WP-40: certification evidence pack ───────────────────────────────


class EvidencePackJob(Base, IdMixin):
    """An async export of everything a certification body asks for.

    Async because it must be: a 1,500-checkpoint engagement with 200 photos will
    not finish inside a request cycle. The job row is the progress the UI polls.

    `manifest` carries the integrity hashes from docs/cams/09 §2.5, so the pack
    is self-verifying — a recipient can confirm nothing changed after issue
    without access to this system.
    """

    __tablename__ = "EvidencePackJob"

    scopeKind: Mapped[str] = mapped_column(String, nullable=False)  # AUDIT | PROGRAMME_CYCLE
    scopeId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    status: Mapped[str] = mapped_column(String, nullable=False, default="QUEUED")
    progressPct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currentStep: Mapped[str | None] = mapped_column(String)

    includeEvidencePhotos: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    includeFullRegister: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    itemCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    totalBytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # [{path, kind, sha256, bytes}] — the self-verification contract.
    manifest: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    storagePath: Mapped[str | None] = mapped_column(String)
    errorMessage: Mapped[str | None] = mapped_column(Text)

    requestedById: Mapped[str] = mapped_column(String, nullable=False)
    requestedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_EvidencePackJob_scope", "scopeKind", "scopeId", "status"),)


# ── WP-43: notification preferences ──────────────────────────────────


class NotificationPreference(Base, IdMixin):
    """Per-user, per-event-class delivery preference.

    Keyed on an event CLASS rather than each of the 14 event codes: nobody wants
    fourteen toggles, and a class ("CAPA", "ASSIGNMENT") is the granularity
    people actually think in.

    Absent row = the module default (DAILY digest, in-app always on). Storing
    only deviations means a new event type inherits sensible behaviour instead
    of being silently muted for every existing user.
    """

    __tablename__ = "NotificationPreference"

    userId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    module: Mapped[str] = mapped_column(String, nullable=False, default="CAMS")
    # ASSIGNMENT | EXECUTION | CAPA | SIGNOFF | PROGRAMME
    eventClass: Mapped[str] = mapped_column(String, nullable=False)

    inAppEnabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    emailFrequency: Mapped[str] = mapped_column(String, nullable=False, default="DAILY")

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("userId", "module", "eventClass", name="uq_NotificationPreference"),
    )


# ── WP-45: supplier audits, WITHOUT a new entity ─────────────────────


class SupplierAuditLink(Base, IdMixin):
    """Links an engagement to an existing ERM `VendorProfile`.

    The brief asks for "a `Supplier` auditable entity distinct from own
    facilities". The platform already has one: `VendorProfile` carries
    vendorCode, legalName, category, criticality, tier, siteScope and a
    relationship owner, and ERM's dual-lens vendor scoring depends on it.
    Creating a second supplier table would fork the master data and guarantee
    drift — so this is a LINK, and the supplier stays where it lives.

    `supplierContactName`/`Email` are free text on purpose: a vendor factory
    manager will never have a platform seat (the WP-25 lightweight-participation
    problem), so their response arrives out of band until that lands.
    """

    __tablename__ = "SupplierAuditLink"

    engagementKind: Mapped[str] = mapped_column(String, nullable=False)  # AUDIT | INSPECTION
    engagementId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    vendorProfileId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # Which of the vendor's sites this engagement covers (a vendor may have many).
    vendorSiteRef: Mapped[str | None] = mapped_column(String)
    supplierContactName: Mapped[str | None] = mapped_column(String)
    supplierContactEmail: Mapped[str | None] = mapped_column(String)

    # Snapshot at scheduling time — a vendor re-tiered later must not silently
    # rewrite why this audit was scheduled.
    criticalityAtScheduling: Mapped[str | None] = mapped_column(String)
    tierAtScheduling: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    createdById: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint(
            "engagementKind", "engagementId", "vendorProfileId", name="uq_SupplierAuditLink"
        ),
        Index("ix_SupplierAuditLink_vendor", "vendorProfileId"),
    )


# ── WP-46: field-facing i18n (Q18 — en + hi, no ta/kn) ───────────────


class CheckpointTranslation(Base, IdMixin):
    """Per-language checkpoint text.

    Q18 answered **no** to Tamil and Kannada, so this ships `en` + `hi`. The
    mechanism is language-agnostic: adding a language is rows, never a schema
    change, so honouring Q18 costs nothing later.

    Keyed on `checkpointCode` + `libraryCode` rather than a materialised
    response id, because a translation belongs to the QUESTION, not to one
    audit's copy of it — otherwise every new engagement would need re-translating.

    Auditor-facing UI stays English-first (the brief's decision); this is for
    the conduct screen and anything an auditee reads.
    """

    __tablename__ = "CheckpointTranslation"

    libraryCode: Mapped[str] = mapped_column(String, nullable=False, index=True)
    checkpointCode: Mapped[str] = mapped_column(String, nullable=False, index=True)
    language: Mapped[str] = mapped_column(String, nullable=False)  # en | hi

    questionText: Mapped[str] = mapped_column(Text, nullable=False)
    guidance: Mapped[str | None] = mapped_column(Text)

    # Who produced it. A machine translation of a safety question is not the
    # same artefact as a reviewed one, and a field auditor deserves to know.
    source: Mapped[str] = mapped_column(String, nullable=False, default="HUMAN")
    reviewedById: Mapped[str | None] = mapped_column(String)
    reviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "libraryCode", "checkpointCode", "language", name="uq_CheckpointTranslation"
        ),
    )


__all__ = [
    "AuditFinding",
    "EvidencePackJob",
    "NotificationPreference",
    "SupplierAuditLink",
    "CheckpointTranslation",
]
