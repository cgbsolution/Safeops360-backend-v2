"""Correlation reporting (spec §D) — the defensible data asset.

For each skill node (competency) at each site, compare the re-incident rate in
the window BEFORE a triggered training completion vs the window AFTER. No content
vendor can produce this — it joins training completion to the incident data the
platform owns. Deterministic + airgap-safe (plain counting; no model calls).

  • log_completion       — write a TrainingCorrelationPoint at completion time,
                           capturing the pre-window re-incident count
  • run_correlation_scan — fill postWindowCount once the window elapses + emit a
                           Daily Brief "correlation" card (Alert row, sentinel kind)
  • compute_report       — aggregate points into the before/after report the UI +
                           Daily Brief consume
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competency_matrix import Competency
from app.models.training_engine import TrainingAssignment, TrainingCorrelationPoint
from app.services.training_engine import resolver
from app.services.training_engine.classify import build_classification_light, mapping_matches
from app.services.training_engine.config import resolve_config
from app.services.training_engine.resolver import now_utc


def _model_for(module: str):
    if module == "OBSERVATION":
        from app.models.observation import Observation

        return Observation
    if module == "NEAR_MISS":
        from app.models.near_miss import NearMiss

        return NearMiss
    if module == "INCIDENT":
        from app.models.incident import Incident

        return Incident
    return None


async def _count_mapped_between(db: AsyncSession, *, competency_id: str, plant_id: str, start, end) -> int:
    """Count records mapped to the competency at the plant with createdAt in
    [start, end). Same mapping-match logic as the threshold rule, windowed."""
    mappings = await resolver.mappings_for_competency(db, competency_id=competency_id, plant_id=plant_id)
    if not mappings:
        return 0
    modules = {m.sourceModule for m in mappings}
    if "ANY" in modules:
        modules = {"INCIDENT", "NEAR_MISS", "OBSERVATION"}
    count = 0
    for mod in modules:
        model = _model_for(mod)
        if model is None:
            continue
        stmt = select(model).where(model.plantId == plant_id).where(model.createdAt >= start).where(model.createdAt < end)
        if hasattr(model, "isDeleted"):
            stmt = stmt.where(model.isDeleted.is_(False))
        records = (await db.execute(stmt)).scalars().all()
        applicable = [m for m in mappings if m.sourceModule in (mod, "ANY")]
        for r in records:
            cls = build_classification_light(mod, r)
            if any(mapping_matches(m.classificationField, m.classificationValue, m.matchMode, cls) for m in applicable):
                count += 1
    return count


async def log_completion(db: AsyncSession, a: TrainingAssignment) -> TrainingCorrelationPoint:
    """Log one correlation data point at training completion (spec workflow 2)."""
    config = await resolve_config(db, a.plantId)
    window = config.correlationWindowDays
    completed = a.completedAt or now_utc()
    pre = await _count_mapped_between(
        db,
        competency_id=a.competencyId,
        plant_id=a.plantId,
        start=completed - timedelta(days=window),
        end=completed,
    )
    point = TrainingCorrelationPoint(
        plantId=a.plantId,
        competencyId=a.competencyId,
        personUserId=a.personUserId,
        assignmentId=a.id,
        sourceModule=a.sourceModule,
        sourceRecordId=a.sourceRecordId,
        sourceRecordRef=a.sourceRecordRef,
        trainingCompletedAt=completed,
        windowDays=window,
        preWindowCount=pre,
        postWindowCount=None,
        computedAt=None,
    )
    db.add(point)
    await db.flush()
    return point


async def run_correlation_scan(db: AsyncSession) -> dict:
    """Fill postWindowCount for points whose window has elapsed, then emit a Daily
    Brief 'correlation' card per (plant, competency) that shows a clear signal."""
    now = now_utc()
    pending = (
        await db.execute(
            select(TrainingCorrelationPoint).where(TrainingCorrelationPoint.postWindowCount.is_(None))
        )
    ).scalars().all()

    filled = 0
    touched: set[tuple[str, str]] = set()
    for p in pending:
        if p.trainingCompletedAt + timedelta(days=p.windowDays) > now:
            continue  # window not elapsed yet
        end = p.trainingCompletedAt + timedelta(days=p.windowDays)
        p.postWindowCount = await _count_mapped_between(
            db, competency_id=p.competencyId, plant_id=p.plantId, start=p.trainingCompletedAt, end=end
        )
        p.computedAt = now
        filled += 1
        touched.add((p.plantId, p.competencyId))

    cards = 0
    for plant_id, competency_id in touched:
        try:
            if await _emit_correlation_card(db, plant_id, competency_id):
                cards += 1
        except Exception:  # noqa: BLE001
            pass

    await db.commit()
    return {"filled": filled, "cards": cards}


async def _emit_correlation_card(db: AsyncSession, plant_id: str, competency_id: str) -> bool:
    from app.services.alerts import AlertDraft, materialise

    agg = await _aggregate(db, plant_id=plant_id, competency_id=competency_id)
    if agg is None or agg["cohortSize"] < 1 or (agg["preTotal"] + agg["postTotal"]) < 1:
        return False
    comp = await db.get(Competency, competency_id)
    name = comp.name if comp else competency_id
    pre, post = agg["preTotal"], agg["postTotal"]
    if post < pre:
        sev, verb = "info", "fell"
    elif post > pre:
        sev, verb = "attention", "rose"
    else:
        return False  # no change → no card
    delta_pct = agg["improvementPct"]
    draft = AlertDraft(
        severity=sev,
        title=f"Training impact: re-incidents {verb} {pre}→{post} after '{name}'",
        body_text=(
            f"Across {agg['cohortSize']} completion(s) of '{name}', mapped re-incidents "
            f"{verb} from {pre} to {post} in the {agg['windowDays']}-day windows."
        ),
        dedupe_key=f"training.correlation:{plant_id}:{competency_id}",
        site_id=plant_id,
        body_params={
            "source": "sentinel",
            "kind": "correlation",
            "competency": name,
            "competencyId": competency_id,
            "pre": pre,
            "post": post,
            "improvementPct": delta_pct,
            "cohortSize": agg["cohortSize"],
            "suggestedAction": (
                "Sustain and widen this training — the data shows it is reducing recurrence."
                if post < pre
                else "Re-incidents rose after training — review content effectiveness and root causes."
            ),
        },
        deep_link=f"/skill-matrix/correlation?competency={competency_id}",
        audience_roles=["HSE_MANAGER", "PLANT_HEAD", "LD_MANAGER"],
    )
    await materialise(db, draft)
    return True


async def _aggregate(db: AsyncSession, *, plant_id: str, competency_id: str) -> dict | None:
    points = (
        await db.execute(
            select(TrainingCorrelationPoint)
            .where(TrainingCorrelationPoint.plantId == plant_id)
            .where(TrainingCorrelationPoint.competencyId == competency_id)
            .where(TrainingCorrelationPoint.postWindowCount.is_not(None))
        )
    ).scalars().all()
    if not points:
        return None
    pre_total = sum(p.preWindowCount for p in points)
    post_total = sum(p.postWindowCount or 0 for p in points)
    improvement = round((pre_total - post_total) / pre_total * 100, 1) if pre_total else None
    return {
        "plantId": plant_id,
        "competencyId": competency_id,
        "cohortSize": len(points),
        "preTotal": pre_total,
        "postTotal": post_total,
        "improvementPct": improvement,
        "windowDays": points[0].windowDays,
    }


async def compute_report(db: AsyncSession, *, plant_ids: list[str] | None, competency_id: str | None = None) -> list[dict]:
    """The correlation report: one row per (plant, competency) with computed
    before/after re-incident totals. Reads only computed points (post filled)."""
    stmt = select(TrainingCorrelationPoint)
    if plant_ids is not None:
        stmt = stmt.where(TrainingCorrelationPoint.plantId.in_(plant_ids))
    if competency_id:
        stmt = stmt.where(TrainingCorrelationPoint.competencyId == competency_id)
    points = (await db.execute(stmt)).scalars().all()

    groups: dict[tuple[str, str], list[TrainingCorrelationPoint]] = {}
    for p in points:
        groups.setdefault((p.plantId, p.competencyId), []).append(p)

    comp_ids = list({cid for _p, cid in groups})
    comp_names: dict[str, str] = {}
    if comp_ids:
        comps = (await db.execute(select(Competency).where(Competency.id.in_(comp_ids)))).scalars().all()
        comp_names = {c.id: c.name for c in comps}

    out: list[dict] = []
    for (pid, cid), pts in groups.items():
        computed = [p for p in pts if p.postWindowCount is not None]
        pre_total = sum(p.preWindowCount for p in computed)
        post_total = sum(p.postWindowCount or 0 for p in computed)
        improvement = round((pre_total - post_total) / pre_total * 100, 1) if pre_total else None
        out.append(
            {
                "plantId": pid,
                "competencyId": cid,
                "competencyName": comp_names.get(cid, cid),
                "cohortSize": len(pts),
                "computedCohortSize": len(computed),
                "preTotal": pre_total,
                "postTotal": post_total,
                "improvementPct": improvement,
                "windowDays": pts[0].windowDays,
                "pending": len(pts) - len(computed),
            }
        )
    out.sort(key=lambda r: (r["improvementPct"] is None, -(r["improvementPct"] or 0)))
    return out


__all__ = ["log_completion", "run_correlation_scan", "compute_report", "_aggregate"]
