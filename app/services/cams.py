"""CAMS service layer — shared by the CAMS router and (later) consumer modules.

Owns the genuinely-shared engine behaviour:
  • tenant-scoped sequential code generation (AUD-/INS-/TPL-/FND-/AT-)
  • the recurrence engine (auto-generate engagements ahead of due date)
  • checklist scoring + NC severity roll-up to overallResult
  • auto-creation of findings from non-conforming answers (ncTriggersFinding)
  • raising a CAPA from a finding via the existing AUDIT* CAPA source types
  • the engagement closure gate (MAJOR/CRITICAL finding ⇒ CAPA required)

Standalone-mode safe: every cross-module reference (EnterpriseRisk, Skill
Matrix, Equipment) is a plain id with no hard FK, so absence degrades to an
empty field rather than an error.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy.orm import selectinload

from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.capa import Capa, CapaSourceCategory, CapaSourceType
from app.models.cams import (
    CamsAuditType,
    CamsComplianceLink,
    CamsEngagement,
    CamsFinding,
    CamsRecurrence,
    CamsResponse,
    CamsTemplate,
    CamsTemplateQuestion,
    CamsTemplateSection,
)
from app.models.plant import Plant
from app.models.user import User

# Obligations come through a SERVICE BOUNDARY, not a direct model import
# (WP-52, open question Q8 — Statutory Registers is the system of record).
#
# The previous code imported `app.models.erm_p2.LegalObligation` inside a bare
# try/except and, on failure, returned an all-zero compliance payload. A broken
# dependency therefore rendered as "0 obligations, 0% assurance" — which reads
# as GOOD NEWS on a compliance dashboard and is indistinguishable from a
# genuinely empty register. That silent fallback is deleted; see
# app/services/obligations.py.
from app.services import obligations as obligations_svc


def now() -> datetime:
    return datetime.now(timezone.utc)


# ── name / lookup helpers ────────────────────────────────────────────────────
async def user_name_map(db: AsyncSession, ids: Iterable[str | None]) -> dict[str, str]:
    clean = {i for i in ids if i}
    if not clean:
        return {}
    rows = (await db.execute(select(User).where(User.id.in_(clean)))).scalars().all()
    return {u.id: (u.name or u.email or u.id) for u in rows}


async def plant_name_map(db: AsyncSession, ids: Iterable[str | None]) -> dict[str, str]:
    clean = {i for i in ids if i}
    if not clean:
        return {}
    rows = (await db.execute(select(Plant).where(Plant.id.in_(clean)))).scalars().all()
    return {p.id: p.name for p in rows}


# ── code generation (tenant-scoped sequential, mirrors the ERM convention) ────
async def next_audit_type_code(db: AsyncSession) -> str:
    n = (await db.execute(select(func.count()).select_from(CamsAuditType))).scalar() or 0
    return f"AT-{(n + 1):04d}"


async def next_template_code(db: AsyncSession) -> str:
    n = (await db.execute(select(func.count()).select_from(CamsTemplate))).scalar() or 0
    return f"TPL-{(n + 1):04d}"


def _engagement_prefix(engagement_type: str) -> str:
    return "INS" if engagement_type == "INSPECTION" else "AUD"


async def next_engagement_code(db: AsyncSession, engagement_type: str) -> str:
    prefix = _engagement_prefix(engagement_type)
    year = now().year
    like = f"{prefix}-{year}-%"
    n = (
        await db.execute(
            select(func.count()).select_from(CamsEngagement).where(CamsEngagement.engagementCode.like(like))
        )
    ).scalar() or 0
    return f"{prefix}-{year}-{(n + 1):04d}"


async def next_finding_code(db: AsyncSession) -> str:
    year = now().year
    like = f"FND-{year}-%"
    n = (
        await db.execute(
            select(func.count()).select_from(CamsFinding).where(CamsFinding.findingCode.like(like))
        )
    ).scalar() or 0
    return f"FND-{year}-{(n + 1):04d}"


# ── recurrence ────────────────────────────────────────────────────────────────
_FREQ_DAYS = {
    "WEEKLY": 7,
    "MONTHLY": 30,
    "QUARTERLY": 91,
    "HALF_YEARLY": 182,
    "ANNUAL": 365,
}


def frequency_to_days(frequency: str, custom_interval_days: int | None) -> int:
    if frequency == "CUSTOM_DAYS":
        return max(1, custom_interval_days or 30)
    return _FREQ_DAYS.get(frequency, 30)


async def generate_due_engagements(db: AsyncSession, *, actor_id: str | None = None) -> dict[str, Any]:
    """Walk active recurrence rules; create engagements whose next-due date falls
    within `leadTimeDays` of now. Idempotent: skips a (recurrence, site, day)
    that already has an engagement. Returns a summary the caller can surface."""
    rules = (
        await db.execute(select(CamsRecurrence).where(CamsRecurrence.isActive.is_(True)).where(CamsRecurrence.isDeleted.is_(False)))
    ).scalars().all()
    created: list[str] = []
    today = now()
    for r in rules:
        interval = frequency_to_days(r.frequency, r.customIntervalDays)
        # Prisma stores DateTime as `timestamp` WITHOUT tz, so SQLAlchemy reads it
        # back naive; `today` is tz-aware. Normalise to aware-UTC before arithmetic.
        last = r.lastGeneratedAt
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last is None:
            last = today - timedelta(days=interval)
        next_due = last + timedelta(days=interval)
        # Only generate when due date is within the lead window.
        if next_due - today > timedelta(days=r.leadTimeDays):
            continue
        atype = await db.get(CamsAuditType, r.auditTypeId) if r.auditTypeId else None
        engagement_type = atype.engagementType if atype else "INSPECTION"
        sites = r.siteScope or [None]
        any_made = False
        for site_id in sites:
            # dedupe: same recurrence + site + same planned day
            existing = (
                await db.execute(
                    select(func.count())
                    .select_from(CamsEngagement)
                    .where(CamsEngagement.recurrenceId == r.id)
                    .where(CamsEngagement.siteId == site_id)
                    .where(func.date(CamsEngagement.plannedDate) == next_due.date())
                )
            ).scalar() or 0
            if existing:
                continue
            code = await next_engagement_code(db, engagement_type)
            title = f"{atype.name if atype else 'Scheduled Inspection'} — {next_due.date().isoformat()}"
            eng = CamsEngagement(
                engagementCode=code,
                title=title[:200],
                engagementType=engagement_type,
                auditTypeId=r.auditTypeId,
                standardRefs=(atype.standardRefs if atype else []) or [],
                siteId=site_id,
                scopeStatement="Auto-generated from recurrence rule.",
                leadAuditorId=r.defaultLeadAuditorId or (actor_id or ""),
                auditTeamIds=[],
                plannedDate=next_due,
                templateId=r.templateId or (atype.defaultTemplateId if atype else None),
                status="SCHEDULED",
                riskBasis="ROUTINE",
                recurrenceId=r.id,
                createdBy=actor_id,
            )
            db.add(eng)
            # MUST flush before the next next_engagement_code() call: the session
            # is autoflush=False, so without this the count-based code generator
            # would mint the SAME code for every site in this run → unique violation.
            await db.flush()
            created.append(code)
            any_made = True
        if any_made:
            r.lastGeneratedAt = next_due
    return {"generated": len(created), "codes": created}


# ── scoring + NC roll-up ──────────────────────────────────────────────────────
_NC_RANK = {"OBSERVATION": 0, "MINOR_NC": 1, "MAJOR_NC": 2, "CRITICAL_NC": 3}
_RESULT_FOR_RANK = {0: "CONFORMING", 1: "MINOR_NC", 2: "MAJOR_NC", 3: "CRITICAL_NC"}


def _answer_conformance(ans: dict[str, Any]) -> str | None:
    """Derive CONFORM / NC / NA from a stored answer dict."""
    c = ans.get("conformance")
    if c in ("CONFORM", "NC", "NA"):
        return c
    return None


def compute_score(
    sections: list[CamsTemplateSection],
    answers_by_q: dict[str, dict[str, Any]],
    scoring_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return {scorePercent, overallResult, sectionScores[]} per scoring mode.

    sections must have .questions loaded. answers_by_q maps questionId → answer.
    """
    cfg = scoring_config or {}
    mode = cfg.get("mode", "PERCENT_CONFORMANCE")
    section_scores: list[dict[str, Any]] = []
    worst_nc_rank = 0

    total_conform = 0
    total_assessed = 0
    weighted_num = 0.0
    weighted_den = 0.0

    for sec in sections:
        sec_conform = 0
        sec_assessed = 0
        for q in sec.questions:
            ans = answers_by_q.get(q.id)
            if not ans:
                continue
            conf = _answer_conformance(ans)
            if conf == "NA" or conf is None:
                continue
            sec_assessed += 1
            if conf == "CONFORM":
                sec_conform += 1
            elif conf == "NC":
                sev = (ans.get("ncSeverity") or "MINOR_NC")
                worst_nc_rank = max(worst_nc_rank, _NC_RANK.get(sev, 1))
        sec_pct = round((sec_conform / sec_assessed) * 100, 1) if sec_assessed else None
        section_scores.append({"sectionId": sec.id, "scorePercent": sec_pct})
        total_conform += sec_conform
        total_assessed += sec_assessed
        if sec_pct is not None:
            w = sec.weightPct if sec.weightPct is not None else (100.0 / max(1, len(sections)))
            weighted_num += sec_pct * w
            weighted_den += w

    overall_result = _RESULT_FOR_RANK[worst_nc_rank]

    if mode == "NONE":
        score_pct = None
    elif mode == "PASS_FAIL":
        score_pct = 100.0 if worst_nc_rank == 0 else 0.0
    elif mode == "WEIGHTED_SCORE":
        score_pct = round(weighted_num / weighted_den, 1) if weighted_den else None
    else:  # PERCENT_CONFORMANCE
        score_pct = round((total_conform / total_assessed) * 100, 1) if total_assessed else None

    return {"scorePercent": score_pct, "overallResult": overall_result, "sectionScores": section_scores}


