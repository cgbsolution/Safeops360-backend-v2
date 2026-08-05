"""Safety Culture Management router — /api/culture.

Follows the observations router template: get_current_user + can() per handler,
plant-scope on list queries. Culture scores feed the ERM KRI engine (see
app/services/safety_culture.register_culture_kris) — that is the module's
structural differentiator, not a bolt-on dashboard.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.plant import Plant
from app.models.safety_culture import (
    CultureObservationClosure,
    LeadershipWalk,
    PerceptionSurveyTemplate,
    PerceptionSurveyResponse,
)
from app.models.user import User
from app.schemas.safety_culture import (
    IntegrityReview,
    LeadershipWalkComplete,
    LeadershipWalkCreate,
    ObservationLinkAction,
    ObservationVerifyClosure,
    RecognitionAwardRequest,
    SurveyResponseSubmit,
    SurveyTemplateCreate,
    WalkRaiseObservation,
)
from app.services import safety_culture as svc
from app.services.permissions import PermissionContext, can, get_accessible_plants

router = APIRouter(prefix="/api/culture", tags=["safety-culture"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _require(db: AsyncSession, user: User, code: str, plant_id: str | None = None) -> None:
    res = await can(db, user.id, code, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")


async def _accessible_or_403(db: AsyncSession, user: User, plant_id: str) -> None:
    plants = await get_accessible_plants(db, user.id)
    if plants is not None and plant_id not in plants:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Plant not in your scope")


async def _scoped_plants(db: AsyncSession, user: User) -> list[tuple[str, str, str, str | None]]:
    """Every plant in the caller's scope as (id, name, code, state) — the shared
    basis for the §Fix 7 multi-site rollup views. Mirrors maturity_enterprise."""
    scope = await get_accessible_plants(db, user.id)
    rows = (await db.execute(select(Plant.id, Plant.name, Plant.code, Plant.state))).all()
    out: list[tuple[str, str, str, str | None]] = []
    for pid, pname, pcode, pstate in rows:
        if scope is not None and pid not in scope:
            continue
        out.append((pid, pname, pcode, pstate))
    return out


async def _current_quarter() -> str:
    n = _now()
    return f"{n.year}-Q{(n.month - 1) // 3 + 1}"


# ════════════════════════════════════════════════════════════════════════════
# §1 Culture Maturity
# ════════════════════════════════════════════════════════════════════════════
@router.get("/maturity/enterprise")
async def maturity_enterprise(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "SAFETY_CULTURE.READ")
    plants_scope = await get_accessible_plants(db, user.id)
    plants = (await db.execute(select(Plant.id, Plant.name, Plant.code, Plant.state))).all()
    sites = []
    stage_counts: dict[str, int] = {"Reactive": 0, "Dependent": 0, "Independent": 0, "Interdependent": 0}
    total = 0.0
    n = 0
    for pid, pname, pcode, pstate in plants:
        if plants_scope is not None and pid not in plants_scope:
            continue
        prof = await svc.maturity_profile_out(db, pid, with_history=False)
        sites.append({
            "plantId": pid, "plantName": pname, "plantCode": pcode, "state": pstate,
            "currentStage": prof["currentStage"], "stageScore": prof["stageScore"],
            "componentScores": prof["componentScores"], "lastCalculatedAt": prof["lastCalculatedAt"],
        })
        stage_counts[prof["currentStage"]] = stage_counts.get(prof["currentStage"], 0) + 1
        total += prof["stageScore"]
        n += 1
    sites.sort(key=lambda s: s["stageScore"], reverse=True)
    return {
        "enterpriseScore": round(total / n, 1) if n else 0.0,
        "siteCount": n, "stageCounts": stage_counts, "sites": sites,
    }


@router.get("/maturity/{plant_id}")
async def maturity_site(plant_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "SAFETY_CULTURE.READ", plant_id)
    await _accessible_or_403(db, user, plant_id)
    return await svc.maturity_profile_out(db, plant_id, with_history=True)


@router.post("/maturity/recalculate/{plant_id}")
async def maturity_recalc(plant_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "SAFETY_CULTURE.RECALC", plant_id)
    await _accessible_or_403(db, user, plant_id)
    await svc.escalate_missed_walks(db, plant_id)
    await svc.calculate_culture_score(db, plant_id)
    await svc.sync_integrity_flags(db, plant_id)
    period = _now().strftime("%Y-%m")
    await svc.award_recognition(db, plant_id, period)
    await db.commit()
    return await svc.maturity_profile_out(db, plant_id, with_history=True)


# ════════════════════════════════════════════════════════════════════════════
# §2 BBS Quality Index + closure loop + integrity
# ════════════════════════════════════════════════════════════════════════════
@router.get("/observations/quality-index/{plant_id}")
async def obs_quality_index(plant_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "SAFETY_CULTURE.READ", plant_id)
    await _accessible_or_403(db, user, plant_id)
    return await svc.bbs_quality_index(db, plant_id, svc.config_for(await svc._vertical_for_plant(db, plant_id)))


@router.get("/observations/quality-index-rollup")
async def obs_quality_rollup(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """§Fix 7 — BBS Quality Index ranked across every site in the caller's scope."""
    await _require(db, user, "SAFETY_CULTURE.READ")
    return await svc.bbs_quality_rollup(db, await _scoped_plants(db, user))


