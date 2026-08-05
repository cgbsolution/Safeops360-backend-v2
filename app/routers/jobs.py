"""Job Monitor admin API (P2-1 · SC-01). System-admin only. Lists every
registered scheduler job with last-run time/status and a Run-Now trigger."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.job_run import JobRun
from app.models.user import User
from app.services import scheduler as sched
from app.services.permissions import get_user_role_codes

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

_ADMIN_ROLES = {"SYSTEM_ADMIN", "SUPER_ADMIN", "ADMIN", "PLATFORM_ADMIN"}


async def _require_admin(db: AsyncSession, user: User) -> None:
    roles = set(await get_user_role_codes(db, user.id))
    if not (roles & _ADMIN_ROLES):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Job monitor is System-Admin only.")


@router.get("")
async def list_jobs(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require_admin(db, user)
    out = []
    for jid, job in sched.JOBS.items():
        last = (
            await db.execute(select(JobRun).where(JobRun.jobId == jid).order_by(JobRun.startedAt.desc()).limit(1))
        ).scalar_one_or_none()
        out.append({
            "jobId": jid, "label": job.label, "intervalSeconds": job.interval_seconds,
            "lastRunAt": last.startedAt.isoformat() if last and last.startedAt else None,
            "lastStatus": last.status if last else "NEVER_RUN",
            "lastSummary": last.summary if last else None,
            "lastError": last.error if last else None,
            "lastRecordsAffected": last.recordsAffected if last else None,
        })
    return {"jobs": out}


@router.post("/{job_id}/run")
async def run_now(job_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require_admin(db, user)
    if job_id not in sched.JOBS:
        raise HTTPException(404, f"Unknown job {job_id}")
    return await sched.run_job(job_id, trigger="MANUAL")


@router.get("/{job_id}/history")
async def job_history(job_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require_admin(db, user)
    rows = (await db.execute(select(JobRun).where(JobRun.jobId == job_id).order_by(JobRun.startedAt.desc()).limit(50))).scalars().all()
    return {"jobId": job_id, "runs": [
        {"id": r.id, "startedAt": r.startedAt.isoformat() if r.startedAt else None, "status": r.status,
         "trigger": r.trigger, "summary": r.summary, "error": r.error, "recordsAffected": r.recordsAffected}
        for r in rows]}