# ── findings auto-creation from NC answers ────────────────────────────────────
async def sync_findings_from_answers(
    db: AsyncSession,
    engagement: CamsEngagement,
    sections: list[CamsTemplateSection],
    answers_by_q: dict[str, dict[str, Any]],
    *,
    actor_id: str | None = None,
) -> int:
    """For each NC answer on a question with ncTriggersFinding=true and no
    finding yet, create a CamsFinding pre-filled from the question. Returns the
    number created. Mutates answers in place to set findingId."""
    created = 0
    q_index = {q.id: (q, sec) for sec in sections for q in sec.questions}
    for qid, ans in answers_by_q.items():
        if _answer_conformance(ans) != "NC":
            continue
        if ans.get("findingId"):
            continue
        q_sec = q_index.get(qid)
        if not q_sec:
            continue
        q, _sec = q_sec
        if not q.ncTriggersFinding:
            continue
        sev = ans.get("ncSeverity") or "MINOR_NC"
        code = await next_finding_code(db)
        f = CamsFinding(
            findingCode=code,
            engagementId=engagement.id,
            sourceQuestionId=qid,
            title=(q.text or "Non-conformance")[:200],
            description=ans.get("note") or q.text or "",
            severity=sev,
            standardClauseRef=q.standardClauseRef,
            siteId=engagement.siteId,
            areaOrAssetRef=engagement.areaOrAssetRef,
            ownerId=engagement.auditeeOwnerId,
            status="OPEN",
            evidenceAttachmentIds=ans.get("evidenceAttachmentIds") or [],
            createdBy=actor_id,
        )
        db.add(f)
        await db.flush()
        ans["findingId"] = f.id
        created += 1
    return created


