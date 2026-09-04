"""Observations router. Vertical slice that demonstrates the full pattern
the other modules will follow:

  • authorize() at the top of every handler
  • plant-scope filter on list queries (via get_accessible_plants)
  • workflow engine kicked off on create
  • permission service consulted for both module action AND scope

This file is the template for porting the remaining 7 operational modules.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.incident import Incident
from app.models.permit import Permit
from app.models.observation import (
    Observation,
    ObservationAttachment,
    ObservationCategory,
    ObservationStatus,
    ObservationTaxonomy,
)
from app.models.observation_severity import OVERRIDE_SOURCE_EDIT
from app.models.plant import Area, Plant
from app.models.user import User
from app.models.workflow import WorkflowTask
from app.schemas.observation import (
    ObservationCreate,
    ObservationListResponse,
    ObservationOut,
    ObservationUpdate,
)
from app.services import workflow_engine
from app.services.register_view import status_counts, workflow_bottleneck, workflow_chips
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
)
from app.services.storage import (
    build_storage_path,
    create_signed_download_url,
    create_signed_upload_url,
    is_storage_configured,
)

router = APIRouter(prefix="/api/observations", tags=["observations"])


async def _is_workflow_actor(db: AsyncSession, user_id: str, observation_id: str) -> bool:
    """True if the caller has any WorkflowTask (pending or completed) for
    this observation. Workflow assignees need to read the record's
    attachments to do their job, even when their role's OBSERVATION.READ
    scope is OWN_RECORDS and they aren't the observer/responsible person."""
    stmt = (
        select(WorkflowTask.id)
        .where(WorkflowTask.module == "OBSERVATION")
        .where(WorkflowTask.recordId == observation_id)
        .where(WorkflowTask.assignedToId == user_id)
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def _has_uploaded_attachment(db: AsyncSession, user_id: str, observation_id: str) -> bool:
    """True if the caller uploaded at least one (non-deleted) attachment to
    this observation. Whoever contributes evidence must always be able to see
    it back in the gallery, even without an OBSERVATION.READ grant."""
    stmt = (
        select(ObservationAttachment.id)
        .where(ObservationAttachment.observationId == observation_id)
        .where(ObservationAttachment.uploadedById == user_id)
        .where(ObservationAttachment.deletedAt.is_(None))
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


# ─── Register (list-screen) view model ───────────────────────────────

# 180 days of unsafe records feed the "this week's focus" hero. Bounded so a
# large tenant cannot turn the register into a full-table scan.
_HERO_WINDOW_DAYS = 180
_HERO_ROW_CAP = 5000


async def _register_payload(db: AsyncSession, scoped, rows) -> dict[str, Any]:
    """Everything the observations register renders, from one scoped SELECT."""
    counts = await status_counts(db, scoped, Observation.status)

    plant_names = dict(
        (
            await db.execute(
                select(Plant.id, Plant.name).where(Plant.id.in_({r.plantId for r in rows}))
            )
        ).all()
    ) if rows else {}
    area_ids = {r.areaId for r in rows if r.areaId}
    area_names = dict(
        (
            await db.execute(select(Area.id, Area.name).where(Area.id.in_(area_ids)))
        ).all()
    ) if area_ids else {}
    chips = await workflow_chips(db, "OBSERVATION", [r.id for r in rows])

    # Open backlog across the whole accessible set — NOT just this page, so the
    # category and dwell panels agree with the tab counts above them.
    open_scoped = scoped.where(Observation.status != "CLOSED")
    open_rows = (
        await db.execute(
            open_scoped.with_only_columns(
                Observation.id, Observation.category, Observation.areaId
            ).limit(_HERO_ROW_CAP)
        )
    ).all()

    # category x distinct-area rollup of the open backlog.
    cat_agg: dict[str, dict[str, Any]] = {}
    for _oid, category, area_id in open_rows:
        key = category.value if hasattr(category, "value") else str(category)
        entry = cat_agg.setdefault(key, {"count": 0, "areas": set()})
        entry["count"] += 1
        if area_id:
            entry["areas"].add(area_id)
    category_groups = sorted(
        (
            {"category": k, "count": v["count"], "areaCount": len(v["areas"])}
            for k, v in cat_agg.items()
        ),
        key=lambda r: r["count"],
        reverse=True,
    )

    bottleneck = await workflow_bottleneck(db, "OBSERVATION", [o[0] for o in open_rows])

    # Hero source rows: at-risk records in the trailing window. Returned as a
    # projection rather than a finished hero so the page keeps ownership of the
    # copy and the click-through, which are presentation.
    window_start = datetime.now(timezone.utc) - timedelta(days=_HERO_WINDOW_DAYS)
    unsafe = (
        await db.execute(
            scoped.with_only_columns(
                Observation.category,
                Observation.subCategoryCode,
                Observation.date,
                Observation.status,
                Observation.responsiblePersonId,
                Observation.plantId,
                Observation.areaId,
            )
            .where(Observation.type.in_(["UNSAFE_ACT", "UNSAFE_CONDITION"]))
            .where(Observation.date >= window_start)
            .limit(_HERO_ROW_CAP)
        )
    ).all()
    hero_plant_ids = {u.plantId for u in unsafe}
    hero_area_ids = {u.areaId for u in unsafe if u.areaId}
    hero_plants = dict(
        (
            await db.execute(select(Plant.id, Plant.name).where(Plant.id.in_(hero_plant_ids)))
        ).all()
    ) if hero_plant_ids else {}
    hero_areas = dict(
        (
            await db.execute(select(Area.id, Area.name).where(Area.id.in_(hero_area_ids)))
        ).all()
    ) if hero_area_ids else {}

    def _v(x):
        return x.value if hasattr(x, "value") else x

    items = []
    for r in rows:
        item = ObservationOut.model_validate(r).model_dump()
        item["plantName"] = plant_names.get(r.plantId)
        item["areaName"] = area_names.get(r.areaId) if r.areaId else None
        item["workflow"] = chips.get(r.id)
        items.append(item)

    return {
        "items": items,
        "total": len(items),
        "statusCounts": counts,
        "categoryGroups": category_groups,
        "bottleneck": bottleneck,
        "openCount": len(open_rows),
        "unsafeRecords": [
            {
                "category": _v(u.category),
                "subCategoryCode": u.subCategoryCode,
                "date": u.date,
                "status": _v(u.status),
                "responsiblePersonId": u.responsiblePersonId,
                "plantId": u.plantId,
                "plantName": hero_plants.get(u.plantId),
                "areaName": hero_areas.get(u.areaId) if u.areaId else None,
            }
            for u in unsafe
        ],
    }


# No response_model on purpose — same as the other register endpoints
# (incidents / near_miss / ptw / manhours). This route serves TWO shapes and a
# fixed response_model silently filters the response down to its own fields:
# with ObservationListResponse declared here, FastAPI dropped statusCounts,
# categoryGroups, bottleneck, openCount and unsafeRecords from the
# `register=true` payload, and the web register page crashed on the first of
# them it read.
@router.get("")
async def list_observations(
    status_filter: str | None = None,
    register: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """List observations the caller can see. Plant-scoped server-side.

    `register=true` returns the full list-screen view model instead of the bare
    item list: display names, status tab counts, the open category x area
    breakdown, per-step dwell, and the unsafe-record projection the "this
    week's focus" hero is built from. The web register needed eight separate
    queries to assemble that; mobile still gets the plain shape by default.
    """
    read_check = await can(db, user.id, "OBSERVATION.READ", PermissionContext())
    if not read_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, read_check.reason or "Access denied")

    accessible_plants = await get_accessible_plants(db, user.id)

    stmt = select(Observation)
    # Apply plant scope. None ⇒ ALL_PLANTS (no filter). Empty list ⇒ no rows.
    if accessible_plants is None:
        pass
    elif len(accessible_plants) == 0:
        return ObservationListResponse(items=[], total=0)
    else:
        stmt = stmt.where(Observation.plantId.in_(accessible_plants))

    # OWN_RECORDS users (e.g. Workers) only see records they're attached to.
    # We detect this by absence of OWN_PLANT/OWN_DEPARTMENT/ALL_PLANTS scopes.
    # The check is rough — refine later when needed.
    if accessible_plants is not None and read_check.matched_scope == "OWN_RECORDS":
        stmt = stmt.where(
            (Observation.observerId == user.id) | (Observation.responsiblePersonId == user.id)
        )

    if status_filter:
        try:
            ObservationStatus(status_filter)
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid status: {status_filter}") from e

    # Scope-only SELECT, captured BEFORE the status filter and the page limit:
    # the tab counts, the category breakdown, the dwell panel and the hero
    # cluster all describe the caller's whole accessible register, not the
    # hundred rows this page happens to return.
    scoped = stmt

    # Newest-created first — platform-wide register convention. Ordering by the
    # user-entered event `date` buried a just-submitted record whenever the
    # event itself was backdated.
    page = scoped
    if status_filter:
        page = page.where(Observation.status == ObservationStatus(status_filter))
    rows = (
        await db.execute(
            page.order_by(Observation.createdAt.desc(), Observation.id.desc()).limit(100)
        )
    ).scalars().all()

    if not register:
        return ObservationListResponse(
            items=[ObservationOut.model_validate(r) for r in rows], total=len(rows)
        )

    return await _register_payload(db, scoped, rows)


@router.post("", response_model=ObservationOut, status_code=status.HTTP_201_CREATED)
async def create_observation(
    payload: ObservationCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ObservationOut:
    await require_permission_with_context(
        "OBSERVATION.CREATE", user, db, plant_id=payload.plantId
    )

    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plant")

    # P3-1 BBS quality gate — reject vague at-risk submissions; compute specificity.
    from app.services.bbs_quality import capa_recommended, quality_score, validate_quality

    _otype = payload.type.value if hasattr(payload.type, "value") else str(payload.type)
    _qerr = validate_quality(_otype, payload.description)
    if _qerr:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _qerr)
    _qscore = quality_score(
        payload.description,
        payload.areaId,
        payload.responsiblePersonId,
        location=payload.location,
    )

    # ─── STOP taxonomy gate ───
    # 400s on a category/sub-category that doesn't belong to this type's axis
    # (e.g. "Reactions of People" on an UNSAFE_CONDITION). Server-side by
    # design — the type-aware dropdowns are UX, this is the enforcement.
    from app.services import observation_taxonomy as tax

    _cat_code, _sub_code, _axis = await tax.validate_selection(
        db, payload.type, payload.categoryCode, payload.subCategoryCode
    )
    # Dual-write the legacy `category` enum from the STOP code so every existing
    # group-by-category consumer (insight rules, Daily Brief, BBS quality,
    # list-view analytics, mobile) keeps working untouched. Safe observations
    # keep the hazard category the observer picked.
    _legacy = tax.legacy_category_for(_cat_code) or payload.category
    if _legacy is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A hazard category is required for safe observations.",
        )
    # Coerce explicitly: legacy_category_for hands back a plain string, and
    # relying on SQLAlchemy's name-vs-value lookup to accept it would break the
    # day a STOP code's name and value stop being identical.
    _category = ObservationCategory(_legacy)

    # Compare on date only — sidesteps the offset-naive vs offset-aware
    # datetime mismatch you get when the form sends a bare YYYY-MM-DD that
    # Pydantic parses as a naive datetime. Only reachable on the manual
    # fallback path: with an SLA policy in force the server computes the date.
    if payload.targetDate is not None:
        target_d = payload.targetDate.date() if hasattr(payload.targetDate, "date") else payload.targetDate
        if target_d < datetime.now(timezone.utc).date():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Target closure date cannot be in the past.")

    # ─── Worker Involved gate ───
    # Mandatory only for UNSAFE_ACT at HIGH/CRITICAL. Enforced server-side so
    # the form's conditional asterisk cannot be the only thing holding the rule
    # (spec §7 walks all four severity × two type combinations).
    from app.services import observation_deroster as deroster_svc

    _workers_required = deroster_svc.worker_involved_required(payload.type, payload.severity)
    if _workers_required and not payload.workersInvolved:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "At least one worker must be named for a High or Critical severity Unsafe Act.",
        )
    for _w in payload.workersInvolved:
        try:
            _w.validated()
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    # ─── Severity suggestion gate ───
    # The engine is re-resolved HERE rather than trusting `payload.suggestedSeverity`.
    # It is deterministic, so recomputing costs one indexed lookup — and keying
    # the justification requirement on a client-supplied value would let any
    # client skip it by claiming the suggestion matched. A combination with no
    # seeded rule yields no suggestion, no requirement and no log row, which is
    # exactly today's behaviour (§6.6 — degrade gracefully, never block).
    from app.services import observation_severity as sev_svc

    _suggestion = await sev_svc.resolve(
        db,
        observation_type=_axis or payload.type,
        category=_cat_code,
        sub_category=_sub_code,
        plant_id=payload.plantId,
        area_id=payload.areaId,
    )
    try:
        sev_svc.require_reason(_suggestion, payload.severity, payload.severityOverrideReason)
    except sev_svc.SeverityOverrideError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    # Number generation — same pattern as Node side: count existing rows + 1
    count_stmt = select(func.count()).select_from(Observation).where(Observation.plantId == payload.plantId)
    last = (await db.execute(count_stmt)).scalar_one()
    number = f"SO-{payload.date.year}-{plant.code}-{last + 1:04d}"

    obs = Observation(
        number=number,
        date=payload.date,
        type=payload.type,
        category=_category,
        categoryCode=_cat_code,
        subCategoryCode=_sub_code,
        taxonomyAxis=_axis,
        severity=payload.severity,
        plantId=payload.plantId,
        areaId=payload.areaId,
        location=(payload.location or "").strip() or None,
        department=(payload.department or "").strip() or None,
        observerId=user.id,
        responsiblePersonId=payload.responsiblePersonId,
        contractorCompanyId=payload.contractorCompanyId,
        description=payload.description,
        qualityScore=_qscore,
        antecedent=getattr(payload, "antecedent", None),
        behaviourObserved=getattr(payload, "behaviourObserved", None),
        consequence=getattr(payload, "consequence", None),
        immediateAction=payload.immediateAction,
        targetDate=payload.targetDate,
        status=ObservationStatus.OPEN,
    )
    db.add(obs)
    await db.flush()

    # ─── Severity override log ───
    # After the flush, so `obs.id` exists and the row can never reference an id
    # that was rolled back. Inside the main transaction, so an override can
    # never be silently lost while the observation it justifies saves. Writes
    # nothing when the observer accepted the suggestion — agreement carries no
    # analytical signal the observation itself doesn't already carry.
    sev_svc.log_override(
        db,
        observation=obs,
        suggestion=_suggestion,
        final_severity=payload.severity,
        reason=payload.severityOverrideReason,
        actor_id=user.id,
        source=payload.severityOverrideSource,
    )

    # ─── SLA-based target closure date ───
    # Computed from the severity × category-group matrix and stamped with a
    # frozen copy of the policy applied. Falls back to the submitted free-text
    # date when no policy matches, so unconfigured policy never blocks a
    # submission (spec §2.1). Runs INSIDE the main transaction — the date and
    # its history row are part of the record, not a best-effort side effect.
    from app.services import observation_sla as sla_svc

    await sla_svc.apply_on_create(
        db, obs, submitted_target_date=payload.targetDate, actor_id=user.id
    )
    await db.flush()

    # ─── Named workers + deroster trigger ───
    # Also inside the main transaction: a High-severity act whose worker rows
    # silently failed to save would leave the record looking unattributed, and
    # the soft-lock is the whole point of the feature.
    _workers = []
    if payload.workersInvolved:
        try:
            _workers = await deroster_svc.persist_workers_involved(
                db, obs, payload.workersInvolved, actor_id=user.id
            )
        except deroster_svc.DerosterError as e:
            raise HTTPException(e.status_code, str(e)) from e

    if _workers:
        await deroster_svc.trigger_for_observation(
            db, obs, _workers, actor_id=user.id, category_label=obs.subCategoryCode
        )

    # Kick off workflow. Best-effort — workflow init failures must NOT
    # poison the main transaction (otherwise the Observation INSERT, even
    # though already flushed, gets rolled back at commit time → 500).
    # Wrap in a SAVEPOINT so a flush failure in the engine rolls back
    # only the engine's partial work, leaving the outer transaction
    # consistent.
    import sys
    import traceback

    try:
        async with db.begin_nested():
            await workflow_engine.initiate(
                db,
                module="OBSERVATION",
                record_id=obs.id,
                record_number=obs.number,
                record_title=obs.description[:120],
                record_data={
                    "type": obs.type.value,
                    "severity": obs.severity.value,
                    "plantId": obs.plantId,
                    "observerId": obs.observerId,
                    "responsiblePersonId": obs.responsiblePersonId,
                },
                initiator_id=user.id,
                plant_id=obs.plantId,
            )
    except Exception as e:  # noqa: BLE001
        print(f"Observation workflow init failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    # TriageAgent — run on submission. Best-effort, never blocks creation.
    # Output is appended to closureTriggers with ruleId="rule_triage_on_submit".
    # Same SAVEPOINT pattern: a write failure here (e.g. column missing,
    # transient DB error) rolls back only this block.
    try:
        async with db.begin_nested():
            from app.services.ai.agents.triage import run_triage

            triage = await run_triage(
                observation={
                    "type": obs.type.value,
                    "category": obs.category.value,
                    "severity": obs.severity.value,
                    "description": obs.description,
                    "immediateAction": obs.immediateAction,
                }
            )
            if triage is not None:
                entry = {
                    "ruleId": "rule_triage_on_submit",
                    "ruleName": "Triage (AI)",
                    "fired": not triage.get("skipped", False),
                    "reason": triage.get("rationale") or triage.get("reason") or "",
                    "spawnedRecordType": "AI_TRIAGE",
                    "data": triage,
                }
                existing = obs.closureTriggers or []
                if not isinstance(existing, list):
                    existing = []
                obs.closureTriggers = [entry, *existing]
                await db.flush()
    except Exception as e:  # noqa: BLE001
        print(f"TriageAgent failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    # Training & Competency Engine — stage a trigger event (dedicated outbox) so
    # the background resolver can evaluate the severity + threshold rules against
    # this observation's classification. Best-effort SAVEPOINT — never blocks
    # creation (spec: rule engine runs as a background job, not in the request).
    try:
        async with db.begin_nested():
            from app.services.training_engine import emit_training_trigger

            await emit_training_trigger(db, "OBSERVATION", obs)
    except Exception as e:  # noqa: BLE001
        print(f"Training trigger emit failed: {e}", file=sys.stderr)

    # Final refresh before serialising. The savepoint flushes above
    # (workflow init, TriageAgent's UPDATE on closureTriggers) leave
    # `obs` with expired attributes — even with expire_on_commit=False
    # SQLAlchemy can mark fields stale after a write. Reading any of
    # those (e.g. updatedAt) inside Pydantic's sync validator triggers
    # MissingGreenlet because the lazy load needs an async context.
    # Refreshing here loads everything in one round-trip.
    await db.refresh(obs)
    out = ObservationOut.model_validate(obs)
    # Echo back the named workers with their flags so the form can route
    # straight to the review panel on a qualifying submission.
    from app.schemas.observation_sla import WorkerInvolvedOut

    out.workersInvolved = [
        WorkerInvolvedOut(**r) for r in await deroster_svc.load_workers_involved(db, obs.id)
    ]
    return out


@router.get("/{observation_id}")
async def get_observation(
    observation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    obs = await db.get(Observation, observation_id)
    if obs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Observation not found")

    record_dict = {
        "observerId": obs.observerId,
        "responsiblePersonId": obs.responsiblePersonId,
    }
    result = await can(
        db,
        user.id,
        "OBSERVATION.READ",
        PermissionContext(record_id=obs.id, plant_id=obs.plantId, record=record_dict),
    )
    if not result.allowed and not await _is_workflow_actor(db, user.id, observation_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    # Detail view hydrates the named workers + their reviews; the list route
    # deliberately does not (one child query per row).
    from app.schemas.observation_sla import WorkerInvolvedOut
    from app.services import observation_deroster as deroster_svc

    validated = ObservationOut.model_validate(obs)
    validated.workersInvolved = [
        WorkerInvolvedOut(**r) for r in await deroster_svc.load_workers_involved(db, obs.id)
    ]
    out: dict[str, Any] = validated.model_dump()

    # ── Display names + cross-module links the detail view renders ─────
    plant = await db.get(Plant, obs.plantId)
    out["plant"] = {"id": plant.id, "name": plant.name} if plant else None
    area = await db.get(Area, obs.areaId) if obs.areaId else None
    out["area"] = {"id": area.id, "name": area.name} if area else None

    people_ids = {
        pid for pid in (obs.observerId, obs.responsiblePersonId) if pid
    }
    people = {
        uid: {"id": uid, "name": name, "designation": desig}
        for uid, name, desig in (
            await db.execute(
                select(User.id, User.name, User.designation).where(User.id.in_(people_ids))
            )
        ).all()
    }
    out["observer"] = people.get(obs.observerId)
    out["responsiblePerson"] = (
        people.get(obs.responsiblePersonId) if obs.responsiblePersonId else None
    )

    out["contractorCompany"] = None
    if obs.contractorCompanyId:
        from app.models.epc import ContractorCompany

        company = await db.get(ContractorCompany, obs.contractorCompanyId)
        out["contractorCompany"] = (
            {"id": company.id, "name": company.name} if company else None
        )

    # The taxonomy row behind categoryCode. Null on safe observations and on
    # legacy at-risk rows the migration couldn't map.
    #
    # Keyed on (categoryCode, axis) ONLY. It used to require subCategoryCode
    # too, which meant that once the Sub-category field was removed every new
    # observation fell through to the coarse legacy `category` enum — a record
    # filed as "Bypassing safety device / guards" displayed as "Tools
    # Equipment". The sub-category label is still returned when the record
    # happens to carry one.
    out["stopTaxonomy"] = None
    if obs.categoryCode and obs.taxonomyAxis:
        stmt = (
            select(
                ObservationTaxonomy.categoryLabel,
                ObservationTaxonomy.subCategoryLabel,
                ObservationTaxonomy.stopReferenceCode,
            )
            .where(ObservationTaxonomy.categoryCode == obs.categoryCode)
            .where(ObservationTaxonomy.observationType == obs.taxonomyAxis)
        )
        if obs.subCategoryCode:
            stmt = stmt.where(ObservationTaxonomy.subCategoryCode == obs.subCategoryCode)
        else:
            # Any row for this category carries the category's own label; take
            # the first in display order for a stable answer.
            stmt = stmt.order_by(ObservationTaxonomy.displayOrder)
        tax = (await db.execute(stmt.limit(1))).first()
        if tax is not None:
            out["stopTaxonomy"] = {
                "categoryLabel": tax[0],
                # Only meaningful when the record actually carries one — the
                # placeholder "General" row must never surface as a real
                # sub-category the observer chose.
                "subCategoryLabel": tax[1] if obs.subCategoryCode else None,
                "stopReferenceCode": tax[2],
            }

    # Every value targetDate has held — the provenance trail the sidebar shows.
    from app.models.observation_sla import ObservationTargetDateHistory

    history = (
        await db.execute(
            select(ObservationTargetDateHistory)
            .where(ObservationTargetDateHistory.observationId == obs.id)
            .order_by(ObservationTargetDateHistory.changedAt.asc())
        )
    ).scalars().all()
    out["targetDateHistory"] = [
        {c.name: getattr(h, c.name) for c in h.__table__.columns} for h in history
    ]

    # ── "Related Items": the records this observation is linked to ─────
    async def _ref(model, record_id: str | None) -> dict[str, Any] | None:
        if not record_id:
            return None
        row = await db.get(model, record_id)
        if row is None:
            return None
        return {"id": row.id, "number": getattr(row, "number", None)}

    from app.models.equipment import Inspection

    out["activePermit"] = await _ref(Permit, obs.activePermitId)
    out["triggeredInspection"] = await _ref(Inspection, obs.triggeredInspectionId)
    out["contributedToIncident"] = await _ref(Incident, obs.contributedToIncidentId)

    # Coaching tasks spawned by the closure rules. The table is Prisma-only
    # (no SQLAlchemy model yet), so it is read as plain SQL rather than being
    # silently dropped from the payload.
    try:
        coaching = (
            await db.execute(
                text(
                    'SELECT id, number, type, status FROM "CoachingTask" '
                    'WHERE "fromObservationId" = :oid ORDER BY "createdAt"'
                ),
                {"oid": obs.id},
            )
        ).all()
        out["coachingTasks"] = [
            {"id": c[0], "number": c[1], "type": c[2], "status": c[3]} for c in coaching
        ]
    except Exception:  # noqa: BLE001 — table absent on older deployments
        out["coachingTasks"] = []
    return out


@router.patch("/{observation_id}", response_model=ObservationOut)
async def update_observation(
    observation_id: str,
    payload: ObservationUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ObservationOut:
    obs = await db.get(Observation, observation_id)
    if obs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Observation not found")

    record_dict = {
        "observerId": obs.observerId,
        "responsiblePersonId": obs.responsiblePersonId,
    }
    perm_code = "OBSERVATION.CLOSE" if payload.status == ObservationStatus.CLOSED else "OBSERVATION.UPDATE"
    result = await can(
        db,
        user.id,
        perm_code,
        PermissionContext(record_id=obs.id, plant_id=obs.plantId, record=record_dict),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    # ─── Core-detail edit ("edit while open"). A CLOSED observation is a
    #     finalised record — its facts can't be edited (workflow status changes
    #     still go through their own panels). ───
    core_edit = any(
        v is not None
        for v in (
            payload.type, payload.category, payload.categoryCode, payload.subCategoryCode,
            payload.severity, payload.description, payload.areaId, payload.location,
            payload.department,
        )
    )
    if core_edit and obs.status == ObservationStatus.CLOSED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot edit a closed observation.")

    # ─── STOP taxonomy ───
    # Only re-validated when the edit actually touches type or the taxonomy —
    # a status-only PATCH on a legacy record (no categoryCode yet) must not be
    # blocked by a requirement that record predates.
    from app.services import observation_taxonomy as tax

    if any(v is not None for v in (payload.type, payload.categoryCode, payload.subCategoryCode)):
        new_type = payload.type if payload.type is not None else obs.type
        old_axis = obs.taxonomyAxis or tax.axis_for_type(obs.type)
        # Crossing the act/condition boundary invalidates the stored pair by
        # definition. Drop it and let validate_selection demand a fresh one —
        # the server-side mirror of the form's clear-and-re-prompt, so an API
        # caller can't leave a now-invalid category silently attached either.
        axis_changed = tax.axis_for_type(new_type) != old_axis
        keep_cat = None if axis_changed else obs.categoryCode
        keep_sub = None if axis_changed else obs.subCategoryCode
        cat_code, sub_code, axis = await tax.validate_selection(
            db,
            new_type,
            payload.categoryCode if payload.categoryCode is not None else keep_cat,
            payload.subCategoryCode if payload.subCategoryCode is not None else keep_sub,
        )
        obs.categoryCode = cat_code
        obs.subCategoryCode = sub_code
        obs.taxonomyAxis = axis
        legacy = tax.legacy_category_for(cat_code)
        if legacy is not None:
            obs.category = ObservationCategory(legacy)

    if payload.type is not None:
        obs.type = payload.type
    if payload.category is not None and tax.legacy_category_for(obs.categoryCode) is None:
        # An at-risk record's `category` is owned by the STOP code (dual-write);
        # only a safe observation lets the client set it directly.
        obs.category = payload.category
    if payload.severity is not None:
        obs.severity = payload.severity
    if payload.description is not None:
        obs.description = payload.description
    if payload.areaId is not None:
        obs.areaId = payload.areaId or None
    if payload.location is not None:
        obs.location = payload.location.strip() or None
    if payload.department is not None:
        obs.department = payload.department.strip() or None
    if payload.responsiblePersonId is not None:
        obs.responsiblePersonId = payload.responsiblePersonId or None
    if payload.targetDate is not None:
        obs.targetDate = payload.targetDate

    # ─── Severity suggestion gate (edit path) ───
    # Runs only when the edit touches an input the suggestion depends on. A
    # status-only or description-only PATCH must never demand a justification
    # for a severity that was set — and possibly already justified — earlier.
    #
    # Re-saving an unchanged, already-logged override is also not a new
    # decision: `existing_override` finds the row for this exact
    # (suggested, final) pair and lets the save through without duplicating it.
    # A different pair is a fresh decision and needs its own reason.
    _sev_inputs_touched = any(
        v is not None
        for v in (payload.severity, payload.categoryCode, payload.subCategoryCode,
                  payload.type, payload.areaId)
    )
    if _sev_inputs_touched:
        from app.services import observation_severity as sev_svc

        suggestion = await sev_svc.resolve_for_observation(db, obs)
        if sev_svc.diverges(suggestion, obs.severity):
            final = sev_svc.normalise_severity(obs.severity)
            already = await sev_svc.existing_override(
                db,
                observation_id=obs.id,
                suggested=suggestion["suggested"],
                final=final,
            )
            if already is None:
                try:
                    sev_svc.require_reason(
                        suggestion, obs.severity, payload.severityOverrideReason
                    )
                except sev_svc.SeverityOverrideError as e:
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
                sev_svc.log_override(
                    db,
                    observation=obs,
                    suggestion=suggestion,
                    final_severity=obs.severity,
                    reason=payload.severityOverrideReason,
                    actor_id=user.id,
                    source=OVERRIDE_SOURCE_EDIT,
                )

    if payload.status is not None:
        obs.status = payload.status
        if payload.status == ObservationStatus.CLOSED:
            obs.closedAt = datetime.now(timezone.utc)
    if payload.closingRemark is not None:
        obs.closingRemark = payload.closingRemark
    await db.flush()
    await db.refresh(obs)
    return ObservationOut.model_validate(obs)


@router.delete("/{observation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_observation(
    observation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hard-delete an observation. Per the RBAC matrix, only HSE_MANAGER
    (own plant), CORPORATE_HSE (all plants), and ADMIN (all plants)
    have OBSERVATION.DELETE — the permission service enforces the scope.
    Cascades remove the workflow instance, tasks, history, and any
    attachments via DB foreign keys (ondelete=CASCADE)."""
    obs = await db.get(Observation, observation_id)
    if obs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Observation not found")
    record_dict = {
        "observerId": obs.observerId,
        "responsiblePersonId": obs.responsiblePersonId,
    }
    result = await can(
        db,
        user.id,
        "OBSERVATION.DELETE",
        PermissionContext(record_id=obs.id, plant_id=obs.plantId, record=record_dict),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    # Drop the workflow instance + downstream rows first. Prisma's
    # WorkflowInstance has ondelete CASCADE on its FKs to history/tasks,
    # but Observation doesn't FK into WorkflowInstance — we have to
    # delete it ourselves.
    from app.models.workflow import WorkflowInstance

    inst_rows = (
        await db.execute(
            select(WorkflowInstance).where(
                WorkflowInstance.module == "OBSERVATION",
                WorkflowInstance.recordId == observation_id,
            )
        )
    ).scalars().all()
    for inst in inst_rows:
        await db.delete(inst)

    # Soft-delete attachments instead of hard-delete so the storage
    # objects (Supabase) can be reaped later by a cleanup job.
    att_rows = (
        await db.execute(
            select(ObservationAttachment).where(
                ObservationAttachment.observationId == observation_id,
                ObservationAttachment.deletedAt.is_(None),
            )
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    for att in att_rows:
        att.deletedAt = now

    await db.delete(obs)
    await db.flush()
    return None


# ─── Attachments ─────────────────────────────────────────────────────────
# Same two-phase upload pattern as IncidentAttachment — see that router for
# the design notes (init → direct PUT to Supabase signed URL → complete).

VALID_OBS_CATEGORIES = {"INITIAL_PHOTO", "ACTION_EVIDENCE", "VERIFICATION_PHOTO", "DOCUMENT"}
ALLOWED_OBS_MIME = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic",
    "video/mp4", "video/quicktime",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv", "text/plain",
}
MAX_OBS_FILE_SIZE = 50 * 1024 * 1024


def _attachment_to_dict(a: ObservationAttachment) -> dict[str, Any]:
    # Frontend (attachment-gallery.tsx) reads `att.uploadedBy.id` — emit the
    # nested user object, not just the flat id, otherwise it crashes with
    # "Cannot read properties of undefined".
    uploaded_by: dict[str, Any] | None = None
    if a.uploadedBy is not None:
        uploaded_by = {
            "id": a.uploadedBy.id,
            "name": a.uploadedBy.name,
            "designation": a.uploadedBy.designation,
        }
    return {
        "id": a.id,
        "observationId": a.observationId,
        "category": a.category,
        "fileName": a.fileName,
        "fileSize": a.fileSize,
        "mimeType": a.mimeType,
        "caption": a.caption,
        "exifData": a.exifData,
        "uploadedAt": a.uploadedAt,
        "uploadedById": a.uploadedById,
        "uploadedBy": uploaded_by,
    }


@router.get("/{observation_id}/attachments")
async def list_attachments(
    observation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    obs = await db.get(Observation, observation_id)
    if obs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Observation not found")
    record = {"observerId": obs.observerId, "responsiblePersonId": obs.responsiblePersonId}
    result = await can(
        db, user.id, "OBSERVATION.READ",
        PermissionContext(record_id=obs.id, plant_id=obs.plantId, record=record),
    )
    # The observer and anyone who uploaded evidence here can always see the
    # gallery, even without an OBSERVATION.READ grant — so an uploader never
    # loses sight of their own contribution.
    if (
        not result.allowed
        and obs.observerId != user.id
        and not await _is_workflow_actor(db, user.id, observation_id)
        and not await _has_uploaded_attachment(db, user.id, observation_id)
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")

    rows = (
        await db.execute(
            select(ObservationAttachment)
            .options(selectinload(ObservationAttachment.uploadedBy))
            .where(ObservationAttachment.observationId == observation_id)
            .where(ObservationAttachment.deletedAt.is_(None))
            .order_by(ObservationAttachment.uploadedAt.desc())
        )
    ).scalars().all()
    return {"items": [_attachment_to_dict(r) for r in rows]}


@router.post("/{observation_id}/attachments")
async def upload_attachment(
    observation_id: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    obs = await db.get(Observation, observation_id)
    if obs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Observation not found")
    record = {"observerId": obs.observerId, "responsiblePersonId": obs.responsiblePersonId}
    result = await can(
        db, user.id, "OBSERVATION.UPDATE",
        PermissionContext(record_id=obs.id, plant_id=obs.plantId, record=record),
    )
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    if not is_storage_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Supabase Storage isn't configured. Set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY.",
        )

    phase = payload.get("phase")
    if phase == "init":
        category = str(payload.get("category") or "")
        file_name = str(payload.get("fileName") or "").strip()
        file_size = int(payload.get("fileSize") or 0)
        mime_type = str(payload.get("mimeType") or "")
        if not file_name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "File name is required")
        if category not in VALID_OBS_CATEGORIES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid category. Must be one of: {', '.join(VALID_OBS_CATEGORIES)}")
        if file_size <= 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "File size must be a positive number")
        if file_size > MAX_OBS_FILE_SIZE:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"File size exceeds the {MAX_OBS_FILE_SIZE // 1024 // 1024} MB limit.")
        if mime_type not in ALLOWED_OBS_MIME:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"MIME type {mime_type} is not allowed.")

        # Reuse incident-style storage path layout, namespaced by "observations"
        from app.services.storage import build_storage_path
        storage_path = build_storage_path(incident_id=observation_id, category=category, file_name=file_name)
        # Override the prefix from "incidents/" to "observations/" — the helper
        # always writes incidents/ but the bucket is shared and we want a clear
        # namespace per module. Done via simple replace.
        if storage_path.startswith("incidents/"):
            storage_path = "observations/" + storage_path[len("incidents/"):]
        try:
            signed = create_signed_upload_url(storage_path)
        except Exception as e:  # noqa: BLE001
            # Bubble up a helpful error so the UI can show what's wrong
            # (bucket missing, wrong key, RLS denial, etc.) instead of a
            # generic "Init failed".
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"Storage upload init failed: {e}",
            ) from e

        att = ObservationAttachment(
            observationId=observation_id,
            category=category,
            fileName=file_name,
            storagePath=storage_path,
            fileSize=file_size,
            mimeType=mime_type,
            uploadedById=user.id,
        )
        db.add(att)
        await db.flush()
        return {
            "phase": "init",
            "attachmentId": att.id,
            "storagePath": storage_path,
            "uploadUrl": signed["uploadUrl"],
            "token": signed["token"],
        }

    if phase == "complete":
        attachment_id = payload.get("attachmentId")
        if not attachment_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "attachmentId required")
        att = await db.get(ObservationAttachment, attachment_id)
        if att is None or att.observationId != observation_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found for this observation")
        att.caption = payload.get("caption")
        att.exifData = payload.get("exifData")
        await db.flush()
        return {"ok": True}

    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown phase")


@router.delete("/{observation_id}/attachments/{attachment_id}")
async def delete_attachment(
    observation_id: str,
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    att = await db.get(ObservationAttachment, attachment_id)
    if att is None or att.observationId != observation_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    obs = await db.get(Observation, observation_id)
    # The uploader can always remove their own attachment — covers the
    # action-owner-uploaded-by-mistake / rework-rejection cases. Falls
    # through to the standard RBAC check otherwise.
    is_uploader = att.uploadedById == user.id
    if not is_uploader:
        record = {
            "observerId": obs.observerId if obs else None,
            "responsiblePersonId": obs.responsiblePersonId if obs else None,
            "uploadedById": att.uploadedById,
        }
        result = await can(
            db, user.id, "OBSERVATION.UPDATE",
            PermissionContext(record_id=att.id, plant_id=obs.plantId if obs else None, record=record),
        )
        if not result.allowed and not await _is_workflow_actor(db, user.id, observation_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    att.deletedAt = datetime.now(timezone.utc)
    await db.flush()
    return {"ok": True}


@router.get("/{observation_id}/attachments/{attachment_id}/download")
async def download_attachment(
    observation_id: str,
    attachment_id: str,
    inline: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    att = await db.get(ObservationAttachment, attachment_id)
    if att is None or att.observationId != observation_id or att.deletedAt is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    obs = await db.get(Observation, observation_id)
    # The uploader can always view their own file — mirrors delete_attachment's
    # is_uploader bypass so the person who uploaded a photo can preview it even
    # without an OBSERVATION.READ grant.
    is_uploader = att.uploadedById == user.id
    record = {
        "observerId": obs.observerId if obs else None,
        "responsiblePersonId": obs.responsiblePersonId if obs else None,
        "uploadedById": att.uploadedById,
    }
    result = await can(
        db, user.id, "OBSERVATION.READ",
        PermissionContext(record_id=obs.id if obs else None, plant_id=obs.plantId if obs else None, record=record),
    )
    if not result.allowed and not is_uploader and not await _is_workflow_actor(db, user.id, observation_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")
    url = create_signed_download_url(
        att.storagePath,
        expires_in_sec=300,
        download=None if inline else att.fileName,
    )
    return {"url": url}


# ── P3-1 Raise a corrective action from an at-risk observation ────────────────
@router.post("/{observation_id}/raise-capa")
async def raise_capa_from_observation(
    observation_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    """One-click CAPA from an at-risk observation (SAFETY_OBSERVATION source).
    Idempotent — returns the existing CAPA if one was already raised."""
    obs = await db.get(Observation, observation_id)
    if obs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Observation not found")
    await require_permission_with_context("OBSERVATION.UPDATE", user, db, plant_id=obs.plantId)
    if obs.capaId:
        return {"capaId": obs.capaId, "created": False}
    from app.services.capa_spawn import spawn_capa
    capa = await spawn_capa(
        db, source_code="SAFETY_OBSERVATION", plant_id=obs.plantId,
        title=f"Corrective action — {obs.description[:120]}", problem=obs.description[:500],
        ref_id=obs.id, ref_url=f"/observations/{obs.id}", ref_summary=obs.number,
        metadata={"observationNumber": obs.number}, severity="MODERATE",
        detected_method="SAFETY_OBSERVATION", owner_id=obs.responsiblePersonId or user.id, actor_id=user.id, due_days=30,
    )
    await db.flush()
    obs.capaId = capa.id
    await db.commit()
    return {"capaId": capa.id, "capaNumber": capa.capaNumber, "created": True}
