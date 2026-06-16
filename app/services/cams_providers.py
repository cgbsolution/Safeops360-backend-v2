"""CAMS provider layer (§1.3 / §12) — standalone-safe access to platform deps.

Every external capability CAMS consumes (ERM obligations register, Equipment
Master, Skill-Matrix competency, the Incident RCA library) is resolved here
behind a tiny interface that detects availability and degrades. The
`CAMS_STANDALONE` build flag forces the bundled implementations even when a
platform import would succeed, so the SAME codebase ships integrated and
standalone with no fork.

Rule (§1.2): external links are ENRICHMENTS — they light up when present and
never break the engine when absent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_standalone() -> bool:
    """True when the CAMS_STANDALONE build/licence flag forces bundled providers."""
    return get_settings().cams_standalone


# ── RCA method library (§4) ───────────────────────────────────────────────────
# Shipped catalogue so root-cause analysis ALWAYS works — in standalone mode the
# Incident module's RCA library is absent, so CAMS ships its own. Integrated mode
# uses the same method codes, so this catalogue is the safe always-present set.
RCA_METHOD_CATALOGUE: list[dict[str, str]] = [
    {"code": "5_WHY", "name": "5 Whys", "description": "Iterative why-questioning to the root cause."},
    {"code": "FISHBONE", "name": "Fishbone / Ishikawa", "description": "Cause-and-effect across the 6M categories."},
    {"code": "FMEA", "name": "FMEA", "description": "Failure mode and effects analysis."},
    {"code": "FTA", "name": "Fault Tree Analysis", "description": "Top-down boolean fault decomposition."},
    {"code": "BOWTIE", "name": "Bowtie", "description": "Threat → hazard → consequence barrier analysis."},
    {"code": "CAUSE_MAP", "name": "Cause Map", "description": "Visual cause-and-effect mapping."},
    {"code": "TAPROOT", "name": "TapRooT", "description": "Structured systematic root-cause method."},
]


def rca_methods() -> list[dict[str, str]]:
    """Available RCA methods. Always returns the CAMS-shipped catalogue so RCA
    works in both modes; integrated deployments share these method codes."""
    return list(RCA_METHOD_CATALOGUE)


# ── Obligations provider (§5.1) ───────────────────────────────────────────────
def _obligation_shape(o: Any, source: str) -> dict[str, Any]:
    return {
        "id": o.id,
        "obligationCode": o.obligationCode,
        "title": o.title,
        "regulatorName": getattr(o, "regulatorName", "") or "",
        "siteId": o.siteId,
        "status": o.status,
        "validUntil": o.validUntil,
        "source": source,
    }


async def list_obligations(db: AsyncSession) -> list[dict[str, Any]]:
    """Uniform obligation rows for the Compliance Tracker, from whichever register
    backs this deployment:
      integrated → ERM Phase-2 LegalObligation
      standalone → CAMS-owned CamsObligation (bundled register)
    Returns [] only when neither register has data."""
    if not is_standalone():
        try:
            from app.models.erm_p2 import LegalObligation

            objs = (
                await db.execute(select(LegalObligation).where(LegalObligation.isDeleted.is_(False)))
            ).scalars().all()
            return [_obligation_shape(o, "ERM") for o in objs]
        except Exception:
            pass  # ERM module absent → fall through to the bundled register
    from app.models.cams import CamsObligation

    objs = (
        await db.execute(select(CamsObligation).where(CamsObligation.isDeleted.is_(False)))
    ).scalars().all()
    return [_obligation_shape(o, "CAMS") for o in objs]


def obligations_source() -> str:
    """Which register the compliance tracker is reading — for diagnostics/UI."""
    if is_standalone():
        return "CAMS_BUNDLED"
    try:
        import app.models.erm_p2  # noqa: F401

        return "ERM"
    except Exception:
        return "CAMS_BUNDLED"


# ── Equipment / asset provider (§1.3, TC-16) ──────────────────────────────────
async def list_assets(db: AsyncSession, site_id: str | None = None) -> list[dict[str, Any]]:
    """Asset picklist for asset/area inspections:
      integrated → Equipment Master
      standalone → CAMS lite asset register
    Same shape either way; degrades to [] (free-text areaOrAssetRef still works)."""
    if not is_standalone():
        try:
            from app.models.equipment import Equipment

            rows = (await db.execute(select(Equipment))).scalars().all()
            out = []
            for e in rows:
                if getattr(e, "isDeleted", False):
                    continue
                if site_id and e.plantId != site_id:
                    continue
                out.append({
                    "id": e.id, "code": e.code, "name": e.name, "category": e.category,
                    "siteId": e.plantId, "location": e.location, "source": "EQUIPMENT_MASTER",
                })
            return out
        except Exception:
            pass
    from app.models.cams import CamsAssetLite

    stmt = select(CamsAssetLite).where(CamsAssetLite.isDeleted.is_(False))
    if site_id:
        stmt = stmt.where(CamsAssetLite.siteId == site_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [{
        "id": a.id, "code": a.assetCode, "name": a.name, "category": a.category,
        "siteId": a.siteId, "location": a.location, "source": "CAMS_LITE",
    } for a in rows]


# ── Skill-Matrix competency provider (§4 enrichment, graceful degrade) ────────
async def check_competencies(db: AsyncSession, user_id: str | None, required_codes: list[str]) -> list[str]:
    """Non-blocking warnings for required auditor competencies the user does not
    currently hold. Skill-Matrix-backed when present; standalone/absent → no
    warnings (the requirement degrades silently, never blocks scheduling)."""
    codes = [c for c in (required_codes or []) if c]
    if not codes or not user_id or is_standalone():
        return []
    try:
        from app.models.competency_matrix import Competency, CompetencyRecord
    except Exception:
        return []  # Skill Matrix absent → enrichment degrades

    comps = (await db.execute(select(Competency).where(Competency.code.in_(codes)))).scalars().all()
    code_by_id = {c.id: c.code for c in comps}
    defined = {c.code for c in comps}
    warnings = [f"Competency '{c}' is not defined in the Skill Matrix." for c in codes if c not in defined]

    if comps:
        recs = (
            await db.execute(
                select(CompetencyRecord)
                .where(CompetencyRecord.personUserId == user_id)
                .where(CompetencyRecord.competencyId.in_(list(code_by_id)))
            )
        ).scalars().all()
        now = _now()
        held = {
            code_by_id.get(r.competencyId)
            for r in recs
            if r.currentValidatedAt is not None and (r.validUntil is None or (_as_aware(r.validUntil) or now) >= now)
        }
        warnings += [f"Lead auditor does not hold a current '{c.code}' competency." for c in comps if c.code not in held]
    return warnings
