"""RCA origination + domain derivation (ERM Cross-Domain RCA).

The three origination paths (all write the same RootCauseAnalysis entity):
  A — EVENT       : exposed from an Incident (incident stays system-of-record)
  B — RISK        : opened directly on an EnterpriseRisk (no incident required)
  C — LOSS_EVENT  : opened on a LossEvent (financial/compliance/cyber loss)

primaryDomain is derived from the source's RiskCategory.code (both EnterpriseRisk
and LossEvent reference the shared RiskCategory taxonomy). There is no RiskDomain
enum in the schema — this maps the 10 seeded category codes onto the 8 canonical
risk domains the cross-domain analytics aggregate over.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.erm import EnterpriseRisk, RiskCategory
from app.models.erm_p2 import LossEvent
from app.models.incident import Incident
from app.models.rca import RootCauseAnalysis
from app.services.rca import normalise_rca_method

RISK_DOMAINS = [
    "OPERATIONAL", "FINANCIAL", "COMPLIANCE", "EXTERNAL",
    "REPUTATIONAL", "CYBER", "STRATEGIC", "ESG",
]

# RiskCategory.code (seed-erm.ts) → canonical RCA risk domain.
CATEGORY_CODE_TO_DOMAIN: dict[str, str] = {
    "STR": "STRATEGIC",
    "FIN": "FINANCIAL",
    "OPS": "OPERATIONAL",
    "CMP": "COMPLIANCE",
    "REP": "REPUTATIONAL",
    "TEC": "CYBER",
    "ESG": "ESG",
    "SCM": "OPERATIONAL",   # supply-chain disruption presents operationally
    "PPL": "OPERATIONAL",   # people/talent
    "GEO": "EXTERNAL",      # geopolitical / external macro
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _domain_for_category(db: AsyncSession, category_id: str | None) -> str:
    if not category_id:
        return "OPERATIONAL"
    cat = await db.get(RiskCategory, category_id)
    if cat is None:
        return "OPERATIONAL"
    return CATEGORY_CODE_TO_DOMAIN.get(cat.code, "OPERATIONAL")


def assert_single_origin(
    origin_type: str,
    source_event_id: str | None,
    source_risk_id: str | None,
    source_loss_event_id: str | None,
) -> None:
    """Exactly one of the three origin references must be set, and it must match
    originType (RCA-T04)."""
    present = [
        ("EVENT", source_event_id),
        ("RISK", source_risk_id),
        ("LOSS_EVENT", source_loss_event_id),
    ]
    set_count = sum(1 for _, v in present if v)
    if set_count != 1:
        raise ValueError("Exactly one origin reference must be set (event XOR risk XOR loss).")
    set_type = next(t for t, v in present if v)
    if set_type != origin_type:
        raise ValueError(f"originType={origin_type} does not match the set source reference ({set_type}).")


async def next_rca_code(db: AsyncSession) -> str:
    year = _now().year
    n = (
        await db.execute(
            select(func.count())
            .select_from(RootCauseAnalysis)
            .where(RootCauseAnalysis.rcaCode.like(f"RCA-{year}-%"))
            .execution_options(include_deleted=True)
        )
    ).scalar() or 0
    return f"RCA-{year}-{n + 1:04d}"


async def derive_primary_domain(
    db: AsyncSession,
    *,
    origin_type: str,
    source_risk_id: str | None = None,
    source_loss_event_id: str | None = None,
    source_event_id: str | None = None,  # noqa: ARG001 — events are operational
) -> str:
    if origin_type == "RISK" and source_risk_id:
        risk = await db.get(EnterpriseRisk, source_risk_id)
        if risk is None:
            raise ValueError("Source risk not found.")
        return await _domain_for_category(db, risk.categoryId)
    if origin_type == "LOSS_EVENT" and source_loss_event_id:
        loss = await db.get(LossEvent, source_loss_event_id)
        if loss is None:
            raise ValueError("Source loss event not found.")
        return await _domain_for_category(db, loss.categoryId)
    # EVENT (incident / near-miss / audit finding) — operational volume driver.
    return "OPERATIONAL"


async def create_risk_rca(
    db: AsyncSession,
    *,
    source_risk_id: str,
    title: str,
    methodology: str = "FIVE_WHY",
    narrative: str | None = None,
    occurrence_date: datetime | None = None,
    actor_id: str | None = None,
) -> RootCauseAnalysis:
    """Path B — open an RCA directly on a risk (deterioration / appetite / KRI / deep-dive)."""
    risk = await db.get(EnterpriseRisk, source_risk_id)
    if risk is None:
        raise ValueError("Source risk not found.")
    domain = await _domain_for_category(db, risk.categoryId)
    rca = RootCauseAnalysis(
        rcaCode=await next_rca_code(db),
        title=title,
        originType="RISK",
        sourceRiskId=source_risk_id,
        primaryDomain=domain,
        methodology=normalise_rca_method(methodology) or "FIVE_WHY",
        status="DRAFT",
        analysisPayload={},
        narrative=narrative,
        analystId=actor_id or risk.riskOwnerId,
        occurrenceDate=occurrence_date,
        plantId=risk.plantId,
        createdBy=actor_id,
    )
    db.add(rca)
    return rca


async def create_loss_rca(
    db: AsyncSession,
    *,
    source_loss_event_id: str,
    title: str,
    methodology: str = "FIVE_WHY",
    narrative: str | None = None,
    occurrence_date: datetime | None = None,
    actor_id: str | None = None,
) -> RootCauseAnalysis:
    """Path C — open an RCA on a loss event (the cross-domain anchor)."""
    loss = await db.get(LossEvent, source_loss_event_id)
    if loss is None:
        raise ValueError("Source loss event not found.")
    domain = await _domain_for_category(db, loss.categoryId)
    rca = RootCauseAnalysis(
        rcaCode=await next_rca_code(db),
        title=title,
        originType="LOSS_EVENT",
        sourceLossEventId=source_loss_event_id,
        primaryDomain=domain,
        methodology=normalise_rca_method(methodology) or "FIVE_WHY",
        status="DRAFT",
        analysisPayload={},
        narrative=narrative,
        analystId=actor_id or "SYSTEM",
        occurrenceDate=occurrence_date or loss.eventDate,
        plantId=loss.siteId,
        createdBy=actor_id,
    )
    db.add(rca)
    return rca


async def expose_incident_rca(
    db: AsyncSession,
    incident: Incident,
    *,
    actor_id: str | None = None,
    approve: bool = False,
) -> RootCauseAnalysis:
    """Path A — expose an incident's completed RCA as a RootCauseAnalysis. The
    incident remains system-of-record; analysisPayload is a snapshot of its
    rootCauseData (no re-entry, no parallel store). Idempotent on sourceEventId."""
    existing = (
        await db.execute(
            select(RootCauseAnalysis)
            .where(RootCauseAnalysis.originType == "EVENT")
            .where(RootCauseAnalysis.sourceEventId == incident.id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one_or_none()

    method = normalise_rca_method(incident.rootCauseMethod) or "FIVE_WHY"
    payload = dict(incident.rootCauseData or {})
    title = f"RCA — Incident {incident.number}"
    occurred = incident.occurredAt or incident.date

    if existing is not None:
        existing.methodology = method
        existing.analysisPayload = payload
        existing.narrative = incident.rootCauseSummary
        existing.occurrenceDate = occurred
        existing.updatedBy = actor_id
        if approve and existing.status != "APPROVED":
            existing.status = "APPROVED"
            existing.approverId = actor_id
            existing.approvedAt = _now()
        return existing

    rca = RootCauseAnalysis(
        rcaCode=await next_rca_code(db),
        title=title,
        originType="EVENT",
        sourceEventId=incident.id,
        primaryDomain="OPERATIONAL",
        methodology=method,
        status="APPROVED" if approve else "IN_ANALYSIS",
        analysisPayload=payload,
        narrative=incident.rootCauseSummary,
        analystId=actor_id or incident.reporterId,
        approverId=actor_id if approve else None,
        approvedAt=_now() if approve else None,
        occurrenceDate=occurred,
        plantId=incident.plantId,
        createdBy=actor_id,
    )
    db.add(rca)
    return rca


__all__ = [
    "RISK_DOMAINS",
    "CATEGORY_CODE_TO_DOMAIN",
    "assert_single_origin",
    "next_rca_code",
    "derive_primary_domain",
    "create_risk_rca",
    "create_loss_rca",
    "expose_incident_rca",
]
