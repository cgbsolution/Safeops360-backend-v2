"""Pydantic schemas for the shared Evidence Attachment layer (Stream B §5)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# Cross-cutting document classes (spec §5.1). `certificate`/`license`/`SDS` are
# the ones Stream B §6 auto-extraction keys off.
DOCUMENT_CATEGORIES = {"SDS", "certificate", "license", "photo", "report", "other"}


class EvidenceInit(BaseModel):
    phase: str = Field(pattern="^init$")
    category: str
    fileName: str
    fileSize: int = Field(gt=0)
    mimeType: str
    documentCategory: str | None = None
    # Supplying a slotKey enables versioning: a new upload with the same slot on
    # the same entity supersedes the prior file instead of overwriting it.
    slotKey: str | None = None
    caption: str | None = None


class EvidenceComplete(BaseModel):
    phase: str = Field(pattern="^complete$")
    attachmentId: str
    caption: str | None = None


class EvidenceUploader(BaseModel):
    id: str
    name: str
    designation: str | None = None

    model_config = {"from_attributes": True}


class AttachmentOut(BaseModel):
    id: str
    entityType: str
    entityId: str
    category: str
    documentCategory: str | None
    fileName: str
    fileSize: int
    mimeType: str
    caption: str | None
    slotKey: str | None
    version: int
    supersedesId: str | None
    isCurrent: bool
    extraction: dict[str, Any] | None
    uploadedAt: datetime
    uploadedById: str
    uploadedBy: EvidenceUploader | None = None

    model_config = {"from_attributes": True}


class AttachmentCount(BaseModel):
    """Row badge payload — count of current, non-deleted files per entity id."""

    counts: dict[str, int]
