"""Fire checklist runs — periodic records on the CAMS engine.

The four Page Industries checklists (PIL/EHS/CL 025-028) do not get their own
store. `models/fire_safety.py` already fixed that policy — fire inspections are
CAMS engagements, "one engine, no parallel checklist store" — and this module is
the thin layer that makes CAMS behave like a *periodic register* rather than an
ad-hoc audit:

    template (CamsTemplate)  +  asset (FireEquipment)  +  period  ->  one run

The three things that layer has to add, and nothing else:

  1. **Period identity.** `CamsEngagement.periodLabel`, granularity-encoded, so
     "today's daily sheet for panel P" resolves to exactly one row. Auto-create
     on first touch, made race-safe by a partial unique index rather than by an
     application lock — two inspectors opening the same sheet at the same second
     is the normal case in a plant, not an edge case.

  2. **The sign-off chain.** Prepared -> Reviewed -> Approved, printed at the foot
     of every source sheet. Mapped onto the CAMS state machine rather than a
     parallel status column, so stage ORDER is enforced by the machine that
     already enforces it and there is one answer to "can this be approved yet".

  3. **The grid pivot.** The paper sheets print a month of daily inspections, or
     a year of monthly ones, as one page. That is a *view* over a set of period
     runs — see fire_checklist_templates.py for why the alternative (a response
     store keyed by item x date) is both a fork of the engine and a misreading of
     the document.

Deterministic and offline throughout: no external calls, no clock beyond
`datetime.now`, no AI.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.models.cams import (
    CamsEngagement, CamsResponse, CamsTemplate, CamsTemplateQuestion, CamsTemplateSection,
)
from app.models.fire_safety import FireEquipment, PlantNonWorkingDay
from app.services import cams as cams_svc
from app.services import fire_capa
from app.services import fire_signoff
from app.services.fire_checklist_templates import (
    LAYOUT_DAY_GRID, LAYOUT_MONTH_GRID, LAYOUT_QUARTER_GRID,
)

SOURCE_MODULE = "FIRE"

# ── Sign-off stages -> CAMS engagement statuses ──────────────────────────────
#
# The source sheets have a three-stage block (Prepared by: Person In-charge /
# Reviewed by: Intermediatory Head / Approved by: HOD). CAMS has a six-state
# machine. Mapping onto it rather than adding a fourth status column means the
# existing `_TRANSITIONS` table is what rejects "approve before review" — one
# rule, one place.
#
# APPROVED lands on REPORT_ISSUED, not CLOSED, and that is deliberate. CLOSED
# runs `engagement_close_blockers`, which refuses to close an engagement with
# open findings. For a sheet whose only finding-raising item is the extinguisher
# refill-due check, that is the correct behaviour to keep — approving the record
# and closing out the defects it raised are genuinely different acts, and
# collapsing them would let a defect be signed away by the HOD approving the
# sheet it was found on. CLOSED remains reachable through CAMS once the findings
# are closed.
STAGE_DRAFT = "DRAFT"
STAGE_SUBMITTED = "SUBMITTED"
STAGE_REVIEWED = "REVIEWED"
STAGE_APPROVED = "APPROVED"

_STATUS_TO_STAGE: dict[str, str] = {
    "PLANNED": STAGE_DRAFT,
    "SCHEDULED": STAGE_DRAFT,
    "IN_PROGRESS": STAGE_DRAFT,
    "FIELDWORK_COMPLETE": STAGE_SUBMITTED,
    "FINDINGS_REVIEW": STAGE_REVIEWED,
    "REPORT_ISSUED": STAGE_APPROVED,
    "CLOSED": STAGE_APPROVED,
    "CANCELLED": "CANCELLED",
}

# stage the caller asks for -> (required current stage, CAMS status to move to)
_STAGE_STEP: dict[str, tuple[str, str]] = {
    STAGE_SUBMITTED: (STAGE_DRAFT, "FIELDWORK_COMPLETE"),
    STAGE_REVIEWED: (STAGE_SUBMITTED, "FINDINGS_REVIEW"),
    STAGE_APPROVED: (STAGE_REVIEWED, "REPORT_ISSUED"),
}

# Answers are editable in DRAFT only. Everything from SUBMITTED onward is a
# signed record: the spec's rule is that a correction after approval is a new
# run for a corrective period, never an edit of the old one.
EDITABLE_STAGES = frozenset({STAGE_DRAFT})


class ChecklistError(Exception):
    """Domain error with an HTTP status the router maps straight through."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def stage_of(engagement: CamsEngagement) -> str:
    return _STATUS_TO_STAGE.get(engagement.status, STAGE_DRAFT)