# ── findings → CAPA (AUDIT source) ────────────────────────────────────────────
def capa_source_code_for(engagement_type: str) -> str:
    """Map an engagement type to the existing AUDIT* CAPA source type code."""
    if engagement_type == "COMPLIANCE_AUDIT":
        return "AUDIT_REGULATORY"
    if engagement_type == "SUPPLIER_AUDIT":
        return "AUDIT_EXTERNAL"
    return "AUDIT_INTERNAL"


_SEVERITY_TO_CAPA = {
    "OBSERVATION": "LOW",
    "OPPORTUNITY_FOR_IMPROVEMENT": "LOW",
    "MINOR_NC": "MODERATE",
    "MAJOR_NC": "HIGH",
    "CRITICAL_NC": "CRITICAL",
}


async def raise_capa_for_finding(
    db: AsyncSession,
    finding: CamsFinding,
    engagement: CamsEngagement,
    actor_id: str,
) -> Capa:
    """Create a CAPA on the existing AUDIT* source type and link it to the
    finding. No new CAPA source type is introduced (constraint §1.3.4)."""
    source_code = capa_source_code_for(engagement.engagementType)
    st = (await db.execute(select(CapaSourceType).where(CapaSourceType.code == source_code))).scalar_one_or_none()
    if st is None:
        raise ValueError(f"CAPA source type '{source_code}' is not seeded.")
    cat = await db.get(CapaSourceCategory, st.categoryId)
    plant = None
    if engagement.siteId:
        plant = await db.get(Plant, engagement.siteId)
    if plant is None:
        plant = (await db.execute(select(Plant).order_by(Plant.code).limit(1))).scalar_one_or_none()
    if plant is None:
        raise ValueError("No plant available to scope the CAPA.")
    year = now().year
    count = (
        await db.execute(
            select(func.count()).select_from(Capa).where(Capa.plantId == plant.id).where(Capa.sourceCategoryId == st.categoryId)
        )
    ).scalar() or 0
    prefix = cat.prefix if cat else "AUD"
    capa = Capa(
        capaNumber=f"CAPA-{prefix}-{year}-{plant.code}-{(count + 1):03d}",
        title=f"Audit finding: {finding.title}"[:200],
        plantId=plant.id,
        sourceCategoryId=st.categoryId,
        sourceTypeId=st.id,
        sourceTypeCode=source_code,
        sourceReferenceId=finding.id,
        sourceReferenceUrl=f"/cams/findings/{finding.id}",
        sourceReferenceSummary=f"{finding.findingCode} — {engagement.engagementCode}",
        sourceMetadata={
            "findingCode": finding.findingCode,
            "engagementCode": engagement.engagementCode,
            "standardClauseRef": finding.standardClauseRef,
            "severity": finding.severity,
        },
        problemDescription=finding.description or finding.title,
        detectionMethod="AUDIT_FINDING",
        detectedAt=now(),
        detectedByUserId=actor_id,
        primaryCategory="Audit / Compliance",
        actionType="CORRECTIVE_AND_PREVENTIVE",
        severity=_SEVERITY_TO_CAPA.get(finding.severity, "MODERATE"),
        priority="HIGH" if finding.severity in ("MAJOR_NC", "CRITICAL_NC") else "MODERATE",
        state="SUBMITTED",
        stateChangedAt=now(),
        stateChangedByUserId=actor_id,
        raisedByUserId=actor_id,
        primaryOwnerUserId=finding.ownerId or engagement.auditeeOwnerId or actor_id,
        createdByUserId=actor_id,
    )
    db.add(capa)
    await db.flush()
    finding.capaId = capa.id
    if finding.status == "OPEN":
        finding.status = "CAPA_RAISED"
    return capa


# ── closure gate ──────────────────────────────────────────────────────────────
async def engagement_close_blockers(db: AsyncSession, engagement_id: str) -> list[str]:
    """Return human-readable reasons an engagement cannot be CLOSED yet.

    Rule (§2): MAJOR_NC / CRITICAL_NC findings require a CAPA before close;
    findings must reach CLOSED/ACCEPTED_RISK."""
    findings = (
        await db.execute(select(CamsFinding).where(CamsFinding.engagementId == engagement_id).where(CamsFinding.isDeleted.is_(False)))
    ).scalars().all()
    blockers: list[str] = []
    for f in findings:
        if f.severity in ("MAJOR_NC", "CRITICAL_NC") and not f.capaId:
            blockers.append(f"{f.findingCode} ({f.severity}) has no CAPA raised.")
        if f.status not in ("CLOSED", "ACCEPTED_RISK"):
            blockers.append(f"{f.findingCode} is not resolved (status {f.status}).")
    return blockers


