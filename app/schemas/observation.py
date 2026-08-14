from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.observation import (
    ObservationCategory,
    ObservationStatus,
    ObservationType,
    Severity,
)
from app.models.observation_severity import (
    OVERRIDE_SOURCE_OBSERVER_FORM,
    OVERRIDE_SOURCES,
)
from app.schemas.observation_sla import WorkerInvolvedIn, WorkerInvolvedOut


class ObservationCreate(BaseModel):
    # extra="ignore" so any stray form fields (location, correctiveAction —
    # carried over from the Prisma era — or anything else the form sends
    # that the schema doesn't enumerate) are silently dropped instead of
    # rejected with a 422.
    model_config = ConfigDict(extra="ignore")

    plantId: str
    areaId: str | None = None
    type: ObservationType
    # Legacy hazard category. Required for the SAFE types (which don't carry
    # the STOP taxonomy); for at-risk types the router DERIVES it from
    # categoryCode instead, so the client omits it — see create_observation.
    category: ObservationCategory | None = None
    # DuPont STOP taxonomy. Required for UNSAFE_ACT / UNSAFE_CONDITION and
    # rejected-if-mismatched by services.observation_taxonomy.validate_selection;
    # ignored for the safe types.
    categoryCode: str | None = None
    subCategoryCode: str | None = None
    severity: Severity = Severity.LOW
    # Why this severity differs from the one the engine suggested for the chosen
    # category / sub-category / area. Required (min 10 chars) ONLY when the
    # server's own re-resolution produces a suggestion and it differs — the
    # client's claim about what it was shown is never what the gate keys on.
    # See services/observation_severity.require_reason.
    severityOverrideReason: str | None = None
    # What the form actually displayed, echoed back for diagnostics. Advisory
    # only: the server recomputes the suggestion and that value is authoritative.
    suggestedSeverity: Severity | None = None
    # Provenance label for the override log — which surface the severity
    # decision was made on. Set by the /capture triage conversion, which calls
    # create_observation in-process; the observer form leaves it at the default.
    # A REPORTING label only: it never affects whether a reason is required, and
    # an override recorded under any source is still fully logged and readable
    # with `includeAllSources=true`. It is not a trust boundary.
    severityOverrideSource: str = OVERRIDE_SOURCE_OBSERVER_FORM

    @field_validator("severityOverrideSource")
    @classmethod
    def _known_source(cls, v: str) -> str:
        return v if v in OVERRIDE_SOURCES else OVERRIDE_SOURCE_OBSERVER_FORM
    description: str = Field(min_length=10)
    # P3-1 BBS — optional ABC (antecedent → behaviour → consequence) analysis
    antecedent: str | None = None
    behaviourObserved: str | None = None
    consequence: str | None = None
    immediateAction: str | None = None
    # responsiblePersonId is now assigned by the Section Head during the
    # CHECKER step, not by the observer at creation time. Kept optional
    # here so direct API callers can still set it if they want to.
    responsiblePersonId: str | None = None
    # Contractor traceability — set when the observation involves a contractor.
    contractorCompanyId: str | None = None
    # Only honoured when NO SLA policy matches this severity / category group.
    # With a policy in force the server computes the date and ignores this —
    # the field is read-only in the UI and trusting the client here would make
    # that read-only purely cosmetic. See services/observation_sla.apply_on_create.
    targetDate: datetime | None = None
    date: datetime
    # Named worker(s) who committed the act. MANDATORY for UNSAFE_ACT at
    # HIGH/CRITICAL severity, optional otherwise — deliberately not required on
    # Medium/Low or on Unsafe Conditions, so routine reporting stays about
    # fixing hazards rather than naming people.
    workersInvolved: list[WorkerInvolvedIn] = []


class ObservationUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: ObservationStatus | None = None
    closingRemark: str | None = None
    responsiblePersonId: str | None = None
    targetDate: datetime | None = None
    # ─── Editable core details ("edit while open"). All optional; only the
    #     keys the client sends are applied. The router blocks these once the
    #     observation is CLOSED and enforces OBSERVATION.UPDATE. ───
    type: ObservationType | None = None
    category: ObservationCategory | None = None
    categoryCode: str | None = None
    subCategoryCode: str | None = None
    severity: Severity | None = None
    # Same rule as on create, but only evaluated when the edit actually touches
    # something the suggestion depends on (severity, taxonomy, type or area).
    # A description-only PATCH never has to justify a severity set months ago.
    severityOverrideReason: str | None = None
    description: str | None = Field(default=None, min_length=10)
    areaId: str | None = None


class ObservationOut(BaseModel):
    id: str
    number: str
    date: datetime
    type: ObservationType
    category: ObservationCategory
    # STOP taxonomy. Null on safe observations and on legacy at-risk rows the
    # migration could not confidently map (those sit in UnmappedLegacyObservation).
    categoryCode: str | None = None
    subCategoryCode: str | None = None
    taxonomyAxis: str | None = None
    severity: Severity
    plantId: str
    areaId: str | None
    observerId: str
    responsiblePersonId: str | None
    contractorCompanyId: str | None = None
    description: str
    immediateAction: str | None
    # Still a flat field. The SLA layer added provenance alongside it rather
    # than nesting it as `targetClosureDate.date`, so every existing report,
    # dashboard and mobile screen reading `targetDate` kept working unchanged.
    targetDate: datetime | None
    targetDateSource: str | None = None
    targetDateSlaConfig: dict | None = None
    targetDateOverrideReason: str | None = None
    closingRemark: str | None
    closedAt: datetime | None
    status: ObservationStatus
    createdAt: datetime
    updatedAt: datetime

    # Submit-time auto-detections + cross-module trigger output. Present in the
    # schema since the beginning but absent from this model until the detail
    # view needed them for its "Related Items" block.
    isRepeat: bool = False
    similarObservationIds: list[str] | None = None
    activePermitId: str | None = None
    permitReviewFlagged: bool = False
    triggeredInspectionId: str | None = None
    triggeredTbtId: str | None = None
    triggeredCapaId: str | None = None
    contributedToIncidentId: str | None = None
    closureTriggers: list | None = None
    # AI agent outputs persisted by the workflow engine (rule_triage_on_submit
    # + rule_lessons_distribution). Shape: [{ruleId, ruleName, fired, data}].
    # The mobile / web clients render whatever the rules emitted; an empty
    # array or `null` means no agent has fired yet.
    closureTriggers: list[dict] | None = None
    # Populated by the detail route only (the list route leaves it empty rather
    # than firing a child query per row).
    workersInvolved: list[WorkerInvolvedOut] = []

    model_config = {"from_attributes": True}


class ObservationListResponse(BaseModel):
    items: list[ObservationOut]
    total: int