def is_locked(engagement: CamsEngagement) -> bool:
    return stage_of(engagement) not in EDITABLE_STAGES


# ═══════════════════════════════════════════════════════════════════════════
# Period labels
# ═══════════════════════════════════════════════════════════════════════════
_RE = {
    "DAILY": re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"),
    "MONTHLY": re.compile(r"^(\d{4})-(\d{2})$"),
    "QUARTERLY": re.compile(r"^(\d{4})-Q([1-4])$"),
    "ANNUAL": re.compile(r"^(\d{4})$"),
}


def period_label(frequency: str, when: date) -> str:
    """The canonical occurrence key for `when` at this cadence."""
    if frequency == "DAILY":
        return when.isoformat()
    if frequency == "MONTHLY":
        return f"{when.year:04d}-{when.month:02d}"
    if frequency == "QUARTERLY":
        return f"{when.year:04d}-Q{(when.month - 1) // 3 + 1}"
    if frequency == "ANNUAL":
        return f"{when.year:04d}"
    raise ChecklistError(f"Unknown frequency '{frequency}'.")


def validate_period(frequency: str, label: str) -> str:
    """Reject a label that does not match its cadence.

    Worth being strict about: a mistyped "2026-8" would otherwise create a
    perfectly valid-looking second record for August alongside "2026-08", and
    nothing downstream would ever notice.
    """
    rx = _RE.get(frequency)
    if rx is None:
        raise ChecklistError(f"Unknown frequency '{frequency}'.")
    m = rx.match(label or "")
    if not m:
        shape = {"DAILY": "YYYY-MM-DD", "MONTHLY": "YYYY-MM",
                 "QUARTERLY": "YYYY-Qn", "ANNUAL": "YYYY"}[frequency]
        raise ChecklistError(f"Period '{label}' is not valid for a {frequency} checklist (expected {shape}).")
    if frequency in ("DAILY", "MONTHLY"):
        month = int(m.group(2))
        if not 1 <= month <= 12:
            raise ChecklistError(f"Period '{label}' has an impossible month.")
    if frequency == "DAILY":
        try:
            date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError as exc:
            raise ChecklistError(f"Period '{label}' is not a real date.") from exc
    return label


def period_start(frequency: str, label: str) -> datetime:
    """First instant of the period — used as the run's `plannedDate`.

    A periodic record still needs a point on the timeline: the CAMS list views,
    the calendar and every date filter on the platform sort by `plannedDate`, and
    a run with no date would silently drop out of all of them.
    """
    validate_period(frequency, label)
    if frequency == "DAILY":
        y, m, d = (int(x) for x in label.split("-"))
    elif frequency == "MONTHLY":
        y, m, d = int(label[:4]), int(label[5:7]), 1
    elif frequency == "QUARTERLY":
        y, q = int(label[:4]), int(label[-1])
        m, d = (q - 1) * 3 + 1, 1
    else:
        y, m, d = int(label), 1, 1
    return datetime.combine(date(y, m, d), time.min, tzinfo=timezone.utc)


def _month_days(year: int, month: int) -> list[date]:
    return [date(year, month, d) for d in range(1, calendar.monthrange(year, month)[1] + 1)]


