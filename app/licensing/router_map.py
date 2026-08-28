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
    # Spans seven differently-licensed modules — gating happens per route
    # inside the router, not here. See app/routers/analytics_strip.py.
    "analytics_strip": None,
    "masters": None,
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
    # ── The Operations bundle: Fire & Life Safety + Chemical/Hazmat ──
    #
    # These three routers were mounted UNGATED, so every deployment could reach
    # the fire register, the checklist engine and the full chemical inventory
    # regardless of what its licence granted. That was a bootstrap left over
    # from before FIRE and CHEMICAL existed as codes, not a decision.
    #
    # There is deliberately NO "OPERATIONS" module code. Fire and Chemical/
    # Hazmat together ARE the Operations bundle, gated on the two codes that
    # already exist — inventing a third would mean a new vocabulary in the
    # registry, both RBAC seeders and every issued licence, to express something
    # these two already express.
    #
    # `fire_checklists` gates on FIRE rather than CAMS even though a checklist
    # run IS a CamsEngagement: the authority being licensed is the fire
    # register's periodic inspection, and a CAMS-only client must not get the
    # fire checklist engine for free by owning the engine it happens to run on.
    #
    # ┌─────────────────────────────────────────────────────────────────────┐
    # │ TEMPORARILY DISABLED — 2026-08-28. RESTORE, DO NOT DELETE.          │
    # └─────────────────────────────────────────────────────────────────────┘
    #
    # These three shipped to production ahead of a licence carrying FIRE and
    # CHEMICAL, so Fire Safety & ER and Chemical & Hazmat both went dark with
    # "module is not included in your licence edition" — two screens that had
    # worked the day before.
    #
    # The fix is a reissued licence, NOT this. But the Ed25519 signing key
    # (`vf-2026-06`) could not be located, so no licence granting those codes
    # can be produced at all until a key rotation is done. Rather than leave a
    # live fire register unreachable while that is sorted out, the gate is
    # lifted — restoring exactly the behaviour these modules had before Build 2.
    #
    # WHAT THIS RE-OPENS, stated plainly so it is not forgotten: with these
    # commented out, /api/fire/* and /api/chemicals/* are reachable on every
    # deployment regardless of what its licence grants. That was the status quo
    # for the life of the product until Build 2, so it is the old hole back, not
    # a new one — but it IS a hole, and this comment is the only thing tracking
    # it.
    #
    # TO RESTORE (all three conditions, in this order):
    #   1. a signing key exists again  — see docs/reissue-production-licence.md
    #   2. every installation's licence carries FIRE and CHEMICAL — verify with
    #        python scripts/audit_operations_licence.py --fleet <dir-of-.lic>
    #      which exits 1 while any of them would lose access
    #   3. uncomment the three lines below, then deploy
    #
    # RBAC is unaffected and stays on: chemical still requires CHEMICAL.* and
    # fire still requires FIRE.*, so the contractor-reads-the-hazmat-inventory
    # defect Build 2 fixed remains fixed. Only the LICENCE check is lifted.
    #
    # "fire_safety": "FIRE",
    # "fire_checklists": "FIRE",
    # "chemical": "CHEMICAL",
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
    "manhours_submissions": "MANHOURS",
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
