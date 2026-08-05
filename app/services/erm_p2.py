"""ERM Phase 2 engines — KRI status/breach, appetite breach detection,
compliance task/status, loss auto-feed + calibration.

No scheduler in the platform, so each "nightly job" is a service function callable
on-demand via a /run endpoint (same pattern as the Phase 1 rollup engine).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.erm import EnterpriseRisk, RiskCategory
from app.models.erm_p2 import (
    AppetiteBreach,
    AppetiteStatement,
    ComplianceTask,
    KriBreachEvent,
    KriDefinition,
    KriReading,
    LegalObligation,
    LossEvent,
)
from app.models.user import User


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


# ════════════════════════════════════════════════════════════════════════════
# KRI status + readings + breach
# ════════════════════════════════════════════════════════════════════════════
def kri_status(direction: str, value: float, green: float, amber: float) -> str:
    """GREEN/AMBER/RED for a value, direction-aware. Boundaries inclusive on the
    safer side (value == green → GREEN, value == amber → AMBER)."""
    if direction == "LOWER_IS_WORSE":
        if value >= green:
            return "GREEN"
        if value >= amber:
            return "AMBER"
        return "RED"
    # HIGHER_IS_WORSE (default)
    if value <= green:
        return "GREEN"
    if value <= amber:
        return "AMBER"
    return "RED"


async def record_reading(
    db: AsyncSession, kri: KriDefinition, period_label: str, period_end: datetime,
    value: float, source: str, entered_by: str | None = None, notes: str = "",
) -> KriReading:
    """Upsert a reading for (kri, period); recompute status, current flag, the
    KRI's denormalised status, and breach events. Caller commits."""
    status = kri_status(kri.direction, value, kri.thresholdGreen, kri.thresholdAmber)
    existing = (
        await db.execute(select(KriReading).where(KriReading.kriId == kri.id).where(KriReading.periodLabel == period_label))
    ).scalar_one_or_none()
    if existing:
        existing.value = value
        existing.status = status
        existing.source = source
        existing.notes = notes
        existing.periodEnd = period_end
        reading = existing
    else:
        reading = KriReading(
            kriId=kri.id, periodLabel=period_label, periodEnd=period_end, value=value,
            status=status, source=source, enteredBy=entered_by, notes=notes, isCurrent=False, createdBy=entered_by,
        )
        db.add(reading)
    await db.flush()

    # Recompute isCurrent = the reading with the latest periodEnd.
    rows = (await db.execute(select(KriReading).where(KriReading.kriId == kri.id))).scalars().all()
    latest = max(rows, key=lambda r: _aware(r.periodEnd) or _now())
    for r in rows:
        r.isCurrent = r.id == latest.id
    kri.currentStatus = latest.status
    kri.currentValue = latest.value

    # Breach handling on the current reading.
    if reading.id == latest.id and status in ("AMBER", "RED"):
        await _ensure_breach(db, kri, reading, status)
        if status == "RED":
            await _notify_kri_red(db, kri, reading)
    return reading


async def _ensure_breach(db: AsyncSession, kri: KriDefinition, reading: KriReading, breach_type: str) -> None:
    open_b = (
        await db.execute(
            select(KriBreachEvent).where(KriBreachEvent.kriId == kri.id).where(KriBreachEvent.status != "RESOLVED")
        )
    ).scalar_one_or_none()
    if open_b:
        open_b.breachType = breach_type
        open_b.readingId = reading.id
    else:
        db.add(KriBreachEvent(kriId=kri.id, readingId=reading.id, breachType=breach_type, status="OPEN"))