# ── ISO clause catalogue (data-driven; standalone-shipped) ────────────────────
CLAUSE_CATALOGUE: list[dict[str, str]] = [
    # ISO 45001:2018 — OH&S
    {"standard": "ISO 45001", "clause": "ISO 45001:5.1", "title": "Leadership & commitment"},
    {"standard": "ISO 45001", "clause": "ISO 45001:5.4", "title": "Consultation & participation of workers"},
    {"standard": "ISO 45001", "clause": "ISO 45001:6.1.2", "title": "Hazard identification & assessment of risks"},
    {"standard": "ISO 45001", "clause": "ISO 45001:6.1.3", "title": "Determination of legal requirements"},
    {"standard": "ISO 45001", "clause": "ISO 45001:7.2", "title": "Competence"},
    {"standard": "ISO 45001", "clause": "ISO 45001:7.4", "title": "Communication"},
    {"standard": "ISO 45001", "clause": "ISO 45001:8.1.1", "title": "Operational planning & control"},
    {"standard": "ISO 45001", "clause": "ISO 45001:8.1.2", "title": "Eliminating hazards & reducing risks"},
    {"standard": "ISO 45001", "clause": "ISO 45001:8.1.3", "title": "Management of change"},
    {"standard": "ISO 45001", "clause": "ISO 45001:8.1.4", "title": "Procurement & contractors"},
    {"standard": "ISO 45001", "clause": "ISO 45001:8.2", "title": "Emergency preparedness & response"},
    {"standard": "ISO 45001", "clause": "ISO 45001:9.1", "title": "Monitoring, measurement, analysis & evaluation"},
    {"standard": "ISO 45001", "clause": "ISO 45001:9.2", "title": "Internal audit"},
    {"standard": "ISO 45001", "clause": "ISO 45001:10.2", "title": "Incident, nonconformity & corrective action"},
    # ISO 14001:2015 — Environment
    {"standard": "ISO 14001", "clause": "ISO 14001:6.1.2", "title": "Environmental aspects"},
    {"standard": "ISO 14001", "clause": "ISO 14001:6.1.3", "title": "Compliance obligations"},
    {"standard": "ISO 14001", "clause": "ISO 14001:8.1", "title": "Operational planning & control"},
    {"standard": "ISO 14001", "clause": "ISO 14001:8.2", "title": "Emergency preparedness & response"},
    {"standard": "ISO 14001", "clause": "ISO 14001:9.1.2", "title": "Evaluation of compliance"},
    {"standard": "ISO 14001", "clause": "ISO 14001:10.2", "title": "Nonconformity & corrective action"},
    # ISO 9001:2015 — Quality
    {"standard": "ISO 9001", "clause": "ISO 9001:7.1.5", "title": "Monitoring & measuring resources"},
    {"standard": "ISO 9001", "clause": "ISO 9001:8.5.1", "title": "Control of production & service provision"},
    {"standard": "ISO 9001", "clause": "ISO 9001:8.7", "title": "Control of nonconforming outputs"},
    {"standard": "ISO 9001", "clause": "ISO 9001:9.2", "title": "Internal audit"},
    {"standard": "ISO 9001", "clause": "ISO 9001:10.2", "title": "Nonconformity & corrective action"},
]

_CONDUCTED = ("FIELDWORK_COMPLETE", "FINDINGS_REVIEW", "REPORT_ISSUED", "CLOSED")
_OPEN_FINDING = lambda s: s not in ("CLOSED", "ACCEPTED_RISK")  # noqa: E731


