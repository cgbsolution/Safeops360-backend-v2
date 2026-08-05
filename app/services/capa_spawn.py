"""Shared CAPA auto-spawn helper (P2-3 MOC, P3-2 Kaizen, …).

Mirrors erm_t3._create_capa but lives in the service layer so any module can
auto-raise a CAPA on the universal engine without duplicating the ~25-field
construction. Idempotent helpers check for an existing CAPA on the same source ref.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capa import Capa, CapaSourceCategory, CapaSourceType
from app.models.plant import Plant


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def next_capa_number(db: AsyncSession, *, prefix: str, plant_id: str, category_id: str) -> str:
    """Next CAPA number for a (plant, category) sequence = highest existing suffix + 1.

    Scans ALL rows for this prefix INCLUDING soft-deleted ones (the unique key on
    capaNumber covers soft-deleted rows too), so it never collides. The old
    `count(rows)+1` broke whenever any CAPA was deleted — the live count no longer
    matched the highest number, so it re-issued an existing number and 500'd on
    the duplicate-key constraint.
    """
    existing = (
        await db.execute(
            select(Capa.capaNumber)
            .where(Capa.plantId == plant_id)
            .where(Capa.sourceCategoryId == category_id)
            .where(Capa.capaNumber.like(prefix + "%"))
            .execution_options(include_deleted=True)
        )
    ).scalars().all()
    used = {int(n.rsplit("-", 1)[-1]) for n in existing if n.rsplit("-", 1)[-1].isdigit()}
    return f"{prefix}{(max(used) + 1) if used else 1:03d}"


async def spawn_capa(
    db: AsyncSession, *, source_code: str, plant_id: str | None, title: str, problem: str,
    ref_id: str, ref_url: str | None = None, ref_summary: str | None = None,
    metadata: dict | None = None, severity: str = "MODERATE", priority: str = "HIGH",
    detected_method: str, owner_id: str | None, actor_id: str | None,
    due_days: int = 90,
) -> Capa | None:
    """Create one CAPA on the universal engine. Returns the Capa (caller commits).
    Raises ValueError if the source type isn't seeded."""
    st = (await db.execute(select(CapaSourceType).where(CapaSourceType.code == source_code))).scalar_one_or_none()
    if st is None:
        raise ValueError(f"{source_code} CAPA source type not seeded.")
    cat = await db.get(CapaSourceCategory, st.categoryId)
    plant = (await db.get(Plant, plant_id)) if plant_id else None
    if plant is None:
        plant = (await db.execute(select(Plant).order_by(Plant.code).limit(1))).scalar_one_or_none()
    if plant is None:
        raise ValueError("No plant available to scope the CAPA.")
    capa_number = await next_capa_number(
        db, prefix=f"CAPA-{cat.prefix if cat else source_code[:3]}-{_now().year}-{plant.code}-",
        plant_id=plant.id, category_id=st.categoryId,
    )
    capa = Capa(
        capaNumber=capa_number,
        title=title[:200], plantId=plant.id, sourceCategoryId=st.categoryId, sourceTypeId=st.id, sourceTypeCode=source_code,
        sourceReferenceId=ref_id, sourceReferenceUrl=ref_url, sourceReferenceSummary=ref_summary, sourceMetadata=metadata or {},
        problemDescription=problem, detectionMethod=detected_method, detectedAt=_now(), detectedByUserId=actor_id,
        primaryCategory=cat.name if cat else source_code, severity=severity, priority=priority, state="ACTIONS_PLANNED",
        stateChangedAt=_now(), closureTargetDate=_now() + timedelta(days=due_days), raisedByUserId=actor_id,
        primaryOwnerUserId=owner_id, createdByUserId=actor_id,
    )
    db.add(capa)
    return capa


async def existing_capas_for(db: AsyncSession, source_code: str, ref_id: str) -> list[Capa]:
    return (
        await db.execute(select(Capa).where(Capa.sourceTypeCode == source_code).where(Capa.sourceReferenceId == ref_id))
    ).scalars().all()


# FindingSeverity → (CAPA severity, closure-target days). Aligns with the seeded
# INSPECTION_FINDING SLA intent (default 60d) but tightens for high-severity.
_FINDING_SEVERITY_MAP = {
    "CRITICAL": ("CRITICAL", 30),
    "HIGH": ("HIGH", 45),
    "MEDIUM": ("MODERATE", 60),
    "LOW": ("LOW", 90),
}


async def spawn_inspection_finding_capa(
    db: AsyncSession, finding, plant_id: str | None, actor_id: str | None
) -> dict[str, Any]:
    """Promote an inspection finding into the UNIVERSAL Capa register
    (source INSPECTION_FINDING) so it gets SLA, escalation, audit-chain and the
    unified dashboards — bridging the Node-side per-finding CAPA into the
    platform CAPA engine. Idempotent: one universal CAPA per finding."""
    existing = await existing_capas_for(db, "INSPECTION_FINDING", finding.id)
    if existing:
        return {
            "created": 0,
            "capaIds": [c.id for c in existing],
            "capaNumbers": [c.capaNumber for c in existing],
            "skipped": "already exists",
        }
    sev, due = _FINDING_SEVERITY_MAP.get((finding.severity or "MEDIUM").upper(), ("MODERATE", 60))
    capa = await spawn_capa(
        db, source_code="INSPECTION_FINDING", plant_id=plant_id,
        title=f"Inspection finding: {finding.title}",
        problem=f"Finding {finding.findingNumber}: {(finding.description or '')[:500]}",
        ref_id=finding.id, ref_url=f"/inspections/findings/{finding.id}",
        ref_summary=f"{finding.findingNumber} — {finding.title}",
        metadata={"findingNumber": finding.findingNumber, "inspectionId": finding.inspectionId,
                  "findingSeverity": finding.severity, "isCritical": finding.isCritical},
        severity=sev, detected_method="INSPECTION_FINDING", owner_id=finding.ownerId,
        actor_id=actor_id, due_days=due,
    )
    return {"created": 1, "capaIds": [capa.id], "capaNumbers": [capa.capaNumber]}


async def spawn_moc_capas(db: AsyncSession, cr, actor_id: str | None) -> dict[str, Any]:
    """I-18 — on MOC approval, spawn an implementation-tracking CAPA (MOC_ACTION).
    Idempotent: skips if a MOC CAPA already exists for this change request."""
    existing = await existing_capas_for(db, "MOC_ACTION", cr.id)
    if existing:
        return {"created": 0, "capaIds": [c.id for c in existing], "skipped": "already exists"}
    sev = "HIGH" if (cr.classification or "").lower() in ("major", "critical", "high") else "MODERATE"
    due = None
    if getattr(cr, "targetCompletionDate", None):
        delta = (cr.targetCompletionDate.replace(tzinfo=timezone.utc) if cr.targetCompletionDate.tzinfo is None else cr.targetCompletionDate) - _now()
        due = max(7, delta.days)
    capa = await spawn_capa(
        db, source_code="MOC_ACTION", plant_id=cr.plantId,
        title=f"Implement change: {cr.title}", problem=f"MOC {cr.number}: {cr.description[:500]}",
        ref_id=cr.id, ref_url=f"/moc/{cr.id}", ref_summary=f"{cr.number} — {cr.title}",
        metadata={"mocNumber": cr.number, "isTemporary": cr.isTemporary},
        severity=sev, detected_method="MOC_APPROVAL", owner_id=cr.initiatedByUserId, actor_id=actor_id,
        due_days=due or 30,
    )
    return {"created": 1, "capaIds": [capa.id]}
