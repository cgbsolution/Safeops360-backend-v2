"""WP-47 - buyer-regime template support (SMETA, BSCI, WRAP, Higg FEM, SLCP).

docs/cams/09 §3.7. Open questions Q7 (**yes, seed them**) and Q19
(**self-design the structures**).

**What Q19 means in practice, stated plainly.** These regime definitions are
**authored by SafeOps360**. They are NOT the official measurement criteria of
Sedex, amfori, WRAP, Cascale/Higg or SLCP - those are licensed documents and are
not reproduced here. What this module provides is the *engineering shape* each
regime needs: its severity taxonomy, its result scale, its section structure and
its scoring style. A customer running a real SMETA audit loads the official
checkpoint content into this shape; the shape is ours, the content is theirs.

Every seeded template is labelled `authored: "SafeOps360"` and carries a
`disclaimer` so nobody mistakes it for the licensed instrument. That labelling
is the difference between a useful starting structure and a misrepresentation.

**What the model was missing.** Each regime differs on three axes the audit
engine had hard-coded:

  1. **Severity taxonomy** - the engine has `critical/major/minor/observation`.
     SMETA-style audits use Business-Critical / Critical / Major / Minor;
     BSCI-style grade A-E; WRAP uses pass/corrective-action.
  2. **Result scale** - pass/partial/fail/na vs conform/nc/na vs a 0-3 maturity
     score (Higg-style) vs yes/no/partial with a weighting.
  3. **Section nesting** - the library is flat (category -> checkpoint). Several
     regimes are three-deep (pillar -> section -> question).

`RegimeSpec` declares all three per regime, so a template can be tagged and the
engine renders the right vocabulary without a schema change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The disclaimer stamped onto every seeded regime template.
AUTHORSHIP_DISCLAIMER = (
    "Structure authored by SafeOps360 to match the shape of this audit regime. "
    "It is not the regime owner's official measurement criteria, which are "
    "licensed separately. Load your licensed checkpoint content into this "
    "structure."
)


@dataclass(frozen=True)
class SeverityLevel:
    code: str
    label: str
    # Maps onto the engine's native criticality so scoring, CAPA severity and
    # the critical-failure gate keep working unchanged. This mapping is the
    # whole reason a regime can be added without touching the scorer.
    nativeCriticality: str  # critical | major | minor | observation
    requiresImmediateAction: bool = False


@dataclass(frozen=True)
class ResultOption:
    code: str
    label: str
    # Normalised bucket the engine scores on: pass | partial | fail | na.
    nativeBucket: str
    # Optional numeric weight for maturity-style scales (Higg). None = binary.
    weight: float | None = None


@dataclass(frozen=True)
class RegimeSpec:
    code: str
    name: str
    owner: str
    # PILLAR_SECTION_QUESTION (3 deep) or CATEGORY_QUESTION (the engine's native 2)
    nesting: str
    severities: tuple[SeverityLevel, ...]
    results: tuple[ResultOption, ...]
    scoringStyle: str  # GATE | PERCENTAGE | MATURITY
    notes: str = ""
    sections: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "owner": self.owner,
            "authored": "SafeOps360",
            "disclaimer": AUTHORSHIP_DISCLAIMER,
            "nesting": self.nesting,
            "scoringStyle": self.scoringStyle,
            "notes": self.notes,
            "sections": list(self.sections),
            "severities": [
                {
                    "code": s.code,
                    "label": s.label,
                    "nativeCriticality": s.nativeCriticality,
                    "requiresImmediateAction": s.requiresImmediateAction,
                }
                for s in self.severities
            ],
            "results": [
                {
                    "code": r.code,
                    "label": r.label,
                    "nativeBucket": r.nativeBucket,
                    "weight": r.weight,
                }
                for r in self.results
            ],
        }


_PASS_FAIL = (
    ResultOption("COMPLIANT", "Compliant", "pass"),
    ResultOption("PARTIAL", "Partially compliant", "partial"),
    ResultOption("NON_COMPLIANT", "Non-compliant", "fail"),
    ResultOption("NA", "Not applicable", "na"),
)


REGIMES: dict[str, RegimeSpec] = {
    # ── Social-audit shape, 4-tier severity with a zero-tolerance top band ────
    "SMETA_LIKE": RegimeSpec(
        code="SMETA_LIKE",
        name="Ethical Trade Audit (SMETA-shaped)",
        owner="Sedex",
        nesting="PILLAR_SECTION_QUESTION",
        scoringStyle="GATE",
        sections=("Labour Standards", "Health & Safety", "Environment", "Business Ethics"),
        severities=(
            SeverityLevel("BUSINESS_CRITICAL", "Business Critical", "critical", True),
            SeverityLevel("CRITICAL", "Critical", "critical", True),
            SeverityLevel("MAJOR", "Major", "major"),
            SeverityLevel("MINOR", "Minor", "minor"),
            SeverityLevel("OBSERVATION", "Observation", "observation"),
        ),
        results=_PASS_FAIL,
        notes=(
            "Four-pillar structure. Business-critical issues (forced labour, child labour, "
            "imminent danger to life) are zero-tolerance and gate the result regardless of "
            "overall score."
        ),
    ),
    # ── Graded social audit: an A-E letter grade, not a percentage ────────────
    "BSCI_LIKE": RegimeSpec(
        code="BSCI_LIKE",
        name="Social Compliance Audit (amfori BSCI-shaped)",
        owner="amfori",
        nesting="PILLAR_SECTION_QUESTION",
        scoringStyle="PERCENTAGE",
        sections=(
            "Social Management System", "Workers Involvement", "Freedom of Association",
            "No Discrimination", "Fair Remuneration", "Decent Working Hours",
            "Occupational Health & Safety", "No Child Labour", "No Precarious Employment",
            "No Bonded Labour", "Protection of the Environment", "Ethical Business Behaviour",
        ),
        severities=(
            SeverityLevel("ZERO_TOLERANCE", "Zero tolerance", "critical", True),
            SeverityLevel("CRUCIAL", "Crucial", "critical"),
            SeverityLevel("MAJOR", "Major", "major"),
            SeverityLevel("MINOR", "Minor", "minor"),
        ),
        results=_PASS_FAIL,
        notes=(
            "Twelve performance areas, each rated, rolling into an overall A-E grade. "
            "A zero-tolerance finding caps the grade irrespective of the others."
        ),
    ),
    # ── Certification-style: pass / corrective action, no partial credit ──────
    "WRAP_LIKE": RegimeSpec(
        code="WRAP_LIKE",
        name="Responsible Production Certification (WRAP-shaped)",
        owner="WRAP",
        nesting="CATEGORY_QUESTION",
        scoringStyle="GATE",
        sections=(
            "Compliance with Laws", "Prohibition of Forced Labour",
            "Prohibition of Child Labour", "Prohibition of Harassment or Abuse",
            "Compensation and Benefits", "Hours of Work",
            "Prohibition of Discrimination", "Health and Safety",
            "Freedom of Association", "Environment", "Customs Compliance", "Security",
        ),
        severities=(
            SeverityLevel("NON_COMPLIANCE", "Non-compliance", "critical", True),
            SeverityLevel("CORRECTIVE_ACTION", "Corrective action required", "major"),
            SeverityLevel("OBSERVATION", "Observation", "observation"),
        ),
        # Deliberately no PARTIAL: this regime shape is binary by design, and
        # offering a middle option would let an auditor dodge the actual call.
        results=(
            ResultOption("COMPLIANT", "Compliant", "pass"),
            ResultOption("NON_COMPLIANT", "Non-compliant", "fail"),
            ResultOption("NA", "Not applicable", "na"),
        ),
        notes="Twelve principles, binary assessment. Certification is gated on full compliance.",
    ),
    # ── Maturity-scored environmental self/verified assessment ───────────────
    "HIGG_FEM_LIKE": RegimeSpec(
        code="HIGG_FEM_LIKE",
        name="Facility Environmental Module (Higg FEM-shaped)",
        owner="Cascale",
        nesting="PILLAR_SECTION_QUESTION",
        scoringStyle="MATURITY",
        sections=(
            "Environmental Management System", "Energy & Greenhouse Gases",
            "Water Use", "Wastewater", "Emissions to Air", "Waste Management",
            "Chemicals Management",
        ),
        severities=(
            SeverityLevel("CRITICAL_GAP", "Critical gap", "critical", True),
            SeverityLevel("IMPROVEMENT", "Improvement required", "major"),
            SeverityLevel("OPPORTUNITY", "Opportunity", "observation"),
        ),
        # A LEVEL scale, not pass/fail — the point of a maturity module is that
        # "we measure it" and "we improve on it" are different achievements.
        results=(
            ResultOption("LEVEL_0", "Not started", "fail", 0.0),
            ResultOption("LEVEL_1", "Tracking & baseline", "partial", 0.33),
            ResultOption("LEVEL_2", "Targets & improvement", "partial", 0.66),
            ResultOption("LEVEL_3", "Best practice & verified", "pass", 1.0),
            ResultOption("NA", "Not applicable", "na", None),
        ),
        notes=(
            "Maturity levels rather than pass/fail: each section scores 0-3 and the module "
            "reports a weighted percentage per environmental area."
        ),
    ),
    # ── Converged social data collection: data quality matters as much as answer
    "SLCP_LIKE": RegimeSpec(
        code="SLCP_LIKE",
        name="Converged Social Data Collection (SLCP-shaped)",
        owner="SLCP",
        nesting="PILLAR_SECTION_QUESTION",
        scoringStyle="PERCENTAGE",
        sections=(
            "Recruitment & Hiring", "Working Hours", "Wages & Benefits",
            "Employee Treatment", "Employee Involvement", "Health & Safety",
            "Termination", "Management Systems", "Above & Beyond",
        ),
        severities=(
            SeverityLevel("CRITICAL", "Critical", "critical", True),
            SeverityLevel("MAJOR", "Major", "major"),
            SeverityLevel("MINOR", "Minor", "minor"),
            SeverityLevel("DATA_GAP", "Data gap", "observation"),
        ),
        results=_PASS_FAIL,
        notes=(
            "Data-collection rather than pass/fail certification: the output is a verified "
            "data set a buyer interprets against their own standard. DATA_GAP records a "
            "question the facility could not evidence, which is itself a reportable outcome."
        ),
    ),
}


def get_regime(code: str) -> RegimeSpec | None:
    return REGIMES.get((code or "").upper())


def list_regimes() -> list[dict[str, Any]]:
    return [r.as_dict() for r in REGIMES.values()]


def native_criticality(regime_code: str, severity_code: str) -> str:
    """Regime severity -> the engine's native criticality.

    Falls back to `major` for an unknown code rather than dropping the finding:
    an unmapped severity that silently became `observation` would understate a
    real problem, which is the failure mode that matters here.
    """
    spec = get_regime(regime_code)
    if spec:
        for s in spec.severities:
            if s.code == severity_code.upper():
                return s.nativeCriticality
    return "major"


def native_bucket(regime_code: str, result_code: str) -> str | None:
    """Regime result -> the scoring bucket. None when unrecognised."""
    spec = get_regime(regime_code)
    if spec:
        for r in spec.results:
            if r.code == result_code.upper():
                return r.nativeBucket
    return None


def regime_ready(regime_code: str, discipline_codes: list[str]) -> dict[str, Any]:
    """"Are we SMETA-ready?" - which of a regime's sections the scope covers.

    Matches on a normalised section name against the discipline labels in scope.
    Deliberately a coarse string match, and reported as such: it answers "have
    you scoped anything against this section", not "would you pass".
    """
    spec = get_regime(regime_code)
    if spec is None:
        return {"known": False, "regime": regime_code}

    def norm(s: str) -> str:
        return "".join(ch for ch in s.lower() if ch.isalnum())

    have = {norm(d) for d in discipline_codes}
    covered, missing = [], []
    for sec in spec.sections:
        n = norm(sec)
        if any(n in h or h in n for h in have):
            covered.append(sec)
        else:
            missing.append(sec)
    total = len(spec.sections) or 1
    return {
        "known": True,
        "regime": spec.code,
        "name": spec.name,
        "coveredSections": covered,
        "missingSections": missing,
        "coveragePct": round(len(covered) / total * 100, 1),
        "caveat": (
            "Section-name matching only: this indicates what has been SCOPED against the "
            "regime, not whether the facility would pass it."
        ),
    }


__all__ = [
    "AUTHORSHIP_DISCLAIMER",
    "RegimeSpec",
    "SeverityLevel",
    "ResultOption",
    "REGIMES",
    "get_regime",
    "list_regimes",
    "native_criticality",
    "native_bucket",
    "regime_ready",
]
