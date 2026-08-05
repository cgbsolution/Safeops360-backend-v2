"""Pydantic schemas for ERM attachments (risk supporting docs, control
evidence). Cloned from the incident attachment schemas.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AttachmentInit(BaseModel):
    phase: str = Field(pattern="^init$")
    category: str
    fileName: str
    fileSize: int = Field(gt=0)
    mimeType: str
    # Control-only optional fields (ignored for risk uploads).
    controlTestId: str | None = None
    reviewDate: datetime | None = None


class AttachmentComplete(BaseModel):
    phase: str = Field(pattern="^complete$")
    attachmentId: str
    caption: str | None = None


class AttachmentUploader(BaseModel):
    id: str
    name: str
    designation: str | None = None

    model_config = {"from_attributes": True}


class RiskAttachmentOut(BaseModel):
    id: str
    riskId: str
    category: str
    fileName: str
    fileSize: int
    mimeType: str
    caption: str | None
    uploadedAt: datetime
    uploadedById: str
    uploadedBy: AttachmentUploader | None = None

    model_config = {"from_attributes": True}


class ControlAttachmentOut(BaseModel):
    id: str
    controlId: str
    category: str
    fileName: str
    fileSize: int
    mimeType: str
    caption: str | None
    uploadedAt: datetime
    uploadedById: str
    uploadedBy: AttachmentUploader | None = None

    model_config = {"from_attributes": True}