def grid_periods(layout: str, frequency: str, window: str) -> list[tuple[str, str]]:
    """Column (periodLabel, header) pairs for one printed page.

    `window` is what the page covers: "YYYY-MM" for a daily grid (one month per
    page, matching the sheet), "YYYY" for the month and quarter grids.
    """
    if layout == LAYOUT_DAY_GRID:
        if not _RE["MONTHLY"].match(window or ""):
            raise ChecklistError(f"A daily grid is paged by month; '{window}' is not YYYY-MM.")
        y, m = int(window[:4]), int(window[5:7])
        return [(d.isoformat(), str(d.day)) for d in _month_days(y, m)]
    if layout == LAYOUT_MONTH_GRID:
        if not _RE["ANNUAL"].match(window or ""):
            raise ChecklistError(f"A month grid is paged by year; '{window}' is not YYYY.")
        y = int(window)
        return [(f"{y:04d}-{m:02d}", calendar.month_abbr[m]) for m in range(1, 13)]
    if layout == LAYOUT_QUARTER_GRID:
        if not _RE["ANNUAL"].match(window or ""):
            raise ChecklistError(f"A quarter grid is paged by year; '{window}' is not YYYY.")
        y = int(window)
        labels = ["First Quarter", "Second Quarter", "Third Quarter", "Forth Quarter"]
        return [(f"{y:04d}-Q{q}", labels[q - 1]) for q in range(1, 5)]
    # A FORM template has exactly one column: the period itself.
    return [(window, window)]


async def non_working_days(db, plant_id: str, days: list[date]) -> dict[str, str]:
    """{isoDate: label} for the dates in `days` the plant does not run.

    Sundays are computed, not looked up — see PlantNonWorkingDay's docstring for
    why storing them would be a calendar that drifts from the calendar. Explicit
    HOLIDAY rows win over the computed SUNDAY where both apply, because a row is
    someone's deliberate statement and the weekday is an inference.
    """
    if not days:
        return {}
    out = {d.isoformat(): "SUNDAY" for d in days if d.weekday() == 6}
    lo = datetime.combine(min(days), time.min, tzinfo=timezone.utc)
    hi = datetime.combine(max(days), time.max, tzinfo=timezone.utc)
    rows = (
        await db.execute(
            select(PlantNonWorkingDay)
            .where(PlantNonWorkingDay.plantId == plant_id)
            .where(PlantNonWorkingDay.day >= lo)
            .where(PlantNonWorkingDay.day <= hi)
        )
    ).scalars().all()
    for r in rows:
        out[r.day.date().isoformat()] = r.label or "HOLIDAY"
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Template + run resolution
# ═══════════════════════════════════════════════════════════════════════════
async def load_template(db, *, template_code: str | None = None, template_id: str | None = None) -> CamsTemplate:
    """Fetch a fire template with its sections and questions eagerly loaded."""
    stmt = (
        select(CamsTemplate)
        .options(selectinload(CamsTemplate.sections).selectinload(CamsTemplateSection.questions))
        .where(CamsTemplate.isDeleted.is_(False))
    )
    stmt = stmt.where(CamsTemplate.templateCode == template_code) if template_code \
        else stmt.where(CamsTemplate.id == template_id)
    tpl = (await db.execute(stmt)).scalars().first()
    if tpl is None:
        raise ChecklistError(f"Checklist template '{template_code or template_id}' not found. "
                             "Run seed_fire_checklists.py.", 404)
    if not (tpl.documentMeta or {}).get("documentNo"):
        raise ChecklistError(f"Template '{tpl.templateCode}' is not a controlled fire checklist.", 400)
    return tpl


def template_meta(tpl: CamsTemplate) -> dict[str, Any]:
    return dict(tpl.documentMeta or {})


def ordered_questions(tpl: CamsTemplate) -> list[tuple[CamsTemplateSection, CamsTemplateQuestion]]:
    out: list[tuple[CamsTemplateSection, CamsTemplateQuestion]] = []
    for sec in sorted(tpl.sections, key=lambda s: s.orderIndex):
        for q in sorted(sec.questions, key=lambda x: x.orderIndex):
            out.append((sec, q))
    return out


async def resolve_asset(db, tpl: CamsTemplate, asset_id: str) -> FireEquipment:
    """The asset a run is about — and the check the paper process cannot make.

    The client's inspection sheet and their register are separate documents, so
    today nothing stops an FE inspection being filed against a cylinder that is
    not in the register at all. Requiring the asset to resolve is the build spec's
    one deliberate improvement on the paper process, and the reason the FE
    Inspection screen can pre-fill the sheet's "Fire Extinguisher Type" and "Fire
    Extinguisher No" headers instead of asking the inspector to copy them.

    The type check is equally load-bearing: a hydrant checklist filed against a
    smoke detector would score, sign off and file perfectly, and be worthless.
    """
    asset = await db.get(FireEquipment, asset_id)
    if asset is None or asset.isDeleted:
        raise ChecklistError("Asset not found in the fire register.", 404)
    expected = template_meta(tpl).get("assetType")
    if expected and asset.type != expected:
        raise ChecklistError(
            f"'{tpl.name}' applies to {expected.replace('_', ' ').lower()} assets; "
            f"{asset.equipmentCode} is a {asset.type.replace('_', ' ').lower()}.",
            409,
        )
    return asset


