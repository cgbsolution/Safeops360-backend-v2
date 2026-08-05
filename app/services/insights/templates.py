"""Headline / evidence / action phrasing — template + slot-filling only.

There is no model to turn computed findings into natural language at runtime
(airgap constraint, spec §1.1), so every phrase an insight shows is authored
here once as a parameterised template, and the deterministic rule layer fills
the slots from evidence it already computed. This is the job phrasing would
have done, moved to author-time.

Contract the rules rely on:
  * `fill(key, **slots)` returns the phrased string. Every `{slot}` in a
    template MUST be supplied — a missing slot raises, so a rule can never ship
    a half-filled headline. No numeric value is ever hardcoded in a template;
    every number arrives as a slot traced to a real field (acceptance §8).
  * Headlines are kept ≤ 90 chars at author time; `fill` hard-trims as a
    backstop so the `Insight.headline` max_length can never be violated.

If a tenant is ever confirmed to have a local model, this module is the single
swap point — the `Insight` contract above it does not change.
"""

from __future__ import annotations

_HEADLINE_MAX = 90

# key → template. Grouped by the concrete finding each rule emits. Slot names
# are the evidence fields the rule computes.
TEMPLATES: dict[str, str] = {
    # ── Incident ──────────────────────────────────────────────────────────
    "incident.cluster.rootcause": "{count} of {total} open incidents share a cause pattern: {keyword} — {plant}",
    "incident.cluster.rootcause.evidence": "{count} open incidents at {plant} share the term '{keyword}': {refs}",
    "incident.overdue.stalled": "{count} investigations stalled >{days}d — oldest {oldest_ref} at {oldest_days}d",
    "incident.overdue.stalled.evidence": "{count} incidents past {days}d with no update. Oldest: {refs}",
    "incident.overdue.stalled.action": "Reassign or escalate {oldest_ref} — it has sat {oldest_days} days without movement.",
    # ── Near Miss ─────────────────────────────────────────────────────────
    "nearmiss.critical.uninvestigated": "{stale} of {crit} critical near misses uninvestigated >7d — prioritise",
    "nearmiss.critical.uninvestigated.evidence": "{crit} critical-potential near misses logged; {stale} unreviewed >7d: {refs}",
    "nearmiss.critical.uninvestigated.action": "Review the uninvestigated critical near misses first — these precede LTIs.",
    "nearmiss.ratio.nm_lti": "Near-miss to LTI ratio is {ratio}:1 over 12 months ({nm} NMs, {lti} LTIs)",
    "nearmiss.ratio.nm_lti.evidence": "{nm} near misses to {lti} lost-time incidents in the trailing 12 months.",
    # ── Row signals (labels are 1-3 words; evidence is the popover body) ───
    "signal.no_capa.label": "No CAPA linked",
    "signal.no_capa.evidence": "Open investigation ({ref}) with 0 linked CAPAs — a common audit gap.",
    "signal.rca_overdue.label": "RCA overdue",
    "signal.rca_overdue.evidence": "{ref} has sat in investigation {days}d — RCA + CAPA definition is overdue.",
    "signal.rca_overdue.action": "Nudge the assigned investigator to complete the RCA.",
    "signal.prioritize.label": "Prioritize",
    "signal.prioritize.evidence": "{ref} is critical-potential and not yet reviewed — prioritise it.",
    # ── Safety Observations (§2.2) ────────────────────────────────────────
    "observation.cluster.category": "{count} of {total} open unsafe observations at {plant} are {category}",
    "observation.cluster.category.evidence": "{count} open unsafe {category} observations at {plant}: {refs}",
    "observation.duplicate": "{groups} sets of near-identical observations logged ({records} records)",
    "observation.duplicate.evidence": "{records} observations look like duplicates (same place, <48h, near-identical text): {refs}",
    "observation.bottleneck": "{step} is the bottleneck — avg {avg}d, {count} stuck here",
    "observation.bottleneck.evidence": "{count} open observations sit in '{step}', dwelling {avg}d on average — the slowest step: {refs}",
    "signal.duplicate.label": "Likely duplicate",
    "signal.duplicate.evidence": "{ref} closely matches another observation in the same area within 48h.",
    "signal.severity_mismatch.label": "Check severity",
    "signal.severity_mismatch.evidence": "{ref} is marked {severity} but the description is too thin to justify it.",
    "signal.escalate.label": "Escalate",
    "signal.escalate.evidence": "{ref} has sat open {days}d past the review SLA.",
    # Row-Level Insight Layer row signals — repeat-location + stale-step.
    "signal.repeat_location.label": "Repeat ×{count} · 90d",
    "signal.repeat_location.evidence": "{count} {category} observations here in the same area within 90 days — a recurring hazard at this spot.",
    "signal.stale_step.label": "{days}d in step",
    "signal.stale_step.evidence": "{ref} has sat in {step} {days}d vs a {avg}d average for this category — well past the usual dwell.",
    # ── HIRA Studies (§2.4) ───────────────────────────────────────────────
    "hira.review.soon": "{count} HIRA studies due for review — soonest {soonest_ref} in {days}d",
    "hira.review.overdue": "{count} HIRA studies overdue for review — {soonest_ref} {days}d overdue",
    "hira.review.evidence": "{count} in-force studies at or past their next review date: {refs}",
    "hira.cluster.hazard": "{category} hazard live in HIRA studies across {plants} plants",
    "hira.cluster.hazard.evidence": "'{category}' appears in current studies at {plant_list}: {refs}",
    "signal.unmitigated_critical.label": "Unmitigated critical",
    "signal.unmitigated_critical.evidence": "{ref} holds a current entry still at CRITICAL residual risk.",
    "signal.nudge_lead.label": "Nudge team lead",
    "signal.nudge_lead.evidence": "{ref} is a draft study with no activity for {days}d.",
    # ── EAI — Environmental Register (§2.5) ───────────────────────────────
    "eai.obligation.due": "{obligations} compliance obligations due ≤30d across {studies} studies",
    "eai.obligation.due.evidence": "{obligations} monitoring obligations at/near their next-monitoring date: {refs}",
    "eai.significance.count": "{studies} studies hold significant environmental aspects",
    "eai.significance.count.evidence": "{studies} studies carry a significant residual environmental aspect: {refs}",
    "signal.monitoring_overdue.label": "Monitoring overdue",
    "signal.monitoring_overdue.evidence": "{ref} has a compliance obligation past its next-monitoring date.",
    "signal.significant_aspect.label": "Significant aspect",
    "signal.significant_aspect.evidence": "{ref} holds a significant residual environmental aspect.",
    # ── Combined Risk Register (§2.6) ─────────────────────────────────────
    "combined.reduced_no_capa": "{count} critical-initial risks reduced residual with no linked CAPA",
    "combined.reduced_no_capa.evidence": "{count} entries were CRITICAL initially, now lower residual, with no CAPA linked: {refs}",
    "combined.area_cluster": "{count} HIRA & EAI risks share area {area}",
    "combined.area_cluster.evidence": "Area '{area}' carries both HIRA and EAI risks: {refs}",
    "combined.not_active_tracked": "{count} critical-initial risks not yet under active tracking",
    "combined.not_active_tracked.evidence": "{count} CRITICAL-initial register entries still in DRAFT/APPROVED, not ACTIVE: {refs}",
    "signal.not_active.label": "Not active-tracked",
    "signal.not_active.evidence": "{ref} is a critical-initial risk still in {status} — not yet active-tracked.",
    # ── CAPA Management (§2.7) ────────────────────────────────────────────
    "capa.overdue": "{count} CAPAs overdue — worst {worst_ref} at {worst_days}d ({severity})",
    "capa.overdue.evidence": "{count} open CAPAs past their closure target: {refs}",
    "capa.near_breach": "{count} serious CAPAs near breach — {worst_ref} due in {days}d, not started",
    "capa.near_breach.evidence": "{count} CRITICAL/HIGH CAPAs approach their closure target while still in Actions Planned: {refs}",
    "capa.backlog": "{closed} CAPAs closed vs {opened} opened this month — backlog growing",
    "capa.backlog.evidence": "{opened} CAPAs opened and {closed} closed so far this month.",
    "capa.bottleneck": "{owner} holds {count} overdue CAPAs — a bottleneck",
    "capa.bottleneck.evidence": "{owner} owns {count} of the overdue CAPAs: {refs}",
    "signal.audit_finding.label": "Likely audit finding",
    "signal.audit_finding.evidence": "{ref} is {severity} and has sat in Actions Planned {days}d without starting.",
    # ── Management of Change (§2.8) ───────────────────────────────────────
    "moc.overdue": "{count} MOCs overdue — worst {worst_ref} {worst_days}d past target",
    "moc.overdue.evidence": "{count} active change requests past their target completion date: {refs}",
    "moc.cluster.critical": "{count} active changes touch an unmitigated critical risk",
    "moc.cluster.critical.evidence": "{count} changes overlap a critical HIRA/EAI risk (e.g. {risk}): {refs}",
    "signal.stalled_draft.label": "Stalled in draft",
    "signal.stalled_draft.evidence": "{ref} is a {cls} change stuck in draft for {days}d.",
    # ── Training & Competency Engine cross-link (spec §B/§D) ──────────────
    "training.followup.open": "{workers} workers have open training from these events — top gap: {competency}",
    "training.followup.open.evidence": "{count} open training assignments were auto-created from {module} records here; {competency} is the most-assigned competency.",
    "training.followup.overdue": "{overdue} auto-assigned trainings from these events are now overdue",
    "training.followup.overdue.evidence": "{overdue} of {count} event-driven training assignments are past due — competency gaps stay open until completed.",
}


def fill(key: str, **slots: object) -> str:
    """Phrase a template by key. Raises KeyError for an unknown template and
    KeyError/IndexError via str.format for a missing slot — both are bugs we
    want to fail loudly in tests, never ship a half-filled string."""
    template = TEMPLATES[key]
    text = template.format(**slots)
    if len(text) > _HEADLINE_MAX and key.count(".") <= 2 and "evidence" not in key and "action" not in key:
        # Headline backstop only — evidence/action lines may run longer.
        text = text[: _HEADLINE_MAX - 1].rstrip() + "…"
    return text