async def _notify_kri_red(db: AsyncSession, kri: KriDefinition, reading: KriReading) -> None:
    """RED → notify owner + Risk Champion + CRO. Linked-risk 'review recommended'
    badge is derived on the frontend from the linked KRI's currentStatus, so no
    Phase-1 schema mutation is needed here."""
    try:
        from app.models.user import Role, UserRole
        from app.services.notifications import send_email

        emails: set[str] = set()
        owner = await db.get(User, kri.ownerId)
        if owner and owner.email:
            emails.add(owner.email)
        cro_champ = (
            await db.execute(
                select(User.email).join(UserRole, UserRole.userId == User.id).join(Role, Role.id == UserRole.roleId)
                .where(Role.code.in_(("CRO", "RISK_CHAMPION")))
            )
        ).scalars().all()
        emails.update(e for e in cro_champ if e)
        if emails:
            await send_email(
                list(emails), subject=f"[ERM] KRI RED: {kri.kriCode} {kri.name}",
                body=f"KRI {kri.kriCode} '{kri.name}' is RED (value {reading.value} {kri.unit}). Linked enterprise risks flagged for review.",
            )
    except Exception:
        return


async def run_module_fed(db: AsyncSession, period_end: datetime | None = None) -> dict[str, Any]:
    """Generate readings for all active MODULE_FED KRIs via the metric catalogue."""
    from app.services.erm_metrics import METRIC_PROVIDERS

    period_end = period_end or _now()
    written, skipped = 0, 0
    period_label = period_end.strftime("%Y-%m")
    kris = (
        await db.execute(
            select(KriDefinition).where(KriDefinition.feedType == "MODULE_FED").where(KriDefinition.isActive.is_(True)).where(KriDefinition.isDeleted.is_(False))
        )
    ).scalars().all()
    for kri in kris:
        prov = METRIC_PROVIDERS.get(kri.metricProviderKey or "")
        if not prov:
            skipped += 1
            continue
        val = await prov.compute(db, period_end)
        if val is None:
            skipped += 1
            continue
        await record_reading(db, kri, period_label, period_end, float(val), "MODULE_FED")
        written += 1
    await db.flush()
    return {"written": written, "skipped": skipped, "period": period_label}


async def check_no_data(db: AsyncSession, as_of: datetime | None = None) -> dict[str, Any]:
    """Flag NO_DATA breaches for KRIs with no current reading past periodEnd+graceDays.
    First miss → NO_DATA breach + notify owner. Second consecutive miss (an OPEN
    NO_DATA breach already present) → escalate to CRO (T2-03)."""
    as_of = as_of or _now()
    flagged, escalated = 0, 0
    kris = (await db.execute(select(KriDefinition).where(KriDefinition.isActive.is_(True)).where(KriDefinition.isDeleted.is_(False)))).scalars().all()
    for kri in kris:
        latest = (
            await db.execute(select(KriReading).where(KriReading.kriId == kri.id).order_by(KriReading.periodEnd.desc()).limit(1))
        ).scalar_one_or_none()
        stale = latest is None or (_aware(latest.periodEnd) + timedelta(days=kri.graceDays)) < as_of
        if not stale:
            continue
        kri.currentStatus = "NO_DATA"
        existing = (
            await db.execute(select(KriBreachEvent).where(KriBreachEvent.kriId == kri.id).where(KriBreachEvent.breachType == "NO_DATA").where(KriBreachEvent.status != "RESOLVED"))
        ).scalar_one_or_none()
        if not existing:
            db.add(KriBreachEvent(kriId=kri.id, breachType="NO_DATA", status="OPEN"))
            flagged += 1
            await _notify_no_data(db, kri, to_cro=False)  # first miss → owner
        else:
            # second consecutive miss (breach already open) → escalate to CRO
            existing.resolutionNotes = (existing.resolutionNotes or "") + f" | escalated (2nd consecutive miss) {as_of.date().isoformat()}"
            escalated += 1
            await _notify_no_data(db, kri, to_cro=True)
    await db.flush()
    return {"flagged": flagged, "escalated": escalated}


async def _notify_no_data(db: AsyncSession, kri: KriDefinition, to_cro: bool) -> None:
    try:
        from app.models.user import Role, UserRole
        from app.services.notifications import send_email

        emails: set[str] = set()
        owner = await db.get(User, kri.ownerId)
        if owner and owner.email:
            emails.add(owner.email)
        if to_cro:
            cro = (
                await db.execute(
                    select(User.email).join(UserRole, UserRole.userId == User.id).join(Role, Role.id == UserRole.roleId).where(Role.code == "CRO")
                )
            ).scalars().all()
            emails.update(e for e in cro if e)
        if emails:
            sev = "ESCALATION (2nd consecutive miss)" if to_cro else "missing reading"
            await send_email(list(emails), subject=f"[ERM] KRI data {sev}: {kri.kriCode}", body=f"KRI {kri.kriCode} '{kri.name}' has no current reading past its grace window.")
    except Exception:
        return