def _as_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Analytics & Benchmarking (C-13) ───────────────────────────────────────────
async def compute_analytics(db: AsyncSession) -> dict[str, Any]:
    today = now()
    engagements = (
        await db.execute(select(CamsEngagement).where(CamsEngagement.isDeleted.is_(False)))
    ).scalars().all()
    findings = (
        await db.execute(select(CamsFinding).where(CamsFinding.isDeleted.is_(False)))
    ).scalars().all()

    # Template → set of clause refs (for clause-conformance assessments).
    tpl_ids = {e.templateId for e in engagements if e.templateId}
    tpl_clauses: dict[str, set[str]] = {}
    if tpl_ids:
        tpls = (
            await db.execute(
                select(CamsTemplate)
                .where(CamsTemplate.id.in_(tpl_ids))
                .options(selectinload(CamsTemplate.sections).selectinload(CamsTemplateSection.questions))
            )
        ).scalars().all()
        for t in tpls:
            tpl_clauses[t.id] = {q.standardClauseRef for s in t.sections for q in s.questions if q.standardClauseRef}

    plants = await plant_name_map(db, [e.siteId for e in engagements])

    # Programme health.
    status_counts: dict[str, int] = {}
    for e in engagements:
        status_counts[e.status] = status_counts.get(e.status, 0) + 1
    overdue = sum(
        1 for e in engagements
        if e.status in ("PLANNED", "SCHEDULED") and _as_aware(e.plannedDate) and _as_aware(e.plannedDate) < today
    )
    total = len(engagements)
    conducted_n = sum(1 for e in engagements if e.status in _CONDUCTED)
    programme = {
        "planned": status_counts.get("PLANNED", 0),
        "scheduled": status_counts.get("SCHEDULED", 0),
        "inProgress": status_counts.get("IN_PROGRESS", 0),
        "fieldworkComplete": status_counts.get("FIELDWORK_COMPLETE", 0),
        "reportIssued": status_counts.get("REPORT_ISSUED", 0),
        "closed": status_counts.get("CLOSED", 0),
        "cancelled": status_counts.get("CANCELLED", 0),
        "overdue": overdue,
        "total": total,
        "completionRatePct": round((conducted_n / total) * 100, 1) if total else 0,
    }

    # Findings.
    sev_counts: dict[str, int] = {}
    for f in findings:
        sev_counts[f.severity] = sev_counts.get(f.severity, 0) + 1
    repeat = sum(1 for f in findings if f.isRepeatFinding)
    repeat_rate = round((repeat / len(findings)) * 100, 1) if findings else 0
    open_findings = sum(1 for f in findings if _OPEN_FINDING(f.status))
    closure_days = [
        (_as_aware(f.closedAt) - _as_aware(f.createdAt)).days
        for f in findings
        if f.closedAt and f.createdAt
    ]
    avg_closure = round(sum(closure_days) / len(closure_days), 1) if closure_days else None

    by_type: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for e in engagements:
        by_type[e.engagementType] = by_type.get(e.engagementType, 0) + 1
        key = e.sourceModule or "CAMS-native"
        by_source[key] = by_source.get(key, 0) + 1

    # Findings indexed by engagement.
    findings_by_eng: dict[str, list] = {}
    for f in findings:
        findings_by_eng.setdefault(f.engagementId, []).append(f)

    # Benchmarking by site.
    site_ids = sorted({e.siteId for e in engagements}, key=lambda x: (x is None, x or ""))
    benchmarking = []
    for sid in site_ids:
        engs = [e for e in engagements if e.siteId == sid]
        cond = [e for e in engs if e.status in _CONDUCTED]
        scores = [e.scorePercent for e in cond if e.scorePercent is not None]
        site_findings = [f for e in engs for f in findings_by_eng.get(e.id, [])]
        mc = sum(1 for f in site_findings if f.severity in ("MAJOR_NC", "CRITICAL_NC"))
        rep = sum(1 for f in site_findings if f.isRepeatFinding)
        benchmarking.append({
            "siteId": sid,
            "siteName": plants.get(sid) if sid else "Corporate / unspecified",
            "auditsPlanned": len(engs),
            "auditsConducted": len(cond),
            "completionRatePct": round((len(cond) / len(engs)) * 100, 1) if engs else 0,
            "avgScorePct": round(sum(scores) / len(scores), 1) if scores else None,
            "findingCount": len(site_findings),
            "findingDensity": round(len(site_findings) / len(cond), 2) if cond else 0,
            "majorCriticalCount": mc,
            "repeatCount": rep,
        })

    # Clause conformance: each (conducted engagement, clause-in-its-template) is an
    # assessment; a finding on that engagement carrying the clause is a nonconformance.
    assess: dict[str, int] = {}
    ncs: dict[str, int] = {}
    for e in engagements:
        if e.status not in _CONDUCTED or not e.templateId:
            continue
        clauses = tpl_clauses.get(e.templateId, set())
        eng_finding_clauses = {f.standardClauseRef for f in findings_by_eng.get(e.id, []) if f.standardClauseRef}
        for c in clauses:
            assess[c] = assess.get(c, 0) + 1
            if c in eng_finding_clauses:
                ncs[c] = ncs.get(c, 0) + 1
    clause_conformance = sorted(
        [
            {"clause": c, "assessments": a, "nonConformances": ncs.get(c, 0),
             "conformancePct": round(((a - ncs.get(c, 0)) / a) * 100, 1) if a else 0}
            for c, a in assess.items()
        ],
        key=lambda r: r["conformancePct"],
    )

    # Pareto of findings by clause.
    clause_finding_counts: dict[str, int] = {}
    for f in findings:
        if f.standardClauseRef:
            clause_finding_counts[f.standardClauseRef] = clause_finding_counts.get(f.standardClauseRef, 0) + 1
    pareto = sorted(
        [{"key": c, "label": c, "count": n} for c, n in clause_finding_counts.items()],
        key=lambda r: r["count"], reverse=True,
    )[:8]

    # CAPA overdue % (AUDIT source).
    audit_codes = ("AUDIT_INTERNAL", "AUDIT_EXTERNAL", "AUDIT_REGULATORY")
    capas = (
        await db.execute(select(Capa).where(Capa.sourceTypeCode.in_(audit_codes)))
    ).scalars().all()
    capa_open = [c for c in capas if c.state not in ("CLOSED", "CLOSED_RECURRED", "CANCELLED", "REJECTED")]
    capa_overdue = sum(
        1 for c in capa_open
        if c.closureTargetDate and _as_aware(c.closureTargetDate) < today
    )
    capa_overdue_pct = round((capa_overdue / len(capa_open)) * 100, 1) if capa_open else 0

    # ── Fold in ComplianceAudit audits (centralized union) ────────────────────
    a_engs = await audit_engagements(db)
    a_finds = await audit_findings(db)
    _AUDIT_CONDUCTED = {"IN_PROGRESS", "FINDINGS_REVIEW", "REPORT_ISSUED", "CLOSED"}
    _PROG_BUCKET = {"SCHEDULED": "scheduled", "IN_PROGRESS": "inProgress",
                    "FINDINGS_REVIEW": "fieldworkComplete", "REPORT_ISSUED": "reportIssued",
                    "CLOSED": "closed", "CANCELLED": "cancelled", "PLANNED": "planned"}
    bench_by_site = {b["siteId"]: b for b in benchmarking}
    a_finds_by_site: dict[str | None, list] = {}
    for af in a_finds:
        a_finds_by_site.setdefault(af["siteId"], []).append(af)
        sev_counts[af["severity"]] = sev_counts.get(af["severity"], 0) + 1
        if af["status"] != "CLOSED":
            open_findings += 1
    for ae in a_engs:
        st = ae["status"]
        programme[_PROG_BUCKET.get(st, "inProgress")] = programme.get(_PROG_BUCKET.get(st, "inProgress"), 0) + 1
        programme["total"] += 1
        by_type["COMPLIANCE_AUDIT"] = by_type.get("COMPLIANCE_AUDIT", 0) + 1
        by_source["AUDIT"] = by_source.get("AUDIT", 0) + 1
        if st == "SCHEDULED" and ae.get("plannedDate"):
            try:
                if _as_aware(datetime.fromisoformat(ae["plannedDate"])) < today:
                    programme["overdue"] += 1
            except (TypeError, ValueError):
                pass
        # Benchmarking fold-in.
        sid = ae["siteId"]
        b = bench_by_site.get(sid)
        if b is None:
            b = {"siteId": sid, "siteName": ae.get("siteName") or "Corporate / unspecified",
                 "auditsPlanned": 0, "auditsConducted": 0, "completionRatePct": 0, "avgScorePct": None,
                 "findingCount": 0, "findingDensity": 0, "majorCriticalCount": 0, "repeatCount": 0,
                 "_scores": []}
            bench_by_site[sid] = b
            benchmarking.append(b)
        b.setdefault("_scores", [])
        b["auditsPlanned"] += 1
        if st in _AUDIT_CONDUCTED:
            b["auditsConducted"] += 1
        if ae.get("scorePercent") is not None:
            b["_scores"].append(ae["scorePercent"])
        sf = a_finds_by_site.get(sid, [])
        b["findingCount"] += len(sf)
        b["majorCriticalCount"] += sum(1 for f in sf if f["severity"] in ("MAJOR_NC", "CRITICAL_NC"))
    # Recompute blended benchmarking aggregates for sites touched by audits.
    for b in benchmarking:
        scores = b.pop("_scores", None)
        if scores:
            existing = [b["avgScorePct"]] if b["avgScorePct"] is not None else []
            alls = existing + scores
            b["avgScorePct"] = round(sum(alls) / len(alls), 1)
        b["completionRatePct"] = round((b["auditsConducted"] / b["auditsPlanned"]) * 100, 1) if b["auditsPlanned"] else 0
        b["findingDensity"] = round(b["findingCount"] / b["auditsConducted"], 2) if b["auditsConducted"] else 0
    conducted_combined = sum(programme.get(k, 0) for k in ("inProgress", "fieldworkComplete", "reportIssued", "closed"))
    programme["completionRatePct"] = round((conducted_combined / programme["total"]) * 100, 1) if programme["total"] else 0
    combined_findings_n = len(findings) + len(a_finds)
    repeat_rate = round((repeat / combined_findings_n) * 100, 1) if combined_findings_n else 0

    return {
        "programme": programme,
        "findingsBySeverity": sev_counts,
        "repeatFindingRatePct": repeat_rate,
        "avgClosureDays": avg_closure,
        "openFindingCount": open_findings,
        "byType": by_type,
        "bySourceModule": by_source,
        "benchmarkingBySite": benchmarking,
        "clauseConformance": clause_conformance,
        "paretoByClause": pareto,
        "capaOverduePct": capa_overdue_pct,
    }


