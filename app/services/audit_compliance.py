"""Audit & Compliance Management — service layer.

Functions flush but DO NOT commit; the router commits. Mirrors the pattern used
by the other vertical modules (oos/capa). Schema is owned by Prisma; this layer
reads/writes the SQLAlchemy mirror in app/models/audit_compliance.py.

Lifecycle: schedule -> conduct (partial-save per checkpoint) -> submit (route
failed/partial to auditees + auto-CAPA on critical) -> auditee respond ->
plant-manager review -> close. The `score` snapshot on ComplianceAudit is
recomputed only at submit/review/close; live conduct progress is computed
on-read.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sys
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Integer, and_, cast, func, not_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.audit_compliance import (
    AuditCheckpointLibrary,
    AuditCheckpointResponse,
    AuditReport,
    AuditTemplate,
    CheckpointInteraction,
    ComplianceAudit,
)
from app.models.cams import CamsAuditType
from app.models.factory import FactoryProfile
from app.models.plant import Plant
from app.models.user import User
from app.services import (
    assurance,
    citations,
    independence,
    independence_events,
    page_grading,
    scoring_rules,
    signoff,
)
from app.services.insights import rules_audit_report

MINIMUM_PASS_SCORE = 80.0

# capa_severity_if_triggered (checkpoint) -> CAPA severity
_CAPA_SEVERITY = {"critical": "CRITICAL", "major": "HIGH", "minor": "MODERATE"}

# normalized scoring bucket -> first-class assessmentStatus (audit-lifecycle v2)
_ASSESS_STATUS = {"pass": "PASS", "partial": "PARTIAL", "fail": "FAIL", "na": "NA"}

# overallStatus values for a checkpoint that has NOT yet been submitted to the
# auditee workflow — routing can be cleared freely on these.
_PRE_SUBMIT_STATUSES = {
    "not_answered", "answered_pass", "answered_partial", "answered_fail", "answered_na",
}

# CheckpointWorkflowState — the iteration state machine (audit-lifecycle v2).
# Terminal-for-finalization states; an audit can only close once EVERY
# checkpoint is in one of these.
_TERMINAL_STATES = {"PASSED", "RESOLVED", "ACCEPTED_WITH_CAPA", "FINALIZED"}


async def _notify(db: AsyncSession, user_id: str | None, subject: str, body: str) -> None:
    """Best-effort handoff notification (email). Never raises — a notification
    failure must not block the transition (mirrors erm.notify_escalation). The
    my-checkpoints inbox is the primary in-app channel; this is the nudge."""
    if not user_id:
        return
    try:
        u = await db.get(User, user_id)
        email = getattr(u, "email", None) if u else None
        if not email:
            return
        from app.services.notifications import send_email
        await send_email([email], subject, body)
    except Exception:  # noqa: BLE001 — notifications are best-effort
        pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _naive(dt: datetime | None) -> datetime | None:
    """Drop tzinfo so naive (asyncpg) and aware datetimes can be compared."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _industry_short(code: str) -> str:
    """GARMENTS_TEXTILE -> GT, MANUFACTURING_GENERIC -> MG."""
    return "".join(part[0] for part in code.split("_") if part)[:3].upper() or "AC"


def _norm_value(value: Any) -> str | None:
    """Map a checkpoint response value into a scoring bucket."""
    if value in ("pass", "yes"):
        return "pass"
    if value == "partial":
        return "partial"
    if value in ("fail", "no"):
        return "fail"
    if value == "na":
        return "na"
    return None


# The engine's verdict bucket -> the Page grade that means the same thing.
# The inverse of page_grading.GRADE_TO_VALUE, needed because two paths still
# set a bare bucket: the bulk "mark discipline Pass/NA" fast path, and the
# supplier-portal / legacy API clients. Both must leave the grading columns
# populated, or a bulk-marked discipline would score zero out of its allotment.
_VALUE_TO_GRADE = {
    "pass": page_grading.GRADE_EFFECTIVE,
    "partial": page_grading.GRADE_SOME_IMPROVEMENT,
    "fail": page_grading.GRADE_MAJOR_IMPROVEMENT,
    "na": page_grading.GRADE_NA,
}


def _apply_page_grading(
    resp: AuditCheckpointResponse, payload: dict[str, Any], merged: dict[str, Any]
) -> str | None:
    """Reconcile the Page grading columns with the engine's verdict bucket, in
    whichever direction this particular save supplied.

    Returns the resulting bucket (pass/partial/fail/na, or None if cleared) so
    the caller drives the existing routing / CAPA / workflow logic off it
    unchanged — the grading is a richer face on the same verdict, not a second
    state machine beside it.

    Precedence is deliberate: an explicit `gradeAwarded` always wins over a bare
    `value` in the same payload, because the grade is what the auditor actually
    chose on screen and the value is derived from it.
    """
    # ── The narrowed vocabulary, resolved before anything else ────────────
    #
    # A TRISTATE checkpoint is answered with one of Conformance /
    # Non-Conformance / Observation. It is rewritten here into the grade +
    # status the rest of this function already speaks, so the score, the
    # routing, the CAPA rules and both reports need no branch of their own —
    # the three parameters are a narrower FACE on the same verdict, exactly as
    # the Page grade is a richer face on the engine's bucket.
    #
    # Done by mutating `payload` rather than by forking the logic below, so
    # there is only ever one place that decides what a saved verdict means.
    if "conformance" in payload:
        raw = payload.get("conformance")
        if raw in (None, ""):
            payload = {**payload, "gradeAwarded": None, "complianceStatus": None}
        else:
            tri = page_grading.normalise_tristate(raw)
            if tri is None:
                raise ValueError(
                    f"Unknown conformance '{raw}' — expected one of "
                    + ", ".join(page_grading.TRISTATE_VERDICTS)
                )
            g, s = page_grading.TRISTATE_TO_GRADE_STATUS[tri]
            # An explicit grade/status alongside a conformance token would make
            # the row's meaning depend on which the server read first.
            payload = {**payload, "gradeAwarded": g, "complianceStatus": s}

    grade_sent = "gradeAwarded" in payload
    value_sent = "value" in payload

    if grade_sent:
        grade = page_grading.normalise_grade(payload.get("gradeAwarded"))
        if payload.get("gradeAwarded") not in (None, "") and grade is None:
            raise ValueError(f"Unknown grade '{payload.get('gradeAwarded')}'")
    elif value_sent:
        grade = _VALUE_TO_GRADE.get(_norm_value(payload.get("value")) or "")
    else:
        grade = resp.gradeAwarded

    val = page_grading.value_for_grade(grade)
    resp.gradeAwarded = grade
    # Keep the JSON blob coherent with the columns. Reports, the supplier
    # portal and the auditee screens all still read `auditorResponse.value`;
    # letting the two disagree would be the worst of both models.
    merged["value"] = val

    # Status (column F). Only auto-suggested when the auditor has not chosen one
    # for this checkpoint — a Repeated Non Compliance must never be silently
    # downgraded to Non Compliance by a later re-grade.
    if "complianceStatus" in payload:
        status = page_grading.normalise_status(payload.get("complianceStatus"))
        if payload.get("complianceStatus") not in (None, "") and status is None:
            raise ValueError(f"Unknown status '{payload.get('complianceStatus')}'")
        resp.complianceStatus = status
    elif grade is None:
        resp.complianceStatus = None
    elif resp.complianceStatus is None:
        resp.complianceStatus = page_grading.suggest_status(grade)

    # N/A is the one re-grade that MUST overwrite a status the auditor chose.
    # Marking a checkpoint not applicable says it was never assessed, so a row
    # left reading "Complied" (or worse, "Repeated Non Compliance") while
    # scoring as N/A is a contradiction the workbook has no row for — and on a
    # tristate card it lights up two mutually exclusive controls at once.
    # Enforced here as well as in the client because a rule that lives only in
    # the UI is not a rule.
    if grade == page_grading.GRADE_NA and resp.complianceStatus not in page_grading.NOT_APPLICABLE_STATUSES:
        resp.complianceStatus = page_grading.STATUS_NA

    # Risk grade (column H). Cleared when the checkpoint stops being a finding,
    # so a re-graded-to-Effective checkpoint cannot keep reporting High risk.
    if "riskGrade" in payload:
        risk = page_grading.normalise_risk_grade(payload.get("riskGrade"))
        if payload.get("riskGrade") not in (None, "") and risk is None:
            raise ValueError(f"Unknown risk grade '{payload.get('riskGrade')}'")
        resp.riskGrade = risk
    # Cleared on the GRADE, not on whether the mode demands one: a
    # Non-Conformance re-graded to Conformance must not keep reporting High
    # risk, but a risk grade an auditor set on a tristate finding is theirs and
    # is kept.
    if not page_grading.carries_risk_grade(grade):
        resp.riskGrade = None

    # Score allotted (column D) is never the auditor's choice — it is 3 for a
    # scored checkpoint and NULL for an N/A one, which is what takes it out of
    # the denominator.
    resp.scoreAllotted = page_grading.allotted_for_grade(grade)

    # Score obtained (column E). An explicit value from the client is honoured —
    # the workbook lets an auditor override the ladder — and otherwise it follows
    # grade + status, which is where the -1 repeat penalty comes from.
    #
    # The re-derivation is guarded on the grading actually having moved in THIS
    # payload. Without that guard an observation-only autosave (the conduct
    # screen fires one on every keystroke pause) would silently reset a score the
    # auditor had deliberately overridden a moment earlier.
    if "scoreObtained" in payload and payload.get("scoreObtained") is not None:
        try:
            score = int(payload["scoreObtained"])
        except (TypeError, ValueError) as e:
            raise ValueError("scoreObtained must be a whole number") from e
        if score not in page_grading.SCORE_OBTAINED_CHOICES:
            raise ValueError(
                f"scoreObtained must be one of {', '.join(str(s) for s in page_grading.SCORE_OBTAINED_CHOICES)}"
            )
        resp.scoreObtained = None if resp.scoreAllotted is None else score
    elif grade_sent or value_sent or "complianceStatus" in payload:
        resp.scoreObtained = page_grading.suggest_score(grade, resp.complianceStatus)
    elif resp.scoreAllotted is None:
        resp.scoreObtained = None  # became N/A — nothing left to score

    return val


# ─────────────────────────────────────────────────────────────────────
# Serialization
# ─────────────────────────────────────────────────────────────────────


def _response_to_dict(r: AuditCheckpointResponse, *, include_interactions: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": r.id,
        "checkpointCode": r.checkpointCode,
        "checkpointQuestion": r.checkpointQuestion,
        "guidance": r.guidance,
        "requirementReference": r.requirementReference,
        "standard": r.standard,
        "categoryId": r.categoryId,
        "categoryName": r.categoryName,
        "categoryColor": r.categoryColor,
        "criticality": r.criticality,
        "responseType": r.responseType,
        "sequence": r.sequence,
        "orderIndex": r.orderIndex,
        # Department-segregated management-system audits. Null on every other
        # library, which is how the client tells a department audit from a
        # discipline one without being told which library it is looking at.
        "streamCode": r.streamCode,
        "replicationKey": r.replicationKey,
        "pairKey": r.pairKey,
        "conformanceMode": page_grading.normalise_conformance_mode(r.conformanceMode),
        # The three-parameter face of the stored status, so a TRISTATE card can
        # render its selection without re-deriving the mapping client-side.
        "conformance": page_grading.tristate_for_status(r.complianceStatus),
        "standardClauses": r.standardClauses or [],
        # Page Industries grading (checklist columns C–F, H, I).
        "requirementType": r.requirementType,
        "gradeAwarded": r.gradeAwarded,
        "scoreAllotted": r.scoreAllotted,
        "scoreObtained": r.scoreObtained,
        "complianceStatus": r.complianceStatus,
        "riskGrade": r.riskGrade,
        "requiresPhotoOnFail": r.requiresPhotoOnFail,
        "autoTriggerCapaOnFail": r.autoTriggerCapaOnFail,
        "capaSeverity": r.capaSeverity,
        "linkedSafeopsModule": r.linkedSafeopsModule,
        "routedToUserId": r.routedToUserId,
        # Ownership (audit-lifecycle v2). owner = auditee; auditor = conductor.
        "assignedOwnerId": r.assignedOwnerId,
        "assignedAuditorId": r.assignedAuditorId,
        "assignedById": r.assignedById,
        "assignedAt": _iso(r.assignedAt),
        # Ad-hoc / custom flag.
        "isAdHoc": r.isAdHoc,
        "addedById": r.addedById,
        # Two-axis state.
        "assessmentStatus": r.assessmentStatus,
        "workflowState": r.workflowState,
        "currentRound": r.currentRound,
        # Carousel capture.
        "observation": r.observation,
        "auditorNote": r.auditorNote,
        "auditorEvidenceIds": r.auditorEvidenceIds or [],
        "auditeeEvidenceIds": r.auditeeEvidenceIds or [],
        "capaId": r.capaId,
        "finalizedAt": _iso(r.finalizedAt),
        "auditorResponse": r.auditorResponse,
        "auditeeResponse": r.auditeeResponse,
        "plantManagerReview": r.plantManagerReview,
        "capa": r.capa,
        "overallStatus": r.overallStatus,
        "answeredAt": _iso(r.answeredAt),
    }
    if include_interactions:
        d["interactions"] = [
            _interaction_to_dict(i) for i in sorted(r.interactions, key=lambda x: (x.timestamp, x.round))
        ]
    return d


