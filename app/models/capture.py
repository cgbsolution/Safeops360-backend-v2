"""Guided Field Capture — models.

Low-literacy field reporting: icon-first wizard submissions staged in
``CaptureSubmission`` (idempotent on the client-generated UUID), triaged by a
safety officer onto the 5x5 matrix and optionally *converted* into a real
Observation / Near Miss / Incident (which then run their normal workflows).
``CaptureTaxonomy`` is one table for the three spec collections
(hazard_taxonomy / cause_library / control_library — same shape, ``kind``
discriminator) with bilingual labels at data level. ``UploadSession``/
``UploadChunk`` back the resumable chunked media upload for offline sync.

Schema is owned here (SQLAlchemy) and applied by hand-DDL
(``prisma/apply-capture-ddl.ts``) — NEVER via ``prisma db push`` (drifted
tables would be dropped). Mirrored in schema.prisma for seed-script access.
camelCase column names match the platform-wide Prisma convention.
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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin, SoftDeleteMixin, TimestampMixin
from app.models.user import User


# ── Field submission (staging entity — see DECISIONS.md D3) ──────────────────
class CaptureSubmission(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """One guided-wizard field report. Insert-only from the field (no merge
    conflicts on offline sync); officers mutate only triage/conversion fields.
    ``clientSubmissionId`` is generated on-device before any network call —
    the (tenantId, clientSubmissionId) unique index is the idempotency
    backstop for sync retries."""

    __tablename__ = "CaptureSubmission"

    tenantId: Mapped[str] = mapped_column(String, nullable=False, default="default")
    number: Mapped[str] = mapped_column(String, nullable=False, unique=True)  # FLD-2026-NW-0001
    clientSubmissionId: Mapped[str] = mapped_column(String, nullable=False)

    # observation | near_miss | unsafe_condition | incident
    type: Mapped[str] = mapped_column(String, nullable=False, default="observation")

    # reporter — nullable + anonHash so "report without my name" stays
    # dedupable/abuse-controllable without storing identity in the clear.
    reporterId: Mapped[str | None] = mapped_column(ForeignKey("User.id"), index=True)
    isAnonymous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    anonHash: Mapped[str | None] = mapped_column(String, index=True)

    # location: area tile, map pin (percent coords on the site layout image),
    # or QR scan (area and/or equipment in one step).
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    areaId: Mapped[str | None] = mapped_column(String, index=True)
    mapPinX: Mapped[float | None] = mapped_column(Float)
    mapPinY: Mapped[float | None] = mapped_column(Float)
    equipmentId: Mapped[str | None] = mapped_column(String)
    qrScanned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # category = two CaptureTaxonomy(kind=HAZARD) levels; snapshot keeps the
    # labels as shown at submit time so history survives taxonomy edits.
    categoryL1Id: Mapped[str | None] = mapped_column(String, index=True)
    categoryL2Id: Mapped[str | None] = mapped_column(String)
    categorySnapshot: Mapped[dict | None] = mapped_column(JSON)
    aiSuggested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    aiConfidence: Mapped[float | None] = mapped_column(Float)
    aiSuggestion: Mapped[dict | None] = mapped_column(JSON)  # raw provider output (provenance)

    # low | medium | high — technician self-report; the 5x5 mapping happens at
    # officer triage (technician never sees a matrix).
    severitySelfReported: Mapped[str] = mapped_column(String, nullable=False, default="medium")

    description: Mapped[str | None] = mapped_column(Text)  # optional: typed, voice transcript, or AI draft

    # optional voice description (screen 5) — audio lives as a VOICE
    # CaptureAttachment; transcript fields here. English translation is an
    # async job, never blocks submission.
    voiceLangCode: Mapped[str | None] = mapped_column(String)
    transcriptOriginal: Mapped[str | None] = mapped_column(Text)
    transcriptEnglish: Mapped[str | None] = mapped_column(Text)
    # none | device | pending | done | failed
    transcriptionStatus: Mapped[str] = mapped_column(String, nullable=False, default="none")

    # submitted | triaged | converted | closed | rejected
    status: Mapped[str] = mapped_column(String, nullable=False, default="submitted", index=True)

    # officer triage (5x5)
    triagedById: Mapped[str | None] = mapped_column(String)
    triagedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hiraLikelihood: Mapped[int | None] = mapped_column(Integer)
    hiraSeverity: Mapped[int | None] = mapped_column(Integer)
    riskScore: Mapped[int | None] = mapped_column(Integer)
    riskLevel: Mapped[str | None] = mapped_column(String)  # LOW | MODERATE | HIGH | CRITICAL
    triageNote: Mapped[str | None] = mapped_column(Text)

    # conversion into the real module record (golden-thread entry point)
    convertedEntityType: Mapped[str | None] = mapped_column(String)  # Observation | NearMiss | Incident
    convertedEntityId: Mapped[str | None] = mapped_column(String)
    convertedById: Mapped[str | None] = mapped_column(String)
    convertedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # downstream golden-thread links (loose ids, populated by later modules)
    linkedRcaIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    linkedCapaIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    linkedPtwIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # adoption analytics (spec 1.1.7) — logged per submission
    tapCount: Mapped[int | None] = mapped_column(Integer)
    durationMs: Mapped[int | None] = mapped_column(Integer)
    wasOffline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    appVersion: Mapped[str | None] = mapped_column(String)
    deviceLang: Mapped[str | None] = mapped_column(String)

    taxonomyVersion: Mapped[int | None] = mapped_column(Integer)  # client cache version at submit
    createdAtClient: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    reporter: Mapped[User | None] = relationship(foreign_keys=[reporterId], lazy="joined")

    __table_args__ = (
        UniqueConstraint("tenantId", "clientSubmissionId", name="uq_CaptureSubmission_tenant_client"),
        Index("ix_CaptureSubmission_plant_status_created", "tenantId", "plantId", "status", "createdAt"),
        # cluster rule: >=3 same-category same-area in 7 days
        Index("ix_CaptureSubmission_cluster", "plantId", "areaId", "categoryL1Id"),
    )


class CaptureAttachment(Base, IdMixin):
    """Media evidence on a submission (photo / <=30s video / <=60s voice).
    Same two-phase signed-URL upload as ObservationAttachment; offline media
    additionally flows through UploadSession chunks before landing here.
    ``clientMediaId`` makes media attach idempotent on sync retry."""

    __tablename__ = "CaptureAttachment"

    submissionId: Mapped[str] = mapped_column(
        ForeignKey("CaptureSubmission.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # PHOTO | VIDEO | VOICE | DOCUMENT
    kind: Mapped[str] = mapped_column(String, nullable=False, index=True)
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    storagePath: Mapped[str] = mapped_column(String, nullable=False)
    fileSize: Mapped[int] = mapped_column(Integer, nullable=False)
    mimeType: Mapped[str] = mapped_column(String, nullable=False)
    durationSec: Mapped[float | None] = mapped_column(Float)
    caption: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String)
    clientMediaId: Mapped[str | None] = mapped_column(String, index=True)
    uploadedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    uploadedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


# ── Taxonomy (hazard categories + cause library + control library) ───────────
class CaptureTaxonomy(Base, IdMixin, TimestampMixin):
    """One node of the icon-tile pickers. ``kind`` HAZARD nodes are 2 levels
    (screen 2); CAUSE/CONTROL nodes are up to 3 levels and carry the fishbone
    category for the guided 5-Why flow. ``labels`` is bilingual at data level
    per the spec: {"en": ..., "hi": ...}."""

    __tablename__ = "CaptureTaxonomy"

    tenantId: Mapped[str | None] = mapped_column(String)  # null = global
    # HAZARD | CAUSE | CONTROL
    kind: Mapped[str] = mapped_column(String, nullable=False, index=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parentId: Mapped[str | None] = mapped_column(String, index=True)
    code: Mapped[str] = mapped_column(String, nullable=False)  # stable key, e.g. slip_trip_fall
    labels: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    iconKey: Mapped[str | None] = mapped_column(String)
    # EQUIPMENT | PERSON | PROCESS | ENVIRONMENT | MATERIAL | MANAGEMENT (CAUSE/CONTROL)
    fishboneCategory: Mapped[str | None] = mapped_column(String)
    sortWeight: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("kind", "code", name="uq_CaptureTaxonomy_kind_code"),
    )


class TaxonomyAlias(Base, IdMixin):
    """Maps a retired/renamed taxonomy code to its replacement so submissions
    synced with a stale offline taxonomy cache still resolve (spec 1.4
    conflict policy)."""

    __tablename__ = "TaxonomyAlias"

    kind: Mapped[str] = mapped_column(String, nullable=False)
    fromCode: Mapped[str] = mapped_column(String, nullable=False)
    toCode: Mapped[str] = mapped_column(String, nullable=False)
    note: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("kind", "fromCode", name="uq_TaxonomyAlias_kind_from"),
    )


# ── Guided RCA field input (spec 1.3) ─────────────────────────────────────────
class RcaFieldRequest(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """An RCA owner's "Request field input" — fans out an in-app notification
    to the selected technicians, who respond through the guided 5-Why picker."""

    __tablename__ = "RcaFieldRequest"

    rcaId: Mapped[str] = mapped_column(String, nullable=False, index=True)  # RootCauseAnalysis.id
    requestedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    plantId: Mapped[str | None] = mapped_column(String, index=True)
    contextSummary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    hazardCategoryCode: Mapped[str | None] = mapped_column(String)  # scopes the cause picker
    technicianIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    dueAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # OPEN | CLOSED | CANCELLED
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN", index=True)


class RcaFieldInput(Base, IdMixin, TimestampMixin):
    """One technician's structured cause contribution: tap-picked cause path
    (max 3 levels), prevention suggestions from the control library, optional
    voice note per level. Promotable to an official RcaIdentifiedCause with
    provenance kept (``promotedCauseId``)."""

    __tablename__ = "RcaFieldInput"

    requestId: Mapped[str] = mapped_column(
        ForeignKey("RcaFieldRequest.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rcaId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    contributorId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    isAnonymous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    anonHash: Mapped[str | None] = mapped_column(String)

    fishboneCategory: Mapped[str | None] = mapped_column(String)
    # [{level, nodeId, code, label, voiceNote?: {storagePath, langCode}}]
    causePath: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # CaptureTaxonomy(kind=CONTROL) ids — "what would prevent this?"
    controlSuggestionIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    note: Mapped[str | None] = mapped_column(Text)

    voiceStoragePath: Mapped[str | None] = mapped_column(String)
    voiceLangCode: Mapped[str | None] = mapped_column(String)
    transcriptOriginal: Mapped[str | None] = mapped_column(Text)
    transcriptEnglish: Mapped[str | None] = mapped_column(Text)

    promotedCauseId: Mapped[str | None] = mapped_column(String)  # RcaIdentifiedCause.id
    promotedById: Mapped[str | None] = mapped_column(String)
    promotedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── Resumable chunked upload (offline sync path, spec 1.4) ───────────────────
class UploadSession(Base, IdMixin, TimestampMixin):
    """One media file being uploaded in <=2 MB chunks (the Vercel proxy caps
    request bodies). ``sha256`` gives content-hash dedup: re-initiating an
    already-completed hash returns the existing storagePath immediately.
    Chunks are staged in Postgres, assembled once complete, pushed to Supabase
    Storage, and the chunk rows deleted."""

    __tablename__ = "UploadSession"

    ownerId: Mapped[str | None] = mapped_column(ForeignKey("User.id"), index=True)
    clientMediaId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    mimeType: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="PHOTO")
    totalSize: Mapped[int] = mapped_column(Integer, nullable=False)
    chunkSize: Mapped[int] = mapped_column(Integer, nullable=False)
    totalChunks: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String, index=True)
    # PENDING | COMPLETE | FAILED
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING", index=True)
    storagePath: Mapped[str | None] = mapped_column(String)


class UploadChunk(Base, IdMixin):
    __tablename__ = "UploadChunk"

    sessionId: Mapped[str] = mapped_column(
        ForeignKey("UploadSession.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunkIndex: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("sessionId", "chunkIndex", name="uq_UploadChunk_session_index"),
    )


__all__ = [
    "CaptureSubmission",
    "CaptureAttachment",
    "CaptureTaxonomy",
    "TaxonomyAlias",
    "RcaFieldRequest",
    "RcaFieldInput",
    "UploadSession",
    "UploadChunk",
]
