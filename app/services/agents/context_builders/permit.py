"""Rich context builder for PTW-module agent invocations.

Assembles the input the PermitRiskReviewerAgent needs to do multi-signal
reasoning over a permit submission. Mirrors the three-block shape its
prompt expects:

  • permitReviewRequest — the full permit submission (identity, scope,
    location, validity window, crew with cert/medical/contractor flags,
    isolations, gas test plan, PPE checklist, fire watch + rescue flags)
  • rulesFindings — deterministic rules-engine findings already produced
    for this permit. SafeOps360 does not yet ship a PTW rules engine, so
    this is an empty array — the prompt is written to tolerate that.
  • context — orchestrator-fetched supporting data:
      - activePermitsInRadius (same plant, overlapping validity)
      - recentFindingsInArea (recent CRITICAL / HIGH observations in
        the same area in the last 30 days)
      - pastIncidentsSimilarWork (closed incidents in the last 24 months
        whose activePermitId pointed at a permit of the same type or
        whose activityBeingPerformed matches keywords from this permit's
        scope)

Returned values are JSON-serialisable. Long text fields are previewed to
~300 chars; the agent's tools can pull more detail if it needs it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.incident import Incident, IncidentStatus
from app.models.observation import Observation, ObservationStatus, Severity
from app.models.permit import (
    Permit,
    PermitCrewMember,
    PermitGasTestPlan,
    PermitIsolation,
    PermitStatus,
    PermitSubjectEquipment,
    PermitToolEquipment,
)
from app.models.plant import Area, Plant
from app.models.user import User


# Permit statuses that count as "alive and overlapping" for SIMOPS purposes.
# We include the approval states because an approved-but-not-yet-active
# permit is in the queue and the Issuer needs to be aware of it.
_LIVE_STATUSES: tuple[PermitStatus, ...] = (
    PermitStatus.ISSUER_APPROVED,
    PermitStatus.SAFETY_APPROVED,
    PermitStatus.PLANT_HEAD_APPROVED,
    PermitStatus.ACTIVE,
    PermitStatus.SUSPENDED,
)


async def build_context(db: AsyncSession, permit_id: str) -> dict[str, Any]:
    """Assemble the rich review context for a permit submission."""
    stmt = (
        select(Permit)
        .where(Permit.id == permit_id)
        .options(
            selectinload(Permit.workCrew),
            selectinload(Permit.isolations),
            selectinload(Permit.toolsEquipment),
            selectinload(Permit.subjectEquipment),
            selectinload(Permit.gasTestPlan),
        )
    )
    permit = (await db.execute(stmt)).scalar_one_or_none()
    if permit is None:
        raise ValueError(f"Permit {permit_id!r} not found")

    plant = await db.get(Plant, permit.plantId) if permit.plantId else None
    area = await db.get(Area, permit.areaId) if permit.areaId else None
    originator = (
        await db.get(User, permit.originatorId) if permit.originatorId else None
    )

    # ── Crew roster: resolve names from User table in one batch ──────
    crew_user_ids = [c.userId for c in permit.workCrew if c.userId]
    crew_users: dict[str, User] = {}
    if crew_user_ids:
        rows = (
            (await db.execute(select(User).where(User.id.in_(crew_user_ids))))
            .scalars()
            .all()
        )
        crew_users = {u.id: u for u in rows}

    permit_review_request = {
        "permitId": permit.id,
        "permitNumber": permit.number,
        "type": _enum(permit.type),
        "status": _enum(permit.status),
        "scopeOfWork": permit.scopeOfWork or "",
        "scopeOfWorkLength": len(permit.scopeOfWork or ""),
        "where": {
            "plantId": permit.plantId,
            "plantName": plant.name if plant else None,
            "plantLocation": plant.location if plant else None,
            "plantState": plant.state if plant else None,
            "plantUnitType": plant.unitType if plant else None,
            "areaId": permit.areaId,
            "areaName": area.name if area else None,
            "location": permit.location,
            "specificLocation": permit.specificLocation,
            "gpsLatitude": permit.gpsLatitude,
            "gpsLongitude": permit.gpsLongitude,
        },
        "validity": {
            "validFrom": _iso(permit.validFrom),
            "validTo": _iso(permit.validTo),
            "validityHours": permit.validityHours,
        },
        "originator": {
            "userId": originator.id if originator else None,
            "name": originator.name if originator else None,
            "role": originator.role if originator else None,
            "department": originator.department if originator else None,
        },
        "contractorName": permit.contractorName,
        "workOrderNumber": permit.workOrderNumber,
        "environment": {
            "weatherConditionsAtIssue": permit.weatherConditionsAtIssue,
            "windSpeedKmh": permit.windSpeedKmh,
        },
        "isolations": [
            {
                "isolationType": iso.isolationType,
                "description": iso.description,
                "isolationPointTag": iso.isolationPointTag,
                "lotoTagNumber": iso.lotoTagNumber,
                "verifiedAt": _iso(iso.isolationVerifiedAt),
            }
            for iso in permit.isolations
        ],
        "isolationsLegacyText": (permit.isolationsRequired or "")[:200]
        if permit.isolationsRequired
        else None,
        "ppeChecklist": (permit.ppeChecklist or "")[:400]
        if permit.ppeChecklist
        else None,
        "gasTest": {
            "required": permit.gasTestRequired,
            "currentResult": permit.gasTestResult,
            "o2Level": permit.o2Level,
            "lelLevel": permit.lelLevel,
            "h2sLevel": permit.h2sLevel,
            "plan": (
                {
                    "refreshFrequencyMinutes": permit.gasTestPlan.refreshFrequencyMinutes,
                    "parametersToTest": permit.gasTestPlan.parametersToTest,
                    "instrumentSerial": permit.gasTestPlan.instrumentSerial,
                    "instrumentLastCalibrated": _iso(
                        permit.gasTestPlan.instrumentLastCalibrated
                    ),
                }
                if permit.gasTestPlan
                else None
            ),
        },
        "fireWatch": {
            "required": permit.fireWatchRequired,
            "fireWatchPersonId": permit.fireWatchPersonId,
            "standbyPersonId": permit.standbyPersonId,
        },
        "rescuePlan": (permit.rescuePlan or "")[:400] if permit.rescuePlan else None,
        "workCrew": [
            _crew_member_payload(c, crew_users.get(c.userId)) for c in permit.workCrew
        ],
        "subjectEquipment": [
            {
                "equipmentId": se.equipmentId,
                "workNature": se.workNature,
            }
            for se in permit.subjectEquipment
        ],
        "toolsEquipment": [
            {
                "equipmentId": te.equipmentId,
                "freeTextDescription": te.freeTextDescription,
                "inspectionCurrentAtIssuance": te.inspectionCurrentAtIssuance,
            }
            for te in permit.toolsEquipment
        ],
        "adjacentAreaNotifications": permit.adjacentAreaNotifications,
        "attachedDrawingIds": permit.attachedDrawingIds or [],
    }

    # ── Rules findings: none today — SafeOps has no deterministic PTW
    # rules engine yet. The agent's prompt tolerates an empty array.
    rules_findings: list[dict[str, Any]] = []

    # ── Context blocks ───────────────────────────────────────────────
    active_permits_in_radius = await _active_permits_in_radius(db, permit)
    recent_findings_in_area = await _recent_findings_in_area(db, permit)
    past_incidents_similar_work = await _past_incidents_similar_work(db, permit)

    return {
        "sourceModule": "PTW",
        "permitReviewRequest": permit_review_request,
        "rulesFindings": rules_findings,
        "context": {
            "activePermitsInRadius": active_permits_in_radius,
            "recentFindingsInArea": recent_findings_in_area,
            "pastIncidentsSimilarWork": past_incidents_similar_work,
        },
    }


def _crew_member_payload(
    crew: PermitCrewMember, user: User | None
) -> dict[str, Any]:
    return {
        "userId": crew.userId,
        "name": user.name if user else None,
        "role": crew.role,
        "userDesignation": user.designation if user else None,
        "userDepartment": user.department if user else None,
        "trainingValidAtIssuance": crew.trainingValidAtIssuance,
        "trainingValidationNotes": crew.trainingValidationNotes,
        "medicalValidAtIssuance": crew.medicalValidAtIssuance,
        "contractorActiveAtIssuance": crew.contractorActiveAtIssuance,
        "removedAt": _iso(crew.removedAt),
    }


async def _active_permits_in_radius(
    db: AsyncSession, permit: Permit
) -> list[dict[str, Any]]:
    """Return permits at the same plant whose validity window overlaps
    this permit's, in a status that counts as 'in flight'.

    SafeOps' radius is plant-scoped — we don't have reliable area
    polygons. Plant + same area = highest signal; plant + different area
    = lower signal but still relevant for SIMOPS reasoning. We return
    both and let the agent decide; each result includes `sameArea`.
    """
    stmt = (
        select(Permit)
        .where(Permit.plantId == permit.plantId)
        .where(Permit.id != permit.id)
        .where(Permit.status.in_(list(_LIVE_STATUSES)))
        # Overlap: their start before our end AND their end after our start.
        .where(Permit.validFrom <= permit.validTo)
        .where(Permit.validTo >= permit.validFrom)
        .order_by(Permit.validFrom.asc())
        .limit(20)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "permitNumber": p.number,
            "permitId": p.id,
            "type": _enum(p.type),
            "status": _enum(p.status),
            "sameArea": (p.areaId is not None and p.areaId == permit.areaId),
            "areaId": p.areaId,
            "location": p.location,
            "scopeOfWorkPreview": (p.scopeOfWork or "")[:240],
            "validFrom": _iso(p.validFrom),
            "validTo": _iso(p.validTo),
            "contractorName": p.contractorName,
            "fireWatchRequired": p.fireWatchRequired,
            "gasTestRequired": p.gasTestRequired,
        }
        for p in rows
    ]


async def _recent_findings_in_area(
    db: AsyncSession, permit: Permit
) -> list[dict[str, Any]]:
    """Recent (last 30 days) HIGH/CRITICAL observations in the same area
    or plant. Observations are SafeOps' nearest analog to inspection
    findings — they capture unsafe acts/conditions reported by frontline
    staff and inspectors. Filter to HIGH+ severity so the agent doesn't
    drown in low-priority noise.
    """
    window_start = datetime.now(timezone.utc) - timedelta(days=30)
    stmt = (
        select(Observation)
        .where(Observation.plantId == permit.plantId)
        .where(Observation.date >= window_start)
        .where(Observation.severity.in_([Severity.HIGH, Severity.CRITICAL]))
    )
    if permit.areaId:
        # Prefer area-scoped, but fall back to plant-wide if zero rows.
        area_stmt = stmt.where(Observation.areaId == permit.areaId)
        area_rows = (
            (await db.execute(area_stmt.order_by(Observation.date.desc()).limit(15)))
            .scalars()
            .all()
        )
        if area_rows:
            return [_observation_payload(o, scope="area") for o in area_rows]

    stmt = stmt.order_by(Observation.date.desc()).limit(10)
    rows = (await db.execute(stmt)).scalars().all()
    return [_observation_payload(o, scope="plant") for o in rows]


def _observation_payload(o: Observation, *, scope: str) -> dict[str, Any]:
    return {
        "observationNumber": o.number,
        "date": _iso(o.date),
        "type": _enum(o.type),
        "category": _enum(o.category),
        "severity": _enum(o.severity),
        "status": _enum(o.status),
        "descriptionPreview": (o.description or "")[:240],
        "immediateActionPreview": (
            (o.immediateAction or "")[:200] if o.immediateAction else None
        ),
        "scope": scope,
    }


async def _past_incidents_similar_work(
    db: AsyncSession, permit: Permit
) -> list[dict[str, Any]]:
    """Closed incidents in the last 24 months at the same plant whose
    active permit (at the time) was the same type as this one. When no
    active-permit linkage exists, fall back to keyword match on the
    activity description against the current scope's first 5 distinctive
    words.

    The agent's prompt tells it not to invent incident numbers; everything
    surfaced here is real.
    """
    window_start = datetime.now(timezone.utc) - timedelta(days=730)

    # Pass 1: incidents whose activePermitId points at a permit of the
    # same type as ours. Stronger signal than keyword matching.
    same_type_stmt = (
        select(Incident)
        .join(Permit, Permit.id == Incident.activePermitId)
        .where(Incident.plantId == permit.plantId)
        .where(Incident.status == IncidentStatus.CLOSED)
        .where(Incident.date >= window_start)
        .where(Permit.type == permit.type)
        .order_by(Incident.date.desc())
        .limit(8)
    )
    same_type_rows = (await db.execute(same_type_stmt)).scalars().all()

    incidents = list(same_type_rows)

    # Pass 2: keyword fall-back if we have headroom. Extract distinctive
    # tokens from the scope; skip stop words.
    if len(incidents) < 5 and permit.scopeOfWork:
        keywords = _distinctive_tokens(permit.scopeOfWork, max_tokens=4)
        if keywords:
            keyword_clauses = [
                or_(
                    Incident.activityBeingPerformed.ilike(f"%{kw}%"),
                    Incident.description.ilike(f"%{kw}%"),
                )
                for kw in keywords
            ]
            existing_ids = {i.id for i in incidents}
            kw_stmt = (
                select(Incident)
                .where(Incident.plantId == permit.plantId)
                .where(Incident.status == IncidentStatus.CLOSED)
                .where(Incident.date >= window_start)
                .where(or_(*keyword_clauses))
                .order_by(Incident.date.desc())
                .limit(8)
            )
            kw_rows = (await db.execute(kw_stmt)).scalars().all()
            for inc in kw_rows:
                if inc.id not in existing_ids and len(incidents) < 8:
                    incidents.append(inc)

    return [
        {
            "incidentNumber": i.number,
            "incidentId": i.id,
            "type": _enum(i.type),
            "severity": i.severity,
            "occurredAt": _iso(i.occurredAt or i.date),
            "activityBeingPerformed": i.activityBeingPerformed,
            "descriptionPreview": (
                (i.initialDescription or i.description or "")[:240]
            ),
            "rootCauseSummary": (
                (i.rootCauseSummary or "")[:240] if i.rootCauseSummary else None
            ),
            "rootCauses": (i.rootCauses or [])[:5] if hasattr(i, "rootCauses") else [],
        }
        for i in incidents
    ]


# ─── Helpers ─────────────────────────────────────────────────────────


_STOP_WORDS = {
    "the", "and", "with", "for", "from", "into", "near", "this", "that",
    "work", "permit", "area", "plant", "site", "shall", "will", "must",
    "have", "been", "they", "their", "during", "while", "shall",
}


def _distinctive_tokens(text: str, *, max_tokens: int) -> list[str]:
    """Pull a few distinctive lowercase tokens out of a scope description.
    Cheap heuristic: split on non-letters, drop short words and stop
    words, keep the first `max_tokens` unique survivors. The agent can
    refine with its own tools if it needs to."""
    import re as _re  # local import keeps the module's top-of-file clean

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


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


def _enum(v: Any) -> str | None:
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)
