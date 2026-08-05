"""JobRun — observability for the background scheduler (P2-1). One row per job
execution (scheduled or on-demand) so admins can see when each job last ran and
whether it succeeded."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class JobRun(Base, IdMixin):
    __tablename__ = "JobRun"

    jobId: Mapped[str] = mapped_column(String, nullable=False)
    startedAt: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False, server_default=func.now())
    finishedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    status: Mapped[str] = mapped_column(String, nullable=False, default="RUNNING")  # RUNNING|SUCCESS|FAILED
    trigger: Mapped[str] = mapped_column(String, nullable=False, default="SCHEDULED")  # SCHEDULED|MANUAL|STARTUP
    recordsAffected: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=False), server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_JobRun_job_started", "jobId", "startedAt"), Index("ix_JobRun_status", "status"))


__all__ = ["JobRun"]
