"""Workflow engine — direct port of `src/lib/workflow/engine.ts`.

Generic Maker → Checker → Assignee → Verifier → Closure pipeline used by every
operational module. Enforces:
  • segregation of duties (assignee match)
  • RBAC triple-check (assignee + role + module permission)
  • full audit trail via WorkflowHistory rows
  • role-based assignee resolution that reads UserRole (multi-role aware)

Public surface mirrors the TS WorkflowEngine object:
  initiate(...)   — create instance + first task
  approve(...)    — checker approves
  reject(...)     — checker rejects
  submit_execution(...) — assignee task done
  verify(...)     — verifier accepts / rejects
  resubmit(...)   — initiator re-submits a rejected workflow
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import User, UserRole
from app.models.workflow import (
    Action,
    InstanceStatus,
    StepType,
    TaskStatus,
    TaskType,
    WorkflowDefinition,
    WorkflowHistory,
    WorkflowInstance,
    WorkflowStep,
    WorkflowTask,
)
from app.services.permissions import get_user_role_codes


class WorkflowError(Exception):
    """Workflow-level error. Routers convert to HTTP 400 / 403."""


# ───────────────────────────────────────────────────────────────────────────
# Assignee resolution
# ───────────────────────────────────────────────────────────────────────────


async def _find_user_by_roles(
    db: AsyncSession, role_codes: list[str], plant_id: str | None
) -> str | None:
    """Pick a user holding any of the given roles. UserRole-driven so multi-role
    users are visible regardless of which role they hold as primary."""
    if not role_codes:
        return None
    now = datetime.now(timezone.utc)
    stmt = (
        select(UserRole, User)
        .join(User, User.id == UserRole.userId)
        .where((UserRole.validTo.is_(None)) | (UserRole.validTo > now))
        .options(selectinload(UserRole.role))
        .order_by(User.createdAt)
    )
    result = await db.execute(stmt)
    rows = [(ur, u) for (ur, u) in result.all() if ur.role.code in role_codes and ur.role.isActive]
    if not rows:
        return None

    if plant_id:
        # Prefer users at the requested plant
        for ur, u in rows:
            same_plant = u.plantId == plant_id
            scope_ok = ur.scopeType is None or ur.scopeType != "PLANT" or ur.scopeValue == plant_id
            if same_plant and scope_ok:
                return u.id
        # Fall back to globally-scoped role rows
        for ur, u in rows:
            if ur.scopeType is None or ur.scopeType != "PLANT":
                return u.id
    return rows[0][1].id


# Statuses in which the assigned user can still act on a task. PENDING is the
# normal case; OVERDUE / ESCALATED are SLA states the escalation layer stamps
# ON TOP of an unfinished task — the original assignee can (and must) still
# complete the work. These are stored as free strings on WorkflowTask.status
# (only PENDING/COMPLETED/SKIPPED/EXPIRED exist on the TaskStatus enum), so we
# compare against literals here. Treating only PENDING as actionable made
# overdue/escalated tasks impossible to complete from the UI or the API.
OPEN_TASK_STATUSES = (TaskStatus.PENDING.value, "OVERDUE", "ESCALATED")


async def _close_pending_tasks(
    db: AsyncSession, *, instance_id: str, except_task_id: str | None = None
) -> int:
    """Mark all still-open (PENDING / OVERDUE / ESCALATED) tasks on an instance
    as SKIPPED (except optionally the one currently being processed). Used when
    the workflow reaches a terminal state — leftover open tasks from old bugs,
    duplicates, or SLA-escalation siblings would otherwise keep showing as
    'Awaiting Action'."""
    rows = (
        await db.execute(
            select(WorkflowTask)
            .where(WorkflowTask.instanceId == instance_id)
            .where(WorkflowTask.status.in_(OPEN_TASK_STATUSES))
        )
    ).scalars().all()
    closed = 0
    now = datetime.now(timezone.utc)
    for t in rows:
        if except_task_id and t.id == except_task_id:
            continue
        t.status = TaskStatus.SKIPPED.value
        t.completedAt = now
        closed += 1
    return closed


async def _close_escalation_siblings(
    db: AsyncSession, *, instance_id: str, step_id: str, except_task_id: str
) -> int:
    """After a step advances, close the leftover SLA-escalation 'nudge' tasks
    on the step we just left. When a task breaches its SLA, the escalation
    layer raises a parallel task (stepName prefixed '[Escalation] …') for the
    escalationRole holder. Once the primary assignee completes the work and the
    parallel gate clears, that nudge is moot — but it isn't PENDING, so the
    gate never counted it and it would otherwise dangle in the manager's inbox.

    Scoped tightly to escalation siblings (the '[Escalation] ' stepName prefix)
    so genuine parallel-approver tasks (e.g. Joint Review's two reviewers) are
    never touched — those are real PENDING tasks the gate already waits on."""
    rows = (
        await db.execute(
            select(WorkflowTask)
            .where(WorkflowTask.instanceId == instance_id)
            .where(WorkflowTask.stepId == step_id)
            .where(WorkflowTask.status.in_(OPEN_TASK_STATUSES))
            .where(WorkflowTask.stepName.like("[Escalation]%"))
        )
    ).scalars().all()
    closed = 0
    now = datetime.now(timezone.utc)
    for t in rows:
        if t.id == except_task_id:
            continue
        t.status = TaskStatus.SKIPPED.value
        t.completedAt = now
        closed += 1
    return closed


async def _enrich_record_data(
    db: AsyncSession, *, module: str, record_id: str, base: dict[str, Any]
) -> dict[str, Any]:
    """Pull the canonical owner/observer/assignee fields off the actual
    record so the next step's approverField lookup ('ACTION_OWNER',
    'RESPONSIBLE_PERSON', 'ORIGINATOR' …) resolves to the right user
    even when the caller passed a partial record_data dict.

    Caller-supplied keys win — base values aren't overwritten."""
    merged = dict(base or {})
    try:
        if module == "OBSERVATION":
            from app.models.observation import Observation

            obs = await db.get(Observation, record_id)
            if obs is not None:
                merged.setdefault("observerId", obs.observerId)
                merged.setdefault("responsiblePersonId", obs.responsiblePersonId)
                merged.setdefault("actionOwnerId", obs.responsiblePersonId)
                merged.setdefault("plantId", obs.plantId)
                merged.setdefault("severity", obs.severity.value if hasattr(obs.severity, "value") else obs.severity)
                merged.setdefault("type", obs.type.value if hasattr(obs.type, "value") else obs.type)
                merged.setdefault("category", obs.category.value if hasattr(obs.category, "value") else obs.category)
        elif module == "INCIDENT":
            from app.models.incident import Incident

            inc = await db.get(Incident, record_id)
            if inc is not None:
                merged.setdefault("reporterId", inc.reporterId)
                merged.setdefault("plantId", inc.plantId)
        elif module == "NEAR_MISS":
            from app.models.near_miss import NearMiss

            nm = await db.get(NearMiss, record_id)
            if nm is not None:
                merged.setdefault("reporterId", nm.reporterId)
                merged.setdefault("actionOwnerId", nm.actionOwnerId)
                merged.setdefault("responsiblePersonId", nm.actionOwnerId)
                merged.setdefault("plantId", nm.plantId)
        # Other modules can be added as their workflows wire up. Falling
        # through with the base dict is safe — we just lose the safety net.
    except Exception:
        # Enrichment is best-effort; never let it block task creation.
        pass
    return merged


