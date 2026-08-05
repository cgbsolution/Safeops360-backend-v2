"""Real RuleContext — the only place impact rules touch SQLAlchemy.

Everything returns plain SimpleNamespace-shaped rows (id/number/… attributes)
so rules stay decoupled from ORM classes and the test fakes stay trivial.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capa import Capa
from app.models.capture import CaptureSubmission
from app.models.permit import Permit, PermitStatus
from app.models.plant import Area
from app.models.rca import RootCauseAnalysis

OPEN_CAPA_STATES = (
    "DRAFT", "SUBMITTED", "UNDER_RCA", "ACTIONS_PLANNED", "ACTIONS_IN_PROGRESS", "PENDING_VERIFICATION",
)


class DbRuleContext:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def capas_for_source(self, source_type_code: str, ref_id: str) -> list[SimpleNamespace]:
        rows = (
            await self.db.execute(
                select(Capa)
                .where(Capa.sourceTypeCode == source_type_code)
                .where(Capa.sourceReferenceId == ref_id)
            )
        ).scalars().all()
        return [
            SimpleNamespace(
                id=c.id,
                number=c.capaNumber,
                state=c.state,
                dueAt=c.closureTargetDate,
                title=c.title,
                open=c.state in OPEN_CAPA_STATES,
            )
            for c in rows
        ]

    async def active_permits(
        self, plant_id: str, area_id: str | None = None, exclude_id: str | None = None
    ) -> list[SimpleNamespace]:
        stmt = (
            select(Permit)
            .where(Permit.plantId == plant_id)
            .where(Permit.status.in_((PermitStatus.ACTIVE, PermitStatus.SUSPENDED)))
        )
        if area_id:
            stmt = stmt.where(Permit.areaId == area_id)
        if exclude_id:
            stmt = stmt.where(Permit.id != exclude_id)
        rows = (await self.db.execute(stmt)).scalars().all()
        return [
            SimpleNamespace(
                id=p.id, number=p.number, type=str(getattr(p.type, "value", p.type)),
                areaId=p.areaId, validTo=p.validTo, status=str(getattr(p.status, "value", p.status)),
            )
            for p in rows
        ]

    async def permit(self, permit_id: str) -> SimpleNamespace | None:
        p = await self.db.get(Permit, permit_id)
        if p is None:
            return None
        return SimpleNamespace(
            id=p.id, number=p.number, type=str(getattr(p.type, "value", p.type)),
            plantId=p.plantId, areaId=p.areaId, validTo=p.validTo,
            status=str(getattr(p.status, "value", p.status)),
        )

    async def rca_origin_area(self, rca_id: str) -> tuple[str | None, str | None]:
        """(plantId, areaId) of the RCA's origin event, best-effort — the RCA
        row carries plantId; the areaId comes from the source incident when
        the origin is an EVENT."""
        rca = await self.db.get(RootCauseAnalysis, rca_id)
        if rca is None:
            return None, None
        plant_id = rca.plantId
        area_id = None
        if rca.sourceEventId:
            from app.models.incident import Incident

            incident = await self.db.get(Incident, rca.sourceEventId)
            if incident is not None:
                plant_id = plant_id or incident.plantId
                area_id = incident.areaId
        return plant_id, area_id

    async def area_name(self, area_id: str | None) -> str | None:
        if not area_id:
            return None
        area = await self.db.get(Area, area_id)
        return area.name if area else None

    async def count_high_submissions(
        self, plant_id: str, area_id: str | None, category_l1_code: str | None, days: int
    ) -> int:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(func.count())
            .select_from(CaptureSubmission)
            .where(CaptureSubmission.plantId == plant_id)
            .where(CaptureSubmission.isDeleted.is_(False))
            .where(CaptureSubmission.riskLevel.in_(("HIGH", "CRITICAL")))
            .where(CaptureSubmission.createdAt >= since)
        )
        if area_id:
            stmt = stmt.where(CaptureSubmission.areaId == area_id)
        if category_l1_code:
            # categorySnapshot->l1->code
            stmt = stmt.where(CaptureSubmission.categorySnapshot["l1"]["code"].as_string() == category_l1_code)
        return (await self.db.execute(stmt)).scalar_one()

    async def permits_citing_hira_control(self, plant_id: str | None, control_name: str) -> list[SimpleNamespace]:
        """No first-class PTW→HIRA-control FK exists (DECISIONS.md D8): the
        honest impact set is active permits at the control's plant whose type
        is influenced by the HIRA entry — resolved upstream and passed via the
        event payload where available; here we fall back to plant-wide actives."""
        if not plant_id:
            return []
        return await self.active_permits(plant_id)
