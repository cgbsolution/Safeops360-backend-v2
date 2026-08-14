from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.permit import PtwEvidenceInput


class ApproveRequest(BaseModel):
    taskId: str
    comments: str | None = None
    attachments: list[str] | None = None
    recordData: dict[str, Any] | None = None
    plantId: str | None = None
    # PTW closed-loop: approvals on permit tasks REQUIRE field evidence
    # (GPS + photo + signature). Ignored for non-PTW modules. The router
    # validates via app/services/ptw_evidence.py and 422s when missing.
    evidence: PtwEvidenceInput | None = None


class RejectRequest(BaseModel):
    taskId: str
    reason: str = Field(min_length=1)
    comments: str | None = None
    # Optional for PTW — a rejection may happen off-site; whatever the
    # device can provide is still recorded on the evidence trail.
    evidence: PtwEvidenceInput | None = None


class SubmitExecutionRequest(BaseModel):
    taskId: str
    executionData: dict[str, Any] | None = None
    comments: str | None = None
    attachments: list[str] | None = None
    recordData: dict[str, Any] | None = None
    plantId: str | None = None


class VerifyRequest(BaseModel):
    taskId: str
    accepted: bool
    comments: str | None = None
    recordData: dict[str, Any] | None = None
    plantId: str | None = None


class ResubmitRequest(BaseModel):
    instanceId: str
    comments: str | None = None
    recordData: dict[str, Any] | None = None
    plantId: str | None = None


class ReassignRequest(BaseModel):
    taskId: str
    toUserId: str
    reason: str | None = None


class MyCountResponse(BaseModel):
    """Inbox counters for the workflow-task header / dashboard pill.

    The legacy `count` field is kept for back-compat with older callers; new
    clients should consume the structured pending / overdue / completed
    triplet which mirrors the mobile inbox layout. The five `tabXxx` fields
    drive the segmented Inbox tab bar.
    """

    count: int
    pending: int = 0
    overdue: int = 0
    completed: int = 0
    tabPendingApprovals: int = 0
    tabMyTasks: int = 0
    tabPendingVerification: int = 0
    tabSubmittedByMe: int = 0
    tabOverdueEscalated: int = 0
    # Unread (never-opened) counts per tab, for the rose pips on the tab bar.
    # SubmittedByMe has none — those tasks belong to other people.
    unreadTotal: int = 0
    unreadPendingApprovals: int = 0
    unreadMyTasks: int = 0
    unreadPendingVerification: int = 0
    unreadOverdueEscalated: int = 0
    # Unread rows in the Notification table — the Inbox's sixth tab. Events with
    # no workflow task behind them (an audit assignment, an overdue risk
    # treatment). Kept OUT of `count`, which older clients read as the pending
    # task total; the sidebar badge adds the two itself.
    unreadNotifications: int = 0
    # Tasks whose status IS literally OVERDUE/ESCALATED. Narrower than
    # `overdue`/`tabOverdueEscalated`, which also count anything merely past its
    # dueAt or flagged URGENT. The web Inbox badge uses this one: summing the
    # broad definition made the badge read "2 overdue" while nothing had
    # actually breached.
    overdueStrict: int = 0
    # Workflow INSTANCES this user kicked off — what the "Submitted by me" tab
    # lists. `tabSubmittedByMe` counts the tasks hanging off those instances,
    # which is a different (usually larger) number.
    submittedInstances: int = 0


class WorkflowTaskOut(BaseModel):
    id: str
    module: str
    recordId: str
    recordNumber: str | None = None
    recordTitle: str | None = None
    stepName: str
    taskType: str
    status: str
    priority: str
    assignedAt: datetime
    dueAt: datetime | None = None
    # Initiator identity — surfaced by /api/workflow/tasks so the mobile inbox
    # can render "Initiated by <name> (designation · role · department · plant)
    # · Received Y" without an extra round-trip, matching the web Inbox.
    initiatedById: str | None = None
    initiatedByName: str | None = None
    initiatedByDesignation: str | None = None
    initiatedByRole: str | None = None
    initiatedByDepartment: str | None = None
    initiatedByPlantName: str | None = None
    isOverdue: bool = False
    # Inbox read state — false until the assignee has opened the record. Drives
    # the unread highlight on the row, same contract as Notification.isRead.
    isRead: bool = False
    readAt: datetime | None = None

    model_config = {"from_attributes": True}