async def _resolve_assignee(
    db: AsyncSession,
    *,
    approver_role: str | None,
    approver_field: str | None,
    approver_user_id: str | None,
    approver_group_roles: str | None,
    record_data: dict[str, Any],
    initiator_id: str,
    plant_id: str | None,
) -> str | None:
    """Maps a step's approver* fields to a concrete user id."""
    if approver_user_id:
        return approver_user_id

    if approver_field:
        field_val = record_data.get(approver_field) or record_data.get(approver_field.lower())
        if isinstance(field_val, str):
            return field_val
        if approver_field == "ORIGINATOR":
            return (
                record_data.get("observerId")
                or record_data.get("reporterId")
                or record_data.get("originatorId")
                or initiator_id
            )
        if approver_field == "ACTION_OWNER":
            return record_data.get("actionOwnerId") or record_data.get("responsiblePersonId")
        if approver_field == "RESPONSIBLE_PERSON":
            return record_data.get("responsiblePersonId") or record_data.get("actionOwnerId")
        if approver_field == "INVESTIGATION_LEAD":
            # Maps to the Incident.investigationTeamLead column. Falls back
            # to actionOwnerId so the task still has a valid assignee even
            # when the lead hasn't been picked yet during classification.
            return (
                record_data.get("investigationTeamLead")
                or record_data.get("investigationLeadId")
                or record_data.get("actionOwnerId")
            )
        if approver_field == "ASSIGNED_INSPECTOR":
            return record_data.get("inspectorId")
        if approver_field == "RECEIVER":
            return record_data.get("receiverId")
        if approver_field == "ISSUER":
            return record_data.get("issuerId")
        if approver_field == "TRAINER":
            return record_data.get("trainerId")

    if approver_group_roles:
        try:
            roles = json.loads(approver_group_roles)
            if isinstance(roles, list) and roles:
                user_id = await _find_user_by_roles(db, roles, plant_id)
                if user_id:
                    return user_id
        except (json.JSONDecodeError, TypeError):
            pass

    if approver_role:
        return await _find_user_by_roles(db, [approver_role], plant_id)

    return None


# ───────────────────────────────────────────────────────────────────────────
# Condition evaluation (port of evaluateCondition)
# ───────────────────────────────────────────────────────────────────────────


def _evaluate_rule(rule: dict, record_data: dict) -> bool:
    actual = record_data.get(rule.get("field"))
    op = rule.get("operator")
    value = rule.get("value")
    if op == "=":
        return actual == value
    if op == "!=":
        return actual != value
    if op == "in":
        items = value if isinstance(value, list) else [v.strip() for v in str(value).split(",")]
        return actual in items
    if op == "not_in":
        items = value if isinstance(value, list) else [v.strip() for v in str(value).split(",")]
        return actual not in items
    if op == ">":
        try:
            return float(actual) > float(value)
        except (TypeError, ValueError):
            return False
    if op == "<":
        try:
            return float(actual) < float(value)
        except (TypeError, ValueError):
            return False
    return False


def _evaluate_condition(expr: str | None, record_data: dict) -> bool:
    if not expr:
        return True
    try:
        cond = json.loads(expr)
    except (json.JSONDecodeError, TypeError):
        return True
    if isinstance(cond, dict) and cond.get("version") == 2:
        rules = cond.get("rules") or []
        if not rules:
            return True
        results = [_evaluate_rule(r, record_data) for r in rules]
        return any(results) if cond.get("combinator") == "OR" else all(results)
    # v1 legacy: { field: value | array } — keys ANDed
    if isinstance(cond, dict):
        for field, expected in cond.items():
            actual = record_data.get(field)
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True
    return True


def _find_next_applicable_step(
    steps: list[WorkflowStep], from_sequence: int, record_data: dict
) -> WorkflowStep | None:
    for s in steps:
        if s.sequence <= from_sequence:
            continue
        if not _evaluate_condition(s.conditionExpr, record_data):
            continue
        return s
    return None


