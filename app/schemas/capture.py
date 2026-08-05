"""Guided Field Capture — request/response schemas.

The wizard's payload mirrors the spec's document shape but flattened for
Postgres columns. Everything optional except the idempotency key, type and
location plant — no free-text field is ever mandatory (spec 1.1.1).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SubmissionType = Literal["observation", "near_miss", "unsafe_condition", "incident", "ptw", "flra"]
SelfSeverity = Literal["low", "medium", "high"]


class LocationIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    areaId: str | None = None
    mapPinX: float | None = Field(default=None, ge=0, le=100)  # % of site layout image
    mapPinY: float | None = Field(default=None, ge=0, le=100)
    equipmentId: str | None = None
    qrScanned: bool = False


class CategoryIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    l1Id: str | None = None
    l2Id: str | None = None
    # offline clients may only know stable codes (taxonomy cache) — the server
    # resolves codes → ids, following TaxonomyAlias for stale caches.
    l1Code: str | None = None
    l2Code: str | None = None
    aiSuggested: bool = False
    aiConfidence: float | None = None

    # ── DuPont STOP taxonomy (observation flow only) ──
    # The observation flow classifies against ObservationTaxonomy rather than
    # the CaptureTaxonomy hazard tree, because the tiles have to differ by
    # act/condition. Validated against the submission's axis server-side and
    # snapshotted; there are no CaptureTaxonomy rows for these, so l1Id/l2Id
    # stay null rather than being pointed at an invented hazard node.
    stopCategoryCode: str | None = None
    stopSubCategoryCode: str | None = None


class VoiceIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    langCode: str | None = None
    # on-device Web Speech transcript, when the browser produced one
    transcriptOriginal: str | None = None
    clientMediaId: str | None = None  # links to the VOICE attachment


class CaptureMetaIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tapCount: int | None = Field(default=None, ge=0, le=500)
    durationMs: int | None = Field(default=None, ge=0)
    offline: bool = False
    appVersion: str | None = None
    deviceLang: str | None = None


class SubmissionCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    clientSubmissionId: str = Field(min_length=8, max_length=64)
    type: SubmissionType = "observation"
    plantId: str | None = None  # defaults to the reporter's plant
    anonymous: bool = False
    location: LocationIn = Field(default_factory=LocationIn)
    category: CategoryIn | None = None
    # Act-vs-condition for the `observation` flow (the wizard's "Kind" step).
    # Deliberately NOT folded into `type`: `unsafe_condition` is already a
    # separate top-level report flow with its own duration step, so reusing it
    # here would silently reroute the reporter. Persisted in categorySnapshot
    # (no column needed) and read back at conversion — before this, the Kind
    # chip only reached the description text, so every converted capture
    # observation became UNSAFE_ACT no matter what was picked.
    observationKind: Literal["unsafe_act", "unsafe_condition"] | None = None
    severity: SelfSeverity = "medium"
    description: str | None = None
    voice: VoiceIn | None = None
    capture: CaptureMetaIn | None = None
    createdAtClient: datetime | None = None
    taxonomyVersion: int | None = None


class TriageBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hiraLikelihood: int = Field(ge=1, le=5)
    hiraSeverity: int = Field(ge=1, le=5)
    note: str | None = None


class ConvertBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    target: Literal["observation", "near_miss", "incident", "ptw", "flra"]
    # officer can override/complete the narrative before conversion; when
    # absent a description is synthesised from category labels + transcript.
    description: str | None = None
    # incident conversions must classify the initial type (existing Phase-1 contract)
    incidentType: str | None = None

    # ── Observation conversion: DuPont STOP taxonomy override. ──
    # The wizard classifies against the CaptureTaxonomy hazard list, which the
    # converter maps onto a STOP category/sub-category. A triager who can see
    # the photo and the transcript often knows better — these let them say so.
    # Omitted ⇒ the mapping stands. Either way the pair is validated against
    # the record's act/condition axis server-side.
    categoryCode: str | None = None
    subCategoryCode: str | None = None

    # ── PTW conversion: the authorisation-chain fields a field technician
    # cannot supply — the officer completes them at triage (spec §8.2). ──
    permitType: str | None = None
    validFrom: datetime | None = None
    validTo: datetime | None = None
    issuerId: str | None = None
    receiverId: str | None = None

    # ── FLRA conversion: crew + toolbox-talk the officer supplies. ──
    teamMemberIds: list[str] = Field(default_factory=list)
    toolboxTalkById: str | None = None


class CleanupTextBody(BaseModel):
    """AI grammar/clarity cleanup request (spec §7a)."""
    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=4000)
    lang: str = "hi"


class SuggestCategoryBody(BaseModel):
    """Text → hazard category suggestion request (spec §7b)."""
    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=4000)
    lang: str = "hi"


class GuidedAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    q: str = Field(max_length=200)
    a: str = Field(min_length=1, max_length=1000)


class DraftDescriptionBody(BaseModel):
    """AI 'help me write' request — a few guided-question answers → a drafted,
    FACT-ONLY report description (guided draft, spec §7c). The reporter always
    accepts/edits the result; it is never auto-applied."""
    model_config = ConfigDict(extra="ignore")

    reportType: str = "observation"
    lang: str = "hi"
    categoryLabel: str | None = Field(default=None, max_length=300)
    location: str | None = Field(default=None, max_length=300)
    severity: str | None = None
    answers: list[GuidedAnswer] = Field(min_length=1, max_length=8)


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str = Field(min_length=3)