# ── Compliance Tracker (C-12) ──────────────────────────────────────────────────
async def compute_compliance(db: AsyncSession) -> dict[str, Any]:
    """Statutory obligations + audit coverage.

    When the obligations register cannot be read the response says so
    (`available: False` + a reason, and NULL counts) rather than reporting
    zeros. "No obligations" and "could not read obligations" are different
    facts, and only one of them is good news — see WP-52 / F-48.
    """
    try:
        obligations = await obligations_svc.list_obligations(db)
    except obligations_svc.ObligationsUnavailable as e:
        return obligations_svc.unavailable_payload(e.reason)
    links = (
        await db.execute(select(CamsComplianceLink).where(CamsComplianceLink.isDeleted.is_(False)))
    ).scalars().all()

    eng_ids = {l.engagementId for l in links if l.engagementId}
    find_ids = {l.findingId for l in links if l.findingId}
    engs = {e.id: e for e in (await db.execute(select(CamsEngagement).where(CamsEngagement.id.in_(eng_ids)))).scalars().all()} if eng_ids else {}
    finds = {f.id: f for f in (await db.execute(select(CamsFinding).where(CamsFinding.id.in_(find_ids)))).scalars().all()} if find_ids else {}
    plants = await plant_name_map(db, [o.siteId for o in obligations])

    links_by_obl: dict[str, list] = {}
    for l in links:
        links_by_obl.setdefault(l.obligationId, []).append(l)

    today = now()
    twelve_mo_ago = today - timedelta(days=365)
    rows = []
    verified_n = 0
    open_nc_total = 0
    status_counts: dict[str, int] = {}
    for o in obligations:
        status_counts[o.status] = status_counts.get(o.status, 0) + 1
        ol = links_by_obl.get(o.id, [])
        verified = False
        last_verify_code = None
        open_nc = 0
        link_out = []
        for l in ol:
            eng = engs.get(l.engagementId) if l.engagementId else None
            fnd = finds.get(l.findingId) if l.findingId else None
            link_out.append({
                "id": l.id, "engagementId": l.engagementId, "engagementCode": eng.engagementCode if eng else None,
                "findingId": l.findingId, "findingCode": fnd.findingCode if fnd else None,
                "obligationId": l.obligationId, "linkType": l.linkType, "notes": l.notes, "createdAt": l.createdAt,
            })
            if l.linkType == "VERIFIES" and eng and eng.conductedDate and _as_aware(eng.conductedDate) >= twelve_mo_ago:
                verified = True
                last_verify_code = eng.engagementCode
            if l.linkType in ("BREACHES", "EVIDENCES"):
                if fnd is None or _OPEN_FINDING(fnd.status):
                    open_nc += 1
        if verified:
            verified_n += 1
        open_nc_total += open_nc
        rows.append({
            "obligationId": o.id, "obligationCode": o.obligationCode, "title": o.title,
            "regulatorName": o.regulatorName, "siteId": o.siteId, "siteName": plants.get(o.siteId) if o.siteId else None,
            "status": o.status, "validUntil": o.validUntil,
            # F-49: rows read OVERDUE beside a FUTURE expiry date, which looked
            # like a bug and was not — the obligation is overdue for RENEWAL.
            # Surfacing the derived date is what makes the row make sense.
            "renewalDueAt": o.renewalDueAt, "renewalLeadDays": o.renewalLeadDays,
            "verifiedByAudit": verified, "lastVerifyingEngagementCode": last_verify_code,
            "openNcCount": open_nc, "links": link_out,
        })
    # Sort: open NC first, then unverified, then verified.
    rows.sort(key=lambda r: (0 if r["openNcCount"] else 1, 0 if not r["verifiedByAudit"] else 1, r["obligationCode"]))
    total = len(obligations)
    return {
        # Present on both the success and unavailable shapes so a consumer can
        # branch on ONE key rather than inferring failure from a zero.
        "available": True,
        "unavailableReason": None,
        "totalObligations": total,
        "verifiedByAuditCount": verified_n,
        # null, not 0, on an empty register — 0% over nothing is a meaningless
        # denominator and reads as total failure (the F-50 lesson).
        "verifiedPct": round((verified_n / total) * 100, 1) if total else None,
        "openNcCount": open_nc_total,
        "statusCounts": status_counts,
        "rows": rows,
    }


