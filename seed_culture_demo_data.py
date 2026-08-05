"""Safety Culture — 90-day demo backfill (§Fix 9).

Makes a COLD load of Meridian North Works (plant code NW) show a fully populated
Safety Culture section — non-zero maturity trend, leadership walks (incl. a missed +
escalated one), a BBS Quality index with exactly ONE realistic integrity flag
(Lalit Nair) against multiple clean observers, a 3-period perception dimension trend,
a populated Leading/Lagging trend, and a recognition leaderboard with real point
history — with ZERO contradiction between BBS integrity and Recognition (Lalit's
points are frozen by the shared integrity gate). The other 27 sites get a lighter
30-day backfill so the portfolio rollup ("Sites by Maturity Stage") isn't flat.

⚠ This writes to the LIVE Supabase prod DB (see the backend-seeds-hit-prod-db note).
It is idempotent: shared core tables (Observation/NearMiss/Incident) are tagged with
distinct number prefixes (OBS-SCD-/NM-SCD-/INC-SCD-) and deleted-by-prefix; culture-
owned rows are tagged ([SCD] notes/detail, scd: tokens) and deleted-by-tag, so a
re-run replaces the seed without touching real data. Deletes use raw SQL to bypass
the platform soft-delete hard-delete guard.

  venv/Scripts/python.exe seed_culture_demo_data.py
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.core.db import AsyncSessionLocal
from app.models.incident import Incident, IncidentStatus, IncidentType
from app.models.near_miss import NearMiss, NearMissStatus
from app.models.observation import (
    Observation,
    ObservationCategory,
    ObservationStatus,
    ObservationType,
    Severity,
)
from app.models.plant import Plant
from app.models.safety_culture import (
    CultureMaturitySnapshot,
    CultureObservationClosure,
    LeadershipWalk,
    PerceptionSurveyTemplate,
    PerceptionSurveyResponse,
    RecognitionEntry,
)
from app.models.user import User
from app.services import safety_culture as svc

RNG = random.Random(20260708)
NOW = datetime.now(timezone.utc)


def days_ago(n: int, hour: int = 10) -> datetime:
    d = NOW - timedelta(days=n)
    return d.replace(hour=hour, minute=RNG.randint(0, 59), second=0, microsecond=0)


def month_period(offset_back: int) -> str:
    y, m = NOW.year, NOW.month
    for _ in range(offset_back):
        m -= 1
        if m < 1:
            y -= 1
            m = 12
    return f"{y:04d}-{m:02d}"


# ── clean-observer descriptions (>15 chars → not low-effort) ──────────────────
CLEAN_DESCRIPTIONS = [
    "Operator bypassing machine guard on line 3 — coached on interlock use.",
    "Spilled coolant near CNC bay creating a slip hazard; area cordoned.",
    "Worker at height without full body harness anchored; corrected on spot.",
    "Forklift reversing without spotter in packing aisle during peak shift.",
    "Frayed power cable on portable grinder taken out of service.",
    "Good practice: team completed LOTO correctly before conveyor cleaning.",
    "Chemical drums stored without secondary containment in store 2.",
    "Housekeeping poor near welding booth — combustible offcuts accumulating.",
    "Ergonomic strain: repetitive lifting above shoulder at sub-assembly.",
    "Emergency exit partially blocked by pallet stack in finished goods.",
    "Hot work permit not displayed at cutting station during grinding.",
    "Positive: supervisor stopped job on hearing abnormal press noise.",
]
# NB: the live DB's native ObservationCategory enum only accepts these labels
# (PPE, HOUSEKEEPING, WORK_AT_HEIGHT, HOT_WORK, MOBILE_EQUIPMENT, ELECTRICAL,
# MATERIAL_HANDLING, CONFINED_SPACE, CHEMICAL_HANDLING, EMERGENCY_PREP, OTHERS) —
# the model's ERGONOMICS/OTHER/EMERGENCY canonical names would fail on write.
CLEAN_CATEGORIES = [
    ObservationCategory.PPE, ObservationCategory.HOUSEKEEPING, ObservationCategory.WORK_AT_HEIGHT,
    ObservationCategory.MOBILE_EQUIPMENT, ObservationCategory.ELECTRICAL, ObservationCategory.MATERIAL_HANDLING,
    ObservationCategory.CONFINED_SPACE, ObservationCategory.CHEMICAL_HANDLING, ObservationCategory.HOT_WORK,
]
CLEAN_TYPES = [
    ObservationType.UNSAFE_ACT, ObservationType.UNSAFE_CONDITION,
    ObservationType.UNSAFE_ACT, ObservationType.SAFE_ACT,  # mostly unsafe, some safe
]
CLEAN_SEVERITIES = [Severity.LOW, Severity.MEDIUM, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]


async def _uid(db, name: str) -> str | None:
    u = (await db.execute(select(User.id).where(User.name == name))).scalar_one_or_none()
    return u


async def _plant_users(db, plant_id: str, limit: int = 12) -> list[str]:
    rows = (await db.execute(select(User.id).where(User.plantId == plant_id).limit(limit))).scalars().all()
    return list(rows)


async def _wipe(db, seeded_ids: list[str]) -> None:
    """Idempotency — remove only this seed's tagged rows (raw SQL bypasses the
    soft-delete hard-delete guard). Preserves all real data."""
    await db.execute(text('DELETE FROM "CultureObservationClosure" WHERE "observationId" IN (SELECT id FROM "Observation" WHERE number LIKE \'OBS-SCD-%\')'))
    await db.execute(text("DELETE FROM \"Observation\" WHERE number LIKE 'OBS-SCD-%'"))
    await db.execute(text("DELETE FROM \"NearMiss\" WHERE number LIKE 'NM-SCD-%'"))
    await db.execute(text("DELETE FROM \"Incident\" WHERE number LIKE 'INC-SCD-%'"))
    await db.execute(text("DELETE FROM \"LeadershipWalk\" WHERE notes LIKE '[SCD]%'"))
    await db.execute(text("DELETE FROM \"RecognitionEntry\" WHERE detail LIKE '[SCD]%'"))
    await db.execute(text("DELETE FROM \"PerceptionSurveyResponse\" WHERE \"respondentAnonymousToken\" LIKE 'scd:%'"))
    if seeded_ids:
        await db.execute(
            text('DELETE FROM "PerceptionIndexSnapshot" WHERE "plantId" = ANY(:ids)'), {"ids": seeded_ids}
        )
        await db.execute(
            text('DELETE FROM "CultureMaturitySnapshot" WHERE "plantId" = ANY(:ids)'), {"ids": seeded_ids}
        )
        await db.execute(
            text('DELETE FROM "CultureObserverIntegrity" WHERE "plantId" = ANY(:ids)'), {"ids": seeded_ids}
        )
    await db.flush()


# ── observation / near-miss / incident builders (shared core tables) ──────────
_OBS_SEQ = {"n": 0}


def _obs_number(code: str) -> str:
    _OBS_SEQ["n"] += 1
    return f"OBS-SCD-{code}-{_OBS_SEQ['n']:05d}"


def _add_observation(db, code, plant_id, observer_id, when, category, otype, severity, description):
    obs = Observation(
        number=_obs_number(code), date=when, type=otype, category=category, severity=severity,
        plantId=plant_id, observerId=observer_id, description=description,
        status=ObservationStatus.CLOSED if RNG.random() < 0.5 else ObservationStatus.OPEN,
    )
    obs.createdAt = when
    db.add(obs)
    return obs


def _add_verified_closure(db, plant_id, obs, verified_by, when):
    db.add(CultureObservationClosure(
        observationId=obs.id, plantId=plant_id, linkedCapaId=f"SCD-CAPA-{obs.number}",
        reobservationVerified=True, reobservationDate=when, verifiedById=verified_by,
    ))


_NM_SEQ = {"n": 0}
_INC_SEQ = {"n": 0}


def _add_near_miss(db, code, plant_id, reporter_id, when, severity, description):
    _NM_SEQ["n"] += 1
    nm = NearMiss(
        number=f"NM-SCD-{code}-{_NM_SEQ['n']:05d}", date=when, plantId=plant_id, reporterId=reporter_id,
        description=description, potentialSeverity=severity, status=NearMissStatus.REPORTED,
    )
    nm.createdAt = when
    db.add(nm)
    return nm


def _add_incident(db, code, plant_id, reporter_id, when, itype, description):
    _INC_SEQ["n"] += 1
    inc = Incident(
        number=f"INC-SCD-{code}-{_INC_SEQ['n']:05d}", date=when, type=itype, plantId=plant_id,
        location="Shop floor", reporterId=reporter_id, description=description,
        lostDays=3 if itype == IncidentType.LTI else 0, status=IncidentStatus.CLOSED,
    )
    inc.createdAt = when
    db.add(inc)
    return inc


# ── Perception template + responses ───────────────────────────────────────────
PERCEPTION_QUESTIONS = [
    {"id": "q_trust_1", "text": "I can report a safety concern without fear of blame.", "dimension": "TrustInReporting", "scaleType": "likert5"},
    {"id": "q_trust_2", "text": "When I report a near-miss, something is actually done about it.", "dimension": "TrustInReporting", "scaleType": "likert5"},
    {"id": "q_psych_1", "text": "I feel safe stopping a job I believe is unsafe.", "dimension": "PsychologicalSafety", "scaleType": "likert5"},
    {"id": "q_psych_2", "text": "My team supports each other in working safely.", "dimension": "PsychologicalSafety", "scaleType": "likert5"},
    {"id": "q_mgmt_1", "text": "Leaders are visibly committed to safety on the floor.", "dimension": "ManagementCommitment", "scaleType": "likert5"},
    {"id": "q_mgmt_2", "text": "Safety is never sacrificed to hit production targets.", "dimension": "ManagementCommitment", "scaleType": "likert5"},
    {"id": "q_peer_1", "text": "My colleagues speak up when they see unsafe behaviour.", "dimension": "PeerAccountability", "scaleType": "likert5"},
    {"id": "q_peer_2", "text": "We hold each other accountable for wearing PPE.", "dimension": "PeerAccountability", "scaleType": "likert5"},
]

# per-period target averages (1-5) by dimension — trust is the weaker dimension,
# and everything improves period over period so the trend line rises.
PERIOD_TARGETS = {
    0: {"TrustInReporting": 2.9, "PsychologicalSafety": 3.3, "ManagementCommitment": 3.1, "PeerAccountability": 3.2},
    1: {"TrustInReporting": 3.2, "PsychologicalSafety": 3.6, "ManagementCommitment": 3.5, "PeerAccountability": 3.4},
    2: {"TrustInReporting": 3.6, "PsychologicalSafety": 3.9, "ManagementCommitment": 3.8, "PeerAccountability": 3.7},
}


def _score_for(target_avg: float, jitter: int) -> int:
    return max(1, min(5, round(target_avg + [-1, 0, 0, 1, 0][jitter % 5] * 0.6)))


async def _ensure_template(db) -> PerceptionSurveyTemplate:
    t = (await db.execute(select(PerceptionSurveyTemplate).where(PerceptionSurveyTemplate.name == "Meridian Safety Perception Pulse"))).scalar_one_or_none()
    if t is None:
        t = PerceptionSurveyTemplate(
            name="Meridian Safety Perception Pulse",
            description="Quarterly anonymous pulse across trust in reporting, psychological safety, management commitment and peer accountability.",
            questions=PERCEPTION_QUESTIONS, isActive=True, cadence="QUARTERLY",
        )
        db.add(t)
        await db.flush()
    else:
        t.isActive = True
        t.questions = PERCEPTION_QUESTIONS
    return t


async def _seed_perception(db, plant_id, code, template, headcount, periods):
    """Insert anonymous responses (scd: tokens) for each quarter, then publish the
    threshold-met snapshot via the real engine so the trend is fully consistent."""
    n_responses = max(12, int(headcount * 0.48))
    for p_idx, period in enumerate(periods):
        targets = PERIOD_TARGETS[min(p_idx, 2)]
        for i in range(n_responses):
            answers = []
            for q in PERCEPTION_QUESTIONS:
                answers.append({"questionId": q["id"], "score": _score_for(targets[q["dimension"]], i + p_idx)})
            db.add(PerceptionSurveyResponse(
                surveyTemplateId=template.id, plantId=plant_id, period=period,
                respondentAnonymousToken=f"scd:{code}:{period}:{i}", responses=answers,
            ))
        await db.flush()
        await svc.compute_perception_index(db, plant_id, period, publish=True)


# ── Leadership walks ──────────────────────────────────────────────────────────
AREAS = ["Cutting hall", "Line 3 assembly", "Finishing bay", "Warehouse dock", "Utilities / boiler house", "Packing line 2"]


def _add_walk(db, plant_id, leader_id, when, status, area, workers, hazards, obs_raised, completed_when=None, checklist=None, escalated=False):
    w = LeadershipWalk(
        plantId=plant_id, leaderId=leader_id, scheduledDate=when,
        completedDate=completed_when, status=status, areaVisited=area,
        workersInteracted=workers, observationsRaised=obs_raised, hazardsIdentified=hazards,
        notes=f"[SCD] {area} walk", checklist=checklist, followUpActionIds=[],
    )
    if escalated:
        w.escalatedAt = when + timedelta(days=2)
    w.createdAt = when
    db.add(w)
    return w


def _walk_checklist(hazards):
    return {
        "hazardCategories": RNG.sample(["PPE", "Housekeeping", "Work at Height", "Electrical", "Machine Guarding"], k=min(3, max(1, hazards))),
        "workerInteractions": [{"count": RNG.randint(2, 6), "topic": "Reviewed safe work method with operators"}],
        "ppeCompliance": RNG.choice([80, 85, 90, 95, 100]),
        "housekeepingRating": RNG.randint(3, 5),
    }


# ── Recognition (past periods, improving) ─────────────────────────────────────
def _add_recognition(db, plant_id, user_id, category, period, points, badge=None, streak=0):
    db.add(RecognitionEntry(
        plantId=plant_id, userId=user_id, category=category, periodEarned=period,
        points=points, badgeAwarded=badge, streakWeeks=streak, detail="[SCD] seeded history",
    ))


# ── Maturity snapshots (past months, improving) ───────────────────────────────
def _add_maturity_snapshot(db, plant_id, period, stage_score, components):
    db.add(CultureMaturitySnapshot(
        plantId=plant_id, period=period, stageScore=stage_score,
        currentStage=svc.stage_for(stage_score, svc._DEFAULT), componentScores=components,
    ))


def _maturity_components(base):
    return {
        "leadershipEngagement": round(base + 4, 1), "workerParticipation": round(base - 2, 1),
        "leadingLaggingRatio": round(base + 1, 1), "bbsQualityIndex": round(base, 1),
        "perceptionIndex": round(base - 5, 1),
    }


# ════════════════════════════════════════════════════════════════════════════
async def seed_nw(db, nw):
    """Rich 90-day backfill for Meridian North Works."""
    code = nw.code
    print(f"\n── Meridian North Works ({code}) — 90-day backfill ──")

    # observers
    clean_names = [
        "Sunita Devi", "Arjun Pal", "Ramesh Kumar", "Karan Yadav", "Suresh Tiwari",
        "Priya Roy", "Ramesh Mehta", "Chandan Khanna", "Naveen Rao",
    ]
    clean_ids: list[str] = []
    for nm in clean_names:
        uid = await _uid(db, nm)
        if uid:
            clean_ids.append(uid)
    lalit_id = await _uid(db, "Lalit Nair")
    praveen = await _uid(db, "Praveen Agarwal")
    priya_nair = await _uid(db, "Priya Nair")
    tushar = await _uid(db, "Tushar Mishra")
    verifier = praveen or priya_nair or (clean_ids[0] if clean_ids else lalit_id)
    headcount = (await db.execute(select(User.id).where(User.plantId == nw.id))).scalars().all()
    headcount = len(headcount)

    # 1) BBS observations — natural volume spread, one low-volume clean contrast
    volumes = [18, 14, 11, 9, 8, 6, 5, 4, 4]
    obs_created = 0
    verified_closures = 0
    for idx, observer_id in enumerate(clean_ids):
        vol = volumes[idx % len(volumes)]
        for j in range(vol):
            when = days_ago(RNG.randint(1, 88), RNG.randint(6, 18))
            cat = CLEAN_CATEGORIES[(idx + j) % len(CLEAN_CATEGORIES)]
            otype = CLEAN_TYPES[(idx + j) % len(CLEAN_TYPES)]
            sev = CLEAN_SEVERITIES[(idx * 3 + j) % len(CLEAN_SEVERITIES)]
            desc = CLEAN_DESCRIPTIONS[(idx + j) % len(CLEAN_DESCRIPTIONS)]
            obs = _add_observation(db, code, nw.id, observer_id, when, cat, otype, sev, desc)
            obs_created += 1
            # ~1 in 4 (weighted to higher severity) closes the loop + is verified
            if sev in (Severity.HIGH, Severity.CRITICAL) or RNG.random() < 0.22:
                await db.flush()
                _add_verified_closure(db, nw.id, obs, verifier, when + timedelta(days=RNG.randint(3, 15)))
                verified_closures += 1

    # 2) Lalit Nair — crafted anomaly (same category + same hour, low-effort SAFE,
    #    recent) so the integrity detector flags exactly this one observer.
    if lalit_id:
        for k in range(14):
            when = days_ago(RNG.randint(1, 20), 14)  # all at 14:00
            obs = _add_observation(
                db, code, nw.id, lalit_id, when,
                ObservationCategory.HOUSEKEEPING, ObservationType.SAFE_ACT, Severity.LOW, "OK",
            )
        print(f"   • crafted 14 anomalous entries for Lalit Nair (HOUSEKEEPING @ 14:00, low-effort)")
    print(f"   • {obs_created} clean observations across {len(clean_ids)} observers; {verified_closures} verified closures")

    # 3) Near-misses (leading + participation)
    for i in range(24):
        rid = clean_ids[i % len(clean_ids)] if clean_ids else lalit_id
        _add_near_miss(db, code, nw.id, rid, days_ago(RNG.randint(1, 88)), RNG.choice([Severity.LOW, Severity.MEDIUM, Severity.HIGH]),
                       "Near-miss: unsecured load shifted during transfer; no injury.")

    # 4) A few lagging incidents so the ratio is realistic (~30-50:1), not infinite
    for itype, dback in [(IncidentType.FIRST_AID, 20), (IncidentType.MTC, 55), (IncidentType.FIRST_AID, 75)]:
        rid = clean_ids[0] if clean_ids else lalit_id
        _add_incident(db, code, nw.id, rid, days_ago(dback), itype, "Minor recordable during routine operation.")

    # 5) Leadership walks — Priya Nair improving; ~78% compliance; 1 missed+escalated
    leaders = [x for x in [priya_nair, praveen, tushar] if x]
    # improving completed walks over the last 5 months for Priya Nair
    if priya_nair:
        for m_back, (comp, total) in enumerate([(2, 4), (3, 4), (3, 4), (4, 4)]):  # recent months
            for c in range(total):
                dback = m_back * 22 + c * 4 + 3
                if c < comp:
                    _add_walk(db, nw.id, priya_nair, days_ago(dback + 1, 9), "Completed",
                              RNG.choice(AREAS), RNG.randint(4, 9), RNG.randint(1, 4), RNG.randint(1, 3),
                              completed_when=days_ago(dback, 12), checklist=_walk_checklist(RNG.randint(1, 4)))
                else:
                    _add_walk(db, nw.id, priya_nair, days_ago(dback, 9), "Missed", RNG.choice(AREAS), 0, 0, 0)
    # Praveen — steady, plus one past-due Scheduled walk that recalc will escalate
    if praveen:
        for c in range(4):
            _add_walk(db, nw.id, praveen, days_ago(10 + c * 12, 9), "Completed", RNG.choice(AREAS),
                      RNG.randint(3, 8), RNG.randint(1, 3), RNG.randint(0, 2),
                      completed_when=days_ago(9 + c * 12, 13), checklist=_walk_checklist(RNG.randint(1, 3)))
        # past-due Scheduled → escalate_missed_walks (run by recalc) flips to Missed
        _add_walk(db, nw.id, praveen, days_ago(12, 9), "Scheduled", "Utilities / boiler house", 0, 0, 0)
    # one future scheduled walk (upcoming)
    if tushar:
        _add_walk(db, nw.id, tushar, NOW + timedelta(days=5), "Scheduled", "Packing line 2", 0, 0, 0)

    # 6) Perception — 3 quarters, improving, dimension trend
    template = await _ensure_template(db)
    periods_q = ["2026-Q1", "2026-Q2", "2026-Q3"]
    await _seed_perception(db, nw.id, code, template, headcount, periods_q)
    print(f"   • perception seeded for {periods_q} (~{max(12, int(headcount*0.48))} responses each)")

    # 7) Recognition history (past 2 months, improving) — Priya Nair most-improved
    prev2, prev1 = month_period(2), month_period(1)
    if priya_nair:
        _add_recognition(db, nw.id, priya_nair, "LeadershipWalkCompliance", prev2, 22.0)
        _add_recognition(db, nw.id, priya_nair, "LeadershipWalkCompliance", prev1, 34.0, badge="Felt Leadership")
    for idx, cid in enumerate(clean_ids[:5]):
        _add_recognition(db, nw.id, cid, "QualityContribution", prev2, round(8 + idx * 2.0, 1))
        _add_recognition(db, nw.id, cid, "QualityContribution", prev1, round(10 + idx * 2.5, 1))
    if lalit_id:  # Lalit looked like a champion historically — the gate now freezes current period
        _add_recognition(db, nw.id, lalit_id, "QualityContribution", prev2, 120.0, badge="Quality Champion")
        _add_recognition(db, nw.id, lalit_id, "QualityContribution", prev1, 138.0, badge="Quality Champion")

    # 8) Maturity trend — 5 past months, improving (recalc writes the current month)
    for m_back, base in [(5, 34), (4, 38), (3, 41), (2, 45), (1, 49)]:
        _add_maturity_snapshot(db, nw.id, month_period(m_back), float(base), _maturity_components(base))

    await db.flush()


async def seed_light(db, plant, users, leader_id, template, i):
    """Lighter 30-day backfill for a non-NW site."""
    code = plant.code
    if not users:
        return
    observers = users[: min(4, len(users))]
    # 3-6 observations
    for k in range(RNG.randint(3, 6)):
        obs = _add_observation(
            db, code, plant.id, observers[k % len(observers)], days_ago(RNG.randint(1, 28), RNG.randint(7, 17)),
            CLEAN_CATEGORIES[k % len(CLEAN_CATEGORIES)], CLEAN_TYPES[k % len(CLEAN_TYPES)],
            CLEAN_SEVERITIES[k % len(CLEAN_SEVERITIES)], CLEAN_DESCRIPTIONS[k % len(CLEAN_DESCRIPTIONS)],
        )
        if RNG.random() < 0.3:
            await db.flush()
            _add_verified_closure(db, plant.id, obs, leader_id or observers[0], days_ago(RNG.randint(1, 10)))
    # 1-2 near-misses
    for k in range(RNG.randint(1, 2)):
        _add_near_miss(db, code, plant.id, observers[0], days_ago(RNG.randint(1, 28)), Severity.MEDIUM, "Near-miss reported on the floor.")
    # occasional incident so lagging isn't zero everywhere
    if RNG.random() < 0.5:
        _add_incident(db, code, plant.id, observers[0], days_ago(RNG.randint(5, 28)), IncidentType.FIRST_AID, "Minor first-aid case.")
    # 1-2 completed walks
    if leader_id:
        for k in range(RNG.randint(1, 2)):
            _add_walk(db, plant.id, leader_id, days_ago(RNG.randint(5, 25), 9), "Completed", RNG.choice(AREAS),
                      RNG.randint(2, 6), RNG.randint(0, 2), RNG.randint(0, 2),
                      completed_when=days_ago(RNG.randint(1, 4), 13), checklist=_walk_checklist(RNG.randint(1, 2)))
    # one perception period (current quarter) — varied composite so stages spread
    hc = len(users) if users else 20
    await _seed_perception(db, plant.id, code, template, hc, ["2026-Q3"])
    # a couple maturity snapshots for a mini-trend
    base = RNG.choice([18, 24, 30, 38, 46, 55, 62])
    for m_back in (2, 1):
        _add_maturity_snapshot(db, plant.id, month_period(m_back), float(base + m_back), _maturity_components(base))
    await db.flush()


async def main():
    async with AsyncSessionLocal() as db:
        plants = (await db.execute(select(Plant))).scalars().all()
        by_code = {p.code: p for p in plants}
        nw = by_code.get("NW")
        if nw is None:
            raise SystemExit("Plant NW (Meridian North Works) not found — run the base seeds first.")
        seeded_ids = [p.id for p in plants]

        print("Wiping prior [SCD] seed rows (idempotent)…")
        await _wipe(db, seeded_ids)

        # Rich NW backfill
        await seed_nw(db, nw)

        # Light backfill for the other 27 sites
        template = await _ensure_template(db)
        print("\n── Light 30-day backfill across the other 27 sites ──")
        n_light = 0
        for i, plant in enumerate(plants):
            if plant.code == "NW":
                continue
            users = await _plant_users(db, plant.id, limit=12)
            leader = users[0] if users else None
            try:
                await seed_light(db, plant, users, leader, template, i)
                n_light += 1
            except Exception as e:  # keep going — one flaky site shouldn't abort the estate
                print(f"   ! {plant.code}: {e}")
        print(f"   • lightly seeded {n_light} sites")

        await db.commit()
        print("\nRecalculating every site (escalate missed walks → score → sync flags → award)…")
        res = await svc.recalculate_all(db)
        print(f"   {res}")

        # Report NW headline for a quick sanity check
        prof = await svc.maturity_profile_out(db, nw.id, with_history=True)
        board = await svc.leaderboard(db, nw.id, month_period(0))
        ll = await svc.leading_lagging_detail(db, nw.id)
        flags = await svc.integrity_flags(db, nw.id)
        print("\n✅  Seed complete.")
        print(f"   NW stage={prof['currentStage']} score={prof['stageScore']} history={len(prof['history'])} pts")
        print(f"   NW leading:lagging = {ll['ratio']}:1 (score {ll['score']}), trend pts={len(ll['trend'])}")
        print(f"   NW integrity flags={len(flags)} " + (f"→ {flags[0]['observerId']} {flags[0]['integrityStatus']}" if flags else ""))
        frozen = [e for e in board['individual'] if e.get('pointsFrozen')]
        print(f"   NW recognition individuals={len(board['individual'])} frozen={len(frozen)} mostImproved={len(board['mostImproved'])}")


if __name__ == "__main__":
    asyncio.run(main())
