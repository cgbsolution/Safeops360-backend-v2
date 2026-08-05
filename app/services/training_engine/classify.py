"""Classification extraction + trigger emission for the Training Engine.

Turns an Incident / Near Miss / Observation record into the denormalised
classification blob the rule engine consumes, infers SIF-potential (there is no
dedicated SIF boolean anywhere in the schema — it is derived here), matches a
blob against a HazardToSkillMapping, and emits a TrainingTriggerEvent (the
dedicated outbox) in the caller's transaction.

The blob is written once, at trigger time, so the background resolver never has
to re-query the source module — the Training Engine integrates with Incident /
Near Miss / Observation purely through their existing classification fields, with
NO schema change to those modules (spec NFR).
"""

from __future__ import annotations

import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.training_engine import TrainingTriggerEvent

# Incident types that are inherently Serious-Injury-or-Fatality class.
_SIF_INCIDENT_TYPES = {"FATALITY", "HIPO_NEAR_MISS"}


def coerce(v) -> str | None:
    """Enum member (native_enum=False String) or plain value → its string value.
    str(SomeEnum.X) yields 'SomeEnum.X', so we must use .value/.name, not str()."""
    if v is None:
        return None
    val = getattr(v, "value", None)
    if val is not None:
        return str(val)
    name = getattr(v, "name", None)
    if name is not None:
        return str(name)
    return str(v)


# ── classification blob builders ─────────────────────────────────────────────
def _observation_light(o) -> dict:
    return {
        "module": "OBSERVATION",
        "category": coerce(o.category),
        "type": coerce(o.type),
        "severity": coerce(o.severity),
        "plantId": o.plantId,
        "departmentId": None,
        "textBlob": " ".join(
            filter(
                None,
                [
                    getattr(o, "description", None),
                    getattr(o, "behaviourObserved", None),
                    getattr(o, "immediateAction", None),
                ],
            )
        ).lower(),
    }


def _nearmiss_light(nm) -> dict:
    pc = nm.potentialConsequences or []
    fatality = any(
        isinstance(x, dict) and str(x.get("subRating", "")).upper() == "FATALITY_POTENTIAL"
        for x in pc
    )
    return {
        "module": "NEAR_MISS",
        "hazardCategory": nm.hazardCategory,
        "initialRootCauseCategory": nm.initialRootCauseCategory,
        "rootCauseCategory": nm.rootCauseCategory,
        "severity": coerce(nm.potentialSeverity) or nm.riskLevel,
        "type": None,
        "plantId": nm.plantId,
        "departmentId": nm.departmentId,
        "fatalityPotential": fatality,
        "textBlob": " ".join(
            filter(
                None,
                [
                    getattr(nm, "correctiveActions", None),
                    getattr(nm, "description", None),
                    nm.rootCauseCategory,
                    nm.initialRootCauseCategory,
                ],
            )
        ).lower(),
    }


def _incident_light(i) -> dict:
    return {
        "module": "INCIDENT",
        "type": coerce(i.type),
        "severity": i.severity,  # already a plain string on Incident
        "plantId": i.plantId,
        "departmentId": i.departmentId,
        "rootCauseCategory": None,  # no single column; keyword path covers it
        "textBlob": " ".join(
            filter(
                None,
                [
                    getattr(i, "rootCauseSummary", None),
                    getattr(i, "rootCauseDetail", None),
                    getattr(i, "correctiveActions", None),
                    getattr(i, "preventiveActions", None),
                ],
            )
        ).lower(),
    }


def build_classification_light(module: str, record) -> dict:
    """Classification WITHOUT the involved-worker resolution (no person queries)
    — used for the threshold count over many historical records."""
    if module == "OBSERVATION":
        return _observation_light(record)
    if module == "NEAR_MISS":
        return _nearmiss_light(record)
    if module == "INCIDENT":
        return _incident_light(record)
    return {"module": module, "plantId": getattr(record, "plantId", None), "textBlob": ""}


