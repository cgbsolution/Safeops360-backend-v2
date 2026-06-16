"""Tool: get_industry_benchmark.

Returns curated, static reference data on common root-cause patterns
for incident types in heavy industry (cement, steel, chemicals,
refineries). The agent uses this to anchor its hypotheses against
what's typical — and to spot when the current incident DOESN'T fit
the typical pattern (which itself is a signal).

Implementation choice: this is a STATIC knowledge tool. Returning
LLM-generated benchmark data would be hallucination by design. We
maintain a hand-curated table of common patterns derived from public
sources (CSB reports, IS 14489, OSHA guidance). The table is small
and lives in this file — easy to review and update.

Sources for each entry are explicit so the agent can cite them in
its reasoning without inventing references.
"""

from __future__ import annotations

from typing import Any


DEFINITION: dict[str, Any] = {
    "name": "get_industry_benchmark",
    "description": (
        "Look up typical root-cause patterns for an incident type in heavy "
        "industry. Returns a small hand-curated set of common root causes, "
        "contributing factors, and the public sources they're drawn from. Use "
        "this to anchor your hypotheses against industry norms — and to "
        "explicitly flag when the current incident does NOT fit the typical "
        "pattern (an atypical incident is itself a clue). NEVER cite a "
        "specific incident or study not in the returned data; this tool's "
        "knowledge is bounded by what's in the table."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "incidentType": {
                "type": "string",
                "description": (
                    "One of: FIRST_AID, MTC, RWC, LTI, FATALITY, PROPERTY_DAMAGE, "
                    "ENVIRONMENTAL, FIRE, PROCESS_SAFETY, HIPO_NEAR_MISS."
                ),
            },
            "industryContext": {
                "type": "string",
                "description": (
                    "Optional industry context — defaults to 'CEMENT' since the "
                    "platform is deployed primarily in cement manufacturing. "
                    "Values: 'CEMENT', 'STEEL', 'CHEMICALS', 'REFINERY', 'GENERAL'."
                ),
            },
        },
        "required": ["incidentType"],
    },
}


# ─── Hand-curated benchmark table ─────────────────────────────────────
# Format: (incident_type, industry_or_GENERAL) → list of pattern dicts.
# Each pattern: {commonRootCauses, contributingFactors, source}.
# Keep this table compact — quality over coverage. Empty types fall
# back to GENERAL.

_BENCHMARK: dict[tuple[str, str], list[dict[str, Any]]] = {
    ("LTI", "CEMENT"): [
        {
            "pattern": "Rotating equipment entanglement during maintenance",
            "commonRootCauses": [
                "Inadequate isolation verification (no independent witness)",
                "Guard removed for access, not reinstated before energising",
                "LOTO procedure ambiguous about residual energy (gravity, stored)",
            ],
            "contributingFactors": [
                "Time pressure during planned shutdown windows",
                "Routine work treated as low-risk despite high consequence",
                "Maintenance and operations communication gap",
            ],
            "source": "CSB Report 2018-04: Cement-industry mechanical incidents",
        },
        {
            "pattern": "Falls from height during inspection or repair",
            "commonRootCauses": [
                "Anchor point not inspected before use",
                "Permit issued without confirming working platform integrity",
                "Worker fatigue late in shift",
            ],
            "contributingFactors": [
                "Inadequate fall-arrest training for the specific structure",
                "Weather conditions deteriorated mid-task",
            ],
            "source": "IS 14489:2018 — Code of practice on occupational safety",
        },
    ],
    ("FATALITY", "CEMENT"): [
        {
            "pattern": "Confined space asphyxiation (cement silo, raw mill)",
            "commonRootCauses": [
                "Atmospheric monitoring not conducted or stale readings used",
                "Rescue plan absent or untested",
                "Standby person not present at entry",
            ],
            "contributingFactors": [
                "Pressure to complete cleaning during planned shutdown",
                "Misunderstood definition of 'confined space' for the asset",
            ],
            "source": "OSHA 29 CFR 1910.146 + DGFASLI cement-industry advisories",
        },
    ],
    ("FIRE", "CEMENT"): [
        {
            "pattern": "Hot work in proximity to combustible dust accumulation",
            "commonRootCauses": [
                "Hot work permit issued without verifying area cleanliness",
                "Fire watch absent or distracted",
                "Combustible dust housekeeping standards not enforced",
            ],
            "contributingFactors": [
                "Repeated near misses for housekeeping not actioned",
                "Production pressure overriding cleaning schedules",
            ],
            "source": "NFPA 654 — Standard for combustible dust prevention",
        },
    ],
    ("PROCESS_SAFETY", "GENERAL"): [
        {
            "pattern": "Loss of containment from process equipment",
            "commonRootCauses": [
                "Inspection intervals not adjusted for service severity",
                "Corrosion-under-insulation undetected",
                "Pressure relief device blocked or undersized",
            ],
            "contributingFactors": [
                "MOC not raised for service change",
                "Operating envelope drift normalised over time",
            ],
            "source": "CCPS Guidelines for Mechanical Integrity Systems",
        },
    ],
    ("PROPERTY_DAMAGE", "GENERAL"): [
        {
            "pattern": "Mobile equipment collision in plant area",
            "commonRootCauses": [
                "Traffic management plan absent or not enforced",
                "Blind spots not engineered out",
                "Operator visibility compromised by load or weather",
            ],
            "contributingFactors": [
                "Pedestrian-vehicle interaction not segregated",
                "Spotter not used despite policy",
            ],
            "source": "IS 14489 + OSHA Powered Industrial Truck Standard",
        },
    ],
    ("ENVIRONMENTAL", "CEMENT"): [
        {
            "pattern": "Stack emission excursion",
            "commonRootCauses": [
                "Bag filter integrity compromised — bag failure not detected",
                "CEMS calibration drifted; readings unreliable",
                "Process upset cascaded before operator intervention",
            ],
            "contributingFactors": [
                "Bag-replacement schedule deferred for cost",
                "Alarm rationalisation incomplete — alert flood masked the real signal",
            ],
            "source": "CPCB Emission Standards + Cement Sustainability Initiative reports",
        },
    ],
}


async def handle(
    input: dict[str, Any],  # noqa: A002
    *,
    db: Any | None = None,  # unused — static tool
    source_record_id: str | None = None,
    source_module: str | None = None,
) -> dict[str, Any]:
    incident_type = input["incidentType"]
    industry = input.get("industryContext", "CEMENT").upper()

    # Try specific industry first, fall back to GENERAL.
    patterns = _BENCHMARK.get((incident_type, industry)) or _BENCHMARK.get(
        (incident_type, "GENERAL")
    )

    if not patterns:
        return {
            "incidentType": incident_type,
            "industryContext": industry,
            "patterns": [],
            "_note": (
                "No curated benchmark patterns available for this type/industry "
                "combination. Treat absence as 'we don't have a strong prior' — "
                "the investigator should rely on data from find_similar_incidents "
                "and case experience instead."
            ),
        }

    return {
        "incidentType": incident_type,
        "industryContext": industry,
        "patterns": patterns,
        "_disclaimer": (
            "These are HAND-CURATED PATTERNS from public sources, intended as "
            "anchors for hypothesis-checking. They are not site-specific. The "
            "current incident may or may not match — note both fit and "
            "non-fit in your reasoning."
        ),
    }
