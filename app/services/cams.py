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

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy.orm import selectinload

from app.models.capa import Capa, CapaSourceCategory, CapaSourceType
from app.models.cams import (
    CamsAnalyticsSnapshot,
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
from app.services import cams_providers as providers


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


# ── consumer-integration engine entry point (§8) ─────────────────────────────
async def create_consumer_engagement(
    db: AsyncSession,
    *,
    source_module: str,
    title: str,
    engagement_type: str = "INSPECTION",
    lead_auditor_id: str,
    planned_date: datetime | None = None,
    site_id: str | None = None,
    audit_type_id: str | None = None,
    template_id: str | None = None,
    standard_refs: list[str] | None = None,
    area_or_asset_ref: str | None = None,
    scope_statement: str = "",
    actor_id: str | None = None,
) -> CamsEngagement:
    """Shared AuditEngineService entry point (§8). A consumer module (Fire / PPE /
    Pharma / EPC) calls this to raise a CAMS engagement carrying its
    `source_module` provenance — IDENTICAL machinery to a CAMS-native engagement.
    This is what makes "one engine" real: consumers don't keep their own audit
    logic, they call here. Does NOT commit — the caller owns the transaction."""
    when = planned_date or now()
    e = CamsEngagement(
        engagementCode=await next_engagement_code(db, engagement_type),
        title=title[:200],
        engagementType=engagement_type,
        auditTypeId=audit_type_id,
        standardRefs=standard_refs or [],
        siteId=site_id,
        areaOrAssetRef=area_or_asset_ref,
        scopeStatement=scope_statement or f"{title} (raised by {source_module}).",
        leadAuditorId=lead_auditor_id,
        auditTeamIds=[],
        plannedDate=when,
        scheduledStart=when,
        templateId=template_id,
        status="SCHEDULED",
        riskBasis="ROUTINE",
        sourceModule=source_module,
        createdBy=actor_id,
    )
    db.add(e)
    await db.flush()
    return e


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


# ── repeat-finding detection (Analytics engine §5.2.3) ────────────────────────
# A recurrence within this many days flags the later finding as a repeat. The
# window is data-driven (override per call) so the certification-readiness signal
# can be tuned without code changes.
REPEAT_WINDOW_DAYS = 365


def _repeat_key(f: CamsFinding) -> tuple[str, str | None, str | None] | None:
    """Recurrence group key: same standard clause + site + area/asset. Findings
    with no clause ref can't be matched by clause and are excluded."""
    if not f.standardClauseRef:
        return None
    return (f.standardClauseRef, f.siteId, f.areaOrAssetRef)


async def detect_repeat_findings(db: AsyncSession, *, window_days: int | None = None) -> dict[str, int]:
    """Flag a finding `isRepeatFinding` when an earlier finding sharing its
    (clause, site, area/asset) was raised within the window; `repeatOfFindingId`
    points at the nearest prior occurrence.

    This is the Analytics engine OWNING the field (§4 / §5.2.3) — it is computed,
    not user-set. Recomputes from scratch each run (clears stale flags), so it is
    idempotent and safe to call after any finding write or as a backfill. Does
    NOT commit — the caller owns the transaction boundary.
    """
    window = window_days if (window_days and window_days > 0) else REPEAT_WINDOW_DAYS
    findings = (
        await db.execute(select(CamsFinding).where(CamsFinding.isDeleted.is_(False)))
    ).scalars().all()

    # A finding's "occurrence" is WHEN the audit happened (engagement
    # conducted/planned date), not when the row was written — recurrence is a
    # temporal property of the audit programme. Fall back to the finding's own
    # createdAt only if the engagement has no dates.
    eng_ids = {f.engagementId for f in findings if f.engagementId}
    eng_date: dict[str, datetime | None] = {}
    if eng_ids:
        engs = (await db.execute(select(CamsEngagement).where(CamsEngagement.id.in_(eng_ids)))).scalars().all()
        for e in engs:
            eng_date[e.id] = _as_aware(e.conductedDate) or _as_aware(e.plannedDate)

    def occurred(f: CamsFinding) -> datetime:
        return eng_date.get(f.engagementId) or _as_aware(f.createdAt) or now()

    groups: dict[tuple, list[CamsFinding]] = {}
    for f in findings:
        key = _repeat_key(f)
        if key is None:
            # not clause-mapped → can never be a clause-recurrence; clear stale flag
            if f.isRepeatFinding or f.repeatOfFindingId:
                f.isRepeatFinding = False
                f.repeatOfFindingId = None
            continue
        groups.setdefault(key, []).append(f)

    flagged = 0
    for group in groups.values():
        # chronological by occurrence; findingCode breaks ties deterministically
        group.sort(key=lambda x: (occurred(x), x.findingCode))
        for i, f in enumerate(group):
            if i == 0:
                is_repeat, repeat_of = False, None
            else:
                prev = group[i - 1]
                gap = occurred(f) - occurred(prev)
                is_repeat = gap <= timedelta(days=window)
                repeat_of = prev.id if is_repeat else None
            if f.isRepeatFinding != is_repeat or f.repeatOfFindingId != repeat_of:
                f.isRepeatFinding = is_repeat
                f.repeatOfFindingId = repeat_of
            if is_repeat:
                flagged += 1
    await db.flush()
    return {"scanned": len(findings), "flagged": flagged, "windowDays": window}


# ── Analytics & Benchmarking (C-13) ───────────────────────────────────────────
async def compute_analytics(
    db: AsyncSession,
    *,
    site_id: str | None = None,
    engagement_type: str | None = None,
    standard_ref: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    """Compute the analytics payload. With no scope args this is the full-tenant
    view; passing site/type/standard/date narrows the population (enables QoQ
    period comparison and per-scope benchmarking, §5.2.5). Findings are limited
    to the scoped engagements so every metric stays internally consistent."""
    today = now()
    eng_stmt = select(CamsEngagement).where(CamsEngagement.isDeleted.is_(False))
    if site_id:
        eng_stmt = eng_stmt.where(CamsEngagement.siteId == site_id)
    if engagement_type:
        eng_stmt = eng_stmt.where(CamsEngagement.engagementType == engagement_type)
    if date_from:
        eng_stmt = eng_stmt.where(CamsEngagement.plannedDate >= date_from)
    if date_to:
        eng_stmt = eng_stmt.where(CamsEngagement.plannedDate <= date_to)
    engagements = (await db.execute(eng_stmt)).scalars().all()
    if standard_ref:
        # standardRefs is a JSON list → filter in Python for cross-dialect portability
        engagements = [e for e in engagements if standard_ref in (e.standardRefs or [])]

    eng_ids = {e.id for e in engagements}
    findings = (
        (await db.execute(
            select(CamsFinding).where(CamsFinding.engagementId.in_(eng_ids)).where(CamsFinding.isDeleted.is_(False))
        )).scalars().all()
        if eng_ids else []
    )

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

    # CAPA overdue % (AUDIT source). When scoped, restrict to CAPAs raised from
    # the scoped findings so the metric matches the rest of the payload.
    audit_codes = ("AUDIT_INTERNAL", "AUDIT_EXTERNAL", "AUDIT_REGULATORY")
    capa_stmt = select(Capa).where(Capa.sourceTypeCode.in_(audit_codes))
    if any([site_id, engagement_type, standard_ref, date_from, date_to]):
        finding_ids = {f.id for f in findings}
        capa_stmt = capa_stmt.where(Capa.sourceReferenceId.in_(finding_ids) if finding_ids else Capa.id.is_(None))
    capas = (await db.execute(capa_stmt)).scalars().all()
    capa_open = [c for c in capas if c.state not in ("CLOSED", "CLOSED_RECURRED", "CANCELLED", "REJECTED")]
    capa_overdue = sum(
        1 for c in capa_open
        if c.closureTargetDate and _as_aware(c.closureTargetDate) < today
    )
    capa_overdue_pct = round((capa_overdue / len(capa_open)) * 100, 1) if capa_open else 0

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


async def precompute_snapshot(
    db: AsyncSession,
    *,
    period_label: str,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    site_id: str | None = None,
    engagement_type: str | None = None,
    standard_ref: str | None = None,
    actor_id: str | None = None,
) -> CamsAnalyticsSnapshot:
    """Compute and persist a point-in-time analytics snapshot for a (period, scope).

    Idempotent per (periodLabel + scope): re-running updates the existing row
    rather than duplicating, so a nightly job or a re-seed converges. `metrics`
    holds the full analytics payload; `snapshotHash` is its sha256 for board-pack
    integrity (§12). Does NOT commit — the caller owns the transaction."""
    metrics = await compute_analytics(
        db, site_id=site_id, engagement_type=engagement_type, standard_ref=standard_ref,
        date_from=period_start, date_to=period_end,
    )
    digest = hashlib.sha256(json.dumps(metrics, sort_keys=True, default=str).encode()).hexdigest()

    rows = (
        await db.execute(
            select(CamsAnalyticsSnapshot)
            .where(CamsAnalyticsSnapshot.periodLabel == period_label)
            .where(CamsAnalyticsSnapshot.isDeleted.is_(False))
        )
    ).scalars().all()
    snap = next(
        (s for s in rows
         if s.scopeSiteId == site_id and s.scopeEngagementType == engagement_type and s.scopeStandardRef == standard_ref),
        None,
    )
    if snap is None:
        snap = CamsAnalyticsSnapshot(periodLabel=period_label, createdBy=actor_id)
        db.add(snap)
    snap.periodStart = period_start
    snap.periodEnd = period_end
    snap.scopeSiteId = site_id
    snap.scopeEngagementType = engagement_type
    snap.scopeStandardRef = standard_ref
    snap.metrics = metrics
    snap.snapshotHash = digest
    snap.generatedAt = now()
    snap.updatedBy = actor_id
    await db.flush()
    return snap


# ── Compliance Tracker (C-12) ──────────────────────────────────────────────────
async def compute_compliance(db: AsyncSession) -> dict[str, Any]:
    """Surface the obligations register + audit-coverage. Reads through the
    obligations provider (§5.1): the ERM Phase-2 register in integrated mode, the
    CAMS-owned bundled register in standalone mode — identical behaviour either
    way. Degrades to an empty register only when neither has data."""
    obligations = await providers.list_obligations(db)
    links = (
        await db.execute(select(CamsComplianceLink).where(CamsComplianceLink.isDeleted.is_(False)))
    ).scalars().all()

    eng_ids = {l.engagementId for l in links if l.engagementId}
    find_ids = {l.findingId for l in links if l.findingId}
    engs = {e.id: e for e in (await db.execute(select(CamsEngagement).where(CamsEngagement.id.in_(eng_ids)))).scalars().all()} if eng_ids else {}
    finds = {f.id: f for f in (await db.execute(select(CamsFinding).where(CamsFinding.id.in_(find_ids)))).scalars().all()} if find_ids else {}
    plants = await plant_name_map(db, [o["siteId"] for o in obligations])

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
        status_counts[o["status"]] = status_counts.get(o["status"], 0) + 1
        ol = links_by_obl.get(o["id"], [])
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
            "obligationId": o["id"], "obligationCode": o["obligationCode"], "title": o["title"],
            "regulatorName": o["regulatorName"], "siteId": o["siteId"], "siteName": plants.get(o["siteId"]) if o["siteId"] else None,
            "status": o["status"], "validUntil": o["validUntil"],
            "verifiedByAudit": verified, "lastVerifyingEngagementCode": last_verify_code,
            "openNcCount": open_nc, "links": link_out,
        })
    # Sort: open NC first, then unverified, then verified.
    rows.sort(key=lambda r: (0 if r["openNcCount"] else 1, 0 if not r["verifiedByAudit"] else 1, r["obligationCode"]))
    total = len(obligations)
    return {
        "totalObligations": total,
        "verifiedByAuditCount": verified_n,
        "verifiedPct": round((verified_n / total) * 100, 1) if total else 0,
        "openNcCount": open_nc_total,
        "statusCounts": status_counts,
        "rows": rows,
        "obligationsSource": providers.obligations_source(),
    }


