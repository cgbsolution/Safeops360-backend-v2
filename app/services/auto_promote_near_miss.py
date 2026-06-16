"""Auto-promotion path: a Near Miss with potentialSeverity = CRITICAL
on submission (or upgraded to Critical during joint review) is treated
as a High-Potential Near Miss and immediately spawned as an Incident
record in INVESTIGATION status. This is the most important capability
in the Near Miss module per the brief.

Public surface:
    promote_near_miss_to_incident(db, near_miss_id, *, actor_id) → str
        Creates the Incident, marks the NearMiss as PROMOTED, suspends
        the near-miss workflow (instance.status → 'AUTO_PROMOTED'),
        notifies Plant HSE Manager + Plant Head + Corporate HSE,
        returns the new Incident.id.

Idempotent: if the near miss already has promotedIncidentId set, the
existing incident id is returned without side-effects.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import Incident, IncidentStatus, IncidentType
from app.models.near_miss import NearMiss, NearMissStatus
from app.models.plant import Plant
from app.models.user import Role, User, UserRole
from app.models.workflow import InstanceStatus, WorkflowInstance
from app.services.notifications import send_email, send_sms


async def _next_incident_number(db: AsyncSession, plant_code: str, year: int) -> str:
    count = (await db.execute(select(WorkflowInstance.id))).scalars().all()  # placeholder
    # Number = INC-YYYY-PLANT-NNNN where NNNN is the count of incidents at this plant + 1
    from sqlalchemy import func

    last = (
        await db.execute(
            select(func.count())
            .select_from(Incident)
            .where(Incident.plantId.in_(select(Plant.id).where(Plant.code == plant_code)))
        )
    ).scalar_one()
    return f"INC-{year}-{plant_code}-{last + 1:04d}"


async def _users_with_role(db: AsyncSession, role_code: str, plant_id: str | None = None) -> list[User]:
    """Pull users that hold `role_code`. Filters to plant_id when supplied,
    falls back to all users if no plant match."""
    stmt = (
        select(User)
        .join(UserRole, UserRole.userId == User.id)
        .join(Role, Role.id == UserRole.roleId)
        .where(Role.code == role_code, Role.isActive == True)
    )
    rows = (await db.execute(stmt)).scalars().all()
    if plant_id:
        scoped = [u for u in rows if u.plantId == plant_id]
        if scoped:
            return scoped
    return list(rows)


async def _notify_critical(db: AsyncSession, *, nm: NearMiss, incident_number: str) -> None:
    """Send SMS + email to Plant HSE Manager + Plant Head + Corporate HSE."""
    plant = await db.get(Plant, nm.plantId)
    plant_name = plant.name if plant else nm.plantId

    recipients: list[User] = []
    for role in ("HSE_MANAGER", "PLANT_HEAD"):
        recipients.extend(await _users_with_role(db, role, plant_id=nm.plantId))
    recipients.extend(await _users_with_role(db, "CORPORATE_HSE"))
    # Dedupe
    seen: set[str] = set()
    unique = [u for u in recipients if not (u.id in seen or seen.add(u.id))]

    emails = [u.email for u in unique if u.email]
    # SMS not stored on User in this schema yet; pull from a hypothetical
    # designation-based number map. For now SMS is opt-in via a future
    # User.phone column — leaving as a no-op stub list.
    phones: list[str] = []

    subject = f"[SafeOps360] CRITICAL Near Miss auto-promoted to {incident_number} ({plant_name})"
    body = (
        f"A Critical-severity near miss was reported and has been auto-promoted to Incident Investigation.\n\n"
        f"Near miss number : {nm.number}\n"
        f"Plant            : {plant_name}\n"
        f"Reported on      : {nm.date.isoformat() if nm.date else '—'}\n"
        f"Description      : {nm.description[:400]}\n\n"
        f"Investigation record number: {incident_number}\n\n"
        f"Please open the incident in SafeOps360 to begin the investigation."
    )
    sms_msg = (
        f"SafeOps360: CRITICAL near miss {nm.number} at {plant_name} "
        f"auto-promoted to incident {incident_number}. Please log in."
    )

    try:
        await send_email(emails, subject, body)
    except Exception as e:  # noqa: BLE001
        print(f"[auto-promote] email failed: {e}", file=sys.stderr)
    try:
        if phones:
            await send_sms(phones, sms_msg)
    except Exception as e:  # noqa: BLE001
        print(f"[auto-promote] sms failed: {e}", file=sys.stderr)


async def promote_near_miss_to_incident(
    db: AsyncSession,
    *,
    near_miss_id: str,
    actor_id: str | None = None,
    suspend_workflow: bool = False,
) -> str | None:
    """Create the Incident record and cross-link to the near miss.

    `suspend_workflow` controls whether the near-miss workflow is closed
    when promotion fires:
      - False (default): the NM workflow continues running through its
        normal Joint Review → CAPA → Verifier → Closure path while the
        spawned Incident investigation runs in parallel. This is the
        new behaviour — the post-Joint-Review hook in workflow_engine
        and the manual-promote endpoint both use this mode.
      - True: legacy at-submission auto-promote behaviour where the
        Incident investigation fully replaces the near-miss workflow.
        No callers use this today; kept for reversibility.

    Returns the new incident.id, or the existing one if already promoted.
    Idempotent."""
    nm = await db.get(NearMiss, near_miss_id)
    if nm is None:
        return None
    if nm.promotedIncidentId:
        return nm.promotedIncidentId

    plant = await db.get(Plant, nm.plantId)
    if plant is None:
        raise ValueError(f"Plant {nm.plantId} not found for near miss {nm.number}")

    incident_number = await _next_incident_number(db, plant.code, nm.date.year)

    # Map near-miss data → incident shape. Brief says "HIGH_POTENTIAL_NEAR_MISS"
    # — closest existing IncidentType is HIPO_NEAR_MISS.
    incident = Incident(
        number=incident_number,
        date=nm.date,
        type=IncidentType.HIPO_NEAR_MISS,
        plantId=nm.plantId,
        areaId=nm.areaId,
        location=(nm.specificLocation or nm.location or "—"),
        reporterId=actor_id or nm.reporterId,
        description=f"[Auto-promoted from near miss {nm.number}]\n\n{nm.description}",
        immediateCause=nm.controlsThatFailed,
        rootCauseDetail=nm.rootCauseDetail,
        correctiveActions=nm.correctiveActions or nm.recommendedActions,
        status=IncidentStatus.INVESTIGATION,
    )
    db.add(incident)
    await db.flush()

    # Cross-link the near miss. The `promotedToIncident` boolean +
    # `promotedIncidentId` FK are the canonical "this NM was promoted"
    # signal — the UI keys off promotedToIncident, not status. We
    # intentionally do NOT touch nm.status here: the Prisma source-of-truth
    # schema only declares NearMissStatus = REPORTED | UNDER_REVIEW |
    # ACTION_ASSIGNED | CLOSED, and writing any other value blows up at
    # the Postgres enum boundary. Workflow suspension below already
    # signals to the workflow tracker that this NM no longer follows
    # the standard path.
    nm.promotedToIncident = True
    nm.promotedIncidentId = incident.id
    nm.promotedAt = datetime.now(timezone.utc)

    # Optionally suspend the near-miss workflow. By default we leave it
    # running so the NM completes its own Joint Review → CAPA → Verifier
    # → Closure path in parallel with the incident investigation. Only
    # the legacy at-submission flow asked for full takeover.
    if suspend_workflow:
        inst = (
            await db.execute(
                select(WorkflowInstance).where(
                    WorkflowInstance.module == "NEAR_MISS",
                    WorkflowInstance.recordId == nm.id,
                )
            )
        ).scalar_one_or_none()
        if inst is not None:
            inst.status = InstanceStatus.COMPLETED.value  # closest terminal state
            inst.currentStepName = "Auto-promoted to Incident"
            inst.completedAt = datetime.now(timezone.utc)
            from app.services.workflow_engine import _close_pending_tasks

            await _close_pending_tasks(db, instance_id=inst.id)

    await db.flush()

    # Best-effort notifications — never block the promotion
    try:
        await _notify_critical(db, nm=nm, incident_number=incident_number)
    except Exception as e:  # noqa: BLE001
        print(f"[auto-promote] notification fan-out failed: {e}", file=sys.stderr)

    return incident.id