def _audit_to_dict(
    a: ComplianceAudit,
    *,
    include_responses: bool = False,
    include_interactions: bool = False,
    supplier: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialise an audit.

    `supplier` is resolved by the CALLER (batched for lists, single for detail)
    because the link lives in `SupplierAuditLink`, not on the audit row. It is
    passed in rather than fetched here so a 400-row register does not issue 400
    extra queries.

    **`subjectType` is DERIVED from the link's presence**, never stored. There
    is no column to backfill and no way for a stored flag to drift out of step
    with whether a supplier is actually attached — the two cannot disagree if
    only one of them exists.
    """
    d: dict[str, Any] = {
        "id": a.id,
        "auditNumber": a.auditNumber,
        "title": a.title,
        "subjectType": "VENDOR" if supplier else "OWN_SITE",
        # The audited party, named on every row. For an own-facility audit this
        # is the plant; the register must never render a supplier audit as if it
        # were an audit of our own site.
        "subjectLabel": (supplier or {}).get("legalName") if supplier else None,
        "supplier": supplier,
        "plantId": a.plantId,
        "templateId": a.templateId,
        "industryCode": a.industryCode,
        "auditType": a.auditType,
        "scopeDepartments": a.scopeDepartments,
        "scopeAreas": a.scopeAreas,
        "scopeDescription": a.scopeDescription,
        "selectedDisciplineIds": a.selectedDisciplineIds or [],
        "scopePresetUsed": a.scopePresetUsed,
        "materializedCheckpointCount": a.materializedCheckpointCount,
        "adHocCount": a.adHocCount,
        "scheduledDate": _iso(a.scheduledDate),
        "scheduledStartTime": a.scheduledStartTime,
        "estimatedDurationHours": a.estimatedDurationHours,
        "leadAuditorUserId": a.leadAuditorUserId,
        "coAuditors": a.coAuditors,
        "auditees": a.auditees,
        "plantManagerUserId": a.plantManagerUserId,
        "status": a.status,
        "actualStartAt": _iso(a.actualStartAt),
        "actualEndAt": _iso(a.actualEndAt),
        "submittedAt": _iso(a.submittedAt),
        "score": a.score,
        "totalCheckpoints": a.totalCheckpoints,
        "answeredCheckpoints": a.answeredCheckpoints,
        "overallCompliancePct": a.overallCompliancePct,
        "auditPassed": a.auditPassed,
        "openCapaCount": a.openCapaCount,
        "criticalFailureCount": a.criticalFailureCount,
        "openingRemarks": a.openingRemarks,
        "closingRemarks": a.closingRemarks,
        "isRecurring": a.isRecurring,
        "createdByUserId": a.createdByUserId,
        "createdAt": _iso(a.createdAt),
        "closedAt": _iso(a.closedAt),
    }
    if include_responses:
        d["responses"] = [
            _response_to_dict(r, include_interactions=include_interactions)
            for r in sorted(a.responses, key=lambda x: (x.categoryId, x.sequence))
        ]
    return d


# ─────────────────────────────────────────────────────────────────────
# Reference data (libraries + templates)
# ─────────────────────────────────────────────────────────────────────


def library_subject_scope(industry_code: str, categories: list[dict[str, Any]]) -> str:
    """Which audit SUBJECT a checkpoint library is written for.

    A supplier audit must never materialise an own-facility library. "Are the
    kiln refractory inspections within validity" is a question about our plant;
    running it against a garment supplier produces a report that reads like an
    internal plant inspection, which is a credibility problem in front of a
    client rather than a cosmetic one.

    Derived rather than stored, for the same reason `subjectType` is derived
    from the supplier link: there is no column to add, nothing to backfill, and
    no way for a stored flag to drift out of step with the content.

    Three signals, in descending explicitness:

      1. `subject_scope` on a category — the hook the import path sets when a
         customer loads their own Supplier Code of Conduct.
      2. `regimeCode` on a category — written by `seed_buyer_regimes.py`. The
         buyer regimes (SMETA/BSCI/WRAP/Higg/SLCP) are social-compliance
         instruments aimed at a supplier's facility, not ours.
      3. The `REGIME_` industry-code prefix, which is how those libraries are
         keyed.

    Anything else is an own-facility industry library. Defaulting to OWN_SITE is
    the safe direction: a mislabelled supplier library shows up as missing from
    the supplier picker (visible, fixable), whereas defaulting to VENDOR would
    silently offer plant checklists for supplier audits — the bug this exists
    to prevent.
    """
    for c in categories or []:
        scope = (c.get("subject_scope") or "").upper()
        if scope in ("VENDOR", "OWN_SITE", "BOTH"):
            return scope
        if c.get("regimeCode"):
            return "VENDOR"
    if (industry_code or "").startswith("REGIME_"):
        return "VENDOR"
    return "OWN_SITE"


# ── Checkpoint evidence: what may be attached, and where it lands ────
#
# Domain policy, not an HTTP concern, which is why it lives here rather than in
# the router that happens to enforce it: "a factory licence is acceptable audit
# evidence" is a statement about auditing. Keeping it here also makes it
# testable without a storage client.
#
# Photographs were the only thing this flow was built for, but half of an audit's
# evidence is paperwork — a licence, a calibration certificate, a test report, a
# wage register extract. Those arrive as PDFs and Office files, and while they
# were rejected at the door the evidence for exactly the statutory checkpoints
# that need it hardest had nowhere to go but email.
#
# An allowlist, not a widening to anything. Same document set the rest of the
# product already accepts (`routers/attachments.py`, `routers/erm_attachments.py`)
# so the audit module is not the one place with its own idea of a safe upload.
# `image/svg+xml` is deliberately absent: it looks like an image, can carry
# script, and is never a photograph of a shop floor.
ALLOWED_IMAGE_MIME = frozenset({
    "image/jpeg", "image/png", "image/webp", "image/heic", "image/gif",
})
ALLOWED_DOCUMENT_MIME = frozenset({
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
    "application/msword",  # legacy .doc
    "application/vnd.ms-excel",  # legacy .xls
    "text/csv",
    "text/plain",
})
ALLOWED_UPLOAD_MIME = ALLOWED_IMAGE_MIME | ALLOWED_DOCUMENT_MIME

UNSUPPORTED_UPLOAD_MESSAGE = (
    "Attach a photo (JPEG, PNG, WebP, HEIC) or a document (PDF, Word, Excel, CSV, text)."
)


def attachment_storage_path(
    audit_id: str | None, checkpoint_code: str | None, file_name: str
) -> str:
    """Where one piece of checkpoint evidence is stored.

    Two properties this must hold, both load-bearing:

    * The file name comes from a browser, so `../`, `..\\` and separators are
      flattened — a path is built from it, and it must not escape its prefix.
    * The extension SURVIVES. Attachments stored before `mimeType` was recorded
      are classified by extension on the client, so stripping it would make an
      old PDF indistinguishable from a photograph — which renders as a broken
      thumbnail where a reviewer expects evidence.

    The random prefix is what stops a second upload of `licence.pdf` on the same
    checkpoint silently overwriting the first auditor's evidence.
    """
    safe = re.sub(r"[\\/]", "_", file_name)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", safe)[:80] or "file"
    seg = re.sub(r"[^a-z0-9._-]", "_", (checkpoint_code or "general").lower())[:40]
    short = secrets.token_hex(4)
    return f"audit-compliance/{audit_id or 'unassigned'}/{seg}/{short}-{safe}"


# ── Report streams ───────────────────────────────────────────────────
#
# A department audit is conducted once and reported twice. Page's own workbook
# is two sheets — the IMS one (ISO 9001 / 14001 / 45001) and the EnMS one
# (ISO 50001) — and their management review reads them as two documents against
# two different certification scopes. So the stream is a property of the
# CHECKPOINT (which sheet the line came off), and a report is a filter on it.
#
# Ordered as the workbook's tabs are, which is the order the reports are issued
# and the order the conduct screen offers them in.
STREAMS: tuple[str, ...] = ("IMS", "ENMS")

STREAM_META: dict[str, dict[str, str]] = {
    "IMS": {
        "code": "IMS",
        "label": "IMS",
        "longLabel": "Integrated Management System",
        "reportTitle": "Internal Audit Report — IMS",
        "standards": "ISO 9001:2015, ISO 14001:2015 & ISO 45001:2018",
        "color": "#7C3AED",
    },
    "ENMS": {
        "code": "ENMS",
        "label": "EnMS",
        "longLabel": "Energy Management System",
        "reportTitle": "Internal Audit Report — EnMS",
        "standards": "ISO 50001:2018",
        "color": "#CA8A04",
    },
}


def normalise_stream(value: Any) -> str | None:
    """Accept a stream code in any casing. Unknown / empty -> None.

    None means "the whole audit", which is what every report was before streams
    existed and what every single-stream library still produces — so it must
    stay a legal value rather than an error.
    """
    s = str(value or "").strip().upper()
    return s if s in STREAMS else None


def list_streams() -> list[dict[str, str]]:
    return [dict(STREAM_META[s]) for s in STREAMS]


# ── Audit categories ─────────────────────────────────────────────────
#
# The scheduler's first choice. A category names the KIND of audit being run,
# and each category resolves exactly one checkpoint library — which is what
# makes "the disciplines populate from the category" true rather than a
# coincidence of whichever library sorted first.
#
# Ordered as they appear in the wizard, and each declares the audit SUBJECT it
# belongs to. `subjectType` is what keeps the two axes from tangling: a category
# is offered only for the subject it was written for, so an own-facility audit
# cannot be scoped against a supplier checklist and vice versa.
#
# `SOCIAL_COMPLIANCE` is VENDOR. The PIL Social Compliance checklist asks whether
# "the factory" holds a valid licence, pays minimum wages and employs no child
# labour — questions put to a SUPPLIER's factory during a social audit, not to
# our own site, where the same ground is covered by the internal HR/EHS audit.
# Offering it for an own-facility audit produced a report that read as though we
# were screening ourselves as a vendor.
AUDIT_CATEGORIES: list[dict[str, Any]] = [
    {
        "code": "INTERNAL",
        "label": "Internal",
        "description": "Page Industries internal audit — HR, EHS and Production.",
        "industryCode": "PAGE_INDUSTRIES",
        "subjectType": "OWN_SITE",
        "auditType": "internal_audit",
        "standards": ["Page Industries Internal Audit"],
    },
    {
        "code": "MANAGEMENT_SYSTEMS",
        "label": "QMS, EMS, OHS",
        "description": "Integrated management-system audit against ISO 9001, 14001, 45001 and 50001.",
        "industryCode": "PAGE_IMS",
        "subjectType": "OWN_SITE",
        "auditType": "management_system_audit",
        "standards": [
            "ISO 9001:2015", "ISO 14001:2015", "ISO 45001:2018", "ISO 50001:2018",
        ],
    },
    {
        "code": "SOCIAL_COMPLIANCE",
        "label": "Social Compliance",
        "description": "PIL Social Compliance Audit checklist — labour, wages, safety and environment at a supplier's factory.",
        "industryCode": "PAGE_SOCIAL",
        "subjectType": "VENDOR",
        "auditType": "social_compliance_audit",
        "standards": ["PIL Social Compliance Audit Checklist (Annexure-2, v4)"],
    },
    {
        "code": "SUPPLIER_COC",
        "label": "Supplier Code of Conduct",
        "description": "Supplier Code of Conduct — labour standards, health & safety, environment, ethics and management system.",
        "industryCode": "SUPPLIER_COC",
        "subjectType": "VENDOR",
        "auditType": "supplier_coc_audit",
        "standards": ["Supplier Code of Conduct"],
    },
]

_CATEGORY_BY_INDUSTRY = {c["industryCode"]: c["code"] for c in AUDIT_CATEGORIES}


def library_audit_category(industry_code: str, categories: list[dict[str, Any]]) -> str | None:
    """Which audit category a checkpoint library belongs to, or None.

    Derived, not stored — same reasoning as `library_subject_scope`: there is no
    column to add, nothing to backfill, and no stored flag that can drift out of
    step with the content.

    `audit_category` on a category dict is the explicit hook, so a library
    imported later can declare its own category without editing this map. The
    industry-code map is the fallback for the three libraries shipped here.

    None means "not one of the own-facility audit categories" — the supplier
    checklists and any retired industry library. That is why the wizard must not
    treat None as a fourth category: those libraries are reached through the
    audit SUBJECT (supplier), which is a different axis entirely.
    """
    for c in categories or []:
        declared = (c.get("audit_category") or "").upper()
        if declared:
            return declared
    return _CATEGORY_BY_INDUSTRY.get(industry_code)


def library_segregation(categories: list[dict[str, Any]]) -> str:
    """What a category of this library IS — a discipline or a department.

    Derived from the content, like `library_subject_scope` and
    `library_audit_category` and for the same reason: no column to backfill and
    no stored flag that can contradict the checkpoints underneath it.

    This is not cosmetic. The scheduling wizard, the programme wizard and the
    conduct navigator all label this axis, and "Disciplines in scope" over a
    list reading HR / Admin / OHC is simply a false statement on screen.
    """
    for c in categories or []:
        if (c.get("segregation") or "").upper() == "DEPARTMENT":
            return "DEPARTMENT"
    return "DISCIPLINE"


def library_conformance_mode(categories: list[dict[str, Any]]) -> str:
    """FULL (seven-value status ladder) or TRISTATE (Conformance /
    Non-Conformance / Observation). A library declares one; a mixed library
    reports FULL, because the wizard's summary must not promise the narrower
    control for checkpoints that will not offer it."""
    modes = {
        page_grading.normalise_conformance_mode(c.get("conformance_mode"))
        for c in categories or []
    }
    return (
        page_grading.CONFORMANCE_TRISTATE
        if modes == {page_grading.CONFORMANCE_TRISTATE}
        else page_grading.CONFORMANCE_FULL
    )


def library_streams(categories: list[dict[str, Any]]) -> list[str]:
    """Which report streams this library's checkpoints belong to, in STREAMS
    order. Empty for every library that is reported as one document."""
    found: set[str] = set()
    for c in categories or []:
        for s in c.get("streams") or []:
            if normalise_stream(s):
                found.add(normalise_stream(s))
        for cp in c.get("checkpoints") or []:
            s = normalise_stream(cp.get("stream"))
            if s:
                found.add(s)
    return [s for s in STREAMS if s in found]


def list_audit_categories() -> list[dict[str, Any]]:
    """The category menu, for the scheduling wizard."""
    return [dict(c) for c in AUDIT_CATEGORIES]


async def list_libraries(db: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(AuditCheckpointLibrary).where(AuditCheckpointLibrary.isActive.is_(True)).order_by(
                AuditCheckpointLibrary.industryName
            )
        )
    ).scalars().all()
    out = []
    for lib in rows:
        cats = lib.categories or []
        out.append(
            {
                "id": lib.id,
                "industryCode": lib.industryCode,
                "industryName": lib.industryName,
                "version": lib.version,
                "checkpointCount": lib.checkpointCount,
                # OWN_SITE | VENDOR | BOTH — which audit subject this library is
                # written for. The scheduling wizard branches on it so a supplier
                # audit cannot be scoped against plant checklists.
                "subjectScope": library_subject_scope(lib.industryCode, cats),
                # INTERNAL | MANAGEMENT_SYSTEMS | SOCIAL_COMPLIANCE, or null for
                # the supplier checklists. The wizard's category selector groups
                # on this, and the disciplines it offers are this library's.
                "auditCategory": library_audit_category(lib.industryCode, cats),
                # DISCIPLINE | DEPARTMENT — what a "category" of this library is.
                # The wizard labels its scope selector from this; without it a
                # department library renders under "Disciplines in scope".
                "segregation": library_segregation(cats),
                # FULL | TRISTATE — which conformance control its checkpoints
                # will offer during conduct.
                "conformanceMode": library_conformance_mode(cats),
                # The report streams it produces, empty when it is one document.
                "streams": [dict(STREAM_META[s]) for s in library_streams(cats)],
                # A library can be correctly scoped and still have no content —
                # the buyer regimes ship as structure with zero checkpoints
                # because the criteria are licensed. Selectable requires BOTH.
                "isSelectable": sum(len(c.get("checkpoints") or []) for c in cats) > 0,
                "categories": [
                    {
                        "category_code": c.get("category_code"),
                        "category_name": c.get("category_name"),
                        "category_color": c.get("category_color"),
                        "category_icon": c.get("category_icon"),
                        "checkpointCount": len(c.get("checkpoints", [])),
                    }
                    for c in cats
                ],
            }
        )
    return out


async def get_library(db: AsyncSession, industry_code: str) -> dict[str, Any] | None:
    """Full library (categories + checkpoints) for the authoring view."""
    lib = (
        await db.execute(select(AuditCheckpointLibrary).where(AuditCheckpointLibrary.industryCode == industry_code))
    ).scalar_one_or_none()
    if lib is None:
        return None
    return {
        "id": lib.id, "industryCode": lib.industryCode, "industryName": lib.industryName,
        "version": lib.version, "checkpointCount": lib.checkpointCount, "isActive": lib.isActive,
        "categories": lib.categories or [],
    }


async def import_library(db: AsyncSession, *, user: User, payload: dict[str, Any]) -> dict[str, Any]:
    """Create or replace a per-industry checkpoint library — the source the audit
    flow materializes from. The bulk-import path for large (≈1500-checkpoint)
    libraries: an admin pastes/uploads the discipline→checkpoint structure once.
    Upsert by industryCode; recomputes checkpointCount."""
    code = (payload.get("industryCode") or "").strip()
    if not code:
        raise ValueError("industryCode is required")
    cats = payload.get("categories") or []
    if not isinstance(cats, list) or not cats:
        raise ValueError("At least one discipline (category) with checkpoints is required")
    # Light validation: each category needs a code + at least one checkpoint with a code+question.
    seen_codes: set[str] = set()
    cp_count = 0
    for c in cats:
        if not c.get("category_code"):
            raise ValueError("Every discipline needs a category_code")
        for cp in c.get("checkpoints") or []:
            if not cp.get("code") or not cp.get("question"):
                raise ValueError(f"Checkpoint in {c['category_code']} needs a code and a question")
            if cp["code"] in seen_codes:
                raise ValueError(f"Duplicate checkpoint code: {cp['code']}")
            seen_codes.add(cp["code"])
            # Requirement Type is optional, but a MISSPELLED one must not pass:
            # it would normalise to null at materialisation and the checkpoint
            # would silently lose its statutory classification on every audit
            # thereafter — an omission nobody would notice until an auditor
            # went looking for it.
            if cp.get("requirement_type") is not None:
                if page_grading.normalise_requirement_type(cp["requirement_type"]) is None:
                    raise ValueError(
                        f"Checkpoint {cp['code']}: requirement_type must be "
                        f"STATUTORY_REGULATORY or INTERNAL_REQUIREMENT "
                        f"(got '{cp['requirement_type']}')"
                    )
            cp_count += 1
    if cp_count == 0:
        raise ValueError("The library has no checkpoints")

    lib = (
        await db.execute(select(AuditCheckpointLibrary).where(AuditCheckpointLibrary.industryCode == code))
    ).scalar_one_or_none()
    if lib is None:
        lib = AuditCheckpointLibrary(
            industryCode=code, industryName=payload.get("industryName") or code,
            version=payload.get("version") or "2026.1", categories=cats,
            checkpointCount=cp_count, isActive=True,
        )
        db.add(lib)
        created = True
    else:
        lib.industryName = payload.get("industryName") or lib.industryName
        lib.version = payload.get("version") or lib.version
        lib.categories = cats
        lib.checkpointCount = cp_count
        lib.isActive = True
        created = False
    await db.flush()
    return {"ok": True, "created": created, "industryCode": code, "checkpointCount": cp_count, "disciplines": len(cats)}


# ─────────────────────────────────────────────────────────────────────
# Library editing — the checkpoint set as an editable template.
#
# The library arrived as a bulk JSON import, which is the right tool for
# authoring 1,500 checkpoints in one go and the wrong one for fixing a typo in
# question 37. Re-pasting the whole document to change one line risks losing
# every other edit made since, so these are surgical operations on one
# checkpoint or one discipline at a time.
#
# THE IMPORTANT SEMANTIC: editing a library does NOT change audits already
# materialised from it. Every audit snapshots its checkpoints at creation
# (`AuditCheckpointResponse` carries its own copy of the question, criticality
# and requirement type), precisely so a wording change in November cannot
# restate what an auditor assessed in March. Edits reach the NEXT audit.
# ─────────────────────────────────────────────────────────────────────

# Fields a caller may set on a library checkpoint, and how each is coerced.
_CP_TEXT_FIELDS = ("question", "guidance", "requirement_reference", "standard")
_CP_BOOL_FIELDS = ("requires_photo_on_fail", "auto_trigger_capa_on_fail")
_CRITICALITIES = ("critical", "major", "minor", "informational")


async def _load_library(db: AsyncSession, industry_code: str) -> AuditCheckpointLibrary:
    lib = (
        await db.execute(
            select(AuditCheckpointLibrary).where(
                AuditCheckpointLibrary.industryCode == industry_code
            )
        )
    ).scalar_one_or_none()
    if lib is None:
        raise ValueError(f"Checkpoint library '{industry_code}' not found")
    return lib


def _bump_version(version: str | None) -> str:
    """`2026.1` -> `2026.2`. The version is what tells an auditor whether two
    audits were run against the same criteria, so every edit moves it."""
    v = (version or "").strip() or "2026.1"
    head, _, tail = v.rpartition(".")
    if head and tail.isdigit():
        return f"{head}.{int(tail) + 1}"
    return f"{v}.1"


def _apply_cp_fields(cp: dict[str, Any], patch: dict[str, Any]) -> None:
    """Merge a validated patch into a library checkpoint, in place."""
    for f in _CP_TEXT_FIELDS:
        if f in patch:
            cp[f] = (patch[f] or "").strip()
    for f in _CP_BOOL_FIELDS:
        if f in patch:
            cp[f] = bool(patch[f])
    if "criticality" in patch:
        crit = (patch["criticality"] or "").strip().lower()
        if crit not in _CRITICALITIES:
            raise ValueError(f"criticality must be one of {', '.join(_CRITICALITIES)}")
        cp["criticality"] = crit
        # Severity drives the CAPA the checkpoint raises. Left unsynced, a
        # checkpoint downgraded to minor would keep spawning critical CAPAs.
        cp["capa_severity_if_triggered"] = (
            "critical" if crit == "critical" else "major" if crit == "major" else "minor"
        )
    if "requirement_type" in patch:
        raw = patch["requirement_type"]
        if raw in (None, ""):
            cp["requirement_type"] = None
        else:
            code = page_grading.normalise_requirement_type(raw)
            if code is None:
                raise ValueError(
                    "requirement_type must be STATUTORY_REGULATORY or INTERNAL_REQUIREMENT"
                )
            cp["requirement_type"] = code
    if "linked_safeops_module" in patch:
        cp["linked_safeops_module"] = patch["linked_safeops_module"] or None


def _find_cp(cats: list[dict], code: str) -> tuple[dict, dict, int] | None:
    for cat in cats:
        for i, cp in enumerate(cat.get("checkpoints") or []):
            if cp.get("code") == code:
                return cat, cp, i
    return None


def _recount(lib: AuditCheckpointLibrary, cats: list[dict]) -> int:
    total = sum(len(c.get("checkpoints") or []) for c in cats)
    # Reassigned rather than mutated in place: `categories` is a JSON column and
    # SQLAlchemy does not track in-place mutation of a plain list/dict, so an
    # edit that only mutated would flush nothing and silently do nothing.
    lib.categories = cats
    lib.checkpointCount = total
    lib.version = _bump_version(lib.version)
    return total


async def update_library_checkpoint(
    db: AsyncSession, *, industry_code: str, code: str, patch: dict[str, Any]
) -> dict[str, Any]:
    """Edit one checkpoint in place. Only the supplied fields change."""
    lib = await _load_library(db, industry_code)
    cats = [dict(c) for c in (lib.categories or [])]
    for c in cats:
        c["checkpoints"] = [dict(cp) for cp in (c.get("checkpoints") or [])]

    found = _find_cp(cats, code)
    if found is None:
        raise ValueError(f"Checkpoint '{code}' is not in this library")
    cat, cp, idx = found

    if "question" in patch and len((patch["question"] or "").strip()) < 4:
        raise ValueError("The question must be at least 4 characters")
    _apply_cp_fields(cp, patch)

    # Moving a checkpoint between disciplines is an edit, not a delete-recreate:
    # the code is what links it to history, so it has to survive the move.
    target = patch.get("category_code")
    if target and target != cat.get("category_code"):
        dest = next((c for c in cats if c.get("category_code") == target), None)
        if dest is None:
            raise ValueError(f"Discipline '{target}' is not in this library")
        cat["checkpoints"].pop(idx)
        dest["checkpoints"].append(cp)

    _recount(lib, cats)
    await db.flush()
    return {"ok": True, "code": code, "version": lib.version, "checkpoint": cp}


async def add_library_checkpoint(
    db: AsyncSession, *, industry_code: str, discipline_code: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Append a checkpoint to a discipline, minting its code if none is given."""
    lib = await _load_library(db, industry_code)
    cats = [dict(c) for c in (lib.categories or [])]
    for c in cats:
        c["checkpoints"] = [dict(cp) for cp in (c.get("checkpoints") or [])]

    cat = next((c for c in cats if c.get("category_code") == discipline_code), None)
    if cat is None:
        raise ValueError(f"Discipline '{discipline_code}' is not in this library")

    question = (data.get("question") or "").strip()
    if len(question) < 4:
        raise ValueError("The question must be at least 4 characters")

    code = (data.get("code") or "").strip() or _next_cp_code(cats, cat)
    if _find_cp(cats, code) is not None:
        raise ValueError(f"Checkpoint code '{code}' is already used in this library")

    crit = (data.get("criticality") or "major").strip().lower()
    if crit not in _CRITICALITIES:
        raise ValueError(f"criticality must be one of {', '.join(_CRITICALITIES)}")

    cp: dict[str, Any] = {
        "code": code,
        "question": question,
        "guidance": (data.get("guidance") or "").strip(),
        "requirement_reference": (data.get("requirement_reference") or "").strip(),
        "standard": (data.get("standard") or "").strip() or "Page Industries Internal Audit",
        "criticality": crit,
        "response_type": "page_grading",
        "requires_photo_on_fail": bool(
            data.get("requires_photo_on_fail", crit in ("critical", "major"))
        ),
        "auto_trigger_capa_on_fail": bool(
            data.get("auto_trigger_capa_on_fail", crit == "critical")
        ),
        "capa_severity_if_triggered": (
            "critical" if crit == "critical" else "major" if crit == "major" else "minor"
        ),
        "linked_safeops_module": data.get("linked_safeops_module") or None,
        "requirement_type": page_grading.normalise_requirement_type(
            data.get("requirement_type")
        ),
    }

    # ── A streamed library has no unstreamed checkpoints ──────────────────
    #
    # On a department library every line belongs to the IMS sheet or the EnMS
    # one, and a report is a filter on that. A checkpoint authored here without
    # a stream would materialise into every future audit and appear in NEITHER
    # report — present in the register, absent from both documents, which is the
    # hardest kind of gap to notice. So the stream is required as soon as the
    # discipline has one, and an unknown token is rejected rather than dropped.
    _dcat_streams = [
        s for s in (cat.get("streams") or [])
        if isinstance(s, str) and s.strip().upper() in STREAMS
    ] or sorted({
        (c.get("stream") or "").upper()
        for c in (cat.get("checkpoints") or [])
        if (c.get("stream") or "").upper() in STREAMS
    })
    if _dcat_streams:
        raw = (data.get("stream") or "").strip().upper()
        if not raw:
            raise ValueError(
                f"'{cat.get('category_name') or discipline_code}' is audited against "
                f"{len(_dcat_streams)} separate reports — say which one this checkpoint "
                f"belongs to ({', '.join(STREAM_META[s]['label'] for s in _dcat_streams)})."
            )
        if raw not in _dcat_streams:
            raise ValueError(
                f"Unknown stream '{data.get('stream')}' — expected one of "
                + ", ".join(_dcat_streams)
            )
        cp["stream"] = raw
        # No replication or pair key. A hand-authored line has no counterpart on
        # the other sheet and no Sl No shared with another department, and
        # inventing one would make the replicate action copy a verdict onto a
        # checkpoint that is not the same question.
        cp["replication_key"] = None
        cp["pair_key"] = None
        cp["standard_clauses"] = data.get("standard_clauses") or []

    cat["checkpoints"].append(cp)
    total = _recount(lib, cats)
    await db.flush()
    return {"ok": True, "checkpoint": cp, "version": lib.version, "checkpointCount": total}


def _next_cp_code(cats: list[dict], cat: dict) -> str:
    """`PI-HR-041` after `PI-HR-040`. Derived from the discipline's existing
    codes so a hand-added checkpoint is indistinguishable from a seeded one;
    falls back to the discipline code when the set is empty."""
    codes = [cp.get("code", "") for cp in cat.get("checkpoints") or []]
    prefix, highest = None, 0
    for c in codes:
        m = re.match(r"^(.*?)(\d+)$", c or "")
        if m:
            prefix = prefix or m.group(1)
            highest = max(highest, int(m.group(2)))
    if prefix is None:
        prefix = f"{cat.get('category_code', 'CP')}-"
    width = 3
    while True:
        candidate = f"{prefix}{highest + 1:0{width}d}"
        if _find_cp(cats, candidate) is None:
            return candidate
        highest += 1


async def delete_library_checkpoint(
    db: AsyncSession, *, industry_code: str, code: str
) -> dict[str, Any]:
    """Remove a checkpoint from the library.

    Safe by construction: audits already materialised keep their own snapshot
    rows, so removing a line here retires it from FUTURE audits and changes no
    record of a past one.
    """
    lib = await _load_library(db, industry_code)
    cats = [dict(c) for c in (lib.categories or [])]
    for c in cats:
        c["checkpoints"] = [dict(cp) for cp in (c.get("checkpoints") or [])]

    found = _find_cp(cats, code)
    if found is None:
        raise ValueError(f"Checkpoint '{code}' is not in this library")
    cat, _cp, idx = found
    if len(cat["checkpoints"]) == 1:
        raise ValueError(
            f"'{cat.get('category_name') or cat.get('category_code')}' would be left with no "
            "checkpoints — remove the discipline instead."
        )
    cat["checkpoints"].pop(idx)
    total = _recount(lib, cats)
    await db.flush()
    return {"ok": True, "code": code, "version": lib.version, "checkpointCount": total}


async def upsert_library_discipline(
    db: AsyncSession, *, industry_code: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Add a discipline, or rename / recolour an existing one.

    `category_code` is the identity and is never rewritten by this call: it is
    what every materialised checkpoint row, every discipline-scoped allocation
    and every programme coverage join is keyed on. The display name is free to
    change; the code is not.
    """
    lib = await _load_library(db, industry_code)
    cats = [dict(c) for c in (lib.categories or [])]
    for c in cats:
        c["checkpoints"] = [dict(cp) for cp in (c.get("checkpoints") or [])]

    code = (data.get("category_code") or "").strip().upper()
    if not code:
        raise ValueError("A discipline needs a category_code")
    name = (data.get("category_name") or "").strip()

    existing = next((c for c in cats if c.get("category_code") == code), None)
    if existing is not None:
        if name:
            existing["category_name"] = name
        if data.get("category_color"):
            existing["category_color"] = data["category_color"]
        if data.get("category_icon"):
            existing["category_icon"] = data["category_icon"]
        created = False
    else:
        if len(name) < 2:
            raise ValueError("A new discipline needs a name")
        cats.append({
            "category_code": code,
            "category_name": name,
            "category_color": data.get("category_color") or "#64748B",
            "category_icon": data.get("category_icon") or "list",
            "sequence": len(cats) + 1,
            "checkpoints": [],
        })
        created = True

    _recount(lib, cats)
    await db.flush()
    return {"ok": True, "created": created, "categoryCode": code, "version": lib.version}


async def delete_library_discipline(
    db: AsyncSession, *, industry_code: str, discipline_code: str
) -> dict[str, Any]:
    """Remove a discipline and every checkpoint in it. Refuses to empty the
    library — a checklist with no disciplines cannot materialise an audit, and
    the failure would surface at scheduling rather than here."""
    lib = await _load_library(db, industry_code)
    cats = [dict(c) for c in (lib.categories or [])]
    if len(cats) <= 1:
        raise ValueError("A library must keep at least one discipline")
    target = next((c for c in cats if c.get("category_code") == discipline_code), None)
    if target is None:
        raise ValueError(f"Discipline '{discipline_code}' is not in this library")
    removed = len(target.get("checkpoints") or [])
    cats = [c for c in cats if c.get("category_code") != discipline_code]
    total = _recount(lib, cats)
    await db.flush()
    return {
        "ok": True, "categoryCode": discipline_code, "removedCheckpoints": removed,
        "version": lib.version, "checkpointCount": total,
    }


async def list_templates(db: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(AuditTemplate).where(AuditTemplate.isActive.is_(True)).order_by(AuditTemplate.name)
        )
    ).scalars().all()
    return [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "auditType": t.auditType,
            "baseIndustry": t.baseIndustry,
            "checkpointConfiguration": t.checkpointConfiguration,
            "version": t.version,
        }
        for t in rows
    ]


# ─────────────────────────────────────────────────────────────────────
# List + dashboards
# ─────────────────────────────────────────────────────────────────────


async def _supplier_blocks(
    db: AsyncSession, audit_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """auditId -> a compact supplier block, in two queries for the whole page.

    Resolved through `services/vendors.py`; the audit engine never touches the
    vendor model. Returns only what a register row renders — the full block
    (posture drift, contact, response channel) belongs to the detail screen.
    """
    if not audit_ids:
        return {}
    from app.models.cams_completion import SupplierAuditLink
    from app.services import vendors as vendor_svc

    links = (
        await db.execute(
            select(SupplierAuditLink).where(
                SupplierAuditLink.engagementKind == "AUDIT",
                SupplierAuditLink.engagementId.in_(audit_ids),
            )
        )
    ).scalars().all()
    if not links:
        return {}

    vendors = await vendor_svc.get_vendors(db, [ln.vendorProfileId for ln in links])
    out: dict[str, dict[str, Any]] = {}
    for ln in links:
        v = vendors.get(ln.vendorProfileId)
        out[ln.engagementId] = {
            "vendorProfileId": ln.vendorProfileId,
            "vendorCode": v.vendorCode if v else None,
            # A deleted or missing vendor must still render as a supplier audit
            # rather than silently reverting to own-facility formatting.
            "legalName": v.legalName if v else "Unknown vendor",
            "criticality": v.criticality if v else ln.criticalityAtScheduling,
            "tier": v.tier if v else ln.tierAtScheduling,
            "criticalityAtScheduling": ln.criticalityAtScheduling,
            "vendorSiteRef": ln.vendorSiteRef,
            "riskPostureChanged": bool(v and v.criticality != ln.criticalityAtScheduling),
        }
    return out


def audit_party_ids(audit) -> list[str]:
    """Everyone who is a party to one engagement — audit team + audited party.

    Single source of truth for "is this person on this audit", shared by the
    router's OWN_RECORDS record context and the register's party filter below,
    so the list can never show a row the detail endpoint then denies.
    Tolerates legacy flat `coAuditors` / `auditees` (bare id strings)."""
    ids = [audit.leadAuditorUserId, audit.createdByUserId, audit.plantManagerUserId]
    for c in (audit.coAuditors or []):
        ids.append(c.get("userId") if isinstance(c, dict) else c)
    for a in (audit.auditees or []):
        ids.append(a.get("userId") if isinstance(a, dict) else a)
    return list(dict.fromkeys(i for i in ids if i))


async def list_audits(
    db: AsyncSession,
    *,
    accessible_plants: list[str] | None,
    subject_type: str | None = None,
    party_user_id: str | None = None,
) -> list[dict[str, Any]]:
    # Newest-created first — platform-wide register convention. `scheduledDate`
    # is a plan field, so a just-scheduled audit for an earlier date used to
    # land mid-list instead of on top.
    stmt = select(ComplianceAudit).order_by(
        ComplianceAudit.createdAt.desc(), ComplianceAudit.id.desc()
    )
    if accessible_plants is not None:
        stmt = stmt.where(ComplianceAudit.plantId.in_(accessible_plants))
    rows = (await db.execute(stmt)).scalars().all()

    # Own-records readers (the auditee-class roles) see only engagements they
    # are actually party to. The plant scope above is deliberately coarse for
    # them — get_accessible_plants_for() widens an OWN_RECORDS grant to the
    # user's plant set — so without this the register listed every audit at the
    # plant and each one 403'd (rendered as a 404) when opened.
    if party_user_id is not None:
        rows = [a for a in rows if party_user_id in audit_party_ids(a)]

    suppliers = await _supplier_blocks(db, [a.id for a in rows])

    # Filtering happens AFTER resolution because `subjectType` is derived from
    # the link, not stored — there is no column to filter in SQL. The plant
    # scope above has already bounded the row count.
    want = (subject_type or "").upper()
    if want == "VENDOR":
        rows = [a for a in rows if a.id in suppliers]
    elif want in ("OWN_SITE", "OWN_FACILITY"):
        rows = [a for a in rows if a.id not in suppliers]

    return [_audit_to_dict(a, supplier=suppliers.get(a.id)) for a in rows]


async def programme_dashboard(
    db: AsyncSession,
    *,
    accessible_plants: list[str] | None,
    party_user_id: str | None = None,
) -> dict[str, Any]:
    stmt = select(ComplianceAudit)
    if accessible_plants is not None:
        stmt = stmt.where(ComplianceAudit.plantId.in_(accessible_plants))
    audits = (await db.execute(stmt)).scalars().all()
    # Same own-records narrowing as list_audits — otherwise the KPI tiles count
    # audits that are absent from the table right below them.
    if party_user_id is not None:
        audits = [a for a in audits if party_user_id in audit_party_ids(a)]

    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    compliance_values: list[float] = []
    total_open_capa = 0
    total_critical = 0
    open_count = 0
    closed_count = 0
    next_scheduled: dict[str, Any] | None = None
    next_dt: datetime | None = None
    now = _naive(_utcnow())

    for a in audits:
        by_status[a.status] = by_status.get(a.status, 0) + 1
        by_type[a.auditType] = by_type.get(a.auditType, 0) + 1
        if a.overallCompliancePct is not None:
            compliance_values.append(a.overallCompliancePct)
        total_open_capa += a.openCapaCount or 0
        total_critical += a.criticalFailureCount or 0
        if a.status == "closed":
            closed_count += 1
        else:
            open_count += 1
        sched = _naive(a.scheduledDate)
        if a.status == "scheduled" and sched and sched >= now:
            if next_dt is None or sched < next_dt:
                next_dt = sched
                next_scheduled = {
                    "id": a.id,
                    "auditNumber": a.auditNumber,
                    "title": a.title,
                    "auditType": a.auditType,
                    "scheduledDate": _iso(a.scheduledDate),
                }

    avg_compliance = round(sum(compliance_values) / len(compliance_values), 1) if compliance_values else None

    return {
        "total": len(audits),
        "open": open_count,
        "closed": closed_count,
        "averageCompliancePct": avg_compliance,
        "openCapas": total_open_capa,
        "criticalFindings": total_critical,
        "byStatus": by_status,
        "byType": by_type,
        "nextScheduled": next_scheduled,
    }


async def _load_audit(
    db: AsyncSession, audit_id: str, *, with_responses: bool = False, with_interactions: bool = False
) -> ComplianceAudit | None:
    stmt = select(ComplianceAudit).where(ComplianceAudit.id == audit_id)
    if with_interactions:
        stmt = stmt.options(selectinload(ComplianceAudit.responses).selectinload(AuditCheckpointResponse.interactions))
    elif with_responses:
        stmt = stmt.options(selectinload(ComplianceAudit.responses))
    return (await db.execute(stmt)).scalar_one_or_none()


def _is_terminal(r: AuditCheckpointResponse) -> bool:
    """Terminal-for-finalization. PASSED is terminal only when the verdict
    agrees (defends against a workflowState↔assessmentStatus desync). Tolerant
    of legacy rows: an assessed pass/NA or a legacy accepted response counts."""
    if r.workflowState in ("RESOLVED", "ACCEPTED_WITH_CAPA", "FINALIZED"):
        return True
    if r.workflowState == "PASSED" and r.assessmentStatus in ("PASS", "NA", "NOT_ASSESSED"):
        return True
    if r.workflowState == "OPEN" and r.assessmentStatus in ("PASS", "NA"):
        return True
    if r.overallStatus == "response_accepted":
        return True
    return False


def _finalizability(audit: ComplianceAudit, *, stream: str | None = None) -> dict[str, Any]:
    """Whether every checkpoint is terminal; lists blockers otherwise.

    `stream` narrows it to one report's checkpoints. A department audit issues
    an IMS report and an EnMS report as separate documents, and the IMS one is
    complete when every IMS checkpoint is resolved — holding it back because an
    EnMS finding is still with its auditee would make the two documents
    hostage to each other for no reason a reader could discover.
    """
    scoped = [
        r for r in audit.responses if stream is None or r.streamCode == stream
    ]
    blockers = [
        {
            "checkpointCode": r.checkpointCode,
            "categoryName": r.categoryName,
            "workflowState": r.workflowState,
            "assessmentStatus": r.assessmentStatus,
        }
        for r in sorted(scoped, key=lambda x: (x.categoryId, x.sequence))
        if not _is_terminal(r)
    ]
    total = len(scoped)
    # An audit can only be finalized after it has been submitted (the conduct →
    # submit → resolve → finalize lifecycle); an all-pass in-progress audit is
    # not yet finalizable — it must be submitted first.
    submitted = audit.status in ("submitted_pending_response", "response_in_progress", "under_review", "closed")
    return {
        "finalizable": submitted and total > 0 and not blockers,
        "submitted": submitted,
        "total": total,
        "terminal": total - len(blockers),
        "blockerCount": len(blockers),
        "blockers": blockers,
    }


# ── Scale helpers (1500-checkpoint support) ──────────────────────────────────
# These compute via grouped/aggregate queries so the detail page, conduct
# navigator, and finalize gate never materialize the full response set.


def _terminal_clause():
    """SQL twin of `_is_terminal` — true for a checkpoint that is terminal for
    finalization. Kept in lockstep with `_is_terminal` above."""
    R = AuditCheckpointResponse
    return or_(
        R.workflowState.in_(["RESOLVED", "ACCEPTED_WITH_CAPA", "FINALIZED"]),
        and_(R.workflowState == "PASSED", R.assessmentStatus.in_(["PASS", "NA", "NOT_ASSESSED"])),
        and_(R.workflowState == "OPEN", R.assessmentStatus.in_(["PASS", "NA"])),
        R.overallStatus == "response_accepted",
    )


async def _finalizability_db(db: AsyncSession, audit: ComplianceAudit) -> dict[str, Any]:
    """Count-based finalizability — same shape as `_finalizability` but never
    loads the response rows (only a count + the first 50 blockers)."""
    R = AuditCheckpointResponse
    aid = audit.id
    total = (await db.execute(select(func.count(R.id)).where(R.auditId == aid))).scalar_one() or 0
    nonterm = (
        await db.execute(select(func.count(R.id)).where(R.auditId == aid, not_(_terminal_clause())))
    ).scalar_one() or 0
    blocker_rows = (
        await db.execute(
            select(R.checkpointCode, R.categoryName, R.workflowState, R.assessmentStatus)
            .where(R.auditId == aid, not_(_terminal_clause()))
            .order_by(R.sequence)
            .limit(50)
        )
    ).all()
    submitted = audit.status in ("submitted_pending_response", "response_in_progress", "under_review", "closed")
    return {
        "finalizable": submitted and total > 0 and nonterm == 0,
        "submitted": submitted,
        "total": total,
        "terminal": total - nonterm,
        "blockerCount": nonterm,
        "blockers": [
            {"checkpointCode": b.checkpointCode, "categoryName": b.categoryName,
             "workflowState": b.workflowState, "assessmentStatus": b.assessmentStatus}
            for b in blocker_rows
        ],
    }


async def _discipline_rollup(db: AsyncSession, audit_id: str) -> list[dict[str, Any]]:
    """Per-discipline counts via ONE grouped query (uses assessmentStatus, the
    first-class verdict column). Drives the conduct navigator + detail RAG
    without loading any checkpoint rows."""
    R = AuditCheckpointResponse
    A = R.assessmentStatus
    rows = (
        await db.execute(
            select(
                R.categoryId, R.categoryName, R.categoryColor,
                func.count(R.id).label("total"),
                func.count(R.id).filter(A != "NOT_ASSESSED").label("answered"),
                func.count(R.id).filter(A == "PASS").label("passed"),
                func.count(R.id).filter(A == "PARTIAL").label("partial"),
                func.count(R.id).filter(A == "FAIL").label("failed"),
                func.count(R.id).filter(A == "NA").label("na"),
                func.count(R.id).filter(and_(A == "FAIL", R.criticality == "critical")).label("criticalFailed"),
                func.count(R.id).filter(and_(A == "FAIL", R.criticality == "major")).label("majorFailed"),
                func.count(R.id).filter(
                    and_(A == "FAIL", R.criticality.notin_(["critical", "major"]))
                ).label("minorFailed"),
                # Page grading — summed in SQL for the same reason the counts
                # are: the navigator repaints on every save of a 1,500-row audit
                # and must never pull rows to add up points.
                func.coalesce(func.sum(R.scoreAllotted), 0).label("scoreAllotted"),
                func.coalesce(func.sum(R.scoreObtained), 0).label("scoreObtained"),
                func.count(R.id).filter(
                    and_(
                        A.in_(["FAIL", "PARTIAL"]),
                        R.complianceStatus.in_(sorted(page_grading.REPEAT_STATUSES)),
                    )
                ).label("repeatFindings"),
                func.count(R.id).filter(
                    and_(A.in_(["FAIL", "PARTIAL"]), R.requirementType == page_grading.REQ_STATUTORY)
                ).label("statutoryFindings"),
            )
            .where(R.auditId == audit_id)
            .group_by(R.categoryId, R.categoryName, R.categoryColor)
        )
    ).all()
    out = [
        {
            "categoryId": r.categoryId, "categoryName": r.categoryName,
            "categoryColor": r.categoryColor or "", "total": r.total,
            "answered": r.answered, "passed": r.passed, "partial": r.partial,
            "failed": r.failed, "na": r.na, "criticalFailed": r.criticalFailed,
            "majorFailed": r.majorFailed, "minorFailed": r.minorFailed,
            "scoreAllotted": int(r.scoreAllotted or 0),
            "scoreObtained": int(r.scoreObtained or 0),
            "scorePct": page_grading.compute_points_score(
                obtained=int(r.scoreObtained or 0), allotted=int(r.scoreAllotted or 0)
            ),
            "repeatFindings": r.repeatFindings,
            "statutoryFindings": r.statutoryFindings,
        }
        for r in rows
    ]
    out.sort(key=lambda c: c["categoryName"])
    return out


def _score_from_rollup(rollup: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the AuditScore shape (same as `_compute_score`) from the discipline
    rollup — the read-path score with no full row load."""
    passed = sum(c["passed"] for c in rollup)
    partial = sum(c["partial"] for c in rollup)
    failed = sum(c["failed"] for c in rollup)
    na = sum(c["na"] for c in rollup)
    total = sum(c["total"] for c in rollup)
    crit = sum(c["criticalFailed"] for c in rollup)
    major = sum(c["majorFailed"] for c in rollup)
    minor = sum(c["minorFailed"] for c in rollup)
    answered = passed + partial + failed + na
    obtained_total = sum(c["scoreObtained"] for c in rollup)
    allotted_total = sum(c["scoreAllotted"] for c in rollup)
    # Points, not the pass-ratio — see `_compute_score`. The two paths must
    # agree exactly, because this one serves the register and that one is what
    # gets frozen into the report snapshot.
    overall = page_grading.compute_points_score(
        obtained=obtained_total, allotted=allotted_total
    )
    category_scores = []
    for c in rollup:
        category_scores.append({
            "category_id": c["categoryId"], "category_name": c["categoryName"],
            "total": c["total"], "passed": c["passed"], "partial": c["partial"],
            "failed": c["failed"], "na": c["na"],
            "score_obtained": c["scoreObtained"], "score_allotted": c["scoreAllotted"],
            "score_pct": c["scorePct"],
        })
    return {
        "total_checkpoints": total, "answered": answered, "passed": passed,
        "partially_passed": partial, "failed": failed, "not_applicable": na,
        "overall_score_pct": overall,
        "category_scores": sorted(category_scores, key=lambda c: c["category_name"]),
        "critical_failures": crit, "major_failures": major, "minor_failures": minor,
        "audit_passed": crit == 0 and overall >= MINIMUM_PASS_SCORE,
        "score_obtained": obtained_total,
        "score_allotted": allotted_total,
        "score_band": page_grading.band(overall, MINIMUM_PASS_SCORE),
        "repeat_findings": sum(c["repeatFindings"] for c in rollup),
        "statutory_findings": sum(c["statutoryFindings"] for c in rollup),
    }


async def _allocation_summary(db: AsyncSession, audit_id: str) -> dict[str, int]:
    """assigned / unassigned counts via aggregate (assigned = an effective owner
    by explicit allocation OR discipline routing)."""
    R = AuditCheckpointResponse
    total = (await db.execute(select(func.count(R.id)).where(R.auditId == audit_id))).scalar_one() or 0
    assigned = (
        await db.execute(
            select(func.count(R.id)).where(
                R.auditId == audit_id,
                or_(R.assignedOwnerId.isnot(None), R.routedToUserId.isnot(None)),
            )
        )
    ).scalar_one() or 0
    return {"assigned": assigned, "unassigned": total - assigned, "total": total}


def _review_clause():
    """Rows the detail page's review surface needs: any adverse verdict
    (fail/partial = a finding) OR any non-quiescent workflow state."""
    R = AuditCheckpointResponse
    return or_(
        R.assessmentStatus.in_(["FAIL", "PARTIAL"]),
        R.workflowState.notin_(["OPEN", "PASSED"]),
    )


# Cap the detail page's embedded review set; beyond this the conduct worklist
# (paginated, filterable) is the way to reach every finding.
_REVIEW_CAP = 500


def _coauditor_ids(co_auditors: list | None) -> list[str]:
    """Extract user ids from coAuditors, tolerating BOTH the legacy flat shape
    (list[str]) and the structured shape (list[{userId, disciplineIds}], Phase 3)."""
    out: list[str] = []
    for c in co_auditors or []:
        if isinstance(c, dict):
            uid = c.get("userId")
            if uid:
                out.append(uid)
        elif c:
            out.append(c)
    return out


def _progress_from_rollup(rollup: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the `progress` block (same shape as `_live_progress`) from the
    discipline rollup, so the detail page needs no full row load."""
    total = sum(c["total"] for c in rollup)
    answered = sum(c["answered"] for c in rollup)
    return {
        "total": total,
        "answered": answered,
        "completionPct": round(answered / total * 100, 1) if total else 0,
        "categories": [
            {"categoryId": c["categoryId"], "categoryName": c["categoryName"],
             "categoryColor": c["categoryColor"], "total": c["total"],
             "answered": c["answered"], "failed": c["failed"], "partial": c["partial"]}
            for c in rollup
        ],
    }


# value filter token -> assessmentStatus column value (list_checkpoints).
_VALUE_TO_ASSESS = {"pass": "PASS", "partial": "PARTIAL", "fail": "FAIL", "na": "NA", "unanswered": "NOT_ASSESSED"}


async def list_checkpoints(
    db: AsyncSession, *, audit_id: str, discipline_id: str | None = None,
    workflow_state: str | None = None, assessment_status: str | None = None,
    value: str | None = None, criticality: str | None = None, q: str | None = None,
    grade: str | None = None, compliance_status: str | None = None,
    risk_grade: str | None = None, requirement_type: str | None = None,
    assigned_auditor_id: str | None = None, stream: str | None = None,
    cursor: str | None = None, limit: int = 50,
) -> dict[str, Any]:
    """Paginated, filterable checkpoint slice for an audit. Cursor =
    "{sequence}:{id}", ordered by (sequence, id) for stable paging. Never loads
    interactions (fetch those per-row on demand)."""
    R = AuditCheckpointResponse
    conds = [R.auditId == audit_id]
    if discipline_id:
        conds.append(R.categoryId == discipline_id)
    if assigned_auditor_id:
        conds.append(R.assignedAuditorId == assigned_auditor_id)
    if workflow_state:
        conds.append(R.workflowState == workflow_state)
    if assessment_status:
        conds.append(R.assessmentStatus == assessment_status)
    if value:
        mapped = _VALUE_TO_ASSESS.get(value)
        if mapped is None:
            raise ValueError(f"Invalid value filter '{value}'")
        conds.append(R.assessmentStatus == mapped)
    if criticality:
        conds.append(R.criticality == criticality)
    if stream:
        code = stream.strip().upper()
        if code not in STREAMS:
            raise ValueError(
                f"Invalid stream filter '{stream}' — expected one of {', '.join(STREAMS)}"
            )
        conds.append(R.streamCode == code)
    # Page grading filters. Each raises rather than silently returning
    # everything on an unknown token — a filter that quietly does nothing is
    # how an auditor concludes a discipline is clean when it isn't.
    if grade:
        code = page_grading.normalise_grade(grade)
        if code is None:
            raise ValueError(f"Invalid grade filter '{grade}'")
        conds.append(R.gradeAwarded == code)
    if compliance_status:
        code = page_grading.normalise_status(compliance_status)
        if code is None:
            raise ValueError(f"Invalid status filter '{compliance_status}'")
        conds.append(R.complianceStatus == code)
    if risk_grade:
        code = page_grading.normalise_risk_grade(risk_grade)
        if code is None:
            raise ValueError(f"Invalid risk grade filter '{risk_grade}'")
        conds.append(R.riskGrade == code)
    if requirement_type:
        code = page_grading.normalise_requirement_type(requirement_type)
        if code is None:
            raise ValueError(f"Invalid requirement type filter '{requirement_type}'")
        conds.append(R.requirementType == code)
    if q:
        like = f"%{q.strip()}%"
        conds.append(or_(R.checkpointCode.ilike(like), R.checkpointQuestion.ilike(like)))

    total = (await db.execute(select(func.count(R.id)).where(*conds))).scalar_one() or 0

    paged = select(R).where(*conds).order_by(R.sequence, R.id)
    if cursor:
        try:
            c_seq_s, c_id = cursor.split(":", 1)
            c_seq = int(c_seq_s)
        except (ValueError, AttributeError) as e:
            raise ValueError("Invalid cursor") from e
        paged = paged.where(or_(R.sequence > c_seq, and_(R.sequence == c_seq, R.id > c_id)))
    limit = max(1, min(limit, 200))
    rows = (await db.execute(paged.limit(limit + 1))).scalars().all()
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = f"{items[-1].sequence}:{items[-1].id}" if has_more and items else None
    return {
        "items": [_response_to_dict(r) for r in items],
        "nextCursor": next_cursor,
        "total": total,
        "returned": len(items),
    }


async def get_checkpoint_interactions(db: AsyncSession, *, audit_id: str, checkpoint_id: str) -> dict[str, Any]:
    """The append-only iteration thread for ONE checkpoint (lazy-loaded by the
    detail drill-in / conduct expand)."""
    R = AuditCheckpointResponse
    resp = (
        await db.execute(
            select(R).where(R.id == checkpoint_id, R.auditId == audit_id)
            .options(selectinload(R.interactions))
        )
    ).scalar_one_or_none()
    if resp is None:
        raise ValueError("Checkpoint not found on this audit")
    return {
        "checkpointCode": resp.checkpointCode,
        "interactions": [
            _interaction_to_dict(i) for i in sorted(resp.interactions, key=lambda x: (x.timestamp, x.round))
        ],
    }


async def bulk_save_response(
    db: AsyncSession, *, user: User, audit_id: str, value: str,
    checkpoint_ids: list[str] | None = None, discipline_id: str | None = None,
    only_unanswered: bool = True,
) -> dict[str, Any]:
    """Mark a set of checkpoints (explicit ids OR a whole discipline) as pass/na
    in one call — the "mark discipline compliant" fast path for large audits.

    Safety rails: only `pass`/`na` are allowed; FAIL/PARTIAL rows and in-flight
    findings (workflowState not OPEN/PASSED) are NEVER touched, so a deliberate
    finding/observation can't be clobbered; `only_unanswered` (default) further
    restricts to NOT_ASSESSED rows. Mirrors the pass/na branch of save_response."""
    if value not in ("pass", "na"):
        raise ValueError("Bulk save supports only 'pass' or 'na'")
    audit = await _load_audit(db, audit_id)
    if audit is None:
        raise ValueError("Audit not found")
    if audit.status in ("closed", "cancelled"):
        raise ValueError(f"Audit is {audit.status}; checkpoint responses are locked")

    R = AuditCheckpointResponse
    conds = [R.auditId == audit_id]
    ids = set(checkpoint_ids or [])
    if ids:
        conds.append(R.id.in_(ids))
    elif discipline_id:
        conds.append(R.categoryId == discipline_id)
    else:
        raise ValueError("Provide checkpointIds or disciplineId")
    # Never overwrite a deliberate adverse verdict or an in-flight finding.
    conds.append(~R.assessmentStatus.in_(["FAIL", "PARTIAL"]))
    conds.append(R.workflowState.in_(["OPEN", "PASSED"]))
    if only_unanswered:
        conds.append(R.assessmentStatus == "NOT_ASSESSED")

    rows = (await db.execute(select(R).where(*conds))).scalars().all()
    now = _utcnow()
    for resp in rows:
        merged = dict(resp.auditorResponse or {})
        merged["value"] = value
        merged["responded_at"] = now.isoformat()
        merged["is_saved"] = True
        resp.auditorResponse = merged
        resp.assessmentStatus = _ASSESS_STATUS[value]
        resp.overallStatus = f"answered_{value}"
        resp.answeredAt = now
        resp.workflowState = "PASSED"
        # Bulk-marking is still a grade. Leaving the Page columns null here
        # would drop every bulk-marked checkpoint out of the score denominator,
        # so a discipline marked compliant in one click would read 0%.
        grade = _VALUE_TO_GRADE[value]
        resp.gradeAwarded = grade
        resp.complianceStatus = page_grading.suggest_status(grade)
        resp.scoreAllotted = page_grading.allotted_for_grade(grade)
        resp.scoreObtained = page_grading.suggest_score(grade, resp.complianceStatus)
        resp.riskGrade = None

    if audit.status == "scheduled" and rows:
        audit.status = "in_progress"
        if audit.actualStartAt is None:
            audit.actualStartAt = now
    await db.flush()

    answered = (
        await db.execute(
            select(func.count(R.id)).where(R.auditId == audit_id)
            .where(R.overallStatus.notlike("not_answered"))
        )
    ).scalar_one() or 0
    audit.answeredCheckpoints = answered
    await db.flush()
    return {"ok": True, "updated": len(rows), "answered": answered, "value": value}


# The verdict, and only the verdict. `replicate_response` copies these five
# columns and nothing else — evidence, ownership, the iteration thread and any
# CAPA belong to the department they were raised in, and a photograph of HR's
# training register is not evidence about Admin's.
_REPLICATED_COLUMNS = (
    "gradeAwarded", "complianceStatus", "riskGrade", "scoreAllotted", "scoreObtained",
)


async def replicate_response(
    db: AsyncSession, *, user: User, audit_id: str, checkpoint_code: str,
    target_departments: list[str] | None = None, include_findings: bool = True,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Copy one checkpoint's verdict onto the SAME workbook line in the other
    departments of this audit.

    40 of the 60 IMS lines and all 22 EnMS lines are common to HR, Admin and
    OHC, so a department audit asks the same question three times. Answering it
    three times by hand is both the rework the customer asked us to remove and
    the way the three copies end up disagreeing about a single shared record.

    Matching is on `replicationKey` — the workbook's own Sl No plus the stream —
    never on the question text, which differs by department prefix, and never on
    the checkpoint code, which encodes the department it belongs to.

    Two things this deliberately will NOT do:

      • Touch a row that is already graded, unless `overwrite` is asked for. The
        auditor may have found Admin genuinely different from HR, and silently
        overwriting that is worse than making them click twice.
      • Touch a row whose finding is already in flight with its auditee
        (AWAITING_AUDITEE and onwards). Re-grading underneath a live iteration
        would rewrite the question the auditee is currently answering.
    """
    audit = await _load_audit(db, audit_id)
    if audit is None:
        raise ValueError("Audit not found")
    if audit.status in ("closed", "cancelled"):
        raise ValueError(f"Audit is {audit.status}; checkpoint responses are locked")

    R = AuditCheckpointResponse
    source = (
        await db.execute(
            select(R).where(R.auditId == audit_id, R.checkpointCode == checkpoint_code)
        )
    ).scalar_one_or_none()
    if source is None:
        raise ValueError(f"Checkpoint {checkpoint_code} not found on this audit")
    if not source.replicationKey:
        raise ValueError(
            f"{checkpoint_code} is specific to {source.categoryName} — it has no "
            "counterpart in another department to replicate to."
        )
    if source.assessmentStatus == "NOT_ASSESSED":
        raise ValueError("Grade this checkpoint before replicating it.")

    conds = [
        R.auditId == audit_id,
        R.replicationKey == source.replicationKey,
        R.id != source.id,
        # Never reach into a live iteration — see the docstring.
        R.workflowState.in_(["OPEN", "PASSED"]),
    ]
    if target_departments:
        conds.append(R.categoryId.in_(list(target_departments)))
    candidates = (await db.execute(select(R).where(*conds))).scalars().all()

    now = _utcnow()
    updated: list[str] = []
    skipped: list[dict[str, str]] = []
    for target in candidates:
        if target.assessmentStatus != "NOT_ASSESSED" and not overwrite:
            skipped.append({
                "checkpointCode": target.checkpointCode,
                "department": target.categoryName,
                "reason": "already graded",
            })
            continue
        for col in _REPLICATED_COLUMNS:
            setattr(target, col, getattr(source, col))
        merged = dict(target.auditorResponse or {})
        merged["value"] = (source.auditorResponse or {}).get("value")
        # The auditor's comment travels with the verdict by default: a
        # Non-Conformance replicated without the sentence explaining it is a
        # finding nobody can act on, and `submit_audit` would reject the audit
        # for exactly that. It is optional because a comment naming a specific
        # HR record does not belong on the Admin row.
        if include_findings:
            merged["text_observation"] = (source.auditorResponse or {}).get("text_observation")
            target.observation = source.observation
        merged["responded_at"] = now.isoformat()
        merged["is_saved"] = True
        # Replication is an act, and the record has to say so — otherwise a
        # reviewer reading three identical findings cannot tell three
        # observations from one observation copied twice.
        merged["replicated_from"] = source.checkpointCode
        target.auditorResponse = merged
        target.assessmentStatus = source.assessmentStatus
        target.overallStatus = source.overallStatus
        target.answeredAt = now
        target.workflowState = "PASSED" if source.assessmentStatus in ("PASS", "NA") else "OPEN"
        await _log_interaction(
            db, instance=target, audit_id=audit.id, actor_id=user.id,
            actor_role=_actor_role_for(user, audit), action="ASSESSED",
            resulting_state=target.workflowState, round=0,
            comment=(
                f"Replicated from {source.checkpointCode} ({source.categoryName}) — "
                "same checkpoint, another department."
            ),
            at=now,
        )
        updated.append(target.checkpointCode)

    if audit.status == "scheduled" and updated:
        audit.status = "in_progress"
        if audit.actualStartAt is None:
            audit.actualStartAt = now
    await db.flush()

    answered = (
        await db.execute(
            select(func.count(R.id)).where(R.auditId == audit_id)
            .where(R.overallStatus.notlike("not_answered"))
        )
    ).scalar_one() or 0
    audit.answeredCheckpoints = answered
    await db.flush()
    return {
        "ok": True,
        "sourceCheckpointCode": source.checkpointCode,
        "replicationKey": source.replicationKey,
        "updated": len(updated),
        "updatedCheckpointCodes": updated,
        "skipped": skipped,
        "answered": answered,
    }


async def replication_targets(
    db: AsyncSession, *, audit_id: str, checkpoint_code: str
) -> dict[str, Any]:
    """Which departments this checkpoint could be replicated to, and what state
    each is in — so the confirm dialog names what it is about to overwrite
    instead of reporting the damage afterwards."""
    R = AuditCheckpointResponse
    source = (
        await db.execute(
            select(R).where(R.auditId == audit_id, R.checkpointCode == checkpoint_code)
        )
    ).scalar_one_or_none()
    if source is None:
        raise ValueError(f"Checkpoint {checkpoint_code} not found on this audit")
    if not source.replicationKey:
        return {"checkpointCode": checkpoint_code, "replicationKey": None, "targets": []}
    rows = (
        await db.execute(
            select(R).where(
                R.auditId == audit_id,
                R.replicationKey == source.replicationKey,
                R.id != source.id,
            ).order_by(R.categoryName)
        )
    ).scalars().all()
    return {
        "checkpointCode": source.checkpointCode,
        "replicationKey": source.replicationKey,
        "targets": [
            {
                "checkpointCode": r.checkpointCode,
                "departmentId": r.categoryId,
                "departmentName": r.categoryName,
                "assessmentStatus": r.assessmentStatus,
                "conformance": page_grading.tristate_for_status(r.complianceStatus),
                "gradeAwarded": r.gradeAwarded,
                # An in-flight finding is not replaceable at all; a graded row
                # is replaceable but only on an explicit overwrite.
                "locked": r.workflowState not in ("OPEN", "PASSED"),
                "wouldOverwrite": r.assessmentStatus != "NOT_ASSESSED",
            }
            for r in rows
        ],
    }


async def stream_rollup(db: AsyncSession, audit_id: str) -> list[dict[str, Any]]:
    """Per-stream counts and points, via ONE grouped query.

    The department rollup drives the navigator; this drives the two reports and
    the "IMS 41/62 · EnMS 12/22" line the conduct header shows. Returns [] for
    an audit whose checkpoints carry no stream, which is every audit outside the
    department-segregated libraries — the caller then renders nothing rather
    than an empty stream called "null".
    """
    R = AuditCheckpointResponse
    A = R.assessmentStatus
    rows = (
        await db.execute(
            select(
                R.streamCode,
                func.count(R.id).label("total"),
                func.count(R.id).filter(A != "NOT_ASSESSED").label("answered"),
                func.count(R.id).filter(A == "PASS").label("passed"),
                func.count(R.id).filter(A == "PARTIAL").label("partial"),
                func.count(R.id).filter(A == "FAIL").label("failed"),
                func.count(R.id).filter(A == "NA").label("na"),
                func.count(R.id).filter(
                    and_(A == "FAIL", R.criticality == "critical")
                ).label("criticalFailed"),
                func.coalesce(func.sum(R.scoreAllotted), 0).label("scoreAllotted"),
                func.coalesce(func.sum(R.scoreObtained), 0).label("scoreObtained"),
            )
            .where(R.auditId == audit_id, R.streamCode.isnot(None))
            .group_by(R.streamCode)
        )
    ).all()
    by_code = {r.streamCode: r for r in rows}
    out = []
    for code in STREAMS:
        r = by_code.get(code)
        if r is None:
            continue
        out.append({
            **STREAM_META[code],
            "total": r.total, "answered": r.answered, "passed": r.passed,
            "partial": r.partial, "failed": r.failed, "na": r.na,
            "criticalFailed": r.criticalFailed,
            "scoreAllotted": int(r.scoreAllotted or 0),
            "scoreObtained": int(r.scoreObtained or 0),
            "scorePct": page_grading.compute_points_score(
                obtained=int(r.scoreObtained or 0), allotted=int(r.scoreAllotted or 0)
            ),
        })
    return out


async def get_audit(db: AsyncSession, audit_id: str) -> dict[str, Any] | None:
    """Slim detail payload (1500-checkpoint safe). Returns the audit header +
    discipline rollup + a BOUNDED review set (findings / in-flight rows with
    their threads) — NOT the full response array. The conduct worklist + the
    paginated /checkpoints endpoint reach every checkpoint."""
    audit = await _load_audit(db, audit_id)
    if audit is None:
        return None
    d = _audit_to_dict(audit, supplier=(await _supplier_blocks(db, [audit_id])).get(audit_id))

    # The detail screen gets the FULL supplier block (posture drift since
    # scheduling, contact, whether a response channel actually exists) rather
    # than the compact register shape.
    #
    # Belt-and-braces around an OPTIONAL block. `channel_for_engagement` already
    # swallows its own failures, but the audit detail screen must not be
    # reachable-only-if-the-supplier-layer-is-healthy: the frontend turns any
    # error from this endpoint into `notFound()`, so a fault here reads to the
    # user as "this audit does not exist". Losing the supplier panel is
    # recoverable; losing the audit is not.
    if d["subjectType"] == "VENDOR":
        try:
            from app.services import cams_suppliers as _sup

            d["supplierDetail"] = await _sup.supplier_for_engagement(
                db, engagement_kind="AUDIT", engagement_id=audit_id
            )
        except Exception as e:  # noqa: BLE001
            print(f"Supplier block failed for audit {audit_id}: {e}", file=sys.stderr)
            d["supplierDetail"] = None

    rollup = await _discipline_rollup(db, audit_id)
    d["disciplineRollup"] = rollup
    d["progress"] = _progress_from_rollup(rollup)
    d["finalizability"] = await _finalizability_db(db, audit)
    d["allocationSummary"] = await _allocation_summary(db, audit_id)
    # Empty for every audit outside the department-segregated libraries, which
    # is what tells the client there is one report here rather than two.
    d["streamRollup"] = await stream_rollup(db, audit_id)

    # Which conformance vocabulary this audit's checkpoints were materialised
    # with. Read from the ROWS, not from the library: the rows are what the
    # auditor will actually answer, and the library may have been re-authored
    # since. A mixed audit reports FULL — the filter row and the bulk actions
    # must offer the vocabulary that every card can answer in, not one that
    # only some can.
    _modes = {
        page_grading.normalise_conformance_mode(m)
        for m in (
            await db.execute(
                select(AuditCheckpointResponse.conformanceMode)
                .where(AuditCheckpointResponse.auditId == audit_id)
                .distinct()
            )
        ).scalars().all()
    }
    d["conformanceMode"] = (
        page_grading.CONFORMANCE_TRISTATE
        if _modes == {page_grading.CONFORMANCE_TRISTATE}
        else page_grading.CONFORMANCE_FULL
    )

    # Bounded review set: adverse / in-flight rows (the only ones the detail page
    # acts on) WITH their interaction threads. Pass/NA/OPEN rows are reached via
    # the conduct worklist. Capped — beyond the cap the worklist is the path.
    R = AuditCheckpointResponse
    findings = (
        await db.execute(
            select(R).where(R.auditId == audit_id, _review_clause())
            .order_by(R.sequence).limit(_REVIEW_CAP)
            .options(selectinload(R.interactions))
        )
    ).scalars().all()
    d["responses"] = [_response_to_dict(r, include_interactions=True) for r in findings]
    d["responsesTruncated"] = len(findings) >= _REVIEW_CAP

    # A-03 overview enrichment: factory name + profile link, template, standards,
    # owner count — all via light aggregates (no full row load).
    plant = await db.get(Plant, audit.plantId)
    # Never the cuid: this feeds the audit overview header AND the report PDF's
    # "Site" line, and a cuid there reads as a filled-in answer.
    d["plantName"] = plant.name if plant else "Unknown site"
    d["plantCode"] = plant.code if plant else None
    d["factoryProfileId"] = (
        await db.execute(select(FactoryProfile.id).where(FactoryProfile.siteId == audit.plantId))
    ).scalar_one_or_none()
    if audit.templateId:
        tmpl = await db.get(AuditTemplate, audit.templateId)
        d["templateName"] = tmpl.name if tmpl else None
        d["templateVersion"] = tmpl.version if tmpl else None
    else:
        d["templateName"] = None
        d["templateVersion"] = None
    std_rows = (
        await db.execute(select(R.standard).where(R.auditId == audit_id, R.standard != "").distinct())
    ).scalars().all()
    d["standards"] = sorted({(s or "").strip() for s in std_rows if (s or "").strip()})
    owner_rows = (
        await db.execute(
            select(func.coalesce(R.assignedOwnerId, R.routedToUserId)).where(
                R.auditId == audit_id,
                or_(R.assignedOwnerId.isnot(None), R.routedToUserId.isnot(None)),
            ).distinct()
        )
    ).scalars().all()
    owners = {o for o in owner_rows if o}
    d["ownerCount"] = len(owners)

    # Every person named on ANY checkpoint of this audit — owner, routed owner
    # and conducting auditor, across all three columns rather than the
    # `coalesce` above (which keeps only one of owner/routed) and across ALL
    # responses rather than the bounded `findings` slice below.
    #
    # This is what makes the conduct screen able to name a cross-plant assignee.
    # The observed failure: the EMS and EnMS disciplines were assigned to users
    # belonging to a different plant, the screen resolved names only from the
    # plant-scoped directory, and every one of those checkpoints read
    # "Unknown user" — while QMS and OHS, assigned to same-plant users, looked
    # fine. A name must not depend on where the assignee happens to work.
    assignee_rows = (
        await db.execute(
            select(R.assignedOwnerId, R.routedToUserId, R.assignedAuditorId).where(R.auditId == audit_id)
        )
    ).all()

    # Resolve names for every referenced actor (header + the review rows' owners
    # and thread actors) so the meta strip + owner chips never show "—".
    uid_set: set[str] = {audit.leadAuditorUserId, audit.plantManagerUserId}
    uid_set.update(_coauditor_ids(audit.coAuditors))
    uid_set.update((a.get("userId") if isinstance(a, dict) else a) for a in (audit.auditees or []))
    uid_set.update(owners)
    for _owner, _routed, _auditor in assignee_rows:
        uid_set.update((_owner, _routed, _auditor))
    for r in findings:
        uid_set.update((r.assignedOwnerId, r.routedToUserId, r.addedById, r.assignedById))
        for i in r.interactions:
            uid_set.add(i.actorId)
    uid_set = {u for u in uid_set if u}
    d["userNames"] = {}
    if uid_set:
        rows = (await db.execute(select(User.id, User.name).where(User.id.in_(uid_set)))).all()
        d["userNames"] = {uid: nm for uid, nm in rows}

    # Full cast + each member's discipline scope + whether they still hold the
    # permission their seat needs. Discipline names come from the rollup we
    # already computed above, so naming them costs no extra query.
    from app.services import audit_assignment as _assign  # local: avoids an import cycle

    d["team"] = await _assign.audit_team(
        db, audit,
        discipline_names={r["categoryId"]: r["categoryName"] for r in rollup},
    )
    return d


def _live_progress(responses: list[AuditCheckpointResponse]) -> dict[str, Any]:
    total = len(responses)
    answered = 0
    cat_map: dict[str, dict[str, Any]] = {}
    for r in responses:
        cat = cat_map.setdefault(
            r.categoryId,
            {"categoryId": r.categoryId, "categoryName": r.categoryName, "categoryColor": r.categoryColor,
             "total": 0, "answered": 0, "failed": 0, "partial": 0},
        )
        cat["total"] += 1
        val = _norm_value((r.auditorResponse or {}).get("value")) if r.auditorResponse else None
        if val is not None:
            answered += 1
            cat["answered"] += 1
            if val == "fail":
                cat["failed"] += 1
            elif val == "partial":
                cat["partial"] += 1
    return {
        "total": total,
        "answered": answered,
        "completionPct": round(answered / total * 100, 1) if total else 0,
        "categories": sorted(cat_map.values(), key=lambda c: c["categoryName"]),
    }


def _compute_score(audit: ComplianceAudit, responses: list[AuditCheckpointResponse]) -> dict[str, Any]:
    """The write-path score, from loaded rows.

    Scoring is POINTS-based: Σ score obtained / Σ score allotted, where every
    scored checkpoint is allotted 3 and an N/A one is allotted nothing. That is
    the number Page reconcile against their own workbook, and it is not the same
    as the engine's historic `(passed + 0.5·partial) / assessable` pass-ratio —
    a repeat non-compliance scores -1 against an allotment of 3, so a discipline
    can legitimately land below zero. It is reported as-is; clamping it would
    hide exactly the penalty the -1 exists to apply.

    The pass/partial/fail/na counts are still produced. They are what the
    critical-failure gate, the RAG bars and every existing consumer read, and
    they remain the same verdict seen from the other side.
    """
    passed = partial = failed = na = answered = 0
    crit_fail = major_fail = minor_fail = 0
    repeat_findings = statutory_findings = 0
    obtained_total = allotted_total = 0
    cat_scores: dict[str, dict[str, Any]] = {}

    for r in responses:
        val = _norm_value((r.auditorResponse or {}).get("value")) if r.auditorResponse else None
        cat = cat_scores.setdefault(
            r.categoryId,
            {"category_id": r.categoryId, "category_name": r.categoryName, "total": 0,
             "passed": 0, "partial": 0, "failed": 0, "na": 0,
             "score_obtained": 0, "score_allotted": 0},
        )
        cat["total"] += 1
        if r.scoreAllotted:
            cat["score_allotted"] += r.scoreAllotted
            cat["score_obtained"] += r.scoreObtained or 0
            allotted_total += r.scoreAllotted
            obtained_total += r.scoreObtained or 0
        if val is None:
            continue
        answered += 1
        if val == "pass":
            passed += 1
            cat["passed"] += 1
        elif val == "partial":
            partial += 1
            cat["partial"] += 1
        elif val == "fail":
            failed += 1
            cat["failed"] += 1
            if r.criticality == "critical":
                crit_fail += 1
            elif r.criticality == "major":
                major_fail += 1
            else:
                minor_fail += 1
        elif val == "na":
            na += 1
            cat["na"] += 1
        if val in ("fail", "partial"):
            if page_grading.is_repeat(r.complianceStatus):
                repeat_findings += 1
            if r.requirementType == page_grading.REQ_STATUTORY:
                statutory_findings += 1

    overall = page_grading.compute_points_score(
        obtained=obtained_total, allotted=allotted_total
    )

    category_scores = []
    for c in cat_scores.values():
        c["score_pct"] = page_grading.compute_points_score(
            obtained=c["score_obtained"], allotted=c["score_allotted"]
        )
        category_scores.append(c)

    audit_passed = crit_fail == 0 and overall >= MINIMUM_PASS_SCORE

    return {
        "total_checkpoints": len(responses),
        "answered": answered,
        "passed": passed,
        "partially_passed": partial,
        "failed": failed,
        "not_applicable": na,
        "overall_score_pct": overall,
        "category_scores": sorted(category_scores, key=lambda c: c["category_name"]),
        "critical_failures": crit_fail,
        "major_failures": major_fail,
        "minor_failures": minor_fail,
        "audit_passed": audit_passed,
        # Page grading rollup — the workbook's own arithmetic, so a reader can
        # check the percentage rather than take it on trust.
        "score_obtained": obtained_total,
        "score_allotted": allotted_total,
        "score_band": page_grading.band(overall, MINIMUM_PASS_SCORE),
        "repeat_findings": repeat_findings,
        "statutory_findings": statutory_findings,
    }


async def audit_dashboard(db: AsyncSession, audit_id: str) -> dict[str, Any] | None:
    audit = await _load_audit(db, audit_id)  # header only — score via aggregate
    if audit is None:
        return None
    rollup = await _discipline_rollup(db, audit_id)
    score = _score_from_rollup(rollup)
    R = AuditCheckpointResponse
    crit_total = (
        await db.execute(select(func.count(R.id)).where(R.auditId == audit_id, R.criticality == "critical"))
    ).scalar_one() or 0
    crit_compliant = crit_total - score["critical_failures"]
    return {
        "auditId": audit.id,
        "auditNumber": audit.auditNumber,
        "title": audit.title,
        "status": audit.status,
        "score": score,
        "criticalCompliance": {
            "total": crit_total,
            "compliant": crit_compliant,
            "pct": round(crit_compliant / crit_total * 100, 1) if crit_total else 100.0,
        },
        "donut": {
            "pass": score["passed"],
            "partial": score["partially_passed"],
            "fail": score["failed"],
            "na": score["not_applicable"],
            "not_answered": score["total_checkpoints"] - score["answered"],
        },
    }


# ─────────────────────────────────────────────────────────────────────
# Create (materialize checkpoint rows)
# ─────────────────────────────────────────────────────────────────────


async def _next_number(db: AsyncSession, industry_code: str, plant_code: str) -> str:
    """`AUD-{industry}-{year}-{plant}-{seq}`, where seq is MAX(existing) + 1.

    Read over EVERY row, soft-deleted ones included. This used to be
    `COUNT(*) + 1`, which was wrong twice over and had taken Schedule Audit down
    completely:

      1. `auditNumber` is UNIQUE across the physical table, and a soft-delete
         leaves the row — and its number — in place. Counting only live rows
         re-issues a number that still exists.
      2. `ComplianceAudit` is a governed entity, so the global soft-delete
         filter in `app.core.soft_delete` silently rewrites every ORM SELECT
         here to `isDeleted = false`. The count came back 9 against 18 real
         rows, so every attempt proposed `AUD-GT-2026-NW-0010` — a live audit —
         and died on the unique constraint as a 500. It could never recover on
         its own: the count only moves when a create succeeds.

    `include_deleted=True` opts this one query out of that filter. That is the
    documented escape hatch and the right use of it: uniqueness is a property of
    the table, not of what the current user is allowed to see.

    The sequence is deliberately global rather than per-plant/per-industry —
    that is the existing scheme in the tenant (0001…0018 run unbroken across
    GT/CP/SD1/SM and both sites), so max+1 preserves it exactly.
    """
    year = _utcnow().year
    short = _industry_short(industry_code)
    # Anchor on the trailing digits: a plant code may itself contain a dash, so
    # splitting on separators is not safe.
    tail = func.regexp_replace(ComplianceAudit.auditNumber, r"^.*-", "")
    last = (
        await db.execute(
            select(func.max(cast(tail, Integer)))
            .where(ComplianceAudit.auditNumber.op("~")(r"-[0-9]+$"))
            .execution_options(include_deleted=True)
        )
    ).scalar() or 0
    return f"AUD-{short}-{year}-{plant_code}-{(last + 1):04d}"


def _route_for_category(category_code: str, auditees: list[dict[str, Any]]) -> str | None:
    for a in auditees:
        cats = a.get("responsibleCategories") or []
        if category_code in cats:
            return a.get("userId")
    return None


def _route_auditor_for_category(category_code: str, co_auditors: list | None, lead_id: str) -> str:
    """Which auditor conducts this discipline. coAuditors may be the structured
    shape [{userId, disciplineIds}] (a discipline match wins) or the legacy flat
    [userId] (no per-discipline scope → lead conducts). Falls back to the lead."""
    for c in co_auditors or []:
        if isinstance(c, dict):
            if category_code in (c.get("disciplineIds") or []) and c.get("userId"):
                return c["userId"]
    return lead_id


def _snapshot_fields(cat: dict[str, Any], cp: dict[str, Any]) -> dict[str, Any]:
    """The library checkpoint's DEFINITION, frozen onto a response row.

    Everything here is snapshotted rather than read live so a later library edit
    cannot retroactively restate what an audit was assessed against — the same
    rule `requirementType` was introduced under, now applied to the whole
    definition.

    Extracted because `create_audit` and `add_disciplines` materialise rows from
    the same library with the same semantics, and they had two byte-identical
    copies of this block. Adding the department/stream fields to one and not the
    other would have meant a discipline added mid-audit silently landed in
    neither report.

    `conformance_mode` is read from the CATEGORY first: the vocabulary is a
    property of the checklist, not of an individual line. A checkpoint may still
    override it, which is what lets an ad-hoc line be added to a tristate
    department without forcing the whole department onto the seven-value ladder.
    """
    return {
        "checkpointQuestion": cp.get("question", ""),
        "guidance": cp.get("guidance", ""),
        "requirementReference": cp.get("requirement_reference", ""),
        "standard": cp.get("standard", ""),
        "criticality": cp.get("criticality", "major"),
        "responseType": cp.get("response_type", "pass_partial_fail"),
        "requirementType": page_grading.normalise_requirement_type(
            cp.get("requirement_type")
        ),
        # ── Department-segregated management-system audits (PAGE_IMS) ─────
        "streamCode": (cp.get("stream") or None),
        "replicationKey": (cp.get("replication_key") or None),
        "pairKey": (cp.get("pair_key") or None),
        "conformanceMode": page_grading.normalise_conformance_mode(
            cp.get("conformance_mode") or cat.get("conformance_mode")
        ),
        "standardClauses": cp.get("standard_clauses") or [],
        "requiresPhotoOnFail": bool(cp.get("requires_photo_on_fail", False)),
        "autoTriggerCapaOnFail": bool(cp.get("auto_trigger_capa_on_fail", False)),
        "capaSeverity": cp.get("capa_severity_if_triggered"),
        "linkedSafeopsModule": cp.get("linked_safeops_module"),
    }


# Titles that carry no information about the engagement. WP-01 soft-deleted the
# ones already in the tenant, but a one-off cleanse cannot stop the next one:
# `AUD-GT-2026-NW-0016` was created afterwards and titled "Audit", which the
# cleanse's `^(test|demo)` pattern never matched. A title appears on the report
# cover a certification body reads, so it is validated at save time.
_PLACEHOLDER_TITLES = {
    "audit", "test", "test audit", "demo", "demo audit", "new audit",
    "untitled", "untitled audit", "sample", "sample audit", "abc", "xyz", "asdf",
}
_PLACEHOLDER_PREFIXES = ("test ", "demo ", "test-", "demo-", "tmp ", "temp ")


def validate_audit_title(title: str | None) -> str:
    """Reject placeholder titles. Returns the cleaned title."""
    t = (title or "").strip()
    if len(t) < 5:
        raise ValueError(
            "Give the audit a descriptive title (at least 5 characters) — it appears on the "
            "report cover. For example: “Q3 SA8000 + ISO 45001 Audit — North Works”."
        )
    low = t.lower().rstrip(".!")
    if low in _PLACEHOLDER_TITLES or low.startswith(_PLACEHOLDER_PREFIXES) or low.isdigit():
        raise ValueError(
            f"“{t}” is a placeholder, not an audit title. It would be printed on the report "
            "cover. Name the scope and period — e.g. “Q3 SA8000 + ISO 45001 Audit — North Works”."
        )
    return t


async def create_audit(db: AsyncSession, *, user: User, data: dict[str, Any]) -> ComplianceAudit:
    industry_code = data.get("industryCode")
    # `None` here, not the generic default: the fallback is applied AFTER the
    # library is resolved, so an audit whose checklist belongs to a category can
    # inherit that category's audit type. Defaulting now would make every
    # programme-materialised audit read "compliance_audit" regardless.
    audit_type = data.get("auditType")
    config: dict[str, Any] = {"mode": "all"}
    template: AuditTemplate | None = None

    template_id = data.get("templateId")
    if template_id:
        template = await db.get(AuditTemplate, template_id)
        if template is None:
            raise ValueError("Invalid templateId")
        industry_code = template.baseIndustry
        audit_type = audit_type or template.auditType
        config = template.checkpointConfiguration or {"mode": "all"}

    if not industry_code:
        raise ValueError("industryCode or templateId is required")

    library = (
        await db.execute(
            select(AuditCheckpointLibrary).where(AuditCheckpointLibrary.industryCode == industry_code)
        )
    ).scalar_one_or_none()
    if library is None:
        raise ValueError(f"No checkpoint library for industry {industry_code}")

    # ── Audit type follows the checklist's category ─────────────────────────
    #
    # An explicit auditType (the scheduling wizard sends the category's) and a
    # template's own type both win — they are the more specific statement of
    # intent. Otherwise the category the resolved library belongs to supplies it,
    # so a report names the regime the audit was actually run under.
    #
    # Placed here rather than in each caller because the callers are several: the
    # wizard, the programme's `materialise_slot`, and the raw API. A rule that
    # lives in one of them is not a rule — a programme-materialised QMS audit
    # would read "compliance_audit" while a hand-scheduled one read
    # "management_system_audit", for no reason a reader could discover.
    if not audit_type:
        _cat = library_audit_category(industry_code, library.categories or [])
        audit_type = next(
            (c["auditType"] for c in AUDIT_CATEGORIES if c["code"] == _cat),
            "compliance_audit",
        )

    plant = await db.get(Plant, data["plantId"])
    plant_code = plant.code if plant else "XX"

    # ── Audit subject (WP-45): own facility, or a supplier ─────────────────
    #
    # `plantId` stays the OWNING plant — the site that holds the vendor
    # relationship — even when the audit is conducted at the supplier's
    # factory. It is not a cosmetic choice: `plantId` drives audit numbering,
    # RBAC plant scoping (`get_accessible_plants_for`), the independence scope
    # and programme coverage. Pointing it at a supplier site (which is not a
    # `Plant` row at all) would break all four. The supplier is named by
    # `SupplierAuditLink`, which is what `subjectType` is derived from.
    subject_type = (data.get("subjectType") or "OWN_SITE").upper()
    vendor_profile_id = data.get("vendorProfileId")
    if subject_type == "VENDOR":
        if not vendor_profile_id:
            raise ValueError("A supplier audit needs vendorProfileId")
        from app.services import vendors as vendor_svc

        if await vendor_svc.get_vendor(db, vendor_profile_id) is None:
            raise ValueError("Vendor not found")
    elif vendor_profile_id:
        raise ValueError(
            "vendorProfileId was supplied but subjectType is not VENDOR — refusing to "
            "create an audit whose subject is ambiguous"
        )

    if subject_type == "VENDOR":
        # ── The checklist must match the subject ──────────────────────────
        #
        # An own-facility library asks "is the kiln refractory inspection within
        # validity" — a question about OUR plant. Materialising it against a
        # garment supplier produces a report that reads like an internal plant
        # inspection, which is the failure this guard exists to stop.
        #
        # Enforced here and not only in the wizard, because the wizard is one
        # caller: the API, the ad-hoc flow and any future scheduler all reach
        # this function, and a UI-only rule is not a rule.
        _scope = library_subject_scope(industry_code, library.categories or [])
        if _scope == "OWN_SITE":
            raise ValueError(
                f"'{library.industryName}' is an own-facility checklist and cannot be "
                "used for a supplier audit. Select a supplier compliance checklist "
                "(e.g. a Supplier Code of Conduct or social-compliance regime)."
            )
        if not sum(len(c.get("checkpoints") or []) for c in (library.categories or [])):
            # The buyer regimes ship as structure with zero checkpoints because
            # the criteria are licensed. Scheduling against one would create an
            # audit with nothing to assess.
            raise ValueError(
                f"'{library.industryName}' has no checkpoints loaded yet, so this audit "
                "would have nothing to assess. Import the supplier checklist content "
                "before scheduling against it."
            )
    else:
        # ── The mirror of the guard above, which did not exist ─────────────
        #
        # A supplier checklist asks whether "the factory" holds a valid licence
        # and pays minimum wages — put to a SUPPLIER during a social audit. Run
        # against our own site it produces a report that reads as though we were
        # screening ourselves as a vendor, and it duplicates ground the internal
        # HR/EHS audit already covers.
        #
        # This became reachable the moment Social Compliance moved to the vendor
        # subject: before that every own-facility library was OWN_SITE and the
        # asymmetry cost nothing.
        _scope = library_subject_scope(industry_code, library.categories or [])
        if _scope == "VENDOR":
            raise ValueError(
                f"'{library.industryName}' is a supplier checklist and cannot be used "
                "for an audit of our own facility. Choose the Internal or "
                "QMS/EMS/OHS category, or set the audit subject to Supplier."
            )

    auditees = data.get("auditees") or []
    mode = (config or {}).get("mode", "all")
    subset_codes = set((config or {}).get("codes") or [])
    subset_categories = set((config or {}).get("categories") or [])

    # Discipline scope (audit-lifecycle v2). A discipline is a library
    # category_code. When the client sends a non-empty selection it is
    # AUTHORITATIVE: the discipline chips define exactly which checkpoints
    # materialize (every checkpoint in each selected discipline), and any
    # template code/category subset is ignored — the chips already express the
    # scope and the live "will materialize N" count is the sum of the selected
    # disciplines. An empty selection means "full library" (back-compat for
    # programmatic callers), and in that path the template subset
    # codes/categories still filter as before.
    selected = set(data.get("selectedDisciplineIds") or [])

    # ── Independence at engagement creation (docs/cams/09 §2.1.5) ──────────
    # Runs BEFORE the row is written so a conflicted team never produces a
    # half-created audit. `independenceAcknowledged` carries the caller past
    # WARN-level conflicts only; BLOCK still needs a waiver, which cannot be
    # granted for an engagement that does not exist yet — hence "choose another
    # auditor" is the only path here, by design.
    _lead = data.get("leadAuditorUserId") or user.id
    _team = independence._coauditor_ids(data.get("coAuditors") or [])
    _auditee_ids = independence._auditee_ids(auditees)
    if data.get("plantManagerUserId"):
        _auditee_ids.append(data["plantManagerUserId"])
    _scope = independence.EngagementScope(
        kind="AUDIT",
        id=None,
        siteId=data["plantId"],
        disciplineCodes=sorted(selected),
        areaIds=[a for a in (data.get("scopeAreas") or []) if a],
        departments=[d for d in (data.get("scopeDepartments") or []) if d],
        leadAuditorId=_lead,
        teamAuditorIds=_team,
        auditeeUserIds=sorted({a for a in _auditee_ids if a}),
        # Supplier audits add one conflict source: the person who owns the
        # commercial relationship cannot audit their own vendor.
        vendorProfileId=vendor_profile_id if subject_type == "VENDOR" else None,
    )
    _verdicts = await independence.check_many(
        db, user_ids=[_lead, *_team], scope=_scope, assigning_as="AUDITOR"
    )
    _names = {}
    _blocked = {uid: v for uid, v in _verdicts.items() if v.blocking}
    # Record what the guard decided BEFORE raising. `record_verdicts` writes on
    # its own session precisely because the raise below rolls this one back —
    # the blocked attempt is the evidence, and it must outlive the audit that
    # never got created (docs/cams — Independence Register §2.2).
    await independence_events.record_verdicts(
        verdicts=_verdicts,
        engagement_kind="AUDIT",
        origin="CREATE_AUDIT",
        attempted_by_user_id=user.id,
        site_id=data["plantId"],
    )
    if _blocked:
        _names = {
            u.id: u.name
            for u in (
                await db.execute(select(User).where(User.id.in_(list(_blocked))))
            ).scalars().all()
        }
        parts = [
            f"{_names.get(uid, uid)}: {v.blocking[0].reason}" for uid, v in _blocked.items()
        ]
        raise ValueError(
            "Auditor independence — " + " | ".join(parts)
        )

    audit = ComplianceAudit(
        auditNumber=await _next_number(db, industry_code, plant_code),
        title=validate_audit_title(data.get("title")),
        plantId=data["plantId"],
        templateId=template_id,
        industryCode=industry_code,
        auditType=audit_type,
        scopeDepartments=data.get("scopeDepartments") or [],
        scopeAreas=data.get("scopeAreas") or [],
        scopeDescription=data.get("scopeDescription") or "",
        scopePresetUsed=data.get("scopePresetUsed"),
        scheduledDate=data["scheduledDate"],
        scheduledStartTime=data.get("scheduledStartTime") or "09:00",
        estimatedDurationHours=data.get("estimatedDurationHours") or 2,
        leadAuditorUserId=data.get("leadAuditorUserId") or user.id,
        coAuditors=data.get("coAuditors") or [],
        auditees=auditees,
        plantManagerUserId=data.get("plantManagerUserId"),
        status="scheduled",
        openingRemarks=data.get("openingRemarks") or "",
        createdByUserId=user.id,
    )
    db.add(audit)
    await db.flush()

    # Name the subject in the same transaction that created the audit. Doing it
    # as a separate call afterwards (the previous shape) meant a failure between
    # the two left an audit that was scheduled AS a supplier audit but has no
    # supplier — and `subjectType` would read OWN_SITE for it forever.
    if subject_type == "VENDOR":
        from app.services import cams_suppliers as _sup

        await _sup.link_supplier(
            db,
            engagement_kind="AUDIT",
            engagement_id=audit.id,
            vendor_profile_id=vendor_profile_id,
            vendor_site_ref=data.get("vendorSiteRef"),
            contact_name=data.get("supplierContactName"),
            contact_email=data.get("supplierContactEmail"),
            actor_id=user.id,
        )

        # ── External access links, issued in the same transaction ───────────
        #
        # A supplier audit's parties have no platform seats: the supplier manager
        # stands in for our plant manager, and external co-auditors and auditees
        # work from a link rather than an account. Their email IS their identity,
        # so the credential is minted here, with the audit, rather than as a
        # separate step someone has to remember — an audit whose external parties
        # cannot reach it is not a scheduled audit.
        #
        # One link per (person, role). Issuing is per-recipient tolerant: a
        # mistyped co-auditor address costs that one link, not the whole audit,
        # and the result names which links were actually created.
        from app.services import supplier_portal as _portal

        _recipients: list[dict[str, Any]] = []
        if data.get("supplierContactEmail"):
            _recipients.append({
                "email": data["supplierContactEmail"],
                "name": data.get("supplierContactName"),
                "role": "SUPPLIER_MANAGER",
            })
        # Externals carry disciplines so a co-auditor's link is scoped to the
        # part of the audit they were actually brought in to conduct.
        for _c in data.get("externalCoAuditors") or []:
            if _c.get("email"):
                _recipients.append({
                    "email": _c["email"], "name": _c.get("name"), "role": "CO_AUDITOR",
                    "disciplineCodes": [d for d in (_c.get("disciplineIds") or []) if d in selected] or sorted(selected),
                })
        for _a in data.get("externalAuditees") or []:
            if _a.get("email"):
                _recipients.append({
                    "email": _a["email"], "name": _a.get("name"), "role": "AUDITEE",
                    "disciplineCodes": [d for d in (_a.get("disciplineIds") or []) if d in selected] or sorted(selected),
                })
        if _recipients:
            issued = await _portal.issue_tokens(
                db,
                audit_id=audit.id,
                recipients=_recipients,
                vendor_profile_id=vendor_profile_id,
                actor_id=user.id,
            )
            # Returned to the caller ONCE. The raw token is never stored (only a
            # hash is), so this is the only moment the links can be handed over —
            # if the response is dropped they must be re-issued, which is the
            # intended cost of not keeping replayable credentials in the database.
            audit._issuedPortalLinks = [  # type: ignore[attr-defined]
                {
                    "email": t.token.supplierContactEmail,
                    "name": t.token.supplierContactName,
                    "role": t.token.role,
                    "portalPath": t.portalPath,
                    "expiresAt": _iso(t.token.expiresAt),
                }
                for t in issued
            ]

    rows: list[AuditCheckpointResponse] = []
    materialized_disciplines: list[str] = []
    seq = 0
    for cat in library.categories or []:
        cat_code = cat.get("category_code")
        if selected:
            if cat_code not in selected:
                continue
        elif mode == "subset" and subset_categories and cat_code not in subset_categories:
            continue
        order_in_disc = 0
        for cp in cat.get("checkpoints", []):
            code = cp.get("code")
            # Template code subset only applies on the back-compat (no explicit
            # discipline selection) path — see the `selected` comment above.
            if not selected and mode == "subset" and subset_codes and code not in subset_codes:
                continue
            seq += 1
            order_in_disc += 1
            owner = _route_for_category(cat_code, auditees)
            auditor = _route_auditor_for_category(cat_code, audit.coAuditors, audit.leadAuditorUserId)
            rows.append(
                AuditCheckpointResponse(
                    auditId=audit.id,
                    plantId=audit.plantId,
                    checkpointCode=code,
                    categoryId=cat_code,
                    categoryName=cat.get("category_name", ""),
                    categoryColor=cat.get("category_color", ""),
                    **_snapshot_fields(cat, cp),
                    sequence=seq,
                    orderIndex=order_in_disc,
                    routedToUserId=owner,
                    assignedOwnerId=owner,
                    assignedAuditorId=auditor,
                    assessmentStatus="NOT_ASSESSED",
                    workflowState="OPEN",
                    currentRound=0,
                    isAdHoc=False,
                    overallStatus="not_answered",
                )
            )
        if order_in_disc:
            materialized_disciplines.append(cat_code)

    # Template custom checkpoints (audit-lifecycle v2): materialize the chosen
    # template's custom checkpoints whose discipline is in scope. Flagged custom
    # via isAdHoc; they continue each discipline's orderIndex.
    if template is not None and (template.customCheckpoints or []):
        order_max: dict[str, int] = {}
        meta: dict[str, tuple[str, str]] = {}
        for r in rows:
            order_max[r.categoryId] = max(order_max.get(r.categoryId, 0), r.orderIndex)
            meta[r.categoryId] = (r.categoryName, r.categoryColor)
        lib_cats = {c.get("category_code"): c for c in (library.categories or [])}
        for ccp in template.customCheckpoints or []:
            dcode = ccp.get("discipline_code")
            if not dcode:
                continue
            if selected and dcode not in selected:
                continue  # respect the discipline scope
            if dcode in meta:
                cname, ccolor = meta[dcode]
            else:
                libcat = lib_cats.get(dcode)
                cname = (libcat or {}).get("category_name") or ccp.get("discipline_name", "")
                ccolor = (libcat or {}).get("category_color", "")
            seq += 1
            order_max[dcode] = order_max.get(dcode, 0) + 1
            owner = _route_for_category(dcode, auditees)
            auditor = _route_auditor_for_category(dcode, audit.coAuditors, audit.leadAuditorUserId)
            rows.append(
                _new_checkpoint_row(
                    audit=audit, cat_code=dcode, cat_name=cname, cat_color=ccolor,
                    code=ccp.get("code") or f"CUST-{dcode}-{order_max[dcode]:02d}",
                    question=ccp.get("question", ""), criticality=ccp.get("criticality", "major"),
                    guidance=ccp.get("guidance", ""),
                    requirement_reference=ccp.get("requirement_reference", ""),
                    standard=ccp.get("standard", ""),
                    requires_photo=bool(ccp.get("evidence_required_on_fail")),
                    sequence=seq, order_index=order_max[dcode], owner=owner, auditor=auditor,
                    is_adhoc=True, added_by=ccp.get("added_by_id"),
                )
            )
            if dcode not in materialized_disciplines:
                materialized_disciplines.append(dcode)

    # Guard: never persist a phantom empty audit. The session is rolled back on
    # this ValueError (router → 400), so nothing leaks.
    if not rows:
        raise ValueError(
            "The selected scope produced no checkpoints — adjust the disciplines or template."
        )

    db.add_all(rows)
    # Record the *actual* materialized scope (so an empty input resolves to the
    # full discipline list, and the audit self-describes its scope).
    audit.selectedDisciplineIds = materialized_disciplines
    audit.totalCheckpoints = len(rows)
    audit.materializedCheckpointCount = len(rows)
    audit.adHocCount = 0
    await db.flush()

    # Competence snapshot (docs/cams/09 §2.2) — freeze what the Skill Matrix said
    # about each auditor at assignment time. A live read cannot answer "was this
    # person qualified when the audit was conducted?" after a revalidation, so
    # this is captured rather than derived. No-ops when the audit type declares
    # no required competencies, which is every audit type until WP-49 populates
    # them from the admin screen.
    for _uid in {audit.leadAuditorUserId, *_coauditor_ids(audit.coAuditors)} - {None}:
        await assurance.capture_competence_snapshot(
            db,
            engagement_kind="AUDIT",
            engagement_id=audit.id,
            user_id=_uid,
            audit_type_id=audit.auditType,
            captured_by=user.id,
        )
    await db.flush()

    # ── The moment the audit is set, the time is claimed ──────────────────
    #
    # Scheduling an audit used to produce a date in this table and nothing in
    # anybody's calendar, so the first the auditee heard of it was often the
    # auditor arriving. This books the fieldwork window plus the opening and
    # closing meetings for whoever is named so far — usually just the lead
    # auditor at this point, which is correct: `update_audit_team` re-syncs and
    # picks up the auditees when they are actually identified.
    #
    # Best-effort by contract (`sync_engagement` never raises). An unreachable
    # Exchange leaves the bookings PENDING for the retry job; it must not be
    # able to fail the creation of the audit itself.
    from app.services import calendar_booking as _cal

    await _cal.sync_engagement(
        db, engagement_kind="AUDIT", engagement_id=audit.id, actor_id=user.id
    )

    # ── And the moment the time is claimed, the people are told ───────────
    #
    # The calendar invite says WHEN. It does not say what the audit is, which
    # disciplines are yours, or what you are expected to do — and a calendar
    # invite from a system address is routinely accepted unread. Every seat now
    # also gets an in-app notification and an email carrying a role-specific
    # deep link into the audit.
    #
    # Best-effort by contract (`notify_audit_scheduled` swallows its own
    # exceptions), for the same reason as the calendar sync above: an audit must
    # not fail to be created because SMTP was down.
    from app.services import cams_audit_notifications as _camsnotif

    await _camsnotif.notify_audit_scheduled(db, audit=audit, actor_id=user.id)
    return audit


async def add_disciplines(
    db: AsyncSession, *, user: User, audit_id: str, discipline_ids: list[str]
) -> dict[str, Any]:
    """Materialize one or more additional disciplines into a running audit
    (before finalization), without disturbing existing checkpoints."""
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        raise ValueError("Audit not found")
    # Pre-finalization only. After submit the score/compliance snapshot is frozen
    # and auto-CAPA has already run; adding checkpoints then would corrupt the
    # denominators and silently skip auto-CAPA on the new rows.
    if audit.status not in ("scheduled", "in_progress"):
        raise ValueError(
            f"Audit is '{audit.status}'; disciplines can only be added before submission"
        )

    library = (
        await db.execute(
            select(AuditCheckpointLibrary).where(AuditCheckpointLibrary.industryCode == audit.industryCode)
        )
    ).scalar_one_or_none()
    if library is None:
        raise ValueError(f"No checkpoint library for industry {audit.industryCode}")

    existing_codes = {r.checkpointCode for r in audit.responses}
    existing_disc = list(audit.selectedDisciplineIds or [])
    want = set(discipline_ids) - set(existing_disc)
    seq = max((r.sequence for r in audit.responses), default=0)
    auditees = audit.auditees or []

    new_rows: list[AuditCheckpointResponse] = []
    added_disc: list[str] = []
    for cat in library.categories or []:
        cat_code = cat.get("category_code")
        if cat_code not in want:
            continue
        order_in_disc = 0
        for cp in cat.get("checkpoints", []):
            code = cp.get("code")
            if code in existing_codes:
                continue  # never duplicate an already-materialized checkpoint
            seq += 1
            order_in_disc += 1
            owner = _route_for_category(cat_code, auditees)
            auditor = _route_auditor_for_category(cat_code, audit.coAuditors, audit.leadAuditorUserId)
            new_rows.append(
                AuditCheckpointResponse(
                    auditId=audit.id,
                    plantId=audit.plantId,
                    checkpointCode=code,
                    categoryId=cat_code,
                    categoryName=cat.get("category_name", ""),
                    categoryColor=cat.get("category_color", ""),
                    **_snapshot_fields(cat, cp),
                    sequence=seq,
                    orderIndex=order_in_disc,
                    routedToUserId=owner,
                    assignedOwnerId=owner,
                    assignedAuditorId=auditor,
                    assessmentStatus="NOT_ASSESSED",
                    workflowState="OPEN",
                    currentRound=0,
                    isAdHoc=False,
                    overallStatus="not_answered",
                )
            )
        if order_in_disc:
            added_disc.append(cat_code)

    db.add_all(new_rows)
    audit.selectedDisciplineIds = existing_disc + added_disc
    audit.totalCheckpoints = (audit.totalCheckpoints or 0) + len(new_rows)
    audit.materializedCheckpointCount = audit.totalCheckpoints
    await db.flush()
    return {
        "ok": True,
        "added": len(new_rows),
        "disciplines": added_disc,
        "totalCheckpoints": audit.totalCheckpoints,
    }


# ─────────────────────────────────────────────────────────────────────
# Custom checkpoints (audit-lifecycle v2) — ad-hoc to a live audit + template
# fork + promote-to-template. `isAdHoc` on an instance flags ANY custom
# (non-base-library) checkpoint so it shows the "Custom" badge everywhere;
# `adHocCount` on the audit counts ad-hoc additions made during conduct.
# ─────────────────────────────────────────────────────────────────────


def _new_checkpoint_row(
    *, audit: ComplianceAudit, cat_code: str, cat_name: str, cat_color: str, code: str,
    question: str, criticality: str, guidance: str = "", requirement_reference: str = "",
    standard: str = "", response_type: str = "pass_partial_fail", requires_photo: bool = False,
    auto_capa: bool = False, capa_severity: str | None = None, linked_module: str | None = None,
    sequence: int, order_index: int, owner: str | None = None, auditor: str | None = None,
    is_adhoc: bool = False, added_by: str | None = None,
    stream_code: str | None = None, conformance_mode: str | None = None,
) -> AuditCheckpointResponse:
    return AuditCheckpointResponse(
        auditId=audit.id, plantId=audit.plantId,
        checkpointCode=code, checkpointQuestion=question, guidance=guidance or "",
        requirementReference=requirement_reference or "", standard=standard or "",
        categoryId=cat_code, categoryName=cat_name, categoryColor=cat_color or "",
        criticality=criticality or "major", responseType=response_type or "pass_partial_fail",
        # A custom line added to a department audit belongs to one of the two
        # streams and is answered in that department's vocabulary. Both are
        # INHERITED from the sibling rows rather than asked for: an ad-hoc
        # checkpoint with no stream would appear in neither report, and one on
        # the seven-value ladder inside a tristate department would show the
        # auditor a control none of the other cards have.
        streamCode=stream_code,
        # No replication or pair key: an ad-hoc line exists on this audit only,
        # so it has no counterpart in another department or on the other sheet.
        replicationKey=None, pairKey=None,
        conformanceMode=page_grading.normalise_conformance_mode(conformance_mode),
        sequence=sequence, orderIndex=order_index,
        requiresPhotoOnFail=bool(requires_photo), autoTriggerCapaOnFail=bool(auto_capa),
        capaSeverity=capa_severity, linkedSafeopsModule=linked_module,
        routedToUserId=owner, assignedOwnerId=owner,
        assignedAuditorId=auditor or audit.leadAuditorUserId,
        assessmentStatus="NOT_ASSESSED", workflowState="OPEN", currentRound=0,
        isAdHoc=is_adhoc, addedById=added_by, overallStatus="not_answered",
    )


def _interaction_to_dict(i: CheckpointInteraction) -> dict[str, Any]:
    return {
        "id": i.id,
        "checkpointInstanceId": i.checkpointInstanceId,
        "auditId": i.auditId,
        "round": i.round,
        "actorId": i.actorId,
        "actorRole": i.actorRole,
        "action": i.action,
        "comment": i.comment,
        "evidenceIds": i.evidenceIds or [],
        "resultingState": i.resultingState,
        "timestamp": _iso(i.timestamp),
    }


async def _log_interaction(
    db: AsyncSession, *, instance: AuditCheckpointResponse, audit_id: str, actor_id: str,
    actor_role: str, action: str, resulting_state: str, comment: str | None = None,
    evidence_ids: list[str] | None = None, round: int | None = None, at: datetime | None = None,
) -> CheckpointInteraction:
    """Append one immutable row to a checkpoint's iteration thread. The Gate-6
    state machine reuses this; Gate 4 uses it only for ADHOC_ADDED. Pass `at` to
    stamp an explicit timestamp — used when several interactions are logged in
    one transaction (server now() would tie them) so the thread stays ordered."""
    inter = CheckpointInteraction(
        checkpointInstanceId=instance.id,
        auditId=audit_id,
        round=round if round is not None else instance.currentRound,
        actorId=actor_id,
        actorRole=actor_role,
        action=action,
        comment=comment,
        evidenceIds=evidence_ids or [],
        resultingState=resulting_state,
    )
    # Always stamp from ONE clock (the app's), never the DB server_default — so
    # ordering is consistent across actions and a single transaction's multiple
    # logs (which DB now() would tie) stay deterministically ordered via `at`.
    inter.timestamp = at if at is not None else _utcnow()
    db.add(inter)
    return inter


def _actor_role_for(user: User, audit: ComplianceAudit) -> str:
    if user.id == audit.leadAuditorUserId:
        return "LEAD_AUDITOR"
    if user.id in _coauditor_ids(audit.coAuditors):
        return "CO_AUDITOR"
    return "AUDITOR"


async def add_adhoc_checkpoint(
    db: AsyncSession, *, user: User, audit_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Auditor adds a custom checkpoint to THIS audit only (carousel "+").
    Slots into its discipline, counts toward scoring, logs ADHOC_ADDED, and
    optionally promotes itself to the audit's template."""
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        raise ValueError("Audit not found")
    if audit.status not in ("scheduled", "in_progress"):
        raise ValueError(f"Audit is '{audit.status}'; checkpoints can only be added before submission")
    if payload.get("promoteToTemplate") and not audit.templateId:
        raise ValueError("This audit has no template to promote the checkpoint to")

    disc_code = payload.get("disciplineId") or payload.get("disciplineCode")
    if not disc_code:
        raise ValueError("disciplineId is required")
    question = (payload.get("question") or "").strip()
    if len(question) < 4:
        raise ValueError("question must be at least 4 characters")

    # Resolve the discipline's display name/colour from existing rows, else the
    # library. Its conformance vocabulary comes from the same place, for the
    # same reason: a card offering three buttons among a department that answers
    # on seven (or the reverse) is a control the auditor has no way to read.
    name: str | None = None
    color = ""
    conformance_mode: str | None = None
    for r in audit.responses:
        if r.categoryId == disc_code:
            name, color, conformance_mode = r.categoryName, r.categoryColor, r.conformanceMode
            break
    if name is None:
        library = (
            await db.execute(
                select(AuditCheckpointLibrary).where(AuditCheckpointLibrary.industryCode == audit.industryCode)
            )
        ).scalar_one_or_none()
        libcat = next(
            (c for c in ((library.categories if library else []) or []) if c.get("category_code") == disc_code),
            None,
        )
        if libcat is None:
            raise ValueError(f"Unknown discipline '{disc_code}'")
        name, color = libcat.get("category_name", ""), libcat.get("category_color", "")
        conformance_mode = libcat.get("conformance_mode")

    # Which stream a custom line joins. Offered as an explicit choice because in
    # a department audit "add a checkpoint" is genuinely ambiguous — it could
    # belong to either report — and defaulted to the stream the department
    # mostly holds so a caller that does not know about streams still lands
    # somewhere a report will print it.
    stream_code = (payload.get("streamCode") or payload.get("stream") or "").upper() or None
    dept_streams = [r.streamCode for r in audit.responses if r.categoryId == disc_code and r.streamCode]
    if stream_code is not None and stream_code not in set(dept_streams):
        raise ValueError(
            f"Unknown stream '{stream_code}' for {name} — expected one of "
            + (", ".join(sorted(set(dept_streams))) or "none (this checklist has no streams)")
        )
    if stream_code is None and dept_streams:
        stream_code = Counter(dept_streams).most_common(1)[0][0]

    existing_codes = {r.checkpointCode for r in audit.responses}
    n_adhoc = (audit.adHocCount or 0) + 1
    code = f"{audit.auditNumber}-AH{n_adhoc:02d}"
    while code in existing_codes:
        n_adhoc += 1
        code = f"{audit.auditNumber}-AH{n_adhoc:02d}"

    order_index = max((r.orderIndex for r in audit.responses if r.categoryId == disc_code), default=0) + 1
    seq = max((r.sequence for r in audit.responses), default=0) + 1
    owner = payload.get("assignedOwnerId") or _route_for_category(disc_code, audit.auditees or [])
    auditor = _route_auditor_for_category(disc_code, audit.coAuditors, audit.leadAuditorUserId)

    row = _new_checkpoint_row(
        audit=audit, cat_code=disc_code, cat_name=name, cat_color=color, code=code, question=question,
        criticality=payload.get("severity") or payload.get("criticality") or "major",
        guidance=payload.get("guidance", ""),
        requirement_reference=payload.get("requirementReference", ""),
        standard=payload.get("standardClauseRef") or payload.get("standard", ""),
        requires_photo=bool(payload.get("evidenceRequiredOnFail")),
        sequence=seq, order_index=order_index, owner=owner, auditor=auditor, is_adhoc=True, added_by=user.id,
        stream_code=stream_code, conformance_mode=conformance_mode,
    )
    db.add(row)
    await db.flush()

    await _log_interaction(
        db, instance=row, audit_id=audit.id, actor_id=user.id, actor_role=_actor_role_for(user, audit),
        action="ADHOC_ADDED", resulting_state="OPEN", round=0,
        comment=f"Ad-hoc checkpoint added to {name}",
    )

    if disc_code not in (audit.selectedDisciplineIds or []):
        audit.selectedDisciplineIds = list(audit.selectedDisciplineIds or []) + [disc_code]
    audit.adHocCount = (audit.adHocCount or 0) + 1
    audit.totalCheckpoints = (audit.totalCheckpoints or 0) + 1
    audit.materializedCheckpointCount = (audit.materializedCheckpointCount or 0) + 1

    promoted_id: str | None = None
    if payload.get("promoteToTemplate") and audit.templateId:
        fork = await _fork_template_with_checkpoint(
            db, user=user, template_id=audit.templateId,
            cp={
                "discipline_code": disc_code, "discipline_name": name, "question": question,
                "criticality": row.criticality, "guidance": row.guidance,
                "requirement_reference": row.requirementReference, "standard": row.standard,
                "evidence_required_on_fail": row.requiresPhotoOnFail,
            },
        )
        promoted_id = fork.id

    await db.flush()
    return {"ok": True, "checkpoint": _response_to_dict(row), "promotedTemplateId": promoted_id}


async def _fork_template_with_checkpoint(
    db: AsyncSession, *, user: User, template_id: str, cp: dict[str, Any]
) -> AuditTemplate:
    """Fork a new template version with one custom checkpoint appended; retire
    the parent (templates are versioned/immutable once forked).

    The parent row is SELECT … FOR UPDATE locked so two concurrent forks of the
    same template serialize: the first retires it, the second re-reads it as
    inactive and is rejected (rather than both forking from the same baseline
    into divergent sibling versions). Only the active head can be forked."""
    parent = (
        await db.execute(
            select(AuditTemplate).where(AuditTemplate.id == template_id).with_for_update()
        )
    ).scalar_one_or_none()
    if parent is None:
        raise ValueError("Template not found")
    if not parent.isActive:
        raise ValueError("This template version has been superseded; fork the current version instead")

    existing = list(parent.customCheckpoints or [])
    dcode = cp.get("discipline_code") or "GEN"
    code = cp.get("code") or f"CUST-{dcode}-{len(existing) + 1:02d}"
    cp_def = {
        "code": code,
        "discipline_code": dcode,
        "discipline_name": cp.get("discipline_name", ""),
        "question": cp.get("question", ""),
        "criticality": cp.get("criticality", "major"),
        "guidance": cp.get("guidance", ""),
        "requirement_reference": cp.get("requirement_reference", ""),
        "standard": cp.get("standard", ""),
        "evidence_required_on_fail": bool(cp.get("evidence_required_on_fail")),
        "is_custom": True,
        "added_by_id": user.id,
        "added_at": _utcnow().isoformat(),
    }
    try:
        new_version = f"{int(float(parent.version)) + 1}.0"
    except (TypeError, ValueError):
        new_version = f"{parent.version}-v2"

    fork = AuditTemplate(
        tenantId=parent.tenantId, name=parent.name, description=parent.description,
        auditType=parent.auditType, baseIndustry=parent.baseIndustry,
        checkpointConfiguration=parent.checkpointConfiguration,
        customCheckpoints=existing + [cp_def], parentTemplateId=parent.id,
        scoring=parent.scoring, workflow=parent.workflow, isActive=True,
        version=new_version, createdByUserId=user.id,
    )
    db.add(fork)
    parent.isActive = False  # retire the parent version; keep it for history
    await db.flush()
    return fork


async def allocate_checkpoints(
    db: AsyncSession, *, user: User, audit_id: str,
    owner_id: str | None = None, auditor_id: str | None = None,
    set_owner: bool = True, set_auditor: bool = False,
    checkpoint_ids: list[str] | None = None, discipline_id: str | None = None,
) -> dict[str, Any]:
    """Allocate checkpoints to an AUDITEE (`owner_id`), an AUDITOR
    (`auditor_id`), or both — per-row, bulk (`checkpoint_ids`), or whole
    discipline (`discipline_id`).

    Discipline-wide allocation is the fast path, not the only one. A discipline
    is a convenient default because one department usually owns it, but real
    audits cut across that constantly: a single HR checkpoint about crèche
    staffing belongs to the welfare officer rather than the HR head, and one
    electrical item inside Production is the maintenance engineer's. Passing
    `checkpoint_ids` allocates exactly those rows and nothing else.

    `set_owner` / `set_auditor` say which axis this call is changing, so that
    passing `owner_id=None` UNASSIGNS the auditee rather than being mistaken for
    "leave the auditee alone" — the two are different intentions and a null
    cannot express both.

    Each owner change logs a ROUTED_TO_OWNER interaction, so a reassignment
    carries any in-flight iteration with it visibly. Auditor changes are a work
    split with no thread state, so they are not logged as interactions.
    """
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        raise ValueError("Audit not found")
    if audit.status in ("closed", "cancelled"):
        raise ValueError(f"Audit is {audit.status}; allocation is locked")
    if not set_owner and not set_auditor:
        raise ValueError("Nothing to allocate — specify an auditee, an auditor, or both")

    ids = set(checkpoint_ids or [])
    targets = [
        r for r in audit.responses
        if (r.id in ids) or (discipline_id is not None and r.categoryId == discipline_id)
    ]
    if not targets:
        raise ValueError("No matching checkpoints to allocate")

    # ── Auditor axis ─────────────────────────────────────────────────────
    auditor_changed = 0
    if set_auditor:
        if auditor_id:
            au = await db.get(User, auditor_id)
            if au is None:
                raise ValueError("Auditor not found")
            if au.plantId and au.plantId != audit.plantId:
                raise ValueError("Auditor belongs to a different plant")
            # Conducting a checkpoint is an auditor act, so the person doing it
            # must not be an auditee on this same engagement. Without this,
            # per-checkpoint allocation would be a way around the segregation
            # rule the team screen enforces.
            if auditor_id in {a.get("userId") for a in (audit.auditees or []) if isinstance(a, dict)}:
                raise ValueError(
                    f"{au.name} is an auditee on this audit and cannot also conduct checkpoints."
                )
        for r in targets:
            if r.workflowState == "FINALIZED":
                continue
            # Null falls back to the lead, who covers everything not explicitly
            # assigned — leaving it null would strand the row with no conductor.
            new_auditor = auditor_id or audit.leadAuditorUserId
            if r.assignedAuditorId != new_auditor:
                r.assignedAuditorId = new_auditor
                auditor_changed += 1
        if not set_owner:
            await db.flush()
            return {
                "ok": True, "updated": auditor_changed, "auditorId": auditor_id,
                "auditorReassignments": auditor_changed,
            }

    owner_name = owner_id
    if owner_id:
        u = await db.get(User, owner_id)
        if u is None:
            raise ValueError("Owner not found")
        if u.plantId and u.plantId != audit.plantId:
            raise ValueError("Owner belongs to a different plant")
        owner_name = u.name

        # Independence rule 2 — same-engagement exclusivity (docs/cams/09 §2.1.5).
        # This path had NO plausibility guard of any kind, which is how an
        # insurance manager came to own 513 audit checkpoints (F-36). The check
        # is against the auditor side of THIS engagement only; being an auditee
        # here while auditing elsewhere is legitimate and stays allowed.
        scope = await independence.scope_for_audit(db, audit, include_allocation=False)
        conflict = independence.same_engagement_conflict(
            owner_id, scope, assigning_as="AUDITEE"
        )
        if conflict is not None:
            waiver = await independence.active_waiver(
                db, engagement_kind="AUDIT", engagement_id=audit.id, user_id=owner_id
            )
            if waiver is None:
                raise ValueError(f"{owner_name}: {conflict.reason}")

    # Default responder for an in-flight finding that is being unassigned — so
    # it never drops out of every inbox (it would otherwise become un-routable).
    default_owner = audit.plantManagerUserId or audit.leadAuditorUserId

    now = _utcnow()
    updated = 0
    for r in targets:
        if r.assignedOwnerId == owner_id:
            continue
        prev = r.assignedOwnerId
        r.assignedOwnerId = owner_id
        r.assignedById = user.id
        r.assignedAt = now
        if owner_id:
            r.routedToUserId = owner_id  # assignment routes the finding here
        elif r.overallStatus in _PRE_SUBMIT_STATUSES:
            r.routedToUserId = None  # not yet submitted — safe to clear
        else:
            # In-flight (pending_auditee / response_submitted / …): keep it
            # routed to a real responder rather than orphaning it.
            r.routedToUserId = default_owner
        comment = (
            "Unassigned" if not owner_id
            else f"Reassigned to {owner_name}" if prev
            else f"Assigned to {owner_name}"
        )
        await _log_interaction(
            db, instance=r, audit_id=audit.id, actor_id=user.id,
            actor_role=_actor_role_for(user, audit), action="ROUTED_TO_OWNER",
            resulting_state=r.workflowState, comment=comment,
        )
        updated += 1

    await db.flush()
    return {
        "ok": True, "updated": updated, "ownerId": owner_id,
        "auditorId": auditor_id if set_auditor else None,
        "auditorReassignments": auditor_changed,
    }


async def update_audit_team(
    db: AsyncSession, *, user: User, audit_id: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Re-seat the audit team AFTER the audit exists — and re-route the
    checkpoints to match.

    The team used to be fixed at creation, which does not survive contact with a
    real audit. Auditees are very often unknown when the audit is scheduled and
    are only identified at (or after) the opening meeting, once the auditor has
    met the departments and knows who actually owns what. Forcing that
    information to be supplied a week early meant it was either guessed — every
    finding routing to the plant manager — or the audit was cancelled and
    rescheduled to correct the cast.

    Seating someone is only half the job. `assignedAuditorId` and
    `assignedOwnerId` were written onto every checkpoint at materialisation from
    the team as it stood then, so a team change that did not re-route would seat
    a new auditee who owns nothing and leave findings routing to the old one.

    What is deliberately NOT overwritten:

      • Hand-allocated checkpoints (`assignedById` set). Someone opened the
        allocation screen and made a per-checkpoint decision; a discipline-level
        default must not silently undo it. `overrideManualAllocations` opts in.
      • FINALIZED checkpoints. The audit is closed over them and their owner is
        part of the record.

    In-flight findings ARE re-routed, and each carries a ROUTED_TO_OWNER
    interaction, so the thread shows the handover rather than the finding
    silently appearing in a different inbox.
    """
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        raise ValueError("Audit not found")
    if audit.status in ("closed", "cancelled"):
        raise ValueError(f"Audit is {audit.status}; the team is locked")

    override_manual = bool(data.get("overrideManualAllocations"))

    # Who is seated BEFORE anything is reassigned. The JSON columns are
    # overwritten in place below, so this is the only chance to know who is
    # genuinely new — and without it, adding one auditee would re-notify the
    # entire team, which is how a notification channel gets muted.
    from app.services import cams_audit_notifications as _camsnotif

    _team_before = _camsnotif.team_snapshot(audit)

    # Only the keys actually supplied are touched, so a call that names auditees
    # cannot blank the co-auditors it never mentioned.
    co_in = data.get("coAuditors")
    au_in = data.get("auditees")
    pm_in = data.get("plantManagerUserId", ...)

    co_auditors = (
        [
            {"userId": c["userId"] if isinstance(c, dict) else c,
             "disciplineIds": (c.get("disciplineIds") or []) if isinstance(c, dict) else []}
            for c in co_in
        ]
        if co_in is not None else list(audit.coAuditors or [])
    )
    auditees = (
        [
            {"userId": a["userId"] if isinstance(a, dict) else a,
             "responsibleCategories": (a.get("responsibleCategories") or []) if isinstance(a, dict) else []}
            for a in au_in
        ]
        if au_in is not None else list(audit.auditees or [])
    )
    plant_manager = audit.plantManagerUserId if pm_in is ... else (pm_in or None)

    co_ids = [c["userId"] for c in co_auditors if c.get("userId")]
    au_ids = [a["userId"] for a in auditees if a.get("userId")]

    # No duplicate seats — the same person twice in one slot produces two
    # routing rules for one discipline and a silently arbitrary winner.
    for label, ids in (("co-auditor", co_ids), ("auditee", au_ids)):
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            names = await _names_for(db, dupes)
            raise ValueError(f"Duplicate {label}: {', '.join(names)}")

    # Segregation of duties, checked here and not only at creation — otherwise
    # this endpoint is a way around the rule create_audit enforces.
    clash = (set(co_ids) | {audit.leadAuditorUserId}) & set(au_ids)
    if clash:
        names = await _names_for(db, clash)
        raise ValueError(
            f"{', '.join(names)} cannot be both auditor and auditee on the same audit."
        )

    # Seat the PROPOSED team before deriving the scope: `scope_for_audit` reads
    # the audit's own team fields, so checking independence against the stored
    # team would be checking the arrangement we are replacing. A blocking
    # verdict raises, the router turns that into a 400, and the session is
    # rolled back — so nothing assigned here survives a rejection.
    #
    # `include_allocation=False` for the same reason: the per-checkpoint owners
    # still on the rows belong to the OLD cast and are re-routed below.
    audit.coAuditors = co_auditors
    audit.auditees = auditees
    audit.plantManagerUserId = plant_manager

    scope = await independence.scope_for_audit(db, audit, include_allocation=False)
    verdicts = await independence.check_many(
        db, user_ids=[audit.leadAuditorUserId, *co_ids], scope=scope, assigning_as="AUDITOR"
    )
    # Recorded BEFORE the raise, on its own session — a blocked attempt is
    # evidence and has to outlive the transaction that never committed.
    await independence_events.record_verdicts(
        verdicts=verdicts,
        engagement_kind="AUDIT",
        origin="UPDATE_AUDIT_TEAM",
        attempted_by_user_id=user.id,
        site_id=audit.plantId,
    )
    blocked = {uid: v for uid, v in verdicts.items() if v.blocking}
    if blocked:
        ids = list(blocked)
        names = await _names_for(db, ids)
        parts = [f"{n}: {blocked[i].blocking[0].reason}" for i, n in zip(ids, names)]
        raise ValueError("Auditor independence — " + " | ".join(parts))

    # ── Re-route the checkpoints ─────────────────────────────────────────
    now = _utcnow()
    default_owner = plant_manager or audit.leadAuditorUserId
    auditor_changed = owner_changed = 0

    for r in audit.responses:
        if r.workflowState == "FINALIZED":
            continue

        # Who conducts it. Purely a work-split among auditors, with no state
        # attached, so it is always recomputed.
        new_auditor = _route_auditor_for_category(
            r.categoryId, co_auditors, audit.leadAuditorUserId
        )
        if new_auditor != r.assignedAuditorId:
            r.assignedAuditorId = new_auditor
            auditor_changed += 1

        # Who answers for it.
        if r.assignedById and not override_manual:
            continue  # hand-allocated — leave it alone
        new_owner = _route_for_category(r.categoryId, auditees)
        if new_owner == r.assignedOwnerId:
            continue
        prev = r.assignedOwnerId
        r.assignedOwnerId = new_owner
        if new_owner:
            r.routedToUserId = new_owner
        elif r.overallStatus in _PRE_SUBMIT_STATUSES:
            r.routedToUserId = None
        else:
            # An in-flight finding must never be left un-routable.
            r.routedToUserId = default_owner
        owner_changed += 1

        owner_name = (await _names_for(db, [new_owner]))[0] if new_owner else None
        await _log_interaction(
            db, instance=r, audit_id=audit.id, actor_id=user.id,
            actor_role=_actor_role_for(user, audit), action="ROUTED_TO_OWNER",
            resulting_state=r.workflowState,
            comment=(
                "Unassigned by a team change" if not new_owner
                else f"Reassigned to {owner_name} by a team change" if prev
                else f"Assigned to {owner_name} by a team change"
            ),
            at=now,
        )

    await db.flush()

    # Newly seated people get their calendars booked; people already booked are
    # not re-invited, because the sync diffs against what was delivered rather
    # than re-sending the whole cast. This is the half of the client's request
    # that the create-time booking cannot cover on its own — auditees are named
    # here, days after the audit was set.
    from app.services import calendar_booking as _cal

    cal = await _cal.sync_engagement(
        db, engagement_kind="AUDIT", engagement_id=audit.id, actor_id=user.id
    )

    # …and the same diff drives the notification. Only the seats that are new
    # since `_team_before` hear about it. Best-effort, as above.
    fanout = await _camsnotif.notify_team_changed(
        db, audit=audit, before=_team_before, actor_id=user.id
    )
    return {
        "ok": True,
        "coAuditors": len(co_auditors),
        "auditees": len(auditees),
        "checkpointsRerouted": owner_changed,
        "auditorReassignments": auditor_changed,
        "calendarInvitesSent": cal.get("attendeesAdded", 0),
        "calendarInvitesWithdrawn": cal.get("attendeesRemoved", 0),
        "notificationsSent": fanout.get("inApp", 0),
        "emailsSent": fanout.get("email", 0),
    }


async def _names_for(db: AsyncSession, ids) -> list[str]:
    """Display names for an id collection, in the order given. A missing user
    still renders — an audit that names someone must not collapse because the
    account was later removed."""
    ids = list(ids)
    if not ids:
        return []
    rows = (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()
    by_id = {u.id: u.name for u in rows}
    return [by_id.get(i, "Unknown user") for i in ids]


async def my_assigned_checkpoints(
    db: AsyncSession, *, user: User, accessible_plants: list[str] | None = None
) -> dict[str, Any]:
    """Auditee transparency (A-06): every checkpoint assigned to me across all
    audits, in every state, grouped by audit with a personal scorecard. Scoped
    to the caller's accessible plants (mirrors list_audits)."""
    stmt = (
        select(AuditCheckpointResponse, ComplianceAudit)
        .join(ComplianceAudit, AuditCheckpointResponse.auditId == ComplianceAudit.id)
        .where(
            or_(
                AuditCheckpointResponse.routedToUserId == user.id,
                AuditCheckpointResponse.assignedOwnerId == user.id,
            )
        )
        .order_by(ComplianceAudit.scheduledDate.desc())
    )
    if accessible_plants is not None:
        stmt = stmt.where(ComplianceAudit.plantId.in_(accessible_plants))
    rows = (await db.execute(stmt)).all()

    audits_map: dict[str, dict[str, Any]] = {}
    totals = {"total": 0, "needsResponse": 0, "audits": 0}
    for r, a in rows:
        grp = audits_map.get(a.id)
        if grp is None:
            grp = {
                "auditId": a.id, "auditNumber": a.auditNumber, "title": a.title,
                "status": a.status, "plantId": a.plantId, "industryCode": a.industryCode,
                "items": [],
                "scorecard": {"total": 0, "pass": 0, "partial": 0, "fail": 0, "na": 0,
                              "not_assessed": 0, "needsResponse": 0},
            }
            audits_map[a.id] = grp
        needs = r.overallStatus == "pending_auditee"
        d = _response_to_dict(r)
        d["needsResponse"] = needs
        grp["items"].append(d)
        sc = grp["scorecard"]
        sc["total"] += 1
        val = _norm_value((r.auditorResponse or {}).get("value")) if r.auditorResponse else None
        sc[val if val in ("pass", "partial", "fail", "na") else "not_assessed"] += 1
        if needs:
            sc["needsResponse"] += 1
            totals["needsResponse"] += 1
        totals["total"] += 1

    audits = list(audits_map.values())
    totals["audits"] = len(audits)
    return {"audits": audits, "totals": totals}


async def add_template_custom_checkpoint(
    db: AsyncSession, *, user: User, template_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Template-level surface (A-08a): add a custom checkpoint to a discipline of
    a template, forking a new version that future audits pick up as standard."""
    disc_code = payload.get("disciplineId") or payload.get("disciplineCode")
    if not disc_code:
        raise ValueError("disciplineId is required")
    question = (payload.get("question") or "").strip()
    if len(question) < 4:
        raise ValueError("question must be at least 4 characters")
    fork = await _fork_template_with_checkpoint(
        db, user=user, template_id=template_id,
        cp={
            "discipline_code": disc_code,
            "discipline_name": payload.get("disciplineName", ""),
            "question": question,
            "criticality": payload.get("severity") or payload.get("criticality") or "major",
            "guidance": payload.get("guidance", ""),
            "requirement_reference": payload.get("requirementReference", ""),
            "standard": payload.get("standardClauseRef") or payload.get("standard", ""),
            "evidence_required_on_fail": bool(payload.get("evidenceRequiredOnFail")),
        },
    )
    return {
        "ok": True,
        "templateId": fork.id,
        "version": fork.version,
        "parentTemplateId": fork.parentTemplateId,
        "customCheckpointCount": len(fork.customCheckpoints or []),
    }


# ─────────────────────────────────────────────────────────────────────
# Conduct: partial-save + submit
# ─────────────────────────────────────────────────────────────────────


# camelCase payload key -> stored snake_case key, for partial-merge saves.
#
# `auditFindings` is the workbook's column G under its own name. It is an ALIAS
# onto the same observation field the engine already had rather than a second
# store — the auditor's comment on a checkpoint is one thing, and two columns
# holding it would immediately disagree.
_SAVE_KEY_MAP = {
    "value": "value",
    "numericValue": "numeric_value",
    "selectedOptions": "selected_options",
    "textObservation": "text_observation",
    "auditFindings": "text_observation",
    "auditorNotes": "auditor_notes",
    "photos": "photos",
    "evidenceLinks": "evidence_links",
}


async def save_response(db: AsyncSession, *, user: User, audit_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    audit = await _load_audit(db, audit_id)
    if audit is None:
        raise ValueError("Audit not found")
    if audit.status in ("closed", "cancelled"):
        raise ValueError(f"Audit is {audit.status}; checkpoint responses are locked")

    code = payload["checkpointCode"]
    resp = (
        await db.execute(
            select(AuditCheckpointResponse)
            .where(AuditCheckpointResponse.auditId == audit_id)
            .where(AuditCheckpointResponse.checkpointCode == code)
        )
    ).scalar_one_or_none()
    if resp is None:
        raise ValueError(f"Checkpoint {code} not found on this audit")

    now = _utcnow()
    # MERGE only the fields the client actually sent (the router passes
    # exclude_unset), so an observation-only save never wipes the value.
    merged = dict(resp.auditorResponse or {})
    for src, dst in _SAVE_KEY_MAP.items():
        if src in payload:
            merged[dst] = payload[src]
    merged["responded_at"] = now.isoformat()
    merged["is_saved"] = True

    # Page grading (columns C–F, H) — writes the grading columns and returns the
    # engine bucket everything below this line already keys off. It also rewrites
    # merged["value"], so it must run before auditorResponse is handed over.
    val = _apply_page_grading(resp, payload, merged)
    resp.auditorResponse = merged

    if val is not None:
        resp.overallStatus = f"answered_{val}"
        resp.answeredAt = now
    else:
        resp.overallStatus = "not_answered"
        resp.answeredAt = None

    # Mirror the auditor's verdict into the first-class carousel fields so
    # reports + the iteration thread read structured columns rather than
    # re-parsing the auditorResponse JSON blob.
    resp.assessmentStatus = _ASSESS_STATUS.get(val, "NOT_ASSESSED")
    if "text_observation" in merged:
        resp.observation = merged.get("text_observation") or None
    if "photos" in merged:
        resp.auditorEvidenceIds = [
            p.get("storagePath") for p in (merged.get("photos") or []) if isinstance(p, dict) and p.get("storagePath")
        ]

    # Reconcile the iteration state with the (possibly edited) verdict. The
    # auditor's verdict steers OPEN/PASSED checkpoints AND in-flight findings
    # (AWAITING_AUDITEE/AUDITEE_RESPONDED/MORE_INFO_REQUESTED) — so a re-assess
    # after REOPEN works, a late fail enters the thread, and re-passing an
    # in-flight finding closes it out. ESCALATED_PM and the resolved terminals
    # (RESOLVED/ACCEPTED_WITH_CAPA/FINALIZED) are owned by the PM / state machine
    # and are never overridden by a carousel save.
    post_submit = audit.status in ("submitted_pending_response", "response_in_progress", "under_review")
    _IN_FLIGHT = ("AWAITING_AUDITEE", "AUDITEE_RESPONDED", "MORE_INFO_REQUESTED")
    if resp.workflowState in ("OPEN", "PASSED", *_IN_FLIGHT):
        if val in ("pass", "na"):
            if resp.workflowState in _IN_FLIGHT:
                # Re-assessed as compliant — close the in-flight finding.
                resp.routedToUserId = None
                resp.currentRound = 0
                await _log_interaction(db, instance=resp, audit_id=audit.id, actor_id=user.id,
                                       actor_role=_actor_role_for(user, audit), action="AUDITOR_ACCEPT",
                                       resulting_state="PASSED", round=resp.currentRound,
                                       comment="Re-assessed as compliant — finding closed.", at=now)
            resp.workflowState = "PASSED"
        elif val in ("fail", "partial"):
            if resp.workflowState in ("OPEN", "PASSED"):
                if post_submit:
                    # Post-submit reassess routes a finding straight into the
                    # thread with no second submit gate — so enforce the SAME
                    # evidence rule submit_audit applies (an observation, plus a
                    # photo where the checkpoint demands one). Without this the
                    # reopen→fail path would mint an evidence-free finding/CAPA.
                    if not (resp.observation or "").strip():
                        raise ValueError("Audit findings are required before routing a finding.")
                    if resp.requiresPhotoOnFail and not (resp.auditorEvidenceIds or []):
                        raise ValueError("An evidence photo is required for this checkpoint before routing the finding.")
                    if page_grading.requires_risk_grade(resp.gradeAwarded, resp.conformanceMode) and not resp.riskGrade:
                        raise ValueError("A risk grade is required before routing a finding.")
                    owner = resp.assignedOwnerId or resp.routedToUserId or audit.plantManagerUserId or audit.leadAuditorUserId
                    resp.routedToUserId = owner
                    resp.workflowState = "AWAITING_AUDITEE"
                    resp.overallStatus = "pending_auditee"
                    resp.currentRound = 0
                    await _log_interaction(db, instance=resp, audit_id=audit.id, actor_id=user.id,
                                           actor_role=_actor_role_for(user, audit), action="ASSESSED",
                                           resulting_state="AWAITING_AUDITEE", round=0,
                                           comment=(resp.observation or "")[:500] or None, at=now)
                    await _log_interaction(db, instance=resp, audit_id=audit.id, actor_id=user.id,
                                           actor_role=_actor_role_for(user, audit), action="ROUTED_TO_OWNER",
                                           resulting_state="AWAITING_AUDITEE", round=0, at=now + timedelta(milliseconds=1))
                    await _notify(db, owner, f"Audit {audit.auditNumber}: finding assigned to you",
                                  f"Checkpoint {code} needs your response.")
                else:
                    resp.workflowState = "OPEN"  # pre-submit — submit_audit will route it
            # already in-flight and still fail/partial → leave the thread untouched
        elif resp.workflowState in ("OPEN", "PASSED"):  # verdict cleared
            resp.workflowState = "OPEN"

    # Coherence guard: the unconditional overallStatus rewrite above reflects the
    # raw verdict (answered_fail, …) — but an in-flight finding's overallStatus is
    # owned by the auditee workflow, not the verdict. Re-snap it so a verdict edit
    # on a routed finding can never strand it out of pending_auditee /
    # response_submitted (which would break auditee_respond + the needs-response
    # inbox count). Re-pass already moved it to PASSED, so it's excluded here.
    if resp.workflowState in ("AWAITING_AUDITEE", "MORE_INFO_REQUESTED"):
        resp.overallStatus = "pending_auditee"
    elif resp.workflowState == "AUDITEE_RESPONDED":
        resp.overallStatus = "response_submitted"

    # Flip the audit into conduct on first save.
    if audit.status == "scheduled":
        audit.status = "in_progress"
        if audit.actualStartAt is None:
            audit.actualStartAt = now

    await db.flush()

    # Recompute the answered counter (one indexed count — avoids drift).
    answered = (
        await db.execute(
            select(func.count(AuditCheckpointResponse.id))
            .where(AuditCheckpointResponse.auditId == audit_id)
            .where(AuditCheckpointResponse.overallStatus.notlike("not_answered"))
        )
    ).scalar_one() or 0
    audit.answeredCheckpoints = answered
    await db.flush()

    return {"ok": True, "checkpointCode": code, "overallStatus": resp.overallStatus, "answered": answered}


async def submit_audit(db: AsyncSession, *, user: User, audit_id: str) -> dict[str, Any]:
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        raise ValueError("Audit not found")
    if audit.status not in ("scheduled", "in_progress"):
        raise ValueError(f"Audit cannot be submitted from status '{audit.status}'")

    # Enforce the "observation/evidence required on fail/partial" rule the
    # carousel shows — every fail/partial needs an observation, and a photo
    # where the checkpoint demands it. Otherwise the finding (and any auto-CAPA)
    # carries no substance.
    missing: list[str] = []
    for r in audit.responses:
        ar = r.auditorResponse or {}
        v = _norm_value(ar.get("value"))
        if v in ("fail", "partial"):
            if not (ar.get("text_observation") or "").strip():
                missing.append(f"{r.checkpointCode} (audit findings)")
            elif r.requiresPhotoOnFail and not (ar.get("photos") or []):
                missing.append(f"{r.checkpointCode} (evidence photo)")
            # A finding with no assessed risk cannot be prioritised by the
            # auditee or the plant head, and it is the one column of the
            # workbook that nothing else can supply — on the internal-audit
            # form. The IMS/EnMS department form has no risk column at all, so
            # `requires_risk_grade` returns False for those rows and the gate
            # does not apply; see services/page_grading.
            elif page_grading.requires_risk_grade(r.gradeAwarded, r.conformanceMode) and not r.riskGrade:
                missing.append(f"{r.checkpointCode} (risk grade)")
    if missing:
        head = ", ".join(missing[:8])
        more = f" + {len(missing) - 8} more" if len(missing) > 8 else ""
        raise ValueError(f"{len(missing)} graded checkpoint(s) are incomplete: {head}{more}")

    now = _utcnow()
    capa_count = 0
    capa_failures: list[dict[str, Any]] = []
    routed_owners: set[str] = set()
    # Resolved ONCE for the whole submit — a 1,500-checkpoint audit can spawn
    # dozens of CAPAs and this must not become a per-checkpoint lookup.
    capa_subject = await _capa_subject(db, audit)
    for r in audit.responses:
        val = _norm_value((r.auditorResponse or {}).get("value")) if r.auditorResponse else None
        if val in ("fail", "partial"):
            r.overallStatus = "pending_auditee"
            # Route to the allocated owner; an unassigned finding routes to a
            # default (plant manager / lead) but leaves assignedOwnerId null so
            # the allocation UI still flags it "unassigned".
            owner = r.assignedOwnerId or r.routedToUserId or audit.plantManagerUserId or audit.leadAuditorUserId
            r.routedToUserId = owner
            # Open the iteration thread: the auditor's finding, then the route.
            # Distinct timestamps keep ASSESSED strictly before ROUTED_TO_OWNER
            # (both are logged in this one transaction, so server now() ties).
            r.workflowState = "AWAITING_AUDITEE"
            r.currentRound = 0
            t = _utcnow()
            await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id,
                                   actor_role=_actor_role_for(user, audit), action="ASSESSED",
                                   resulting_state="AWAITING_AUDITEE", round=0,
                                   comment=(r.observation or "")[:500] or None, at=t)
            await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id,
                                   actor_role=_actor_role_for(user, audit), action="ROUTED_TO_OWNER",
                                   resulting_state="AWAITING_AUDITEE", round=0, at=t + timedelta(milliseconds=1))
            if owner:
                routed_owners.add(owner)
        elif val in ("pass", "na"):
            r.workflowState = "PASSED"
        if val == "fail" and r.autoTriggerCapaOnFail:
            spawned = await _spawn_capa(
                db, user=user, audit=audit, response=r,
                subject=capa_subject, subject_resolved=True,
            )
            if spawned:
                r.workflowState = "ACCEPTED_WITH_CAPA"
                capa_count += 1
            else:
                # The silent failure this replaces was the worst of the three:
                # an audit could close with critical non-conformances and NO
                # corrective action raised, and nothing anywhere said so. The
                # submit still succeeds — blocking it would strand a completed
                # on-site audit over a permission gap — but the caller is told
                # exactly which checkpoints did not get their CAPA and why.
                capa_failures.append({
                    "checkpointCode": r.checkpointCode,
                    "categoryName": r.categoryName,
                    "criticality": r.criticality,
                    "reason": getattr(r, "capaSpawnError", None) or "unknown error",
                })

    for owner_id in routed_owners:
        await _notify(db, owner_id, f"Audit {audit.auditNumber}: findings assigned to you",
                      f"Audit '{audit.title}' was submitted. Findings routed to you await your response.")

    score = _compute_score(audit, audit.responses)
    audit.score = score
    audit.overallCompliancePct = score["overall_score_pct"]
    audit.auditPassed = score["audit_passed"]
    audit.criticalFailureCount = score["critical_failures"]
    audit.openCapaCount = (audit.openCapaCount or 0) + capa_count
    audit.status = "submitted_pending_response"
    audit.submittedAt = now
    audit.actualEndAt = now
    # A critical failure that owed a CAPA and did not get one is a gap in the
    # RECORD, so the auditor who submitted must be told at the moment they can
    # still act on it — not discover it when the report prints no CAPA number.
    if capa_failures:
        await _notify(
            db, audit.leadAuditorUserId,
            f"Audit {audit.auditNumber}: {len(capa_failures)} CAPA(s) were not raised",
            "These checkpoints failed and should have raised a corrective action: "
            + ", ".join(f["checkpointCode"] for f in capa_failures)
            + f". Reason: {capa_failures[0]['reason']}",
        )
    await db.flush()
    return {
        "ok": True, "status": audit.status, "capasSpawned": capa_count, "score": score,
        "capaFailures": capa_failures,
    }


async def _capa_subject(db: AsyncSession, audit: ComplianceAudit) -> dict[str, Any] | None:
    """The supplier context a CAPA needs, or None for an own-facility audit.

    Resolved once per submit rather than per failed checkpoint — a 1,500-CP
    audit can spawn dozens of CAPAs and this must not become a per-row query.
    """
    from app.models.cams_completion import SupplierAuditLink
    from app.services import vendors as vendor_svc

    link = (
        await db.execute(
            select(SupplierAuditLink).where(
                SupplierAuditLink.engagementKind == "AUDIT",
                SupplierAuditLink.engagementId == audit.id,
            )
        )
    ).scalars().first()
    if link is None:
        return None
    v = await vendor_svc.get_vendor(db, link.vendorProfileId)
    return {
        "vendorProfileId": link.vendorProfileId,
        "vendorCode": v.vendorCode if v else None,
        "legalName": v.legalName if v else "Unknown vendor",
        "relationshipOwnerId": v.relationshipOwnerId if v else None,
        "supplierContactName": link.supplierContactName,
        "supplierContactEmail": link.supplierContactEmail,
    }


async def _spawn_capa(
    db: AsyncSession,
    *,
    user: User,
    audit: ComplianceAudit,
    response: AuditCheckpointResponse,
    subject: dict[str, Any] | None = None,
    subject_resolved: bool = False,
) -> bool:
    """Auto-spawn a CAPA from a critical checkpoint failure. Best-effort:
    wrapped in a SAVEPOINT so a CAPA failure never blocks the audit submit.

    `subject_resolved=True` means the caller already looked the supplier up (and
    `subject=None` genuinely means own-facility). Without that flag a None would
    be ambiguous between "own facility" and "not looked up yet", and the loop in
    `submit_audit` would re-query per checkpoint.

    On failure the reason is recorded on `response.capaSpawnError` (transient,
    not a column — read by the callers below) instead of being lost to stderr.
    A blanket "please retry" was actively misleading: the commonest failure here
    is the actor lacking CAPA.CREATE, which no amount of retrying fixes.
    """
    response.capaSpawnError = None  # type: ignore[attr-defined]
    if response.capa and response.capa.get("capa_id"):
        return False  # already linked
    if not subject_resolved:
        subject = await _capa_subject(db, audit)
    try:
        async with db.begin_nested():
            from app.routers.capa import create_capa
            from app.schemas.capa import CapaCreate

            obs = (response.auditorResponse or {}).get("text_observation", "")
            # The auditor's Risk Grade (column H) beats the checkpoint's
            # configured criticality here: criticality was set before anyone
            # visited the site, the risk grade is what they concluded having
            # seen the actual finding. Falls back to the configured severity
            # when no risk grade was captured.
            severity = page_grading.capa_severity(
                response.riskGrade,
                _CAPA_SEVERITY.get(response.capaSeverity or response.criticality, "MODERATE"),
            )
            problem = (
                f"Audit {audit.auditNumber} ({audit.auditType}) — checkpoint {response.checkpointCode} "
                f"in category '{response.categoryName}' failed. Question: {response.checkpointQuestion} "
                f"Auditor observation: {obs or 'see audit record'}. "
                f"Requirement: {response.requirementReference or 'n/a'}."
            )
            # ── Who answers for this finding (WP-45) ──────────────────────
            #
            # On a supplier audit the corrective action is the SUPPLIER's, but
            # the supplier has no platform seat — so the CAPA owner is the
            # internal relationship owner, who is accountable for chasing it.
            # An unowned CAPA is one nobody is measured on; assigning it to a
            # party who cannot log in is the same thing with extra steps.
            #
            # `routedToUserId` is deliberately NOT used for a supplier audit:
            # it points at the internal auditee the checkpoint was routed to,
            # which is a meaningful owner for our own factory and a wrong one
            # for someone else's.
            if subject:
                source_code = "AUDIT_EXTERNAL"
                owner = (
                    subject.get("relationshipOwnerId")
                    or audit.plantManagerUserId
                    or audit.leadAuditorUserId
                )
                title = f"Supplier finding ({subject['legalName']}): {response.checkpointQuestion[:70]}"
                vendor_code = subject.get("vendorCode")
                party = subject["legalName"] + (f" ({vendor_code})" if vendor_code else "")
                problem += (
                    f" Audited party: {party} — corrective action is owed by the "
                    "supplier and tracked by the internal relationship owner."
                )
            else:
                source_code = "AUDIT_INTERNAL"
                owner = (
                    response.routedToUserId
                    or audit.plantManagerUserId
                    or audit.leadAuditorUserId
                )
                title = f"Audit finding: {response.checkpointQuestion[:90]}"

            payload = CapaCreate(
                plantId=audit.plantId,
                sourceTypeCode=source_code,
                sourceReferenceId=response.id,
                sourceReferenceUrl=f"/cams/audits/{audit.id}",
                sourceReferenceSummary=f"Audit {audit.auditNumber} — {response.checkpointCode} failed",
                sourceMetadata={
                    "auditNumber": audit.auditNumber,
                    "checkpointCode": response.checkpointCode,
                    "criticality": response.criticality,
                    "categoryName": response.categoryName,
                    **(
                        {
                            "subjectType": "VENDOR",
                            "vendorProfileId": subject.get("vendorProfileId"),
                            "vendorCode": subject.get("vendorCode"),
                            "supplierLegalName": subject.get("legalName"),
                            "supplierContactEmail": subject.get("supplierContactEmail"),
                        }
                        if subject
                        else {"subjectType": "OWN_SITE"}
                    ),
                },
                title=title,
                problemDescription=problem,
                detectedAt=audit.actualStartAt or _utcnow(),
                primaryCategory="Audit / Compliance Finding",
                severity=severity,
                priority=severity,
                primaryOwnerUserId=owner,
            )
            capa = await create_capa(payload, user=user, db=db)
            response.capa = {
                "auto_triggered": True,
                "capa_id": capa.id,
                "capa_number": capa.capaNumber,
                "capa_status": capa.state,
                "capa_due_date": _iso(capa.closureTargetDate),
            }
        return True
    except Exception as e:  # noqa: BLE001
        # Keep the reason. A permission gap and a database blip both used to
        # arrive as the same "please retry", and only one of them is worth
        # retrying — telling them apart is the whole point of this branch.
        from fastapi import HTTPException as _HTTPException

        if isinstance(e, _HTTPException) and e.status_code in (401, 403):
            reason = (
                f"You do not have permission to raise a CAPA at this plant "
                f"(CAPA.CREATE). Ask an admin to grant it in Configuration → Roles, "
                f"or have someone who holds it raise the CAPA for "
                f"{response.checkpointCode}."
            )
        elif isinstance(e, _HTTPException):
            reason = f"CAPA could not be created: {e.detail}"
        else:
            reason = f"CAPA could not be created: {type(e).__name__}: {e}"
        response.capaSpawnError = reason  # type: ignore[attr-defined]
        print(f"Auto-CAPA spawn failed for {response.checkpointCode}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return False


# ─────────────────────────────────────────────────────────────────────
# Auditee response + plant-manager review + close
# ─────────────────────────────────────────────────────────────────────


async def auditee_respond(db: AsyncSession, *, user: User, audit_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    audit = await _load_audit(db, audit_id)
    if audit is None:
        raise ValueError("Audit not found")
    code = payload["checkpointCode"]
    resp = (
        await db.execute(
            select(AuditCheckpointResponse)
            .where(AuditCheckpointResponse.auditId == audit_id)
            .where(AuditCheckpointResponse.checkpointCode == code)
        )
    ).scalar_one_or_none()
    if resp is None:
        raise ValueError(f"Checkpoint {code} not found")
    if resp.overallStatus != "pending_auditee":
        raise ValueError(f"Checkpoint {code} is not awaiting an auditee response (status: {resp.overallStatus})")
    if resp.workflowState not in ("AWAITING_AUDITEE", "MORE_INFO_REQUESTED"):
        raise ValueError(f"Checkpoint {code} is not awaiting a response (state: {resp.workflowState})")
    # SoD: only the routed owner may respond (same guard as the transition path).
    owner_ids = {resp.assignedOwnerId, resp.routedToUserId} - {None}
    if owner_ids and user.id not in owner_ids:
        raise ValueError("This checkpoint is routed to a different owner")
    if len((payload.get("actionTaken") or payload.get("responseText") or "").strip()) < 3:
        raise ValueError("Describe the action taken (at least 3 characters)")

    now = _utcnow()
    resp.auditeeResponse = {
        "respondent_user_id": user.id,
        "response_text": payload.get("responseText", ""),
        "action_taken": payload.get("actionTaken", ""),
        "action_date": payload.get("actionDate"),
        "estimated_closure_date": payload.get("estimatedClosureDate"),
        "photos": payload.get("photos") or [],
        "responded_at": now.isoformat(),
        "status": "responded",
        "round": resp.currentRound,
    }
    resp.overallStatus = "response_submitted"
    resp.workflowState = "AUDITEE_RESPONDED"  # keep the state machine in sync
    await _log_interaction(db, instance=resp, audit_id=audit.id, actor_id=user.id,
                           actor_role="AUDITEE", action="AUDITEE_RESPONSE", resulting_state="AUDITEE_RESPONDED",
                           comment=(payload.get("actionTaken") or payload.get("responseText") or "")[:500] or None,
                           round=resp.currentRound)
    await _notify(db, audit.leadAuditorUserId, f"Audit {audit.auditNumber}: response submitted",
                  f"Checkpoint {code} has an auditee response awaiting review.")

    if audit.status in ("submitted_pending_response",):
        audit.status = "response_in_progress"
    await db.flush()
    return {"ok": True, "checkpointCode": code, "overallStatus": resp.overallStatus}


async def pm_review(db: AsyncSession, *, user: User, audit_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        raise ValueError("Audit not found")
    code = payload["checkpointCode"]
    decision = payload["decision"]  # accepted | rejected
    if decision not in ("accepted", "rejected"):
        raise ValueError("decision must be 'accepted' or 'rejected'")
    resp = next((r for r in audit.responses if r.checkpointCode == code), None)
    if resp is None:
        raise ValueError(f"Checkpoint {code} not found")
    if resp.overallStatus != "response_submitted":
        raise ValueError(f"Checkpoint {code} has no submitted response to review (status: {resp.overallStatus})")
    # Legacy PM review only acts on an escalated checkpoint — the auditor reviews
    # first (AUDITEE_RESPONDED) and may escalate; the PM never resolves a
    # non-escalated finding (that would bypass the auditor's review).
    if resp.workflowState != "ESCALATED_PM":
        raise ValueError(f"Checkpoint {code} is not escalated for plant-manager decision (state: {resp.workflowState})")
    # SoD: the PM can't decide on a response they themselves authored.
    if (resp.auditeeResponse or {}).get("respondent_user_id") == user.id:
        raise ValueError("You can't decide on your own auditee response")

    now = _utcnow()
    resp.plantManagerReview = {
        "reviewer_user_id": user.id,
        "decision": decision,
        "comments": payload.get("comments", ""),
        "reviewed_at": now.isoformat(),
    }
    if decision == "accepted":
        resp.overallStatus = "response_accepted"
        resp.workflowState = "RESOLVED"
        await _log_interaction(db, instance=resp, audit_id=audit.id, actor_id=user.id,
                               actor_role="PLANT_MANAGER", action="PM_DECISION", resulting_state="RESOLVED",
                               comment=payload.get("comments") or None, round=resp.currentRound)
    else:
        resp.overallStatus = "pending_auditee"
        resp.currentRound += 1
        resp.workflowState = "MORE_INFO_REQUESTED"
        if resp.auditeeResponse:
            resp.auditeeResponse = {**resp.auditeeResponse, "status": "rejected"}
        await _log_interaction(db, instance=resp, audit_id=audit.id, actor_id=user.id,
                               actor_role="PLANT_MANAGER", action="PM_DECISION", resulting_state="MORE_INFO_REQUESTED",
                               comment=payload.get("comments") or None, round=resp.currentRound)
        await _notify(db, resp.routedToUserId, f"Audit {audit.auditNumber}: more information requested",
                      f"Checkpoint {code} was sent back — round {resp.currentRound}.")

    audit.status = "under_review"
    audit.score = _compute_score(audit, audit.responses)
    await db.flush()
    return {"ok": True, "checkpointCode": code, "decision": decision}


async def close_audit(db: AsyncSession, *, user: User, audit_id: str, closing_remarks: str = "") -> dict[str, Any]:
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        raise ValueError("Audit not found")
    if audit.status not in ("submitted_pending_response", "response_in_progress", "under_review"):
        raise ValueError(f"Audit cannot be closed from status '{audit.status}' — it must be submitted first")

    # Finalization gate (audit-lifecycle v2): every checkpoint must be terminal.
    fin = _finalizability(audit)
    if not fin["finalizable"]:
        raise ValueError(
            f"{fin['blockerCount']} checkpoint(s) still in review — resolve every checkpoint before closing"
        )

    # WP-41 sign-off gate (docs/cams/09 §3.1). The finalizability gate proves
    # the WORK is done; this proves someone ACCEPTED it. Without it, "closed"
    # means only that the auditor stopped typing — a certification body expects
    # a named lead auditor and a named auditee owner to have signed.
    _so = await signoff.signoff_status(db, audit)
    if not _so["canClose"]:
        raise ValueError(_so["statement"])

    now = _utcnow()
    score = _compute_score(audit, audit.responses)
    audit.score = score
    audit.overallCompliancePct = score["overall_score_pct"]
    audit.auditPassed = score["audit_passed"]
    audit.criticalFailureCount = score["critical_failures"]
    # Lock every checkpoint into FINALIZED.
    for r in audit.responses:
        r.workflowState = "FINALIZED"
        r.finalizedAt = now
    audit.status = "closed"
    audit.actualEndAt = audit.actualEndAt or now
    audit.closedAt = now
    if closing_remarks:
        audit.closingRemarks = closing_remarks
    await db.flush()

    # Release any audit time still in the future. A closed audit that leaves
    # nine people blocked for a window nobody intends to use is how a booking
    # feature earns the reputation of being something to work around. Bookings
    # that have already started are left alone — they are the record that the
    # time was held.
    from app.services import calendar_booking as _cal

    await _cal.sync_engagement(
        db, engagement_kind="AUDIT", engagement_id=audit.id, actor_id=user.id
    )

    out: dict[str, Any] = {"ok": True, "status": "closed", "score": score}

    # ── WP-45: the audit result becomes vendor risk evidence ──────────────
    #
    # This is the point of connecting the two modules. Without it a supplier
    # audit closes, produces a PDF, and the vendor's risk band still reflects a
    # desk review from six months ago — the cross-module claim the product
    # makes would be false.
    #
    # Best-effort by design: a vendor write-back problem must never leave an
    # audit stuck open. The failure is reported in the response rather than
    # swallowed, so the caller can see it happened.
    supplier = await _capa_subject(db, audit)
    if supplier:
        try:
            from app.services import vendors as vendor_svc

            res = await vendor_svc.record_audit_assessment(
                db,
                vendor_id=supplier["vendorProfileId"],
                audit_code=audit.auditNumber,
                audit_id=audit.id,
                compliance_pct=audit.overallCompliancePct,
                critical_failures=audit.criticalFailureCount or 0,
                audit_passed=audit.auditPassed,
                assessor_id=user.id,
                assessment_date=now,
            )
            out["vendorAssessment"] = res.as_dict()
        except Exception as e:  # noqa: BLE001
            print(f"Vendor write-back failed for {audit.auditNumber}: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            out["vendorAssessment"] = {"written": False, "reason": f"Write-back failed: {e}"}

    return out


# ─────────────────────────────────────────────────────────────────────
# Iteration state machine (A-05) — multi-round auditor ↔ auditee ↔ PM.
# ─────────────────────────────────────────────────────────────────────

# action -> states it is valid from (server-side guard; the router also
# permission-gates by role).
_ACTION_FROM = {
    "AUDITEE_RESPOND": {"AWAITING_AUDITEE", "MORE_INFO_REQUESTED"},
    "ACCEPT": {"AUDITEE_RESPONDED"},
    "REQUEST_MORE_INFO": {"AUDITEE_RESPONDED"},
    "RAISE_CAPA": {"AUDITEE_RESPONDED"},
    "ESCALATE": {"AUDITEE_RESPONDED"},
    "PM_ACCEPT": {"ESCALATED_PM"},
    "PM_RAISE_CAPA": {"ESCALATED_PM"},
    "PM_SEND_BACK": {"ESCALATED_PM"},
    "REOPEN": {"PASSED"},
}


def pm_decision_block_reason(audit, user_id: str) -> str | None:
    """Why `user_id` may NOT take the plant manager's decision, or None.

    Escalation exists to move a disputed finding to someone who did not raise
    it. The router gates `PM_*` on AUDIT_COMPLIANCE.APPROVE, but that permission
    is held at OWN_PLANT scope by every HSE manager at the site — including the
    co-auditor who conducted the very discipline being escalated. The observed
    failure: a finding escalated for the plant manager's decision was accepted
    by the OHS co-auditor who raised it, and `plantManagerReview` then recorded
    him as the reviewing plant manager. A permission cannot express "someone
    other than the person who raised this", so identity is checked here.

    Two rules, in this order, because they fail for different reasons and the
    caller needs to be told which:

      1. **Independence.** The audit team can never review its own escalation.
         Checked FIRST — if the same person is somehow both auditor and
         designated reviewer, the honest answer is "you conducted this audit",
         not "go reassign the reviewer" (to themselves).
      2. **Identity.** When a reviewer IS designated, they are the reviewer.
         Anyone else holding APPROVE at the plant is not this audit's reviewer.

    With no reviewer designated (`plantManagerUserId` is optional) rule 2 has
    nothing to compare against and the permission gate stands alone — but rule 1
    still applies.
    """
    auditor_ids = {audit.leadAuditorUserId, *_coauditor_ids(audit.coAuditors)} - {None}
    if user_id in auditor_ids:
        return (
            "You conducted this audit, so you cannot take the plant manager's "
            "decision on a finding escalated out of it. Escalation exists to "
            "reach a reviewer independent of the audit team."
        )
    if audit.plantManagerUserId and user_id != audit.plantManagerUserId:
        return (
            "Only the plant manager assigned to this audit can decide an "
            "escalated finding. Reassign the reviewer on the audit team if this "
            "needs to move to someone else."
        )
    return None


async def transition_checkpoint(
    db: AsyncSession, *, user: User, audit_id: str, checkpoint_id: str, action: str, payload: dict[str, Any],
) -> dict[str, Any]:
    """Single state-machine dispatcher for the iteration thread. Validates the
    current workflowState allows `action`, performs it, appends an immutable
    interaction, increments the round on every send-back, spawns AUDIT-source
    CAPA on RAISE_CAPA, and fires best-effort handoff notifications."""
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        raise ValueError("Audit not found")
    if audit.status in ("closed", "cancelled"):
        raise ValueError(f"Audit is {audit.status}; checkpoint actions are locked")

    r = next((x for x in audit.responses if x.id == checkpoint_id or x.checkpointCode == checkpoint_id), None)
    if r is None:
        raise ValueError("Checkpoint not found on this audit")

    valid_from = _ACTION_FROM.get(action)
    if valid_from is None:
        raise ValueError(f"Unknown action '{action}'")
    if r.workflowState not in valid_from:
        raise ValueError(f"Action '{action}' is not allowed from state '{r.workflowState}'")

    # Segregation of duties. The auditee response must come from the routed
    # owner (when one exists); the auditor review must not be done by the same
    # person who wrote the response being reviewed.
    if action == "AUDITEE_RESPOND":
        owner_ids = {r.assignedOwnerId, r.routedToUserId} - {None}
        if owner_ids and user.id not in owner_ids:
            raise ValueError("This checkpoint is routed to a different owner")
    if action in ("ACCEPT", "REQUEST_MORE_INFO", "RAISE_CAPA", "ESCALATE",
                  "PM_ACCEPT", "PM_RAISE_CAPA", "PM_SEND_BACK"):
        responder = (r.auditeeResponse or {}).get("respondent_user_id")
        if responder and responder == user.id:
            raise ValueError("You can't review your own auditee response")

    if action in ("PM_ACCEPT", "PM_RAISE_CAPA", "PM_SEND_BACK"):
        _blocked = pm_decision_block_reason(audit, user.id)
        if _blocked:
            raise ValueError(_blocked)

    # Min-length parity with the client forms (server is the real gate).
    if action == "AUDITEE_RESPOND" and len((payload.get("actionTaken") or payload.get("comment") or "").strip()) < 3:
        raise ValueError("Describe the action taken (at least 3 characters)")
    if action in ("REQUEST_MORE_INFO", "PM_SEND_BACK") and len((payload.get("comment") or "").strip()) < 3:
        raise ValueError("A note (at least 3 characters) is required")

    now = _utcnow()
    comment = (payload.get("comment") or "").strip() or None
    evidence_ids = payload.get("evidenceIds") or []
    photos = payload.get("photos") or []

    if action == "AUDITEE_RESPOND":
        r.auditeeResponse = {
            "respondent_user_id": user.id,
            "response_text": comment or "",
            "action_taken": payload.get("actionTaken") or comment or "",
            "action_date": payload.get("actionDate"),
            "estimated_closure_date": payload.get("estimatedClosureDate"),
            "photos": photos,
            "responded_at": now.isoformat(),
            "status": "responded",
            "round": r.currentRound,
        }
        if evidence_ids:
            r.auditeeEvidenceIds = list(dict.fromkeys((r.auditeeEvidenceIds or []) + evidence_ids))
        r.workflowState = "AUDITEE_RESPONDED"
        r.overallStatus = "response_submitted"
        await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id, actor_role="AUDITEE",
                               action="AUDITEE_RESPONSE", resulting_state="AUDITEE_RESPONDED",
                               comment=comment, evidence_ids=evidence_ids, round=r.currentRound)
        await _notify(db, audit.leadAuditorUserId, f"Audit {audit.auditNumber}: response submitted",
                      f"Checkpoint {r.checkpointCode} awaits your review.")

    elif action == "ACCEPT":
        r.workflowState = "RESOLVED"
        r.overallStatus = "response_accepted"
        await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id,
                               actor_role=_actor_role_for(user, audit), action="AUDITOR_ACCEPT",
                               resulting_state="RESOLVED", comment=comment, round=r.currentRound)

    elif action == "REQUEST_MORE_INFO":
        r.currentRound += 1
        r.workflowState = "MORE_INFO_REQUESTED"
        r.overallStatus = "pending_auditee"
        await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id,
                               actor_role=_actor_role_for(user, audit), action="REQUEST_MORE_INFO",
                               resulting_state="MORE_INFO_REQUESTED", comment=comment, round=r.currentRound)
        await _notify(db, r.routedToUserId, f"Audit {audit.auditNumber}: more information requested",
                      f"Checkpoint {r.checkpointCode} needs more information — round {r.currentRound}.")

    elif action in ("RAISE_CAPA", "PM_RAISE_CAPA"):
        spawned = await _spawn_capa(db, user=user, audit=audit, response=r)
        if not spawned:
            # Never mint a CAPA-less ACCEPTED_WITH_CAPA terminal — fail the
            # action (mirrors submit_audit's `if spawned` guard). The reason is
            # passed through verbatim: "please retry" was the message shown when
            # the actor simply lacked CAPA.CREATE, sending them round a loop that
            # could never succeed.
            raise ValueError(
                getattr(r, "capaSpawnError", None)
                or "Could not raise a CAPA for this checkpoint — please retry"
            )
        r.capaId = (r.capa or {}).get("capa_id")
        r.workflowState = "ACCEPTED_WITH_CAPA"
        r.overallStatus = "response_accepted"
        if action == "PM_RAISE_CAPA":
            r.plantManagerReview = {"reviewer_user_id": user.id, "decision": "capa",
                                    "comments": comment or "", "reviewed_at": now.isoformat()}
            role, act = "PLANT_MANAGER", "PM_DECISION"
        else:
            role, act = _actor_role_for(user, audit), "RAISE_CAPA"
        await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id, actor_role=role,
                               action=act, resulting_state="ACCEPTED_WITH_CAPA",
                               comment=comment or (f"CAPA {r.capaId}" if r.capaId else None), round=r.currentRound)

    elif action == "ESCALATE":
        r.workflowState = "ESCALATED_PM"
        await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id,
                               actor_role=_actor_role_for(user, audit), action="ESCALATE_PM",
                               resulting_state="ESCALATED_PM", comment=comment, round=r.currentRound)
        await _notify(db, audit.plantManagerUserId, f"Audit {audit.auditNumber}: checkpoint escalated",
                      f"Checkpoint {r.checkpointCode} was escalated for your decision.")

    elif action == "PM_ACCEPT":
        r.workflowState = "RESOLVED"
        r.overallStatus = "response_accepted"
        r.plantManagerReview = {"reviewer_user_id": user.id, "decision": "accepted",
                                "comments": comment or "", "reviewed_at": now.isoformat()}
        await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id, actor_role="PLANT_MANAGER",
                               action="PM_DECISION", resulting_state="RESOLVED", comment=comment, round=r.currentRound)

    elif action == "PM_SEND_BACK":
        r.currentRound += 1
        r.workflowState = "MORE_INFO_REQUESTED"
        r.overallStatus = "pending_auditee"
        r.plantManagerReview = {"reviewer_user_id": user.id, "decision": "send_back",
                                "comments": comment or "", "reviewed_at": now.isoformat()}
        await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id, actor_role="PLANT_MANAGER",
                               action="PM_DECISION", resulting_state="MORE_INFO_REQUESTED",
                               comment=comment, round=r.currentRound)
        await _notify(db, r.routedToUserId, f"Audit {audit.auditNumber}: sent back",
                      f"Checkpoint {r.checkpointCode} was sent back for more work — round {r.currentRound}.")

    elif action == "REOPEN":
        if not comment:
            raise ValueError("A reason is required to reopen a passed checkpoint")
        r.workflowState = "OPEN"
        r.finalizedAt = None
        # Reset the verdict so the reopened checkpoint is non-terminal and the
        # finalization gate actually blocks until it is re-assessed.
        r.assessmentStatus = "NOT_ASSESSED"
        r.overallStatus = "not_answered"
        await _log_interaction(db, instance=r, audit_id=audit.id, actor_id=user.id,
                               actor_role=_actor_role_for(user, audit), action="REOPEN",
                               resulting_state="OPEN", comment=comment, round=r.currentRound)

    audit.score = _compute_score(audit, audit.responses)
    await db.flush()
    return {
        "ok": True,
        "checkpointCode": r.checkpointCode,
        "workflowState": r.workflowState,
        "currentRound": r.currentRound,
        "overallStatus": r.overallStatus,
    }


# ─────────────────────────────────────────────────────────────────────
# Reports (A-07) — Interim + Final, immutable snapshots.
# ─────────────────────────────────────────────────────────────────────


def _canonical_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _result_label(score: dict[str, Any]) -> str:
    if score["critical_failures"] > 0:
        return "CRITICAL_NC"
    if score["major_failures"] > 0:
        return "MAJOR_NC"
    if score["minor_failures"] > 0 or score["partially_passed"] > 0:
        return "MINOR_NC"
    return "CONFORMING"


def _standards_rollup(responses: list[AuditCheckpointResponse]) -> list[dict[str, Any]]:
    """Aggregate conformance by standard (SA8000 / ISO 45001 / …) for the final.

    Scored on POINTS, like every other percentage this product prints. It used
    to use the engine's superseded pass-ratio, so the same audit could report a
    standard at one number here and a discipline at another number computed a
    different way two sections earlier.
    """
    agg: dict[str, dict[str, int]] = {}
    for r in responses:
        # One IMS checkpoint is assessed against ISO 9001, 14001 and 45001 at
        # once, so it counts toward EACH of them. `standard` is the display join
        # of those three; aggregating on that string would invent a fourth
        # standard named "ISO 9001:2015 · ISO 14001:2015 · ISO 45001:2018" and
        # report the three real ones as absent — on the one report a
        # certification body reads per standard.
        #
        # Note this makes the totals here sum to MORE than the checkpoint count
        # on a multi-standard audit, which is correct: a line assessed against
        # three standards is three standards' worth of evidence.
        stds = [
            (c.get("standard") or "").strip()
            for c in (r.standardClauses or [])
            if (c.get("standard") or "").strip()
        ] or [(r.standard or "").strip()]
        val = _norm_value((r.auditorResponse or {}).get("value")) if r.auditorResponse else None
        for std in stds:
            if not std:
                continue
            a = agg.setdefault(
                std,
                {"total": 0, "pass": 0, "partial": 0, "fail": 0, "na": 0,
                 "scoreObtained": 0, "scoreAllotted": 0},
            )
            a["total"] += 1
            if r.scoreAllotted:
                a["scoreAllotted"] += r.scoreAllotted
                a["scoreObtained"] += r.scoreObtained or 0
            if val in ("pass", "partial", "fail", "na"):
                a[val] += 1
    out = []
    for std, a in sorted(agg.items()):
        a["scorePct"] = page_grading.compute_points_score(
            obtained=a["scoreObtained"], allotted=a["scoreAllotted"]
        )
        out.append({"standard": std, **a})
    return out


def _build_report_snapshot(
    audit: ComplianceAudit, report_type: str,
    *, rules: scoring_rules.ScoringRules | None = None, stream: str | None = None,
) -> dict[str, Any]:
    """Freeze one report.

    `stream` scopes the whole snapshot to one of the two sheets a department
    audit is conducted against — every count, every score, every finding and
    every clause in it. NOT a view filter applied afterwards: the headline
    percentage on the IMS report has to be the IMS points over the IMS
    allotment, and a report that computed the audit-wide number and then printed
    an IMS-only register underneath it would be internally inconsistent in the
    one place that matters most.

    `stream=None` is the whole audit, which is what every other library
    produces and what every report generated before streams existed was.
    """
    responses = sorted(
        (r for r in audit.responses if stream is None or r.streamCode == stream),
        key=lambda x: (x.categoryId, x.sequence),
    )
    score = _compute_score(audit, responses)
    total = len(responses)
    assessed = sum(1 for r in responses if r.assessmentStatus != "NOT_ASSESSED")

    findings: list[dict[str, Any]] = []
    open_iters: list[dict[str, Any]] = []
    crit_open = 0
    not_assessed = 0
    capa_total = capa_open = capa_overdue = 0
    now = _naive(_utcnow())

    for r in responses:
        val = _norm_value((r.auditorResponse or {}).get("value")) if r.auditorResponse else None
        owner = r.assignedOwnerId or r.routedToUserId
        capa = r.capa or {}
        if capa.get("capa_id"):
            capa_total += 1
            st = capa.get("capa_status")
            if st not in ("CLOSED", "CLOSED_RECURRED", "VERIFIED"):
                capa_open += 1
            due = capa.get("capa_due_date")
            try:
                if st not in ("CLOSED", "CLOSED_RECURRED", "VERIFIED") and due and _naive(datetime.fromisoformat(due)) < now:
                    capa_overdue += 1
            except (TypeError, ValueError):
                pass
        if val in ("fail", "partial"):
            findings.append({
                "checkpointCode": r.checkpointCode, "discipline": r.categoryName, "severity": r.criticality,
                "assessmentStatus": r.assessmentStatus, "workflowState": r.workflowState, "round": r.currentRound,
                "ownerId": owner, "question": r.checkpointQuestion, "observation": r.observation,
                "standard": r.standard, "requirementReference": r.requirementReference,
                # A finding is read for how bad it is and whether it is a
                # repeat. Both are workbook columns and both belong on the row.
                "requirementType": r.requirementType, "gradeAwarded": r.gradeAwarded,
                "scoreAllotted": r.scoreAllotted, "scoreObtained": r.scoreObtained,
                "complianceStatus": r.complianceStatus, "riskGrade": r.riskGrade,
                "isRepeat": page_grading.is_repeat(r.complianceStatus),
                "capaNumber": capa.get("capa_number"), "capaStatus": capa.get("capa_status"),
                "isAdHoc": r.isAdHoc,
            })
        # An OPEN ITERATION is a finding awaiting someone — not merely a
        # checkpoint nobody has reached yet.
        #
        # This used to be `not _is_terminal(r)`, which is the SAME call site as
        # the "82 open items in a closed report" defect. `_is_terminal` answers
        # "may the audit be finalised?", for which an unassessed checkpoint is
        # correctly a blocker. It does not answer "is an iteration in flight?",
        # for which an unassessed checkpoint is simply not started. Reusing one
        # predicate for both questions reported 81 unassessed checkpoints as 81
        # open iterations on an audit whose Findings Register correctly read 0.
        #
        # `_is_terminal` is unchanged — the finalisation gate still needs it.
        #
        # The `val in (fail, partial)` arm below is what makes a raised finding
        # an open item BEFORE it has been routed (workflowState OPEN, pre-submit)
        # — that is correct and is why it is here. But a finding does not stop
        # being a fail once it is answered, so on its own that arm counted EVERY
        # finding on a CLOSED audit as an open iteration: the flag underneath
        # then fired on all 39 findings of AUD-PI-2026-NW-0014 and told the
        # reader its record was corrupt. It was not — `_finalizability` had
        # already proved every row terminal, which is the only reason the audit
        # could close at all.
        #
        # A resolved terminal state therefore wins over the verdict. RESOLVED /
        # ACCEPTED_WITH_CAPA / FINALIZED mean the iteration is closed, whatever
        # the auditor's original grade was.
        resolved = r.workflowState in ("RESOLVED", "ACCEPTED_WITH_CAPA", "FINALIZED")
        in_flight = r.workflowState not in ("OPEN", "PASSED", "RESOLVED",
                                            "ACCEPTED_WITH_CAPA", "FINALIZED")
        if not resolved and (val in ("fail", "partial") or in_flight):
            open_iters.append({
                "checkpointCode": r.checkpointCode, "discipline": r.categoryName,
                "workflowState": r.workflowState, "round": r.currentRound, "ownerId": owner,
                "unassigned": not owner,
            })
            if r.criticality == "critical" and val == "fail":
                crit_open += 1
        elif r.assessmentStatus == "NOT_ASSESSED":
            not_assessed += 1

    # Zero-assessable (e.g. all-NA / nothing assessed) audit: a 0% next to
    # "Conforming" is contradictory, so report a neutral NOT_ASSESSED result +
    # null score. NA counts as "answered" but not "assessable".
    assessable = score["passed"] + score["partially_passed"] + score["failed"]
    overall_pct = None if assessable == 0 else score["overall_score_pct"]
    overall_result = "NOT_ASSESSED" if assessable == 0 else _result_label(score)

    # ── Grade suppression + the gate sentence, from the ONE scoring module ──
    # `services/scoring_rules` (WP-16) was written and then never called by
    # anything — the report computed its own verdict, which is exactly the
    # split-brain the scoring work existed to end. Both now come from here.
    #
    # `applicable` is the post-N/A denominator: grading 1 of 82 is dishonest
    # whether or not the other 81 were N/A-excluded.
    _rules = rules or scoring_rules.ScoringRules()
    applicable = total - score["not_applicable"]
    snapshot_grade = scoring_rules.grade_visibility(
        assessed=assessable, applicable=applicable, rules=_rules
    )
    _verdict = scoring_rules.evaluate(
        overall_pct=overall_pct,
        critical_failures=score["critical_failures"],
        rules=_rules,
    )
    # Below the coverage floor the percentage and band are still COMPUTED and
    # still stored — they are simply not presented as a headline verdict.
    if not snapshot_grade["showGrade"]:
        overall_result = "INSUFFICIENT_COVERAGE"

    # Per-discipline RAG summary — the structured spine of the report (so 1500
    # checkpoints read as a discipline breakdown, not a flat dump). all-NA /
    # not-assessed disciplines → null pct (neutral, not a misleading 0%; M1).
    #
    # `pct` is the POINTS score, the same number `categoryScores.score_pct`
    # carries and the same one the headline percentage uses. It used to be the
    # engine's superseded pass-ratio, which meant every snapshot stored TWO
    # different percentages for one discipline under two different keys — and a
    # renderer that reached for this one printed 85.0% beside a table saying
    # 88.3%. Keeping the key (an API consumer may read it) but making it agree
    # is what closes that off: there is now no way to pick the wrong number.
    discipline_rag = []
    for c in score["category_scores"]:
        d_assess = c["passed"] + c["partial"] + c["failed"]
        discipline_rag.append({
            "categoryId": c["category_id"], "categoryName": c["category_name"], "total": c["total"],
            "passed": c["passed"], "partial": c["partial"], "failed": c["failed"], "na": c["na"],
            "scoreObtained": c["score_obtained"], "scoreAllotted": c["score_allotted"],
            "pct": None if (d_assess == 0 or not c["score_allotted"]) else c["score_pct"],
        })

    # Departments, not disciplines, when the library says so. `discipline_rag`
    # keeps its key (API consumers read it) but the LABEL the report prints has
    # to name what the rows actually are — "3 disciplines" over HR / Admin / OHC
    # is wrong on the cover of a document a certification body reads.
    scope_axis = (
        "DEPARTMENT"
        if responses and any(r.streamCode for r in responses)
        else "DISCIPLINE"
    )
    axis_word = "department" if scope_axis == "DEPARTMENT" else "discipline"
    n_scope = len(discipline_rag)

    snapshot: dict[str, Any] = {
        "reportType": report_type,
        # Which of the two documents this is. Null on every report that covers
        # a whole audit, which is how a reader (and the PDF renderer) tells a
        # single-report audit from one half of a pair.
        "reportStream": stream,
        "reportStreamLabel": (STREAM_META[stream]["label"] if stream else None),
        "reportStreamTitle": (STREAM_META[stream]["reportTitle"] if stream else None),
        "reportStreamStandards": (STREAM_META[stream]["standards"] if stream else None),
        # DISCIPLINE | DEPARTMENT — what `disciplineRag` and `categoryScores`
        # are broken down by on THIS report.
        "scopeAxis": scope_axis,
        "auditCode": audit.auditNumber, "title": audit.title, "siteId": audit.plantId,
        "industryCode": audit.industryCode, "auditType": audit.auditType,
        "leadAuditorId": audit.leadAuditorUserId, "plantManagerId": audit.plantManagerUserId,
        "templateId": audit.templateId, "scopePresetUsed": audit.scopePresetUsed,
        "disciplinesInScope": audit.selectedDisciplineIds or [],
        # WP-50 (F-30). `selectedDisciplineIds == []` is a SENTINEL meaning "the
        # full library", not "no disciplines" — and the report rendered its raw
        # length, so a full-scope audit printed "0 discipline(s)". The truth is
        # in the materialised rows, so derive from them and give the UI a label
        # it cannot misread.
        "disciplinesInScopeCount": n_scope,
        "disciplinesInScopeLabel": (
            f"All {n_scope} {axis_word}s"
            if not (audit.selectedDisciplineIds or [])
            else f"{n_scope} {axis_word}" + ("s" if n_scope != 1 else "")
        ),
        "plannedDate": _iso(audit.scheduledDate), "submittedAt": _iso(audit.submittedAt), "closedAt": _iso(audit.closedAt),
        "overallScorePct": overall_pct, "overallResult": overall_result,
        # The arithmetic behind the headline percentage. Frozen alongside it so
        # the report can PRINT `311 of 357 points` rather than asking a reader
        # to trust 87.1% — the single cheapest thing that makes the score
        # checkable against the customer's own workbook.
        "scoreObtained": score["score_obtained"], "scoreAllotted": score["score_allotted"],
        "auditPassed": score["audit_passed"],
        "checkpointsTotal": total, "checkpointsAssessed": assessed,
        "passCount": score["passed"], "failCount": score["failed"],
        "partialCount": score["partially_passed"], "naCount": score["not_applicable"],
        "categoryScores": score["category_scores"],
        "criticalFailures": score["critical_failures"], "majorFailures": score["major_failures"],
        "minorFailures": score["minor_failures"],
        "openIterationsCount": len(open_iters), "criticalOpenCount": crit_open,
        # Distinct from open iterations: not started ≠ in review.
        "notAssessedCount": not_assessed,
        # Grade visibility + the rule sentence, both from `scoring_rules`.
        "grade": snapshot_grade,
        "gate": {"band": _verdict["band"], "passed": _verdict["passed"],
                 "explanation": _verdict["explanation"], "rules": _verdict["rules"]},
        "adHocCount": audit.adHocCount or 0,
        "capaSummary": {"total": capa_total, "open": capa_open, "overdue": capa_overdue},
        "findings": findings, "openIterations": open_iters,
        "disciplineRag": discipline_rag,
    }

    # WP-50 (F-29). A CLOSED audit cannot legitimately have open items — closure
    # finalises every checkpoint. If rows still read non-terminal here, that is a
    # data-integrity defect, not an action list, and the report must say which.
    # Presenting it as "82 open items" (the worst artefact in the module) implied
    # work outstanding on a completed audit. Naming it honestly costs nothing and
    # feeds the Band 0 integrity strip.
    if audit.status == "closed" and open_iters:
        snapshot["dataIntegrityFlags"] = [{
            "code": "CLOSED_WITH_NON_TERMINAL_CHECKPOINTS",
            "count": len(open_iters),
            "message": (
                f"{len(open_iters)} checkpoint(s) on this closed audit are not in a terminal "
                "state. This is a data-integrity defect in the record, not outstanding work. "
                "Re-run the assessment-status backfill (WP-02)."
            ),
        }]
        # Not "open items requiring action" — the register keeps the detail.
        snapshot["openIterations"] = []
        snapshot["openIterationsCount"] = 0
    # ── WP-12: certification-grade sections ───────────────────────────
    # A certification body reads these before the numbers. Each is DERIVED from
    # the record rather than typed by hand, so the report cannot claim a method
    # the audit did not follow.
    snapshot["methodology"] = _methodology_block(audit, responses, report_type)
    snapshot["clauseIndex"] = _clause_index(responses)
    snapshot["distributionList"] = _distribution_list(audit)

    if report_type == "FINAL":
        # The full checkpoint register (every row + its thread) is NOT inlined
        # into the immutable snapshot — at 1500 checkpoints that is unbounded
        # JSON. It is served lazily/paginated from GET /reports/{id}/register
        # (the audit is read-only post-close, so the register is stable).
        snapshot["hasFullRegister"] = True
        snapshot["standardsRollup"] = _standards_rollup(responses)
        snapshot["finalizability"] = _finalizability(audit, stream=stream)

    # ── Section 1 insight layer ───────────────────────────────────────────
    # Computed HERE, inside the snapshot that `generate_report` hashes, so the
    # insights are frozen with everything else at issue. It is deliberately not
    # a view-time query: a headline that changed after issue would break the
    # immutability the SHA-256 digest asserts. Underlying data changes produce
    # a new issue (I02, I03 …), and its insights are recomputed then.
    #
    # Purely derivative — every input is a field already counted above, so this
    # cannot disagree with the register it sits over. Deterministic rules +
    # template slot-filling only (`services/insights`), no model call, which is
    # what keeps report generation working on an airgapped deployment.
    snapshot["insights"] = rules_audit_report.compute_report_insights(snapshot)
    return snapshot


def _methodology_block(
    audit: ComplianceAudit, responses: list[AuditCheckpointResponse], report_type: str
) -> dict[str, Any]:
    """Scope, method and LIMITATIONS — ISO 19011 §5.5.2 / §6.5.

    The limitations list is the part that earns trust: a report that states what
    it could NOT establish reads as more credible than one that implies total
    coverage. Every entry is derived, so it cannot drift from the record.
    """
    limitations: list[str] = []

    unassessed = sum(1 for r in responses if r.assessmentStatus == "NOT_ASSESSED")
    if unassessed:
        limitations.append(
            f"{unassessed} of {len(responses)} checkpoints were not assessed and are excluded "
            "from the compliance percentage."
        )
    na = sum(1 for r in responses if r.assessmentStatus == "NA")
    if na:
        limitations.append(
            f"{na} checkpoint(s) were marked not applicable and are excluded from the denominator."
        )
    if report_type == "INTERIM":
        limitations.append(
            "This is an interim report. Figures are provisional and may change before the audit "
            "is finalised."
        )
    no_evidence = sum(
        1
        for r in responses
        if r.assessmentStatus in ("FAIL", "PARTIAL") and not (r.auditorEvidenceIds or [])
    )
    if no_evidence:
        limitations.append(
            f"{no_evidence} adverse finding(s) carry a written observation but no photographic "
            "evidence."
        )
    if audit.reopenCount:
        limitations.append(
            f"This audit was reopened {audit.reopenCount} time(s) after an earlier closure."
        )
    if not limitations:
        limitations.append(
            "No scope limitations were recorded: every in-scope checkpoint was assessed."
        )

    return {
        "criteria": sorted({r.standard for r in responses if r.standard}) or ["Not specified"],
        "method": (
            "Document review, physical inspection and interview against the checkpoint set "
            "materialised for the disciplines in scope. Each adverse finding is routed to the "
            "responsible auditee and iterated until terminal."
        ),
        "scopeDescription": audit.scopeDescription or "",
        "scopeAreas": list(audit.scopeAreas or []),
        "scopeDepartments": list(audit.scopeDepartments or []),
        "limitations": limitations,
    }


def _clause_index(responses: list[AuditCheckpointResponse]) -> list[dict[str, Any]]:
    """Clause -> checkpoints -> outcome. The index an assessor navigates by.

    Grouped on the free-text `standard` + `requirementReference` pair, which is
    populated on 2,502 / 1,690 of 2,503 rows. String-grouped and honestly so:
    exact clause coverage needs WP-20's ClauseRef catalogue.
    """
    idx: dict[tuple[str, str], dict[str, Any]] = {}
    for r in responses:
        # Expanded per standard for the same reason as `_standards_rollup`: an
        # assessor navigating the index looks up "ISO 45001 §6.1.2", and a row
        # filed under all three standards joined into one string is a row they
        # cannot find under any of them.
        pairs = [
            ((c.get("standard") or "").strip(), (c.get("clause") or "").strip())
            for c in (r.standardClauses or [])
        ] or [((r.standard or "").strip(), (r.requirementReference or "").strip())]
        for std, clause in pairs:
            if not std and not clause:
                continue
            key = (std, clause)
            e = idx.setdefault(
                key,
                {"standard": std or "—", "clause": clause or "—", "total": 0,
                 "pass": 0, "fail": 0, "partial": 0, "na": 0, "notAssessed": 0,
                 "checkpointCodes": []},
            )
            e["total"] += 1
            e[
                {"PASS": "pass", "FAIL": "fail", "PARTIAL": "partial",
                 "NA": "na", "NOT_ASSESSED": "notAssessed"}.get(r.assessmentStatus, "notAssessed")
            ] += 1
            if len(e["checkpointCodes"]) < 12:
                e["checkpointCodes"].append(r.checkpointCode)
    out = list(idx.values())
    # Worst first — an assessor opens the index to find problems, not to read A-Z.
    out.sort(key=lambda e: (-e["fail"], -e["partial"], e["standard"], e["clause"]))
    return out


def _distribution_list(audit: ComplianceAudit) -> list[dict[str, str]]:
    """Who this report is issued to. Ids only; names resolve in generate_report."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(uid: str | None, role: str) -> None:
        if uid and uid not in seen:
            seen.add(uid)
            out.append({"userId": uid, "role": role})

    add(audit.leadAuditorUserId, "Lead auditor")
    for uid in _coauditor_ids(audit.coAuditors):
        add(uid, "Auditor")
    add(audit.plantManagerUserId, "Plant manager")
    for a in audit.auditees or []:
        add(a.get("userId") if isinstance(a, dict) else a, "Auditee owner")
    return out


def _report_to_dict(rep: AuditReport) -> dict[str, Any]:
    return {
        "id": rep.id, "auditId": rep.auditId, "siteId": rep.siteId, "reportType": rep.reportType,
        "reportCode": rep.reportCode, "generatedById": rep.generatedById, "generatedAt": _iso(rep.generatedAt),
        "snapshot": rep.snapshot, "signOffs": rep.signOffs, "pdfAttachmentId": rep.pdfAttachmentId,
        "isSuperseded": rep.isSuperseded, "snapshotHashFull": rep.snapshotHashFull,
        "reportStream": rep.reportStream,
        "reportStreamLabel": (
            STREAM_META[rep.reportStream]["label"] if rep.reportStream in STREAM_META else None
        ),
    }


async def available_report_streams(db: AsyncSession, audit_id: str) -> list[dict[str, Any]]:
    """Which reports this audit can issue, one entry per stream.

    EMPTY means one report covering the whole audit — the caller renders its
    single pair of buttons rather than a synthetic "stream" with a null code,
    which nothing could sensibly post back.

    Derived from the audit's own materialised rows, not from its library: an
    audit scoped to departments that happen to hold no EnMS checkpoint must not
    offer an EnMS report with nothing in it.
    """
    return await stream_rollup(db, audit_id)


async def generate_report(
    db: AsyncSession, *, user: User, audit_id: str, report_type: str,
    stream: str | None = None,
) -> dict[str, Any]:
    """Generate an immutable Interim or Final report. Interim accumulates (the
    latest supersedes prior interims for display, all retained). Final requires
    a finalizable audit.

    Takes no `sign_offs` argument. It used to, and the caller's list was frozen
    into the snapshot verbatim — so the HTTP contract (role + userId, no name,
    no timestamp) determined what an issued compliance document said about who
    had signed it, and every API-generated report printed "LEAD_AUDITOR: -  -".
    Widening that contract would only move the same trust boundary outwards; a
    client must not be able to assert a signature at all. Sign-offs are read
    here from `ComplianceAudit.signOffs`, written solely by
    `signoff.record_signoff`, which authenticates the signer and stamps the
    time server-side.
    """
    report_type = (report_type or "").upper()
    if report_type not in ("INTERIM", "FINAL"):
        raise ValueError("reportType must be INTERIM or FINAL")

    # Interactions are no longer inlined into the snapshot (FINAL register is
    # served lazily), so a plain responses load suffices for both report types.
    audit = await _load_audit(db, audit_id, with_responses=True)
    if audit is None:
        raise ValueError("Audit not found")

    # ── Which of the two documents is being issued ────────────────────────
    #
    # A raw token that is not a stream is REJECTED rather than silently falling
    # back to the whole audit: "generate the EMS report" answered with an
    # audit-wide document that looks right and covers the wrong checkpoints is
    # the worst failure this endpoint has available to it.
    if stream not in (None, ""):
        code = normalise_stream(stream)
        if code is None:
            raise ValueError(
                f"Unknown report stream '{stream}' — expected one of {', '.join(STREAMS)}"
            )
        stream = code
        if not any(r.streamCode == code for r in audit.responses):
            raise ValueError(
                f"This audit has no {STREAM_META[code]['label']} checkpoints, so there is "
                f"nothing to report against {STREAM_META[code]['standards']}."
            )
    else:
        stream = None

    if report_type == "INTERIM":
        if audit.status in ("scheduled", "cancelled"):
            raise ValueError("Nothing to report yet — start conducting the audit first")
    else:
        fin = _finalizability(audit, stream=stream)
        if not fin["finalizable"]:
            _what = f"{STREAM_META[stream]['label']} " if stream else ""
            raise ValueError(
                f"{fin['blockerCount']} {_what}checkpoint(s) still in review — a final "
                "report needs every checkpoint terminal"
            )

    # Per-audit-type scoring config (WP-16). `CamsAuditType.scoringRules` is the
    # store; resolving it needs the DB, so it happens here and is passed down.
    # No match → documented defaults, which is the historic behaviour exactly.
    _at = (
        await db.execute(
            select(CamsAuditType).where(CamsAuditType.typeCode == audit.auditType)
        )
    ).scalar_one_or_none()
    _rules = scoring_rules.rules_from(_at.scoringRules if _at else None)

    snapshot = _build_report_snapshot(audit, report_type, rules=_rules, stream=stream)
    # Freeze friendly plant + actor names into the immutable snapshot so the
    # (external-facing) report shows names, not raw ids — and resolves
    # cross-plant actors the live /users picker can't.
    plant = await db.get(Plant, audit.plantId)
    snapshot["plantName"] = plant.name if plant else "Unknown site"
    snapshot["plantCode"] = plant.code if plant else None

    # ── WP-45: who was audited ─────────────────────────────────────────────
    #
    # Frozen into the immutable snapshot, and set on BOTH branches. A report
    # that silently omits the block for a supplier audit would render with the
    # plant name in the "site" position and read as an audit of our own factory
    # — the single most consequential way this report can be wrong. The
    # own-facility case therefore states itself rather than being an absence.
    _sup_block = await _capa_subject(db, audit)
    if _sup_block:
        snapshot["subjectType"] = "VENDOR"
        snapshot["subject"] = {
            "type": "VENDOR",
            "label": _sup_block["legalName"],
            "vendorCode": _sup_block.get("vendorCode"),
            "vendorProfileId": _sup_block.get("vendorProfileId"),
            "contactName": _sup_block.get("supplierContactName"),
            "statement": (
                f"This report covers an audit of {_sup_block['legalName']}, an external "
                f"supplier. The site named above is the {snapshot['plantName']} operation "
                "that holds the supplier relationship, not the audited premises."
            ),
        }
    else:
        snapshot["subjectType"] = "OWN_SITE"
        snapshot["subject"] = {
            "type": "OWN_SITE",
            "label": snapshot["plantName"],
            "statement": f"This report covers an audit of our own facility at {snapshot['plantName']}.",
        }
    uid_set: set[str] = {audit.leadAuditorUserId, audit.plantManagerUserId}
    for r in audit.responses:
        uid_set.update((r.assignedOwnerId, r.routedToUserId))
    # Signers used to be added here so their ids could be resolved to names.
    # They no longer need to be: a recorded sign-off carries the signer's name
    # and designation with it, captured at signing time — which is the correct
    # record anyway, since a person's current name is not necessarily the one
    # they signed under.
    uid_set.update(s.get("userId") for s in (audit.signOffs or []))
    uid_set = {u for u in uid_set if u}
    snapshot["userNames"] = {}
    if uid_set:
        rows = (await db.execute(select(User.id, User.name).where(User.id.in_(uid_set)))).all()
        snapshot["userNames"] = {uid: nm for uid, nm in rows}
    # ── Assurance blocks (docs/cams/09 §2.1.6, §2.2, §2.3) ────────────────
    # Frozen into the snapshot, like everything else here, so the report keeps
    # saying the same thing after the underlying records change. Each block
    # asserts absence explicitly rather than rendering nothing — a reader must
    # be able to tell "no waivers were issued" from "this product does not track
    # waivers", and only a sentence does that.
    # ── Clause-citation provenance ────────────────────────────────────────
    #
    # 127 of the library's 152 citations are AI drafts, not sourced fact. The
    # clause index below groups on those strings and cannot tell a drafted
    # citation from an authored one, so a report that printed the index without
    # this block would present full clause coverage as full confidence — the
    # same class of defect as a headline score over an unassessed audit.
    #
    # Read from the LIBRARY (the system of record for citation content) rather
    # than from the materialised rows, which carry no provenance. Frozen into
    # the snapshot like everything else, so the count is the one that was true
    # when the report was issued.
    _lib = (
        await db.execute(
            select(AuditCheckpointLibrary).where(
                AuditCheckpointLibrary.industryCode == audit.industryCode
            )
        )
    ).scalar_one_or_none()
    _cit = citations.summarise(_lib.categories if _lib else [])
    snapshot["citationProvenance"] = {
        **_cit,
        # The code list is for tooling, not for a 1,500-checkpoint PDF.
        "uncitedCodes": _cit["uncitedCodes"][:25],
        "footnote": citations.report_footnote(_cit),
    }

    snapshot["independence"] = await assurance.waiver_block_for(
        db, engagement_kind="AUDIT", engagement_id=audit.id
    )
    snapshot["meetings"] = await assurance.meetings_for(
        db, engagement_kind="AUDIT", engagement_id=audit.id
    )
    snapshot["competence"] = await assurance.competence_snapshots_for(
        db, engagement_kind="AUDIT", engagement_id=audit.id
    )
    if audit.reopenCount:
        snapshot["reopenHistory"] = {
            "count": audit.reopenCount,
            "lastReopenedAt": _iso(audit.lastReopenedAt),
            "lastReason": audit.lastReopenReason,
            "statement": (
                f"This audit was reopened {audit.reopenCount} time(s) after closure."
            ),
        }

    # WP-12: revision history — every prior issue of this audit's reports, so a
    # reader can see this is (say) the third issue and what preceded it. Built
    # here rather than in _build_report_snapshot because it needs the DB.
    # Scoped to THIS stream: "revision 3" on the IMS report must count the
    # earlier IMS issues, not the EnMS ones interleaved with them.
    _prior_all = (
        await db.execute(
            select(AuditReport)
            .where(
                AuditReport.auditId == audit_id,
                AuditReport.reportStream.is_(None) if stream is None
                else AuditReport.reportStream == stream,
            )
            .order_by(AuditReport.generatedAt)
        )
    ).scalars().all()
    snapshot["revisionHistory"] = [
        {
            "reportCode": p.reportCode,
            "reportType": p.reportType,
            "generatedAt": _iso(p.generatedAt),
            "superseded": True,
            "snapshotHash": (p.snapshot or {}).get("snapshotHash"),
        }
        for p in _prior_all
    ]
    snapshot["revision"] = len(_prior_all) + 1

    # Resolve distribution-list names against the same frozen name map.
    for _d in snapshot.get("distributionList") or []:
        uid_set.add(_d.get("userId"))
    uid_set = {u for u in uid_set if u}
    if uid_set:
        _rows = (await db.execute(select(User.id, User.name).where(User.id.in_(uid_set)))).all()
        snapshot["userNames"] = {uid: nm for uid, nm in _rows}
    for _d in snapshot.get("distributionList") or []:
        _d["name"] = snapshot["userNames"].get(_d.get("userId", ""), "Unknown")

    # Owner-concentration insights name a person. `compute_report_insights` is
    # pure (no DB), so it left a `{owner}` slot; fill it from the same frozen
    # name map the rest of the report resolves against — never a raw cuid.
    # Runs BEFORE the hash, so the resolved name is part of the frozen snapshot.
    rules_audit_report.resolve_owner_names(
        snapshot.get("insights") or {}, snapshot["userNames"]
    )

    # ── Sign-offs: the RECORDED ones, not the caller's assertion ───────────
    #
    # `AuditReport.signOffs` used to be whatever the caller passed. The HTTP
    # contract (`routers.audit_compliance.SignOff`) carries only role + userId,
    # and the UI sends a hardcoded lead-auditor + plant-manager pair, so every
    # report issued through the API froze two nameless, timeless stubs and
    # printed "LEAD_AUDITOR: -  -". Reports generated in-process (the seeders)
    # passed `audit.signOffs` and looked correct, which is why this went unseen.
    #
    # The system of record is `ComplianceAudit.signOffs`, written only by
    # `signoff.record_signoff` — which authenticates the signer, validates the
    # signature and stamps a server-side time. That is what gets frozen now.
    #
    # A caller can no longer assert a sign-off that was never recorded. That is
    # the point: a printed name against a role, with no signature and no
    # timestamp, reads as "this person signed" when nobody did, and on a
    # compliance document that is the most damaging thing this section can say.
    # Roles the caller nominated that carry no recorded signature are reported
    # as outstanding instead.
    _status = await signoff.signoff_status(db, audit)
    sign_offs = list(_status["signOffs"]) or None
    snapshot["signOffSummary"] = {
        "recorded": len(_status["signOffs"]),
        "missingRequiredRoles": list(_status["missingRequiredRoles"]),
        # Which disciplines still lack their auditor's sign-off — server-side
        # knowledge derived from who actually held allocated checkpoints, not
        # something a client could tell us.
        "unsignedDisciplines": [
            d["disciplineLabel"] for d in _status["disciplines"] if not d["signed"]
        ],
        "disciplinesSigned": _status["disciplinesSigned"],
        "disciplinesTotal": _status["disciplinesTotal"],
        "statement": _status["statement"],
    }

    snapshot["generatedAt"] = _iso(_utcnow())
    # Two digests over the SAME canonical form: the 16-char prefix stays inside
    # the snapshot for display and backward compatibility, and the full 64-char
    # digest is stored on the row where it can actually be verified (§2.5 gap 1).
    # `verify_report_integrity` strips both keys before rehashing — that
    # invariant lives in ONE place, app.services.assurance.canonical_hash.
    _full = assurance.canonical_hash(snapshot, full=True)
    snapshot["snapshotHash"] = _full[:16]

    # Supersede prior reports of the same type AND STREAM for display (all
    # retained). Per stream, because the IMS and EnMS reports are two separate
    # documents: re-issuing the IMS interim after a re-grade must not mark the
    # EnMS one stale, which would take a current document off the screen and
    # replace it with nothing.
    _stream_match = (
        AuditReport.reportStream.is_(None) if stream is None
        else AuditReport.reportStream == stream
    )
    prior = (
        await db.execute(
            select(AuditReport).where(
                AuditReport.auditId == audit_id, AuditReport.reportType == report_type,
                _stream_match, AuditReport.isSuperseded.is_(False),
            )
        )
    ).scalars().all()
    for p in prior:
        p.isSuperseded = True

    base_n = (
        await db.execute(
            select(func.count(AuditReport.id)).where(
                AuditReport.auditId == audit_id, AuditReport.reportType == report_type,
                _stream_match,
            )
        )
    ).scalar_one() or 0
    prefix = "I" if report_type == "INTERIM" else "F"
    # The stream is part of the code, so the two documents are distinguishable
    # on a filename, in an email subject and in the register — not only by
    # opening them.
    stream_part = f"-{STREAM_META[stream]['label'].upper()}" if stream else ""

    # reportCode is derived from a count; under concurrent generation two
    # requests can pick the same number and collide on the unique constraint.
    # Insert inside a SAVEPOINT and retry with the next number on collision.
    for attempt in range(8):
        code = f"RPT-{audit.auditNumber}{stream_part}-{prefix}{base_n + 1 + attempt:02d}"
        rep = AuditReport(
            auditId=audit.id, siteId=audit.plantId, reportType=report_type, reportCode=code,
            reportStream=stream,
            generatedById=user.id, snapshot=snapshot, signOffs=sign_offs or None, isSuperseded=False,
            snapshotHashFull=_full,
        )
        try:
            async with db.begin_nested():
                db.add(rep)
                await db.flush()
            await db.refresh(rep)
            return _report_to_dict(rep)
        except IntegrityError:
            # The savepoint rollback already deassociated the failed INSERT from
            # the session — do NOT expunge it (that raises InvalidRequestError).
            continue
    raise ValueError("Could not allocate a unique report code — please retry")


async def list_reports(db: AsyncSession, audit_id: str) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(AuditReport).where(AuditReport.auditId == audit_id).order_by(AuditReport.generatedAt.desc())
        )
    ).scalars().all()
    return [_report_to_dict(r) for r in rows]


async def get_report(db: AsyncSession, report_id: str) -> dict[str, Any] | None:
    rep = await db.get(AuditReport, report_id)
    return _report_to_dict(rep) if rep else None


async def list_report_register(
    db: AsyncSession, *, report_id: str, discipline_id: str | None = None,
    cursor: str | None = None, limit: int = 50,
) -> dict[str, Any] | None:
    """The FINAL report's full checkpoint register, paginated + lazy (not stored
    in the snapshot). The audit is read-only post-close so the register is
    stable. Each entry carries its full iteration thread."""
    rep = await db.get(AuditReport, report_id)
    if rep is None:
        return None
    R = AuditCheckpointResponse
    conds = [R.auditId == rep.auditId]
    # The register belongs to the REPORT, so it is scoped by the report's own
    # stream and not by anything the caller passes. An IMS report whose register
    # listed EnMS checkpoints would contradict every count on its own cover.
    if rep.reportStream:
        conds.append(R.streamCode == rep.reportStream)
    if discipline_id:
        conds.append(R.categoryId == discipline_id)
    total = (await db.execute(select(func.count(R.id)).where(*conds))).scalar_one() or 0
    paged = select(R).where(*conds).order_by(R.sequence, R.id).options(selectinload(R.interactions))
    if cursor:
        try:
            c_seq_s, c_id = cursor.split(":", 1)
            c_seq = int(c_seq_s)
        except (ValueError, AttributeError) as e:
            raise ValueError("Invalid cursor") from e
        paged = paged.where(or_(R.sequence > c_seq, and_(R.sequence == c_seq, R.id > c_id)))
    limit = max(1, min(limit, 200))
    rows = (await db.execute(paged.limit(limit + 1))).scalars().all()
    has_more = len(rows) > limit
    items = rows[:limit]
    register = [
        {
            "checkpointCode": r.checkpointCode, "discipline": r.categoryName, "question": r.checkpointQuestion,
            "severity": r.criticality, "assessmentStatus": r.assessmentStatus, "workflowState": r.workflowState,
            "standard": r.standard, "requirementReference": r.requirementReference,
            # The workbook's own columns, so the printed register is the same
            # document the customer already reviews — not a translation of it.
            "requirementType": r.requirementType, "gradeAwarded": r.gradeAwarded,
            "scoreAllotted": r.scoreAllotted, "scoreObtained": r.scoreObtained,
            "complianceStatus": r.complianceStatus, "riskGrade": r.riskGrade,
            "observation": r.observation, "isAdHoc": r.isAdHoc,
            "ownerId": r.assignedOwnerId or r.routedToUserId, "capaNumber": (r.capa or {}).get("capa_number"),
            "auditorEvidenceIds": r.auditorEvidenceIds or [], "auditeeEvidenceIds": r.auditeeEvidenceIds or [],
            "interactions": [_interaction_to_dict(i) for i in sorted(r.interactions, key=lambda x: (x.timestamp, x.round))],
        }
        for r in items
    ]
    return {
        "auditId": rep.auditId, "siteId": rep.siteId, "register": register,
        "nextCursor": f"{items[-1].sequence}:{items[-1].id}" if has_more and items else None,
        "total": total, "returned": len(items),
    }
