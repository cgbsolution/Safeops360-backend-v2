"""ERM Phase 3 (BCM) DEEP QA — validation guards, RBAC, business-logic edge cases.

Goes beyond the happy-path smoke: every 400/403/404/422 guard, RBAC denial,
crisis-immutability-after-close, exercise/finding gates, horizon disposition.

Run against the live backend on :8077; re-seed P3 afterwards to restore demo.
"""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8077"
P = F = 0


def req(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("content-type", "application/json")
    if token:
        r.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, (json.loads(raw) if raw else None)
        except Exception:
            return e.code, {"raw": raw}


def check(name, cond, detail=""):
    global P, F
    if cond:
        P += 1; print(f"  PASS  {name}")
    else:
        F += 1; print(f"  FAIL  {name}  -- {detail}")


def login(email):
    st, j = req("POST", "/api/auth/login", body={"email": email, "password": "demo123"})
    return (j or {}).get("access_token"), st


print("== ERM Phase 3 BCM DEEP QA ==")
farhan, _ = login("farhan.qureshi@safeops360.in")
worker, wst = login("worker.it.nw@safeops360.in")
check("login farhan", bool(farhan))
check("login worker (for RBAC denial)", bool(worker), f"login status={wst}")

# ── RBAC: a plain worker must be denied BCM ──────────────────────────────────
if worker:
    st, _ = req("GET", "/api/erm/bcm/dashboard", worker)
    check("worker DENIED bcm dashboard (403)", st == 403, f"status={st}")
    st, _ = req("GET", "/api/erm/bcm/processes", worker)
    check("worker DENIED bcm processes (403)", st == 403, f"status={st}")
    st, _ = req("POST", "/api/erm/bcm/processes", worker, {"name": "x worker", "ownerId": "x", "rtoHours": 4, "mtpdHours": 8, "impactProfile": [], "linkedRiskIds": []})
    check("worker DENIED create process (403)", st == 403, f"status={st}")

# resolve an owner id
_, pl = req("GET", "/api/erm/bcm/processes", farhan)
owner = pl["items"][0]["ownerId"]

# ── BIA validation guards ────────────────────────────────────────────────────
st, j = req("POST", "/api/erm/bcm/processes", farhan, {
    "name": "QA bad impact", "ownerId": owner, "rtoHours": 4, "mtpdHours": 8,
    "impactProfile": [{"dimension": "FINANCIAL", "at4h": 4, "at24h": 3, "at7d": 2, "at30d": 1}], "linkedRiskIds": [],
})
check("BIA impact-profile non-decreasing enforced (400)", st == 400, f"status={st} body={j}")
st, j = req("POST", "/api/erm/bcm/processes", farhan, {
    "name": "QA override no justif", "ownerId": owner, "rtoHours": 100, "mtpdHours": 200,
    "criticalityOverride": "VITAL", "impactProfile": [], "linkedRiskIds": [],
})
check("BIA criticality override requires justification (400)", st == 400, f"status={st}")
st, _ = req("GET", "/api/erm/bcm/processes/nonexistent-id", farhan)
check("BIA get nonexistent process (404)", st == 404, f"status={st}")
st, _ = req("POST", "/api/erm/bcm/processes/nonexistent/dependencies", farhan, {"dependencyType": "EQUIPMENT", "name": "x"})
check("BIA add dependency to nonexistent process (404)", st == 404, f"status={st}")
st, _ = req("DELETE", "/api/erm/bcm/dependencies/nonexistent", farhan)
check("BIA delete nonexistent dependency (404)", st == 404, f"status={st}")
st, j = req("POST", "/api/erm/bcm/processes", farhan, {"name": "ab", "ownerId": owner, "rtoHours": 4, "mtpdHours": 8, "impactProfile": [], "linkedRiskIds": []})
check("BIA name min-length (3) enforced (422)", st == 422, f"status={st}")

# valid override WITH justification works + lands on overridden criticality
st, j = req("POST", "/api/erm/bcm/processes", farhan, {
    "name": "QA override ok", "ownerId": owner, "rtoHours": 100, "mtpdHours": 200,
    "criticalityOverride": "VITAL", "criticalityOverrideJustification": "regulatory driver", "impactProfile": [], "linkedRiskIds": [],
})
check("BIA override w/ justification -> criticality=VITAL despite rto=100", st == 201 and j.get("criticality") == "VITAL", f"status={st} crit={(j or {}).get('criticality')}")

# ── Plan guards ──────────────────────────────────────────────────────────────
st, _ = req("GET", "/api/erm/bcm/plans/nonexistent", farhan)
check("plan get nonexistent (404)", st == 404, f"status={st}")
# create a draft, then test submit/approve/edit-fork
st, npl = req("POST", "/api/erm/bcm/plans", farhan, {"title": "QA deep plan", "planType": "BUSINESS_CONTINUITY", "ownerId": owner, "coveredProcessIds": [], "scopeStatement": "", "activationCriteria": [], "sections": [], "strategySummary": "", "recoveryTasks": []})
plan_id = (npl or {}).get("id")
check("plan create draft (201)", st == 201 and (npl or {}).get("status") == "DRAFT", f"status={st}")
if plan_id:
    st, _ = req("POST", f"/api/erm/bcm/plans/{plan_id}/submit", farhan)
    st2, _ = req("POST", f"/api/erm/bcm/plans/{plan_id}/submit", farhan)
    check("plan submit from non-DRAFT rejected (400)", st2 == 400, f"second submit status={st2}")
    st3, ap = req("POST", f"/api/erm/bcm/plans/{plan_id}/approve", farhan)
    check("plan approve from IN_REVIEW (200, version bump)", st3 == 200 and ap["status"] == "APPROVED" and ap["version"] == 2, f"status={st3}")
    # edit APPROVED forks DRAFT
    st4, ed = req("PATCH", f"/api/erm/bcm/plans/{plan_id}", farhan, {"title": "QA deep plan v2", "planType": "BUSINESS_CONTINUITY", "ownerId": owner, "coveredProcessIds": [], "scopeStatement": "edited", "activationCriteria": [], "sections": [], "strategySummary": "", "recoveryTasks": []})
    check("plan edit-APPROVED forks DRAFT", st4 == 200 and ed["status"] == "DRAFT", f"status={st4} state={(ed or {}).get('status')}")

# ── Crisis guards + immutability-after-close ─────────────────────────────────
st, crx = req("POST", "/api/erm/bcm/crisis/activate", farhan, {"title": "QA deep crisis", "siteId": None, "activatedPlanIds": [], "severityLevel": 1, "linkedRiskIds": [], "linkedIncidentId": None})
cid = (crx or {}).get("id")
check("crisis activate (201)", st == 201, f"status={st} body={crx}")
# severity bounds
st, _ = req("POST", f"/api/erm/bcm/crisis/{cid}/severity", farhan, {"severityLevel": 0})
check("crisis severity 0 rejected (422)", st == 422, f"status={st}")
st, _ = req("POST", f"/api/erm/bcm/crisis/{cid}/severity", farhan, {"severityLevel": 4})
check("crisis severity 4 rejected (422)", st == 422, f"status={st}")
if cid:
    # log on active OK + flips ACTIVATED->MANAGED
    st, _ = req("POST", f"/api/erm/bcm/crisis/{cid}/log", farhan, {"entryType": "ACTION", "content": "qa log"})
    check("crisis log on active (201)", st == 201, f"status={st}")
    # close gate: no note + no capa -> 400
    st, _ = req("POST", f"/api/erm/bcm/crisis/{cid}/close", farhan, {})
    check("crisis close without review rejected (400)", st == 400, f"status={st}")
    # close with note -> 200
    st, cd = req("POST", f"/api/erm/bcm/crisis/{cid}/close", farhan, {"reviewNote": "no further actions"})
    check("crisis close with note (200, CLOSED)", st == 200 and cd["status"] == "CLOSED", f"status={st}")
    # IMMUTABILITY: closed crisis must reject log / stand-down / severity
    st, _ = req("POST", f"/api/erm/bcm/crisis/{cid}/log", farhan, {"entryType": "ACTION", "content": "after close"})
    check("closed crisis log sealed (400)", st == 400, f"status={st}")
    st, _ = req("POST", f"/api/erm/bcm/crisis/{cid}/stand-down", farhan)
    check("closed crisis stand-down rejected (400)", st == 400, f"status={st}")
    st, _ = req("POST", f"/api/erm/bcm/crisis/{cid}/severity", farhan, {"severityLevel": 2})
    check("closed crisis severity change rejected (400)", st == 400, f"status={st}")

# ── Exercise guards ──────────────────────────────────────────────────────────
_, ex = req("GET", "/api/erm/bcm/exercises", farhan)
ct = next((e for e in ex["items"] if e["exerciseType"] == "CALL_TREE_TEST"), None)
non_ct_planned = next((e for e in ex["items"] if e["status"] != "COMPLETED" and e["exerciseType"] != "CALL_TREE_TEST"), None)
# create a planned exercise to exercise the gates without touching seeded ones
st, nex = req("POST", "/api/erm/bcm/exercises", farhan, {"title": "QA deep exercise", "exerciseType": "TABLETOP", "scheduledDate": "2026-07-01T00:00:00Z", "testedPlanIds": [], "facilitatorId": owner, "participants": [], "objectives": []})
eid = (nex or {}).get("id")
check("exercise create (201)", st == 201, f"status={st}")
if eid:
    st, f1 = req("POST", f"/api/erm/bcm/exercises/{eid}/findings", farhan, {"description": "minor", "severity": "MINOR_GAP"})
    check("exercise add MINOR_GAP finding (201)", st == 201, f"status={st}")
    # complete with only a MINOR_GAP (no major) -> allowed
    st, _ = req("POST", f"/api/erm/bcm/exercises/{eid}/complete", farhan, {"outcome": "PARTIALLY_MET", "conductedDate": "2026-07-02T00:00:00Z", "rtoAchievedHours": 6, "reportRichText": "ok"})
    check("exercise complete w/ only MINOR_GAP (200)", st == 200, f"status={st}")
# major-gap gate on a fresh exercise
st, nex2 = req("POST", "/api/erm/bcm/exercises", farhan, {"title": "QA major gate", "exerciseType": "TABLETOP", "scheduledDate": "2026-07-01T00:00:00Z", "testedPlanIds": [], "facilitatorId": owner, "participants": [], "objectives": []})
eid2 = (nex2 or {}).get("id")
if eid2:
    st, fm = req("POST", f"/api/erm/bcm/exercises/{eid2}/findings", farhan, {"description": "major", "severity": "MAJOR_GAP"})
    fid = (fm or {}).get("id")
    st, _ = req("POST", f"/api/erm/bcm/exercises/{eid2}/complete", farhan, {"outcome": "NOT_MET", "conductedDate": "2026-07-02T00:00:00Z", "rtoAchievedHours": None, "reportRichText": ""})
    check("exercise complete blocked: MAJOR_GAP needs CAPA (400)", st == 400, f"status={st}")
    st, rc = req("POST", f"/api/erm/bcm/exercises/findings/{fid}/raise-capa", farhan)
    check("raise BC_EXERCISE CAPA from MAJOR_GAP (200/201)", st in (200, 201) and rc.get("capaId"), f"status={st}")
    # raising again should be idempotent (return existing) OR error, not silently dup
    st2, rc2 = req("POST", f"/api/erm/bcm/exercises/findings/{fid}/raise-capa", farhan)
    check("raise-capa again does not create a 2nd CAPA on same finding", (st2 != 200 and st2 != 201) or (rc2.get("capaId") == rc.get("capaId")), f"status={st2} capaId={rc2.get('capaId') if isinstance(rc2, dict) else rc2}")
    st, _ = req("POST", f"/api/erm/bcm/exercises/{eid2}/complete", farhan, {"outcome": "NOT_MET", "conductedDate": "2026-07-02T00:00:00Z", "rtoAchievedHours": None, "reportRichText": ""})
    check("exercise complete after CAPA raised (200)", st == 200, f"status={st}")
if ct:
    st, r = req("POST", f"/api/erm/bcm/exercises/{ct['id']}/run-call-tree-test", farhan)
    check("run-call-tree-test on completed CALL_TREE exercise (200/400 graceful)", st in (200, 400), f"status={st}")

# ── Scenario + Horizon ───────────────────────────────────────────────────────
_, sc = req("GET", "/api/erm/bcm/scenarios", farhan)
scn = sc[0]
st, hm = req("GET", f"/api/erm/bcm/scenarios/{scn['id']}/stressed-heatmap", farhan)
check("stressed-heatmap baseline=25 + movements key", st == 200 and len(hm["baseline"]) == 25 and "movements" in hm, f"status={st}")
st, _ = req("GET", "/api/erm/bcm/scenarios/nonexistent/stressed-heatmap", farhan)
check("stressed-heatmap nonexistent scenario (404)", st == 404, f"status={st}")
# scenario create: enum timeHorizon OK; empty/free-text rejected (form-fix regression)
st, _ = req("POST", "/api/erm/bcm/scenarios", farhan, {"title": "QA scn ok", "category": "CYBER_ATTACK", "narrative": "n", "probabilityQualitative": "POSSIBLE", "timeHorizon": "1_3_YEARS", "affectedRiskIds": [], "affectedProcessIds": [], "impactEstimates": [], "whatIfAdjustments": []})
check("scenario create w/ enum timeHorizon (201)", st == 201, f"status={st}")
st, _ = req("POST", "/api/erm/bcm/scenarios", farhan, {"title": "QA scn bad", "category": "CYBER_ATTACK", "narrative": "n", "probabilityQualitative": "POSSIBLE", "timeHorizon": "", "affectedRiskIds": [], "affectedProcessIds": [], "impactEstimates": [], "whatIfAdjustments": []})
check("scenario create w/ empty timeHorizon rejected (422)", st == 422, f"status={st}")
# horizon disposition validation + promote
_, hz = req("GET", "/api/erm/bcm/horizon", farhan)
open_hz = next((h for h in hz if not h["disposition"]), None)
st, h2 = req("POST", "/api/erm/bcm/horizon", farhan, {"title": "QA deep horizon", "description": "", "category": "CYBER_ATTACK", "signalStrength": "WEAK", "potentialCategoryIds": []})
hid = (h2 or {}).get("id")
check("horizon create (201)", st == 201, f"status={st}")
if hid:
    st, _ = req("POST", f"/api/erm/bcm/horizon/{hid}/disposition", farhan, {"disposition": "INVALID", "note": "x"})
    check("horizon invalid disposition (422)", st == 422, f"status={st}")
    st, dh = req("POST", f"/api/erm/bcm/horizon/{hid}/disposition", farhan, {"disposition": "PROMOTED_TO_SCENARIO", "note": "promote"})
    check("horizon PROMOTED_TO_SCENARIO sets promotedEntityId", st == 200 and dh.get("promotedEntityId"), f"status={st} body={dh}")

print(f"\n== DEEP QA RESULT: {P} passed, {F} failed ==")
if F:
    raise SystemExit(1)
