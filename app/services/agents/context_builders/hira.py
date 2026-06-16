"""HIRA context builder for the agent platform.

Assembles the input context for the HIRA Assistant (HIRA_ASSISTANT,
module "HIRA"). The source record is a HiraEntry; the assistant reasons
within the context this builder assembles — it has no DB tools.

This builder is what wires Layer A into the runtime. Per the v2 prompt's
runtime input contract, the agent expects:

  activity                — the activity being assessed
  team_already_added      — hazards / controls the team has entered
  layer_a_rules_findings  — deterministic rules that fired (the floor)
  context                 — similar_past_entries, area_incident_history,
                            applicable_hazard_library, applicable_regulations

Keys mirror the prompt's "Runtime input contract" comment and the field
names the prompt body references (similar_past_entries,
applicable_hazard_library, ...) so the model can ground its references —
crucially, every suggested hazard_master_id must exist in
applicable_hazard_library.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.hira import (
    HiraEntry,
    HiraEntryHazard,
    HiraHazard,
    HiraStudy,
)
from app.models.incident import Incident
from app.services.agents.hira_layer_a import evaluate_layer_a

# Upper bound on the hazard library slice we ship. The library is a master
# of modest size; we cap so the input stays within the agent's token
# budget while still guaranteeing the model has valid IDs to reference.
_HAZARD_LIBRARY_CAP = 100
_SIMILAR_ENTRY_CAP = 6
_AREA_INCIDENT_CAP = 10


def _as_str_list(value: Any) -> list[str]:
    """Coerce a JSON column (list of strings or objects) to list[str].

    Entry equipment/materials/energy columns are JSON; callers may have
    stored plain codes or richer objects. Layer A's matchers expect
    strings, so we flatten defensively rather than assume one shape.
    """
    if not value:
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            # Prefer a human/code-ish field if present, else stringify.
            out.append(
                str(item.get("code") or item.get("name") or item.get("label") or item)
            )
        else:
            out.append(str(item))
    return out


async def build_context(db: AsyncSession, record_id: str) -> dict[str, Any]:
    stmt = (
        select(HiraEntry)
        .where(HiraEntry.id == record_id)
        .options(
            selectinload(HiraEntry.study),
            selectinload(HiraEntry.area),
            selectinload(HiraEntry.hazards).selectinload(HiraEntryHazard.hazard),
            selectinload(HiraEntry.existingControls),
            selectinload(HiraEntry.recommendedControls),
            selectinload(HiraEntry.regulationRefs),
        )
    )
    entry = (await db.execute(stmt)).scalar_one_or_none()
    if entry is None:
        return {
            "sourceModule": "HIRA",
            "sourceRecordId": record_id,
            "_note": "HIRA entry not found",
        }

    study = entry.study

    equipment = _as_str_list(entry.equipmentUsed)
    materials = _as_str_list(entry.materialsUsed)
    energy_sources = _as_str_list(entry.energySourcesPresent)

    # ── Layer A — deterministic rules (the mandatory floor) ──────────────
    layer_a = await evaluate_layer_a(
        db,
        activity_description=entry.activityDescription,
        routine=entry.routine,
        frequency=entry.frequency,
        equipment_used=equipment,
        materials_used=materials,
        energy_sources=energy_sources,
        persons_employees=entry.personsEmployees,
        persons_contractors=entry.personsContractors,
        persons_visitors=entry.personsVisitors,
        persons_public=entry.personsPublic,
        plant_id=study.plantId if study else None,
        area_id=entry.areaId,
    )

    # ── CONTEXT: similar past entries (cross-study/plant grounding) ──────
    similar_stmt = (
        select(HiraEntry)
        .where(HiraEntry.id != entry.id)
        .where(HiraEntry.isCurrentVersion.is_(True))
        .options(
            selectinload(HiraEntry.hazards).selectinload(HiraEntryHazard.hazard),
            selectinload(HiraEntry.existingControls),
        )
    )
    if entry.areaId:
        # Same area is the strongest similarity signal we can cheaply use.
        similar_stmt = similar_stmt.where(HiraEntry.areaId == entry.areaId)
    else:
        # No area — keep it related by staying within the same study.
        similar_stmt = similar_stmt.where(HiraEntry.studyId == entry.studyId)
    similar_stmt = similar_stmt.order_by(HiraEntry.createdAt.desc()).limit(
        _SIMILAR_ENTRY_CAP
    )
    similar_entries = (await db.execute(similar_stmt)).scalars().all()

    # ── CONTEXT: area incident history (past 24 months) ─────────────────
    area_incidents: list[Incident] = []
    if study is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=730)
        inc_stmt = (
            select(Incident)
            .where(Incident.plantId == study.plantId)
            .where(Incident.date >= cutoff)
            .order_by(Incident.date.desc())
            .limit(_AREA_INCIDENT_CAP)
        )
        if entry.areaId:
            inc_stmt = inc_stmt.where(Incident.areaId == entry.areaId)
        area_incidents = list((await db.execute(inc_stmt)).scalars().all())

    # ── CONTEXT: applicable hazard library ──────────────────────────────
    # Provide the active library so suggested hazard_master_id values are
    # always grounded. Ranking by category keeps related hazards adjacent.
    hazard_stmt = (
        select(HiraHazard)
        .where(HiraHazard.isActive.is_(True))
        .order_by(HiraHazard.category, HiraHazard.name)
        .limit(_HAZARD_LIBRARY_CAP)
    )
    hazard_library = (await db.execute(hazard_stmt)).scalars().all()

    # ── CONTEXT: applicable regulations ─────────────────────────────────
    applicable_regulations: list[dict[str, Any]] = []
    study_regulations = (study.applicableRegulations or []) if study else []
    for reg in study_regulations:
        # study.applicableRegulations is a free-form JSON list — pass through.
        applicable_regulations.append(
            reg if isinstance(reg, dict) else {"regulation": str(reg)}
        )
    for ref in entry.regulationRefs:
        applicable_regulations.append(
            {
                "regulation": ref.regulation,
                "section": ref.section,
                "requirementSummary": ref.requirementSummary,
                "source": "entry",
            }
        )

    return {
        "sourceModule": "HIRA",
        "sourceRecordId": entry.id,
        "task": "analyze_full_entry",
        "activity": {
            "id": entry.id,
            "studyId": entry.studyId,
            "studyNumber": study.number if study else None,
            "studyTitle": study.title if study else None,
            "sequenceNumber": entry.sequenceNumber,
            "description": entry.activityDescription,
            "areaId": entry.areaId,
            "areaName": entry.area.name if entry.area else None,
            "subLocation": entry.subLocation,
            "routine": entry.routine,
            "frequency": entry.frequency,
            "typicalDurationMin": entry.typicalDurationMin,
            "equipmentUsed": equipment,
            "materialsUsed": materials,
            "energySources": energy_sources,
            "personsEmployees": entry.personsEmployees,
            "personsContractors": entry.personsContractors,
            "personsVisitors": entry.personsVisitors,
            "personsPublic": entry.personsPublic,
        },
        "team_already_added": {
            "existingEntryHazards": [
                {
                    "id": h.id,
                    "hazardMasterId": h.hazardId,
                    "code": h.hazard.code if h.hazard else None,
                    "category": h.hazard.category if h.hazard else None,
                    "name": h.hazard.name if h.hazard else None,
                    "contextualDescription": h.contextualDescription,
                }
                for h in entry.hazards
            ],
            "existingControls": [
                {
                    "id": c.id,
                    "hierarchy": c.hierarchy,
                    "description": c.description,
                    "effectiveness": c.effectiveness,
                }
                for c in entry.existingControls
            ],
            "recommendedControls": [
                {
                    "id": rc.id,
                    "hierarchy": rc.hierarchy,
                    "description": rc.description,
                    "status": rc.status,
                }
                for rc in entry.recommendedControls
            ],
            "initialRisk": {
                "likelihoodScore": entry.initialLikelihoodScore,
                "severityScore": entry.initialSeverityScore,
                "riskScore": entry.initialRiskScore,
                "riskLevel": entry.initialRiskLevel,
            },
            "residualRisk": (
                {
                    "likelihoodScore": entry.residualLikelihoodScore,
                    "severityScore": entry.residualSeverityScore,
                    "riskScore": entry.residualRiskScore,
                    "riskLevel": entry.residualRiskLevel,
                    "acceptable": entry.residualAcceptable,
                }
                if entry.residualRiskScore is not None
                else None
            ),
        },
        "layer_a_rules_findings": layer_a.to_prompt_dict(),
        "context": {
            "similar_past_entries": [
                {
                    "id": e.id,
                    "activityDescription": e.activityDescription,
                    "routine": e.routine,
                    "frequency": e.frequency,
                    "initialRiskScore": e.initialRiskScore,
                    "initialRiskLevel": e.initialRiskLevel,
                    "residualRiskScore": e.residualRiskScore,
                    "residualRiskLevel": e.residualRiskLevel,
                    "hazards": [
                        {
                            "hazardMasterId": h.hazardId,
                            "category": h.hazard.category if h.hazard else None,
                            "name": h.hazard.name if h.hazard else None,
                        }
                        for h in e.hazards
                    ],
                    "controls": [
                        {
                            "hierarchy": c.hierarchy,
                            "description": c.description,
                            "effectiveness": c.effectiveness,
                        }
                        for c in e.existingControls
                    ],
                }
                for e in similar_entries
            ],
            "area_incident_history": [
                {
                    "id": inc.id,
                    "number": inc.number,
                    "occurredAt": inc.date.isoformat() if inc.date else None,
                    "severity": inc.severity,
                    "description": inc.description,
                }
                for inc in area_incidents
            ],
            "applicable_hazard_library": [
                {
                    "hazard_master_id": h.id,
                    "code": h.code,
                    "category": h.category,
                    "name": h.name,
                    "description": h.description,
                    "energyForm": h.energyForm,
                }
                for h in hazard_library
            ],
            "applicable_regulations": applicable_regulations,
        },
    }