def _calc_due_at(step: WorkflowStep) -> datetime | None:
    if not step.slaHours:
        return None
    return datetime.now(timezone.utc) + timedelta(hours=step.slaHours)


# ───────────────────────────────────────────────────────────────────────────
# RBAC triple-check — gates every transition
# ───────────────────────────────────────────────────────────────────────────


async def _rbac_gate(
    db: AsyncSession,
    *,
    task: WorkflowTask,
    step: WorkflowStep,
    user_id: str,
    action: str,  # APPROVE | EXECUTE | VERIFY | REJECT
) -> None:
    # 1. Assignee — direct match. Group-queue match is disabled because the
    # eligibleGroupRoles column isn't in Prisma's schema; task.assignedToId
    # is always populated by resolve_assignee().
    if task.assignedToId != user_id:
        raise WorkflowError("Not your task")

    # 2. Role — the step's required role must be one the user holds
    if step.approverRole:
        user_roles = await get_user_role_codes(db, user_id)
        if step.approverRole not in user_roles:
            raise WorkflowError(f"This step requires the '{step.approverRole}' role.")

    # 3. Permission — verify the user holds the module action grant at ALL
    # (any scope). We deliberately skip the scope check here: the assignee
    # match in (1) + role match in (2) already prove the user is the
    # legitimate actor for this specific record. Re-enforcing OWN_PLANT /
    # OWN_DEPARTMENT scope at this point would require loading the record
    # and inferring its scope-context for every module — and would
    # incorrectly deny assignees whose role grants the action only with a
    # narrower scope (e.g. SUPERVISOR has OBSERVATION.APPROVE under
    # OWN_DEPARTMENT, but they were specifically picked for this task).
    perm_action = "APPROVE" if action == "REJECT" else action
    perm_code = f"{task.module}.{perm_action}"
    rows = await _user_permission_codes(db, user_id)
    if perm_code not in rows:
        raise WorkflowError(f"Missing permission '{perm_code}'.")


async def _user_permission_codes(db: AsyncSession, user_id: str) -> set[str]:
    from app.services.permissions import _load_user_permissions

    rows = await _load_user_permissions(db, user_id)
    return {r.permission_code for r in rows}


# ───────────────────────────────────────────────────────────────────────────
# Status sync — keeps record-level status enums in lockstep with workflow state
# ───────────────────────────────────────────────────────────────────────────


async def _sync_record_status(
    db: AsyncSession,
    *,
    module: str,
    record_id: str,
    next_step_type: StepType | None,
    instance_completed: bool,
) -> None:
    """Mirror of TS syncRecordStatus(). Module-specific status mapping kept here
    to centralise the rules. Only PTW has a non-trivial status enum that the
    engine drives; other modules use generic statuses that are status-set by
    the route handlers themselves."""
    if module == "PTW":
        from app.models.permit import Permit, PermitStatus

        permit = await db.get(Permit, record_id)
        if permit is None or permit.status in {PermitStatus.SUSPENDED, PermitStatus.EXPIRED, PermitStatus.REJECTED}:
            return

        if instance_completed:
            permit.status = PermitStatus.CLOSED
            permit.closedAt = datetime.now(timezone.utc)
        elif next_step_type in (StepType.ASSIGNEE_TASK.value, StepType.CLOSURE.value):
            permit.status = PermitStatus.ACTIVE
        else:
            permit.status = PermitStatus.SUBMITTED
        await db.flush()

        # Post-closure cross-module triggers (Dimension 4). Each rule is
        # wrapped in a SAVEPOINT inside the engine — failures must NEVER
        # block the closure flow. Outer SAVEPOINT here in case any
        # downstream import or SQL raises before the engine catches it.
        if instance_completed:
            try:
                async with db.begin_nested():
                    from app.services.ptw_post_closure import run_ptw_post_closure_rules

                    await run_ptw_post_closure_rules(db, permit_id=record_id)
            except Exception as e:  # noqa: BLE001
                import sys

                print(f"[post-closure] PTW {record_id}: {e}", file=sys.stderr)
    elif module == "OBSERVATION" and instance_completed:
        from app.models.observation import Observation, ObservationStatus

        obs = await db.get(Observation, record_id)
        if obs is not None and obs.status != ObservationStatus.CLOSED:
            obs.status = ObservationStatus.CLOSED
            obs.closedAt = datetime.now(timezone.utc)
            await db.flush()

        # Post-closure cross-module triggers (Dimension 4). Today this is
        # just the LessonsDistributionAgent (Anthropic) — additional rules
        # (focused inspection on repeats, contractor score, PPE trend etc.)
        # can be ported from src/lib/observation/post-closure-rules.ts.
        # Each trigger is best-effort; failures must NEVER block closure.
        # SAVEPOINT so an internal flush failure doesn't poison the main
        # workflow transaction.
        try:
            async with db.begin_nested():
                from app.services.post_closure_rules import run_post_closure_rules

                await run_post_closure_rules(db, observation_id=record_id)
        except Exception as e:  # noqa: BLE001
            import sys
            print(f"[post-closure] OBSERVATION {record_id}: {e}", file=sys.stderr)
    elif module == "NEAR_MISS" and instance_completed:
        # Mirror of the OBSERVATION closure path, plus the 10-rule near
        # miss engine (Dimension 4 of the Near Miss refactor brief).
        from app.models.near_miss import NearMiss, NearMissStatus

        nm = await db.get(NearMiss, record_id)
        if nm is not None and nm.status != NearMissStatus.CLOSED:
            nm.status = NearMissStatus.CLOSED
            nm.closedAt = datetime.now(timezone.utc)
            nm.slaActualClosedAt = nm.closedAt
            # SLA performance string — populated for analytics + sidebar display.
            # Defensively coerce both sides to UTC-aware before subtracting:
            # `nm.closedAt` is aware (we just set it via datetime.now(tz=utc)),
            # but `nm.slaTargetAt` may come back from older rows as naive
            # depending on how the value was originally written. A naive ↔
            # aware subtraction raises TypeError, so normalise first.
            if nm.slaTargetAt:
                target = nm.slaTargetAt
                closed = nm.closedAt
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                if closed.tzinfo is None:
                    closed = closed.replace(tzinfo=timezone.utc)
                delta_h = (target - closed).total_seconds() / 3600.0
                nm.slaPerformance = (
                    f"On time ({int(delta_h)}h spare)" if delta_h >= 0
                    else f"Late by {int(abs(delta_h))}h"
                )
            await db.flush()

        # Run the 10 post-closure rules. Best-effort + savepoint, same
        # pattern as observation.
        try:
            async with db.begin_nested():
                from app.services.post_closure_rules_nm import run_near_miss_post_closure_rules

                await run_near_miss_post_closure_rules(db, near_miss_id=record_id)
        except Exception as e:  # noqa: BLE001
            import sys
            print(f"[post-closure] NEAR_MISS {record_id}: {e}", file=sys.stderr)
    elif module == "INCIDENT" and instance_completed:
        # Mark the incident CLOSED + run the post-closure rules engine
        # (contractor score, observation cross-link, lessons distribution,
        # equipment re-inspection, 90-day effectiveness review scheduling).
        from app.models.incident import Incident as _Inc, IncidentStatus as _IncStatus

        inc = await db.get(_Inc, record_id)
        if inc is not None and inc.status != _IncStatus.CLOSED:
            inc.status = _IncStatus.CLOSED
            inc.closedAt = datetime.now(timezone.utc)
            await db.flush()

        try:
            async with db.begin_nested():
                from app.services.incident_post_closure import (
                    run_incident_post_closure_rules,
                )

                await run_incident_post_closure_rules(db, incident_id=record_id)
        except Exception as e:  # noqa: BLE001
            import sys
            print(f"[post-closure] INCIDENT {record_id}: {e}", file=sys.stderr)


