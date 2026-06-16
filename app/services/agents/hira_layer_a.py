"""HIRA Assistant — Layer A (deterministic rules).

Per HIRA Phase 2 spec §2.3, the agent runs in two layers:
  • Layer A — deterministic preconditions (this file). 10 rules
    (HA-01..HA-10) that ALWAYS fire when their trigger condition is
    met. The output is the "floor" — these hazard suggestions / context
    notes are guaranteed to surface regardless of what the LLM does.
  • Layer B — LLM analysis on top of Layer A findings.

The runtime calls evaluate_layer_a() with the activity/equipment/
materials/energy context, gets a structured findings dict, and includes
it in the prompt for the LLM. The LLM is instructed to treat these as
mandatory inclusions and add its own multi-signal hazards on top.

Rules:
  HA-01  Confined-space terms in activity → confined space hazards
  HA-02  Lifting equipment → fall and crush hazards
  HA-03  Hazardous chemicals in materials → chemical hazards
  HA-04  Electrical energy source → electrical hazards
  HA-05  Non-routine / emergency activity → flag for extra control rigor
  HA-06  Location with ≥3 critical incidents in past 12m → historical pattern
  HA-07  Routine + high frequency → likelihood baseline ≥ "Likely"
  HA-08  Contractors among persons exposed → contractor competency controls
  HA-09  Work at height ≥2m terms → mandatory work-at-height hazards
  HA-10  Hot work terms (welding/cutting/grinding) → mandatory fire hazards
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.hira import HiraEntry, HiraStudy
from app.models.incident import Incident


# ─── Trigger vocabularies ────────────────────────────────────────────


_CONFINED_SPACE_TERMS = {
    "confined space", "tank entry", "vessel entry", "manhole", "silo entry",
    "kiln entry", "shaft", "pit entry", "underground", "duct entry",
}

_LIFTING_EQUIPMENT_TERMS = {
    "crane", "hoist", "lifting", "tackle", "winch", "chain block", "gantry",
    "overhead crane", "tower crane", "mobile crane", "rigging",
}

_HOT_WORK_TERMS = {
    "welding", "cutting", "grinding", "burning", "torch", "brazing",
    "soldering", "plasma cut", "hot tapping", "thermal",
}

_HEIGHT_TERMS = {
    "scaffold", "ladder", "platform", "roof", "elevated", "height", "tower",
    "structural", "rooftop", "edge work", "fall arrest", "harness",
}

_HAZARDOUS_CHEMICAL_KEYWORDS = {
    "acid", "alkali", "caustic", "ammonia", "chlorine", "sulphuric",
    "sulfuric", "hydrochloric", "nitric", "phosphoric", "solvent",
    "benzene", "toluene", "xylene", "naphtha", "methanol", "ethanol",
    "isocyanate", "phenol", "fluorine", "fuel oil", "lpg", "lng",
    "hydrogen", "h2", "h2s",
}

_ELECTRICAL_ENERGY_CODES = {
    "ELECTRICAL", "ELECTRIC", "ELECTRICITY", "HIGH_VOLTAGE", "LV", "MV", "HV",
}


# ─── Output dataclasses ──────────────────────────────────────────────


@dataclass
class LayerAFinding:
    """A single rule firing. Surfaces in the LLM context with a 'rule
    fired — this hazard is mandatory' framing."""

    rule_code: str
    rule_summary: str
    mandatory_hazard_categories: list[str] = field(default_factory=list)
    mandatory_control_themes: list[str] = field(default_factory=list)
    baseline_likelihood_min: int | None = None
    reviewer_note: str | None = None


@dataclass
class LayerAResult:
    findings: list[LayerAFinding] = field(default_factory=list)
    historical_incidents: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""

    def to_prompt_dict(self) -> dict[str, Any]:
        """Serialise for inclusion in the LLM input context."""
        return {
            "layerAFindings": [
                {
                    "ruleCode": f.rule_code,
                    "ruleSummary": f.rule_summary,
                    "mandatoryHazardCategories": f.mandatory_hazard_categories,
                    "mandatoryControlThemes": f.mandatory_control_themes,
                    "baselineLikelihoodMin": f.baseline_likelihood_min,
                    "reviewerNote": f.reviewer_note,
                }
                for f in self.findings
            ],
            "historicalIncidentsAtLocation": self.historical_incidents,
            "layerASummary": self.summary,
        }