# ── Audit Programme — coverage matrix (C-03) ──────────────────────────────────
_PROG_DONE = ("FIELDWORK_COMPLETE", "FINDINGS_REVIEW", "REPORT_ISSUED", "CLOSED")
_PROG_PLANNED = ("PLANNED", "SCHEDULED", "IN_PROGRESS")


async def compute_programme(db: AsyncSession, *, date_from: datetime | None = None, date_to: datetime | None = None) -> dict[str, Any]:
    """Annual/risk-based programme coverage matrix (sites × audit types, each
    carrying its standards): where audits are done / planned / missing, with gap
    flags for un-audited scope (§6 C-03). Site universe = the sites CAMS actually
    runs a programme for (tenant-safe: derived from the engagement population)."""
    eng_stmt = select(CamsEngagement).where(CamsEngagement.isDeleted.is_(False))
    if date_from:
        eng_stmt = eng_stmt.where(CamsEngagement.plannedDate >= date_from)
    if date_to:
        eng_stmt = eng_stmt.where(CamsEngagement.plannedDate <= date_to)
    engagements = (await db.execute(eng_stmt)).scalars().all()

    types = (
        await db.execute(
            select(CamsAuditType).where(CamsAuditType.isDeleted.is_(False)).where(CamsAuditType.isActive.is_(True))
        )
    ).scalars().all()
    site_ids = sorted({e.siteId for e in engagements if e.siteId})
    plants = await plant_name_map(db, site_ids)

    # index engagements by (auditTypeId, siteId)
    by_cell: dict[tuple[str | None, str | None], list[CamsEngagement]] = {}
    for e in engagements:
        by_cell.setdefault((e.auditTypeId, e.siteId), []).append(e)

    matrix: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    covered = 0
    cell_total = 0
    for t in types:
        for sid in site_ids:
            cell_total += 1
            cell = by_cell.get((t.id, sid), [])
            done = sum(1 for e in cell if e.status in _PROG_DONE)
            planned = sum(1 for e in cell if e.status in _PROG_PLANNED)
            last_done = max(
                (_as_aware(e.conductedDate) or _as_aware(e.plannedDate) for e in cell if e.status in _PROG_DONE),
                default=None,
            )
            status = "DONE" if done else ("PLANNED" if planned else "GAP")
            if status != "GAP":
                covered += 1
            row = {
                "auditTypeId": t.id, "auditTypeName": t.name, "standardRefs": t.standardRefs or [],
                "siteId": sid, "siteName": plants.get(sid),
                "done": done, "planned": planned, "total": len(cell), "status": status,
                "lastConductedDate": last_done,
            }
            matrix.append(row)
            if status == "GAP":
                gaps.append({"auditTypeName": t.name, "siteId": sid, "siteName": plants.get(sid), "standardRefs": t.standardRefs or []})

    standards = sorted({s for t in types for s in (t.standardRefs or [])})
    return {
        "sites": [{"siteId": sid, "siteName": plants.get(sid)} for sid in site_ids],
        "auditTypes": [{"auditTypeId": t.id, "name": t.name, "standardRefs": t.standardRefs or []} for t in types],
        "standards": standards,
        "matrix": matrix,
        "gaps": gaps,
        "cellCount": cell_total,
        "coveredCount": covered,
        "coveragePct": round((covered / cell_total) * 100, 1) if cell_total else 0,
    }


