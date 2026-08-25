"""A "No" on a fire checklist raises a CAPA.

The earlier build shipped with finding creation OFF on every item but one, on the
grounds that a daily grid would flood the register. That reasoning was sound and
the conclusion was wrong: the answer to "this would raise 217 CAPAs a month" is
not "then raise none", it is "then stop raising the same CAPA 30 times".

THE FLOOD, AND WHAT ACTUALLY FIXES IT
------------------------------------
A Daily Fire Alarm sheet has 7 checks. A month is 31 records. If the power
indicator lamp is dead on the 3rd and nobody fixes it, the inspector answers
"No" on the 3rd, 4th, 5th … 31st. Raising a CAPA per cell gives 29 CAPAs for one
dead lamp, and a register nobody reads.

But those 29 answers are not 29 problems. They are one unresolved defect,
observed 29 times. So the rule is:

    one open CAPA per (asset, checklist item)

The first "No" raises a finding and a CAPA. Every later "No" on the same item for
the same asset, while that CAPA is still open, attaches to the SAME finding and
increments `CamsFinding.occurrenceCount` instead of raising a second one. The
finding also carries `lastObservedAt` and `observedPeriods`, so the CAPA owner
sees "dead 12 days running, last on the 24th" — which is more actionable than
twelve separate tickets, not less.

Those are typed columns, not a JSON side-car. The first attempt at this wrote the
count into a `sourceMetadata` attribute that does not exist on CamsFinding: every
write silently did nothing, the dedupe still looked correct, and the count sat
permanently at 2. A column fails loudly.

That dedupe is also what makes the feature honest across cadences. Without it a
monthly sheet raises one CAPA a month for an unfixed valve and a daily sheet
raises thirty for the same class of problem, and the two registers are not
comparable.

RE-OBSERVATION AFTER CLOSURE IS A REPEAT, NOT A DUPLICATE
---------------------------------------------------------
Once the CAPA is closed and the lamp fails again, a new finding IS raised — and
flagged `isRepeatFinding` with `repeatOfFindingId` pointing at the old one. CAMS
already has both columns and a repeat-detection analytic; a recurrence that
silently reuses a closed CAPA would hide exactly the pattern that analytic exists
to surface.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not close anything. A later "Yes" records that the condition was not
observed on that date; it does not prove the defect was fixed, and a CAPA that
closes itself because someone ticked a box is not a CAPA. Closure stays a human
act through the existing CAPA workflow.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.models.cams import CamsEngagement, CamsFinding, CamsTemplateQuestion, CamsTemplateSection
from app.services import cams as cams_svc

log = logging.getLogger(__name__)

SOURCE_MODULE = "FIRE"

# Findings in these states are settled; a fresh observation after one of them is a
# recurrence and deserves its own record.
TERMINAL_FINDING_STATES = ("CLOSED", "CLOSED_RECURRED", "VERIFIED", "CANCELLED")

# Default severity for a failed routine check. MINOR_NC because a housekeeping
# defect on a daily round is not a major non-conformance, and inflating every one
# of them makes the severity field meaningless. Items that ARE serious carry
# `ncSeverity` in the template (see fire_checklist_templates.Item).
DEFAULT_SEVERITY = "MINOR_NC"

# Severities the platform treats as CAPA-mandatory. A finding at or above this is
# raised with `requiresCapa`, which the deferred DB constraint from the Fire &
# Life Safety build then enforces at COMMIT.
CAPA_MANDATORY = ("MAJOR_NC", "CRITICAL_NC")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _question_index(
    sections: list[CamsTemplateSection],
) -> dict[str, tuple[CamsTemplateQuestion, CamsTemplateSection]]:
    return {q.id: (q, sec) for sec in sections for q in sec.questions}


async def _open_finding_for(
    db, *, asset_id: str, item_key: str,
) -> CamsFinding | None:
    """The still-open finding for this (asset, item), if there is one.

    Keyed on `areaOrAssetRef` + `sourceQuestionId`. `sourceQuestionId` is the
    template question id, which is stable across periods AND across re-seeds
    (the seeder reuses question rows by item key), so a month of daily records
    all resolve to the same finding.
    """
    rows = (
        await db.execute(
            select(CamsFinding)
            .where(CamsFinding.areaOrAssetRef == asset_id)
            .where(CamsFinding.sourceQuestionId == item_key)
            .where(CamsFinding.status.notin_(TERMINAL_FINDING_STATES))
            .order_by(CamsFinding.createdAt.desc())
        )
    ).scalars().all()
    return rows[0] if rows else None


async def _last_closed_finding_for(db, *, asset_id: str, item_key: str) -> CamsFinding | None:
    rows = (
        await db.execute(
            select(CamsFinding)
            .where(CamsFinding.areaOrAssetRef == asset_id)
            .where(CamsFinding.sourceQuestionId == item_key)
            .where(CamsFinding.status.in_(TERMINAL_FINDING_STATES))
            .order_by(CamsFinding.closedAt.desc().nulls_last(), CamsFinding.createdAt.desc())
        )
    ).scalars().all()
    return rows[0] if rows else None


def _severity_for(q: CamsTemplateQuestion) -> str:
    """Per-item severity, from the template. Falls back to MINOR_NC."""
    # `options` is the generic per-question config slot on CamsTemplateQuestion;
    # the fire seeder writes {"ncSeverity": "..."} into it for the handful of
    # checks that are genuinely major. Reusing it avoids a column that one module
    # would populate.
    opts = q.options
    if isinstance(opts, dict):
        sev = opts.get("ncSeverity")
        if sev in ("OBSERVATION", "MINOR_NC", "MAJOR_NC", "CRITICAL_NC"):
            return sev
    if isinstance(opts, list):
        for entry in opts:
            if isinstance(entry, dict) and entry.get("ncSeverity"):
                sev = entry["ncSeverity"]
                if sev in ("OBSERVATION", "MINOR_NC", "MAJOR_NC", "CRITICAL_NC"):
                    return sev
    return DEFAULT_SEVERITY


async def sync_failures(
    db,
    *,
    run: CamsEngagement,
    sections: list[CamsTemplateSection],
    answers_by_q: dict[str, dict[str, Any]],
    asset_id: str,
    asset_code: str,
    period_label: str,
    actor_id: str,
    raise_capa: bool = True,
) -> dict[str, Any]:
    """Turn every "No" on this run into a finding, and a CAPA where warranted.

    Mutates `answers_by_q` in place to record `findingId` / `capaId` against the
    answer, so the checklist screen can link a failed cell straight to the CAPA
    it raised — the thing that makes this feature useful rather than merely
    compliant.

    Returns a summary the router hands back so the operator is told what their
    submit just created, instead of finding out later.
    """
    q_index = _question_index(sections)
    created_findings: list[dict[str, Any]] = []
    reopened: list[dict[str, Any]] = []
    recurring: list[dict[str, Any]] = []
    capas: list[str] = []

    for qid, ans in answers_by_q.items():
        if ans.get("conformance") != "NC":
            continue
        pair = q_index.get(qid)
        if pair is None:
            continue
        q, _sec = pair
        if not q.ncTriggersFinding:
            # Opt-out is per item and deliberate — see the seeder for which and why.
            continue

        item_key = q.id
        existing = await _open_finding_for(db, asset_id=asset_id, item_key=item_key)

        if existing is not None:
            # Same defect, still open, observed again. One CAPA, one more tick on
            # the counter. Idempotent on the period label so re-submitting the same
            # record does not inflate the count — a corrected sheet is not a new
            # observation.
            observed = list(existing.observedPeriods or [])
            already = period_label in observed
            if not already:
                observed = (observed + [period_label])[-60:]
                existing.observedPeriods = observed
                existing.occurrenceCount = (existing.occurrenceCount or 1) + 1
                existing.lastObservedAt = _now()
                remark = (ans.get("note") or "").strip()
                existing.description = (
                    f"{existing.description or existing.title}\n\n"
                    f"Observed again in {period_label} ({run.engagementCode}). "
                    f"Occurrences: {existing.occurrenceCount}."
                    # A recurrence with a fresh remark is the useful kind — it is
                    # how "still not fixed" is told apart from "worse than last
                    # month" on a defect that has been open for three periods.
                    + (f"\nObserved: {remark}" if remark else "")
                )[:8000]
            ans["findingId"] = existing.id
            if existing.capaId:
                ans["capaId"] = existing.capaId
            reopened.append({
                "findingId": existing.id, "findingCode": existing.findingCode,
                "item": q.text[:90], "occurrences": existing.occurrenceCount,
                "capaId": existing.capaId, "alreadyCounted": already,
            })
            continue

        # New finding. If the same item failed before and was closed, this is a
        # recurrence, which CAMS's repeat analytic needs flagged.
        prior = await _last_closed_finding_for(db, asset_id=asset_id, item_key=item_key)
        severity = _severity_for(q)
        code = await cams_svc.next_finding_code(db)
        finding = CamsFinding(
            findingCode=code,
            engagementId=run.id,
            sourceQuestionId=item_key,
            title=f"{asset_code}: {q.text}"[:200],
            # The inspector's own remark leads, when there is one. Without it the
            # defect said only that an item was marked "No" — so whoever picks up
            # the CAPA learns that the identification number is unreadable but not
            # that it is painted over, which is the half that decides the fix. The
            # sheet's own footnote tells the inspector to "write comments"; this is
            # where those comments have to end up.
            description=(
                (f"Observed: {(ans.get('note') or '').strip()}\n\n" if (ans.get("note") or "").strip() else "")
                + f'Checklist "{run.title}" recorded "No" for: {q.text}\n'
                + f"Asset: {asset_code}\nPeriod: {period_label}\n"
                + f"Record: {run.engagementCode}"
            )[:8000],
            severity=severity,
            standardClauseRef=q.standardClauseRef,
            siteId=run.siteId,
            # The ASSET id, not a free-text area. This is the key the dedupe above
            # reads, so it has to be the id and not a display string.
            areaOrAssetRef=asset_id,
            status="OPEN",
            isRepeatFinding=prior is not None,
            repeatOfFindingId=prior.id if prior is not None else None,
            evidenceAttachmentIds=[],
        )
        finding.occurrenceCount = 1
        finding.lastObservedAt = _now()
        finding.observedPeriods = [period_label]
        # `requiresCapa` is guarded because it arrived with the Fire & Life Safety
        # build and a slim deployment may predate it — unlike the occurrence
        # columns above, which this module itself introduced.
        if hasattr(finding, "requiresCapa"):
            finding.requiresCapa = raise_capa and severity in CAPA_MANDATORY
        db.add(finding)
        await db.flush()
        ans["findingId"] = finding.id

        entry = {
            "findingId": finding.id, "findingCode": finding.findingCode,
            "item": q.text[:90], "severity": severity,
            "isRepeat": prior is not None,
        }

        if raise_capa:
            try:
                capa = await cams_svc.raise_capa_for_finding(
                    db, finding, run, actor_id=actor_id,
                )
                finding.capaId = capa.id
                ans["capaId"] = capa.id
                entry["capaId"] = capa.id
                entry["capaNumber"] = getattr(capa, "capaNumber", None)
                capas.append(capa.id)
            except Exception as exc:  # noqa: BLE001
                # A CAPA that cannot be raised must not lose the finding. The
                # finding is the record of the failure; the CAPA is the follow-up,
                # and swallowing the whole submit because CAPA source types are
                # unseeded would lose the inspection itself.
                log.warning("Fire checklist finding %s raised without a CAPA: %s", code, exc)
                entry["capaError"] = str(exc)[:200]

        (recurring if prior is not None else created_findings).append(entry)

    await db.flush()
    return {
        "findingsCreated": created_findings,
        "findingsRecurring": recurring,
        "findingsUpdated": reopened,
        "capasRaised": capas,
        "totalFailures": len(created_findings) + len(recurring) + len(reopened),
    }


def summarise(result: dict[str, Any]) -> str | None:
    """One human sentence for the toast after a submit."""
    new = len(result.get("findingsCreated") or []) + len(result.get("findingsRecurring") or [])
    again = len(result.get("findingsUpdated") or [])
    capas = len(result.get("capasRaised") or [])
    if not (new or again):
        return None
    bits = []
    if new:
        bits.append(f"{new} defect{'s' if new != 1 else ''} raised")
    if capas:
        bits.append(f"{capas} CAPA{'s' if capas != 1 else ''} opened")
    if again:
        bits.append(f"{again} existing defect{'s' if again != 1 else ''} still open (occurrence recorded)")
    return "; ".join(bits) + "."


__all__ = ["sync_failures", "summarise", "TERMINAL_FINDING_STATES", "DEFAULT_SEVERITY"]