# ── ComplianceAudit → CAMS union adapters ──────────────────────────────────────
# Audits run on the ComplianceAudit engine but the centralized CAMS surfaces
# (command centre / calendar / findings / analytics) present a UNION of audits +
# inspections. These adapters project ComplianceAudit rows into the CAMS
# Engagement / Finding DTO shapes (with status + severity vocab translation) so
# the existing CAMS frontends render audits unchanged.

# ComplianceAudit lifecycle status -> CAMS engagement status vocabulary.
_AUDIT_STATUS_TO_CAMS = {
    "scheduled": "SCHEDULED",
    "in_progress": "IN_PROGRESS",
    "submitted_pending_response": "FINDINGS_REVIEW",
    "response_in_progress": "FINDINGS_REVIEW",
    "under_review": "FINDINGS_REVIEW",
    "closed": "CLOSED",
    "cancelled": "CANCELLED",
}

# Checkpoint criticality -> CAMS finding severity.
_AUDIT_CRIT_TO_SEV = {
    "critical": "CRITICAL_NC", "major": "MAJOR_NC", "minor": "MINOR_NC", "observation": "OBSERVATION",
}


def _audit_finding_status(workflow_state: str, capa: dict | None) -> str:
    """Checkpoint workflowState -> CAMS finding status vocabulary."""
    if workflow_state in ("RESOLVED", "FINALIZED"):
        return "CLOSED"
    if workflow_state == "ACCEPTED_WITH_CAPA" or (capa or {}).get("capa_id"):
        return "CAPA_RAISED"
    return "OPEN"  # AWAITING_AUDITEE / AUDITEE_RESPONDED / MORE_INFO_REQUESTED / ESCALATED_PM / OPEN


async def audit_engagements(db: AsyncSession) -> list[dict[str, Any]]:
    """ComplianceAudit rows as CAMS Engagement dicts (href → /cams/audits/{id})."""
    A = ComplianceAudit
    audits = (await db.execute(select(A))).scalars().all()
    if not audits:
        return []
    # Finding counts (fail/partial) + open counts per audit — one grouped query.
    R = AuditCheckpointResponse
    adverse = R.assessmentStatus.in_(["FAIL", "PARTIAL"])
    not_resolved = R.workflowState.notin_(["RESOLVED", "ACCEPTED_WITH_CAPA", "FINALIZED", "PASSED"])
    fc_rows = (
        await db.execute(
            select(
                R.auditId,
                func.count(R.id).filter(adverse).label("findings"),
                func.count(R.id).filter(and_(adverse, not_resolved)).label("open"),
            ).group_by(R.auditId)
        )
    ).all()
    fc = {r.auditId: (r.findings, r.open) for r in fc_rows}
    names = await user_name_map(db, [a.leadAuditorUserId for a in audits])
    plants = await plant_name_map(db, [a.plantId for a in audits])
    out = []
    for a in audits:
        findings_n, open_n = fc.get(a.id, (0, 0))
        out.append({
            "id": a.id, "engagementCode": a.auditNumber, "title": a.title,
            "engagementType": "COMPLIANCE_AUDIT", "auditTypeId": None, "auditTypeName": None,
            "standardRefs": [], "siteId": a.plantId, "siteName": plants.get(a.plantId),
            "areaOrAssetRef": None, "scopeStatement": a.scopeDescription or "",
            "leadAuditorId": a.leadAuditorUserId, "leadAuditorName": names.get(a.leadAuditorUserId),
            "auditTeamIds": [], "auditeeOwnerId": None, "auditeeOwnerName": None,
            "plannedDate": a.scheduledDate.isoformat() if a.scheduledDate else None,
            "scheduledStart": None, "scheduledEnd": None,
            "conductedDate": a.actualStartAt.isoformat() if a.actualStartAt else None,
            "templateId": a.templateId, "templateName": None, "templateVersionUsed": None,
            "status": _AUDIT_STATUS_TO_CAMS.get(a.status, "IN_PROGRESS"),
            "riskBasis": None, "triggeringRiskId": None,
            "overallResult": None, "scorePercent": a.overallCompliancePct,
            "nextScheduledDate": None, "sourceModule": "AUDIT",
            "findingCount": findings_n, "openFindingCount": open_n,
            "ncCount": findings_n, "updatedAt": a.updatedAt.isoformat() if a.updatedAt else None,
            # Provenance: the audit lives in the ComplianceAudit module.
            "href": f"/cams/audits/{a.id}",
        })
    return out


