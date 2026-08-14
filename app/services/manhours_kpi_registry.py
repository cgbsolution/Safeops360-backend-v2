"""Manhours KPI registry — the single place safety formulas are defined.

Port of `lib/manhours/kpi-registry.ts`. Deliberately data, not code: the engine
reads these definitions and knows nothing about any individual KPI, and nothing
outside this file may restate a formula. An auditor asking "how did you compute
March's LTIFR" gets one answer, from one place.

Two things here are statutory rather than stylistic:
  * every rate divides by NET exposure hours (IS 3786), not gross;
  * a fatality is charged at 6,000 lost days in the severity rate.

`REGISTRY_VERSION` is stamped into every snapshot. Bump it whenever a formula,
multiplier or benchmark changes, so a historical KPI can always be traced to the
formula generation that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

REGISTRY_VERSION = "1.1.0"

KPI_CODES = (
    "LTIFR",
    "TRIFR",
    "TRIR",
    "IFR",
    "DART_RATE",
    "SEVERITY_RATE",
    "FSI",
    "NEAR_MISS_RATE",
    "OBSERVATION_RATE",
    "HEINRICH_RATIO",
    "CAPA_CLOSURE_RATE",
    "TRAINING_COMPLIANCE",
    "INSPECTION_COMPLIANCE",
    "PTW_FLRA_COMPLIANCE",
    "DAYS_SINCE_LAST_LTI",
    "COST_OF_INCIDENTS",
)

# Incident type groupings, named once. FIRST_AID is excluded from the
# "recordable" set per OSHA 29 CFR 1904 recordability rules.
LTI_TYPES = ("LTI", "FATALITY")
RECORDABLE_TYPES = ("MTC", "RWC", "LTI", "FATALITY")
ALL_INJURY_TYPES = ("FIRST_AID", "MTC", "RWC", "LTI", "FATALITY")
DART_TYPES = ("RWC", "LTI", "FATALITY")

# IS 3786 charges each fatality at 6,000 days for the severity rate.
FATALITY_DAY_CHARGE = 6000


@dataclass(frozen=True)
class Benchmarks:
    worldClass: float
    excellent: float
    average: float
    poor: float


@dataclass(frozen=True)
class Numerator:
    """How the top half of a KPI is obtained.

    kind:
      MODULE_COUNT  — count rows of `source` matching `types` in the period
      MODULE_SUM    — sum `sum_field` over those rows
      DAYS_SINCE    — days since the most recent matching row
      CUSTOM        — computed by the engine, keyed by `tag`
      DERIVED       — composed from other KPIs, keyed by `tag`
    """

    kind: Literal["MODULE_COUNT", "MODULE_SUM", "DAYS_SINCE", "CUSTOM", "DERIVED"]
    source: str | None = None
    types: tuple[str, ...] | None = None
    sum_field: str | None = None
    tag: str | None = None
    source_kpis: tuple[str, ...] = ()


@dataclass(frozen=True)
class KpiDefinition:
    code: str
    name: str
    formula: str
    numerator: Numerator
    # EXPOSURE_HOURS divides by netExposureHours; NONE means the numerator
    # already IS the value (percentages, counts, streaks).
    denominator: Literal["EXPOSURE_HOURS", "NONE"]
    multiplier: float
    higher_is_better: bool
    display_format: Literal["decimal_2_places", "integer", "currency_indian", "percent"]
    statutory_reference: str | None = None
    exclusion_rules: tuple[str, ...] = ()
    benchmarks: Benchmarks | None = None
    target_value: float | None = None
    is_percentage: bool = False
    is_streak_metric: bool = False


KPI_REGISTRY: dict[str, KpiDefinition] = {
    "LTIFR": KpiDefinition(
        code="LTIFR",
        name="Lost Time Injury Frequency Rate",
        formula="(LTIs × 1,000,000) ÷ Net Exposure Hours",
        statutory_reference="IS 3786:1983",
        numerator=Numerator("MODULE_COUNT", source="incident", types=LTI_TYPES),
        denominator="EXPOSURE_HOURS",
        multiplier=1_000_000,
        exclusion_rules=(
            "Commuting incidents excluded",
            "Off-site incidents not on company business excluded",
            "Pre-existing conditions unrelated to work excluded",
        ),
        benchmarks=Benchmarks(1.0, 2.0, 5.0, 10.0),
        higher_is_better=False,
        display_format="decimal_2_places",
    ),
    "TRIFR": KpiDefinition(
        code="TRIFR",
        name="Total Recordable Injury Frequency Rate",
        formula="(Recordable Injuries × 1,000,000) ÷ Net Exposure Hours",
        statutory_reference="OSHA 29 CFR 1904 (per-million variant)",
        numerator=Numerator("MODULE_COUNT", source="incident", types=RECORDABLE_TYPES),
        denominator="EXPOSURE_HOURS",
        multiplier=1_000_000,
        benchmarks=Benchmarks(2.0, 4.0, 8.0, 15.0),
        higher_is_better=False,
        display_format="decimal_2_places",
    ),
    "TRIR": KpiDefinition(
        code="TRIR",
        name="Total Recordable Incident Rate",
        formula="(Recordable Injuries × 200,000) ÷ Net Exposure Hours",
        statutory_reference="OSHA 29 CFR 1904",
        numerator=Numerator("MODULE_COUNT", source="incident", types=RECORDABLE_TYPES),
        denominator="EXPOSURE_HOURS",
        multiplier=200_000,
        benchmarks=Benchmarks(0.5, 1.0, 3.0, 5.0),
        higher_is_better=False,
        display_format="decimal_2_places",
    ),
    "IFR": KpiDefinition(
        code="IFR",
        name="Injury Frequency Rate",
        formula="(All Injuries × 1,000,000) ÷ Net Exposure Hours",
        statutory_reference="IS 3786:1983 (injury frequency)",
        numerator=Numerator("MODULE_COUNT", source="incident", types=ALL_INJURY_TYPES),
        denominator="EXPOSURE_HOURS",
        multiplier=1_000_000,
        benchmarks=Benchmarks(3.0, 6.0, 12.0, 20.0),
        higher_is_better=False,
        display_format="decimal_2_places",
    ),
    "DART_RATE": KpiDefinition(
        code="DART_RATE",
        name="Days Away, Restricted, Transferred Rate",
        formula="(DART Cases × 200,000) ÷ Net Exposure Hours",
        statutory_reference="OSHA 29 CFR 1904",
        numerator=Numerator("MODULE_COUNT", source="incident", types=DART_TYPES),
        denominator="EXPOSURE_HOURS",
        multiplier=200_000,
        benchmarks=Benchmarks(0.3, 0.7, 2.0, 4.0),
        higher_is_better=False,
        display_format="decimal_2_places",
    ),
    "SEVERITY_RATE": KpiDefinition(
        code="SEVERITY_RATE",
        name="Severity Rate",
        formula="(Days Lost + 6000 × Fatalities) × 1,000,000 ÷ Net Exposure Hours",
        statutory_reference="IS 3786:1983",
        numerator=Numerator("CUSTOM", tag="SEVERITY_NUMERATOR"),
        denominator="EXPOSURE_HOURS",
        multiplier=1_000_000,
        benchmarks=Benchmarks(50, 150, 500, 1500),
        higher_is_better=False,
        display_format="decimal_2_places",
    ),
    "FSI": KpiDefinition(
        code="FSI",
        name="Frequency-Severity Index",
        formula="√((LTIFR × Severity Rate) ÷ 1000)",
        statutory_reference="IS 3786:1983",
        numerator=Numerator("DERIVED", tag="FSI", source_kpis=("LTIFR", "SEVERITY_RATE")),
        denominator="NONE",
        multiplier=1,
        higher_is_better=False,
        display_format="decimal_2_places",
    ),
    "NEAR_MISS_RATE": KpiDefinition(
        code="NEAR_MISS_RATE",
        name="Near Miss Reporting Rate",
        formula="(Near Misses × 1,000,000) ÷ Net Exposure Hours",
        statutory_reference="Internal leading indicator",
        # No status filter: every reported near miss counts, open ones
        # included. A strong reporting culture IS the signal here.
        numerator=Numerator("MODULE_COUNT", source="nearMiss"),
        denominator="EXPOSURE_HOURS",
        multiplier=1_000_000,
        benchmarks=Benchmarks(1000, 500, 200, 50),
        higher_is_better=True,
        display_format="decimal_2_places",
    ),
    "OBSERVATION_RATE": KpiDefinition(
        code="OBSERVATION_RATE",
        name="Safety Observation Reporting Rate",
        formula="(Observations × 1,000,000) ÷ Net Exposure Hours",
        numerator=Numerator("MODULE_COUNT", source="observation"),
        denominator="EXPOSURE_HOURS",
        multiplier=1_000_000,
        benchmarks=Benchmarks(5000, 2000, 800, 200),
        higher_is_better=True,
        display_format="decimal_2_places",
    ),
    "HEINRICH_RATIO": KpiDefinition(
        code="HEINRICH_RATIO",
        name="Heinrich Ratio (Near Miss : Incident)",
        formula="Near Misses ÷ Total Recordable Incidents",
        numerator=Numerator(
            "DERIVED", tag="HEINRICH_RATIO", source_kpis=("NEAR_MISS_RATE", "TRIFR")
        ),
        denominator="NONE",
        multiplier=1,
        benchmarks=Benchmarks(300, 100, 30, 10),
        higher_is_better=True,
        display_format="decimal_2_places",
    ),
    "CAPA_CLOSURE_RATE": KpiDefinition(
        code="CAPA_CLOSURE_RATE",
        name="CAPA On-Time Closure Rate",
        formula="(CAPAs Closed On Time ÷ Total CAPAs Due) × 100",
        numerator=Numerator("CUSTOM", tag="CAPA_CLOSURE"),
        denominator="NONE",
        multiplier=1,
        benchmarks=Benchmarks(95, 85, 70, 50),
        higher_is_better=True,
        is_percentage=True,
        display_format="percent",
    ),
    "TRAINING_COMPLIANCE": KpiDefinition(
        code="TRAINING_COMPLIANCE",
        name="Training Compliance Rate",
        formula="(Employees with Valid Mandatory Training ÷ Total Employees) × 100",
        numerator=Numerator("CUSTOM", tag="TRAINING_COMPLIANCE"),
        denominator="NONE",
        multiplier=1,
        benchmarks=Benchmarks(98, 95, 85, 70),
        higher_is_better=True,
        is_percentage=True,
        display_format="percent",
    ),
    "INSPECTION_COMPLIANCE": KpiDefinition(
        code="INSPECTION_COMPLIANCE",
        name="Inspection Compliance Rate",
        formula="(Inspections Completed On Time ÷ Total Scheduled) × 100",
        numerator=Numerator("CUSTOM", tag="INSPECTION_COMPLIANCE"),
        denominator="NONE",
        multiplier=1,
        benchmarks=Benchmarks(98, 95, 85, 70),
        higher_is_better=True,
        is_percentage=True,
        display_format="percent",
    ),
    "PTW_FLRA_COMPLIANCE": KpiDefinition(
        code="PTW_FLRA_COMPLIANCE",
        name="PTW-FLRA Linkage Compliance",
        formula="(PTWs with Linked FLRA ÷ Total PTWs Activated) × 100",
        numerator=Numerator("CUSTOM", tag="PTW_FLRA_COMPLIANCE"),
        denominator="NONE",
        multiplier=1,
        # Always 100%. Anything less is a process failure, not a performance
        # band — hence a fixed target rather than benchmarks.
        target_value=100,
        higher_is_better=True,
        is_percentage=True,
        display_format="percent",
    ),
    "DAYS_SINCE_LAST_LTI": KpiDefinition(
        code="DAYS_SINCE_LAST_LTI",
        name="Days Since Last LTI",
        formula="DATEDIFF(NOW, MAX(LTI.occurredAt))",
        numerator=Numerator("DAYS_SINCE", source="incident", types=LTI_TYPES),
        denominator="NONE",
        multiplier=1,
        higher_is_better=True,
        is_streak_metric=True,
        display_format="integer",
    ),
    "COST_OF_INCIDENTS": KpiDefinition(
        code="COST_OF_INCIDENTS",
        name="Total Cost of Incidents",
        formula="SUM(Incident.costTotal)",
        numerator=Numerator("CUSTOM", tag="COST_OF_INCIDENTS"),
        denominator="NONE",
        multiplier=1,
        higher_is_better=False,
        display_format="currency_indian",
    ),
}

# Fail fast on a typo rather than silently omitting a KPI from every report.
for _code in KPI_CODES:
    if _code not in KPI_REGISTRY:
        raise RuntimeError(f"KPI_REGISTRY missing entry for code: {_code}")
    if KPI_REGISTRY[_code].code != _code:
        raise RuntimeError(
            f'KPI_REGISTRY entry "{_code}" has mismatched code: {KPI_REGISTRY[_code].code}'
        )


def band_for(definition: KpiDefinition, value: float | None) -> str | None:
    """Which performance band a value falls in, or None when the KPI has no
    benchmarks (streaks, costs, fixed-target compliance)."""
    if value is None or definition.benchmarks is None:
        return None
    b = definition.benchmarks
    if definition.higher_is_better:
        if value >= b.worldClass:
            return "WORLD_CLASS"
        if value >= b.excellent:
            return "EXCELLENT"
        if value >= b.average:
            return "AVERAGE"
        return "POOR"
    if value <= b.worldClass:
        return "WORLD_CLASS"
    if value <= b.excellent:
        return "EXCELLENT"
    if value <= b.average:
        return "AVERAGE"
    return "POOR"


__all__ = [
    "KPI_CODES",
    "KPI_REGISTRY",
    "REGISTRY_VERSION",
    "KpiDefinition",
    "Numerator",
    "Benchmarks",
    "band_for",
    "FATALITY_DAY_CHARGE",
    "LTI_TYPES",
    "RECORDABLE_TYPES",
    "ALL_INJURY_TYPES",
    "DART_TYPES",
]
