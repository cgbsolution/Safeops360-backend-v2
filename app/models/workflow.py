from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin


class StepType(str, enum.Enum):
    MAKER = "MAKER"
    CHECKER = "CHECKER"
    ASSIGNEE_TASK = "ASSIGNEE_TASK"
    VERIFIER = "VERIFIER"
    CLOSURE = "CLOSURE"


class TaskType(str, enum.Enum):
    APPROVAL = "APPROVAL"
    EXECUTION = "EXECUTION"
    VERIFICATION = "VERIFICATION"


class TaskStatus(str, enum.Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    EXPIRED = "EXPIRED"


class InstanceStatus(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"


class Action(str, enum.Enum):
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    VERIFIED = "VERIFIED"
    REASSIGNED = "REASSIGNED"
    ESCALATED = "ESCALATED"
    COMMENTED = "COMMENTED"
    SUSPENDED = "SUSPENDED"


class WorkflowDefinition(Base, IdMixin):
    __tablename__ = "WorkflowDefinition"

    module: Mapped[str] = mapped_column(String, nullable=False)
    recordType: Mapped[str | None] = mapped_column(String)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    isActive: Mapped[bool] = mapped_column(Boolean, default=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )

    steps: Mapped[list[WorkflowStep]] = relationship(
        back_populates="definition",
        cascade="all, delete-orphan",
        order_by="WorkflowStep.sequence",
    )


class WorkflowStep(Base, IdMixin):
    __tablename__ = "WorkflowStep"

    definitionId: Mapped[str] = mapped_column(
        ForeignKey("WorkflowDefinition.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    stepType: Mapped[StepType] = mapped_column(Enum(StepType, name="StepType", native_enum=False), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)

    approverRole: Mapped[str | None] = mapped_column(String)
    approverField: Mapped[str | None] = mapped_column(String)
    approverUserId: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    approverGroupRoles: Mapped[str | None] = mapped_column(Text)
    slaHours: Mapped[int | None] = mapped_column(Integer)
    slaUnit: Mapped[str | None] = mapped_column(String)
    escalationRole: Mapped[str | None] = mapped_column(String)
    isOptional: Mapped[bool] = mapped_column(Boolean, default=False)
    conditionExpr: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    # Parallel-task strategy: JOINT_APPROVAL or CAPA_FAN_OUT (or null
    # for the default single-task behaviour). See workflow_engine.py.
    parallelStrategy: Mapped[str | None] = mapped_column(String)
    # Optional severity-driven SLA override: {"LOW":336,"HIGH":48,...}
    slaBySeverity: Mapped[dict | None] = mapped_column(JSON)

    definition: Mapped[WorkflowDefinition] = relationship(back_populates="steps")


class WorkflowInstance(Base, IdMixin):
    __tablename__ = "WorkflowInstance"
    __table_args__ = (
        UniqueConstraint("module", "recordId", name="uq_workflow_module_record"),
    )

    definitionId: Mapped[str] = mapped_column(ForeignKey("WorkflowDefinition.id"), nullable=False)
    module: Mapped[str] = mapped_column(String, nullable=False, index=True)
    recordId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    recordNumber: Mapped[str | None] = mapped_column(String)
    # Prisma's WorkflowInstance has no recordTitle / recordData / plantId
    # columns. Caller-supplied values for those are accepted by initiate()
    # but discarded before the row is persisted.

    initiatedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)

    # Prisma stores status as plain string (no enum type), default empty.
    status: Mapped[str] = mapped_column(String, nullable=False, default="IN_PROGRESS")
    currentStepId: Mapped[str | None] = mapped_column(String)
    currentStepName: Mapped[str | None] = mapped_column(String)

    # Prisma column is `initiatedAt` (not `startedAt`).
    initiatedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    definition: Mapped[WorkflowDefinition] = relationship()
    history: Mapped[list[WorkflowHistory]] = relationship(
        back_populates="instance", cascade="all, delete-orphan", order_by="WorkflowHistory.performedAt"
    )
    pendingTasks: Mapped[list[WorkflowTask]] = relationship(
        back_populates="instance", cascade="all, delete-orphan"
    )


class WorkflowTask(Base, IdMixin):
    __tablename__ = "WorkflowTask"

    instanceId: Mapped[str] = mapped_column(
        ForeignKey("WorkflowInstance.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stepId: Mapped[str] = mapped_column(String, nullable=False)
    stepName: Mapped[str] = mapped_column(String, nullable=False)
    taskType: Mapped[TaskType] = mapped_column(Enum(TaskType, name="TaskType", native_enum=False), nullable=False)
    module: Mapped[str] = mapped_column(String, nullable=False, index=True)
    recordId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    recordNumber: Mapped[str | None] = mapped_column(String)
    recordTitle: Mapped[str | None] = mapped_column(String)

    # Prisma marks this NOT NULL — every task must have a resolved assignee.
    # Group-queue (eligibleGroupRoles) is not in Prisma; one user owns each task.
    assignedToId: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    # Prisma stores status as plain string (no enum type).
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    assignedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    dueAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Prisma has a NOT NULL `priority` with default "NORMAL".
    priority: Mapped[str] = mapped_column(String, nullable=False, default="NORMAL")

    instance: Mapped[WorkflowInstance] = relationship(back_populates="pendingTasks")


class WorkflowHistory(Base, IdMixin):
    __tablename__ = "WorkflowHistory"

    instanceId: Mapped[str] = mapped_column(
        ForeignKey("WorkflowInstance.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stepId: Mapped[str | None] = mapped_column(String)
    stepName: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[Action] = mapped_column(Enum(Action, name="WorkflowAction", native_enum=False), nullable=False)
    performedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    comments: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[str | None] = mapped_column(Text)
    fromStatus: Mapped[str | None] = mapped_column(String)
    toStatus: Mapped[str | None] = mapped_column(String)
    performedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    instance: Mapped[WorkflowInstance] = relationship(back_populates="history")


class WorkflowDefinitionVersion(Base, IdMixin):
    __tablename__ = "WorkflowDefinitionVersion"

    definitionId: Mapped[str] = mapped_column(
        ForeignKey("WorkflowDefinition.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    # Prisma column is `editedById` (NOT NULL) and the timestamp column is
    # `editedAt` (not `createdAt`). The previous mapping was double-wrong.
    editedById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False)
    changeNote: Mapped[str | None] = mapped_column(Text)
    editedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
