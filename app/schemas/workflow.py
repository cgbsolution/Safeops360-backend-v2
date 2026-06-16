from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ApproveRequest(BaseModel):
    taskId: str
    comments: str | None = None
    attachments: list[str] | None = None
    recordData: dict[str, Any] | None = None
    plantId: str | None = None


class RejectRequest(BaseModel):
    taskId: str
    reason: str = Field(min_length=1)
    comments: str | None = None


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
    # Initiator info — surfaced by /api/workflow/tasks so the mobile inbox
    # can render "Initiated by X · Received Y" without an extra round-trip.
    initiatedById: str | None = None
    initiatedByName: str | None = None
    isOverdue: bool = False

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
    comments: str | None = None
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
    assignedToName: str | None = None
    assignedToRole: str | None = None
    assignedToDepartment: str | None = None
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
