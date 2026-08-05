"""Facilities — live compliance provider + snapshot precompute (Phase D).

Reads operational metrics per site (= Plant.id) from the existing engines —
CAMS, CAPA, Audit & Compliance, Incidents, ERM Obligations — and never keeps a
duplicate store: `FactoryComplianceSnapshot` is only a cache of these live reads
so the consolidated dashboard renders fast. Every engine is imported and queried
defensively, so a deployment missing one engine simply drops its metric
(graceful degradation, hard constraint §4).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.factory import (
    FactoryCertification,
    FactoryComplianceSnapshot,
    FactoryEnvPeriod,
    SocialComplianceProfile,
    WorkforceComposition,
)
from app.schemas import factory as S
from app.services import factory as fsvc

# ── engine models (optional — degrade gracefully if a module is absent) ──────
try:
    from app.models.cams import CamsEngagement, CamsFinding
except Exception:  # pragma: no cover
    CamsEngagement = CamsFinding = None  # type: ignore
try:
    from app.models.capa import Capa
except Exception:  # pragma: no cover
    Capa = None  # type: ignore
try:
    from app.models.audit_compliance import ComplianceAudit
except Exception:  # pragma: no cover
    ComplianceAudit = None  # type: ignore
try:
    from app.models.incident import Incident
except Exception:  # pragma: no cover
    Incident = None  # type: ignore
try:
    from app.models.erm_p2 import LegalObligation
except Exception:  # pragma: no cover
    LegalObligation = None  # type: ignore
try:
    from app.models.competency_matrix import Competency, CompetencyRecord
except Exception:  # pragma: no cover
    Competency = CompetencyRecord = None  # type: ignore
try:
    from app.models.user import User
except Exception:  # pragma: no cover
    User = None  # type: ignore
try:
    from app.models.permit import Permit
except Exception:  # pragma: no cover
    Permit = None  # type: ignore
try:
    from app.models.moc import ChangeRequest
except Exception:  # pragma: no cover
    ChangeRequest = None  # type: ignore
try:
    from app.models.hira import HiraStudy
except Exception:  # pragma: no cover
    HiraStudy = None  # type: ignore

# Competency state machine (app/services/competency_states.py) — the states that
# count as "currently valid" vs "expired" for the per-site training rollup.
COMP_VALID = ("validated_active", "expiring_soon")
COMP_EXPIRED = ("expired_in_grace", "expired_revoked", "lapsed_requires_full_redo")

# Operational-risk engine constants.
PERMIT_HIGH_RISK = ("HOT_WORK", "CONFINED_SPACE", "WORK_AT_HEIGHT", "ELECTRICAL_LOTO")
MOC_CLOSED = ("closed_successful", "closed_aborted", "closed_rejected", "withdrawn", "expired", "rolled_back")

CAMS_FINDING_CLOSED = ("CLOSED", "ACCEPTED_RISK")
CAMS_CRIT = ("MAJOR_NC", "CRITICAL_NC")
CAPA_CLOSED = ("CLOSED", "CLOSED_RECURRED", "CANCELLED")
# LegalObligation.status: COMPLIANT | DUE_SOON | OVERDUE | UNDER_RENEWAL | NOT_APPLICABLE
OBLIGATION_OK = ("COMPLIANT", "NOT_APPLICABLE")
OBLIGATION_OVERDUE = ("OVERDUE",)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ════════════════════════════════════════════════════════════════════════════
# Live metric computation (read-only across the engines)
# ════════════════════════════════════════════════════════════════════════════
async def compute_site_metrics(db: AsyncSession, site_id: str) -> dict[str, Any]:
    m: dict[str, Any] = {
        "auditComplianceScorePct": None,
        "openFindings": 0,
        "criticalFindings": 0,
        "openCapas": 0,
        "overdueCapas": 0,
        "openObligations": 0,
        "overdueObligations": 0,
        "incidentCount12m": 0,
        "lastAuditDate": None,
        "ermExpectedLossInr": None,
        "ermCriticalCount": 0,
        "ermHighCount": 0,
    }

    # ── I-17: ERM ₹ exposure (Σ residual expected loss of HIGH+CRITICAL risks) ──
    try:
        from app.models.erm import EnterpriseRisk
        risks = (
            await db.execute(
                select(EnterpriseRisk).where(EnterpriseRisk.plantId == site_id)
                .where(EnterpriseRisk.isDeleted.is_(False)).where(EnterpriseRisk.lifecycleState != "CLOSED")
                .where(EnterpriseRisk.residualBand.in_(("HIGH", "CRITICAL")))
            )
        ).scalars().all()
        if risks:
            m["ermExpectedLossInr"] = round(sum(r.residualExpectedLossInr or 0 for r in risks))
            m["ermCriticalCount"] = sum(1 for r in risks if r.residualBand == "CRITICAL")
            m["ermHighCount"] = sum(1 for r in risks if r.residualBand == "HIGH")
    except Exception:
        pass

    # ── CAMS engagements → compliance score + last audit ──
    if CamsEngagement is not None:
        try:
            engs = (
                await db.execute(
                    select(CamsEngagement).where(CamsEngagement.siteId == site_id).where(CamsEngagement.isDeleted.is_(False))
                )
            ).scalars().all()
            scored = [e.scorePercent for e in engs if e.scorePercent is not None and e.conductedDate is not None]
            if scored:
                m["auditComplianceScorePct"] = round(sum(scored) / len(scored), 1)
            conducted = [e.conductedDate for e in engs if e.conductedDate]
            if conducted:
                m["lastAuditDate"] = max(conducted)
        except Exception:
            pass

    # ── CAMS findings → open / critical ──
    if CamsFinding is not None:
        try:
            finds = (
                await db.execute(
                    select(CamsFinding).where(CamsFinding.siteId == site_id).where(CamsFinding.isDeleted.is_(False))
                )
            ).scalars().all()
            open_f = [f for f in finds if f.status not in CAMS_FINDING_CLOSED]
            m["openFindings"] = len(open_f)
            m["criticalFindings"] = sum(1 for f in open_f if f.severity in CAMS_CRIT)
        except Exception:
            pass

    # ── Audit & Compliance fallback for score / last audit ──
    if m["auditComplianceScorePct"] is None and ComplianceAudit is not None:
        try:
            audits = (await db.execute(select(ComplianceAudit).where(ComplianceAudit.plantId == site_id))).scalars().all()
            scored = [a.overallCompliancePct for a in audits if a.overallCompliancePct is not None]
            if scored:
                m["auditComplianceScorePct"] = round(sum(scored) / len(scored), 1)
            dates = [(a.submittedAt or a.closedAt) for a in audits if (a.submittedAt or a.closedAt)]
            if dates and not m["lastAuditDate"]:
                m["lastAuditDate"] = max(dates)
        except Exception:
            pass

    # ── CAPA → open / overdue ──
    if Capa is not None:
        try:
            capas = (await db.execute(select(Capa).where(Capa.plantId == site_id))).scalars().all()
            open_capas = [c for c in capas if c.state not in CAPA_CLOSED]
            m["openCapas"] = len(open_capas)
            now = _now()
            m["overdueCapas"] = sum(
                1 for c in open_capas if getattr(c, "closureTargetDate", None) and _aware(c.closureTargetDate) < now
            )
        except Exception:
            pass

    # ── Incidents → 12-month count ──
    if Incident is not None:
        try:
            cutoff = _now().replace(tzinfo=None) - timedelta(days=365)
            n = (
                await db.execute(
                    select(func.count()).select_from(Incident).where(Incident.plantId == site_id).where(Incident.date >= cutoff)
                )
            ).scalar() or 0
            m["incidentCount12m"] = int(n)
        except Exception:
            pass

    # ── ERM Obligations → open / overdue ──
    if LegalObligation is not None:
        try:
            obs = (
                await db.execute(
                    select(LegalObligation).where(LegalObligation.siteId == site_id).where(LegalObligation.isDeleted.is_(False))
                )
            ).scalars().all()
            m["openObligations"] = sum(1 for o in obs if o.status not in OBLIGATION_OK)
            m["overdueObligations"] = sum(1 for o in obs if o.status in OBLIGATION_OVERDUE)
        except Exception:
            pass

    return m


async def _certs_expiring(db: AsyncSession, profile_id: str) -> int:
    certs = (
        await db.execute(
            select(FactoryCertification)
            .where(FactoryCertification.factoryProfileId == profile_id)
            .where(FactoryCertification.isDeleted.is_(False))
        )
    ).scalars().all()
    return sum(
        1 for c in certs if fsvc.cert_is_expiring(fsvc.compute_cert_status(c.expiryDate, c.renewalLeadDays, c.status))
    )


# ════════════════════════════════════════════════════════════════════════════
# Snapshot precompute (upsert the LIVE row per factory)
# ════════════════════════════════════════════════════════════════════════════
async def recompute_snapshot(db: AsyncSession, profile, user_id: str | None = None) -> FactoryComplianceSnapshot:
    m = await compute_site_metrics(db, profile.siteId)
    certs_expiring = await _certs_expiring(db, profile.id)
    snap = (
        await db.execute(
            select(FactoryComplianceSnapshot)
            .where(FactoryComplianceSnapshot.factoryProfileId == profile.id)
            .where(FactoryComplianceSnapshot.periodLabel == "LIVE")
        )
    ).scalar_one_or_none()
    if snap is None:
        snap = FactoryComplianceSnapshot(factoryProfileId=profile.id, siteId=profile.siteId, periodLabel="LIVE", createdBy=user_id)
        db.add(snap)
    snap.auditComplianceScorePct = m["auditComplianceScorePct"]
    snap.openFindings = m["openFindings"]
    snap.criticalFindings = m["criticalFindings"]
    snap.openCapas = m["openCapas"]
    snap.overdueCapas = m["overdueCapas"]
    snap.openObligations = m["openObligations"]
    snap.overdueObligations = m["overdueObligations"]
    snap.certsExpiringCount = certs_expiring
    snap.lastAuditDate = m["lastAuditDate"]
    snap.incidentCount12m = m["incidentCount12m"]
    snap.computedAt = _now()
    snap.updatedBy = user_id
    return snap


# ════════════════════════════════════════════════════════════════════════════
# F-02 Compliance & Audit tab — live, drillable lists per engine
# ════════════════════════════════════════════════════════════════════════════
async def compliance_detail(db: AsyncSession, site_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {"audits": [], "findings": [], "capas": [], "obligations": [], "incidents": []}

    if CamsEngagement is not None:
        try:
            engs = (
                await db.execute(
                    select(CamsEngagement)
                    .where(CamsEngagement.siteId == site_id)
                    .where(CamsEngagement.isDeleted.is_(False))
                    .order_by(CamsEngagement.plannedDate.desc())
                    .limit(10)
                )
            ).scalars().all()
            out["audits"] = [
                {
                    "id": e.id, "code": e.engagementCode, "title": e.title, "type": e.engagementType,
                    "status": e.status, "score": e.scorePercent,
                    "conductedDate": e.conductedDate.isoformat() if e.conductedDate else None,
                }
                for e in engs
            ]
        except Exception:
            pass
    if CamsFinding is not None:
        try:
            finds = (
                await db.execute(
                    select(CamsFinding)
                    .where(CamsFinding.siteId == site_id)
                    .where(CamsFinding.isDeleted.is_(False))
                    .order_by(CamsFinding.createdAt.desc())
                    .limit(15)
                )
            ).scalars().all()
            out["findings"] = [
                {"id": f.id, "code": f.findingCode, "title": f.title, "severity": f.severity, "status": f.status}
                for f in finds
            ]
        except Exception:
            pass
    if Capa is not None:
        try:
            capas = (
                await db.execute(select(Capa).where(Capa.plantId == site_id).order_by(Capa.detectedAt.desc()).limit(15))
            ).scalars().all()
            now = _now()
            out["capas"] = [
                {
                    "id": c.id, "number": getattr(c, "capaNumber", None) or getattr(c, "number", None),
                    "title": getattr(c, "title", None), "state": c.state, "severity": getattr(c, "severity", None),
                    "overdue": bool(getattr(c, "closureTargetDate", None) and c.state not in CAPA_CLOSED and _aware(c.closureTargetDate) < now),
                }
                for c in capas
            ]
        except Exception:
            pass
    if LegalObligation is not None:
        try:
            obs = (
                await db.execute(
                    select(LegalObligation)
                    .where(LegalObligation.siteId == site_id)
                    .where(LegalObligation.isDeleted.is_(False))
                    .order_by(LegalObligation.validUntil.asc().nulls_last())
                    .limit(15)
                )
            ).scalars().all()
            out["obligations"] = [
                {
                    "id": o.id, "code": getattr(o, "obligationCode", None), "title": getattr(o, "title", None),
                    "status": o.status, "validUntil": o.validUntil.isoformat() if getattr(o, "validUntil", None) else None,
                }
                for o in obs
            ]
        except Exception:
            pass
    if Incident is not None:
        try:
            cutoff = _now().replace(tzinfo=None) - timedelta(days=365)
            incs = (
                await db.execute(
                    select(Incident).where(Incident.plantId == site_id).where(Incident.date >= cutoff).order_by(Incident.date.desc()).limit(15)
                )
            ).scalars().all()
            out["incidents"] = [
                {
                    "id": i.id, "number": getattr(i, "number", None),
                    "type": getattr(i, "type", None).value if getattr(getattr(i, "type", None), "value", None) else str(getattr(i, "type", "")),
                    "status": getattr(i, "status", None).value if getattr(getattr(i, "status", None), "value", None) else str(getattr(i, "status", "")),
                    "date": i.date.isoformat() if getattr(i, "date", None) else None,
                }
                for i in incs
            ]
        except Exception:
            pass

    return out


# ════════════════════════════════════════════════════════════════════════════
# Facility rollup extension (P1) — QoQ deltas + Environment / Certifications /
# Training blocks. Every block is a LIVE, site-scoped read from an existing
# engine, assembled defensively so one slow/missing engine degrades only its own
# block (never the whole tab). No duplicate store, no cross-tab data copy.
# ════════════════════════════════════════════════════════════════════════════
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def quarter_label(d: datetime) -> str:
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def prior_quarter_label(period_ref: str) -> str:
    """'2026-Q2' → '2026-Q1'; '2026-Q1' → '2025-Q4'. Falls back gracefully."""
    try:
        ys, qs = period_ref.split("-Q")
        year, q = int(ys), int(qs)
    except Exception:
        return period_ref
    return f"{year - 1}-Q4" if q == 1 else f"{year}-Q{q - 1}"


def _fmtdate(dt: datetime | None) -> str:
    if not dt:
        return "—"
    a = _aware(dt)
    return f"{a.day:02d} {_MONTHS[a.month - 1]} {a.year}"


def _india_group(i: int) -> str:
    """Indian digit grouping (lakh/crore), matching the frontend `fmtNum`
    (toLocaleString 'en-IN') so a number reads identically in a tile and a row —
    e.g. 210000 → '2,10,000'."""
    s = str(abs(i))
    if len(s) <= 3:
        body = s
    else:
        head, rest = s[-3:], s[:-3]
        parts: list[str] = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        body = ",".join(parts) + "," + head
    return ("-" if i < 0 else "") + body


def _num(n: float | int | None, suffix: str = "") -> str:
    if n is None:
        return "—"
    v = round(float(n), 1)
    i = int(abs(v))
    body = _india_group(i if v >= 0 else -i)
    if v != int(v):  # one-decimal fraction (e.g. 92.3 → "92.3")
        body += f"{abs(v) - i:.1f}"[1:]
    return f"{body}{suffix}"


def _link(module: str, route: str, **query) -> "S.ModuleDeepLink":
    return S.ModuleDeepLink(module=module, route=route, query={k: str(v) for k, v in query.items() if v is not None})


def _delta(
    current: float | None,
    prior: float | None,
    *,
    improve_when_down: bool = False,
    neutral: bool = False,
    as_pct: bool = False,
) -> "S.KpiDelta | None":
    """Build a QoQ delta. `improve_when_down` encodes the §3.6 rule that *direction
    is not meaning*: lower findings/incidents/overdue is an improvement, higher
    compliance is. `neutral` (obligations) carries no RAG verdict."""
    if current is None or prior is None:
        return None
    try:
        c, p = float(current), float(prior)
    except (TypeError, ValueError):
        return None
    direction = "up" if c > p else "down" if c < p else "flat"
    is_improvement: bool | None = None
    if not neutral and direction != "flat":
        rose = direction == "up"
        is_improvement = rose != improve_when_down  # rose&want-up OR fell&want-down
    display_pct = round((c - p) / p * 100, 1) if (as_pct and p != 0) else None
    return S.KpiDelta(priorValue=prior, direction=direction, isImprovement=is_improvement, displayPct=display_pct)


# ── Prior-period snapshot (powers the strip QoQ deltas) ──────────────────────
async def prior_metrics(db: AsyncSession, profile_id: str, prior_label: str) -> "S.SnapshotMetrics | None":
    snap = (
        await db.execute(
            select(FactoryComplianceSnapshot)
            .where(FactoryComplianceSnapshot.factoryProfileId == profile_id)
            .where(FactoryComplianceSnapshot.periodLabel == prior_label)
            .where(FactoryComplianceSnapshot.isDeleted.is_(False))
        )
    ).scalar_one_or_none()
    if snap is None:
        return None
    return S.SnapshotMetrics(
        auditComplianceScorePct=snap.auditComplianceScorePct, openFindings=snap.openFindings,
        criticalFindings=snap.criticalFindings, openCapas=snap.openCapas, overdueCapas=snap.overdueCapas,
        openObligations=snap.openObligations, overdueObligations=snap.overdueObligations,
        certsExpiringCount=snap.certsExpiringCount, incidentCount12m=snap.incidentCount12m,
        lastAuditDate=snap.lastAuditDate, computedAt=snap.computedAt,
    )


# ── Environmental operational rollup (ESG source = FactoryEnvPeriod) ─────────
async def env_block(db: AsyncSession, profile_id: str, site_id: str, period_ref: str, prior_ref: str) -> "S.FacilityMetricBlock":
    block = S.FacilityMetricBlock(
        domainKey="environment", title="Environmental Performance",
        caption="Live from the ESG engine — site-scoped.",
    )
    cur = (
        await db.execute(
            select(FactoryEnvPeriod)
            .where(FactoryEnvPeriod.factoryProfileId == profile_id)
            .where(FactoryEnvPeriod.periodLabel == period_ref)
            .where(FactoryEnvPeriod.isDeleted.is_(False))
        )
    ).scalar_one_or_none()
    if cur is None:
        block.enabled = False
        block.notEnabledText = "Environmental data — module not enabled · roadmap"
        return block
    prior = (
        await db.execute(
            select(FactoryEnvPeriod)
            .where(FactoryEnvPeriod.factoryProfileId == profile_id)
            .where(FactoryEnvPeriod.periodLabel == prior_ref)
            .where(FactoryEnvPeriod.isDeleted.is_(False))
        )
    ).scalar_one_or_none()
    pv = lambda attr: getattr(prior, attr, None) if prior else None  # noqa: E731

    # ETP / effluent compliance drives the effluent RAG.
    etp = (cur.etpStatus or "").upper()
    etp_state = "good" if etp == "COMPLIANT" else "breach" if etp == "NON_COMPLIANT" else "watch"
    energy_state = "watch" if (cur.energyTargetKwh and cur.energyKwh and cur.energyKwh > cur.energyTargetKwh) else "good"
    waste_state = "watch" if (
        cur.wasteDivertedTargetPct is not None and cur.wasteDivertedPct is not None
        and cur.wasteDivertedPct < cur.wasteDivertedTargetPct
    ) else "good"

    block.tiles = [
        S.FacilityTile(id="energy", label="Energy", value=cur.energyKwh, unit="kWh", state=energy_state,
                       delta=_delta(cur.energyKwh, pv("energyKwh"), improve_when_down=True, as_pct=True)),
        S.FacilityTile(id="water", label="Water withdrawn", value=cur.waterWithdrawnKl, unit="kL", state="good",
                       delta=_delta(cur.waterWithdrawnKl, pv("waterWithdrawnKl"), improve_when_down=True, as_pct=True)),
        S.FacilityTile(id="effluent", label="Effluent", value=cur.effluentDischargedKl, unit="kL", state=etp_state,
                       delta=_delta(cur.effluentDischargedKl, pv("effluentDischargedKl"), improve_when_down=True, as_pct=True)),
        S.FacilityTile(id="waste", label="Waste", value=cur.wasteGeneratedT, unit="t", state=waste_state,
                       delta=_delta(cur.wasteGeneratedT, pv("wasteGeneratedT"), improve_when_down=True, as_pct=True)),
        S.FacilityTile(id="scope1", label="Scope 1", value=cur.scope1TCo2e, unit="tCO₂e", state="good",
                       delta=_delta(cur.scope1TCo2e, pv("scope1TCo2e"), improve_when_down=True, as_pct=True)),
        S.FacilityTile(id="scope2", label="Scope 2", value=cur.scope2TCo2e, unit="tCO₂e", state="good",
                       delta=_delta(cur.scope2TCo2e, pv("scope2TCo2e"), improve_when_down=True, as_pct=True)),
    ]

    def _row(rid, primary, value, unit, tone, status=None, target=None):
        sec = None
        bits = []
        if prior and pv(rid) is not None:
            bits.append(f"prior {_num(pv(rid))}")
        if target is not None:
            bits.append(f"target {_num(target)}")
        if bits:
            sec = " · ".join(bits)
        return S.FacilityRollupRow(
            id=rid, primaryText=primary, secondaryText=sec, statusLabel=status, statusTone=tone,
            trailingText=_num(value, f" {unit}" if unit else ""),
        )

    # Waste diversion vs target reads best as the row's own secondary line.
    waste_sec = None
    if cur.wasteDivertedPct is not None:
        wt = f" (target {_num(cur.wasteDivertedTargetPct)}%)" if cur.wasteDivertedTargetPct is not None else ""
        waste_sec = f"{_num(cur.wasteDivertedPct)}% diverted{wt}"

    rows = [
        _row("energyKwh", "Energy consumed", cur.energyKwh, "kWh",
             "warning" if energy_state == "watch" else "positive", target=cur.energyTargetKwh),
        _row("waterWithdrawnKl", "Water withdrawn", cur.waterWithdrawnKl, "kL", "positive"),
        _row("effluentDischargedKl", "Effluent discharged", cur.effluentDischargedKl, "kL",
             "positive" if etp_state == "good" else "critical" if etp_state == "breach" else "warning",
             status=f"ETP {titleish(cur.etpStatus)}" if cur.etpStatus else None),
        S.FacilityRollupRow(
            id="wasteGeneratedT", primaryText="Waste generated", secondaryText=waste_sec,
            statusTone="warning" if waste_state == "watch" else "positive",
            trailingText=_num(cur.wasteGeneratedT, " t")),
        _row("scope1TCo2e", "Scope 1 emissions", cur.scope1TCo2e, "tCO₂e", "muted"),
        _row("scope2TCo2e", "Scope 2 emissions", cur.scope2TCo2e, "tCO₂e", "muted"),
    ]

    # Environmental consents cross-link — prefer the live Statutory Obligations
    # register; fall back to the period row's own consent mirror.
    consent_added = False
    if LegalObligation is not None:
        try:
            cons = (
                await db.execute(
                    select(LegalObligation)
                    .where(LegalObligation.siteId == site_id)
                    .where(LegalObligation.isDeleted.is_(False))
                    .where(LegalObligation.obligationType == "CONSENT")
                    .order_by(LegalObligation.validUntil.asc().nulls_last())
                    .limit(2)
                )
            ).scalars().all()
            for o in cons:
                st = getattr(o, "status", None)
                tone = "critical" if st == "OVERDUE" else "warning" if st == "DUE_SOON" else "positive"
                rows.append(
                    S.FacilityRollupRow(
                        id=f"consent-{o.id}", primaryText=getattr(o, "title", "Environmental consent"),
                        secondaryText="Statutory Obligations register", statusLabel=titleish(st),
                        statusTone=tone, trailingText=_fmtdate(getattr(o, "validUntil", None)),
                        drillTo=_link("obligations", "/compliance", siteId=site_id),
                    )
                )
                consent_added = True
        except Exception:
            pass
    if not consent_added and cur.consentStatus:
        rows.append(S.FacilityRollupRow(
            id="consent-env", primaryText=cur.consentStatus, secondaryText="SPCB consent (ESG record)",
            statusLabel="On record", statusTone="positive"))

    block.rows = rows
    return block


def titleish(s: str | None) -> str:
    if not s:
        return ""
    return s.replace("_", " ").title()


# Proper display labels for the certification-type enum (titleish would mangle
# acronyms like "ISO_9001" → "Iso 9001"). Mirrors CERT_TYPE_LABEL on the client.
CERT_LABEL = {
    "SA8000": "SA8000", "ISO_9001": "ISO 9001", "ISO_14001": "ISO 14001",
    "ISO_45001": "ISO 45001", "WRAP": "WRAP", "BSCI": "BSCI",
    "OEKO_TEX": "OEKO-TEX", "GOTS": "GOTS", "SEDEX_SMETA": "SEDEX / SMETA", "OTHER": "Other",
}


# ── Certifications rollup (FactoryCertification) ─────────────────────────────
async def certifications_block(db: AsyncSession, profile_id: str, site_id: str) -> "S.FacilityMetricBlock":
    block = S.FacilityMetricBlock(
        domainKey="certifications", title="Certifications status",
        caption="Live from the certifications register — site-scoped.",
        drillTo=_link("certifications", f"/facilities/{profile_id}", tab="certifications"),
    )
    certs = (
        await db.execute(
            select(FactoryCertification)
            .where(FactoryCertification.factoryProfileId == profile_id)
            .where(FactoryCertification.isDeleted.is_(False))
            .order_by(FactoryCertification.certificationType.asc())
        )
    ).scalars().all()
    statuses = [(c, fsvc.compute_cert_status(c.expiryDate, c.renewalLeadDays, c.status)) for c in certs]
    active = sum(1 for _, s in statuses if s not in ("EXPIRED", "SUSPENDED"))
    expiring = sum(1 for _, s in statuses if s == "EXPIRING_SOON")
    expired = sum(1 for _, s in statuses if s == "EXPIRED")
    block.tiles = [
        S.FacilityTile(id="active", label="Active certs", value=active, state="good"),
        S.FacilityTile(id="expiring", label="Expiring ≤90d", value=expiring, state="watch" if expiring else "good"),
        S.FacilityTile(id="expired", label="Expired", value=expired, state="breach" if expired else "good"),
    ]
    tone_for = {"VALID": "positive", "EXPIRING_SOON": "warning", "EXPIRED": "critical",
                "SUSPENDED": "critical", "UNDER_RENEWAL": "warning"}
    rows = []
    for c, st in statuses:
        d2e = fsvc.cert_days_to_expiry(c.expiryDate)
        rows.append(
            S.FacilityRollupRow(
                id=c.id, primaryText=CERT_LABEL.get(c.certificationType, titleish(c.certificationType)),
                secondaryText=(c.issuingBody or None),
                statusLabel=titleish(st), statusTone=tone_for.get(st, "muted"),
                trailingText=(f"{_fmtdate(c.expiryDate)}" + (f" · {d2e}d" if d2e is not None and st == "EXPIRING_SOON" else "")),
                drillTo=_link("certifications", f"/facilities/{profile_id}", tab="certifications"),
            )
        )
    block.rows = rows
    block.emptyText = "No certifications recorded for this site."
    return block


# ── Training & competency coverage (CompetencyRecord + User) ─────────────────
async def training_block(db: AsyncSession, site_id: str) -> "S.FacilityMetricBlock":
    block = S.FacilityMetricBlock(
        domainKey="training", title="Training & Competency",
        caption="Live from the Skill Matrix / competency engine — site-scoped.",
        drillTo=_link("skillMatrix", "/skill-matrix", plantId=site_id),
    )
    if CompetencyRecord is None:
        block.enabled = False
        block.notEnabledText = "Training & competency — module not enabled · roadmap"
        return block
    recs = (
        await db.execute(select(CompetencyRecord).where(CompetencyRecord.plantId == site_id))
    ).scalars().all()
    if not recs:
        block.emptyText = "No competency records at this site yet."
        block.tiles = [S.FacilityTile(id="valid", label="Mandatory valid", value="—", unit="%", state="neutral")]
        return block

    comp_ids = {r.competencyId for r in recs}
    comps = (await db.execute(select(Competency).where(Competency.id.in_(comp_ids)))).scalars().all()
    cname = {c.id: c.name for c in comps}

    now = _now()
    soon = now + timedelta(days=30)
    by_worker: dict[str, list] = defaultdict(list)
    for r in recs:
        by_worker[r.personUserId].append(r)
    covered = len(by_worker)
    fully_valid = sum(1 for rs in by_worker.values() if all(x.state in COMP_VALID for x in rs))
    valid_pct = round(fully_valid / covered * 100, 1) if covered else None
    expiring = sum(1 for r in recs if r.state in COMP_VALID and r.validUntil and now <= _aware(r.validUntil) <= soon)
    expired = sum(1 for r in recs if r.state in COMP_EXPIRED)

    total = covered
    if User is not None:
        try:
            uc = (await db.execute(select(func.count()).select_from(User).where(User.plantId == site_id))).scalar() or 0
            total = max(int(uc), covered)
        except Exception:
            total = covered

    valid_state = "neutral" if valid_pct is None else "good" if valid_pct >= 95 else "watch" if valid_pct >= 85 else "breach"
    block.tiles = [
        S.FacilityTile(id="valid", label="Mandatory valid", value=valid_pct, unit="%", state=valid_state),
        S.FacilityTile(id="expiring", label="Expiring ≤30d", value=expiring, state="watch" if expiring else "good"),
        S.FacilityTile(id="expired", label="Expired", value=expired, state="breach" if expired else "good"),
        S.FacilityTile(id="coverage", label="Workers covered", value=f"{covered}/{total}",
                       state="good" if covered >= total else "watch"),
    ]

    # Rows grouped by competency: surface only the ones with lapses/expiries.
    agg: dict[str, dict[str, int]] = defaultdict(lambda: {"expiring": 0, "expired": 0})
    for r in recs:
        if r.state in COMP_EXPIRED:
            agg[r.competencyId]["expired"] += 1
        elif r.state in COMP_VALID and r.validUntil and now <= _aware(r.validUntil) <= soon:
            agg[r.competencyId]["expiring"] += 1
    rows = []
    for cid, a in agg.items():
        if not (a["expiring"] or a["expired"]):
            continue
        nm = cname.get(cid, "Competency")
        if a["expired"]:
            rows.append(S.FacilityRollupRow(
                id=f"{cid}-exp", primaryText=nm, statusLabel="Expired", statusTone="critical",
                trailingText=f"{a['expired']} expired", drillTo=_link("skillMatrix", "/skill-matrix", plantId=site_id)))
        if a["expiring"]:
            rows.append(S.FacilityRollupRow(
                id=f"{cid}-soon", primaryText=nm, statusLabel="Expiring", statusTone="warning",
                trailingText=f"{a['expiring']} expiring ≤30d", drillTo=_link("skillMatrix", "/skill-matrix", plantId=site_id)))
    block.rows = rows
    block.emptyText = "All mandatory competencies current at this site."
    return block


def _ev(x):
    """Normalise a (str, Enum) column value to its string form."""
    return getattr(x, "value", x)


# ── Social-Compliance (SA8000) health score — garment-gated ──────────────────
# Per-dimension penalties from a clean 100: a single ATTENTION ⇒ 86 (the §7
# worked example), matching the SA8000 element model in app/services/factory.py.
SOCIAL_PENALTY = {"NON_COMPLIANT": 20, "ATTENTION": 14, "NOT_ASSESSED": 8, "COMPLIANT": 0}
SOCIAL_DIMENSIONS = (
    ("minimumWageCompliant", "Minimum wage compliant"),
    ("wagesPaidOnTime", "Wages paid on time"),
    ("overtimeVoluntary", "Overtime voluntary / within limits"),
    ("weeklyRestDayProvided", "Weekly rest day provided"),
    ("unionOrWorkerCommitteePresent", "Freedom of association"),
    ("noDepositOrDocumentRetention", "No forced labour (deposits / ID)"),
    ("grievanceMechanismPresent", "Grievance mechanism active"),
    ("antiDiscriminationPolicy", "Anti-discrimination policy"),
)
SOCIAL_TONE = {"COMPLIANT": "positive", "ATTENTION": "warning", "NON_COMPLIANT": "critical", "NOT_ASSESSED": "muted"}


def _is_garment(profile) -> bool:
    pi = (getattr(profile, "primaryIndustry", "") or "").lower()
    return any(k in pi for k in ("garment", "textile", "apparel"))


async def social_block(db: AsyncSession, profile, site_id: str) -> "S.FacilityMetricBlock | None":
    # Garment-gated: omit entirely on non-garment sites (not a neutral tile — a
    # social-compliance gap shouldn't be implied where the lens doesn't apply).
    if not _is_garment(profile):
        return None
    block = S.FacilityMetricBlock(
        domainKey="socialCompliance", title="Social compliance (SA8000)",
        caption="Live from the SA8000 workforce master — site-scoped.",
        drillTo=_link("workforceSA8000", f"/facilities/{profile.id}", tab="workforce"),
    )
    sp = (
        await db.execute(
            select(SocialComplianceProfile)
            .where(SocialComplianceProfile.factoryProfileId == profile.id)
            .where(SocialComplianceProfile.isDeleted.is_(False))
        )
    ).scalar_one_or_none()

    # Child-labour signal from the current workforce composition (SA8000 El.1).
    child = False
    try:
        wc = (
            await db.execute(
                select(WorkforceComposition)
                .where(WorkforceComposition.factoryProfileId == profile.id)
                .where(WorkforceComposition.isCurrent.is_(True))
                .where(WorkforceComposition.isDeleted.is_(False))
            )
        ).scalars().first()
        if wc:
            child = fsvc.child_labour_flag(wc.youngestWorkerAge, wc.workersUnder18Count, wc.minHiringAgePolicy)
    except Exception:
        pass

    if sp is None:
        block.tiles = [S.FacilityTile(id="score", label="Social score", value="—", unit="/100", state="neutral")]
        block.emptyText = "No SA8000 social-compliance profile recorded for this site."
        return block

    score = 100
    open_flags = 0
    rows: list[S.FacilityRollupRow] = []
    for field, label in SOCIAL_DIMENSIONS:
        flag = getattr(sp, field, None) or "NOT_ASSESSED"
        score -= SOCIAL_PENALTY.get(flag, 0)
        if flag != "COMPLIANT":
            open_flags += 1
        rows.append(S.FacilityRollupRow(
            id=field, primaryText=label, statusLabel=titleish(flag), statusTone=SOCIAL_TONE.get(flag, "muted"),
            drillTo=block.drillTo))
    if fsvc.overtime_exceeds_cap(sp.maxWeeklyOvertimeHours):
        score -= 10
    if child:
        score -= 25
        open_flags += 1
    score = max(0, min(100, score))

    rows.append(S.FacilityRollupRow(
        id="childLabour", primaryText="Child-labour evidence",
        statusLabel=("Flag raised" if child else "Clear"), statusTone=("critical" if child else "positive"),
        drillTo=block.drillTo))
    tcov = sp.sa8000AwarenessTrainingPct
    tcov_tone = "positive" if (tcov or 0) >= 90 else "warning" if (tcov or 0) >= 75 else "critical"
    rows.append(S.FacilityRollupRow(
        id="training", primaryText="SA8000 training coverage", statusTone=tcov_tone,
        trailingText=(f"{_num(tcov)}%" if tcov is not None else "—"), drillTo=block.drillTo))

    score_state = "good" if score >= 90 else "watch" if score >= 75 else "breach"
    tcov_state = "good" if (tcov or 0) >= 90 else "watch" if (tcov or 0) >= 75 else "breach"
    block.tiles = [
        S.FacilityTile(id="score", label="Social score", value=int(round(score)), unit="/100", state=score_state),
        S.FacilityTile(id="flags", label="Open flags", value=open_flags, state="watch" if open_flags else "good"),
        S.FacilityTile(id="training", label="SA8000 training", value=tcov, unit="%", state=tcov_state),
    ]
    block.rows = rows
    return block


# ── Live operational-risk snapshot (PTW / MOC / HIRA) — point-in-time, uncached ─
async def operational_risk_block(db: AsyncSession, site_id: str) -> "S.FacilityMetricBlock":
    block = S.FacilityMetricBlock(
        domainKey="operationalRisk", title="Live operational risk",
        caption="Live from the PTW, MOC & HIRA engines — site-scoped, point-in-time.",
        drillTo=_link("ptw", "/ptw", plantId=site_id), lastRefreshedAt=_now(),
    )
    now = _now()
    rows: list[S.FacilityRollupRow] = []
    active = high_open = open_moc = overdue_hira = 0

    if Permit is not None:
        try:
            perms = (await db.execute(select(Permit).where(Permit.plantId == site_id))).scalars().all()
            for p in perms:
                st, ty = _ev(p.status), _ev(p.type)
                if st == "ACTIVE" and (p.validTo is None or _aware(p.validTo) >= now):
                    active += 1
                    if ty in PERMIT_HIGH_RISK:
                        high_open += 1
                        rows.append(S.FacilityRollupRow(
                            id=f"permit-{p.id}", primaryText=titleish(ty),
                            secondaryText=getattr(p, "location", None), statusLabel="Active", statusTone="warning",
                            trailingText=getattr(p, "number", None),
                            drillTo=_link("ptw", "/ptw", type=ty, status="ACTIVE")))
        except Exception:
            block.degraded = True

    if HiraStudy is not None:
        try:
            studies = (await db.execute(select(HiraStudy).where(HiraStudy.plantId == site_id))).scalars().all()
            for s in studies:
                nxt = getattr(s, "nextScheduledReviewDate", None)
                if _ev(getattr(s, "status", None)) == "ACTIVE" and nxt and _aware(nxt) < now:
                    overdue_hira += 1
                    rows.append(S.FacilityRollupRow(
                        id=f"hira-{s.id}", primaryText=getattr(s, "title", "HIRA study"),
                        statusLabel="Review overdue", statusTone="critical", trailingText=_fmtdate(nxt),
                        drillTo=_link("hira", "/hira", plantId=site_id)))
        except Exception:
            block.degraded = True

    if ChangeRequest is not None:
        try:
            mocs = (await db.execute(select(ChangeRequest).where(ChangeRequest.plantId == site_id))).scalars().all()
            for c in mocs:
                if (getattr(c, "status", "") or "") not in MOC_CLOSED:
                    open_moc += 1
                    rows.append(S.FacilityRollupRow(
                        id=f"moc-{c.id}", primaryText=getattr(c, "title", "Change request"),
                        secondaryText=titleish(getattr(c, "status", None)), statusLabel="Open", statusTone="warning",
                        trailingText=getattr(c, "number", None),
                        drillTo=_link("moc", "/moc", plantId=site_id)))
        except Exception:
            block.degraded = True

    block.tiles = [
        S.FacilityTile(id="activePermits", label="Active permits", value=active, state="watch" if high_open else "good"),
        S.FacilityTile(id="highRisk", label="High-risk open", value=high_open,
                       state="breach" if high_open >= 3 else "watch" if high_open else "good"),
        S.FacilityTile(id="overdueHira", label="Overdue HIRA", value=overdue_hira, state="breach" if overdue_hira else "good"),
        S.FacilityTile(id="openMoc", label="Open MOC", value=open_moc, state="watch" if open_moc else "good"),
    ]
    block.rows = rows
    block.emptyText = "No active permits, overdue HIRA reviews or open changes right now."
    return block