# ───────────────────────────────────────────────────────────────────────────
# Task creation
# ───────────────────────────────────────────────────────────────────────────


async def _create_task_for_step(
    db: AsyncSession,
    *,
    instance: WorkflowInstance,
    step: WorkflowStep,
    record_data: dict[str, Any],
    record_number: str | None,
    record_title: str | None,
    module: str,
    record_id: str,
    initiator_id: str,
    plant_id: str | None,
) -> WorkflowTask | None:
    """Single-task convenience wrapper that returns the first task created
    by `_create_tasks_for_step`. Use that helper directly when you need
    the full list (e.g. parallel-strategy steps)."""
    tasks = await _create_tasks_for_step(
        db,
        instance=instance,
        step=step,
        record_data=record_data,
        record_number=record_number,
        record_title=record_title,
        module=module,
        record_id=record_id,
        initiator_id=initiator_id,
        plant_id=plant_id,
    )
    return tasks[0] if tasks else None


async def _create_tasks_for_step(
    db: AsyncSession,
    *,
    instance: WorkflowInstance,
    step: WorkflowStep,
    record_data: dict[str, Any],
    record_number: str | None,
    record_title: str | None,
    module: str,
    record_id: str,
    initiator_id: str,
    plant_id: str | None,
) -> list[WorkflowTask]:
    """Create one or more PENDING WorkflowTask rows for this step.

    Returns a list because steps with `parallelStrategy` set fan out into
    multiple tasks (joint approval × N reviewers, or one execution task
    per NearMissCapa). Single-task steps return a 1-element list. MAKER
    and unsupported step types return [].
    """
    step_type = step.stepType.value if hasattr(step.stepType, "value") else str(step.stepType)

    if step_type == StepType.MAKER.value:
        return []

    task_type_map = {
        StepType.CHECKER.value: TaskType.APPROVAL.value,
        StepType.ASSIGNEE_TASK.value: TaskType.EXECUTION.value,
        StepType.VERIFIER.value: TaskType.VERIFICATION.value,
        StepType.CLOSURE.value: TaskType.APPROVAL.value,
    }
    task_type = task_type_map.get(step_type)
    if task_type is None:
        return []

    enriched = await _enrich_record_data(db, module=module, record_id=record_id, base=record_data)

    # ── Severity-driven SLA override (slaBySeverity wins over slaHours) ──
    sla_hours = _resolve_sla_hours(step, enriched)

    # ── Parallel strategies ────────────────────────────────────────────
    strategy = step.parallelStrategy or None

    assignees: list[str] = []

    if strategy == "JOINT_APPROVAL" and step.approverGroupRoles:
        # One task per role in the group, all must complete to advance.
        roles = _parse_group_roles(step.approverGroupRoles)
        seen: set[str] = set()
        for role in roles:
            uid = await _find_user_by_roles(db, [role], plant_id)
            if uid and uid not in seen:
                assignees.append(uid)
                seen.add(uid)
        if not assignees:
            assignees = [initiator_id]

    elif strategy == "CAPA_FAN_OUT" and module == "NEAR_MISS":
        # One task per NearMissCapa row attached to this near miss. Each
        # CAPA owner gets their own EXECUTION task; the parent step
        # advances only when ALL CAPAs are completed.
        from app.models.near_miss_children import NearMissCapa

        capa_rows = (
            await db.execute(
                select(NearMissCapa).where(NearMissCapa.nearMissId == record_id)
            )
        ).scalars().all()
        for capa in capa_rows:
            if capa.ownerId:
                assignees.append(capa.ownerId)
        if not assignees:
            # No CAPAs defined — fall back to single task assigned to
            # the suggested action owner / initiator. The reviewer will
            # add CAPAs at the previous step normally.
            assignees = [initiator_id]

    elif strategy == "CAPA_ACTION_FAN_OUT" and module == "CAPA":
        # One EXECUTION task per CapaAction row attached to this CAPA. Each
        # action owner gets their own task; the workflow step advances only
        # when ALL actions are completed. Mirrors NEAR_MISS CAPA_FAN_OUT but
        # against the unified Capa table.
        from app.models.capa import CapaAction

        action_rows = (
            await db.execute(
                select(CapaAction).where(CapaAction.capaId == record_id)
            )
        ).scalars().all()
        for action in action_rows:
            if action.ownerUserId:
                assignees.append(action.ownerUserId)
        if not assignees:
            # No actions defined yet — route to initiator so they go back to
            # the previous step and add actions. Common when CAPA was
            # submitted without planning fully.
            assignees = [initiator_id]

    elif strategy == "HIRA_TEAM_FAN_OUT" and module == "HIRA_STUDY":
        # One APPROVAL task per HiraStudyTeamMember. The study cannot
        # advance to Plant Head approval until every team member has
        # signed off (matches spec §3.1 step 4: "every team member has
        # signed"). The team leader gets the first task; other members
        # get parallel sign-off tasks.
        from app.models.hira import HiraStudy, HiraStudyTeamMember

        study = await db.get(HiraStudy, record_id)
        if study is not None:
            assignees.append(study.teamLeaderId)
        member_rows = (
            await db.execute(
                select(HiraStudyTeamMember).where(HiraStudyTeamMember.studyId == record_id)
            )
        ).scalars().all()
        seen = {study.teamLeaderId} if study is not None else set()
        for m in member_rows:
            if m.userId and m.userId not in seen:
                assignees.append(m.userId)
                seen.add(m.userId)
        if not assignees:
            # Edge case: no team rows yet. Route to initiator so the
            # workflow doesn't dead-end; the initiator will populate the
            # team before re-submitting.
            assignees = [initiator_id]

    else:
        # Default single-task path
        assignee = await _resolve_assignee(
            db,
            approver_role=step.approverRole,
            approver_field=step.approverField,
            approver_user_id=step.approverUserId,
            approver_group_roles=step.approverGroupRoles,
            record_data=enriched,
            initiator_id=initiator_id,
            plant_id=plant_id,
        )
        assignees = [assignee or initiator_id]

    # Build tasks
    due_at = (
        datetime.now(timezone.utc) + timedelta(hours=sla_hours) if sla_hours else None
    )
    tasks: list[WorkflowTask] = []
    for uid in assignees:
        t = WorkflowTask(
            instanceId=instance.id,
            stepId=step.id,
            stepName=step.name,
            taskType=task_type,
            module=module,
            recordId=record_id,
            recordNumber=record_number,
            recordTitle=record_title,
            assignedToId=uid,
            status=TaskStatus.PENDING.value,
            dueAt=due_at,
        )
        db.add(t)
        tasks.append(t)
    await db.flush()
    return tasks


