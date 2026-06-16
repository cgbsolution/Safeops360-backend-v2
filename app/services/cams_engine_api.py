"""CAMS shared-service interfaces (§8) — the platform-wide API consumer modules
call INSTEAD of re-implementing auditing.

Fire Safety, PPE, Pharma IMS, EPC import these and pass `source_module`, so the
audit → finding → CAPA → analytics → compliance loop runs on ONE engine with
provenance preserved. Each class is a thin, documented facade over the engine in
`app.services.cams`; consumers never keep their own audit/findings logic, they
call here and hold only the returned CAMS engagement/finding reference.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.cams import CamsEngagement, CamsFinding, CamsTemplate, CamsTemplateSection
from app.services import cams as svc
from app.services import cams_providers as providers


class AuditEngineService:
    """create / schedule / execute / close engagements (caller sets source_module)."""

    create_engagement = staticmethod(svc.create_consumer_engagement)

    @staticmethod
    async def list_for_source(db: AsyncSession, source_module: str) -> list[CamsEngagement]:
        return (
            await db.execute(
                select(CamsEngagement)
                .where(CamsEngagement.sourceModule == source_module)
                .where(CamsEngagement.isDeleted.is_(False))
            )
        ).scalars().all()


class TemplateEngineService:
    """fetch approved templates by type/standard; render & score checklists."""

    score_checklist = staticmethod(svc.compute_score)

    @staticmethod
    async def approved_templates(db: AsyncSession, *, engagement_type: str | None = None, standard: str | None = None) -> list[CamsTemplate]:
        rows = (
            await db.execute(
                select(CamsTemplate)
                .where(CamsTemplate.status == "APPROVED")
                .where(CamsTemplate.isDeleted.is_(False))
                .options(selectinload(CamsTemplate.sections).selectinload(CamsTemplateSection.questions))
            )
        ).scalars().all()

        def keep(t: CamsTemplate) -> bool:
            if engagement_type and engagement_type not in (t.applicableEngagementTypes or []):
                return False
            if standard and standard not in (t.standardRefs or []):
                return False
            return True

        return [t for t in rows if keep(t)]


class FindingsService:
    """create / list / close findings; raise CAPA (AUDIT source)."""

    raise_capa = staticmethod(svc.raise_capa_for_finding)
    sync_from_answers = staticmethod(svc.sync_findings_from_answers)

    @staticmethod
    async def list_for_engagement(db: AsyncSession, engagement_id: str) -> list[CamsFinding]:
        return (
            await db.execute(
                select(CamsFinding)
                .where(CamsFinding.engagementId == engagement_id)
                .where(CamsFinding.isDeleted.is_(False))
            )
        ).scalars().all()


class AnalyticsService:
    """push engagement/finding events; pull metrics."""

    metrics = staticmethod(svc.compute_analytics)
    recompute_repeats = staticmethod(svc.detect_repeat_findings)
    snapshot = staticmethod(svc.precompute_snapshot)


class ComplianceService:
    """obligations read/attest/verify; audit-compliance links."""

    obligations = staticmethod(providers.list_obligations)
    coverage = staticmethod(svc.compute_compliance)


__all__ = [
    "AuditEngineService",
    "TemplateEngineService",
    "FindingsService",
    "AnalyticsService",
    "ComplianceService",
]