async def find_run(db, tpl: CamsTemplate, asset_id: str, period: str) -> CamsEngagement | None:
    return (
        await db.execute(
            select(CamsEngagement)
            .where(CamsEngagement.templateId == tpl.id)
            .where(CamsEngagement.sourceEntityId == asset_id)
            .where(CamsEngagement.periodLabel == period)
            .where(CamsEngagement.isDeleted.is_(False))
        )
    ).scalars().first()


async def get_or_create_run(
    db, tpl: CamsTemplate, asset: FireEquipment, period: str, *, actor_id: str,
) -> tuple[CamsEngagement, bool]:
    """Resolve the run for (template, asset, period), creating it if absent.

    Returns (run, created). The insert is attempted optimistically and the unique
    violation is caught rather than pre-locked: under the real access pattern —
    several people opening today's sheet within the same second — a SELECT-then-
    INSERT would produce duplicates and a table lock would serialise every
    inspector in the plant behind one another. Letting the index arbitrate is
    both correct and the cheapest option.
    """
    meta = template_meta(tpl)
    validate_period(meta.get("frequency", "DAILY"), period)

    existing = await find_run(db, tpl, asset.id, period)
    if existing is not None:
        return existing, False

    if tpl.status != "APPROVED":
        raise ChecklistError(
            f"Template '{tpl.templateCode}' is {tpl.status}; a checklist cannot be raised against "
            "an unapproved controlled document.", 409,
        )

    planned = period_start(meta.get("frequency", "DAILY"), period)
    code = await cams_svc.next_engagement_code(db, "INSPECTION")
    run = CamsEngagement(
        engagementCode=code,
        title=f"{tpl.name} — {asset.equipmentCode} — {period}",
        engagementType="INSPECTION",
        auditTypeId=meta.get("auditTypeId"),
        standardRefs=[meta.get("documentNo")] if meta.get("documentNo") else [],
        siteId=asset.plantId,
        areaOrAssetRef=asset.equipmentCode,
        scopeStatement=f"{meta.get('documentNo', '')} {meta.get('revision', '')} — {asset.location}".strip(),
        # The person opening the sheet is the one filling it. CAMS requires a lead
        # auditor and a routine daily round has no separate assignment step.
        leadAuditorId=actor_id,
        plannedDate=planned,
        templateId=tpl.id,
        templateVersionUsed=tpl.version,
        # Straight to IN_PROGRESS: a periodic sheet that exists is one somebody is
        # filling in. PLANNED/SCHEDULED model a *scheduled* audit with a booking
        # and a team, which a daily round is not, and stepping through them would
        # be two extra clicks per day per panel for no recorded fact.
        status="IN_PROGRESS",
        sourceModule=SOURCE_MODULE,
        sourceEntityId=asset.id,
        periodLabel=period,
        createdBy=actor_id,
        updatedBy=actor_id,
    )
    db.add(run)
    try:
        await db.flush()
    except IntegrityError:
        # Lost the race. Someone else's identical row is the right one to use.
        await db.rollback()
        winner = await find_run(db, tpl, asset.id, period)
        if winner is None:
            raise
        return winner, False

    db.add(CamsResponse(engagementId=run.id, templateVersionUsed=tpl.version, answers=[], sectionScores=[]))
    await db.flush()
    return run, True


async def load_response(db, run: CamsEngagement) -> CamsResponse:
    resp = (
        await db.execute(select(CamsResponse).where(CamsResponse.engagementId == run.id))
    ).scalars().first()
    if resp is None:
        resp = CamsResponse(engagementId=run.id, templateVersionUsed=run.templateVersionUsed or 1,
                            answers=[], sectionScores=[])
        db.add(resp)
        await db.flush()
    return resp