def _parse_group_roles(raw: str) -> list[str]:
    """approverGroupRoles is stored as either a JSON array string or a
    comma-separated list. Accept both."""
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except (json.JSONDecodeError, TypeError):
            pass
    return [r.strip() for r in raw.split(",") if r.strip()]


def _resolve_sla_hours(step: WorkflowStep, record_data: dict[str, Any]) -> int | None:
    """Pick the SLA in hours for this step. `slaBySeverity` (JSON map)
    wins over `slaHours`, keyed by record_data['potentialSeverity'] |
    record_data['severity']."""
    by_sev = step.slaBySeverity or None
    if isinstance(by_sev, dict):
        sev = (
            record_data.get("potentialSeverity")
            or record_data.get("severity")
        )
        if isinstance(sev, str):
            sev = sev.upper()
        if sev and sev in by_sev:
            try:
                return int(by_sev[sev])
            except (TypeError, ValueError):
                pass
    return step.slaHours


# ───────────────────────────────────────────────────────────────────────────
# Public engine surface
# ───────────────────────────────────────────────────────────────────────────


async def initiate(
    db: AsyncSession,
    *,
    module: str,
    record_id: str,
    record_number: str | None,
    record_title: str | None,
    record_data: dict[str, Any],
    initiator_id: str,
    plant_id: str | None,
) -> WorkflowInstance:
    """Find the active definition for `module` (+optional recordType filter via
    record_data['type']) and create the WorkflowInstance + first task."""
    record_type = record_data.get("type")
    stmt = (
        select(WorkflowDefinition)
        .where(WorkflowDefinition.module == module, WorkflowDefinition.isActive == True)
        .options(selectinload(WorkflowDefinition.steps))
    )
    candidates = (await db.execute(stmt)).scalars().all()
    # Prefer recordType match, else fall back to default (recordType is null)
    definition = next((d for d in candidates if d.recordType == record_type), None)
    if definition is None:
        definition = next((d for d in candidates if d.recordType is None), None)
    if definition is None:
        raise WorkflowError(f"No active workflow definition found for module {module}")

    # Sort steps once
    steps = sorted(definition.steps, key=lambda s: s.sequence)
    maker = next((s for s in steps if s.stepType == StepType.MAKER.value), None)
    if maker is None:
        raise WorkflowError("Workflow has no MAKER step")

    next_step = _find_next_applicable_step(steps, maker.sequence, record_data)
    if next_step is None:
        raise WorkflowError("Workflow has no executable step after Maker")

    # Prisma schema has no recordTitle / recordData / plantId on
    # WorkflowInstance — they used to be Python-only fields. Drop them at
    # write time; callers that still pass them are no-ops.
    instance = WorkflowInstance(
        definitionId=definition.id,
        module=module,
        recordId=record_id,
        recordNumber=record_number,
        initiatedById=initiator_id,
        status=InstanceStatus.IN_PROGRESS.value,
        currentStepId=next_step.id,
        currentStepName=next_step.name,
    )
    db.add(instance)
    await db.flush()

    db.add(
        WorkflowHistory(
            instanceId=instance.id,
            stepId=maker.id,
            stepName=maker.name,
            action=Action.SUBMITTED.value,
            performedById=initiator_id,
        )
    )

    await _create_task_for_step(
        db,
        instance=instance,
        step=next_step,
        record_data=record_data,
        record_number=record_number,
        record_title=record_title,
        module=module,
        record_id=record_id,
        initiator_id=initiator_id,
        plant_id=plant_id,
    )
    await _sync_record_status(
        db,
        module=module,
        record_id=record_id,
        next_step_type=next_step.stepType,
        instance_completed=False,
    )
    return instance


