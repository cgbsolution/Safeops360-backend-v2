"""Pydantic contracts for the severity suggestion engine."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SeveritySuggestionOut(BaseModel):
    """What `GET /api/observations/severity-suggestion` returns.

    The shape is identical whether or not a rule matched, so the form renders
    the suggestion or the plain manual dropdown from one response. `suggested is
    None` with `rationale` explaining why is the supported "no opinion" answer.
    """

    suggested: str | None = None
    # The rule's own severity before the area tier was applied. Rendered
    # alongside the tier so an uplift is visible rather than mysterious.
    baseSeverity: str | None = None
    tierApplied: str | None = None
    # area | plant | default — which AreaHazardTier row supplied the tier.
    tierSource: str | None = None
    # True only when the tier actually changed the answer. The form mentions
    # the area only in that case.
    tierUplifted: bool = False
    rationale: str | None = None
    matrixRuleId: str | None = None
    observationType: str | None = None
    categoryCode: str | None = None
    subCategoryCode: str | None = None
    # Echoed so the client never hardcodes the ladder to render an override
    # warning or order a dropdown.
    severityLadder: list[str] = []
    minOverrideReasonChars: int


class SeverityOverrideOut(BaseModel):
    id: str
    observationId: str
    suggestedSeverity: str
    finalSeverity: str
    overrideReason: str | None
    observationType: str | None
    categoryCode: str | None
    subCategoryCode: str | None
    baseSeverity: str | None
    hazardTier: str | None
    source: str
    overriddenById: str | None
    createdAt: datetime

    model_config = ConfigDict(from_attributes=True)


class CalibrationRow(BaseModel):
    """One taxonomy pair's override behaviour.

    `dominantDirection` + `directionConsistencyPct` are the actionable pair: a
    pair overridden 12 times, 100% upward, is a wrong matrix rule. The same 12
    overrides split 6/6 is usually a sub-category covering two different
    exposures — a taxonomy problem, not a matrix one.
    """

    observationType: str | None
    categoryCode: str | None
    subCategoryCode: str | None
    suggestedSeverity: str
    overrides: int
    up: int
    down: int
    observations: int
    overrideRatePct: float | None
    dominantDirection: str
    directionConsistencyPct: float | None


class CalibrationReportOut(BaseModel):
    rows: list[CalibrationRow]
    since: datetime | None
    # Which override sources were counted — the observer form only, by default.
    # A triager's or an editor's severity call is not observer disagreement.
    sources: list[str]
