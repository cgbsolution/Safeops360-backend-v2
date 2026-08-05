"""Demo seed for every P1/P2/P3 + deferred feature built this cycle, so no new
screen renders empty in an evaluation. Idempotent + defensive (each section is
independent — one failure never blocks the rest).

    python seed_remediation_demo.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.core.db import AsyncSessionLocal

NOW = datetime.now(timezone.utc)


def _section(name):
    def deco(fn):
        async def wrapped():
            try:
                async with AsyncSessionLocal() as db:
                    res = await fn(db)
                    await db.commit()
                print(f"  [OK] {name}: {res}")
            except Exception as e:  # noqa: BLE001
                print(f"  [SKIP] {name}: {str(e)[:120]}")
        return wrapped
    return deco


# 1) Scheduler — run every job once so the Job Monitor shows last-run for all.
@_section("P2-1 scheduler — run all jobs")
async def seed_jobs(_db):
    from app.services import scheduler as sched
    ran = []
    for jid in sched.JOBS:
        r = await sched.run_job(jid, trigger="STARTUP")
        ran.append((jid, r["status"]))
    return f"{sum(1 for _, s in ran if s == 'SUCCESS')}/{len(ran)} jobs SUCCESS"


# 2) Incident -> ERM risk alerts (I-04)
@_section("P2-2 incident->ERM flags (I-04)")
async def seed_incident_alerts(db):
    from app.services.erm import sync_incident_risk_alerts
    return await sync_incident_risk_alerts(db)


# 3) Appetite breaches + chip (I-14) — evaluate, then guarantee one open breach
#    carries a triggering risk so the detail chip renders.
@_section("P2-6 appetite breaches + chip (I-14)")
async def seed_appetite(db):
    from app.models.erm import EnterpriseRisk
    from app.models.erm_p2 import AppetiteBreach
    from app.services.erm_p2 import evaluate_appetite
    res = await evaluate_appetite(db)
    open_b = (await db.execute(select(AppetiteBreach).where(AppetiteBreach.status == "OPEN"))).scalars().all()
    risk = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.residualBand.in_(("HIGH", "CRITICAL"))).limit(1))).scalar_one_or_none()
    if open_b and risk and not (open_b[0].triggeringEntityIds or []):
        open_b[0].triggeringEntityIds = [risk.id]
    elif not open_b and risk:
        # mint one demonstrative open breach tied to a real high risk + statement/category
        st = (await db.execute(text('SELECT id,\"categoryId\" FROM \"AppetiteStatement\" LIMIT 1'))).first()
        if st:
            db.add(AppetiteBreach(appetiteStatementId=st[0], categoryId=st[1], bandType="MAX_HIGH_PLUS_COUNT",
                                  observedValue=3, thresholdValue=2, triggeringEntityIds=[risk.id], detectedAt=NOW, status="OPEN"))
    return f"evaluate={res}; open breaches now carry triggers"


# 4) CAMS repeat-finding pair (so repeat-rate isn't 0)
@_section("P2-4 CAMS repeat-finding pair")
async def seed_repeat(db):
    from app.models.cams import CamsFinding
    from app.services.cams_analytics import detect_repeat_findings
    finds = (await db.execute(select(CamsFinding).where(CamsFinding.isDeleted.is_(False)).where(CamsFinding.siteId.is_not(None)).limit(2))).scalars().all()
    if len(finds) >= 2:
        a, b = finds[0], finds[1]
        b.siteId = a.siteId
        b.standardClauseRef = a.standardClauseRef = a.standardClauseRef or "ISO 45001:8.1.2"
        a.status = "CLOSED"; a.closedAt = NOW - timedelta(days=40); a.createdAt = NOW - timedelta(days=120)
        b.createdAt = NOW - timedelta(days=5); b.status = "OPEN"
    await db.flush()
    return await detect_repeat_findings(db)


# 5) Statutory registers + compliance unification (P2-8)
@_section("P2-8 RegulatoryRegistration + link")
async def seed_reg(db):
    from app.models.factory_ext import RegulatoryRegistration
    from app.services.compliance_unification import link_registrations_to_obligations
    have = (await db.execute(text('SELECT count(*) FROM \"RegulatoryRegistration\"'))).scalar()
    if have == 0:
        fps = (await db.execute(text('SELECT id,\"siteId\" FROM \"FactoryProfile\" WHERE \"siteId\" IS NOT NULL LIMIT 3'))).all()
        kinds = [("FACTORY_ACT", "Factory Act Licence", "Chief Inspector of Factories"),
                 ("FIRE_LICENSE", "Fire Safety NOC", "State Fire Services"),
                 ("PCB", "Consent to Operate (Air & Water)", "State Pollution Control Board"),
                 ("BOILER", "Boiler Certificate", "Directorate of Boilers")]
        n = 0
        for fp_id, site in fps:
            for rtype, name, auth in kinds:
                n += 1
                db.add(RegulatoryRegistration(
                    factoryProfileId=fp_id, siteId=site, registrationType=rtype, registrationName=name,
                    issuingAuthority=auth, registrationNumber=f"{rtype}-{2026}-{n:04d}",
                    issueDate=NOW - timedelta(days=300), expiryDate=NOW + timedelta(days=65),
                    renewalFrequency="ANNUAL", status="VALID", alertThresholdDays=90,
                ))
        await db.flush()
    return await link_registrations_to_obligations(db, actor_id="SYSTEM:seed")


# 6) Observations quality + ABC (P3-1)
@_section("P3-1 BBS quality + ABC backfill")
async def seed_bbs(db):
    from app.models.observation import Observation
    from app.services.bbs_quality import quality_score
    obs = (await db.execute(select(Observation).where(Observation.isDeleted.is_(False) if hasattr(Observation, "isDeleted") else text("true")).limit(40))).scalars().all()
    updated = 0
    abc = [("Rushing to meet shift target", "Bypassed the machine guard interlock", "Near-miss hand entrapment risk"),
           ("Poor lighting in the aisle", "Walked under a suspended load", "Potential struck-by injury"),
           ("Missing signage", "Operated forklift without seatbelt", "Ejection risk on tip-over")]
    for i, o in enumerate(obs):
        if o.qualityScore is None:
            o.qualityScore = quality_score(o.description or "", o.areaId, o.responsiblePersonId)
            if i % 3 == 0:
                a = abc[i % len(abc)]
                o.antecedent, o.behaviourObserved, o.consequence = a
            updated += 1
    await db.flush()
    return f"{updated} observations scored ({sum(1 for o in obs if o.antecedent)} with ABC)"


# 7) MOC -> CAPA (I-18) on a real change request
@_section("P2-3 MOC->CAPA (I-18)")
async def seed_moc(db):
    from app.models.moc import ChangeRequest
    from app.services.capa_spawn import spawn_moc_capas
    cr = (await db.execute(select(ChangeRequest).limit(1))).scalar_one_or_none()
    if not cr:
        return "no change requests"
    return await spawn_moc_capas(db, cr, actor_id=cr.initiatedByUserId)


# 8) Kaizen -> CAPA (P3-2)
@_section("P3-2 Kaizen->CAPA")
async def seed_kaizen(db):
    from app.models.capa import Capa
    from app.services.capa_spawn import existing_capas_for, spawn_capa
    post = (await db.execute(text('SELECT id,\"plantId\",\"submitterUserId\" FROM \"KaizenPost\" LIMIT 1'))).first()
    if not post:
        return "no kaizen posts"
    if await existing_capas_for(db, "KAIZEN_INITIATIVE", post[0]):
        return "already linked"
    capa = await spawn_capa(db, source_code="KAIZEN_INITIATIVE", plant_id=post[1],
                            title="Implement Kaizen improvement", problem="Approved Kaizen idea — implementation tracking.",
                            ref_id=post[0], ref_url=f"/kaizen/{post[0]}", ref_summary="Kaizen", metadata={"kaizenPostId": post[0]},
                            severity="LOW", priority="MEDIUM", detected_method="KAIZEN_APPROVAL", owner_id=post[2], actor_id=post[2], due_days=60)
    return f"capa {capa.capaNumber}"


# 9) Expired PPE on active permit (P3-3)
@_section("P3-3 expired-PPE-on-active demo")
async def seed_expired_ppe(db):
    from app.models.permit import Permit, PermitCrewMember, PermitStatus
    from app.models.ppe import PpeIssuance, PpeItem
    permit = (await db.execute(select(Permit).where(Permit.isDeleted.is_(False)).limit(1))).scalar_one()
    permit.status = PermitStatus.ACTIVE  # enum member — a raw string silently fails to persist
    user = (await db.execute(text('SELECT id FROM \"User\" WHERE \"plantId\"=:p LIMIT 1'), {"p": permit.plantId})).first()
    uid = user[0] if user else None
    if uid:
        exists = (await db.execute(select(PermitCrewMember).where(PermitCrewMember.permitId == permit.id))).scalars().first()
        if not exists:
            db.add(PermitCrewMember(permitId=permit.id, userId=uid))
    iss = (await db.execute(select(PpeIssuance).limit(1))).scalar_one_or_none()
    if iss and uid:
        iss.status = "active"; iss.linkedPermitId = permit.id; iss.issuedToUserId = uid
        item = await db.get(PpeItem, iss.ppeItemId)
        if item:
            item.serviceLifeEndDate = NOW - timedelta(days=20)  # lapsed after activation
    await db.flush()
    return f"permit {permit.id[:8]} ACTIVE with expired-PPE crew member"


# 10) EAI legally-significant entry (P2-7 override)
@_section("P2-7 EAI legal-significant entry")
async def seed_eai(db):
    from app.models.eai import EaiEntry, EaiEntryRegulationRef
    entry = (await db.execute(select(EaiEntry).limit(1))).scalar_one_or_none()
    if not entry:
        return "no EAI entries"
    refs = (await db.execute(select(EaiEntryRegulationRef).where(EaiEntryRegulationRef.entryId == entry.id))).scalars().all()
    if not refs:
        db.add(EaiEntryRegulationRef(entryId=entry.id, regulationName="Water (Prevention & Control of Pollution) Act 1974",
                                     clauseReference="Sec 25", complianceRequirement="ETP discharge within consent limits"))
    entry.initialSignificant = True
    if entry.residualImpactLevel is not None:
        entry.residualSignificant = True
    return f"entry {entry.id[:8]} legally SIGNIFICANT"


# 11) Audit trail — hash-chained entries for real entities (so the viewer is non-empty)
@_section("P1-1 audit-trail demo entries")
async def seed_audit(db):
    from app.core.audit_context import AuditActor, set_actor
    from app.models.erm import EnterpriseRisk
    from app.models.incident import Incident
    from app.services.audit_log import write_entries
    set_actor(AuditActor(actor_id="SYSTEM:seed", actor_type="SYSTEM"))
    actor = AuditActor(actor_id="SYSTEM:seed", actor_type="SYSTEM", correlation_id="seed-demo")
    risks = (await db.execute(select(EnterpriseRisk).limit(4))).scalars().all()
    incs = (await db.execute(select(Incident).limit(3))).scalars().all()
    events = []
    for r in risks:
        events.append({"entityType": "EnterpriseRisk", "entityId": r.id, "entityCode": r.riskCode, "plantId": r.plantId,
                       "action": "CREATE", "before": None, "after": {"riskCode": r.riskCode, "title": r.title, "residualBand": r.residualBand}, "changedFields": ["riskCode", "title", "residualBand"]})
        events.append({"entityType": "EnterpriseRisk", "entityId": r.id, "entityCode": r.riskCode, "plantId": r.plantId,
                       "action": "STATE_TRANSITION", "before": {"lifecycleState": "ASSESSED"}, "after": {"lifecycleState": r.lifecycleState}, "changedFields": ["lifecycleState"]})
    for i in incs:
        events.append({"entityType": "Incident", "entityId": i.id, "entityCode": getattr(i, "number", i.id[:8]), "plantId": i.plantId,
                       "action": "CREATE", "before": None, "after": {"severity": i.severity, "status": str(i.status)}, "changedFields": ["severity", "status"]})
    n = await write_entries(db, events, actor)
    return f"{n} hash-chained audit entries"


async def main():
    print("Seeding remediation demo data (P1/P2/P3 + deferred)…")
    for fn in [seed_jobs, seed_incident_alerts, seed_appetite, seed_repeat, seed_reg, seed_bbs,
               seed_moc, seed_kaizen, seed_expired_ppe, seed_eai, seed_audit]:
        await fn()
    print("Done.")


asyncio.run(main())
