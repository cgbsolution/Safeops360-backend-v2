"""Background job scheduler (P2-1) — dependency-free asyncio supervisor (no
APScheduler/Celery needed for the single-process on-prem deployment; mirrors the
existing _licence_recheck_loop pattern).

Every registered job:
  • wraps an idempotent service function
  • runs in its own DB session with a SystemActor (audit attribution + scoped jobs)
  • records a JobRun (RUNNING → SUCCESS/FAILED + summary + recordsAffected)
  • is callable on-demand (run-now) AND on its interval
Misfire recovery: on startup the supervisor reads the last successful JobRun per
job; an overdue job runs once (not N times).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select, text

from app.core.db import AsyncSessionLocal

log = logging.getLogger("safeops360.scheduler")

HOUR = 3600
DAY = 24 * HOUR


@dataclass
class Job:
    id: str
    label: str
    interval_seconds: int
    fn: Callable[[Any], Awaitable[dict]]  # async (db) -> summary dict


# ── Job implementations (each idempotent; opens nothing — gets the db) ───────
async def _job_kri_feeds(db) -> dict:
    from app.services.erm_p2 import run_module_fed
    return await run_module_fed(db)


async def _job_appetite_eval(db) -> dict:
    from app.services.erm_p2 import evaluate_appetite
    return await evaluate_appetite(db)


async def _job_loss_auto_feed(db) -> dict:
    from app.services.erm_p2 import auto_feed_incidents
    return await auto_feed_incidents(db, actor_id="SYSTEM:loss_auto_feed")


async def _job_rollup(db) -> dict:
    from app.models.erm import RollupRule
    from app.services.erm import run_rollup_rule
    rules = (await db.execute(select(RollupRule).where(RollupRule.isActive.is_(True)).where(RollupRule.isDeleted.is_(False)))).scalars().all()
    created = updated = 0
    for r in rules:
        res = await run_rollup_rule(db, r)
        created += res.get("created", 0)
        updated += res.get("updated", 0)
    return {"rules": len(rules), "created": created, "updated": updated}


async def _job_incident_alerts(db) -> dict:
    from app.services.erm import sync_incident_risk_alerts
    return await sync_incident_risk_alerts(db)


async def _job_cams_repeats(db) -> dict:
    from app.services.cams_analytics import detect_repeat_findings
    return await detect_repeat_findings(db)


async def _job_fire_status(db) -> dict:
    from app.services.fire_safety import recompute_all_statuses
    return await recompute_all_statuses(db)


async def _job_compliance(db) -> dict:
    from app.services.erm_p2 import generate_tasks, refresh_statuses
    a = await refresh_statuses(db)
    b = await generate_tasks(db)
    return {**a, **b}


async def _job_audit_integrity(db) -> dict:
    """Sample the audit chain: verify the most-recently-touched entities."""
    from app.models.audit_log import AuditLog
    from app.services.audit_log import verify_chain
    pairs = (
        await db.execute(select(AuditLog.entityType, AuditLog.entityId).distinct().limit(200))
    ).all()
    checked = broken = 0
    for et, eid in pairs:
        v = await verify_chain(db, et, eid)
        checked += 1
        if not v["intact"]:
            broken += 1
            log.warning("audit chain BROKEN for %s/%s at seq %s", et, eid, v["brokenAtSequence"])
    return {"chainsChecked": checked, "chainsBroken": broken}


async def _job_treatment_reminders(db) -> dict:
    from app.services.erm_notifications import run_treatment_pre_due_reminders
    return await run_treatment_pre_due_reminders(db)


async def _job_treatment_overdue(db) -> dict:
    from app.services.erm_notifications import run_treatment_overdue_escalations
    return await run_treatment_overdue_escalations(db)


async def _job_moc_temp_expiry(db) -> dict:
    from app.services.moc_notifications import run_moc_temp_expiry_reminders
    return await run_moc_temp_expiry_reminders(db)


async def _job_moc_approval_escalations(db) -> dict:
    from app.services.moc_notifications import run_moc_approval_escalations
    return await run_moc_approval_escalations(db)


async def _job_treatment_reconcile(db) -> dict:
    """Auto-reconcile closed treatments (§7d) — measure achieved-vs-expected residual
    reduction and escalate overdue treatments up the ladder."""
    from app.services.erm import escalate_overdue_treatments, reconcile_treatment_closures
    a = await reconcile_treatment_closures(db)
    b = await escalate_overdue_treatments(db)
    return {"reconciled": a.get("reconciled", 0), "escalated": b.get("escalated", 0)}


async def _job_capture_voice(db) -> dict:
    """Guided Field Capture — voice transcription (provider abstraction, stub
    default) + English translation of any transcript. Never blocks submission."""
    from app.services.capture_voice import run_capture_voice
    return await run_capture_voice(db)


async def _job_alerts_resolver(db) -> dict:
    """Daily Alert Brief — consume unprocessed DomainEvents through the impact
    rule registry and materialise Alert cards (never computed at read time)."""
    from app.services.alerts import resolve_pending_events
    return await resolve_pending_events(db)


async def _job_ptw_expiry_scan(db) -> dict:
    """T-24h ptw.expiring events + server-side expiry flip (the lazy /inbox
    sweep is no longer the only thing that expires permits)."""
    from app.services.alerts.scans import run_ptw_expiry_scan
    return await run_ptw_expiry_scan(db)


async def _job_capa_overdue_scan(db) -> dict:
    """Daily capa.overdue events off the query-time overdue predicate, with
    source-severity enrichment for the alert rule."""
    from app.services.alerts.scans import run_capa_overdue_scan
    return await run_capa_overdue_scan(db)


async def _job_alert_digest(db) -> dict:
    """06:00 site-local Daily Brief email digest. Runs every 15 min and fires a
    subscription only inside its local 06:00-06:29 window (deduped per day)."""
    from app.services.alerts.digest import run_alert_digest
    return await run_alert_digest(db)


async def _job_sentinel_scan(db) -> dict:
    """Executive Sentinel — run the deterministic AI Insights engine across every
    module + plant and materialise the predictive/leading-indicator cards as Alert
    rows (scored by the Brief Priority Score), firing an in-platform push on each
    new critical. Recomputes nothing; consumes the insight engine."""
    from app.services.sentinel import run_sentinel_scan
    return await run_sentinel_scan(db)


async def _job_culture_recalc(db) -> dict:
    """Recompute every site's Safety Culture maturity score (five components) +
    award quality-weighted recognition. The culture KRIs then feed off the
    refreshed profiles via the hourly kri_module_feeds job."""
    from app.services.safety_culture import recalculate_all
    return await recalculate_all(db)


async def _job_culture_walk_reminders(db) -> dict:
    """§Fix 8 — T-2-day leadership-walk reminders to the leader (deduped)."""
    from app.services.safety_culture import run_walk_reminders
    return await run_walk_reminders(db)


async def _job_culture_survey_launch(db) -> dict:
    """§Fix 8 — keep a perception survey window open each cadence period."""
    from app.services.safety_culture import run_survey_launch
    return await run_survey_launch(db)


async def _job_culture_band_breach(db) -> dict:
    """§Fix 8 — flag + escalate sites whose maturity stage-band regressed."""
    from app.services.safety_culture import run_band_breach_scan
    return await run_band_breach_scan(db)


async def _job_training_engine_resolver(db) -> dict:
    """Training & Competency Engine — drain the TrainingTriggerEvent outbox
    (Incident/Near Miss/Observation classification saves) through the severity +
    threshold rules and materialise TrainingAssignments (spec §B, "run as a
    background job … not synchronously in the request path")."""
    from app.services.training_engine.service import drain_trigger_events
    return await drain_trigger_events(db)


async def _job_deroster_escalation(db) -> dict:
    """Observation deroster — escalate reviews whose SLA has expired.

    Escalates once per record and never decides: an unattended review keeps the
    worker soft-locked until a human confirms or overrules (spec §2.5, "silence
    should escalate, not decide"). Runs every 15 min because the default review
    SLA is 4 hours — an hourly tick would burn a quarter of the window."""
    from app.services.observation_deroster import run_escalation_scan, sync_daily_brief_cards
    out = await run_escalation_scan(db)
    # Same rows, same tick — keeping the Daily Brief cards in step with the
    # escalation state avoids a second scan over the same table.
    cards = await sync_daily_brief_cards(db)
    out["summary"] = f"{out['summary']}; brief cards {cards['created']}+/{cards['resolved']}-"
    return out


async def _job_training_recert_scan(db) -> dict:
    """Recertification rule — competency expiry approaching → auto-assign a
    refresher, independent of incident triggers (spec §B rule 4)."""
    from app.services.training_engine.service import run_recert_scan
    return await run_recert_scan(db)


async def _job_training_overdue_scan(db) -> dict:
    """Flip past-due training assignments to overdue + notify the worker."""
    from app.services.training_engine.service import run_overdue_scan
    return await run_overdue_scan(db)


async def _job_training_correlation_scan(db) -> dict:
    """Fill the post-training re-incident window on correlation points once it
    elapses + emit the Daily Brief 'correlation' card (spec §D)."""
    from app.services.training_engine.correlation import run_correlation_scan
    return await run_correlation_scan(db)


async def _job_person_risk_scan(db) -> dict:
    """Person-risk analytics — aggregate every worker's incidents/near-misses/
    observations 'against their name', auto-flag repeat-involved people into the
    training module, and assign the training their events point to."""
    from app.services.training_engine.person_risk import run_person_risk_scan
    return await run_person_risk_scan(db)


async def _job_observation_weekly_insights(db) -> dict:
    """Weekly Insight Engine — recompute the Safety Observations 'This week's
    focus' hero + secondary row and persist the InsightSnapshot lifecycle for the
    current week (idempotent per weekOf). Deterministic; no model calls. Interval
    is daily so a missed Sunday still lands; the weekOf key makes re-runs update
    rather than duplicate."""
    from app.services.insights.weekly import compute_weekly
    return await compute_weekly(db, module="safety_observation")


async def _job_calendar_booking_retry(db) -> dict:
    """Drain calendar bookings whose delivery has not landed yet.

    Booking a calendar is a network call to somebody else's Exchange, made from
    inside the transaction that schedules an audit. That call is allowed to fail
    — the audit must not — so a failed booking stays PENDING and this job is what
    eventually gets it delivered. Only future bookings are retried; an invitation
    for a window that has already closed cannot block anything.
    """
    from app.services.calendar_booking import run_booking_retry
    return await run_booking_retry(db)


async def _job_chemical_sds_review(db) -> dict:
    """Nightly SDS review sweep (Chemical module §4.5).

    Flags chemicals whose SDS review date has passed and clears the flag on any
    whose sheet has since been renewed. It deliberately does NOT deactivate a
    chemical: business rule §6 makes an overdue review a visible compliance
    signal rather than an operational stop, because hard-deactivating every
    chemical with stale paperwork would halt production over a filing lapse —
    and the predictable response to that is someone disabling this job.
    """
    from app.services.chemical_sds import flag_overdue_sds_reviews
    return await flag_overdue_sds_reviews(db)


async def _job_chemical_threshold_sweep(db) -> dict:
    """Full-estate threshold re-evaluation (Chemical module §4.3).

    The threshold engine is edge-triggered on every receipt and transfer, so
    this job is a safety net rather than the primary path. It exists because the
    hot path can only see movements that happened: a threshold rule EDITED by an
    admin (a tightened GCC limit, say) changes which sites are in breach without
    any inventory moving at all, and nothing else would notice.
    """
    from sqlalchemy import select

    from app.models.chemical import ChemicalInventoryItem
    from app.services.chemical_threshold import evaluate_thresholds

    pairs = (
        await db.execute(
            select(ChemicalInventoryItem.tenantId, ChemicalInventoryItem.plantId)
            .where(ChemicalInventoryItem.isDeleted.is_(False))
            .where(ChemicalInventoryItem.quantityLedger > 0)
            .distinct()
        )
    ).all()
    breached = 0
    for tenant_id, plant_id in pairs:
        evals = await evaluate_thresholds(db, tenant_id=tenant_id, plant_id=plant_id)
        breached += sum(1 for e in evals if e.status == "BREACHED")
    await db.commit()
    return {"sitesEvaluated": len(pairs), "rulesBreached": breached}


JOBS: dict[str, Job] = {j.id: j for j in [
    Job("kri_module_feeds", "KRI module feeds", 1 * HOUR, _job_kri_feeds),
    Job("treatment_pre_due_reminders", "Risk treatment pre-due reminders", 1 * DAY, _job_treatment_reminders),
    Job("treatment_overdue_escalations", "Risk treatment overdue escalations", 6 * HOUR, _job_treatment_overdue),
    Job("treatment_reconcile", "Treatment residual reconcile + escalation ladder", 6 * HOUR, _job_treatment_reconcile),
    Job("moc_temp_expiry_reminders", "MOC temporary-change expiry reminders", 1 * DAY, _job_moc_temp_expiry),
    Job("moc_approval_escalations", "MOC approval SLA escalations", 6 * HOUR, _job_moc_approval_escalations),
    Job("loss_auto_feed", "Incident → Loss Event auto-feed", 2 * HOUR, _job_loss_auto_feed),
    Job("erm_rollup", "HIRA/EAI → CRR → ERM rollup", 4 * HOUR, _job_rollup),
    Job("appetite_eval", "Risk appetite breach evaluation", 4 * HOUR, _job_appetite_eval),
    Job("incident_risk_alerts", "Incident → ERM risk review flag (I-04)", 6 * HOUR, _job_incident_alerts),
    Job("cams_repeat_findings", "CAMS repeat-finding detection", 12 * HOUR, _job_cams_repeats),
    Job("fire_equipment_status", "Fire equipment status recompute", 1 * DAY, _job_fire_status),
    Job("chemical_sds_review", "Chemical SDS review-due sweep", 1 * DAY, _job_chemical_sds_review),
    Job("chemical_threshold_sweep", "Chemical regulatory-threshold re-evaluation", 6 * HOUR, _job_chemical_threshold_sweep),
    Job("compliance_tasks", "Compliance status + task generation", 1 * DAY, _job_compliance),
    Job("audit_trail_integrity", "Audit-trail hash-chain integrity check", 7 * DAY, _job_audit_integrity),
    Job("capture_voice_pipeline", "Field-capture voice transcription + translation", HOUR // 4, _job_capture_voice),
    Job("alerts_impact_resolver", "Daily Brief — impact-rule event resolver", 60, _job_alerts_resolver),
    Job("ptw_expiry_scan", "PTW T-24h expiring events + expiry flip", 1 * HOUR, _job_ptw_expiry_scan),
    Job("capa_overdue_scan", "CAPA overdue events (daily scan)", 1 * DAY, _job_capa_overdue_scan),
    Job("alert_digest", "Daily Brief 06:00 site-local email digest", 15 * 60, _job_alert_digest),
    Job("sentinel_scan", "Executive Sentinel — insight→Alert materialise + critical push", 5 * 60, _job_sentinel_scan),
    Job("culture_recalc", "Safety Culture maturity recompute + recognition", 1 * DAY, _job_culture_recalc),
    Job("culture_walk_reminders", "Culture — T-2d leadership walk reminders", 1 * DAY, _job_culture_walk_reminders),
    Job("culture_survey_launch", "Culture — perception survey window launch", 1 * DAY, _job_culture_survey_launch),
    Job("culture_band_breach", "Culture — stage-band regression escalation", 6 * HOUR, _job_culture_band_breach),
    Job("training_engine_resolver", "Training Engine — trigger→assignment resolver", 60, _job_training_engine_resolver),
    Job("training_recert_scan", "Training — recertification refresher scan", 1 * DAY, _job_training_recert_scan),
    Job("training_overdue_scan", "Training — overdue assignment scan", 6 * HOUR, _job_training_overdue_scan),
    Job("training_correlation_scan", "Training — re-incident correlation scan + Daily Brief card", 1 * DAY, _job_training_correlation_scan),
    Job("person_risk_scan", "Training — person-risk repeat-involvement flag + auto-assign", 6 * HOUR, _job_person_risk_scan),
    Job("observation_weekly_insights", "Weekly Insight Engine — observation hero + row recompute", 1 * DAY, _job_observation_weekly_insights),
    Job("deroster_escalation", "Observation — deroster review SLA escalation", 15 * 60, _job_deroster_escalation),
    # 15 min: an invite that arrives a quarter of an hour late is fine; one that
    # arrives the next day is not, because the person it blocks has already
    # booked something else over the audit.
    Job("calendar_booking_retry", "CAMS — calendar booking delivery retry", 15 * 60, _job_calendar_booking_retry),
]}


async def _last_success(db, job_id: str) -> datetime | None:
    from app.models.job_run import JobRun
    row = (
        await db.execute(
            select(JobRun.finishedAt).where(JobRun.jobId == job_id).where(JobRun.status == "SUCCESS")
            .order_by(JobRun.startedAt.desc()).limit(1)
        )
    ).first()
    return row[0] if row and row[0] else None


def _advisory_key(job_id: str) -> int:
    """Stable 63-bit signed int key for a Postgres advisory lock, per job_id."""
    return int.from_bytes(hashlib.sha256(job_id.encode()).digest()[:8], "big", signed=True)


async def run_job(job_id: str, trigger: str = "MANUAL") -> dict[str, Any]:
    """Execute one job in its own session + JobRun record. Never raises.

    Leader election for SCHEDULED runs: when several app replicas each run their
    own supervisor loop, only ONE may execute a given job at a time — otherwise
    escalation/digest emails double-send. We take a session-level Postgres
    advisory lock keyed by job_id and hold its transaction OPEN for the whole
    run (never committing the lock session), which pins the server backend so
    the lock survives PgBouncer transaction pooling. MANUAL/API triggers bypass
    the lock so an operator can always force a run.
    """
    job = JOBS.get(job_id)
    if job is None:
        return {"error": f"unknown job {job_id}"}

    if trigger != "SCHEDULED":
        return await _execute_job(job_id, job, trigger)

    key = _advisory_key(job_id)
    async with AsyncSessionLocal() as lock_db:
        got = (await lock_db.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})).scalar()
        if not got:
            log.info("job %s skipped — advisory lock held by another instance", job_id)
            return {"jobId": job_id, "status": "SKIPPED", "reason": "locked by another instance"}
        try:
            return await _execute_job(job_id, job, trigger)
        finally:
            try:
                await lock_db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            except Exception:  # noqa: BLE001
                pass