# ════════════════════════════════════════════════════════════════════════════
# Appetite breach detection
# ════════════════════════════════════════════════════════════════════════════
_OPEN_BREACH_STATES = ("OPEN", "UNDER_REVIEW", "TREATMENT_MANDATED", "TEMPORARILY_ACCEPTED")
_ACTIVE_RISK_STATES = ("DRAFT", "SUBMITTED", "ASSESSED", "TREATMENT_ACTIVE", "MONITORING", "ACCEPTED", "ESCALATED")


async def _observed_value(db: AsyncSession, category_id: str, band_type: str) -> tuple[float, list[str]]:
    """Return (observedValue, triggeringEntityIds) for a band in a category."""
    risks = (
        await db.execute(
            select(EnterpriseRisk).where(EnterpriseRisk.categoryId == category_id)
            .where(EnterpriseRisk.isDeleted.is_(False)).where(EnterpriseRisk.lifecycleState != "CLOSED")
        )
    ).scalars().all()
    if band_type == "MAX_RESIDUAL_SCORE":
        scored = [(r.residualScore or 0, r.id) for r in risks]
        if not scored:
            return 0.0, []
        mx = max(s for s, _ in scored)
        return float(mx), [rid for s, rid in scored if s == mx and s > 0]
    if band_type == "MAX_CRITICAL_COUNT":
        ids = [r.id for r in risks if r.residualBand == "CRITICAL"]
        return float(len(ids)), ids
    if band_type == "MAX_HIGH_PLUS_COUNT":
        ids = [r.id for r in risks if r.residualBand in ("HIGH", "CRITICAL")]
        return float(len(ids)), ids
    if band_type == "MAX_RED_KRI_COUNT":
        kris = (
            await db.execute(
                select(KriDefinition).where(KriDefinition.categoryId == category_id)
                .where(KriDefinition.isActive.is_(True)).where(KriDefinition.isDeleted.is_(False)).where(KriDefinition.currentStatus == "RED")
            )
        ).scalars().all()
        return float(len(kris)), [k.id for k in kris]
    return 0.0, []


async def evaluate_appetite(db: AsyncSession) -> dict[str, Any]:
    """Evaluate all ACTIVE statements' tolerance bands; open/resolve/reopen breaches.
    Caller commits."""
    now = _now()
    opened, resolved, reopened = 0, 0, 0
    statements = (
        await db.execute(select(AppetiteStatement).where(AppetiteStatement.status == "ACTIVE").where(AppetiteStatement.isDeleted.is_(False)))
    ).scalars().all()
    for st in statements:
        for band in (st.toleranceBands or []):
            bt, threshold = band.get("bandType"), float(band.get("thresholdValue", 0))
            observed, triggers = await _observed_value(db, st.categoryId, bt)
            breaching = observed > threshold
            existing = (
                await db.execute(
                    select(AppetiteBreach).where(AppetiteBreach.appetiteStatementId == st.id)
                    .where(AppetiteBreach.bandType == bt).where(AppetiteBreach.status.in_(_OPEN_BREACH_STATES))
                )
            ).scalar_one_or_none()
            if breaching:
                if existing is None:
                    db.add(AppetiteBreach(
                        appetiteStatementId=st.id, categoryId=st.categoryId, bandType=bt,
                        observedValue=observed, thresholdValue=threshold, triggeringEntityIds=triggers,
                        detectedAt=now, status="OPEN",
                    ))
                    opened += 1
                    await _notify_appetite_breach(db, st, bt, observed, threshold)
                else:
                    existing.observedValue = observed
                    existing.triggeringEntityIds = triggers
                    # auto-reopen an expired temporary acceptance
                    if existing.status == "TEMPORARILY_ACCEPTED" and existing.reviewByDate and _aware(existing.reviewByDate) < now:
                        existing.status = "OPEN"
                        reopened += 1
            else:
                if existing is not None:
                    existing.status = "RESOLVED"
                    existing.observedValue = observed
                    existing.resolvedAt = now
                    resolved += 1
    await db.flush()
    return {"opened": opened, "resolved": resolved, "reopened": reopened}