# ═══════════════════════════════════════════════════════════════════════════
# Answers
# ═══════════════════════════════════════════════════════════════════════════
# The sheets say: Yes if satisfactory, No if unsatisfactory, NA if not
# applicable. CAMS speaks CONFORM / NC / NA. Same three states, different words —
# translated here so the checklist screens can print the client's vocabulary
# while the engine scores in its own.
_VALUE_TO_CONFORMANCE = {"YES": "CONFORM", "NO": "NC", "NA": "NA"}
CONFORMANCE_TO_VALUE = {v: k for k, v in _VALUE_TO_CONFORMANCE.items()}


def _coerce(q: CamsTemplateQuestion, raw: Any) -> tuple[str | None, str | None]:
    """(storedValue, conformance) for one submitted answer.

    A NUMERIC or TEXT item has no conformance — it is a reading, not a judgement
    (battery voltage, serial number of the detector removed). Scoring skips
    answers with no conformance, so a monthly sheet's score reflects its pass/fail
    checks and is not diluted by the fact that someone wrote down a voltage.
    """
    if raw is None or raw == "":
        return None, None
    if q.questionType == "YES_NO_NA":
        token = str(raw).strip().upper()
        if token not in _VALUE_TO_CONFORMANCE:
            raise ChecklistError(f"'{raw}' is not a valid answer for '{q.text[:60]}' (expected YES, NO or NA).")
        return token, _VALUE_TO_CONFORMANCE[token]
    if q.questionType == "NUMERIC":
        try:
            float(str(raw))
        except (TypeError, ValueError) as exc:
            raise ChecklistError(f"'{raw}' is not a number for '{q.text[:60]}'.") from exc
        return str(raw).strip(), None
    return str(raw).strip(), None


async def save_answers(
    db, tpl: CamsTemplate, run: CamsEngagement, answers: list[dict[str, Any]], *, actor_id: str,
) -> CamsResponse:
    """Merge answers into the run. Rejected once the run is signed off."""
    if is_locked(run):
        raise ChecklistError(
            f"This checklist is {stage_of(run)} and cannot be edited. "
            "Record a correction as a new run for a corrective period.", 409,
        )
    pairs = ordered_questions(tpl)
    q_by_id = {q.id: q for _sec, q in pairs}
    q_by_key = {q.standardClauseRef: q for _sec, q in pairs if q.standardClauseRef}

    resp = await load_response(db, run)
    merged = {a["questionId"]: a for a in (resp.answers or [])}

    for a in answers:
        qid = a.get("questionId")
        q = q_by_id.get(qid) if qid else q_by_key.get(a.get("itemKey"))
        if q is None:
            # Silently skipping an unknown item would let a stale client blank a
            # sheet by omission and report success.
            raise ChecklistError(f"Unknown checklist item '{a.get('itemKey') or qid}'.", 400)
        value, conformance = _coerce(q, a.get("value"))
        prev = merged.get(q.id, {})
        merged[q.id] = {
            "questionId": q.id,
            "value": value,
            "conformance": conformance,
            "note": (a.get("note") or prev.get("note") or ""),
            "evidenceAttachmentIds": a.get("evidenceAttachmentIds") or prev.get("evidenceAttachmentIds") or [],
            "ncSeverity": a.get("ncSeverity") or prev.get("ncSeverity"),
            "findingId": prev.get("findingId"),
        }

    resp.answers = list(merged.values())
    run.updatedBy = actor_id
    await db.flush()
    return resp


def unanswered_mandatory(tpl: CamsTemplate, resp: CamsResponse) -> list[str]:
    """Mandatory items with no answer — the submit gate.

    Blank is not the same as NA. An inspector who has not looked at the hose box
    and one who has looked and found the check inapplicable are recording
    different facts, and a sheet that treats them the same is a sheet that cannot
    be audited.
    """
    answered = {a["questionId"] for a in (resp.answers or []) if a.get("value") not in (None, "")}
    return [q.text for _sec, q in ordered_questions(tpl) if q.isMandatory and q.id not in answered]


