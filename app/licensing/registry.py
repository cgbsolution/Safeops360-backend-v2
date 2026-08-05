"""Module Registry — the canonical, code-defined list of gateable modules.

This is the *vocabulary* every licence references. A licence can only enable
codes that exist here; the Licence Authority validates against it at issue
time and the validator resolves dependencies against it at runtime.

Two classes of module:
  * core (`is_core=True`)  — identity, org/site, RBAC, audit trail, the
    workflow engine, the dashboard shell, and the licensing system itself.
    These are ALWAYS enabled regardless of licence so a client can never be
    locked out of their own data / the renewal screen (build prompt §2.4, TL-14).
  * gateable (`is_core=False`) — everything a client actually buys.

`depends_on` lets a finer-grained module pull in its base. e.g. KRI depends on
ERM, so enabling KRI auto-enables ERM during dependency resolution — which is
also what lets us guard the shared erm_p2/p3/t3 routers on the single ERM code.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModuleDefinition:
    code: str
    name: str
    group: str
    is_core: bool = False
    depends_on: tuple[str, ...] = field(default_factory=tuple)


def _m(code: str, name: str, group: str, *, core: bool = False,
       depends_on: tuple[str, ...] = ()) -> ModuleDefinition:
    return ModuleDefinition(code=code, name=name, group=group, is_core=core,
                            depends_on=depends_on)


# ── Core infrastructure — always on, cannot be disabled by any licence ───────
_CORE: list[ModuleDefinition] = [
    _m("CORE_IDENTITY", "Identity & Users", "Core", core=True),
    _m("CORE_ORG", "Organisation, Plants & Sites", "Core", core=True),
    _m("CORE_RBAC", "Roles & Permissions", "Core", core=True),
    _m("CORE_AUDIT_TRAIL", "Audit Trail", "Core", core=True),
    _m("CORE_WORKFLOW", "Workflow & Inbox Engine", "Core", core=True),
    _m("CORE_DASHBOARD", "Home Dashboard Shell", "Core", core=True),
    _m("CORE_LICENSING", "Licensing & Entitlements", "Core", core=True),
]

# ── Gateable product modules ─────────────────────────────────────────────────
_PRODUCT: list[ModuleDefinition] = [
    # Operational Safety
    _m("OBSERVATION", "Safety Observation", "Operational Safety"),
    _m("NEAR_MISS", "Near Miss", "Operational Safety"),
    _m("PTW", "Permit to Work", "Operational Safety"),
    _m("FLRA", "Field-Level Risk Assessment", "Operational Safety"),
    _m("INCIDENT", "Incident Investigation", "Operational Safety"),
    _m("FIRE", "Fire Safety & Emergency Response", "Operational Safety"),
    # Risk Management
    _m("HIRA", "HIRA — Hazard Risk Register", "Risk Management"),
    _m("EAI", "EAI — Environmental Aspect/Impact", "Risk Management"),
    _m("CAPA", "CAPA — Universal", "Risk Management"),
    _m("MOC", "Management of Change", "Risk Management"),
    _m("STATUTORY_REGISTERS", "Statutory Registers", "Risk Management"),
    # Aggregates HIRA/EAI/ERM where present; degrades to what's entitled
    # rather than hard-requiring a feed (no depends_on — see TL-15).
    _m("RISK_AGG", "Combined Risk Register & Aggregation", "Risk Management"),
    # Enterprise Risk Management (ERM suite — sub-modules pull in the ERM base)
    _m("ERM", "Enterprise Risk Register", "Enterprise Risk"),
    _m("KRI", "Key Risk Indicators", "Enterprise Risk", depends_on=("ERM",)),
    _m("APPETITE", "Risk Appetite", "Enterprise Risk", depends_on=("ERM",)),
    _m("ERM_COMPLIANCE", "Compliance Obligations", "Enterprise Risk", depends_on=("ERM",)),
    _m("LOSS", "Loss Events", "Enterprise Risk", depends_on=("ERM",)),
    _m("BCM", "Business Continuity (ISO 22301)", "Enterprise Risk", depends_on=("ERM",)),
    _m("CONTROL", "Internal Controls", "Enterprise Risk", depends_on=("ERM",)),
    _m("VENDOR", "Vendor / ESG Risk", "Enterprise Risk", depends_on=("ERM",)),
    _m("INSURANCE", "Insurance & Risk Transfer", "Enterprise Risk", depends_on=("ERM",)),
    # Facilities
    _m("FACILITIES", "Factory Profile & Facilities", "Facilities"),
    # Audit & Compliance (CAMS) — covers the audit engine + inspections engine
    _m("CAMS", "CAMS — Audit & Compliance", "Audit & Compliance"),
    # People & Competency
    _m("TRAINING", "Training", "People & Competency"),
    _m("COMPETENCY", "Skill Matrix & Competency", "People & Competency"),
    _m("SCI", "Safety Culture Index & Kaizen", "People & Competency"),
    _m("SAFETY_CULTURE", "Safety Culture Management", "People & Competency"),
    # Assets & Inspection
    _m("PPE", "PPE Management", "Assets & Inspection"),
    _m("INSPECTION", "Inspection Schedule & Findings", "Assets & Inspection"),
    # Field Operations
    _m("CAPTURE", "Guided Field Capture", "Operational Safety"),
    _m("ALERTS", "Daily Alert Brief", "Performance"),
    # Performance
    _m("MANHOURS", "Manhours & KPIs", "Performance"),
    _m("ANOMALIES", "Anomaly Detection", "Performance"),
    # EPC / Sites
    _m("EPC", "Engineering, Procurement & Construction", "EPC / Sites"),
    # AI Assistance
    _m("AI_ASSIST", "AI Agents & Assistance", "AI Assistance"),
]

MODULE_REGISTRY: dict[str, ModuleDefinition] = {
    m.code: m for m in (_CORE + _PRODUCT)
}

CORE_MODULE_CODES: frozenset[str] = frozenset(
    m.code for m in MODULE_REGISTRY.values() if m.is_core
)

ALL_PRODUCT_CODES: frozenset[str] = frozenset(
    m.code for m in MODULE_REGISTRY.values() if not m.is_core
)


def is_known_module(code: str) -> bool:
    return code in MODULE_REGISTRY


def unknown_modules(codes) -> list[str]:
    """Return any codes not present in the registry — used by the Licence
    Authority to reject typos at issue time rather than ship a dead claim."""
    return [c for c in codes if c not in MODULE_REGISTRY]


def resolve_dependencies(codes) -> set[str]:
    """Transitively expand a set of module codes to include every `depends_on`
    base. Enabling KRI thus also enables ERM, which is how we can guard the
    shared ERM routers on the single ERM code. Stops at the first cycle (the
    registry is acyclic, but be defensive)."""
    resolved: set[str] = set()
    stack = list(codes)
    while stack:
        code = stack.pop()
        if code in resolved:
            continue
        mod = MODULE_REGISTRY.get(code)
        if mod is None:
            # Unknown code — drop it; the Authority should have caught this.
            continue
        resolved.add(code)
        for dep in mod.depends_on:
            if dep not in resolved:
                stack.append(dep)
    return resolved


def build_enabled_set(enabled_modules) -> frozenset[str]:
    """The authoritative enabled-module set used for enforcement:
    licence-granted modules + their resolved dependencies + always-on core.
    Core is unioned in unconditionally (build prompt §2.4 / §5.1 step 5)."""
    resolved = resolve_dependencies(enabled_modules)
    return frozenset(resolved | set(CORE_MODULE_CODES))
