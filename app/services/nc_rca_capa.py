"""PIL/MR/F04-R1 — Internal Audit NC Report: RCA + CAPA for every non-conformity.

Page Industries close a management-system audit (QMS / EMS / OHSMS / EnMS) by
issuing one numbered **Non Conformance Report** per non-conformity. Revision R1
of that form records its own reason for existing:

    "Preventive action is replaced with Root Cause Analysis in NC Report format."

So an NC no longer goes straight to a fix. It goes: root cause first, then
Correction, then Preventive Action, then verification of effective closure —
and the form is split by colour into an auditor half and an auditee half.

**What this module does NOT do is store the analysis.** The Why-Why ladder lives
in `RootCauseAnalysis.analysisPayload` (methodology FIVE_WHY — its
`problemStatement` / `whys[]` / `rootCause` shape is exactly the form's), and
the Correction and Preventive Action rows live in `CapaAction`. This module is
the *origination and gating* layer between them: it issues the NCR numbers,
creates the two governed records per NC, and holds the rule that no action may
be planned until the root cause is approved. A second copy of an analysis is a
second thing that can disagree with the first.

The form, mapped:

    Form field (row)                     Where it lives
    ─────────────────────────────────────────────────────────────────────
    Audit Number (4)                     ComplianceAudit.auditNumber
    Department (4)                       AuditCheckpointResponse.categoryName
    QMS/EMS/OHSMS/EnMS (5)               .streamCode + .standardClauses
    NCR Number (6)                       AuditFinding.ncrNumber      ← issued here
    Clause No (6)                        .standardClauses / .requirementReference
    Requirements (7)                     .checkpointQuestion
    Observed Nonconformity (10)          .observation
    Evidence (12)                        .auditorEvidenceIds
    Grade (14)                           .gradeAwarded
    To be completed before (15)          AuditFinding.dueDate
    ── auditee half ─────────────────────────────────────────────────────
    Root Cause Analysis + Whys (16-17)   RootCauseAnalysis.analysisPayload
    Correction (18-21)                   CapaAction(IMMEDIATE_CONTAINMENT)
    Preventive Action (22-25)            CapaAction(PREVENTIVE)
    ── auditor half ─────────────────────────────────────────────────────
    Verification of effectiveness (26)   Capa.verificationResult/-Evidence
    Auditor Signature, Closed On (30)    AuditFinding.auditorSignedBy/-At
    M.R. Signature (30)                  AuditFinding.mrSignedBy/-At
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.capa import Capa, CapaAction, CapaRootCause, CapaSlaProfile
from app.models.cams_completion import AuditFinding
from app.models.rca import RootCauseAnalysis
from app.services.audit_findings import sync_finding_for_checkpoint
from app.services.capa_spawn import spawn_capa
from app.services.rca import generate_rca_summary

# The form prescribes ONE methodology. It is not an auditee's choice, which is
# why nothing here takes a `methodology` argument: PIL/MR/F04-R1 row 17 is a
# cascading Why ladder and an auditee who returns a fishbone has not filled in
# the form. FIVE_WHY is the canonical code for the technique in
# `services.rca._NORMALISE`; the level count below is the actual policy.
PIL_FORM_NO = "PIL/MR/F04-R1"
PIL_METHODOLOGY = "FIVE_WHY"

# The worked example printed on the form runs SIX levels:
#
#   Why process for achieving the set objectives was not effective?
#   → Why review mechanism is not effective?
#     → Why are objectives not updated?
#       → Why are Management review meeting not conducted monthly once?
#         → Why MRM due dates not monitored?
#           → Why HOD's not aware about review frequency?
#
# Five is the floor, not the target, and the ladder is uncapped: the technique
# stops when the answer becomes systemic, not when a row runs out. A fixed
# six-row form would teach auditees to pad to six and stop there, which is the
# failure mode the revision was written to remove.
PIL_MIN_WHY_LEVELS = 5
PIL_SEED_WHY_LEVELS = 5

# The form's own words, rendered above the ladder. Kept here rather than in the
# client so the API, the PDF and the screen cannot drift from each other.
PIL_RCA_PROMPT = "What failed in the system to allow this nonconformity to occur"
PIL_CORRECTION_PROMPT = "What is done to solve this problem"
PIL_PREVENTIVE_PROMPT = "What is done to prevent reoccurrence"

# PIL's vocabulary → the CapaAction enum. The form has no "Corrective Action"
# box, and that is not an omission: ISO 9000 "correction" is action to eliminate
# the detected nonconformity (contain/fix it), which is `IMMEDIATE_CONTAINMENT`;
# what PIL call "Preventive Action" is action to stop it recurring. Screens
# label these with PIL's words, so the auditee sees the form they know while the
# register aggregates on the platform-wide enum.
ACTION_TYPE_FOR_CORRECTION = "IMMEDIATE_CONTAINMENT"
ACTION_TYPE_FOR_PREVENTIVE = "PREVENTIVE"

# An IMS non-conformity is a management-system conformity failure, so its RCA is
# COMPLIANCE-domain rather than the OPERATIONAL default `rca_core` gives every
# EVENT-origin RCA. The cross-domain analytics roll up by domain; filing a
# clause 9.3 NC alongside a shop-floor injury would make both unreadable.
PIL_RCA_DOMAIN = "COMPLIANCE"

# Severity → how long the auditee has for the ROOT CAUSE, when no CapaSlaProfile
# is seeded. The NC's own `dueDate` ("To be completed before", set by the
# auditor on the form) governs total closure; this is the internal milestone
# that keeps an analysis from being written the night before the due date.
_FALLBACK_RCA_DAYS = {"CRITICAL_NC": 3, "MAJOR_NC": 7, "MINOR_NC": 14}

# Statuses at which an NC is still live work.
OPEN_NC_STATUSES = ("OPEN", "CAPA_RAISED", "IN_REMEDIATION", "VERIFICATION")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────
# Selecting the non-conformities
# ─────────────────────────────────────────────────────────────────────
async def ensure_findings_exist(
    db: AsyncSession, audit: ComplianceAudit, *, actor_id: str | None = None
) -> int:
    """Materialise the `AuditFinding` row for every FAIL checkpoint that lacks one.

    `audit_findings.sync_finding_for_checkpoint` is written to be called as
    checkpoints are assessed, but nothing in the request path calls it — today
    the only producer of `AuditFinding` rows is `scripts/backfill_audit_findings.py`,
    run by hand. On a freshly submitted audit there are therefore NO finding rows,
    and a trigger that assumed otherwise would report "0 non-conformities" on an
    audit holding twelve of them: the worst possible failure here, because it
    looks exactly like success.

    So the trigger materialises what it needs. Idempotent (the sync helper skips
    a checkpoint that already has a live finding), and it deliberately does not
    touch PARTIAL rows — this call exists to serve the NC trigger, not to
    backfill the observation half of the register.

    Returns how many rows it had to create.
    """
    responses = (
        await db.execute(
            select(AuditCheckpointResponse).where(
                AuditCheckpointResponse.auditId == audit.id,
                AuditCheckpointResponse.assessmentStatus == "FAIL",
            )
        )
    ).scalars().all()
    if not responses:
        return 0
    have = set(
        (
            await db.execute(
                select(AuditFinding.checkpointResponseId).where(
                    AuditFinding.auditId == audit.id,
                    AuditFinding.isDeleted.is_(False),
                    AuditFinding.checkpointResponseId.is_not(None),
                )
            )
        ).scalars().all()
    )
    created = 0
    for response in responses:
        if response.id in have:
            continue
        made = await sync_finding_for_checkpoint(
            db, audit=audit, response=response, actor_id=actor_id
        )
        if made is not None:
            created += 1
    if created:
        await db.flush()
    return created


async def non_conformities_for_audit(
    db: AsyncSession, audit_id: str
) -> list[tuple[AuditFinding, AuditCheckpointResponse]]:
    """The findings that are NON-CONFORMITIES, paired with their checkpoint.

    Selection is on the CHECKPOINT's `assessmentStatus == FAIL`, not on the
    finding's `observationOnly`. On a tristate IMS audit those two answer
    different questions and disagree constantly:

      * `observationOnly` is derived from the checkpoint's inherent
        *criticality* (`audit_findings.severity_for`) — a property of the
        question in the library.
      * Non-Conformance vs Observation is the auditor's *verdict* — Observation
        grades to PARTIAL, Non-Conformance to FAIL (docs/cams/20).

    A checklist line whose criticality is "observation" but which the auditor
    marked Non-Conformance is a real NC and must get an NC report. Filtering on
    `observationOnly` would silently drop it, which is the class of bug that put
    375 observations on the floor at the CamsFinding boundary.
    """
    rows = (
        await db.execute(
            select(AuditFinding, AuditCheckpointResponse)
            .join(
                AuditCheckpointResponse,
                AuditFinding.checkpointResponseId == AuditCheckpointResponse.id,
            )
            .where(
                AuditFinding.auditId == audit_id,
                AuditFinding.isDeleted.is_(False),
                AuditCheckpointResponse.assessmentStatus == "FAIL",
            )
            .order_by(AuditCheckpointResponse.sequence, AuditFinding.findingCode)
        )
    ).all()
    return [(f, r) for f, r in rows]


# ─────────────────────────────────────────────────────────────────────
# Number allocation
# ─────────────────────────────────────────────────────────────────────
async def _highest_ncr_number(db: AsyncSession, audit_id: str) -> int:
    """Highest NCR number issued on this audit, as an int. 0 when none.

    MAX over the parsed suffix, never COUNT(*) — the count pattern re-issues a
    live number after any soft-delete, which this repo has already been bitten
    by twice (`next_capa_number`, `next_finding_code`). A unique index on
    (auditId, ncrNumber) is the second line of defence; see the DDL script.
    """
    codes = (
        await db.execute(
            select(AuditFinding.ncrNumber).where(
                AuditFinding.auditId == audit_id,
                AuditFinding.ncrNumber.is_not(None),
            )
        )
    ).scalars().all()
    highest = 0
    for c in codes:
        digits = re.sub(r"\D", "", c or "")
        if digits:
            highest = max(highest, int(digits))
    return highest


async def _allocate_rca_codes(db: AsyncSession, count: int) -> list[str]:
    """`count` consecutive unused RCA codes for the current year.

    `rca_core.next_rca_code` derives its number from COUNT(*)+1, which is
    correct only while codes are contiguous from 1 and which cannot allocate a
    BLOCK at all — calling it twelve times inside one uncommitted transaction
    would hand back twelve identical codes and fail on the unique constraint.
    Bulk triggering is the whole point of this module, so it allocates from
    MAX+1 instead. `next_rca_code` is left alone: its other callers create one
    RCA at a time and changing a shared allocator under them is a separate,
    riskier change than this feature needs.
    """
    year = _now().year
    prefix = f"RCA-{year}-"
    existing = (
        await db.execute(
            select(RootCauseAnalysis.rcaCode)
            .where(RootCauseAnalysis.rcaCode.like(f"{prefix}%"))
            .execution_options(include_deleted=True)
        )
    ).scalars().all()
    highest = 0
    for code in existing:
        tail = code.rsplit("-", 1)[-1]
        if tail.isdigit():
            highest = max(highest, int(tail))
    return [f"{prefix}{highest + i:04d}" for i in range(1, count + 1)]


# ─────────────────────────────────────────────────────────────────────
# The Why-Why payload
# ─────────────────────────────────────────────────────────────────────
def seed_why_payload(
    *, nonconformity: str, requirement: str | None, clause: str | None,
    department: str | None, stream: str | None,
) -> dict[str, Any]:
    """A blank PIL ladder, plus a suggested opening Why.

    `suggestedFirstWhy` is derived from the failed REQUIREMENT, not from the
    observation, because the form's own worked example does: "Why process for
    achieving the set objectives was not effective?" Starting an auditee at
    "Why did the observation happen?" reliably produces a first Why about the
    symptom, and the ladder never reaches the system.

    It sits in `pilNcReport` rather than in `whys[0].question` deliberately.
    `services.rca.is_empty_rca_data` treats a why-row as filled if it carries a
    QUESTION or an answer, so seeding the ladder itself would make a blank form
    report as having analysis in it — to that helper and to anything later built
    on it. The ladder stays genuinely empty until the auditee writes in it; the
    client pre-fills the first question from here.

    Everything else is `services.rca`'s FIVE_WHY contract verbatim, so
    `generate_rca_summary` and `is_empty_rca_data` work on these unchanged.
    """
    subject = (requirement or nonconformity or "").strip().rstrip("?.")
    suggested = (
        f"Why was {subject[0].lower() + subject[1:]} not effective?"
        if subject else "Why did this nonconformity occur?"
    )
    return {
        "problemStatement": (nonconformity or "").strip(),
        "whys": [{"question": "", "answer": ""} for _ in range(PIL_SEED_WHY_LEVELS)],
        "rootCause": "",
        # Form context, so the screen and the PDF can render PIL/MR/F04-R1
        # itself rather than a generic 5-Why widget. Read-only for the auditee.
        "pilNcReport": {
            "formNo": PIL_FORM_NO,
            "prompt": PIL_RCA_PROMPT,
            "minLevels": PIL_MIN_WHY_LEVELS,
            "suggestedFirstWhy": suggested,
            "clause": clause,
            "department": department,
            "stream": stream,
        },
    }


def validate_why_payload(payload: Any) -> list[str]:
    """Everything wrong with an auditee's ladder, as messages. Empty = valid.

    Returns a LIST rather than raising on the first problem: an auditee who has
    left three things blank should be told all three, not sent round the loop
    three times.
    """
    problems: list[str] = []
    if not isinstance(payload, dict):
        return ["The root cause analysis is empty."]

    if not (payload.get("problemStatement") or "").strip():
        problems.append("State the nonconformity being analysed (Observed Nonconformity).")

    whys = payload.get("whys") or []
    answered = [
        w for w in whys
        if isinstance(w, dict) and (w.get("answer") or "").strip()
    ]
    if len(answered) < PIL_MIN_WHY_LEVELS:
        problems.append(
            f"{PIL_FORM_NO} requires at least {PIL_MIN_WHY_LEVELS} levels of Why "
            f"— {len(answered)} answered. Keep asking Why of the previous answer "
            f"until the answer names something in the system."
        )
    # A ladder with a gap is not a ladder: level 4 answering level 2 breaks the
    # chain the technique depends on, and reads as complete on a count.
    for i, w in enumerate(whys):
        if not isinstance(w, dict):
            continue
        has_answer = bool((w.get("answer") or "").strip())
        later_answered = any(
            isinstance(x, dict) and (x.get("answer") or "").strip() for x in whys[i + 1:]
        )
        if not has_answer and later_answered:
            problems.append(f"Why {i + 1} has no answer but a later Why does — the chain is broken.")
            break

    if not (payload.get("rootCause") or "").strip():
        problems.append(
            f"Name the root cause. The form asks: {PIL_RCA_PROMPT.lower()}."
        )
    return problems


# ─────────────────────────────────────────────────────────────────────
# The bulk trigger
# ─────────────────────────────────────────────────────────────────────
async def _rca_due_date(
    db: AsyncSession, *, severity: str, closure_target: date | None
) -> datetime:
    """When the ROOT CAUSE is due. Never later than the NC's own closure date."""
    profile = (
        await db.execute(
            select(CapaSlaProfile).where(
                CapaSlaProfile.sourceTypeCode == "AUDIT_INTERNAL",
                CapaSlaProfile.isActive.is_(True),
            )
        )
    ).scalars().first()
    days = profile.rcaDueDays if profile else _FALLBACK_RCA_DAYS.get(severity, 7)
    due = _now() + timedelta(days=days)
    if closure_target is not None:
        cap = datetime.combine(closure_target, datetime.min.time(), tzinfo=timezone.utc)
        due = min(due, cap)
    return due