# ═══════════════════════════════════════════════════════════════════════════
# Sign-off chain
# ═══════════════════════════════════════════════════════════════════════════
def signature_enforced(tpl: CamsTemplate) -> bool:
    """Whether this sheet demands a drawn/typed signature per record.

    Per template, not global, and the reason is practical rather than lax. A daily
    round is signed once for the month on the paper original; demanding 31 drawn
    signatures for 31 daily records gets the tablet handed round and one person
    signing for everybody, which is weaker evidence than the userId stamp alone.
    Monthly, quarterly and annual sheets each print their own signature block, so
    those enforce.

    A template can override with `documentMeta.requireSignature`.
    """
    meta = template_meta(tpl)
    override = meta.get("requireSignature")
    if isinstance(override, bool):
        return override
    return meta.get("frequency") != "DAILY"


async def advance(
    db, tpl: CamsTemplate, run: CamsEngagement, to_stage: str, *, actor_id: str,
    # (userId, display name), resolved by the caller from a LIVE user row. Plain
    # strings rather than the ORM object on purpose: reading an attribute off an
    # expired instance triggers a lazy refresh, which under asyncio raises
    # MissingGreenlet instead of issuing a query — so a service that reads one
    # fails or not depending on how far its caller is from the last commit.
    # See fire_signoff.signer_identity.
    signer: tuple[str, str] | None = None,
    signature_kind: str | None = None,
    signature_payload: str | None = None,
    typed_name: str | None = None,
    designation: str | None = None,
) -> tuple[CamsEngagement, dict[str, Any]]:
    """Move a run one stage along Prepared -> Reviewed -> Approved.

    Order is enforced by requiring the exact predecessor stage, so approving a
    draft is a 409 rather than a silent two-step jump. Findings and CAPAs are
    raised at SUBMITTED — the moment the sheet stops being a working copy — not at
    approval, so a reviewer sees the same defects the preparer did and is
    reviewing something the CAPA register already knows about.

    Returns (run, outcome). `outcome` carries what the transition created — the
    defects raised and CAPAs opened — because an operator who has just submitted a
    sheet with four failures needs telling, not a silent 200.
    """
    step = _STAGE_STEP.get(to_stage)
    if step is None:
        raise ChecklistError(f"'{to_stage}' is not a sign-off stage.", 400)
    required, new_status = step
    current = stage_of(run)
    if current != required:
        raise ChecklistError(
            f"Cannot move to {to_stage}: this checklist is {current} and {to_stage} follows {required}.", 409,
        )

    resp = await load_response(db, run)
    now = _now()
    outcome: dict[str, Any] = {}

    # ── signature, before any state moves ────────────────────────────────────
    # Validated and built first so a bad payload fails the transition instead of
    # leaving a stage advanced with no signature against it.
    sig_entry = None
    if signer is not None:
        try:
            offered = fire_signoff.require_for_stage(
                run, to_stage,
                signature_kind=signature_kind,
                signature_payload=signature_payload,
                typed_name=typed_name,
                enforce=signature_enforced(tpl),
            )
        except fire_signoff.SignatureRequired as exc:
            raise ChecklistError(str(exc), 400) from exc
        if offered is not None:
            try:
                sig_entry = fire_signoff.build_entry(
                    stage=to_stage, user_id=signer[0], user_name=signer[1],
                    signature_kind=offered["kind"],
                    signature_payload=offered["payload"],
                    typed_name=offered["typed"],
                    designation=designation,
                )
            except ValueError as exc:
                raise ChecklistError(str(exc), 400) from exc

    if to_stage == STAGE_SUBMITTED:
        missing = unanswered_mandatory(tpl, resp)
        if missing:
            head = "; ".join(m[:70] for m in missing[:3])
            more = f" (+{len(missing) - 3} more)" if len(missing) > 3 else ""
            raise ChecklistError(f"{len(missing)} required check(s) not answered: {head}{more}", 400)
        answers_by_q = {a["questionId"]: a for a in (resp.answers or [])}
        sections = sorted(tpl.sections, key=lambda s: s.orderIndex)

        # Every "No" becomes a tracked defect, deduped to one open CAPA per
        # (asset, item) so an unfixed lamp is one CAPA with 29 occurrences rather
        # than 29 CAPAs. See services/fire_capa.py.
        asset = await db.get(FireEquipment, run.sourceEntityId) if run.sourceEntityId else None
        outcome = await fire_capa.sync_failures(
            db,
            run=run,
            sections=sections,
            answers_by_q=answers_by_q,
            asset_id=run.sourceEntityId or "",
            asset_code=(asset.equipmentCode if asset else run.areaOrAssetRef or ""),
            period_label=run.periodLabel or "",
            actor_id=actor_id,
        )
        score = cams_svc.compute_score(sections, answers_by_q, tpl.scoringConfig)
        resp.answers = list(answers_by_q.values())
        resp.sectionScores = score["sectionScores"]
        resp.completedBy = actor_id
        resp.completedAt = now
        run.scorePercent = score["scorePercent"]
        run.overallResult = score["overallResult"]
        run.conductedDate = now
    elif to_stage == STAGE_REVIEWED:
        run.reviewedBy, run.reviewedAt = actor_id, now
    else:
        run.approvedBy, run.approvedAt = actor_id, now

    if sig_entry is not None:
        fire_signoff.record(run, sig_entry)
        outcome["signature"] = {"role": sig_entry["role"], "kind": sig_entry["signatureKind"]}

    run.status = new_status
    run.updatedBy = actor_id
    await db.flush()
    return run, outcome


