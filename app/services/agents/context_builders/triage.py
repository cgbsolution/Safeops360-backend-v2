"""Rich context builder for the TriageAgent (OBSERVATION + NEAR_MISS).

Two entry points keep dispatch simple — the agent_service routes by
`sourceModule`, and each builder funnels into the same shared assembly
helper.

The TriageAgent's prompt expects this top-level shape:

  {
    "triageRequest":    { ... record fields ... },
    "rulesFindings":    { ... empty stub for now ... },
    "context":          {
      "similarPastRecords":     [ ... ],
      "areaActivity30d":        { ... },
      "activePermitsInArea":    [ ... ],
      "availableCategories":    [ ... ],
      "availableActionOwnerRoles": [ ... ]
    },
    "tenantPolicy":     { ... default policy ... }
  }

Two pieces of context the prompt expects don't exist as first-class
entities in SafeOps360 yet, so we synthesise them from what we have:

  • availableCategories — derived from the ObservationCategory enum (for
    observations) or MasterItem rows of type=HAZARD_CATEGORY (for near
    misses). Each carries an `isStatutory` flag we infer from category
    code (chemical, confined space, hot work, work at height — anything
    that can trip Factories Act / MAH thresholds).
  • availableActionOwnerRoles — derived from the Role table, filtered
    to roles that can legitimately own corrective actions. `typicalLoad`
    is inferred from open-record counts; that's a coarse proxy until a
    real load-balancing service ships.

`similarPastRecords` uses keyword matching against closed records — a
proper vector search would land here in a later commit. The prompt is
written to tolerate noise in this list (it ranks the top 3 by actual
relevance and ignores the rest).

`rulesFindings` is currently a stub (no Layer A rules engine exists for
the triage path yet — only the workflow-rule engine on post-closure).
The prompt explicitly tolerates an empty stub.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import Incident, IncidentStatus
from app.models.masters import MasterItem
from app.models.near_miss import NearMiss, NearMissStatus
from app.models.observation import (
    Observation,
    ObservationCategory,
    ObservationStatus,
    ObservationType,
    Severity,
)
from app.models.permit import Permit, PermitStatus
from app.models.plant import Area, Plant
from app.models.user import Role, RolePermission, User, UserRole


# Categories on Observation.category that imply potentially statutory
# triggers. This is the conservative "could be statutory" filter — the
# TriageAgent itself decides via isStatutory based on the description.
_STATUTORY_OBS_CATEGORIES: set[str] = {
    "CHEMICAL_HANDLING",
    "CONFINED_SPACE",
    "HOT_WORK",
    "WORK_AT_HEIGHT",
    "EMERGENCY",
    "ELECTRICAL",
}


# Roles eligible to own corrective actions. Filter applied so the agent
# isn't asked to suggest e.g. "WORKER" as an owner.
_OWNER_ELIGIBLE_ROLE_CODES: tuple[str, ...] = (
    "HSE_MANAGER",
    "SAFETY_OFFICER",
    "PLANT_HEAD",
    "DEPARTMENT_HEAD",
    "MAINTENANCE_HEAD",
    "SUPERVISOR",
    "ENVIRONMENT_MANAGER",
    "CONTRACTOR_COORDINATOR",
    "LD_MANAGER",
    "TRAINER",
    "PERMIT_ISSUER",
)


# Default tenant policy until per-plant policy storage ships. The prompt
# and downstream disposition logic read these.
_DEFAULT_TENANT_POLICY: dict[str, Any] = {
    "autoTriageEnabled": True,
    "autoTriageMaxSeverity": "moderate",
    "autoTriageMinConfidence": 0.80,
    "alwaysFlagCategories": [
        "CHEMICAL_HANDLING",
        "CONFINED_SPACE",
        "HOT_WORK",
    ],
    "alwaysFlagLocations": [],
    "note": (
        "Default policy — until per-plant configuration storage ships, "
        "every plant uses the same conservative thresholds."
    ),
}


# ─── Public entry points ──────────────────────────────────────────────


async def build_observation_context(
    db: AsyncSession, observation_id: str
) -> dict[str, Any]:
    obs = await db.get(Observation, observation_id)
    if obs is None:
        raise ValueError(f"Observation {observation_id!r} not found")

    return await _assemble(
        db,
        record_type="observation",
        record=obs,
        plant_id=obs.plantId,
        area_id=obs.areaId,
        record_dt=obs.date,
    )


async def build_near_miss_context(
    db: AsyncSession, near_miss_id: str
) -> dict[str, Any]:
    nm = await db.get(NearMiss, near_miss_id)
    if nm is None:
        raise ValueError(f"NearMiss {near_miss_id!r} not found")

    return await _assemble(
        db,
        record_type="near_miss",
        record=nm,
        plant_id=nm.plantId,
        area_id=nm.areaId,
        record_dt=nm.date,
    )


# ─── Shared assembly ─────────────────────────────────────────────────


async def _assemble(
    db: AsyncSession,
    *,
    record_type: str,
    record: Observation | NearMiss,
    plant_id: str,
    area_id: str | None,
    record_dt: datetime,
) -> dict[str, Any]:
    plant = await db.get(Plant, plant_id)
    area = await db.get(Area, area_id) if area_id else None
    originator = await db.get(User, _originator_id(record))

    triage_request = _serialise_record(record_type, record, plant, area, originator)

    similar = await _similar_past_records(db, record_type, record, plant_id, record_dt)
    area_activity = await _area_activity_30d(db, plant_id, area_id, record_dt)
    active_permits = await _active_permits_in_area(db, plant_id, area_id, record_dt)
    categories = await _available_categories(db, record_type)
    roles = await _available_action_owner_roles(db)

    return {
        "sourceModule": "OBSERVATION" if record_type == "observation" else "NEAR_MISS",
        "triageRequest": triage_request,
        "rulesFindings": {
            "rulesFired": [],
            "forcedDisposition": None,
            "policyViolations": [],
            "requiredAttention": [],
            "_note": (
                "No Layer A rules engine wired up for the triage path yet. "
                "When one ships, populate forcedDisposition / requiredAttention "
                "from its output."
            ),
        },
        "context": {
            "similarPastRecords": similar,
            "areaActivity30d": area_activity,
            "activePermitsInArea": active_permits,
            "availableCategories": categories,
            "availableActionOwnerRoles": roles,
        },
        "tenantPolicy": dict(_DEFAULT_TENANT_POLICY),
    }


# ─── Record serialisation ────────────────────────────────────────────


def _originator_id(record: Observation | NearMiss) -> str:
    if isinstance(record, Observation):
        return record.observerId
    return record.reporterId


def _serialise_record(
    record_type: str,
    record: Observation | NearMiss,
    plant: Plant | None,
    area: Area | None,
    originator: User | None,
) -> dict[str, Any]:
    if isinstance(record, Observation):
        return {
            "recordType": "observation",
            "recordNumber": record.number,
            "recordId": record.id,
            "description": record.description or "",
            "descriptionLength": len(record.description or ""),
            "originatorCategory": _enum(record.category),
            "originatorType": _enum(record.type),
            "originatorSeverity": _enum(record.severity),
            "immediateAction": record.immediateAction,
            "originator": _originator_payload(originator),
            "where": _where_payload(record, plant, area),
            "when": {
                "date": _iso(record.date),
                "createdAt": _iso(record.createdAt),
            },
            "evidence": {
                "photoCount": 0,
                "photoDescriptions": [],
                "voiceTranscript": None,
                "_note": (
                    "Attachment counts are not loaded in the triage context "
                    "to keep the input lean. The reviewer can inspect "
                    "attachments separately."
                ),
            },
            "status": _enum(record.status),
        }

    # NearMiss branch
    return {
        "recordType": "near_miss",
        "recordNumber": record.number,
        "recordId": record.id,
        "description": record.description or "",
        "descriptionLength": len(record.description or ""),
        "originatorHazardCategory": record.hazardCategory,
        "originatorEnergySource": record.energySource,
        "originatorPotentialSeverity": _enum(record.potentialSeverity),
        "originatorPotentialConsequences": record.potentialConsequences,
        "originatorRiskLevel": record.riskLevel,
        "originatorRecommendedActions": record.recommendedActions,
        "immediateAction": record.immediateAction,
        "originator": _originator_payload(originator),
        "where": _where_payload(record, plant, area),
        "when": {
            "date": _iso(record.date),
            "createdAt": _iso(record.createdAt),
        },
        "activity": {
            "what": record.activityBeingPerformed or record.activity,
            "isRoutine": record.activityIsRoutine,
        },
        "controls": {
            "thatFailed": record.controlsThatFailed,
            "thatWorked": record.controlsThatWorked,
        },
        "contractorCompanyId": record.contractorCompanyId,
        "equipmentId": record.equipmentId,
        "activePermitId": record.activePermitId,
        "isAnonymous": record.isAnonymous,
        "multipleWorkersAggravator": record.multipleWorkersAggravator,
        "evidence": {
            "photoCount": 0,
            "photoDescriptions": [],
            "voiceTranscript": None,
            "_note": (
                "Attachment counts are not loaded in the triage context "
                "to keep the input lean."
            ),
        },
        "status": _enum(record.status),
    }


def _originator_payload(originator: User | None) -> dict[str, Any]:
    if originator is None:
        return {"name": None, "role": None, "department": None, "userId": None}
    return {
        "userId": originator.id,
        "name": originator.name,
        "role": originator.role,
        "department": originator.department,
        "designation": originator.designation,
    }


def _where_payload(
    record: Observation | NearMiss, plant: Plant | None, area: Area | None
) -> dict[str, Any]:
    return {
        "plantId": getattr(record, "plantId", None),
        "plantName": plant.name if plant else None,
        "plantState": plant.state if plant else None,
        "plantUnitType": plant.unitType if plant else None,
        "areaId": getattr(record, "areaId", None),
        "areaName": area.name if area else None,
        "specificLocation": getattr(record, "specificLocation", None),
        "legacyLocation": getattr(record, "location", None),
        "gpsLatitude": getattr(record, "gpsLatitude", None),
        "gpsLongitude": getattr(record, "gpsLongitude", None),
    }


# ─── Context: similar past records ───────────────────────────────────


async def _similar_past_records(
    db: AsyncSession,
    record_type: str,
    record: Observation | NearMiss,
    plant_id: str,
    record_dt: datetime,
) -> list[dict[str, Any]]:
    """Keyword-match closed records at the same plant over the last 24
    months. Returns up to 10 candidates. The agent's prompt ranks the
    top 3 by actual relevance — it tolerates noise here."""
    description = (record.description or "")[:1000]
    keywords = _distinctive_tokens(description, max_tokens=4)
    window_start = (record_dt or datetime.now(timezone.utc)) - timedelta(days=730)

    candidates: list[dict[str, Any]] = []

    # Observation matches (closed, same plant, exclude self)
    obs_stmt = (
        select(Observation)
        .where(Observation.plantId == plant_id)
        .where(Observation.status == ObservationStatus.CLOSED)
        .where(Observation.date >= window_start)
    )
    if isinstance(record, Observation):
        obs_stmt = obs_stmt.where(Observation.id != record.id)
    if keywords:
        obs_stmt = obs_stmt.where(
            or_(*[Observation.description.ilike(f"%{kw}%") for kw in keywords])
        )
    obs_stmt = obs_stmt.order_by(Observation.date.desc()).limit(5)
    for o in (await db.execute(obs_stmt)).scalars().all():
        candidates.append(
            {
                "recordId": o.number,
                "recordType": "observation",
                "date": _iso(o.date),
                "category": _enum(o.category),
                "severity": _enum(o.severity),
                "descriptionPreview": (o.description or "")[:240],
            }
        )

    # Near miss matches
    nm_stmt = (
        select(NearMiss)
        .where(NearMiss.plantId == plant_id)
        .where(NearMiss.status == NearMissStatus.CLOSED)
        .where(NearMiss.date >= window_start)
    )
    if isinstance(record, NearMiss):
        nm_stmt = nm_stmt.where(NearMiss.id != record.id)
    if keywords:
        nm_stmt = nm_stmt.where(
            or_(*[NearMiss.description.ilike(f"%{kw}%") for kw in keywords])
        )
    nm_stmt = nm_stmt.order_by(NearMiss.date.desc()).limit(3)
    for n in (await db.execute(nm_stmt)).scalars().all():
        candidates.append(
            {
                "recordId": n.number,
                "recordType": "near_miss",
                "date": _iso(n.date),
                "potentialSeverity": _enum(n.potentialSeverity),
                "hazardCategory": n.hazardCategory,
                "descriptionPreview": (n.description or "")[:240],
                "promotedToIncident": n.promotedToIncident,
            }
        )

    # Incident matches (closed only, lighter weight)
    inc_stmt = (
        select(Incident)
        .where(Incident.plantId == plant_id)
        .where(Incident.status == IncidentStatus.CLOSED)
        .where(Incident.date >= window_start)
    )
    if keywords:
        inc_stmt = inc_stmt.where(
            or_(
                *[
                    or_(
                        Incident.description.ilike(f"%{kw}%"),
                        Incident.initialDescription.ilike(f"%{kw}%"),
                    )
                    for kw in keywords
                ]
            )
        )
    inc_stmt = inc_stmt.order_by(Incident.date.desc()).limit(2)
    for i in (await db.execute(inc_stmt)).scalars().all():
        candidates.append(
            {
                "recordId": i.number,
                "recordType": "incident",
                "date": _iso(i.occurredAt or i.date),
                "type": _enum(i.type),
                "severity": i.severity,
                "rootCauseSummary": (i.rootCauseSummary or "")[:240]
                if i.rootCauseSummary
                else None,
                "descriptionPreview": (i.initialDescription or i.description or "")[:240],
            }
        )

    return candidates[:10]


# ─── Context: area activity in last 30 days ──────────────────────────


async def _area_activity_30d(
    db: AsyncSession,
    plant_id: str,
    area_id: str | None,
    record_dt: datetime,
) -> dict[str, Any]:
    anchor = record_dt or datetime.now(timezone.utc)
    window_start = anchor - timedelta(days=30)

    obs_count_stmt = select(func.count()).select_from(Observation).where(
        Observation.plantId == plant_id
    ).where(Observation.date >= window_start).where(Observation.date <= anchor)
    nm_count_stmt = select(func.count()).select_from(NearMiss).where(
        NearMiss.plantId == plant_id
    ).where(NearMiss.date >= window_start).where(NearMiss.date <= anchor)
    inc_count_stmt = select(func.count()).select_from(Incident).where(
        Incident.plantId == plant_id
    ).where(Incident.date >= window_start).where(Incident.date <= anchor)

    if area_id is not None:
        obs_count_stmt = obs_count_stmt.where(Observation.areaId == area_id)
        nm_count_stmt = nm_count_stmt.where(NearMiss.areaId == area_id)
        inc_count_stmt = inc_count_stmt.where(Incident.areaId == area_id)

    obs_count = (await db.execute(obs_count_stmt)).scalar_one()
    nm_count = (await db.execute(nm_count_stmt)).scalar_one()
    inc_count = (await db.execute(inc_count_stmt)).scalar_one()

    # Trend: compare 30-day window to the prior 30-day window. >20% up = increasing,
    # <-20% = decreasing, else stable. Coarse but useful signal.
    prior_window_start = window_start - timedelta(days=30)
    prior_obs_stmt = select(func.count()).select_from(Observation).where(
        Observation.plantId == plant_id
    ).where(Observation.date >= prior_window_start).where(Observation.date < window_start)
    if area_id is not None:
        prior_obs_stmt = prior_obs_stmt.where(Observation.areaId == area_id)
    prior_obs = (await db.execute(prior_obs_stmt)).scalar_one()
    if prior_obs == 0:
        trend = "insufficient_data" if obs_count == 0 else "increasing"
    else:
        delta = (obs_count - prior_obs) / prior_obs
        if delta > 0.2:
            trend = "increasing"
        elif delta < -0.2:
            trend = "decreasing"
        else:
            trend = "stable"

    return {
        "windowDays": 30,
        "scope": "area" if area_id else "plant",
        "observations": obs_count,
        "nearMisses": nm_count,
        "incidents": inc_count,
        "observationTrend": trend,
    }


# ─── Context: active permits in area ─────────────────────────────────


async def _active_permits_in_area(
    db: AsyncSession,
    plant_id: str,
    area_id: str | None,
    record_dt: datetime,
) -> list[dict[str, Any]]:
    anchor = record_dt or datetime.now(timezone.utc)
    active_statuses = [
        PermitStatus.ACTIVE,
        PermitStatus.SUSPENDED,
        # Closed-loop states (post-approval, pre-acceptance)
        PermitStatus.APPROVED,
        PermitStatus.ISSUED,
        # Deprecated pre-rebuild intermediates (kept for old rows)
        PermitStatus.PLANT_HEAD_APPROVED,
        PermitStatus.SAFETY_APPROVED,
        PermitStatus.ISSUER_APPROVED,
    ]
    stmt = (
        select(Permit)
        .where(Permit.plantId == plant_id)
        .where(Permit.status.in_(active_statuses))
        .where(Permit.validFrom <= anchor)
        .where(Permit.validTo >= anchor)
        .order_by(Permit.validFrom.asc())
        .limit(10)
    )
    if area_id is not None:
        stmt = stmt.where(Permit.areaId == area_id)

    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "permitNumber": p.number,
            "type": _enum(p.type),
            "status": _enum(p.status),
            "location": p.location,
            "scopeOfWorkPreview": (p.scopeOfWork or "")[:200],
            "validFrom": _iso(p.validFrom),
            "validTo": _iso(p.validTo),
            "contractorName": p.contractorName,
        }
        for p in rows
    ]


# ─── Context: available categories ───────────────────────────────────


async def _available_categories(
    db: AsyncSession, record_type: str
) -> list[dict[str, Any]]:
    """For observations, return the ObservationCategory enum as categories.
    For near misses, return MasterItem rows of type=HAZARD_CATEGORY. The
    agent's prompt picks from this list.
    """
    if record_type == "observation":
        return [
            {
                "id": cat.value,
                "name": _humanise(cat.value),
                "description": _category_description(cat.value),
                "isStatutory": cat.value in _STATUTORY_OBS_CATEGORIES,
            }
            for cat in ObservationCategory
        ]

    # Near miss: hazard categories from masters
    stmt = (
        select(MasterItem)
        .where(MasterItem.type == "HAZARD_CATEGORY")
        .where(MasterItem.active.is_(True))
        .order_by(MasterItem.sortOrder.asc(), MasterItem.label.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    if rows:
        return [
            {
                "id": m.code,
                "name": m.label,
                "description": (m.metadata_ or {}).get("description") if m.metadata_ else None,
                "isStatutory": (m.metadata_ or {}).get("isStatutory", False)
                if m.metadata_
                else False,
            }
            for m in rows
        ]

    # Fallback: synthesise from observation categories so the agent
    # never receives an empty list.
    return [
        {
            "id": cat.value,
            "name": _humanise(cat.value),
            "description": _category_description(cat.value),
            "isStatutory": cat.value in _STATUTORY_OBS_CATEGORIES,
        }
        for cat in ObservationCategory
    ]


def _category_description(code: str) -> str:
    return {
        "PPE": "Personal protective equipment — wearing, condition, or availability.",
        "HOUSEKEEPING": "General housekeeping, marking, signage, and area cleanliness.",
        "WORK_AT_HEIGHT": "Work performed above 1.8m where fall hazards apply.",
        "HOT_WORK": "Welding, cutting, grinding, or open-flame work.",
        "MOBILE_EQUIPMENT": "Forklifts, vehicles, cranes, mobile elevating work platforms.",
        "ELECTRICAL": "Electrical equipment, isolation, exposed conductors.",
        "MATERIAL_HANDLING": "Manual or mechanical material movement and storage.",
        "CONFINED_SPACE": "Confined or restricted spaces requiring entry permit.",
        "CHEMICAL_HANDLING": "Chemical storage, transfer, or potential release.",
        "EMERGENCY": "Emergency response, evacuation, or alarms.",
        "OTHER": "Catch-all — use only when no specific category fits.",
    }.get(code, "")


# ─── Context: available action-owner roles ───────────────────────────


async def _available_action_owner_roles(
    db: AsyncSession,
) -> list[dict[str, Any]]:
    stmt = (
        select(Role)
        .where(Role.code.in_(_OWNER_ELIGIBLE_ROLE_CODES))
        .where(Role.isActive.is_(True))
        .order_by(Role.sortOrder.asc(), Role.name.asc())
    )
    roles = (await db.execute(stmt)).scalars().all()

    # Coarse `typicalLoad` proxy: count of open observations + near
    # misses currently assigned across all users with this role. Mapped
    # to low / medium / high buckets.
    loads = await _role_open_loads(db)

    return [
        {
            "roleId": r.code,
            "roleName": r.name,
            "description": r.description,
            "typicalLoad": _load_bucket(loads.get(r.code, 0)),
            "_openRecordsCount": loads.get(r.code, 0),
        }
        for r in roles
    ]


async def _role_open_loads(db: AsyncSession) -> dict[str, int]:
    """Count open Observations + NearMisses keyed by `responsiblePersonId` /
    `actionOwnerId`, then roll up to role. Cheap approximation — gives
    a load indicator without standing up a dedicated assignment service.
    """
    # Resolve user → roles (via UserRole). One user may hold multiple
    # roles; we attribute the load to their primary (User.role) only.
    open_obs_stmt = (
        select(Observation.responsiblePersonId, func.count())
        .where(Observation.responsiblePersonId.is_not(None))
        .where(Observation.status.in_([
            ObservationStatus.OPEN,
            ObservationStatus.ASSIGNED,
            ObservationStatus.IN_PROGRESS,
        ]))
        .group_by(Observation.responsiblePersonId)
    )
    open_nm_stmt = (
        select(NearMiss.actionOwnerId, func.count())
        .where(NearMiss.actionOwnerId.is_not(None))
        .where(NearMiss.status.in_([
            NearMissStatus.UNDER_REVIEW,
            NearMissStatus.ACTION_ASSIGNED,
        ]))
        .group_by(NearMiss.actionOwnerId)
    )

    counts_by_user: dict[str, int] = {}
    for user_id, count in (await db.execute(open_obs_stmt)).all():
        if user_id:
            counts_by_user[user_id] = counts_by_user.get(user_id, 0) + int(count)
    for user_id, count in (await db.execute(open_nm_stmt)).all():
        if user_id:
            counts_by_user[user_id] = counts_by_user.get(user_id, 0) + int(count)

    if not counts_by_user:
        return {}

    user_role_stmt = select(User.id, User.role).where(
        User.id.in_(counts_by_user.keys())
    )
    role_loads: dict[str, int] = {}
    for user_id, role_code in (await db.execute(user_role_stmt)).all():
        if not role_code:
            continue
        role_loads[role_code] = role_loads.get(role_code, 0) + counts_by_user.get(
            user_id, 0
        )
    return role_loads


def _load_bucket(count: int) -> str:
    if count >= 20:
        return "high"
    if count >= 8:
        return "medium"
    return "low"


# ─── Helpers ─────────────────────────────────────────────────────────


_STOP_WORDS = {
    "the", "and", "with", "from", "into", "near", "this", "that", "have", "been",
    "they", "their", "during", "while", "shall", "will", "must", "about", "there",
    "where", "which", "could", "would", "should", "after", "before",
}


def _distinctive_tokens(text: str, *, max_tokens: int) -> list[str]:
    import re as _re

    tokens: list[str] = []
    seen: set[str] = set()
    for raw in _re.split(r"[^a-zA-Z]+", text):
        if len(raw) < 4:
            continue
        lower = raw.lower()
        if lower in _STOP_WORDS or lower in seen:
            continue
        seen.add(lower)
        tokens.append(lower)
        if len(tokens) >= max_tokens:
            break
    return tokens


def _humanise(code: str) -> str:
    return code.replace("_", " ").title()


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


def _enum(v: Any) -> str | None:
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)
