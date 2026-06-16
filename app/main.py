"""FastAPI app factory.

`uvicorn app.main:app --reload` to run in development.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.db import engine, warmup
from app.routers import (
    agents,
    agents_config,
    anomalies,
    audit_compliance,
    auth,
    cams,
    capa,
    competency,
    dashboard,
    devices,
    eai,
    erm,
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
    plants,
    flra,
    hira,
    incidents,
    inspections,
    kaizen,
    manhours,
    moc,
    near_miss,
    observations,
    ppe,
    ptw,
    ptw_active,
    risk_dashboard,
    risk_register,
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Pre-warm the connection pool so the first user request doesn't pay
    # the cold TCP + TLS + auth handshake to Supabase.
    try:
        await warmup()
        log.info("DB connection warmed up")
    except Exception as e:  # noqa: BLE001
        log.warning(f"DB warmup failed (non-fatal): {e}")
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="SafeOps360 — Backend",
        version="1.0.0",
        description="Python backend for the SafeOps360 EHS platform.",
        debug=not settings.is_production,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Order matters only for OpenAPI grouping.
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(observations.router)
    app.include_router(near_miss.router)
    app.include_router(ptw.router)
    app.include_router(ptw_active.router)
    app.include_router(flra.router)
    app.include_router(incidents.router)
    app.include_router(training.router)
    app.include_router(inspections.router)
    app.include_router(manhours.router)
    app.include_router(workflow.router)
    app.include_router(workflow_definitions.router)
    app.include_router(anomalies.router)
    app.include_router(agents.router)
    app.include_router(agents_config.router)
    app.include_router(hira.router)
    app.include_router(capa.router)
    app.include_router(eai.router)
    app.include_router(erm.router)
    app.include_router(erm_p2.router)
    app.include_router(erm_p3.router)
    app.include_router(erm_t3.router)
    app.include_router(competency.router)
    app.include_router(moc.router)
    app.include_router(risk_register.router)
    app.include_router(risk_dashboard.router)
    app.include_router(scr.router)
    app.include_router(sci.router)
    app.include_router(kaizen.router)
    app.include_router(ppe.router)
    # EPC — Engineering, Procurement & Construction module
    app.include_router(epc_sites.router)
    app.include_router(epc_contractors.router)
    app.include_router(epc_workers.router)
    app.include_router(epc_mobilization.router)
    app.include_router(epc_gate.router)
    app.include_router(epc_induction.router)
    app.include_router(epc_dashboard.router)
    app.include_router(audit_compliance.router)
    app.include_router(cams.router)
    app.include_router(devices.router)
    app.include_router(plants.router)
    app.include_router(dashboard.router)

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