# ═══════════════════════════════════════════════════════════════════════════
# Serialisation — single run and grid
# ═══════════════════════════════════════════════════════════════════════════
def _answer_out(q: CamsTemplateQuestion, ans: dict[str, Any] | None) -> dict[str, Any]:
    ans = ans or {}
    return {
        "value": ans.get("value"),
        "conformance": ans.get("conformance"),
        "note": ans.get("note") or "",
        "findingId": ans.get("findingId"),
    }


def run_out(tpl: CamsTemplate, run: CamsEngagement, resp: CamsResponse | None,
            asset: FireEquipment | None = None) -> dict[str, Any]:
    """A single run, sectioned exactly as the source sheet prints it."""
    answers = {a["questionId"]: a for a in ((resp.answers if resp else None) or [])}
    meta = template_meta(tpl)
    sections = []
    for sec in sorted(tpl.sections, key=lambda s: s.orderIndex):
        sections.append({
            "id": sec.id,
            "title": sec.title,
            "note": (meta.get("sectionNotes") or {}).get(sec.title),
            "items": [
                {
                    "questionId": q.id,
                    "itemKey": q.standardClauseRef,
                    "text": q.text,
                    "type": q.questionType,
                    "guidance": q.guidance,
                    "mandatory": q.isMandatory,
                    **_answer_out(q, answers.get(q.id)),
                }
                for q in sorted(sec.questions, key=lambda x: x.orderIndex)
            ],
        })
    return {
        "runId": run.id,
        "engagementCode": run.engagementCode,
        "templateCode": tpl.templateCode,
        "templateName": tpl.name,
        "document": meta,
        "assetId": run.sourceEntityId,
        "assetCode": asset.equipmentCode if asset else run.areaOrAssetRef,
        "assetLocation": asset.location if asset else None,
        "plantId": run.siteId,
        "periodLabel": run.periodLabel,
        "stage": stage_of(run),
        "camsStatus": run.status,
        "locked": is_locked(run),
        "scorePercent": run.scorePercent,
        "overallResult": run.overallResult,
        "signOff": {
            "preparedBy": resp.completedBy if resp else None,
            "preparedAt": resp.completedAt.isoformat() if resp and resp.completedAt else None,
            "reviewedBy": run.reviewedBy,
            "reviewedAt": run.reviewedAt.isoformat() if run.reviewedAt else None,
            "approvedBy": run.approvedBy,
            "approvedAt": run.approvedAt.isoformat() if run.approvedAt else None,
            "roles": meta.get("signOffRoles", []),
            # The captured marks, with images — a single-record view renders them.
            # Grid responses use fire_signoff.summary() instead, which strips the
            # images: 31 daily records x 3 signatures would be ~23 MB of base64.
            "signatures": fire_signoff.out(run),
            "signatureRequired": None,
        },
        "sections": sections,
    }