async def _load_task_with_definition(db: AsyncSession, task_id: str) -> tuple[WorkflowTask, WorkflowInstance, list[WorkflowStep]]:
    # Use an explicit `select()` with `.execution_options(populate_existing=True)`
    # so the eager-load options ALWAYS apply — even if the WorkflowTask is
    # already in the identity map without its `instance`/`definition`/`steps`
    # relations loaded (which happens when an earlier db.get(WorkflowTask, ...)
    # without options ran in the same session). `db.get()` would silently
    # return the cached instance and ignore the options → lazy load on
    # `task.instance` → MissingGreenlet under async.
    stmt = (
        select(WorkflowTask)
        .where(WorkflowTask.id == task_id)
        .options(
            selectinload(WorkflowTask.instance)
            .selectinload(WorkflowInstance.definition)
            .selectinload(WorkflowDefinition.steps)
        )
        .execution_options(populate_existing=True)
    )
    task = (await db.execute(stmt)).scalar_one_or_none()
    if task is None:
        raise WorkflowError("Task not found")
    instance = task.instance
    steps = sorted(instance.definition.steps, key=lambda s: s.sequence)
    return task, instance, steps


async def _advance(
    db: AsyncSession,
    *,
    task: WorkflowTask,
    instance: WorkflowInstance,
    steps: list[WorkflowStep],
    user_id: str,
    action: Action,
    comments: str | None,
    attachments: list[str] | None,
    record_data: dict[str, Any],
    plant_id: str | None,
) -> dict[str, Any]:
    current_step = next((s for s in steps if s.id == task.stepId), None)
    if current_step is None:
        raise WorkflowError("Step missing")

    # Mark this specific task complete and record history first.
    task.status = TaskStatus.COMPLETED.value
    task.completedAt = datetime.now(timezone.utc)
    db.add(
        WorkflowHistory(
            instanceId=task.instanceId,
            stepId=current_step.id,
            stepName=current_step.name,
            action=action,
            performedById=user_id,
            comments=comments,
            attachments=json.dumps(attachments) if attachments else None,
        )
    )
    await db.flush()

    # Parallel-step gate: if other PENDING tasks remain on the SAME step
    # (joint-approval reviewers still working, other CAPA owners not yet
    # done), don't advance the instance. Just record this completion and
    # return — the workflow waits for the rest.
    remaining_stmt = (
        select(func.count())
        .select_from(WorkflowTask)
        .where(WorkflowTask.instanceId == instance.id)
        .where(WorkflowTask.stepId == current_step.id)
        .where(WorkflowTask.status == TaskStatus.PENDING.value)
    )
    remaining = (await db.execute(remaining_stmt)).scalar_one()
    if remaining > 0:
        return {
            "ok": True,
            "waitingFor": remaining,
            "stepName": current_step.name,
            "advancedTo": None,
        }

    # Gate cleared — no PENDING work remains on this step. Retire any leftover
    # SLA-escalation nudge tasks raised against this step so they don't linger
    # in the escalation-role holder's inbox after the work is finished.
    await _close_escalation_siblings(
        db, instance_id=instance.id, step_id=current_step.id, except_task_id=task.id
    )

    # All tasks on this step are done — advance the instance to the next
    # applicable step.
    next_step = _find_next_applicable_step(steps, current_step.sequence, record_data)

    if next_step:
        instance.currentStepId = next_step.id
        instance.currentStepName = next_step.name
    else:
        instance.currentStepId = None
        instance.currentStepName = "Completed"
        instance.status = InstanceStatus.COMPLETED.value
        instance.completedAt = datetime.now(timezone.utc)
        await _close_pending_tasks(db, instance_id=instance.id, except_task_id=task.id)
    await db.flush()

    if next_step:
        await _create_task_for_step(
            db,
            instance=instance,
            step=next_step,
            record_data=record_data,
            record_number=task.recordNumber,
            record_title=task.recordTitle,
            module=task.module,
            record_id=task.recordId,
            initiator_id=instance.initiatedById,
            plant_id=plant_id,
        )

    await _sync_record_status(
        db,
        module=task.module,
        record_id=task.recordId,
        next_step_type=next_step.stepType if next_step else None,
        instance_completed=next_step is None,
    )

    # Post-step hooks. Anything that needs to run AFTER a specific step
    # finishes (across all approvers, parallel-step gate cleared) goes
    # here. Today we have one: auto-promote a CRITICAL near miss to an
    # Incident the moment Joint Review approves. The NM workflow keeps
    # running in parallel with the spawned investigation.
    if (
        task.module == "NEAR_MISS"
        and current_step.name == "Joint Review"
        and action == Action.APPROVED.value
    ):
        try:
            from app.models.near_miss import NearMiss
            from app.models.observation import Severity

            nm = await db.get(NearMiss, task.recordId)
            if (
                nm is not None
                and nm.potentialSeverity == Severity.CRITICAL
                and not nm.promotedIncidentId
            ):
                from app.services.auto_promote_near_miss import (
                    promote_near_miss_to_incident,
                )

                await promote_near_miss_to_incident(
                    db,
                    near_miss_id=nm.id,
                    actor_id=user_id,
                    suspend_workflow=False,  # NM continues; Incident parallel
                )
        except Exception as e:  # noqa: BLE001
            # Never block the approval over a promotion failure. Log + carry
            # on; an admin can backfill via the manual-promote endpoint.
            import sys as _sys
            import traceback as _tb

            print(
                f"[workflow] post-Joint-Review auto-promote failed: {e}",
                file=_sys.stderr,
            )
            _tb.print_exc(file=_sys.stderr)

    return {"ok": True, "advancedTo": next_step.name if next_step else "Completed"}