async def _notify_appetite_breach(db, st, band_type, observed, threshold) -> None:
    try:
        from app.models.user import Role, UserRole
        from app.services.notifications import send_email

        emails = (
            await db.execute(
                select(User.email).join(UserRole, UserRole.userId == User.id).join(Role, Role.id == UserRole.roleId).where(Role.code.in_(("CRO", "RISK_CHAMPION")))
            )
        ).scalars().all()
        recip = [e for e in emails if e]
        if recip:
            await send_email(recip, subject=f"[ERM] Appetite breach: {band_type}", body=f"Tolerance band {band_type} breached (observed {observed} > threshold {threshold}). RMC decision required.")
    except Exception:
        return


# ════════════════════════════════════════════════════════════════════════════
# Compliance task generation + status
# ════════════════════════════════════════════════════════════════════════════
_FREQ_MONTHS = {"MONTHLY": 1, "QUARTERLY": 3, "HALF_YEARLY": 6, "ANNUAL": 12}


def _period_label(d: datetime, freq: str) -> str:
    if freq == "MONTHLY":
        return d.strftime("%Y-%m")
    if freq == "QUARTERLY":
        return f"{d.year}-Q{(d.month - 1)//3 + 1}"
    if freq == "HALF_YEARLY":
        return f"{d.year}-H{1 if d.month <= 6 else 2}"
    if freq == "ANNUAL":
        return f"FY{d.year}"
    return d.strftime("%Y-%m")


def compute_obligation_status(obl: LegalObligation, tasks: list[ComplianceTask], now: datetime | None = None) -> str:
    now = now or _now()
    if not obl.isActive:
        return "NOT_APPLICABLE"
    open_tasks = [t for t in tasks if t.status in ("PENDING", "SUBMITTED", "OVERDUE")]
    # OVERDUE: any task overdue (status OVERDUE, or PENDING past due)
    if any(t.status == "OVERDUE" or (t.status == "PENDING" and _aware(t.dueDate) < now) for t in tasks):
        return "OVERDUE"
    # UNDER_RENEWAL: a RENEWAL task SUBMITTED awaiting verification
    if any(t.taskType == "RENEWAL" and t.status == "SUBMITTED" for t in tasks):
        return "UNDER_RENEWAL"
    # DUE_SOON: a PENDING task due within renewalLeadDays
    lead = timedelta(days=obl.renewalLeadDays)
    if any(t.status == "PENDING" and _aware(t.dueDate) <= now + lead for t in open_tasks):
        return "DUE_SOON"
    return "COMPLIANT"


async def refresh_statuses(db: AsyncSession) -> dict[str, Any]:
    """Mark PENDING-past-due tasks OVERDUE; recompute each obligation's status."""
    now = _now()
    obls = (await db.execute(select(LegalObligation).where(LegalObligation.isDeleted.is_(False)))).scalars().all()
    flipped = 0
    for obl in obls:
        tasks = (await db.execute(select(ComplianceTask).where(ComplianceTask.obligationId == obl.id))).scalars().all()
        for t in tasks:
            if t.status == "PENDING" and _aware(t.dueDate) < now:
                t.status = "OVERDUE"
                flipped += 1
        new_status = compute_obligation_status(obl, tasks, now)
        if obl.status != new_status:
            obl.status = new_status
    await db.flush()
    return {"obligationsEvaluated": len(obls), "tasksFlippedOverdue": flipped}