async def grid_out(
    db, tpl: CamsTemplate, asset: FireEquipment, window: str,
) -> dict[str, Any]:
    """The paper page: items down, periods across.

    One query for the whole page rather than one per column — a month of daily
    runs is 31 columns, and 31 round-trips to render one screen is the difference
    between a page that opens and a page an inspector gives up on.
    """
    meta = template_meta(tpl)
    layout = meta.get("layout", LAYOUT_DAY_GRID)
    cols = grid_periods(layout, meta.get("frequency", "DAILY"), window)
    labels = [c[0] for c in cols]

    runs = (
        await db.execute(
            select(CamsEngagement)
            .where(CamsEngagement.templateId == tpl.id)
            .where(CamsEngagement.sourceEntityId == asset.id)
            .where(CamsEngagement.periodLabel.in_(labels))
            .where(CamsEngagement.isDeleted.is_(False))
        )
    ).scalars().all()
    by_period = {r.periodLabel: r for r in runs}

    responses: dict[str, CamsResponse] = {}
    if runs:
        for r in (
            await db.execute(
                select(CamsResponse).where(CamsResponse.engagementId.in_([x.id for x in runs]))
            )
        ).scalars().all():
            responses[r.engagementId] = r

    # answers[periodLabel][questionId]
    answers: dict[str, dict[str, dict[str, Any]]] = {}
    for period, run in by_period.items():
        resp = responses.get(run.id)
        answers[period] = {a["questionId"]: a for a in ((resp.answers if resp else None) or [])}

    closed: dict[str, str] = {}
    if layout == LAYOUT_DAY_GRID:
        closed = await non_working_days(db, asset.plantId, [date.fromisoformat(p) for p in labels])

    columns = [
        {
            "periodLabel": p,
            "header": header,
            "runId": by_period[p].id if p in by_period else None,
            "stage": stage_of(by_period[p]) if p in by_period else None,
            "locked": is_locked(by_period[p]) if p in by_period else False,
            "nonWorkingDay": closed.get(p),
        }
        for p, header in cols
    ]

    rows = []
    for sec, q in ordered_questions(tpl):
        rows.append({
            "questionId": q.id,
            "itemKey": q.standardClauseRef,
            "sectionTitle": sec.title,
            "text": q.text,
            "type": q.questionType,
            "guidance": q.guidance,
            "cells": {
                p: _answer_out(q, answers.get(p, {}).get(q.id))
                for p in labels
            },
        })

    return {
        "templateCode": tpl.templateCode,
        "templateName": tpl.name,
        "document": meta,
        "layout": layout,
        "window": window,
        "assetId": asset.id,
        "assetCode": asset.equipmentCode,
        "assetLocation": asset.location,
        "assetType": asset.type,
        "assetSubtype": asset.assetSubtype,
        "allottedSerialNo": asset.allottedSerialNo,
        "plantId": asset.plantId,
        "columns": columns,
        "rows": rows,
    }


def default_window(layout: str, when: date | None = None) -> str:
    when = when or _now().date()
    return f"{when.year:04d}-{when.month:02d}" if layout == LAYOUT_DAY_GRID else f"{when.year:04d}"


def shift_window(layout: str, window: str, delta: int) -> str:
    """Previous/next page. Month steps for a daily grid, year steps otherwise."""
    if layout == LAYOUT_DAY_GRID:
        y, m = int(window[:4]), int(window[5:7])
        total = y * 12 + (m - 1) + delta
        return f"{total // 12:04d}-{total % 12 + 1:02d}"
    return f"{int(window) + delta:04d}"


__all__ = [
    "ChecklistError", "SOURCE_MODULE", "signature_enforced",
    "STAGE_DRAFT", "STAGE_SUBMITTED", "STAGE_REVIEWED", "STAGE_APPROVED",
    "stage_of", "is_locked", "period_label", "validate_period", "period_start",
    "grid_periods", "non_working_days", "load_template", "template_meta",
    "ordered_questions", "resolve_asset", "find_run", "get_or_create_run",
    "load_response", "save_answers", "unanswered_mandatory", "advance",
    "run_out", "grid_out", "default_window", "shift_window",
    "CONFORMANCE_TO_VALUE",
]
