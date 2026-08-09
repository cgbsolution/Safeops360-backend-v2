"""Audit-report insight layer — deterministic, computed once at issue.

Same contract as the eight list-screen rule modules (`rules_incident`,
`rules_capa`, …): rules compute evidence, `templates.fill` phrases it, no model
runs anywhere. The difference is *when*: a list screen recomputes on every view
behind a 15-minute cache, whereas this runs ONCE inside `_build_report_snapshot`
and is frozen into the immutable snapshot alongside everything else it hashes.
Re-viewing an issued report can never change a headline here — a changed
underlying record produces a new issue (I02, I03 …), never a silent edit.

That freezing is also why this module takes the built snapshot dict rather than
an `AsyncSession`: every number it needs was already counted upstream, so it
does no I/O at all and cannot disagree with the register printed below it.

On clustering by "observation template"
---------------------------------------
The spec asked for findings to be grouped by a shared observation-template id.
There is no such field: `AuditCheckpointResponse.observation` is freeform Text
(models/audit_compliance.py) and nothing upstream stamps a template reference.
The verbatim repeats visible in the demo fixture come from a canned list in
`scripts/seed_complete_internal_audit.py` — a property of the seeder, not of the
product.

So the patterns below cluster on STRUCTURED fields first — owner, discipline,
requirement type, CAPA coverage, escalation path — all of which mean the same
thing in seeded and real data. Identical-wording clustering is kept, because
copy-pasted observation text is a genuine signal, but it is the last tier, it is
gated hardest (§`_WORDING_MIN`, ≥2 disciplines), and it is phrased as what it
actually observed — identical wording — rather than as a diagnosed root cause.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from app.services import page_grading as pg
from app.services.insights.templates import fill

# ── Gates ────────────────────────────────────────────────────────────────
# Below the floor the whole insight layer is suppressed: an "insight" over two
# findings is a restatement of the register, and page 1 of a compliance report
# is the last place to spend a reader's trust on one (mirrors common.MIN_RECORDS
# for the list screens, at the granularity this report actually has).
MIN_FINDINGS = 4

_CONCENTRATION_PCT = 50.0   # share of a tier one holder needs to BE the pattern
_CONCENTRATION_MIN = 3      # …and the floor beneath which a share is just noise
_WORDING_MIN = 3            # identical observations before wording is a signal
_WORDING_MIN_DISCIPLINES = 2
_CAPA_GAP_MIN = 2
_MAX_PATTERNS = 5           # page 1 holds five; the register below holds the rest
_MAX_CHIPS = 12             # CAPA strip chips before it stops being scannable

_ADVERSE = ("FAIL", "PARTIAL")
_SEVERE = ("critical", "major")

# Compliance band for the gauge. Distinct from `scoring_rules.evaluate`, which
# owns the PASS/FAIL verdict — this is presentation banding only and must never
# be read as the verdict (see `gauge()`).
_BANDS = ((95.0, "green", "Strong"), (80.0, "amber", "Needs improvement"))

_WS_ESCALATED = "ESCALATED_PM"

# Punctuation/whitespace/digit noise dropped before two observations are called
# identical: "…slipped twice." and "…slipped twice" are the same sentence.
_NORM_RE = re.compile(r"[^a-z ]+")
_WS_RE = re.compile(r"\s+")


def _norm(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", _NORM_RE.sub(" ", text.lower())).strip()


def _band(pct: float | None) -> tuple[str, str]:
    if pct is None:
        return "neutral", "Not assessed"
    for floor, key, label in _BANDS:
        if pct >= floor:
            return key, label
    return "red", "Critical"


def _pattern(
    pid: str, kind: str, severity: str, confidence: str,
    headline: str, evidence: str, refs: list[str], action: str | None = None,
) -> dict[str, Any]:
    return {
        "id": pid, "kind": kind, "severity": severity, "confidence": confidence,
        "headline": headline, "evidence": evidence,
        "recordRefs": refs[:12], "refCount": len(refs),
        "suggestedAction": action,
    }


def _confidence(supporting: int) -> str:
    """Confidence is a function of sample size, never vibes — the same ladder
    `insights.common.confidence_for` applies, at report granularity (a 120-row
    audit will not produce the 15-record clusters a year of incidents does)."""
    if supporting >= 8:
        return "high"
    if supporting >= 4:
        return "medium"
    return "low"


def _refs(items: list[dict[str, Any]]) -> list[str]:
    return [str(f.get("checkpointCode") or "—") for f in items]


# ── Section 1 blocks ─────────────────────────────────────────────────────


def gauge(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The overall compliance dial.

    Presentation only. `criticalGate` and `passed` are read straight off the
    `gate` block that `scoring_rules.evaluate` already wrote — this function
    does not re-derive the pass/fail rule, so the dial cannot drift from the
    verdict printed beside it. The colour override below is a VISUAL echo of
    that rule (any critical fail ⇒ red, whatever the percentage), not a second
    implementation of it.
    """
    grade = snapshot.get("grade") or {}
    gate = snapshot.get("gate") or {}
    pct = snapshot.get("overallScorePct")
    show = bool(grade.get("showGrade", True))
    crit = int(snapshot.get("criticalFailures") or 0)

    band, band_label = _band(pct if show else None)
    display_band = "red" if crit > 0 else band

    return {
        "pct": pct if show else None,
        "showGrade": show,
        "band": band,
        "bandLabel": band_label,
        # What the dial is PAINTED — red on a critical fail regardless of pct.
        "displayBand": display_band,
        "criticalGate": crit > 0,
        "result": snapshot.get("overallResult"),
        "passed": gate.get("passed"),
        "explanation": gate.get("explanation"),
        "assessed": grade.get("assessed"),
        "applicable": grade.get("applicable"),
        "coverageLabel": grade.get("label") if not show else None,
        # The arithmetic behind the dial. A percentage a reader cannot check is
        # a percentage they have to trust, and this report is read by people
        # whose job is not to trust it.
        "scoreObtained": snapshot.get("scoreObtained"),
        "scoreAllotted": snapshot.get("scoreAllotted"),
    }