async def generate_tasks(db: AsyncSession) -> dict[str, Any]:
    """Create the next ComplianceTask per active obligation (no duplicate open task
    per period). Renewals at validUntil − renewalLeadDays; filings/attestations
    from frequency."""
    now = _now()
    created = 0
    obls = (
        await db.execute(select(LegalObligation).where(LegalObligation.isActive.is_(True)).where(LegalObligation.isDeleted.is_(False)))
    ).scalars().all()
    for obl in obls:
        existing = (await db.execute(select(ComplianceTask).where(ComplianceTask.obligationId == obl.id))).scalars().all()
        open_periods = {t.periodLabel for t in existing if t.status in ("PENDING", "SUBMITTED", "OVERDUE")}

        if obl.frequency == "PERIODIC_RENEWAL" or obl.obligationType in ("LICENCE", "CONSENT", "REGISTRATION"):
            if not obl.validUntil:
                continue
            due = _aware(obl.validUntil) - timedelta(days=obl.renewalLeadDays)
            label = f"Renewal-{_aware(obl.validUntil).year}"
            if label in {t.periodLabel for t in existing}:
                continue
            # only open the renewal task once we're within ~1.5x lead time
            if due - timedelta(days=obl.renewalLeadDays // 2) <= now <= _aware(obl.validUntil) + timedelta(days=30):
                db.add(ComplianceTask(obligationId=obl.id, taskType="RENEWAL", periodLabel=label, dueDate=due, status="PENDING"))
                created += 1
        elif obl.frequency in _FREQ_MONTHS:
            label = _period_label(now, obl.frequency)
            if label in open_periods or label in {t.periodLabel for t in existing}:
                continue
            # due at end of current period (approx)
            due = now + timedelta(days=_FREQ_MONTHS[obl.frequency] * 30)
            task_type = "FILING" if obl.obligationType == "RETURN_FILING" else "ATTESTATION"
            db.add(ComplianceTask(obligationId=obl.id, taskType=task_type, periodLabel=label, dueDate=due, status="PENDING"))
            created += 1
    await db.flush()
    return {"created": created}


# ════════════════════════════════════════════════════════════════════════════
# Loss events — auto-feed + calibration
# ════════════════════════════════════════════════════════════════════════════
async def auto_feed_incidents(db: AsyncSession, actor_id: str | None = None) -> dict[str, Any]:
    """Scan qualifying incidents (investigation-complete, severity ≥ LTI or recorded
    property/production loss) without a loss event; create DRAFT INCIDENT_AUTO events."""
    from app.models.incident import Incident

    ops_cat = (await db.execute(select(RiskCategory.id).where(RiskCategory.code == "OPS"))).scalar_one_or_none()
    now = _now()
    incidents = (
        await db.execute(
            select(Incident).where(Incident.status.in_(("CAPA_ASSIGNED", "VERIFIED", "CLOSED")))
        )
    ).scalars().all()
    inc_by_id = {i.id: i for i in incidents}

    # T2-25: editing the source incident never mutates a QUANTIFIED/CLOSED loss
    # event — instead, if the incident changed after the loss event, set an info
    # badge flag (no event bus in the platform, so detected on this sync pass).
    updated_flagged = 0
    existing_auto = (
        await db.execute(
            select(LossEvent).where(LossEvent.source == "INCIDENT_AUTO")
            .where(LossEvent.status.in_(("QUANTIFIED", "CLOSED"))).where(LossEvent.isDeleted.is_(False))
        )
    ).scalars().all()
    for le in existing_auto:
        inc = inc_by_id.get(le.sourceIncidentId)
        inc_upd = _aware(getattr(inc, "updatedAt", None)) if inc else None
        if inc_upd and le.updatedAt and inc_upd > _aware(le.updatedAt) and not le.sourceUpdatedFlag:
            le.sourceUpdatedFlag = True
            updated_flagged += 1

    created = 0
    for inc in incidents:
        itype = getattr(inc, "type", None)
        itype_s = itype.value if hasattr(itype, "value") else str(itype or "")
        prop = getattr(inc, "costPropertyDamage", None) or getattr(inc, "propertyDamageCost", None) or 0
        prod = getattr(inc, "costLostProduction", None) or 0
        qualifies = itype_s in ("LTI", "FATALITY") or (prop or 0) > 0 or (prod or 0) > 0
        if not qualifies:
            continue
        exists = (await db.execute(select(LossEvent.id).where(LossEvent.sourceIncidentId == inc.id))).scalar_one_or_none()
        if exists:
            continue
        ev_date = _aware(getattr(inc, "occurredAt", None) or getattr(inc, "date", None) or now)
        code = await _next_loss_code(db)
        db.add(LossEvent(
            eventCode=code, title=getattr(inc, "title", None) or f"Incident loss — {itype_s}",
            description=(getattr(inc, "description", "") or "")[:2000], eventDate=ev_date,
            siteId=getattr(inc, "plantId", None), categoryId=ops_cat, source="INCIDENT_AUTO",
            sourceIncidentId=inc.id, status="DRAFT", grossLossInr=float((prop or 0) + (prod or 0) + (getattr(inc, "costMedical", None) or 0)),
            lossTypes=["PROPERTY_DAMAGE"] if (prop or 0) > 0 else (["BUSINESS_INTERRUPTION"] if (prod or 0) > 0 else ["MEDICAL_COMPENSATION"]),
            createdBy=actor_id,
        ))
        # Flush each insert so _next_loss_code's count() reflects it — otherwise
        # multiple new events in one run collide on the unique eventCode.
        await db.flush()
        created += 1
    return {"created": created, "sourceUpdatedFlagged": updated_flagged}


async def _next_loss_code(db: AsyncSession) -> str:
    year = _now().year
    n = (await db.execute(select(func.count()).select_from(LossEvent))).scalar() or 0
    return f"LE-{year}-{(n + 1):04d}"


async def calibration(db: AsyncSession) -> list[dict[str, Any]]:
    """Risk-vs-actual-loss matrix with Underscored / Watch flags."""
    now = _now()
    start = now - timedelta(days=365)
    risks = (
        await db.execute(
            select(EnterpriseRisk).where(EnterpriseRisk.isDeleted.is_(False)).where(EnterpriseRisk.lifecycleState != "CLOSED")
        )
    ).scalars().all()
    losses = (
        await db.execute(
            select(LossEvent).where(LossEvent.isDeleted.is_(False)).where(LossEvent.isNearMiss.is_(False))
            .where(LossEvent.status.in_(("QUANTIFIED", "CLOSED"))).where(LossEvent.eventDate >= start)
        )
    ).scalars().all()
    cats = {c.id: c.code for c in (await db.execute(select(RiskCategory))).scalars().all()}
    # Risks with a CLOSED mitigation — a loss here means the mitigation was ineffective.
    from app.models.capa import Capa

    closed_treat = (
        await db.execute(
            select(Capa.sourceReferenceId)
            .where(Capa.sourceTypeCode == "RISK_TREATMENT")
            .where(Capa.state.in_(("CLOSED", "VERIFIED")))
        )
    ).scalars().all()
    mitigated_risk_ids = {rid for rid in closed_treat if rid}
    out = []
    for r in risks:
        linked = [le for le in losses if r.id in (le.linkedRiskIds or [])]
        net = sum(le.netLossInr for le in linked)
        flag = None
        # A realised loss on a risk we believed mitigated is the sharpest calibration
        # signal — the control/treatment didn't work. It takes priority.
        if net > 0 and r.id in mitigated_risk_ids:
            flag = "MITIGATION_INEFFECTIVE"
        elif net >= 10_000_000 and r.residualBand in ("LOW", "MEDIUM"):
            flag = "UNDERSCORED"
        elif r.residualBand == "CRITICAL" and net == 0:
            flag = "WATCH"
        out.append({
            "riskId": r.id, "riskCode": r.riskCode, "title": r.title, "categoryCode": cats.get(r.categoryId),
            "residualScore": r.residualScore, "residualBand": r.residualBand,
            "actualNetLoss12m": net, "lossEventCount": len(linked), "flag": flag,
            "hasClosedMitigation": r.id in mitigated_risk_ids,
        })
    out.sort(key=lambda x: (x["flag"] is None, -x["actualNetLoss12m"]))
    return out


async def user_name_map(db: AsyncSession, ids) -> dict[str, str]:
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    rows = (await db.execute(select(User.id, User.name).where(User.id.in_(ids)))).all()
    return {r[0]: r[1] for r in rows}