# ── Board / Management-Review pack (C-15) ─────────────────────────────────────
async def compute_board_pack(
    db: AsyncSession, *, period_label: str | None = None, date_from: datetime | None = None, date_to: datetime | None = None
) -> dict[str, Any]:
    """Assemble the management-review / certification-readiness pack (§6 C-15,
    §15): programme completion, findings profile, repeat-finding rate, clause
    conformance, CAPA status, compliance assurance, benchmarking — one payload.
    `snapshotHash` stamps it for integrity logging (§12)."""
    a = await compute_analytics(db, date_from=date_from, date_to=date_to)
    c = await compute_compliance(db)
    prog = await compute_programme(db, date_from=date_from, date_to=date_to)
    pack = {
        "periodLabel": period_label or "Current",
        "programme": a["programme"],
        "programmeCoveragePct": prog["coveragePct"],
        "programmeGaps": prog["gaps"],
        "findingsBySeverity": a["findingsBySeverity"],
        "repeatFindingRatePct": a["repeatFindingRatePct"],
        "openFindingCount": a["openFindingCount"],
        "avgClosureDays": a["avgClosureDays"],
        "clauseConformance": a["clauseConformance"],
        "paretoByClause": a["paretoByClause"],
        "benchmarkingBySite": a["benchmarkingBySite"],
        "bySourceModule": a["bySourceModule"],
        "capaOverduePct": a["capaOverduePct"],
        "compliance": {
            "totalObligations": c["totalObligations"],
            "verifiedByAuditCount": c["verifiedByAuditCount"],
            "verifiedPct": c["verifiedPct"],
            "openNcCount": c["openNcCount"],
            "obligationsSource": c.get("obligationsSource"),
        },
    }
    pack["snapshotHash"] = hashlib.sha256(json.dumps(pack, sort_keys=True, default=str).encode()).hexdigest()
    return pack