@router.get("/observations/integrity-flags/{plant_id}")
async def obs_integrity(plant_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "SAFETY_CULTURE.READ", plant_id)
    await _accessible_or_403(db, user, plant_id)
    flags = await svc.integrity_flags(db, plant_id)
    return {"plantId": plant_id, "flaggedCount": len(flags), "flags": flags, "framing": "Coaching opportunities — not punitive. Human review required."}


@router.post("/observations/integrity/{observer_id}/review")
async def obs_integrity_review(
    observer_id: str, body: IntegrityReview, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """§Fix 1 — record a human review outcome on an integrity flag (dismiss/uphold)
    with a required note. Dismissing un-freezes the observer's Recognition points
    automatically (the gate is read-time). plant_id is passed as a query param."""
    # The observer's plant drives the scope check; look it up from the User row.
    obs_user = await db.get(User, observer_id)
    plant_id = obs_user.plantId if obs_user else None
    await _require(db, user, "SAFETY_CULTURE.INTEGRITY_REVIEW", plant_id)
    if plant_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Observer not found or has no plant")
    await _accessible_or_403(db, user, plant_id)
    if body.outcome not in ("dismiss", "uphold"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "outcome must be 'dismiss' or 'uphold'")
    res = await svc.review_integrity_flag(
        db, plant_id, observer_id, body.period, body.outcome, body.note, user.id
    )
    await db.commit()
    return res


@router.get("/observations/closure/{plant_id}")
async def obs_closure_list(plant_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """Closure-loop status per observation for the stepper visualisation."""
    await _require(db, user, "SAFETY_CULTURE.READ", plant_id)
    await _accessible_or_403(db, user, plant_id)
    from app.models.observation import Observation

    rows = (
        await db.execute(
            select(Observation.id, Observation.number, Observation.severity, Observation.status, Observation.capaId, Observation.date, Observation.observerId)
            .where(Observation.plantId == plant_id).order_by(Observation.createdAt.desc()).limit(100)
        )
    ).all()
    closures = await svc._closures_by_obs(db, plant_id)
    items = []
    for o in rows:
        cl = closures.get(o.id)
        linked = bool(o.capaId) or (cl is not None and (cl.linkedCapaId or cl.linkedActionId))
        verified = cl is not None and cl.reobservationVerified
        items.append({
            "observationId": o.id, "number": o.number,
            "severity": o.severity.value if hasattr(o.severity, "value") else str(o.severity),
            "status": o.status.value if hasattr(o.status, "value") else str(o.status),
            "observerId": o.observerId,
            "linkedCapaId": (cl.linkedCapaId if cl else None) or o.capaId,
            "linkedActionId": cl.linkedActionId if cl else None,
            "reobservationVerified": verified,
            "reobservationDate": svc._aware(cl.reobservationDate).isoformat() if (cl and cl.reobservationDate) else None,
            "stage": "verified" if verified else ("linked" if linked else "logged"),
        })
    return {"plantId": plant_id, "items": items}


@router.post("/observations/{obs_id}/link-action")
async def obs_link_action(obs_id: str, body: ObservationLinkAction, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    from app.models.observation import Observation

    obs = await db.get(Observation, obs_id)
    if obs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Observation not found")
    await _require(db, user, "SAFETY_CULTURE.CLOSURE", obs.plantId)
    await _accessible_or_403(db, user, obs.plantId)

    linked_capa = body.linkedCapaId
    # Optionally spawn a CAPA if the caller asked and none is linked yet.
    if body.spawnCapa and not linked_capa and not obs.capaId:
        try:
            from app.services.capa_spawn import spawn_capa

            capa = await spawn_capa(
                db, source_code="SAFETY_CULTURE", plant_id=obs.plantId,
                title=f"Culture follow-through: {obs.number}",
                problem=obs.description or "Behaviour observation requiring corrective action.",
                ref_id=obs.id, ref_url=f"/observations/{obs.id}", ref_summary=obs.number,
                detected_method="SAFETY_OBSERVATION", owner_id=obs.responsiblePersonId or user.id, actor_id=user.id, due_days=30,
            )
            if capa:
                linked_capa = capa.id
                obs.capaId = capa.id
        except Exception as e:  # noqa: BLE001 — CAPA source may not be seeded; degrade gracefully
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Could not spawn CAPA: {e}") from e

    cl = (
        await db.execute(select(CultureObservationClosure).where(CultureObservationClosure.observationId == obs_id))
    ).scalar_one_or_none()
    if cl is None:
        cl = CultureObservationClosure(observationId=obs_id, plantId=obs.plantId)
        db.add(cl)
    if linked_capa:
        cl.linkedCapaId = linked_capa
    if body.linkedActionId:
        cl.linkedActionId = body.linkedActionId
    await db.commit()
    await db.refresh(cl)
    return {"observationId": obs_id, "linkedCapaId": cl.linkedCapaId, "linkedActionId": cl.linkedActionId, "reobservationVerified": cl.reobservationVerified}


@router.post("/observations/{obs_id}/verify-closure")
async def obs_verify_closure(obs_id: str, body: ObservationVerifyClosure, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    from app.models.observation import Observation

    obs = await db.get(Observation, obs_id)
    if obs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Observation not found")
    await _require(db, user, "SAFETY_CULTURE.CLOSURE", obs.plantId)
    await _accessible_or_403(db, user, obs.plantId)

    cl = (
        await db.execute(select(CultureObservationClosure).where(CultureObservationClosure.observationId == obs_id))
    ).scalar_one_or_none()
    if cl is None:
        cl = CultureObservationClosure(observationId=obs_id, plantId=obs.plantId, linkedCapaId=obs.capaId)
        db.add(cl)
    cl.reobservationVerified = body.verified
    cl.reobservationDate = body.reobservationDate or _now()
    cl.verifiedById = user.id
    await db.commit()
    await db.refresh(cl)
    return {"observationId": obs_id, "reobservationVerified": cl.reobservationVerified, "reobservationDate": svc._aware(cl.reobservationDate).isoformat() if cl.reobservationDate else None}


# ════════════════════════════════════════════════════════════════════════════
# §3 Leadership walks
# ════════════════════════════════════════════════════════════════════════════
@router.get("/leadership-walks")
async def list_walks(plant_id: str | None = None, leader_id: str | None = None, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "SAFETY_CULTURE.READ", plant_id)
    scope = await get_accessible_plants(db, user.id)
    stmt = select(LeadershipWalk)
    if plant_id:
        stmt = stmt.where(LeadershipWalk.plantId == plant_id)
    elif scope is not None:
        if not scope:
            return {"items": []}
        stmt = stmt.where(LeadershipWalk.plantId.in_(scope))
    if leader_id:
        stmt = stmt.where(LeadershipWalk.leaderId == leader_id)
    # Newest-created first — platform-wide register convention.
    stmt = stmt.order_by(LeadershipWalk.createdAt.desc(), LeadershipWalk.id.desc()).limit(200)
    rows = (await db.execute(stmt)).scalars().all()
    return {"items": [
        {
            "id": w.id, "plantId": w.plantId, "leaderId": w.leaderId,
            "scheduledDate": svc._aware(w.scheduledDate).isoformat() if w.scheduledDate else None,
            "completedDate": svc._aware(w.completedDate).isoformat() if w.completedDate else None,
            "status": w.status, "areaVisited": w.areaVisited, "cadence": w.cadence,
            "workersInteracted": w.workersInteracted, "observationsRaised": w.observationsRaised,
            "hazardsIdentified": w.hazardsIdentified, "notes": w.notes,
            "checklist": w.checklist, "followUpActionIds": w.followUpActionIds,
            "escalatedAt": svc._aware(w.escalatedAt).isoformat() if w.escalatedAt else None,
        }
        for w in rows
    ]}


@router.post("/leadership-walks", status_code=status.HTTP_201_CREATED)
async def create_walk(body: LeadershipWalkCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "SAFETY_CULTURE.WALK_SCHEDULE", body.plantId)
    await _accessible_or_403(db, user, body.plantId)
    walk = LeadershipWalk(
        plantId=body.plantId, leaderId=body.leaderId, scheduledDate=body.scheduledDate,
        areaVisited=body.areaVisited, cadence=body.cadence, notes=body.notes, createdById=user.id, status="Scheduled",
    )
    db.add(walk)
    await db.commit()
    await db.refresh(walk)
    return {"id": walk.id, "status": walk.status}


@router.put("/leadership-walks/{walk_id}/complete")
async def complete_walk(walk_id: str, body: LeadershipWalkComplete, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    walk = await db.get(LeadershipWalk, walk_id)
    if walk is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Walk not found")
    await _require(db, user, "SAFETY_CULTURE.WALK_LOG", walk.plantId)
    await _accessible_or_403(db, user, walk.plantId)
    walk.status = "Completed"
    walk.completedDate = body.completedDate or _now()
    if body.areaVisited:
        walk.areaVisited = body.areaVisited
    walk.workersInteracted = body.workersInteracted
    walk.observationsRaised = body.observationsRaised
    walk.hazardsIdentified = body.hazardsIdentified
    if body.notes:
        walk.notes = body.notes
    if body.checklist is not None:
        walk.checklist = body.checklist.model_dump()
    walk.followUpActionIds = body.followUpActionIds
    await db.commit()
    # recompute the site's leadership component promptly (§Cross-cutting: webhook-style trigger)
    try:
        await svc.calculate_culture_score(db, walk.plantId)
        await db.commit()
    except Exception:
        await db.rollback()
    return {"id": walk.id, "status": walk.status}


@router.post("/leadership-walks/{walk_id}/raise-observation", status_code=status.HTTP_201_CREATED)
async def walk_raise_observation(
    walk_id: str, body: WalkRaiseObservation, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """§Fix 3 — raise a hazard logged on a leadership walk as a real Observation so it
    flows through the SAME BBS closure-loop tracker (Logged → Linked → Verified),
    optionally spawning a CAPA immediately. Returns the new observation id."""
    from app.models.observation import (
        Observation, ObservationCategory, ObservationStatus, ObservationType, Severity,
    )
    from sqlalchemy import func as _func

    walk = await db.get(LeadershipWalk, walk_id)
    if walk is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Walk not found")
    await _require(db, user, "SAFETY_CULTURE.CLOSURE", walk.plantId)
    await _accessible_or_403(db, user, walk.plantId)

    plant = await db.get(Plant, walk.plantId)
    code = plant.code if plant else "NA"

    def _coerce(enum_cls, val, default):
        try:
            return enum_cls(val)
        except Exception:
            return default

    count = (await db.execute(select(_func.count()).select_from(Observation))).scalar_one()
    number = f"OBS-{code}-{count + 1:05d}"

    # A walk hazard is raised as UNSAFE_CONDITION, which now carries the STOP
    # taxonomy. This path bypasses the observations router (it writes the ORM
    # object directly), so map the legacy category the walk form collected onto
    # a CONDITION-axis STOP pair here, and dual-write `category` from it so the
    # record is indistinguishable from one filed through the normal form.
    from app.services import observation_taxonomy as obs_tax

    _cat_code, _sub_code = obs_tax.stop_pair_for_legacy_category(body.category, "CONDITION")
    obs = Observation(
        number=number, date=_now(),
        type=ObservationType.UNSAFE_CONDITION,
        category=_coerce(ObservationCategory, _cat_code, ObservationCategory.OTHERS),
        categoryCode=_cat_code, subCategoryCode=_sub_code, taxonomyAxis="CONDITION",
        severity=_coerce(Severity, body.severity, Severity.MEDIUM),
        plantId=walk.plantId, observerId=walk.leaderId, responsiblePersonId=walk.leaderId,
        description=body.description, status=ObservationStatus.OPEN,
    )
    db.add(obs)
    await db.flush()

    # thread the new observation id onto the walk's follow-ups
    walk.followUpActionIds = list(walk.followUpActionIds or []) + [obs.id]
    walk.observationsRaised = (walk.observationsRaised or 0) + 1

    linked_capa_id = None
    if body.spawnCapa:
        try:
            from app.services.capa_spawn import spawn_capa

            capa = await spawn_capa(
                db, source_code="SAFETY_CULTURE", plant_id=walk.plantId,
                title=f"Leadership-walk hazard: {obs.number}",
                problem=body.description or "Hazard identified during a leadership safety walk.",
                ref_id=obs.id, ref_url=f"/observations/{obs.id}", ref_summary=obs.number,
                detected_method="LEADERSHIP_WALK", owner_id=walk.leaderId, actor_id=user.id, due_days=30,
            )
            if capa:
                linked_capa_id = capa.id
                obs.capaId = capa.id
                db.add(CultureObservationClosure(observationId=obs.id, plantId=walk.plantId, linkedCapaId=capa.id))
        except Exception:
            # CAPA source may not be seeded — degrade to a Logged observation.
            linked_capa_id = None
    await db.commit()
    return {"observationId": obs.id, "number": number, "linkedCapaId": linked_capa_id, "walkId": walk_id}


@router.get("/leadership-walks/compliance/{plant_id}")
async def walk_compliance(plant_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "SAFETY_CULTURE.READ", plant_id)
    await _accessible_or_403(db, user, plant_id)
    return await svc.leadership_compliance(db, plant_id)


@router.get("/leadership-walks/scorecard/{leader_id}")
async def walk_scorecard(leader_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    # A leader may view their own; a manager/HSE with READ may view others.
    if leader_id != user.id:
        await _require(db, user, "SAFETY_CULTURE.READ")
    return await svc.leader_scorecard(db, leader_id)


@router.get("/leadership-walks/compliance-rollup")
async def walk_compliance_rollup(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """§Fix 7 — walk compliance ranked across every site in the caller's scope."""
    await _require(db, user, "SAFETY_CULTURE.READ")
    return await svc.walk_compliance_rollup(db, await _scoped_plants(db, user))


# ════════════════════════════════════════════════════════════════════════════
# §Fix 2 Leading / Lagging Ratio
# ════════════════════════════════════════════════════════════════════════════
@router.get("/leading-lagging-rollup")
async def leading_lagging_rollup(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """§Fix 7 — leading:lagging ratio ranked across every site in the caller's scope."""
    await _require(db, user, "SAFETY_CULTURE.READ")
    return await svc.leading_lagging_rollup(db, await _scoped_plants(db, user))


@router.get("/leading-lagging/{plant_id}")
async def leading_lagging(plant_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "SAFETY_CULTURE.READ", plant_id)
    await _accessible_or_403(db, user, plant_id)
    return await svc.leading_lagging_detail(db, plant_id)


# ════════════════════════════════════════════════════════════════════════════
# §4 Perception surveys
# ════════════════════════════════════════════════════════════════════════════
@router.get("/perception-surveys/templates")
async def list_templates(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "SAFETY_CULTURE.READ")
    rows = (await db.execute(select(PerceptionSurveyTemplate).order_by(PerceptionSurveyTemplate.createdAt.desc()))).scalars().all()
    return {"items": [
        {"id": t.id, "name": t.name, "description": t.description, "industryVertical": t.industryVertical,
         "cadence": t.cadence, "isActive": t.isActive, "questions": t.questions}
        for t in rows
    ]}


@router.post("/perception-surveys/templates", status_code=status.HTTP_201_CREATED)
async def create_template(body: SurveyTemplateCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "SAFETY_CULTURE.SURVEY_ADMIN")
    t = PerceptionSurveyTemplate(
        name=body.name, description=body.description, industryVertical=body.industryVertical,
        cadence=body.cadence, questions=[q.model_dump() for q in body.questions], createdById=user.id, isActive=True,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return {"id": t.id, "name": t.name}


@router.post("/perception-surveys/{template_id}/respond", status_code=status.HTTP_201_CREATED)
async def submit_response(template_id: str, body: SurveyResponseSubmit, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """Anonymous submission. We record only a one-way token (no identity linkage)
    to prevent double-submit; the raw response is never attributable to a person."""
    template = await db.get(PerceptionSurveyTemplate, template_id)
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Survey template not found")
    token = svc.anonymous_token(user.id)
    existing = (
        await db.execute(
            select(PerceptionSurveyResponse.id)
            .where(PerceptionSurveyResponse.surveyTemplateId == template_id)
            .where(PerceptionSurveyResponse.plantId == body.plantId)
            .where(PerceptionSurveyResponse.period == body.period)
            .where(PerceptionSurveyResponse.respondentAnonymousToken == token)
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "You have already responded to this survey for this period.")
    resp = PerceptionSurveyResponse(
        surveyTemplateId=template_id, plantId=body.plantId, period=body.period,
        respondentAnonymousToken=token, responses=[a.model_dump() for a in body.responses],
    )
    db.add(resp)
    await db.commit()
    # recompute the index (only publishes if the response threshold is met)
    try:
        await svc.compute_perception_index(db, body.plantId, body.period, publish=True)
        await db.commit()
    except Exception:
        await db.rollback()
    return {"status": "recorded", "anonymous": True}


@router.get("/perception-surveys/index/{plant_id}/{period}")
async def perception_index(plant_id: str, period: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "SAFETY_CULTURE.READ", plant_id)
    await _accessible_or_403(db, user, plant_id)
    result = await svc.compute_perception_index(db, plant_id, period, publish=False)
    if not result["thresholdMet"]:
        # Suppress dimension scores below the statistical/anonymity threshold.
        return {
            "plantId": plant_id, "period": period, "thresholdMet": False,
            "responseCount": result["responseCount"], "responseRatePercent": result["responseRatePercent"],
            "message": "Below the minimum response threshold — index withheld to protect anonymity and statistical validity.",
        }
    return result


@router.get("/perception-surveys/trend/{plant_id}")
async def perception_trend(plant_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """§Fix 4 — dimension-level trend across every threshold-met period + directional
    cross-site benchmark. Anonymity is preserved: only already-published (threshold-met)
    snapshots are read, never raw responses."""
    await _require(db, user, "SAFETY_CULTURE.READ", plant_id)
    await _accessible_or_403(db, user, plant_id)
    return await svc.perception_trend(db, plant_id)


@router.get("/perception-surveys/rollup")
async def perception_rollup(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """§Fix 7 — latest perception composite ranked across every site in scope."""
    await _require(db, user, "SAFETY_CULTURE.READ")
    return await svc.perception_rollup(db, await _scoped_plants(db, user))


@router.get("/perception-surveys/response-rate/{plant_id}")
async def perception_rate(plant_id: str, period: str | None = None, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "SAFETY_CULTURE.READ", plant_id)
    await _accessible_or_403(db, user, plant_id)
    period = period or await _current_quarter()
    result = await svc.compute_perception_index(db, plant_id, period, publish=False)
    return {
        "plantId": plant_id, "period": period, "responseCount": result["responseCount"],
        "responseRatePercent": result["responseRatePercent"], "thresholdMet": result["thresholdMet"],
    }


# ════════════════════════════════════════════════════════════════════════════
# §6 Recognition
# ════════════════════════════════════════════════════════════════════════════
@router.get("/recognition/leaderboard/{plant_id}/{period}")
async def recognition_leaderboard(plant_id: str, period: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "SAFETY_CULTURE.READ", plant_id)
    await _accessible_or_403(db, user, plant_id)
    return await svc.leaderboard(db, plant_id, period)


@router.get("/recognition/rollup/{period}")
async def recognition_rollup(period: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """§Fix 7 — recognition totals ranked across every site in scope (integrity-gated)."""
    await _require(db, user, "SAFETY_CULTURE.READ")
    return await svc.recognition_rollup(db, await _scoped_plants(db, user), period)


@router.get("/recognition/streaks/{user_id}")
async def recognition_streaks(user_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    if user_id != user.id:
        await _require(db, user, "SAFETY_CULTURE.READ")
    return await svc.user_streaks(db, user_id)


@router.post("/recognition/award")
async def recognition_award(body: RecognitionAwardRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _require(db, user, "SAFETY_CULTURE.RECALC", body.plantId)
    await _accessible_or_403(db, user, body.plantId)
    period = body.period or _now().strftime("%Y-%m")
    res = await svc.award_recognition(db, body.plantId, period)
    await db.commit()
    return res


# ════════════════════════════════════════════════════════════════════════════
# §5 ERM / KRI wiring
# ════════════════════════════════════════════════════════════════════════════
@router.post("/kri/register")
async def register_kris(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """One-time setup: wire culture scores into the ERM KRI engine (admin only)."""
    await _require(db, user, "SAFETY_CULTURE.ADMIN")
    res = await svc.register_culture_kris(db, actor_id=user.id)
    await db.commit()
    return res
