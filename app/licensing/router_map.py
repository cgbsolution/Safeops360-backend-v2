"""Router → module map — the single source of truth for which API surface each
gateable module owns. main.py uses it to attach `require_module(...)` to every
gated router at include time, so the entitlement check is the API security
boundary (build prompt §5.2) regardless of how a route is called.

A value of None means CORE / always-reachable (identity, org, RBAC, workflow,
dashboard, licensing) — these are never gated so a client can never be locked
out of their own data or the renewal screen (§2.4, TL-14).

Keyed by the router's import name in app.routers (e.g. "ptw_active") to keep the
mapping declarative and reviewable in one place.
"""

from __future__ import annotations

ROUTER_MODULE: dict[str, str | None] = {
    # ── Core / always reachable ──
    "auth": None,
    "users": None,
    "plants": None,
    "workflow": None,
    "workflow_definitions": None,
    "dashboard": None,
    "devices": None,
    "licensing": None,
    # ── Operational Safety ──
    "observations": "OBSERVATION",
    # SLA matrix + deroster review are part of the Observation module and gate
    # with it. `workforce` (the Worker Involved picker) stays ungated like
    # `observation_taxonomy` — it is shared lookup data, auth-gated per endpoint.
    "observation_sla": "OBSERVATION",
    "observation_deroster": "OBSERVATION",
    "near_miss": "NEAR_MISS",
    "ptw": "PTW",
    "ptw_active": "PTW",
    "ptw_lifecycle": "PTW",
    "ptw_reports": "PTW",
    "flra": "FLRA",
    "incidents": "INCIDENT",
    # ── Risk Management ──
    "hira": "HIRA",
    "eai": "EAI",
    "capa": "CAPA",
    "moc": "MOC",
    "risk_register": "RISK_AGG",
    "risk_dashboard": "RISK_AGG",
    # ── Enterprise Risk (the sub-modules share these routers; all gate on ERM,
    #    which is auto-enabled whenever any ERM sub-module is licensed) ──
    "erm": "ERM",
    "erm_p2": "ERM",
    "erm_p3": "ERM",
    "erm_t3": "ERM",
    "rca": "ERM",  # Cross-Domain RCA & Causal Intelligence (ERM sub-module)
    # ── Audit & Compliance (CAMS) ──
    "audit_compliance": "CAMS",
    "cams": "CAMS",
    # ── Facilities ──
    "factory": "FACILITIES",
    "factory_ext": "FACILITIES",
    # ── People & Competency ──
    "training": "TRAINING",
    "competency": "COMPETENCY",
    "sci": "SCI",
    "scr": "SCI",
    "kaizen": "SCI",
    # ── Assets & Inspection ──
    "ppe": "PPE",
    "inspections": "INSPECTION",
    "inspection_findings": "INSPECTION",
    # ── Performance ──
    "manhours": "MANHOURS",
    "anomalies": "ANOMALIES",
    # ── AI Assistance ──
    "agents": "AI_ASSIST",
    "agents_config": "AI_ASSIST",
    # ── EPC / Sites ──
    "epc_sites": "EPC",
    "epc_contractors": "EPC",
    "epc_workers": "EPC",
    "epc_mobilization": "EPC",
    "epc_gate": "EPC",
    "epc_induction": "EPC",
    "epc_dashboard": "EPC",
}
