"""Editions / SKUs — named, data-driven module bundles.

Sales quotes a SKU; the Licence Authority expands it to module codes at issue
time (Edition.included_modules → resolve_dependencies → enabled_modules claim).
The validator never reads editions — it trusts the expanded `enabledModules`
claim in the signed payload. Editions exist so issuance is repeatable and so
the admin UI can show "you're on CAMS_ONLY".

Each edition carries default limits the Authority may override per client.
CUSTOM carries no modules — the Authority supplies an explicit list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.licensing.registry import ALL_PRODUCT_CODES


@dataclass(frozen=True)
class EditionLimits:
    max_sites: int | None = None
    max_users: int | None = None
    max_factories: int | None = None


@dataclass(frozen=True)
class Edition:
    code: str
    name: str
    included_modules: tuple[str, ...]
    default_limits: EditionLimits = field(default_factory=EditionLimits)


# The safety / integrated-management bundle (everything an EHS site team runs).
_IMS_MODULES = (
    "CAMS", "CAPA",
    "OBSERVATION", "NEAR_MISS", "PTW", "FLRA", "INCIDENT", "FIRE",
    "HIRA", "EAI", "MOC", "STATUTORY_REGISTERS", "RISK_AGG",
    "PPE", "INSPECTION", "TRAINING", "COMPETENCY", "SCI", "SAFETY_CULTURE",
    "MANHOURS", "ANOMALIES", "AI_ASSIST",
)

# The enterprise-risk bundle.
_ERM_MODULES = (
    "ERM", "KRI", "APPETITE", "ERM_COMPLIANCE", "LOSS",
    "BCM", "CONTROL", "VENDOR", "INSURANCE",
    "RISK_AGG", "CAPA", "AI_ASSIST",
)

EDITIONS: dict[str, Edition] = {
    "CAMS_ONLY": Edition(
        code="CAMS_ONLY",
        name="CAMS — Audit & Compliance (standalone)",
        included_modules=("CAMS", "CAPA", "AI_ASSIST"),
        default_limits=EditionLimits(max_sites=16, max_users=100, max_factories=16),
    ),
    "IMS_CORE": Edition(
        code="IMS_CORE",
        name="Integrated Management System — Core",
        included_modules=_IMS_MODULES,
        default_limits=EditionLimits(max_sites=16, max_users=500, max_factories=16),
    ),
    "ERM_SUITE": Edition(
        code="ERM_SUITE",
        name="Enterprise Risk Management Suite",
        included_modules=_ERM_MODULES,
        default_limits=EditionLimits(max_sites=16, max_users=200, max_factories=16),
    ),
    "FULL_PLATFORM": Edition(
        code="FULL_PLATFORM",
        name="Full Platform",
        included_modules=tuple(sorted(ALL_PRODUCT_CODES)),
        default_limits=EditionLimits(max_sites=None, max_users=None, max_factories=None),
    ),
    "CUSTOM": Edition(
        code="CUSTOM",
        name="Custom",
        included_modules=(),  # explicit per-licence module list
        default_limits=EditionLimits(),
    ),
}


def get_edition(code: str) -> Edition | None:
    return EDITIONS.get(code)


def expand_edition(code: str, custom_modules=None) -> list[str]:
    """Expand an edition code to its base module list. For CUSTOM, the caller
    supplies `custom_modules`. Returns the *unresolved* list (the Authority
    resolves dependencies before signing). Raises on an unknown edition."""
    edition = EDITIONS.get(code)
    if edition is None:
        raise ValueError(f"Unknown edition: {code}")
    modules = list(edition.included_modules)
    if code == "CUSTOM":
        modules = list(custom_modules or [])
    elif custom_modules:
        # Allow an edition to be topped-up with explicit add-on modules.
        modules = list(dict.fromkeys(modules + list(custom_modules)))
    return modules