async def approve(
    db: AsyncSession,
    *,
    task_id: str,
    user_id: str,
    comments: str | None = None,
    attachments: list[str] | None = None,
    record_data: dict[str, Any] | None = None,
    plant_id: str | None = None,
) -> dict[str, Any]:
    task, instance, steps = await _load_task_with_definition(db, task_id)
    if task.status not in OPEN_TASK_STATUSES:
        raise WorkflowError("Task is not open")
    current_step = next((s for s in steps if s.id == task.stepId), None)
    if current_step is None:
        raise WorkflowError("Step missing")
    await _rbac_gate(db, task=task, step=current_step, user_id=user_id, action="APPROVE")

    # PTW closure gate: a permit cannot reach CLOSED until the receiver has
    # returned the permit AND a site-verification has been recorded. Closing
    # remark goes onto Permit.closingRemark via _sync_record_status.
    if task.module == "PTW" and current_step.stepType == StepType.CLOSURE.value:
        from app.models.permit import Permit as PermitModel

        permit_row = await db.get(PermitModel, task.recordId)
        if permit_row is None:
            raise WorkflowError("Permit not found for closure")
        if permit_row.returnedAt is None:
            raise WorkflowError(
                "Receiver must return the permit before closure. "
                "Open the permit detail page → Return panel."
            )
        if permit_row.siteVerifiedAt is None:
            raise WorkflowError(
                "Site verification is required before closure. "
                "Open the permit detail page → Site Verification panel."
            )
        if not (comments and comments.strip()):
            raise WorkflowError("A closing remark is required.")
        # Persist the closing remark + closer onto the permit row so the
        # detail view + post-closure rules engine can read them.
        permit_row.closedById = user_id
        permit_row.closingRemark = comments.strip()
        await db.flush()

    return await _advance(
        db,
        task=task,
        instance=instance,
        steps=steps,
        user_id=user_id,
        action=Action.APPROVED.value,
        comments=comments,
        attachments=attachments,
        record_data=record_data or {},
        plant_id=plant_id,
    )


async def reject(
    db: AsyncSession,
    *,
    task_id: str,
    user_id: str,
    reason: str,
    comments: str | None = None,
) -> dict[str, Any]:
    task, instance, steps = await _load_task_with_definition(db, task_id)
    if task.status not in OPEN_TASK_STATUSES:
        raise WorkflowError("Task is not open")
    current_step = next((s for s in steps if s.id == task.stepId), None)
    if current_step is None:
        raise WorkflowError("Step missing")
    await _rbac_gate(db, task=task, step=current_step, user_id=user_id, action="REJECT")

    task.status = TaskStatus.COMPLETED.value
    task.completedAt = datetime.now(timezone.utc)
    instance.status = InstanceStatus.REJECTED.value
    instance.currentStepName = "Rejected — rework required"
    db.add(
        WorkflowHistory(
            instanceId=task.instanceId,
            stepId=task.stepId,
            stepName=task.stepName,
            action=Action.REJECTED.value,
            performedById=user_id,
            comments=f"{reason}\n\n{comments}" if comments else reason,
            fromStatus=InstanceStatus.IN_PROGRESS.value,
            toStatus=InstanceStatus.REJECTED.value,
        )
    )
    await _close_pending_tasks(db, instance_id=instance.id, except_task_id=task.id)
    await db.flush()
    return {"ok": True, "status": InstanceStatus.REJECTED.value}


async def submit_execution(
    db: AsyncSession,
    *,
    task_id: str,
    user_id: str,
    execution_data: dict[str, Any] | None = None,
    comments: str | None = None,
    attachments: list[str] | None = None,
    record_data: dict[str, Any] | None = None,
    plant_id: str | None = None,
) -> dict[str, Any]:
    task, instance, steps = await _load_task_with_definition(db, task_id)
    if task.status not in OPEN_TASK_STATUSES:
        raise WorkflowError("Task is not open")
    current_step = next((s for s in steps if s.id == task.stepId), None)
    if current_step is None:
        raise WorkflowError("Step missing")
    await _rbac_gate(db, task=task, step=current_step, user_id=user_id, action="EXECUTE")

    # PTW activation gate: full pre-flight before transitioning out of the
    # receiver step. Blocks on FLRA, crew validity, isolations, expiry, and
    # any explicit suspension. The full blocker list is surfaced so the user
    # can fix all of them in one round-trip.
    if task.module == "PTW" and current_step.stepType == StepType.ASSIGNEE_TASK.value:
        from app.services.ptw_activation_gate import can_ptw_transition_to_active

        gate = await can_ptw_transition_to_active(db, task.recordId)
        if not gate.ok:
            messages = [b.message for b in gate.blockers]
            raise WorkflowError(
                "Cannot activate permit:\n• " + "\n• ".join(messages)
                if messages
                else "Activation gate is closed for this permit."
            )

    merged = {**(record_data or {}), **(execution_data or {})}
    return await _advance(
        db,
        task=task,
        instance=instance,
        steps=steps,
        user_id=user_id,
        action=Action.EXECUTED.value,
        comments=comments,
        attachments=attachments,
        record_data=merged,
        plant_id=plant_id,
    )