# ─── Helpers ─────────────────────────────────────────────────────────


def _text_contains_any(text: str, terms: set[str]) -> list[str]:
    """Return the subset of `terms` that appear (case-insensitively) in
    `text`. Returns empty list if no match."""
    lower = (text or "").lower()
    return [t for t in terms if t in lower]


def _list_contains_any(items: list[str] | None, terms: set[str]) -> list[str]:
    """Return the subset of `terms` that appear (case-insensitively) in
    any string of `items`. Used for material codes, energy codes."""
    if not items:
        return []
    matches: set[str] = set()
    for it in items:
        lower = (it or "").lower()
        for t in terms:
            if t.lower() in lower:
                matches.add(t)
    return list(matches)


# ─── Public API ──────────────────────────────────────────────────────


async def evaluate_layer_a(
    db: AsyncSession,
    *,
    activity_description: str,
    routine: str,
    frequency: str,
    equipment_used: list[str] | None,
    materials_used: list[str] | None,
    energy_sources: list[str] | None,
    persons_employees: int = 0,
    persons_contractors: int = 0,
    persons_visitors: int = 0,
    persons_public: int = 0,
    plant_id: str | None = None,
    area_id: str | None = None,
) -> LayerAResult:
    """Run the 10 deterministic HIRA Assistant rules over the given
    activity context. Returns the structured findings dict for inclusion
    in the LLM prompt.

    Pure I/O for HA-06 (historical incidents lookup); all other rules
    are pure functions of the input context.
    """
    result = LayerAResult()

    activity_lower = (activity_description or "").lower()

    # HA-01: confined-space terms
    cs_matches = _text_contains_any(activity_lower, _CONFINED_SPACE_TERMS)
    if cs_matches:
        result.findings.append(
            LayerAFinding(
                rule_code="HA-01",
                rule_summary=f"Activity mentions confined-space terms: {', '.join(cs_matches)}",
                mandatory_hazard_categories=["confined_space", "atmospheric", "rescue"],
                mandatory_control_themes=[
                    "gas testing before entry",
                    "permit-to-work / confined space entry permit",
                    "rescue plan + standby person",
                    "ventilation",
                ],
                reviewer_note="Confined space entry requires a confined-space permit and gas test before each entry.",
            )
        )

    # HA-02: lifting equipment
    lift_matches = _list_contains_any(equipment_used, _LIFTING_EQUIPMENT_TERMS) or _text_contains_any(
        activity_lower, _LIFTING_EQUIPMENT_TERMS
    )
    if lift_matches:
        result.findings.append(
            LayerAFinding(
                rule_code="HA-02",
                rule_summary=f"Lifting equipment in use: {', '.join(lift_matches)}",
                mandatory_hazard_categories=["mechanical", "height", "struck_by"],
                mandatory_control_themes=[
                    "lifting plan / rigging study",
                    "competent rigger and signal-man",
                    "barricade exclusion zone",
                    "load-test certification within validity",
                ],
            )
        )

    # HA-03: hazardous chemical materials
    chem_matches = _list_contains_any(materials_used, _HAZARDOUS_CHEMICAL_KEYWORDS)
    if chem_matches:
        result.findings.append(
            LayerAFinding(
                rule_code="HA-03",
                rule_summary=f"Hazardous chemicals present: {', '.join(chem_matches)}",
                mandatory_hazard_categories=["chemical", "fire_explosion", "biological"],
                mandatory_control_themes=[
                    "MSDS available at point of use",
                    "compatible PPE (gloves, goggles, respirator as appropriate)",
                    "spill kit + bund",
                    "emergency shower / eyewash within 10s reach",
                ],
            )
        )

    # HA-04: electrical energy source
    elec_matches = _list_contains_any(energy_sources, _ELECTRICAL_ENERGY_CODES)
    if elec_matches:
        result.findings.append(
            LayerAFinding(
                rule_code="HA-04",
                rule_summary=f"Electrical energy source: {', '.join(elec_matches)}",
                mandatory_hazard_categories=["electrical", "arc_flash"],
                mandatory_control_themes=[
                    "LOTO with proven zero-energy verification",
                    "qualified electrical person",
                    "arc-flash PPE per incident-energy analysis",
                ],
            )
        )

    # HA-05: non-routine / emergency
    if routine in ("NON_ROUTINE", "EMERGENCY"):
        result.findings.append(
            LayerAFinding(
                rule_code="HA-05",
                rule_summary=f"Activity routine type is {routine}; non-routine/emergency operations need extra control rigor",
                mandatory_control_themes=[
                    "pre-job toolbox talk specific to this scenario",
                    "supervisor on-site for duration",
                    "stop-work authority emphasised",
                ],
                reviewer_note="Default routine controls are insufficient — review whether a written safe-work procedure exists.",
            )
        )

    # HA-06: historical incidents at the area in past 12m
    if plant_id and area_id:
        cutoff = datetime.now(timezone.utc) - timedelta(days=365)
        try:
            rows = (
                await db.execute(
                    select(Incident.id, Incident.severity, Incident.date)
                    .where(Incident.plantId == plant_id)
                    .where(Incident.areaId == area_id)
                    .where(Incident.severity.in_(["HIGH", "CRITICAL"]))
                    .where(Incident.date >= cutoff)
                    .limit(10)
                )
            ).all()
        except Exception:
            rows = []
        if len(rows) >= 3:
            result.findings.append(
                LayerAFinding(
                    rule_code="HA-06",
                    rule_summary=f"{len(rows)} HIGH/CRITICAL incidents in this area in last 12 months",
                    mandatory_control_themes=["site-history review with incident learnings"],
                    reviewer_note="Repeat-incident area — controls that previously failed must be inspected before this activity.",
                )
            )
            result.historical_incidents = [
                {
                    "incidentId": str(r[0]),
                    "severity": r[1],
                    "occurredAt": r[2].isoformat() if r[2] else None,
                }
                for r in rows
            ]

    # HA-07: routine + high frequency → likelihood floor
    if routine == "ROUTINE" and frequency in ("CONTINUOUS", "DAILY"):
        result.findings.append(
            LayerAFinding(
                rule_code="HA-07",
                rule_summary=f"Routine activity at high frequency ({frequency}) — likelihood baseline raised",
                baseline_likelihood_min=4,  # "Likely" on a 5-scale
                reviewer_note="Initial likelihood should not be scored below 'Likely' (4/5) for routine high-frequency exposure.",
            )
        )

    # HA-08: contractors among persons exposed
    if persons_contractors > 0:
        result.findings.append(
            LayerAFinding(
                rule_code="HA-08",
                rule_summary=f"{persons_contractors} contractor(s) among persons exposed",
                mandatory_control_themes=[
                    "contractor induction and SWP brief specific to this activity",
                    "supervisor verification of contractor competency certificates",
                    "communication plan in contractors' working language",
                ],
            )
        )

    # HA-09: work at height
    h_matches = _text_contains_any(activity_lower, _HEIGHT_TERMS)
    if h_matches:
        result.findings.append(
            LayerAFinding(
                rule_code="HA-09",
                rule_summary=f"Work-at-height indicators: {', '.join(h_matches)}",
                mandatory_hazard_categories=["height", "struck_by"],
                mandatory_control_themes=[
                    "fall-arrest harness anchored above shoulder height",
                    "edge protection / guardrails",
                    "rescue plan for suspended worker",
                    "exclusion zone below",
                ],
            )
        )

    # HA-10: hot work
    hot_matches = _text_contains_any(activity_lower, _HOT_WORK_TERMS)
    if hot_matches:
        result.findings.append(
            LayerAFinding(
                rule_code="HA-10",
                rule_summary=f"Hot work indicators: {', '.join(hot_matches)}",
                mandatory_hazard_categories=["fire_explosion", "thermal", "respiratory"],
                mandatory_control_themes=[
                    "hot-work permit",
                    "fire watch with extinguisher",
                    "30-min post-work fire watch",
                    "combustible removal / fire blanket within 11m",
                    "gas-test for flammable atmosphere if applicable",
                ],
            )
        )

    result.summary = (
        f"{len(result.findings)} Layer A rule(s) fired. "
        "These hazard categories and control themes are floor-mandatory; "
        "the LLM may add more but cannot drop these."
    )
    return result