async def _execute_job(job_id: str, job: Job, trigger: str) -> dict[str, Any]:
    from app.core.audit_context import set_system_actor
    from app.models.job_run import JobRun

    set_system_actor(job_id)
    started = datetime.now(timezone.utc).replace(tzinfo=None)
    run_id = None
    async with AsyncSessionLocal() as db:
        jr = JobRun(jobId=job_id, startedAt=started, status="RUNNING", trigger=trigger)
        db.add(jr)
        await db.commit()
        run_id = jr.id
    try:
        async with AsyncSessionLocal() as db:
            summary = await job.fn(db)
            await db.commit()
        status, error = "SUCCESS", None
    except Exception as e:  # noqa: BLE001
        summary, status, error = {}, "FAILED", str(e)[:500]
        log.warning("job %s FAILED: %s", job_id, e)
    finally:
        async with AsyncSessionLocal() as db:
            jr = await db.get(JobRun, run_id)
            if jr:
                jr.status = status
                jr.finishedAt = datetime.now(timezone.utc).replace(tzinfo=None)
                jr.summary = summary
                jr.error = error
                jr.recordsAffected = next((summary.get(k) for k in ("written", "created", "updated", "flagged", "evaluated", "statusChanged") if isinstance(summary, dict) and summary.get(k) is not None), None)
                await db.commit()
    return {"jobId": job_id, "status": status, "summary": summary, "error": error}


async def supervisor_loop(stop: asyncio.Event) -> None:
    """Single supervisor — every 60s, run any job whose interval has elapsed since
    its last success. Staggered naturally by differing intervals."""
    next_due: dict[str, datetime] = {}
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        for jid, job in JOBS.items():
            last = await _last_success(db, jid)
            last_aware = (last.replace(tzinfo=timezone.utc) if last and last.tzinfo is None else last) if last else None
            next_due[jid] = (last_aware + timedelta(seconds=job.interval_seconds)) if last_aware else now
    while not stop.is_set():
        now = datetime.now(timezone.utc)
        for jid, job in JOBS.items():
            if now >= next_due.get(jid, now):
                await run_job(jid, trigger="SCHEDULED")
                next_due[jid] = datetime.now(timezone.utc) + timedelta(seconds=job.interval_seconds)
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