async def build_classification(db: AsyncSession, module: str, record) -> dict:
    """Full blob including involvedUserIds + injury/fatality signals + sifPotential.
    Used at trigger-emit time (the workers are needed for the severity rule)."""
    cls = build_classification_light(module, record)
    involved: list[str] = []

    if module == "INCIDENT":
        from app.models.incident import IncidentPerson

        persons = (
            await db.execute(select(IncidentPerson).where(IncidentPerson.incidentId == record.id))
        ).scalars().all()
        involved = [p.userId for p in persons if p.userId]
        cls["injuryFatal"] = any((p.injurySeverity or "").upper() == "FATAL" for p in persons)

    elif module == "NEAR_MISS":
        from app.models.near_miss_children import NearMissPersonAffected, NearMissPersonInvolved

        inv = (
            await db.execute(
                select(NearMissPersonInvolved).where(NearMissPersonInvolved.nearMissId == record.id)
            )
        ).scalars().all()
        aff = (
            await db.execute(
                select(NearMissPersonAffected).where(NearMissPersonAffected.nearMissId == record.id)
            )
        ).scalars().all()
        involved = list({p.userId for p in [*inv, *aff] if p.userId})

    elif module == "OBSERVATION":
        # Observations gained a person-involved child table with the SLA /
        # deroster work (models/observation_sla.py). Only the USER rows can be
        # returned here: TrainingAssignment.personUserId is a User FK and
        # ContractorWorker has no user account, so a contractor's id would
        # create an assignment pointing at nothing. Contractor corrective
        # actions are gated on their EPC competency record instead — see
        # services/observation_deroster.corrective_action_state.
        #
        # An observation with only contractor workers named therefore still
        # reaches severity_rule with an empty list, which flags it to the HSE
        # Manager for manual review — the correct outcome, not a silent drop.
        from app.models.observation_sla import PARTY_USER, ObservationWorkerInvolved

        rows = (
            await db.execute(
                select(ObservationWorkerInvolved).where(
                    ObservationWorkerInvolved.observationId == record.id
                )
            )
        ).scalars().all()
        involved = [w.userId for w in rows if w.partyType == PARTY_USER and w.userId]
        cls["contractorWorkerIds"] = [
            w.contractorWorkerId for w in rows if w.contractorWorkerId
        ]

    cls["involvedUserIds"] = involved
    cls["sifPotential"] = infer_sif(module, cls)
    return cls


def infer_sif(module: str, cls: dict) -> bool:
    """Derive SIF-potential — there is no stored flag. CRITICAL severity, a
    fatality-class incident type, a recorded fatal injury, or a near-miss with
    FATALITY_POTENTIAL subrating all qualify."""
    if (cls.get("severity") or "").upper() == "CRITICAL":
        return True
    if module == "INCIDENT":
        if coerce(cls.get("type")) in _SIF_INCIDENT_TYPES:
            return True
        if cls.get("injuryFatal"):
            return True
    if module == "NEAR_MISS" and cls.get("fatalityPotential"):
        return True
    return False


# ── mapping matcher (used by resolver.resolve_competencies + count) ──────────
def mapping_matches(field: str, value: str, match_mode: str, cls: dict) -> bool:
    if not value:
        return False
    v = value.strip().lower()
    if field == "keyword" or match_mode == "keyword":
        blob = cls.get("textBlob") or ""
        extra = " ".join(
            str(cls.get(k) or "")
            for k in ("category", "hazardCategory", "initialRootCauseCategory", "rootCauseCategory", "type")
        )
        return v in f"{blob} {extra}".lower()
    actual = coerce(cls.get(field))
    if actual is None:
        return False
    return actual.strip().lower() == v


# ── emit (dedicated outbox, in the caller's transaction) ─────────────────────
async def emit_training_trigger(
    db: AsyncSession,
    module: str,
    record,
    *,
    event_type: str = "classification_saved",
) -> TrainingTriggerEvent | None:
    """Stage a TrainingTriggerEvent in the caller's session (commits atomically
    with the record). Best-effort: a failure to build the blob must never break
    the incident/near-miss/observation write."""
    try:
        cls = await build_classification(db, module, record)
    except Exception as e:  # noqa: BLE001
        print(f"[training_engine] classify failed for {module} {getattr(record,'id',None)}: {e}", file=sys.stderr)
        return None
    ev = TrainingTriggerEvent(
        plantId=cls.get("plantId"),
        sourceModule=module,
        sourceRecordId=record.id,
        sourceRecordRef=getattr(record, "number", None),
        eventType=event_type,
        classification=cls,
    )
    db.add(ev)
    return ev


__all__ = [
    "coerce",
    "build_classification",
    "build_classification_light",
    "infer_sif",
    "mapping_matches",
    "emit_training_trigger",
]