class WorkflowTaskListResponse(BaseModel):
    items: list[WorkflowTaskOut]
    total: int


class WorkflowHistoryEntry(BaseModel):
    """One row in the per-record audit trail. Pivoted by the workflow
    engine each time a step is approved / rejected / executed / verified
    / reassigned / commented / escalated."""

    id: str
    stepName: str
    action: str
    performedById: str
    performedByName: str | None = None
    # Full actor identity, same contract as WorkflowPendingTask. A name alone
    # is ambiguous on role-shaped accounts ("Process Operator" exists at every
    # plant), so the trail carries designation / role / department / plant too.
    performedByDesignation: str | None = None
    performedByRole: str | None = None
    performedByDepartment: str | None = None
    performedByPlantName: str | None = None
    comments: str | None = None
    # JSON array of filenames the actor attached when completing the step.
    # Surfaced so a client can render the corrective-action record (narrative +
    # evidence) without a second round-trip.
    attachments: str | None = None
    fromStatus: str | None = None
    toStatus: str | None = None
    performedAt: datetime

    model_config = {"from_attributes": True}


class WorkflowHistoryResponse(BaseModel):
    items: list[WorkflowHistoryEntry]
    total: int


class WorkflowPendingTask(BaseModel):
    """Currently pending task on a record — drives the "Awaiting Action"
    callout on each module's detail page."""

    id: str
    stepName: str
    taskType: str
    priority: str
    assignedToId: str
    # Every identity fragment the client needs to render
    # "Name — Designation · Role · Department · Plant" without a second
    # round-trip. Any of them can be null when the profile is incomplete;
    # clients render the gap rather than dropping the person.
    assignedToName: str | None = None
    assignedToDesignation: str | None = None
    assignedToRole: str | None = None
    assignedToDepartment: str | None = None
    assignedToPlantName: str | None = None
    assignedAt: datetime
    dueAt: datetime | None = None
    isOverdue: bool = False


class WorkflowPendingResponse(BaseModel):
    items: list[WorkflowPendingTask]
    total: int


# ─── Definition admin ────────────────────────────────────────────────────


class StepInput(BaseModel):
    id: str | None = None
    sequence: int
    stepType: str
    name: str
    approverRole: str | None = None
    approverField: str | None = None
    approverUserId: str | None = None
    approverGroupRoles: str | None = None
    slaHours: int | None = None
    slaUnit: str | None = None
    escalationRole: str | None = None
    isOptional: bool = False
    conditionExpr: str | None = None
    notes: str | None = None
    # The visual editor has no control for these two, but it MUST round-trip
    # them: update_definition() deletes and re-inserts every step, so a field
    # the editor doesn't send back is a field the next save silently erases.
    # JOINT_APPROVAL / CAPA_FAN_OUT steps would quietly collapse to ordinary
    # single-approver steps, and severity-driven SLAs would revert to slaHours.
    parallelStrategy: str | None = None
    slaBySeverity: dict[str, int] | None = None


class DefinitionCreate(BaseModel):
    module: str
    recordType: str | None = None
    name: str
    description: str | None = None
    isActive: bool = True


class DefinitionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    recordType: str | None = None
    isActive: bool | None = None
    steps: list[StepInput] | None = None
    changeNote: str | None = None


class StepOut(BaseModel):
    id: str
    sequence: int
    stepType: str
    name: str
    approverRole: str | None
    approverField: str | None
    approverUserId: str | None
    approverGroupRoles: str | None
    slaHours: int | None
    slaUnit: str | None
    escalationRole: str | None
    isOptional: bool
    conditionExpr: str | None
    notes: str | None
    # Emitted so a client can send them straight back on save — see StepInput.
    parallelStrategy: str | None = None
    slaBySeverity: dict[str, int] | None = None

    model_config = {"from_attributes": True}


class DefinitionOut(BaseModel):
    id: str
    module: str
    recordType: str | None
    name: str
    description: str | None
    isActive: bool
    createdAt: datetime
    updatedAt: datetime
    steps: list[StepOut]

    model_config = {"from_attributes": True}