async def verify(
    db: AsyncSession,
    *,
    task_id: str,
    user_id: str,
    accepted: bool,
    comments: str | None = None,
    record_data: dict[str, Any] | None = None,
    plant_id: str | None = None,
) -> dict[str, Any]:
    task, instance, steps = await _load_task_with_definition(db, task_id)
    if task.status not in OPEN_TASK_STATUSES:
        raise WorkflowError("Task is not open")
    current_step = next((s for s in steps if s.id == task.stepId), None)
    if current_step is None:
        raise WorkflowError("Step missing")
    await _rbac_gate(db, task=task, step=current_step, user_id=user_id, action="VERIFY")

    if not accepted:
        # Verifier rejection — send back to the most recent ASSIGNEE_TASK
        # step so the action owner can redo the work. A new PENDING task
        # is created so the detail page shows an "Awaiting Action" entry
        # with the action owner. If no ASSIGNEE_TASK exists in this
        # workflow, fall through to the legacy "REJECTED back to MAKER"
        # behaviour (resubmit panel).
        task.status = TaskStatus.COMPLETED.value
        task.completedAt = datetime.now(timezone.utc)
        db.add(
            WorkflowHistory(
                instanceId=task.instanceId,
                stepId=task.stepId,
                stepName=task.stepName,
                action=Action.REJECTED.value,
                performedById=user_id,
                comments=comments,
                fromStatus=InstanceStatus.IN_PROGRESS.value,
                toStatus=InstanceStatus.IN_PROGRESS.value,
            )
        )

        rework_step = next(
            (
                s
                for s in reversed([x for x in steps if x.sequence < current_step.sequence])
                if (s.stepType.value if hasattr(s.stepType, "value") else s.stepType) == StepType.ASSIGNEE_TASK.value
            ),
            None,
        )
        if rework_step is not None:
            instance.currentStepId = rework_step.id
            instance.currentStepName = f"Rework — {rework_step.name}"
            # status stays IN_PROGRESS so the resubmit panel doesn't appear
            await db.flush()
            await _create_task_for_step(
                db,
                instance=instance,
                step=rework_step,
                record_data=record_data or {},
                record_number=task.recordNumber,
                record_title=task.recordTitle,
                module=task.module,
                record_id=task.recordId,
                initiator_id=instance.initiatedById,
                plant_id=plant_id,
            )
            return {"ok": True, "status": "REWORK", "sentTo": rework_step.name}

        # Legacy fallback: workflow has no assignee step → flag REJECTED
        instance.status = InstanceStatus.REJECTED.value
        instance.currentStepName = "Verification rejected — rework required"
        await db.flush()
        return {"ok": True, "status": InstanceStatus.REJECTED.value}

    return await _advance(
        db,
        task=task,
        instance=instance,
        steps=steps,
        user_id=user_id,
        action=Action.VERIFIED.value,
        comments=comments,
        attachments=None,
        record_data=record_data or {},
        plant_id=plant_id,
    )


async def resubmit(
    db: AsyncSession,
    *,
    instance_id: str,
    user_id: str,
    comments: str | None = None,
    record_data: dict[str, Any] | None = None,
    plant_id: str | None = None,
) -> dict[str, Any]:
    instance = await db.get(
        WorkflowInstance,
        instance_id,
        options=[selectinload(WorkflowInstance.definition).selectinload(WorkflowDefinition.steps)],
    )
    if instance is None:
        raise WorkflowError("Workflow instance not found")
    if instance.initiatedById != user_id:
        raise WorkflowError("Only the original submitter can re-submit this record")
    if instance.status != InstanceStatus.REJECTED.value:
        raise WorkflowError("Only rejected workflows can be re-submitted")

    steps = sorted(instance.definition.steps, key=lambda s: s.sequence)
    maker = next((s for s in steps if s.stepType == StepType.MAKER.value), steps[0])
    next_step = _find_next_applicable_step(steps, maker.sequence, record_data or {})
    if next_step is None:
        raise WorkflowError("Workflow has no reviewable step after the Maker")

    instance.status = InstanceStatus.IN_PROGRESS.value
    instance.currentStepId = next_step.id
    instance.currentStepName = next_step.name
    instance.completedAt = None
    db.add(
        WorkflowHistory(
            instanceId=instance.id,
            stepId=maker.id,
            stepName=maker.name,
            action=Action.SUBMITTED.value,
            performedById=user_id,
            comments=f"Re-submitted after rework. {comments or ''}".strip(),
            fromStatus=InstanceStatus.REJECTED.value,
            toStatus=InstanceStatus.IN_PROGRESS.value,
        )
    )
    await _create_task_for_step(
        db,
        instance=instance,
        step=next_step,
        record_data=record_data or {},
        record_number=instance.recordNumber,
        record_title=None,  # recordTitle column doesn't exist in Prisma
        module=instance.module,
        record_id=instance.recordId,
        initiator_id=instance.initiatedById,
        plant_id=plant_id,
    )
    await _sync_record_status(
        db,
        module=instance.module,
        record_id=instance.recordId,
        next_step_type=next_step.stepType,
        instance_completed=False,
    )
    return {"ok": True, "sentTo": next_step.name}