async def audit_findings(db: AsyncSession) -> list[dict[str, Any]]:
    """Audit-side findings for the unified register.

    **WP-19.** Reads first-class `AuditFinding` rows when they exist, and only
    falls back to projecting checkpoints for audits that predate the backfill.

    The difference is the whole point of promoting Finding: a projected
    checkpoint has no due date (`due = None` below), no owner distinct from the
    checkpoint's auditee, and `isRepeatFinding: False` hard-coded — which is why
    the unified register showed blank Due on ~40% of its rows *structurally* and
    could never chain a repeat across the two engines (F-3, F-40).
    """
    from app.models.cams_completion import AuditFinding as _AF

    promoted = (
        await db.execute(
            select(_AF, ComplianceAudit)
            .join(ComplianceAudit, _AF.auditId == ComplianceAudit.id)
            .where(_AF.isDeleted.is_(False), ComplianceAudit.isDeleted.is_(False))
            .order_by(ComplianceAudit.scheduledDate.desc(), _AF.findingCode)
        )
    ).all()

    out: list[dict[str, Any]] = []
    covered_audits: set[str] = set()
    # Both clocks are bound ONCE, at function scope, and they are different
    # types on purpose:
    #   `today`  — a date, because `AuditFinding.dueDate` is a DATE column and
    #              `date < date` is the only comparison that is correct there.
    #   `now_ts` — an aware datetime, for age arithmetic against the aware
    #              `createdAt` timestamps that `_as_aware()` returns.
    # Previously only `today` existed, it was a date, and it was bound INSIDE
    # `if promoted:` — which broke the legacy branch below two separate ways:
    # `date - datetime` raised TypeError whenever promoted findings existed, and
    # `today` was unbound (NameError) whenever they did not. Either one 500s the
    # whole Command Centre, because the page fetches this endpoint on load.
    now_ts = now()
    today = now_ts.date()
    if promoted:
        p_plants = await plant_name_map(db, [a.plantId for _, a in promoted])
        p_names = await user_name_map(db, [f.ownerId for f, _ in promoted])
        for f, a in promoted:
            covered_audits.add(a.id)
            out.append({
                "id": f.id, "findingCode": f.findingCode, "engagementId": a.id,
                "engagementCode": a.auditNumber, "engagementTitle": a.title,
                "sourceQuestionId": f.checkpointCode, "title": f.title,
                "description": f.description or f.title, "severity": f.severity,
                "standardClauseRef": f.clauseRef or f.standard,
                "siteId": f.siteId, "siteName": p_plants.get(f.siteId),
                "areaOrAssetRef": None,
                "ownerId": f.ownerId, "ownerName": p_names.get(f.ownerId),
                "rootCauseMethod": None, "rootCauseSummary": None,
                "capaId": f.capaId, "capaNumber": None, "capaState": None,
                "status": f.status,
                # The three fields a projected checkpoint could never carry.
                "isRepeatFinding": f.isRepeatFinding,
                "repeatOfFindingId": f.repeatOfFindingId,
                "dueDate": f.dueDate.isoformat() if f.dueDate else None,
                "isOverdue": bool(
                    f.dueDate and f.status not in ("CLOSED", "ACCEPTED_RISK")
                    and f.dueDate < today
                ),
                "observationOnly": f.observationOnly,
                "closedBy": f.closedById, "closedAt": _iso(f.closedAt),
                "createdAt": _iso(f.createdAt), "source": "AUDIT",
                "href": f"/cams/audits/{a.id}",
            })

    # Legacy projection, ONLY for audits with no promoted findings yet.
    R = AuditCheckpointResponse
    rows = (
        await db.execute(
            select(R, ComplianceAudit)
            .join(ComplianceAudit, R.auditId == ComplianceAudit.id)
            .where(
                R.assessmentStatus.in_(["FAIL", "PARTIAL"]),
                ComplianceAudit.isDeleted.is_(False),
                # Skip audits already represented by promoted rows, or the
                # register would show each finding twice.
                ComplianceAudit.id.notin_(covered_audits) if covered_audits else True,
            )
            .order_by(ComplianceAudit.scheduledDate.desc(), R.sequence)
        )
    ).all()
    if not rows:
        return out
    plants = await plant_name_map(db, [a.plantId for _, a in rows])
    names = await user_name_map(db, [r.assignedOwnerId or r.routedToUserId for r, _ in rows])
    for r, a in rows:
        capa = r.capa or {}
        due = None  # audit checkpoints carry CAPA due in the capa subdoc
        owner = r.assignedOwnerId or r.routedToUserId
        out.append({
            "id": r.id, "findingCode": r.checkpointCode, "engagementId": a.id,
            "engagementCode": a.auditNumber, "engagementTitle": a.title,
            "sourceQuestionId": None, "title": r.checkpointQuestion[:200],
            "description": r.observation or r.checkpointQuestion, "severity": _AUDIT_CRIT_TO_SEV.get(r.criticality, "MINOR_NC"),
            "standardClauseRef": r.standard or None, "siteId": a.plantId, "siteName": plants.get(a.plantId),
            "areaOrAssetRef": None, "ownerId": owner, "ownerName": names.get(owner),
            "rootCauseMethod": None, "rootCauseSummary": None,
            "capaId": capa.get("capa_id"), "capaNumber": capa.get("capa_number"), "capaState": capa.get("capa_status"),
            "status": _audit_finding_status(r.workflowState, capa),
            "isRepeatFinding": False, "repeatOfFindingId": None, "dueDate": due,
            "closedBy": None, "closedAt": _iso(r.finalizedAt) if hasattr(r, "finalizedAt") else None,
            "verificationNote": None, "evidenceAttachmentIds": r.auditorEvidenceIds or [],
            "ageDays": (now_ts - _as_aware(r.createdAt)).days if r.createdAt else 0,
            "capaRequired": r.criticality in ("critical", "major"),
            "createdAt": _iso(r.createdAt), "updatedAt": _iso(r.updatedAt),
            "href": f"/cams/audits/{a.id}",
        })
    return out


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None
