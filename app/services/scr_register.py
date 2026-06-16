"""SCR register engine — all registers feedable from existing modules.

"Zero manual entry": registers are populated FROM source modules. Each register
has a definition + a mapper that turns a source record into the register's
prescribed columns. A generic idempotent upsert (one entry per source
transaction) keeps an immutable audit trail. `sync_all()` back-fills every
register for a plant; real-time event hooks can call the same per-source
functions later.

Sources wired: Incident, PTW (Permit), Inspection (+Equipment), Training
(TrainingCertificate), CAPA. Registers needing HR / Occupational-Health /
Contractor / Environment / Chemical modules are deferred until those exist.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capa import Capa
from app.models.equipment import Equipment, Inspection
from app.models.incident import Incident
from app.models.masters import Department
from app.models.permit import Permit
from app.models.scr import RegisterEntry, RegisterMaster
from app.models.training import TrainingCertificate, TrainingProgram
from app.models.user import User

_INSPECTORATE = "Chief Inspector of Factories"
_FM = "Factory Manager"

# ── Register catalogue (code → definition) ────────────────────────────
REGISTERS: dict[str, dict[str, Any]] = {
    "FORM18": dict(registerName="Register of Accidents & Dangerous Occurrences — Form 18", legalAct="Factories Act, 1948", sectionRule="Section 88 & 88A", sourceModule="IncidentManagement", sourceEventType="INCIDENT_RECORDED", entryFrequency="ON_EVENT", submissionFrequency="ON_OCCURRENCE", submissionAuthority=_INSPECTORATE, authorisedSignatoryRole=_FM, retentionPeriodYears=5),
    "PTW-HOTWORK": dict(registerName="Hot Work Permit Register", legalAct="Factory Rules / IS 13947", sectionRule="PTW", sourceModule="PermitToWork", sourceEventType="PTW_ISSUED", entryFrequency="ON_EVENT", submissionFrequency="ON_OCCURRENCE", submissionAuthority=_INSPECTORATE, authorisedSignatoryRole="Safety Officer", retentionPeriodYears=3),
    "PTW-CONFINED": dict(registerName="Confined Space Entry Permit Register", legalAct="Factories Act, 1948", sectionRule="Section 36A", sourceModule="PermitToWork", sourceEventType="PTW_ISSUED", entryFrequency="ON_EVENT", submissionFrequency="ON_OCCURRENCE", submissionAuthority=_INSPECTORATE, authorisedSignatoryRole="Safety Officer", retentionPeriodYears=3),
    "PTW-HEIGHT": dict(registerName="Work at Height Permit Register", legalAct="Factory Rules", sectionRule="PTW", sourceModule="PermitToWork", sourceEventType="PTW_ISSUED", entryFrequency="ON_EVENT", submissionFrequency="ON_OCCURRENCE", submissionAuthority=_INSPECTORATE, authorisedSignatoryRole="Safety Officer", retentionPeriodYears=3),
    "PTW-LOTO": dict(registerName="Electrical Isolation / LOTO Register", legalAct="CEA Rules, 2010 / Factory Rules", sectionRule="PTW", sourceModule="PermitToWork", sourceEventType="PTW_ISSUED", entryFrequency="ON_EVENT", submissionFrequency="ON_OCCURRENCE", submissionAuthority="Electrical Inspector", authorisedSignatoryRole="Authorised Electrical Person", retentionPeriodYears=3),
    "PTW-EXCAVATION": dict(registerName="Excavation Permit Register", legalAct="Factory Rules", sectionRule="PTW", sourceModule="PermitToWork", sourceEventType="PTW_ISSUED", entryFrequency="ON_EVENT", submissionFrequency="ON_OCCURRENCE", submissionAuthority=_INSPECTORATE, authorisedSignatoryRole="Safety Officer", retentionPeriodYears=3),
    "PTW-COLDWORK": dict(registerName="General Cold Work Permit Register", legalAct="Factory best practice", sectionRule="PTW", sourceModule="PermitToWork", sourceEventType="PTW_ISSUED", entryFrequency="ON_EVENT", submissionFrequency="ON_OCCURRENCE", submissionAuthority=_INSPECTORATE, authorisedSignatoryRole="Safety Officer", retentionPeriodYears=3),
    "FORM11": dict(registerName="Register of Examinations — Form 11 (Lifting Machinery)", legalAct="Factories Act, 1948", sectionRule="Section 28, Factory Rules", sourceModule="Inspection", sourceEventType="INSPECTION_COMPLETED", entryFrequency="ON_EVENT", submissionFrequency="HALF_YEARLY", submissionAuthority=_INSPECTORATE, authorisedSignatoryRole="Competent Person", retentionPeriodYears=5),
    "FORM10": dict(registerName="Register of Examinations — Form 10 (Pressure Vessels)", legalAct="Factories Act, 1948", sectionRule="Section 31 & Sch. IV", sourceModule="Inspection", sourceEventType="INSPECTION_COMPLETED", entryFrequency="ON_EVENT", submissionFrequency="ANNUAL", submissionAuthority=_INSPECTORATE, authorisedSignatoryRole="Competent Person", retentionPeriodYears=5),
    "FORM13": dict(registerName="Register of Examinations — Form 13 (Hoists & Lifts)", legalAct="Factories Act, 1948", sectionRule="Section 29, Factory Rules", sourceModule="Inspection", sourceEventType="INSPECTION_COMPLETED", entryFrequency="ON_EVENT", submissionFrequency="HALF_YEARLY", submissionAuthority=_INSPECTORATE, authorisedSignatoryRole="Competent Person", retentionPeriodYears=5),
    "FIRE-EXT": dict(registerName="Fire Extinguisher / Fire-Equipment Maintenance Register", legalAct="State Factory Rules / NBC", sectionRule="Fire Rules", sourceModule="Inspection", sourceEventType="INSPECTION_COMPLETED", entryFrequency="ON_EVENT", submissionFrequency="MONTHLY", submissionAuthority="Chief Fire Officer", authorisedSignatoryRole="Fire & Safety Officer", retentionPeriodYears=3),
    "EQUIP-EXAM": dict(registerName="Statutory Equipment Examination Register", legalAct="Factories Act, 1948", sectionRule="Equipment examinations", sourceModule="Inspection", sourceEventType="INSPECTION_COMPLETED", entryFrequency="ON_EVENT", submissionFrequency="ON_OCCURRENCE", submissionAuthority=_INSPECTORATE, authorisedSignatoryRole="Competent Person", retentionPeriodYears=5),
    "TRAIN-REGISTER": dict(registerName="Safety Training & Competency Register", legalAct="Factory Rules / OSH Code", sectionRule="Training", sourceModule="Training", sourceEventType="TRAINING_COMPLETED", entryFrequency="ON_EVENT", submissionFrequency="ANNUAL", submissionAuthority=_INSPECTORATE, authorisedSignatoryRole="HSE Manager", retentionPeriodYears=3),
    "CAPA-REGISTER": dict(registerName="CAPA Register", legalAct="Schedule M / ISO 9001", sectionRule="CAPA", sourceModule="CAPA", sourceEventType="CAPA_RAISED", entryFrequency="ON_EVENT", submissionFrequency="ON_OCCURRENCE", submissionAuthority="Internal / Notified Body", authorisedSignatoryRole="Quality / HSE Head", retentionPeriodYears=5),
}

_FORM18_TYPES = {"FIRST_AID", "MTC", "RWC", "LTI", "FATALITY", "FIRE", "PROCESS_SAFETY", "HIPO_NEAR_MISS"}
_NATURE_LABEL = {
    "FIRST_AID": "First-Aid Injury", "MTC": "Medical Treatment Case", "RWC": "Restricted Work Case",
    "LTI": "Lost-Time Injury (Reportable)", "FATALITY": "Fatal Accident", "FIRE": "Dangerous Occurrence — Fire",
    "PROCESS_SAFETY": "Dangerous Occurrence — Process Safety", "HIPO_NEAR_MISS": "Dangerous Occurrence — High-Potential",
}
_NOTIFIABLE = {"LTI", "FATALITY", "FIRE", "PROCESS_SAFETY", "HIPO_NEAR_MISS"}

_PTW_TYPE_REGISTER = {
    "HOT_WORK": "PTW-HOTWORK", "CONFINED_SPACE": "PTW-CONFINED", "WORK_AT_HEIGHT": "PTW-HEIGHT",
    "ELECTRICAL_LOTO": "PTW-LOTO", "EXCAVATION": "PTW-EXCAVATION", "GENERAL_COLD": "PTW-COLDWORK",
}
_PTW_ISSUED = {"ISSUER_APPROVED", "SAFETY_APPROVED", "PLANT_HEAD_APPROVED", "ACTIVE", "SUSPENDED", "EXPIRED", "CLOSED"}


def _ev(v: Any) -> str:
    return v.value if hasattr(v, "value") else str(v)


def _d(dt: datetime | None) -> str | None:
    return dt.date().isoformat() if dt else None


def _inspection_register(category: str | None) -> str:
    c = (category or "").lower()
    if any(k in c for k in ("pressure", "vessel", "boiler", "compressor", "receiver")):
        return "FORM10"
    if any(k in c for k in ("hoist", "elevator", "passenger lift")):
        return "FORM13"
    if any(k in c for k in ("lift", "crane", "sling", "chain", "rigging")):
        return "FORM11"
    if "fire" in c:
        return "FIRE-EXT"
    return "EQUIP-EXAM"


async def ensure_all_registers(db: AsyncSession, plant_id: str) -> dict[str, RegisterMaster]:
    existing = {
        r.registerCode: r
        for r in (await db.execute(select(RegisterMaster).where(RegisterMaster.plantId == plant_id))).scalars().all()
    }
    for code, definition in REGISTERS.items():
        if code not in existing:
            reg = RegisterMaster(registerCode=code, plantId=plant_id, **definition)
            db.add(reg)
            await db.flush()
            existing[code] = reg
    return existing


def _upsert(reg: RegisterMaster, *, source_id: str, module: str, ref: str | None, when: datetime | None,
            fields: dict, actor: str, by_source: dict[str, RegisterEntry], db: AsyncSession, now: datetime) -> str:
    """Idempotent: returns 'created' | 'updated' | 'unchanged'."""
    row = by_source.get(source_id)
    if row is None:
        db.add(RegisterEntry(
            registerId=reg.id, sourceTransactionId=source_id, sourceModule=module, sourceRef=ref,
            entryDate=when or now, entryCreatedBy=actor, entryFieldsJson=fields,
            auditTrail=[{"at": now.isoformat(), "by": actor, "action": "AUTO_CREATED", "source": ref}],
        ))
        return "created"
    if not row.isVoided and row.entryFieldsJson != fields:
        trail = list(row.auditTrail or [])
        trail.append({"at": now.isoformat(), "by": actor, "action": "AUTO_REFRESHED", "source": ref})
        row.entryFieldsJson = fields
        row.entryDate = when or row.entryDate
        row.auditTrail = trail
        return "updated"
    return "unchanged"


async def _entries_by_source(db: AsyncSession, register_ids: list[str]) -> dict[str, dict[str, RegisterEntry]]:
    out: dict[str, dict[str, RegisterEntry]] = {rid: {} for rid in register_ids}
    if not register_ids:
        return out
    for e in (await db.execute(select(RegisterEntry).where(RegisterEntry.registerId.in_(register_ids)))).scalars().all():
        out.setdefault(e.registerId, {})[e.sourceTransactionId] = e
    return out


async def sync_all(db: AsyncSession, *, plant_id: str, actor: str = "SYSTEM") -> dict[str, Any]:
    regs = await ensure_all_registers(db, plant_id)
    by_source = await _entries_by_source(db, [r.id for r in regs.values()])
    now = datetime.now(timezone.utc)
    users = {u.id: u.name for u in (await db.execute(select(User).where(User.plantId == plant_id))).scalars().all()}
    user_ids = set(users)
    stats: dict[str, dict[str, int]] = {}

    def stat(code: str, kind: str) -> None:
        s = stats.setdefault(code, {"created": 0, "updated": 0})
        if kind in s:
            s[kind] += 1

    # 1) FORM18 ← Incident
    incidents = (await db.execute(
        select(Incident).where(Incident.plantId == plant_id).where(Incident.type.in_(_FORM18_TYPES)).order_by(Incident.date.asc())
    )).scalars().all()
    dept_ids = {i.departmentId for i in incidents if i.departmentId}
    dept = {d.id: d.name for d in ((await db.execute(select(Department).where(Department.id.in_(dept_ids)))).scalars().all() if dept_ids else [])}
    extra_users = {i.reporterId for i in incidents} - user_ids
    if extra_users:
        for u in (await db.execute(select(User).where(User.id.in_(extra_users)))).scalars().all():
            users[u.id] = u.name
    reg = regs["FORM18"]
    for seq, i in enumerate(incidents, 1):
        when = i.occurredAt or i.date
        typ = _ev(i.type)
        fields = {
            "srNo": seq, "injuredPersonName": users.get(i.reporterId, "—"), "department": dept.get(i.departmentId, "—"),
            "dateOfAccident": _d(when), "timeOfAccident": when.strftime("%H:%M") if when else None,
            "natureOfInjury": _NATURE_LABEL.get(typ, typ), "causeOfAccident": i.rootCauseSummary or "Under investigation",
            "location": i.specificLocation or i.location or "—", "daysLost": int(i.lostDays or 0),
            "reportableToInspectorate": typ in _NOTIFIABLE, "investigationReference": i.number,
        }
        stat("FORM18", _upsert(reg, source_id=i.id, module="IncidentManagement", ref=i.number, when=when, fields=fields, actor=actor, by_source=by_source[reg.id], db=db, now=now))

    # 2) PTW permit registers ← Permit (routed by type)
    permits = (await db.execute(select(Permit).where(Permit.plantId == plant_id).order_by(Permit.validFrom.asc()))).scalars().all()
    seq_by_reg: dict[str, int] = {}
    for p in permits:
        code = _PTW_TYPE_REGISTER.get(_ev(p.type))
        if not code or _ev(p.status) not in _PTW_ISSUED:
            continue
        reg = regs[code]
        seq_by_reg[code] = seq_by_reg.get(code, 0) + 1
        fields = {
            "srNo": seq_by_reg[code], "permitNumber": p.number, "workLocation": p.specificLocation or p.location or "—",
            "issuedTo": users.get(p.receiverId, "—") if p.receiverId else "—", "issuedBy": users.get(p.issuerId, "—") if p.issuerId else "—",
            "validFrom": _d(p.validFrom), "validTo": _d(p.validTo), "status": _ev(p.status), "closedOn": _d(p.closedAt),
        }
        stat(code, _upsert(reg, source_id=p.id, module="PermitToWork", ref=p.number, when=p.validFrom, fields=fields, actor=actor, by_source=by_source[reg.id], db=db, now=now))

    # 3) Equipment examination registers ← Inspection (routed by equipment category)
    inspections = (await db.execute(
        select(Inspection).where(Inspection.plantId == plant_id).where(Inspection.status == "COMPLETED")
    )).scalars().all()
    eq_ids = {ins.equipmentId for ins in inspections}
    equip = {e.id: e for e in ((await db.execute(select(Equipment).where(Equipment.id.in_(eq_ids)))).scalars().all() if eq_ids else [])}
    inspections.sort(key=lambda x: (x.completedDate or x.scheduledDate))
    seq_by_reg2: dict[str, int] = {}
    for ins in inspections:
        eq = equip.get(ins.equipmentId)
        code = _inspection_register(eq.category if eq else None)
        reg = regs[code]
        seq_by_reg2[code] = seq_by_reg2.get(code, 0) + 1
        when = ins.completedDate or ins.scheduledDate
        fields = {
            "srNo": seq_by_reg2[code], "equipmentCode": eq.code if eq else "—", "equipmentName": eq.name if eq else "—",
            "category": eq.category if eq else "—", "statutoryRegNo": (eq.statutoryRegistrationNumber if eq else None) or "—",
            "examinationDate": _d(when), "inspector": users.get(ins.inspectorId, "—") if ins.inspectorId else "—",
            "result": ins.result or "—", "nextDue": _d(eq.nextInspectionDue) if eq else None, "reference": ins.number,
        }
        stat(code, _upsert(reg, source_id=ins.id, module="Inspection", ref=ins.number, when=when, fields=fields, actor=actor, by_source=by_source[reg.id], db=db, now=now))

    # 4) Training register ← TrainingCertificate (for the plant's people)
    if user_ids:
        certs = (await db.execute(
            select(TrainingCertificate).where(TrainingCertificate.userId.in_(user_ids)).order_by(TrainingCertificate.issuedAt.asc())
        )).scalars().all()
        prog_ids = {c.programId for c in certs}
        progs = {p.id: p for p in ((await db.execute(select(TrainingProgram).where(TrainingProgram.id.in_(prog_ids)))).scalars().all() if prog_ids else [])}
        reg = regs["TRAIN-REGISTER"]
        for seq, c in enumerate(certs, 1):
            prog = progs.get(c.programId)
            fields = {
                "srNo": seq, "employeeName": users.get(c.userId, "—"),
                "programCode": (prog.code if prog else None) or "—", "programName": (prog.name if prog else None) or "—",
                "trainingDate": _d(c.issuedAt), "validUntil": _d(c.validTo), "status": _ev(c.status),
                "certificateNumber": c.certificateNumber,
            }
            stat("TRAIN-REGISTER", _upsert(reg, source_id=c.id, module="Training", ref=c.certificateNumber, when=c.issuedAt, fields=fields, actor=actor, by_source=by_source[reg.id], db=db, now=now))

    # 5) CAPA register ← Capa
    capas = (await db.execute(select(Capa).where(Capa.plantId == plant_id).order_by(Capa.createdAt.asc()))).scalars().all()
    extra = {c.primaryOwnerUserId for c in capas} - set(users)
    if extra:
        for u in (await db.execute(select(User).where(User.id.in_(extra)))).scalars().all():
            users[u.id] = u.name
    reg = regs["CAPA-REGISTER"]
    for seq, c in enumerate(capas, 1):
        fields = {
            "srNo": seq, "capaNumber": c.capaNumber, "title": c.title, "source": c.sourceTypeCode,
            "severity": c.severity, "status": c.state, "raisedOn": _d(c.detectedAt or c.createdAt),
            "dueOn": _d(c.closureTargetDate), "owner": users.get(c.primaryOwnerUserId, "—"),
        }
        stat("CAPA-REGISTER", _upsert(reg, source_id=c.id, module="CAPA", ref=c.capaNumber, when=c.detectedAt or c.createdAt, fields=fields, actor=actor, by_source=by_source[reg.id], db=db, now=now))

    await db.commit()
    total_c = sum(s["created"] for s in stats.values())
    total_u = sum(s["updated"] for s in stats.values())
    return {"plantId": plant_id, "registersTouched": len(stats), "created": total_c, "updated": total_u, "byRegister": stats}


# Backwards-compatible single-register entry point (Form 18 only).
async def sync_form18(db: AsyncSession, *, plant_id: str, actor: str = "SYSTEM") -> dict[str, Any]:
    return await sync_all(db, plant_id=plant_id, actor=actor)