def critical_banner(snapshot: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The fixed FAIL banner. Absent when there is no critical failure — an
    all-clear banner would train readers to skim past the real one."""
    crit = int(snapshot.get("criticalFailures") or 0)
    if crit <= 0:
        return None
    items = [f for f in findings if str(f.get("severity") or "").lower() == "critical"
             and f.get("assessmentStatus") == "FAIL"]
    return {
        "count": crit,
        "headline": fill("audit.report.critical_gate", count=crit,
                         plural="" if crit == 1 else "s"),
        "codes": _refs(items)[:12],
        "disciplines": sorted({str(f.get("discipline") or "—") for f in items}),
    }


def category_chart(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Section 5's numbers, ready to chart. Worst first — the reader opens this
    to find the weak discipline, not to read A–Z.

    Reads `categoryScores`, i.e. the POINTS score (Σ obtained / Σ allotted) that
    `page_grading.compute_points_score` owns and that the report's own headline
    percentage uses.

    It used to read `disciplineRag`, which carries the engine's superseded
    pass-ratio ((passed + 0.5·partial) / assessed). Both blocks have always been
    written into every snapshot, so charting the wrong one put two different
    numbers for one discipline on one page — Production read 85.0% on the bar
    and 88.3% in the table directly beneath it. They are not a rounding
    artefact: the pass-ratio has no concept of a REPEAT finding, which scores
    -1 under the points model, so the two disagree in both directions.

    The points score is the authoritative one (`page_grading` module docstring;
    `audit_compliance._score_from_rollup`, "the two paths must agree exactly"),
    and it is what Page reconcile against their own workbook.
    """
    rows = snapshot.get("categoryScores") or []
    out = []
    for c in rows:
        passed = c.get("passed", 0) or 0
        partial = c.get("partial", 0) or 0
        failed = c.get("failed", 0) or 0
        allotted = c.get("score_allotted", 0) or 0
        assessed = passed + partial + failed
        # Nothing assessed, or nothing allotted → no score. A neutral bar, never
        # a red 0%: not-assessed is not failed-everything.
        pct = c.get("score_pct") if (assessed and allotted) else None
        band, _ = _band(pct)
        out.append({
            "categoryId": c.get("category_id"), "name": c.get("category_name") or "—",
            "pct": pct, "band": band,
            "total": c.get("total", 0), "passed": passed,
            "partial": partial, "failed": failed, "na": c.get("na", 0),
            "assessed": assessed,
            # The arithmetic behind the percentage, carried so every renderer
            # can show a number the reader is able to check by hand.
            "scoreObtained": c.get("score_obtained", 0) or 0,
            "scoreAllotted": allotted,
        })
    # Unassessed disciplines sort last: they are not the worst performers, they
    # are absent ones, and putting a neutral bar at the top of a "worst first"
    # chart misreads as a zero.
    out.sort(key=lambda r: (r["pct"] is None, r["pct"] if r["pct"] is not None else 0, r["name"]))
    return out


def repeat_callout(findings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Repeat non-conformances, pulled out of the category-grouped register.

    Keyed on `complianceStatus` ∈ REPEAT_STATUSES — the auditor's own Column F
    verdict, already carried on every finding as `isRepeat`. The spec proposed
    scanning observation text for "previously raised"; that would miss every
    repeat whose auditor phrased it differently and invent one from any finding
    that merely mentions a previous audit. The structured field is the record.
    """
    items = [f for f in findings if f.get("isRepeat")
             or pg.is_repeat(f.get("complianceStatus"))]
    if not items:
        return None
    disciplines = sorted({str(f.get("discipline") or "—") for f in items})
    return {
        "count": len(items),
        "headline": fill(
            "audit.report.repeat_nc", count=len(items),
            plural="" if len(items) == 1 else "s",
            disciplines=len(disciplines),
            dplural="" if len(disciplines) == 1 else "s",
        ),
        "evidence": fill("audit.report.repeat_nc.evidence", count=len(items),
                         refs=", ".join(_refs(items)[:8])),
        "disciplines": disciplines,
        "items": [
            {
                "checkpointCode": f.get("checkpointCode"),
                "discipline": f.get("discipline"),
                "severity": f.get("severity"),
                "question": f.get("question"),
                "observation": f.get("observation"),
                "statusLabel": pg.STATUS_LABEL.get(
                    str(f.get("complianceStatus") or ""), "Repeated"),
                "ownerId": f.get("ownerId"),
                "capaNumber": f.get("capaNumber"),
            }
            for f in items[:10]
        ],
        "truncated": max(0, len(items) - 10),
    }


def capa_strip(snapshot: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Section 7's three numbers, plus the per-CAPA chips they summarise.

    The totals are NOT recounted here — they are Section 7's own
    `capaSummary`, so the strip and the summary cannot disagree. Only the chip
    list is derived, from the findings that carry a linked CAPA.
    """
    cs = snapshot.get("capaSummary") or {}
    chips = [
        {
            "capaNumber": f.get("capaNumber"),
            "checkpointCode": f.get("checkpointCode"),
            "status": f.get("capaStatus") or "OPEN",
            "severity": f.get("severity"),
            "discipline": f.get("discipline"),
        }
        for f in findings if f.get("capaNumber")
    ]
    chips.sort(key=lambda c: (c["status"] in ("CLOSED", "VERIFIED", "CLOSED_RECURRED"),
                              str(c["capaNumber"])))
    return {
        "total": cs.get("total", 0), "open": cs.get("open", 0), "overdue": cs.get("overdue", 0),
        "chips": chips[:_MAX_CHIPS],
        "truncated": max(0, len(chips) - _MAX_CHIPS),
        # A CAPA can exist without appearing as a chip if its finding row was
        # not adverse; say so rather than letting the counts look wrong.
        "linkedShown": min(len(chips), _MAX_CHIPS),
    }


# ── Systemic patterns ────────────────────────────────────────────────────


def _owner_concentration(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One owner holding the majority of a severity tier is a capacity signal,
    not a performance verdict — phrased accordingly."""
    out = []
    for tier in _SEVERE:
        tier_items = [f for f in findings
                      if str(f.get("severity") or "").lower() == tier and f.get("ownerId")]
        if len(tier_items) < _CONCENTRATION_MIN:
            continue
        by_owner: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for f in tier_items:
            by_owner[str(f["ownerId"])].append(f)
        owner_id, held = max(by_owner.items(), key=lambda kv: len(kv[1]))
        share = len(held) / len(tier_items) * 100
        if len(held) < _CONCENTRATION_MIN or share < _CONCENTRATION_PCT:
            continue
        out.append(_pattern(
            f"owner.{tier}", "cluster",
            "high" if tier == "critical" else "watch",
            _confidence(len(held)),
            fill("audit.report.owner_concentration", owner="{owner}", count=len(held),
                 total=len(tier_items), tier=tier),
            fill("audit.report.owner_concentration.evidence", owner="{owner}",
                 count=len(held), tier=tier, share=round(share),
                 refs=", ".join(_refs(held)[:8])),
            _refs(held),
            fill("audit.report.owner_concentration.action", owner="{owner}", tier=tier),
        ) | {"ownerId": owner_id, "needsOwnerName": True})
    return out


def _discipline_concentration(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fails = [f for f in findings if f.get("assessmentStatus") == "FAIL"]
    if len(fails) < _CONCENTRATION_MIN:
        return []
    counts = Counter(str(f.get("discipline") or "—") for f in fails)
    disc, n = counts.most_common(1)[0]
    share = n / len(fails) * 100
    if n < _CONCENTRATION_MIN or share < _CONCENTRATION_PCT:
        return []
    held = [f for f in fails if str(f.get("discipline") or "—") == disc]
    return [_pattern(
        "discipline.concentration", "cluster", "high", _confidence(n),
        fill("audit.report.discipline_concentration", discipline=disc, count=n,
             total=len(fails), share=round(share)),
        fill("audit.report.discipline_concentration.evidence", discipline=disc,
             count=n, total=len(fails), refs=", ".join(_refs(held)[:8])),
        _refs(held),
        fill("audit.report.discipline_concentration.action", discipline=disc),
    )]


def _statutory_exposure(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Statutory/regulatory failures carry legal exposure an internal-requirement
    failure does not. Column I is checkpoint master data, so this partition is
    as reliable as the library itself."""
    stat = [f for f in findings
            if f.get("requirementType") == pg.REQ_STATUTORY
            and f.get("assessmentStatus") in _ADVERSE]
    if len(stat) < _CONCENTRATION_MIN:
        return []
    disciplines = sorted({str(f.get("discipline") or "—") for f in stat})
    return [_pattern(
        "statutory.exposure", "predictive_risk", "critical", _confidence(len(stat)),
        fill("audit.report.statutory", count=len(stat), plural="" if len(stat) == 1 else "s",
             disciplines=len(disciplines)),
        fill("audit.report.statutory.evidence", count=len(stat),
             dlist=", ".join(disciplines[:4]), refs=", ".join(_refs(stat)[:8])),
        _refs(stat),
        fill("audit.report.statutory.action"),
    )]


def _capa_coverage_gap(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A severe finding with no CAPA is the gap a certification body opens the
    report to find."""
    gap = [f for f in findings
           if str(f.get("severity") or "").lower() in _SEVERE
           and f.get("assessmentStatus") == "FAIL"
           and not f.get("capaNumber")]
    if len(gap) < _CAPA_GAP_MIN:
        return []
    return [_pattern(
        "capa.gap", "next_best_action", "high", _confidence(len(gap)),
        fill("audit.report.capa_gap", count=len(gap), plural="" if len(gap) == 1 else "s"),
        fill("audit.report.capa_gap.evidence", count=len(gap),
             refs=", ".join(_refs(gap)[:8])),
        _refs(gap),
        fill("audit.report.capa_gap.action"),
    )]


def _process_rigour(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Severity × workflow state — how hard the findings were pushed.

    Reported as an observation about the PROCESS, never as a verdict on it: a
    round-1 acceptance can be a well-evidenced fix or a rubber stamp, and this
    layer cannot tell which. It says which happened and lets the reader judge.
    """
    severe = [f for f in findings if str(f.get("severity") or "").lower() in _SEVERE]
    if len(severe) < _CONCENTRATION_MIN:
        return []
    out = []
    escalated = [f for f in severe if f.get("workflowState") == _WS_ESCALATED
                 or int(f.get("round") or 0) >= 2]
    single_round = [f for f in severe
                    if int(f.get("round") or 0) <= 1
                    and f.get("workflowState") in ("RESOLVED", "ACCEPTED_WITH_CAPA", "FINALIZED")]
    if len(single_round) >= _CONCENTRATION_MIN and \
            len(single_round) / len(severe) * 100 >= _CONCENTRATION_PCT:
        out.append(_pattern(
            "rigour.single_round", "anomaly", "watch", _confidence(len(single_round)),
            fill("audit.report.single_round", count=len(single_round), total=len(severe)),
            fill("audit.report.single_round.evidence", count=len(single_round),
                 total=len(severe), refs=", ".join(_refs(single_round)[:8])),
            _refs(single_round),
            fill("audit.report.single_round.action"),
        ))
    if len(escalated) >= _CONCENTRATION_MIN:
        out.append(_pattern(
            "rigour.escalated", "cluster", "watch", _confidence(len(escalated)),
            fill("audit.report.escalated", count=len(escalated), total=len(severe)),
            fill("audit.report.escalated.evidence", count=len(escalated),
                 refs=", ".join(_refs(escalated)[:8])),
            _refs(escalated),
            None,
        ))
    return out


def _wording_cluster(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings whose observation text is word-for-word identical.

    The weakest tier by design — see the module docstring. It reports the
    observation (identical wording across disciplines) and NOT a conclusion
    (a shared root cause), because freeform text cannot support the latter,
    and it carries `basis: "observation_text"` so a renderer can caveat it.
    """
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in findings:
        key = _norm(f.get("observation"))
        # A one-liner like "not met" is boilerplate, not a shared pattern.
        if len(key) >= 40:
            groups[key].append(f)

    qualifying = [
        (items, sorted({str(f.get("discipline") or "—") for f in items}))
        for items in groups.values()
    ]
    qualifying = [
        (items, disc) for items, disc in qualifying
        if len(items) >= _WORDING_MIN and len(disc) >= _WORDING_MIN_DISCIPLINES
    ]
    if not qualifying:
        return []

    # ONE card, for the largest group. Two wording groups of the same size
    # produce the same slot values and therefore the same headline — the real
    # AUD-PI-2026-NW-0021 emits two cards reading "4 findings across 3
    # disciplines carry identical observation wording", which a reader can only
    # read as a rendering fault. Distinguishing them would mean putting a text
    # excerpt in a 90-char headline, where two templates sharing an opening
    # clause still collide. Reporting the largest group and COUNTING the rest is
    # both unambiguous and honest, and it stops the weakest tier from taking two
    # of the five slots on page 1.
    qualifying.sort(key=lambda kv: (-len(kv[0]), _refs(kv[0])[:1]))
    items, disciplines = qualifying[0]
    others = len(qualifying) - 1
    # Only trail an ellipsis when the quotation was actually cut short — the
    # template used to append one unconditionally, so a complete sentence
    # quoted `"…briefed.…"`.
    _obs = (items[0].get("observation") or "").strip()
    excerpt = _obs[:120].rstrip()
    if len(_obs) > len(excerpt):
        excerpt += "…"
    evidence = fill(
        "audit.report.wording.evidence", count=len(items),
        dlist=", ".join(disciplines[:4]), refs=", ".join(_refs(items)[:8]),
        excerpt=excerpt,
    )
    if others:
        evidence += " " + fill(
            "audit.report.wording.more", groups=others,
            plural="" if others == 1 else "s",
            findings=sum(len(i) for i, _ in qualifying[1:]),
        )
    return [_pattern(
        "wording.1", "duplicate", "watch", _confidence(len(items)),
        fill("audit.report.wording", count=len(items), disciplines=len(disciplines)),
        evidence, _refs(items), None,
    ) | {"basis": "observation_text", "otherWordingGroups": others}]


# ── Entry point ──────────────────────────────────────────────────────────

_SEV_RANK = {"critical": 3, "high": 2, "watch": 1, "info": 0}
_CONF_RANK = {"high": 2, "medium": 1, "low": 0}


def compute_report_insights(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Build the Section-1 insight layer for an audit report snapshot.

    Pure: same snapshot in, same block out, no I/O and no clock read. That is
    what lets the result be hashed into the immutable report — an insight layer
    that re-queried at view time would break the one guarantee this document
    sells.
    """
    findings: list[dict[str, Any]] = list(snapshot.get("findings") or [])

    block: dict[str, Any] = {
        "version": 1,
        "gauge": gauge(snapshot),
        "criticalBanner": critical_banner(snapshot, findings),
        "categoryChart": category_chart(snapshot),
        "capaStrip": capa_strip(snapshot, findings),
        "repeats": repeat_callout(findings),
        "patterns": [],
        "suppressed": False,
        "reason": None,
    }

    # Below the floor the DIAL, BANNER, CHART and CAPA STRIP still render —
    # they are re-presentations of counted facts, true at any n. Only the
    # inferred patterns are suppressed, because inference over three findings
    # is not inference. The two are separated deliberately: suppressing the
    # gauge on a small audit would leave page 1 blank on a perfectly valid
    # report.
    if len(findings) < MIN_FINDINGS:
        block["suppressed"] = True
        block["reason"] = "insufficient_findings"
        block["patternNote"] = fill("audit.report.suppressed", count=len(findings),
                                    floor=MIN_FINDINGS)
        return block

    patterns: list[dict[str, Any]] = []
    patterns += _statutory_exposure(findings)
    patterns += _discipline_concentration(findings)
    patterns += _owner_concentration(findings)
    patterns += _capa_coverage_gap(findings)
    patterns += _process_rigour(findings)
    patterns += _wording_cluster(findings)

    patterns.sort(
        key=lambda p: (_SEV_RANK.get(p["severity"], 0), _CONF_RANK.get(p["confidence"], 0)),
        reverse=True,
    )
    block["patterns"] = patterns[:_MAX_PATTERNS]
    block["patternsSuppressedCount"] = max(0, len(patterns) - _MAX_PATTERNS)
    return block


def resolve_owner_names(block: dict[str, Any], user_names: dict[str, str]) -> None:
    """Substitute `{owner}` in the owner-concentration patterns, in place.

    Split out of `compute_report_insights` for the same reason
    `distributionList` resolves late in `generate_report`: names need the DB,
    and this module must stay pure. An id that will not resolve degrades to a
    neutral noun rather than printing a cuid at a reader.
    """
    for p in block.get("patterns") or []:
        if not p.pop("needsOwnerName", False):
            continue
        name = (user_names or {}).get(str(p.get("ownerId") or "")) or "One owner"
        for key in ("headline", "evidence", "suggestedAction"):
            if isinstance(p.get(key), str):
                p[key] = p[key].replace("{owner}", name)