async def trigger_for_audit(
    db: AsyncSession,
    audit: ComplianceAudit,
    *,
    actor_id: str,
    finding_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Issue an NC report — RCA + CAPA — for every non-conformity in the audit.

    **Idempotent.** An NC that already carries an `rcaId` is skipped and named
    in the response, so pressing the button twice is safe and pressing it again
    after a re-opened audit picks up only what is new. That matters more than it
    sounds: the alternative is an auditee finding two RCA forms against one NC
    and filling in whichever they opened first.

    Partial failure does not abort the batch. One NC whose CAPA cannot be raised
    (an unseeded source type, no plant) must not cost the other eleven their
    reports — the caller is told exactly which failed and why, the same contract
    `submit_audit` uses for its auto-CAPA spawn.
    """
    materialised = await ensure_findings_exist(db, audit, actor_id=actor_id)
    pairs = await non_conformities_for_audit(db, audit.id)
    if finding_ids is not None:
        wanted = set(finding_ids)
        pairs = [(f, r) for f, r in pairs if f.id in wanted]

    todo = [(f, r) for f, r in pairs if not f.rcaId]
    skipped = [
        {"findingId": f.id, "findingCode": f.findingCode, "ncrNumber": f.ncrNumber,
         "reason": "already has an NC report"}
        for f, _ in pairs if f.rcaId
    ]
    if not todo:
        return {
            "created": 0, "skipped": len(skipped), "failed": 0,
            "nonConformities": len(pairs), "findingsMaterialised": materialised,
            "items": [], "skippedItems": skipped, "failures": [],
        }

    rca_codes = await _allocate_rca_codes(db, len(todo))
    next_ncr = await _highest_ncr_number(db, audit.id) + 1

    items: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for offset, ((finding, response), rca_code) in enumerate(zip(todo, rca_codes)):
        ncr_number = f"{next_ncr + offset:02d}"
        owner = (
            finding.ownerId
            or response.assignedOwnerId
            or response.routedToUserId
            or audit.plantManagerUserId
            or audit.leadAuditorUserId
            or actor_id
        )
        clause = _clause_text(response)
        title = f"NCR {ncr_number} — {(finding.title or response.checkpointQuestion or '')[:150]}"

        rca = RootCauseAnalysis(
            rcaCode=rca_code,
            title=title[:200],
            originType="EVENT",
            sourceEventId=finding.id,
            primaryDomain=PIL_RCA_DOMAIN,
            methodology=PIL_METHODOLOGY,
            status="DRAFT",
            analysisPayload=seed_why_payload(
                nonconformity=finding.description or response.observation or "",
                requirement=response.checkpointQuestion,
                clause=clause,
                department=response.categoryName,
                stream=response.streamCode,
            ),
            narrative=None,
            # The AUDITEE owns the analysis — the form's colour key puts rows
            # 16-25 in the auditee half. Handing it to the auditor who raised
            # the NC would have the auditor analysing their own finding.
            analystId=owner,
            occurrenceDate=audit.actualEndAt or audit.scheduledDate,
            plantId=audit.plantId,
            createdBy=actor_id,
        )
        db.add(rca)
        await db.flush()

        try:
            capa = await spawn_capa(
                db,
                source_code="AUDIT_INTERNAL",
                plant_id=audit.plantId,
                title=title[:200],
                problem=(finding.description or response.observation or finding.title or "")[:2000],
                ref_id=finding.id,
                # The audit, not a per-NC page: the NC register lives on the
                # audit and there is no standalone NC form route to link to.
                ref_url=f"/cams/audits/{audit.id}",
                ref_summary=f"{audit.auditNumber} · NCR {ncr_number} · {finding.findingCode}",
                metadata={
                    "formNo": PIL_FORM_NO,
                    "ncrNumber": ncr_number,
                    "auditNumber": audit.auditNumber,
                    "findingCode": finding.findingCode,
                    "checkpointCode": response.checkpointCode,
                    "department": response.categoryName,
                    "streamCode": response.streamCode,
                    "clauseRef": clause,
                    "grade": response.gradeAwarded,
                    "standardClauses": response.standardClauses or [],
                },
                severity=_capa_severity(finding.severity),
                priority="HIGH" if finding.severity in ("CRITICAL_NC", "MAJOR_NC") else "MODERATE",
                detected_method="INTERNAL_AUDIT",
                owner_id=owner,
                actor_id=actor_id,
                due_days=_days_until(finding.dueDate),
                # The gate. Actions cannot be planned from here — see
                # `assert_actions_unlocked`.
                state="UNDER_RCA",
            )
        except ValueError as exc:
            # Roll the RCA back with it: an RCA whose CAPA never existed is a
            # form the auditee can fill in that leads nowhere.
            await db.delete(rca)
            await db.flush()
            failures.append({
                "findingId": finding.id,
                "findingCode": finding.findingCode,
                "reason": str(exc),
            })
            continue

        capa.rcaMethodology = PIL_METHODOLOGY
        capa.rcaMethodologyRationale = (
            f"{PIL_FORM_NO} prescribes Why-Why analysis for every internal-audit "
            f"non-conformity (revision R1)."
        )
        capa.rcaRecordId = rca.id
        capa.rcaCompleted = False
        capa.rcaDueDate = await _rca_due_date(
            db, severity=finding.severity, closure_target=finding.dueDate
        )
        capa.correctiveActionDueDate = capa.closureTargetDate
        capa.preventiveActionDueDate = capa.closureTargetDate
        capa.verificationSuccessCriteria = (
            "Verification Details for effective closure (PIL/MR/F04-R1): the "
            "auditor confirms on re-check that the nonconformity has not recurred "
            "and the system change is in place."
        )
        capa.affectedDepartments = [response.categoryName] if response.categoryName else None
        await db.flush()

        finding.ncrNumber = ncr_number
        finding.rcaId = rca.id
        finding.rcaStatus = rca.status
        finding.capaId = capa.id
        finding.orgRepresentativeId = audit.plantManagerUserId
        if finding.status == "OPEN":
            finding.status = "CAPA_RAISED"

        items.append({
            "findingId": finding.id,
            "findingCode": finding.findingCode,
            "ncrNumber": ncr_number,
            "checkpointCode": response.checkpointCode,
            "department": response.categoryName,
            "streamCode": response.streamCode,
            "severity": finding.severity,
            "rcaId": rca.id,
            "rcaCode": rca.rcaCode,
            "capaId": capa.id,
            "capaNumber": capa.capaNumber,
            "ownerId": owner,
            "rcaDueDate": capa.rcaDueDate.isoformat() if capa.rcaDueDate else None,
            "dueDate": finding.dueDate.isoformat() if finding.dueDate else None,
        })

    await db.flush()
    return {
        "created": len(items),
        "skipped": len(skipped),
        "failed": len(failures),
        "nonConformities": len(pairs),
        "findingsMaterialised": materialised,
        "items": items,
        "skippedItems": skipped,
        "failures": failures,
    }


def _clause_text(response: AuditCheckpointResponse) -> str | None:
    """The form's "Clause No". An IMS line cites up to three ISO standards, so
    the structured list wins over the free-text field when it is populated."""
    clauses = response.standardClauses or []
    parts = [
        f"{c.get('standard', '')} {c.get('clause', '')}".strip()
        for c in clauses
        if isinstance(c, dict) and (c.get("clause") or c.get("standard"))
    ]
    return " · ".join(p for p in parts if p) or response.requirementReference or None


def _capa_severity(finding_severity: str) -> str:
    return {
        "CRITICAL_NC": "CRITICAL",
        "MAJOR_NC": "HIGH",
        "MINOR_NC": "MODERATE",
    }.get(finding_severity, "MODERATE")


def _days_until(due: date | None) -> int:
    """Days from now to the auditor's "To be completed before", floored at 7.

    Floored rather than allowed to go negative or zero: an NC raised against a
    date that has already passed still needs a workable closure target, and a
    CAPA created already-overdue reports as an SLA breach on the day it is
    raised, which teaches people to ignore the breach count.
    """
    if due is None:
        return 30
    return max(7, (due - _now().date()).days)


# ─────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────
def assert_actions_unlocked(capa: Capa) -> None:
    """Raise unless this CAPA's actions may be planned.

    Narrow on purpose. It fires only for a CAPA that is BOTH `UNDER_RCA` and
    bound to a governed `rcaRecordId` — that pair is what an NC report is, and
    nothing else in the platform creates it. A blanket "UNDER_RCA blocks
    actions" rule would change behaviour for every other module that can park a
    CAPA in that state for reasons of its own.
    """
    if capa.state == "UNDER_RCA" and capa.rcaRecordId:
        raise ValueError(
            f"{PIL_FORM_NO}: the root cause analysis must be approved before "
            f"Correction or Preventive Action can be planned for this "
            f"non-conformity."
        )


async def release_capa_from_rca(
    db: AsyncSession, rca: RootCauseAnalysis, *, actor_id: str
) -> Capa | None:
    """RCA approved → unlock its CAPA and carry the causes across.

    Called from the RCA approval path. Copies the ladder's conclusion into
    `CapaRootCause` so the CAPA register, the pattern grouping and the CAPA PDF
    all see the root cause without knowing the RCA module exists — the same
    denormalisation `RcaIdentifiedCause.enterpriseCategoryId` makes for the
    rollup. The RCA stays system-of-record; these rows are a projection and are
    rewritten, not appended to, on re-approval after a reopen.
    """
    capa = (
        await db.execute(select(Capa).where(Capa.rcaRecordId == rca.id))
    ).scalars().first()
    if capa is None:
        return None

    payload = rca.analysisPayload or {}
    root_cause = (payload.get("rootCause") or "").strip()
    whys = [
        (w.get("answer") or "").strip()
        for w in (payload.get("whys") or [])
        if isinstance(w, dict) and (w.get("answer") or "").strip()
    ]

    capa.rcaCompleted = True
    capa.rcaSummary = generate_rca_summary(PIL_METHODOLOGY, payload)
    capa.rcaCompletedAt = _now()
    capa.rcaCompletedByUserId = actor_id
    # Every rung except the last is a contributing factor; the last is the
    # system failure the form asks for. Keeping the chain rather than only its
    # conclusion is what lets a reviewer see whether the ladder actually
    # reached the system or stopped at "operator did not follow the SOP".
    capa.contributingFactors = whys[:-1] if len(whys) > 1 else []

    existing = (
        await db.execute(select(CapaRootCause).where(CapaRootCause.capaId == capa.id))
    ).scalars().all()
    for row in existing:
        await db.delete(row)
    if root_cause:
        db.add(CapaRootCause(
            capaId=capa.id,
            description=root_cause,
            # PIL's prompt — "what failed in the SYSTEM" — is a systemic answer
            # by construction, so these tag as SYSTEM rather than the generic
            # PROCESS default the manual submit-rca path uses.
            category="SYSTEM",
            confidence="HIGH",
            sortOrder=0,
        ))

    if capa.state == "UNDER_RCA":
        capa.state = "ACTIONS_PLANNED"
        capa.stateChangedAt = _now()
        capa.stateChangedByUserId = actor_id
    capa.updatedByUserId = actor_id
    await db.flush()
    return capa


async def sync_finding_rca_status(
    db: AsyncSession, rca: RootCauseAnalysis
) -> AuditFinding | None:
    """Mirror the RCA's status onto its NC, for the register."""
    if rca.originType != "EVENT" or not rca.sourceEventId:
        return None
    finding = await db.get(AuditFinding, rca.sourceEventId)
    if finding is None or finding.rcaId != rca.id:
        return None
    finding.rcaStatus = rca.status
    if rca.status == "APPROVED" and finding.status == "CAPA_RAISED":
        finding.status = "IN_REMEDIATION"
    await db.flush()
    return finding


# ─────────────────────────────────────────────────────────────────────
# The register — NC → RCA → CAPA → closure, one row per NC
# ─────────────────────────────────────────────────────────────────────
# Where an NC actually IS, as one value. Derived from the three records rather
# than stored, for the same reason `library_segregation` is derived: a stored
# stage is a fourth thing that can contradict the three it summarises.
NC_STAGES = (
    "NOT_TRIGGERED",      # a non-conformity with no NC report raised yet
    "RCA_PENDING",        # auditee has the form, ladder not submitted
    "RCA_IN_REVIEW",      # submitted, awaiting approval
    "ACTIONS_PENDING",    # RCA approved, no Correction/Preventive planned yet
    "ACTIONS_IN_PROGRESS",
    "AWAITING_VERIFICATION",   # actions done, auditor has not re-checked
    "AWAITING_MR_SIGNOFF",     # auditor verified, MR has not signed
    "CLOSED",
)


def _stage(finding: AuditFinding, capa: Capa | None, actions: list[CapaAction]) -> str:
    if finding.mrSignedAt:
        return "CLOSED"
    if finding.auditorSignedAt:
        return "AWAITING_MR_SIGNOFF"
    if not finding.rcaId:
        return "NOT_TRIGGERED"
    if finding.rcaStatus in ("DRAFT", "IN_ANALYSIS", None):
        return "RCA_PENDING"
    if finding.rcaStatus == "PEER_REVIEW":
        return "RCA_IN_REVIEW"
    # RCA approved from here on — unless the CAPA never came out of the gate.
    # Reading the CAPA rather than trusting `rcaStatus` alone matters: if the
    # release failed, the auditee cannot add an action, and a register saying
    # ACTIONS_PENDING would be telling them to do something the API refuses.
    if capa is not None and capa.state == "UNDER_RCA":
        return "RCA_IN_REVIEW"
    if not actions:
        return "ACTIONS_PENDING"
    if all(a.status in ("COMPLETED", "CANCELLED") for a in actions):
        return "AWAITING_VERIFICATION"
    return "ACTIONS_IN_PROGRESS"


async def nc_register(db: AsyncSession, audit: ComplianceAudit) -> dict[str, Any]:
    """Every non-conformity in the audit with its RCA, CAPA and closure state.

    One query per entity kind rather than per row: a 206-checkpoint IMS audit
    can raise dozens of NCs and this is the screen the MR keeps open during a
    closure review.
    """
    pairs = await non_conformities_for_audit(db, audit.id)
    capa_ids = [f.capaId for f, _ in pairs if f.capaId]
    capas = {
        c.id: c for c in (
            await db.execute(select(Capa).where(Capa.id.in_(capa_ids)))
        ).scalars().all()
    } if capa_ids else {}
    actions_by_capa: dict[str, list[CapaAction]] = {}
    if capa_ids:
        for a in (
            await db.execute(
                select(CapaAction)
                .where(CapaAction.capaId.in_(capa_ids))
                .order_by(CapaAction.actionType, CapaAction.sortOrder)
            )
        ).scalars().all():
            actions_by_capa.setdefault(a.capaId, []).append(a)

    today = _now().date()
    rows: list[dict[str, Any]] = []
    for finding, response in pairs:
        capa = capas.get(finding.capaId) if finding.capaId else None
        actions = actions_by_capa.get(capa.id, []) if capa else []
        stage = _stage(finding, capa, actions)
        rca_due = capa.rcaDueDate.date() if capa and capa.rcaDueDate else None
        rows.append({
            "findingId": finding.id,
            "findingCode": finding.findingCode,
            "ncrNumber": finding.ncrNumber,
            "checkpointCode": response.checkpointCode,
            "requirement": response.checkpointQuestion,
            "department": response.categoryName,
            "streamCode": response.streamCode,
            "clauseRef": _clause_text(response),
            "grade": response.gradeAwarded,
            "severity": finding.severity,
            "nonconformity": finding.description or response.observation,
            "ownerId": finding.ownerId,
            "stage": stage,
            "rcaId": finding.rcaId,
            "rcaStatus": finding.rcaStatus,
            "rcaDueDate": rca_due.isoformat() if rca_due else None,
            "rcaOverdue": bool(
                rca_due and rca_due < today and stage in ("RCA_PENDING", "RCA_IN_REVIEW")
            ),
            "capaId": finding.capaId,
            "capaNumber": capa.capaNumber if capa else None,
            "capaState": capa.state if capa else None,
            "correctionCount": sum(
                1 for a in actions if a.actionType == ACTION_TYPE_FOR_CORRECTION
            ),
            "preventiveCount": sum(
                1 for a in actions if a.actionType == ACTION_TYPE_FOR_PREVENTIVE
            ),
            "openActionCount": sum(
                1 for a in actions if a.status not in ("COMPLETED", "CANCELLED")
            ),
            "dueDate": finding.dueDate.isoformat() if finding.dueDate else None,
            "isOverdue": bool(
                finding.dueDate and finding.dueDate < today and stage != "CLOSED"
            ),
            "isRepeatFinding": finding.isRepeatFinding,
            "verificationResult": capa.verificationResult if capa else None,
            "auditorSignedAt": (
                finding.auditorSignedAt.isoformat() if finding.auditorSignedAt else None
            ),
            "mrSignedAt": finding.mrSignedAt.isoformat() if finding.mrSignedAt else None,
            "closedAt": finding.closedAt.isoformat() if finding.closedAt else None,
        })

    by_stage = {s: 0 for s in NC_STAGES}
    for r in rows:
        by_stage[r["stage"]] = by_stage.get(r["stage"], 0) + 1
    return {
        "auditId": audit.id,
        "auditNumber": audit.auditNumber,
        "auditTitle": audit.title,
        "formNo": PIL_FORM_NO,
        "total": len(rows),
        "triggered": sum(1 for r in rows if r["ncrNumber"]),
        "closed": by_stage.get("CLOSED", 0),
        "overdue": sum(1 for r in rows if r["isOverdue"]),
        "byStage": by_stage,
        "items": rows,
    }


async def nc_report(
    db: AsyncSession, finding: AuditFinding
) -> dict[str, Any]:
    """One NC as PIL/MR/F04-R1 itself — every box on the form, in form order.

    Assembled here rather than in the client so the screen, the PDF and any
    export render the same document. `auditorHalf` / `auditeeHalf` mirror the
    form's own colour split, which is what the UI gates editing on.
    """
    response = (
        await db.get(AuditCheckpointResponse, finding.checkpointResponseId)
        if finding.checkpointResponseId else None
    )
    audit = await db.get(ComplianceAudit, finding.auditId)
    rca = await db.get(RootCauseAnalysis, finding.rcaId) if finding.rcaId else None
    capa = await db.get(Capa, finding.capaId) if finding.capaId else None
    actions = (
        await db.execute(
            select(CapaAction)
            .where(CapaAction.capaId == capa.id)
            .order_by(CapaAction.actionType, CapaAction.sortOrder)
        )
    ).scalars().all() if capa else []

    def _action(a: CapaAction) -> dict[str, Any]:
        return {
            "id": a.id,
            "description": a.description,
            "responsibility": a.ownerUserId,   # form: "Responsibility"
            "targetDate": a.dueDate.isoformat() if a.dueDate else None,
            "completedOn": a.completedAt.isoformat() if a.completedAt else None,
            "hodSignature": a.approverUserId,  # form: "HOD Signature"
            "hodSignedAt": a.approvedAt.isoformat() if a.approvedAt else None,
            "status": a.status,
            "evidence": a.evidenceOfCompletion,
        }

    return {
        "formNo": PIL_FORM_NO,
        "findingId": finding.id,
        "stage": _stage(finding, capa, list(actions)),
        # ── auditor half (yellow on the form) ─────────────────────────
        "auditorHalf": {
            "auditNumber": audit.auditNumber if audit else None,
            "department": response.categoryName if response else None,
            "date": (
                (audit.actualEndAt or audit.scheduledDate).isoformat()
                if audit and (audit.actualEndAt or audit.scheduledDate) else None
            ),
            "managementSystem": response.streamCode if response else None,
            "standardClauses": (response.standardClauses or []) if response else [],
            "ncrNumber": finding.ncrNumber,
            "clauseNo": _clause_text(response) if response else None,
            "requirements": response.checkpointQuestion if response else None,
            "observedNonconformity": finding.description or (
                response.observation if response else None
            ),
            "evidence": (response.auditorEvidenceIds or []) if response else [],
            "grade": response.gradeAwarded if response else None,
            "severity": finding.severity,
            "leadAuditor": audit.leadAuditorUserId if audit else None,
            "auditor": (response.assignedAuditorId if response else None)
                       or (audit.leadAuditorUserId if audit else None),
            "organizationRepresentative": finding.orgRepresentativeId,
            "toBeCompletedBefore": finding.dueDate.isoformat() if finding.dueDate else None,
        },
        # ── auditee half (accent on the form) ─────────────────────────
        "auditeeHalf": {
            "rootCauseAnalysis": {
                "rcaId": rca.id if rca else None,
                "rcaCode": rca.rcaCode if rca else None,
                "status": rca.status if rca else None,
                "prompt": PIL_RCA_PROMPT,
                "minLevels": PIL_MIN_WHY_LEVELS,
                "methodology": PIL_METHODOLOGY,
                "problemStatement": (rca.analysisPayload or {}).get("problemStatement") if rca else None,
                "whys": (rca.analysisPayload or {}).get("whys", []) if rca else [],
                "rootCause": (rca.analysisPayload or {}).get("rootCause") if rca else None,
                "dueDate": capa.rcaDueDate.isoformat() if capa and capa.rcaDueDate else None,
                "locked": bool(rca and rca.status == "APPROVED"),
                "problems": validate_why_payload(rca.analysisPayload) if rca else [],
            },
            "correction": {
                "prompt": PIL_CORRECTION_PROMPT,
                "items": [_action(a) for a in actions if a.actionType == ACTION_TYPE_FOR_CORRECTION],
            },
            "preventiveAction": {
                "prompt": PIL_PREVENTIVE_PROMPT,
                "items": [_action(a) for a in actions if a.actionType == ACTION_TYPE_FOR_PREVENTIVE],
            },
            # The gate, stated to the client so it can disable the Add buttons
            # with a reason rather than letting the auditee type an action and
            # then rejecting it on submit.
            "actionsLocked": bool(capa and capa.state == "UNDER_RCA" and capa.rcaRecordId),
            "actionsLockedReason": (
                f"{PIL_FORM_NO}: approve the root cause analysis first."
                if capa and capa.state == "UNDER_RCA" and capa.rcaRecordId else None
            ),
        },
        # ── closure (form rows 26-30) ─────────────────────────────────
        "closure": {
            "verificationDetails": finding.verificationDetails,
            "verificationResult": capa.verificationResult if capa else None,
            "auditorSignature": finding.auditorSignedById,
            "auditorSignedAt": (
                finding.auditorSignedAt.isoformat() if finding.auditorSignedAt else None
            ),
            "closedOn": finding.closedAt.isoformat() if finding.closedAt else None,
            "mrSignature": finding.mrSignedById,
            "mrSignedAt": finding.mrSignedAt.isoformat() if finding.mrSignedAt else None,
        },
        "capa": {
            "capaId": capa.id if capa else None,
            "capaNumber": capa.capaNumber if capa else None,
            "state": capa.state if capa else None,
        },
    }


# ─────────────────────────────────────────────────────────────────────
# Closure — the form's last two rows
# ─────────────────────────────────────────────────────────────────────
async def verify_nc(
    db: AsyncSession,
    finding: AuditFinding,
    *,
    verification_details: str,
    result: str,
    actor_id: str,
) -> dict[str, Any]:
    """Auditor signs row 26: "Verification Details for effective closure".

    INEFFECTIVE does not close anything — it loops the CAPA back to
    ACTIONS_PLANNED, which is the existing `record_verification` behaviour and
    the only honest response to a re-check that found the nonconformity still
    there. The NC stays open and keeps its NCR number; a second NCR for the same
    finding would make the closure rate look better than it is.
    """
    if result not in ("EFFECTIVE", "PARTIALLY_EFFECTIVE", "INEFFECTIVE", "INCONCLUSIVE"):
        raise ValueError(f"Unknown verification result {result!r}.")

    capa = await db.get(Capa, finding.capaId) if finding.capaId else None
    if capa is None:
        raise ValueError("This non-conformity has no CAPA to verify.")
    if capa.state == "UNDER_RCA":
        raise ValueError(
            f"{PIL_FORM_NO}: the root cause analysis is not approved — there is "
            f"nothing to verify yet."
        )

    open_actions = (
        await db.execute(
            select(CapaAction).where(
                CapaAction.capaId == capa.id,
                CapaAction.status.not_in(("COMPLETED", "CANCELLED")),
            )
        )
    ).scalars().all()
    if open_actions and result == "EFFECTIVE":
        raise ValueError(
            f"{len(open_actions)} action(s) are still open — Correction and "
            f"Preventive Action must be completed before effectiveness can be "
            f"verified."
        )

    capa.verificationResult = result
    capa.verificationEvidence = verification_details
    capa.verificationCompletedAt = _now()
    capa.verificationCompletedByUserId = actor_id

    finding.verificationDetails = verification_details
    finding.auditorSignedById = actor_id
    finding.auditorSignedAt = _now()

    if result == "INEFFECTIVE":
        capa.state = "ACTIONS_PLANNED"
        finding.status = "IN_REMEDIATION"
        # The auditor's signature belongs to the verification that FAILED, and
        # keeping it would present a re-opened NC as auditor-signed.
        finding.auditorSignedById = None
        finding.auditorSignedAt = None
    else:
        capa.state = "VERIFIED"
        finding.status = "VERIFICATION"
    capa.stateChangedAt = _now()
    capa.stateChangedByUserId = actor_id
    await db.flush()
    return {
        "findingId": finding.id,
        "ncrNumber": finding.ncrNumber,
        "result": result,
        "capaState": capa.state,
        "findingStatus": finding.status,
        "reopened": result == "INEFFECTIVE",
    }


async def mr_sign_off(
    db: AsyncSession, finding: AuditFinding, *, actor_id: str
) -> dict[str, Any]:
    """M.R. signs, and the NC closes. The last row of the form.

    Two signatures, in order, because that is what the paper requires: the
    auditor attests the fix works, the Management Representative accepts it on
    behalf of the management system. Allowing the MR to sign an unverified NC
    would make the second signature decorative.
    """
    if not finding.auditorSignedAt:
        raise ValueError(
            f"{PIL_FORM_NO}: the auditor must record verification of effective "
            f"closure before the M.R. can sign."
        )
    if finding.mrSignedAt:
        raise ValueError("This non-conformity is already closed.")

    finding.mrSignedById = actor_id
    finding.mrSignedAt = _now()
    finding.status = "CLOSED"
    finding.closedAt = _now()
    finding.closedById = actor_id

    capa = await db.get(Capa, finding.capaId) if finding.capaId else None
    if capa is not None:
        capa.state = "CLOSED"
        capa.stateChangedAt = _now()
        capa.stateChangedByUserId = actor_id
        capa.closedAt = _now()
        capa.closedByUserId = actor_id
    await db.flush()
    return {
        "findingId": finding.id,
        "ncrNumber": finding.ncrNumber,
        "status": "CLOSED",
        "closedAt": finding.closedAt.isoformat() if finding.closedAt else None,
    }


__all__ = [
    "PIL_FORM_NO",
    "PIL_METHODOLOGY",
    "PIL_MIN_WHY_LEVELS",
    "PIL_RCA_PROMPT",
    "PIL_CORRECTION_PROMPT",
    "PIL_PREVENTIVE_PROMPT",
    "ACTION_TYPE_FOR_CORRECTION",
    "ACTION_TYPE_FOR_PREVENTIVE",
    "NC_STAGES",
    "ensure_findings_exist",
    "non_conformities_for_audit",
    "seed_why_payload",
    "validate_why_payload",
    "trigger_for_audit",
    "nc_register",
    "nc_report",
    "assert_actions_unlocked",
    "release_capa_from_rca",
    "sync_finding_rca_status",
    "verify_nc",
    "mr_sign_off",
]
