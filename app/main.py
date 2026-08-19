"""FastAPI app factory.

`uvicorn app.main:app --reload` to run in development.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.db import engine, warmup
from app.licensing.enforcement import require_module
from app.licensing.router_map import ROUTER_MODULE
from app.licensing.state import refresh_state
from app.routers import (
    agents,
    agents_config,
    alerts,
    analytics_strip,
    anomalies,
    assurance,
    attachments,
    audit_compliance,
    audit_log,
    auth,
    calendar,
    cams,
    cams_completion,
    capa,
    capture,
    chemical,
    supplier_portal,
    competency,
    training_engine,
    dashboard,
    devices,
    eai,
    erm,
    erm_attachments,
    erm_p2,
    erm_p3,
    erm_t3,
    epc_contractors,
    epc_dashboard,
    epc_gate,
    epc_induction,
    epc_mobilization,
    epc_sites,
    epc_workers,
    factory,
    factory_ext,
    fire_checklists,
    fire_safety,
    plants,
    programme,
    flra,
    hira,
    incidents,
    insights,
    inspection_findings,
    inspections,
    jobs,
    whatsapp,
    kaizen,
    licensing,
    manhours,
    manhours_submissions,
    masters,
    moc,
    near_miss,
    notifications,
    observation_deroster,
    observation_severity,
    observation_sla,
    observation_taxonomy,
    observations,
    workforce,
    ppe,
    ptw,
    ptw_active,
    ptw_lifecycle,
    ptw_reports,
    rca,
    rca_field,
    risk_dashboard,
    risk_register,
    safety_culture,
    sci,
    scr,
    training,
    users,
    workflow,
    workflow_definitions,
)

settings = get_settings()
logging.basicConfig(level=settings.log_level)
log = logging.getLogger("safeops360")

# Every router this app mounts, keyed by its import name (matches ROUTER_MODULE).
# Order only affects OpenAPI grouping.
_ROUTERS = {
    "auth": auth, "users": users,
    # MUST be mounted BEFORE `observations`: this dict's order is the mount
    # order, and observations.py owns `GET /api/observations/{observation_id}`,
    # which would otherwise swallow `/api/observations/sla-config` and resolve
    # it as an observation id.
    "observation_sla": observation_sla,
    # Same ordering constraint: owns `/api/observations/severity-suggestion`,
    # which `observations` would otherwise match as `/{observation_id}`.
    "observation_severity": observation_severity,
    "observation_deroster": observation_deroster,
    "observations": observations, "near_miss": near_miss,
    # Unified Worker Involved picker over User + ContractorWorker (the two
    # disjoint people tables). Ungated like the taxonomy lookup — per-endpoint
    # auth plus plant scoping is the real gate.
    "workforce": workforce,
    # DuPont STOP category/sub-category master for the observation forms.
    # Ungated (read-only dropdown data, auth-gated per endpoint) so the same
    # lookup serves web, /capture PWA and mobile without a licence carve-out.
    "observation_taxonomy": observation_taxonomy,
    "ptw": ptw, "ptw_active": ptw_active, "ptw_lifecycle": ptw_lifecycle,
    "ptw_reports": ptw_reports, "flra": flra, "incidents": incidents,
    "training": training, "inspections": inspections, "inspection_findings": inspection_findings, "manhours": manhours,
    "workflow": workflow, "workflow_definitions": workflow_definitions, "anomalies": anomalies,
    "agents": agents, "agents_config": agents_config, "hira": hira, "capa": capa, "eai": eai,
    "erm": erm, "erm_attachments": erm_attachments, "erm_p2": erm_p2, "erm_p3": erm_p3, "erm_t3": erm_t3, "competency": competency,
    # Training & Competency Engine (Trigger + Assignment + Content Adapter +
    # Correlation). Mounted ungated (insights/capture precedent) — per-endpoint
    # SKILL_MATRIX RBAC is the real gate; the signed dev licence predates the code.
    "training_engine": training_engine,
    "moc": moc, "risk_register": risk_register, "risk_dashboard": risk_dashboard,
    "rca": rca, "rca_field": rca_field, "notifications": notifications,
    "scr": scr, "sci": sci, "kaizen": kaizen, "safety_culture": safety_culture, "ppe": ppe,
    "epc_sites": epc_sites, "epc_contractors": epc_contractors, "epc_workers": epc_workers,
    "epc_mobilization": epc_mobilization, "epc_gate": epc_gate, "epc_induction": epc_induction,
    "epc_dashboard": epc_dashboard, "audit_compliance": audit_compliance, "cams": cams,
    # Assurance integrity (docs/cams/09 Part 2) — auditor independence,
    # competence linkage, meeting records, report integrity. Mounted alongside
    # CAMS and gated by the SAME CAMS.* permission codes, so no RBAC migration
    # is needed for a tenant that already has CAMS.
    "assurance": assurance,
    # Annual Audit Programme (docs/cams/08) — the artefact a certification body
    # asks for BEFORE it looks at a single audit. Sits above BOTH engines via a
    # polymorphic slot→engagement pointer. Same CAMS.* permission codes.
    "programme": programme,
    # Waves 3-5 completion (docs/cams/09 §3.3/3.5/3.6, §2.6): suppliers, field
    # i18n, evidence packs, notification preferences. Same CAMS.* codes.
    "cams_completion": cams_completion,
    # Calendar bookings — the audit's claim on participants' Microsoft 365 time.
    # Sits above BOTH engines through the same AUDIT|INSPECTION discriminator as
    # assurance, and reuses the CAMS.* codes for the same reason it does.
    "calendar": calendar,
    # Supplier portal (WP-45 stage 2) — the ONLY unauthenticated router. A
    # vendor factory manager holds no seat, so access is a signed, expiring,
    # single-audit token instead of a session. Mounted ungated for the same
    # reason the WhatsApp webhook is: an external caller cannot present a
    # licence context, and the token itself is the authorisation.
    "supplier_portal": supplier_portal,
    "factory": factory, "factory_ext": factory_ext, "devices": devices, "plants": plants,
    "dashboard": dashboard, "licensing": licensing, "audit_log": audit_log, "jobs": jobs,
    # AI Insights engine (Stream A) — deterministic, airgap-safe insight layer
    # over the list screens. Mounted ungated (read-only, computed from records
    # the caller can already see; auth-gated via get_current_user).
    "insights": insights,
    # Shared Evidence Attachment layer (Stream B) — generic /api/evidence upload
    # for any registered entity. Mounted ungated; each endpoint re-checks the
    # entity's own read/write permission via the evidence registry.
    "attachments": attachments,
    # Fire Safety (FIRE module). Mounted always-on in dev: the unsigned dev licence
    # predates the FIRE code, so gating it via ROUTER_MODULE would 403 it. The FIRE
    # module IS registered in the licensing model (registry/editions) — add
    # "fire_safety": "FIRE" to ROUTER_MODULE once a FIRE-inclusive licence is issued.
    "fire_safety": fire_safety,
    # The Page Industries controlled checklists (PIL/EHS/CL 025-028) and the
    # Register of Fire Extinguishers. Same /api/fire prefix and the same
    # INCIDENT.READ/UPDATE gating as fire_safety — split into its own module only
    # to keep that router from growing past 1,500 lines. Adds no tables: the
    # checklists run on the CAMS engine.
    "fire_checklists": fire_checklists,
    # Guided Field Capture (CAPTURE module) — same dev-licence situation as
    # fire_safety: registered in the licensing model, mounted ungated until a
    # CAPTURE-inclusive licence is issued ("capture": "CAPTURE" in ROUTER_MODULE).
    "capture": capture,
    # Chemical / Hazmat Management. Same dev-licence situation as fire_safety and
    # capture: mounted ungated, with per-endpoint RBAC (INCIDENT.READ/UPDATE plus
    # ADMIN.MANAGE for the threshold/incompatibility masters) as the real gate.
    # Add "chemical": "CHEMICAL" to ROUTER_MODULE once a CHEMICAL-inclusive
    # licence is issued.
    "chemical": chemical,
    # Daily Alert Brief (ALERTS module) — same dev-licence situation; add
    # "alerts": "ALERTS" to ROUTER_MODULE once a licence including it is issued.
    "alerts": alerts,
    # WhatsApp-native capture (Incident Intelligence Slice 2, Feature 6) — a new
    # input adapter into the existing incident workflow. Mounted ungated; the
    # webhook is public (Meta/BSP calls it) and self-guards via sender identity.
    "whatsapp": whatsapp,
    # Module landing-page KPI bands. Spans seven differently-licensed modules,
    # so it stays ungated at the router level and each route carries its own
    # require_module guard (see app/routers/analytics_strip.py).
    "analytics_strip": analytics_strip,
    # Shared MasterItem lookup for dropdowns across every module. Ungated like
    # `plants` / `workforce` — reference data, not a module feature.
    "masters": masters,
    # IS 3786 monthly exposure return. Gated on MANHOURS like the legacy
    # flat-table router it supersedes.
    "manhours_submissions": manhours_submissions,
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Pre-warm the connection pool so the first user request doesn't pay
    # the cold TCP + TLS + auth handshake to Supabase.
    try:
        await warmup()
        log.info("DB connection warmed up")
    except Exception as e:  # noqa: BLE001
        log.warning(f"DB warmup failed (non-fatal): {e}")

    # Validate the licence on boot (offline; no network). A failure here MUST
    # NOT crash the app — it fails closed to a locked state instead.
    try:
        state = await refresh_state()
        log.info("Licence boot validation: status=%s", state.status)
    except Exception as e:  # noqa: BLE001
        log.warning("Licence boot validation failed (fails closed): %s", e)

    # Load the admin-managed allocation caches (both sit within the licence
    # ceiling): organisation-wide first, then per-factory.
    try:
        from app.licensing import org_entitlements
        await org_entitlements.refresh()
    except Exception as e:  # noqa: BLE001
        log.warning("Organisation-entitlement cache load failed: %s", e)

    try:
        from app.licensing import factory_entitlements
        await factory_entitlements.refresh()
    except Exception as e:  # noqa: BLE001
        log.warning("Factory-entitlement cache load failed: %s", e)

    recheck = asyncio.create_task(_licence_recheck_loop())

    # P2-1 background scheduler (opt-in). Single asyncio supervisor; jobs are
    # idempotent and record JobRun rows. Off by default on a shared dev DB.
    sched_stop = asyncio.Event()
    sched_task = None
    if settings.scheduler_enabled:
        from app.services.scheduler import supervisor_loop
        sched_task = asyncio.create_task(supervisor_loop(sched_stop))
        log.info("Background scheduler ENABLED")

    try:
        yield
    finally:
        recheck.cancel()
        if sched_task is not None:
            sched_stop.set()
            sched_task.cancel()
        await engine.dispose()


async def _licence_recheck_loop() -> None:
    """Periodic re-validation — catches expiry roll-over, grace transitions, and
    clock tamper between boots without needing a restart (build prompt §5.1)."""
    interval = max(60, settings.licence_recheck_seconds)
    while True:
        try:
            await asyncio.sleep(interval)
            await refresh_state()
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            log.warning("Periodic licence re-check failed (fails closed): %s", e)


def create_app() -> FastAPI:
    # Install the platform-wide soft-delete guard (P1-3): registers the governed
    # entities and arms the before_flush hard-delete blocker (import side-effect).
    from app.core.soft_delete import register_default_governed

    register_default_governed()

    # Arm the unified audit trail (P1-1): import registers the ORM capture
    # listeners; register the audited entities.
    from app.models.alerts import Alert
    from app.models.attachment import Attachment
    from app.models.audit_compliance import ComplianceAudit
    from app.models.capa import Capa
    from app.models.capture import CaptureSubmission, RcaFieldRequest
    from app.models.erm import EnterpriseRisk, RiskAssessment
    from app.models.erm_p2 import LossEvent
    from app.models.erm_t3 import Control
    from app.models.incident import Incident
    from app.models.incident_intel import GoldenThreadLink, StatutoryFormInstance, WhatsappSender
    from app.models.permit import (
        Permit,
        PermitActionEvidence,
        PermitAttachment,
        PermitCrewMember,
        PermitExtension,
        PermitGasTestReading,
        PermitIsolation,
        PermitSuspension,
    )
    from app.models.fire_safety import (
        FireAmcContract,
        FireAssetCertificate,
        FireDrill,
        FireEmergencyPlan,
        FireEquipment,
        FireZone,
        InspectionFrequencyMaster,
    )
    from app.models.rca import RcaIdentifiedCause, RcaRiskLink, RootCauseAnalysis
    from app.models.safety_culture import (
        CultureMaturityProfile,
        CultureObserverIntegrity,
        LeadershipWalk,
        PerceptionSurveyTemplate,
        RecognitionEntry,
    )
    from app.services.audit_log import register_audited

    register_audited(
        Incident, Capa, ComplianceAudit, Permit, EnterpriseRisk, RiskAssessment, LossEvent,
        Control,
        # PTW closed-loop: every safety-critical permit child table joins the
        # hash-chain — evidence rows, attachments, isolations, gas readings,
        # suspensions, extensions, and crew changes are all tamper-evident.
        PermitActionEvidence, PermitAttachment, PermitIsolation, PermitGasTestReading,
        PermitSuspension, PermitExtension, PermitCrewMember,
        FireEquipment, FireEmergencyPlan, FireDrill,
        # Fire & Life Safety: the frequency master is the reason a due date is
        # what it is, and the AMC/certificate rows are what a regulator asks to
        # see — all three are worth as much tamper-evidence as the asset itself.
        # FireZone joins because moving an asset between zones changes which
        # hot-work permits it guards.
        FireZone, InspectionFrequencyMaster, FireAmcContract, FireAssetCertificate,
        RootCauseAnalysis, RcaIdentifiedCause, RcaRiskLink,
        CaptureSubmission, RcaFieldRequest, Alert,
        # Incident Intelligence Slice 2 — golden-thread links, generated statutory
        # forms, and WhatsApp sender identity are all audit-worthy.
        GoldenThreadLink, StatutoryFormInstance, WhatsappSender,
        # Safety Culture — score recalcs, walk logging, survey admin & recognition
        # awards write to the tamper-evident hash-chain (§Cross-cutting). The
        # integrity-review outcome is auditable too (who cleared/upheld a flag).
        CultureMaturityProfile, LeadershipWalk, PerceptionSurveyTemplate, RecognitionEntry,
        CultureObserverIntegrity,
        # Shared Evidence Attachment layer — compliance evidence is tamper-evident
        # (upload / supersede / soft-delete all write to the hash-chain).
        Attachment,
    )

    app = FastAPI(
        title="SafeOps360 — Backend",
        version="1.0.0",
        description="Python backend for the SafeOps360 EHS platform.",
        # Keep debug OFF in every environment. When Starlette runs with debug=True
        # its ServerErrorMiddleware renders the raw traceback for an unhandled 500
        # AND bypasses the custom Exception handler below — leaking a stack trace to
        # the browser. The handler already logs the full traceback server-side and
        # returns a clean JSON 500, so the debug page is both redundant and unsafe.
        debug=False,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Never leak a raw Python traceback to an API client (even in dev/debug).
    # Unhandled errors are logged server-side and returned as a clean JSON 500.
    # HTTPException keeps its own handler, so 4xx business errors are unaffected.
    from fastapi.responses import JSONResponse
    from starlette.requests import Request

    @app.exception_handler(Exception)
    async def _unhandled_error(request: Request, exc: Exception):  # noqa: ANN001
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error. The team has been notified."},
        )

    # Mount every router, attaching the module-entitlement guard to gated ones.
    # The guard is the API security boundary — a disabled module's endpoints
    # 403 regardless of the UI (build prompt §5.2, TL-01).
    for name, module in _ROUTERS.items():
        module_code = ROUTER_MODULE.get(name)
        deps = [Depends(require_module(module_code))] if module_code else []
        app.include_router(module.router, dependencies=deps)

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.app_env}

    @app.get("/api/meta/min-version", tags=["meta"])
    async def min_supported_version() -> dict[str, str]:
        """Force-update gate consumed by the mobile Bootstrapper. Returns the
        oldest app version we still allow to talk to this backend. Bump these
        to push users off a known-broken build."""
        return {"ios": "1.0.0", "android": "1.0.0"}

    return app


app = create_app()
