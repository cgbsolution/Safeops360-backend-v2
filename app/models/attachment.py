"""Shared Evidence Attachment model (spec Stream B §5).

One generic attachment table for the whole platform, keyed by (`entityType`,
`entityId`), replacing the bespoke per-module clones (IncidentAttachment,
RiskAttachment, MocAttachment, …) for NEW attachment surfaces. The existing
per-module tables are left in place — this is additive, not a migration of live
data.

Distinct from those clones in three ways the spec calls for:
  * `documentCategory` — SDS / certificate / license / photo / report. A plain
    dropdown here, but it is the field Stream B §6's document-AI keys off, so it
    lives at the storage layer, not the AI layer.
  * versioning — re-uploading against the same `slotKey` supersedes the prior
    file (marks it `isCurrent=False`, bumps `version`, sets `supersedesId`)
    rather than silently overwriting; prior versions stay queryable for the
    audit trail.
  * `extraction` — reserved JSON for §6 auto-extraction provenance
    ({status, fields, provider, extractedAt, confirmedBy}). Null until
    extraction is enabled; the attachment layer is fully usable without it.

Storage is Supabase (the platform binary store) behind a swappable path builder
(`build_evidence_storage_path`); an airgapped/local backend drops in behind the
same `app/services/storage.py` interface.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin
from app.models.user import User


class Attachment(Base, IdMixin):
    __tablename__ = "Attachment"
    __table_args__ = (
        Index("ix_Attachment_entity", "entityType", "entityId"),
        Index("ix_Attachment_entity_current", "entityType", "entityId", "isCurrent"),
    )

    # Polymorphic parent — resolved via app/services/evidence_registry.py.
    entityType: Mapped[str] = mapped_column(String, nullable=False, index=True)
    entityId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # Per-entity upload category (e.g. AUDIT_EVIDENCE, SDS_SHEET) — validated
    # against the entity's allowed set in the router.
    category: Mapped[str] = mapped_column(String, nullable=False)
    # Cross-cutting document class the AI layer keys off (spec §5.1 / §6.1).
    documentCategory: Mapped[str | None] = mapped_column(String, index=True)

    fileName: Mapped[str] = mapped_column(String, nullable=False)
    storagePath: Mapped[str] = mapped_column(String, nullable=False)
    fileSize: Mapped[int] = mapped_column(Integer, nullable=False)
    mimeType: Mapped[str] = mapped_column(String, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)

    # Versioning: files sharing (entityType, entityId, slotKey) form a version
    # chain; only one is isCurrent. A null slotKey means a standalone file.
    slotKey: Mapped[str | None] = mapped_column(String, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    supersedesId: Mapped[str | None] = mapped_column(String)
    isCurrent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    # §6 auto-extraction provenance (populated only when extraction is enabled).
    extraction: Mapped[dict | None] = mapped_column(JSONB)

    uploadedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    uploadedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    uploadedBy: Mapped[User] = relationship(foreign_keys=[uploadedById], lazy="joined")


__all__ = ["Attachment"]
