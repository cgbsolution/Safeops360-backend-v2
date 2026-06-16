"""ERM Phase 3 (BCM) end-to-end HTTP smoke test.

Hits the live backend on :8077 as Farhan (BCM Coordinator). Exercises every BCM
read (serialization / MissingGreenlet), the stressed-heatmap engine, the key
write paths (create / patch / approve / submit / DIMS round-trip / BC_EXERCISE
CAPA raise / append-only crisis log) and the six-source CAPA regression.

Run after the Phase-3 seed. Re-seed afterwards to restore curated demo data.
"""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8077"
PASS = 0
FAIL = 0
NOTES = []


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
            return e.code, json.loads(raw) if raw else None
        except Exception:
            return e.code, {"raw": raw}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  -- {detail}")


print("== ERM Phase 3 BCM smoke test ==")

# 1. Login
st, j = req("POST", "/api/auth/login", body={"email": "farhan.qureshi@safeops360.in", "password": "demo123"})
check("login farhan (BCM Coordinator)", st == 200 and j and j.get("access_token"), f"status={st} body={j}")
token = j.get("access_token") if j else None
if not token:
    print("Cannot continue without token"); raise SystemExit(1)

# 2. Dashboard
st, d = req("GET", "/api/erm/bcm/dashboard", token)
check("GET dashboard 200", st == 200, f"status={st}")
if st == 200:
    check("dashboard totalCritical=14", d["totalCritical"] == 14, d.get("totalCritical"))
    check("dashboard coveredCritical=12", d["coveredCritical"] == 12, d.get("coveredCritical"))
    check("dashboard coveragePct~85.7", abs(d["coveragePct"] - 85.7) < 0.2, d.get("coveragePct"))
    check("dashboard unmitigatedSpofs=3", d["unmitigatedSpofs"] == 3, d.get("unmitigatedSpofs"))
    check("dashboard coverageGaps=2", len(d["coverageGaps"]) == 2, len(d.get("coverageGaps", [])))
    check("dashboard activeCrises=0 (CRX closed)", d["activeCrises"] == 0, d.get("activeCrises"))
    check("dashboard exerciseProgramme present", len(d["exerciseProgramme"]) == 6, len(d.get("exerciseProgramme", [])))

# 3. Processes list
st, pl = req("GET", "/api/erm/bcm/processes", token)
check("GET processes 200", st == 200, f"status={st}")
check("processes total=14", st == 200 and pl["total"] == 14, pl.get("total") if st == 200 else st)
bp1 = next((p for p in pl["items"] if p["processCode"] == "BP-0001"), None) if st == 200 else None
check("BP-0001 present + isCovered", bp1 is not None and bp1["isCovered"], bp1)

# 4. Process detail + DIMS round-trip PATCH (the seed dims fix)
if bp1:
    st, pd = req("GET", f"/api/erm/bcm/processes/{bp1['id']}", token)
    check("GET process detail 200", st == 200, f"status={st}")
    dims_ok = st == 200 and all(r.get("dimension") in ("FINANCIAL", "REPUTATIONAL", "REGULATORY", "SAFETY", "BUSINESS_INTERRUPTION") for r in pd.get("impactProfile", []))
    check("impactProfile uses canonical DIMS enum", dims_ok, pd.get("impactProfile") if st == 200 else st)
    if st == 200:
        body = {
            "name": pd["name"], "description": pd["description"], "siteId": pd["siteId"], "ownerId": pd["ownerId"],
            "departmentName": pd["departmentName"], "rtoHours": pd["rtoHours"], "rpoHours": pd["rpoHours"],
            "mtpdHours": pd["mtpdHours"], "peakPeriods": pd["peakPeriods"], "impactProfile": pd["impactProfile"],
            "linkedRiskIds": pd["linkedRiskIds"],
        }
        st2, _ = req("PATCH", f"/api/erm/bcm/processes/{bp1['id']}", token, body)
        check("PATCH process round-trips impactProfile (DIMS valid)", st2 == 200, f"status={st2}")

# 5. Dependency map (SPOF node count)
st, dm = req("GET", "/api/erm/bcm/dependency-map", token)
check("GET dependency-map 200", st == 200, f"status={st}")
if st == 200:
    spof_nodes = [n for n in dm["nodes"] if n["nodeType"] != "PROCESS" and n.get("isSpof")]
    proc_nodes = [n for n in dm["nodes"] if n["nodeType"] == "PROCESS"]
    check("dep-map PROCESS nodes=14", len(proc_nodes) == 14, len(proc_nodes))
    check("dep-map unmitigated-SPOF nodes=3", len(spof_nodes) == 3, len(spof_nodes))

# 6. Plans
st, pls = req("GET", "/api/erm/bcm/plans", token)
check("GET plans 200", st == 200, f"status={st}")
if st == 200:
    check("plans total=8", pls["total"] == 8, pls.get("total"))
    check("plans APPROVED=7", pls["statusCounts"].get("APPROVED") == 7, pls["statusCounts"])
    plan0 = pls["items"][0]
    st2, pdt = req("GET", f"/api/erm/bcm/plans/{plan0['id']}", token)
    check("GET plan detail 200", st2 == 200, f"status={st2}")
    check("plan detail recoveryTasks serialize", st2 == 200 and isinstance(pdt.get("recoveryTasks"), list), pdt.get("recoveryTasks") if st2 == 200 else st2)

# 7. Crisis list + detail (the heavy serialization: cached content, roster, fser, log)
st, cl = req("GET", "/api/erm/bcm/crisis", token)
check("GET crisis list 200", st == 200, f"status={st}")
crx = cl[0] if st == 200 and cl else None
check("crisis CRX-2026-0001 present", crx and crx["crisisCode"] == "CRX-2026-0001", crx)
if crx:
    st, cd = req("GET", f"/api/erm/bcm/crisis/{crx['id']}", token)
    check("GET crisis detail 200", st == 200, f"status={st}")
    if st == 200:
        check("crisis logEntries=23", len(cd["logEntries"]) == 23, len(cd["logEntries"]))
        check("crisis teamRoster present", len(cd["teamRoster"]) > 0, len(cd.get("teamRoster", [])))
        check("crisis cachedPlanContent present", len(cd["cachedPlanContent"]) >= 1, len(cd.get("cachedPlanContent", [])))
        check("crisis recoveryTasks merged from plan", len(cd["recoveryTasks"]) >= 1, len(cd.get("recoveryTasks", [])))
        check("crisis fserPanel key present (null ok)", "fserPanel" in cd, list(cd.keys()))
        # CLOSED crisis log is sealed — appends must be rejected (legal record).
        st2, le = req("POST", f"/api/erm/bcm/crisis/{crx['id']}/log", token, {"entryType": "STATUS_UPDATE", "content": "QA smoke test log entry."})
        check("closed-crisis log append rejected (sealed)", st2 == 400, f"status={st2} body={le}")
        st3, cd2 = req("GET", f"/api/erm/bcm/crisis/{crx['id']}", token)
        check("crisis log count stays 23 (sealed)", st3 == 200 and len(cd2["logEntries"]) == 23, len(cd2["logEntries"]) if st3 == 200 else st3)

# 8. Exercises
st, ex = req("GET", "/api/erm/bcm/exercises", token)
check("GET exercises 200", st == 200, f"status={st}")
check("exercises total=6", st == 200 and ex["total"] == 6, ex.get("total") if st == 200 else st)
planned = next((e for e in ex["items"] if e["status"] == "PLANNED"), None) if st == 200 else None
ct_test = next((e for e in ex["items"] if e["exerciseType"] == "CALL_TREE_TEST"), None) if st == 200 else None
check("call-tree test has callTreeStats", ct_test and ct_test.get("callTreeStats") and ct_test["callTreeStats"].get("notified") == 42, ct_test.get("callTreeStats") if ct_test else None)

# 9. Scenarios + stressed heatmap engine
st, sc = req("GET", "/api/erm/bcm/scenarios", token)
check("GET scenarios 200", st == 200, f"status={st}")
check("scenarios=6", st == 200 and len(sc) == 6, len(sc) if st == 200 else st)
scn1 = next((s for s in sc if s["scenarioCode"] == "SCN-0001"), None) if st == 200 else None
if scn1:
    st2, hm = req("GET", f"/api/erm/bcm/scenarios/{scn1['id']}/stressed-heatmap", token)
    check("GET stressed-heatmap 200", st2 == 200, f"status={st2}")
    if st2 == 200:
        check("stressed-heatmap baseline 25 cells", len(hm["baseline"]) == 25, len(hm["baseline"]))
        check("stressed-heatmap has movements", len(hm["movements"]) > 0, len(hm.get("movements", [])))

# 10. Horizon
st, hz = req("GET", "/api/erm/bcm/horizon", token)
check("GET horizon 200", st == 200, f"status={st}")
check("horizon=5", st == 200 and len(hz) == 5, len(hz) if st == 200 else st)

# 11. Crisis team + call trees
st, tm = req("GET", "/api/erm/bcm/crisis-team", token)
check("GET crisis-team 200", st == 200, f"status={st}")
check("crisis-team=6, no vacancy", st == 200 and len(tm) == 6 and not any(t["vacancy"] for t in tm), tm if st == 200 else st)
st, ctr = req("GET", "/api/erm/bcm/call-trees", token)
check("GET call-trees 200, 2 published", st == 200 and len(ctr) == 2 and all(c["publishedAt"] for c in ctr), ctr if st == 200 else st)

# 12. Six-source CAPA regression
st, cats = req("GET", "/api/capa/source-categories", token)
check("GET capa source-categories 200", st == 200, f"status={st}")
cat_codes = {c["code"] for c in cats} if st == 200 else set()
check("BC_EXERCISE category present", "BC_EXERCISE" in cat_codes, cat_codes)
check("COMPLIANCE category present", "COMPLIANCE" in cat_codes, cat_codes)
check("RISK_TREATMENT category present", "RISK_TREATMENT" in cat_codes, cat_codes)
check("six-source: >=6 CAPA source categories", len(cat_codes) >= 6, len(cat_codes))
st, types = req("GET", "/api/capa/source-types", token)
type_codes = {t["code"] for t in types} if st == 200 else set()
check("source-types include BC_EXERCISE+COMPLIANCE+RISK_TREATMENT", {"BC_EXERCISE", "COMPLIANCE", "RISK_TREATMENT"} <= type_codes, type_codes)
st, caps = req("GET", "/api/capa?sourceType=BC_EXERCISE", token)
check("CAPA list filtered BC_EXERCISE serializes", st == 200 and caps.get("total", 0) >= 1, caps.get("total") if st == 200 else st)
st, vol = req("GET", "/api/capa/dashboard/volume-by-source", token)
check("CAPA volume-by-source aggregates (incl BC_EXERCISE) 200", st == 200, f"status={st}")

# 13. WRITE paths — create process (re-seed cleans up)
st, np = req("POST", "/api/erm/bcm/processes", token, {
    "name": "QA Smoke Test Process", "description": "temp", "siteId": None, "ownerId": bp1["ownerId"],
    "departmentName": "QA", "rtoHours": 3, "rpoHours": 1, "mtpdHours": 12, "impactProfile": [], "linkedRiskIds": [],
})
check("POST create process 201 (VITAL from rto=3)", st == 201 and np.get("criticality") == "VITAL", f"status={st} body={np}")
new_pid = np.get("id") if st == 201 else None
if new_pid:
    st2, _ = req("POST", f"/api/erm/bcm/processes/{new_pid}/approve", token)
    check("POST approve BIA 200", st2 == 200, f"status={st2}")
    st3, dep = req("POST", f"/api/erm/bcm/processes/{new_pid}/dependencies", token, {
        "dependencyType": "EQUIPMENT", "name": "QA dep", "isSinglePointOfFailure": True, "workaround": None, "workaroundDurationHours": None,
    })
    check("POST add dependency 201", st3 == 201 and dep.get("unmitigatedSpof") is True, f"status={st3} body={dep}")
    # MTPD < RTO guard
    st4, _ = req("POST", "/api/erm/bcm/processes", token, {"name": "Bad MTPD", "ownerId": bp1["ownerId"], "rtoHours": 10, "mtpdHours": 5, "impactProfile": [], "linkedRiskIds": []})
    check("create process MTPD<RTO rejected 400", st4 == 400, f"status={st4}")

# 14. WRITE — plan lifecycle
st, npl = req("POST", "/api/erm/bcm/plans", token, {
    "title": "QA Smoke Plan", "planType": "BUSINESS_CONTINUITY", "siteId": None, "ownerId": bp1["ownerId"],
    "coveredProcessIds": [new_pid] if new_pid else [], "scopeStatement": "qa", "activationCriteria": ["c1"],
    "sections": [{"orderIndex": 0, "heading": "H1", "contentRichText": "x", "attachments": []}], "strategySummary": "s",
    "recoveryTasks": [{"orderIndex": 0, "title": "t1", "responsibleRoleName": "Lead", "targetHoursFromActivation": 2}],
})
check("POST create plan 201 (DRAFT)", st == 201 and npl.get("status") == "DRAFT", f"status={st} body={npl}")
new_plan = npl.get("id") if st == 201 else None
if new_plan:
    st2, sub = req("POST", f"/api/erm/bcm/plans/{new_plan}/submit", token)
    check("POST submit plan -> IN_REVIEW", st2 == 200 and sub["status"] == "IN_REVIEW", f"status={st2}")
    st3, app = req("POST", f"/api/erm/bcm/plans/{new_plan}/approve", token)
    check("POST approve plan -> APPROVED + version bump + snapshot", st3 == 200 and app["status"] == "APPROVED" and app["version"] == 2 and len(app["versionSnapshots"]) >= 1, f"status={st3} v={app.get('version')} snaps={len(app.get('versionSnapshots', []))}")

# 15. WRITE — exercise finding MAJOR_GAP + raise BC_EXERCISE CAPA + complete-gate
if planned:
    st, f = req("POST", f"/api/erm/bcm/exercises/{planned['id']}/findings", token, {"description": "QA major gap", "severity": "MAJOR_GAP"})
    check("POST exercise finding 201", st == 201, f"status={st} body={f}")
    fid = f.get("id") if st == 201 else None
    # completing with an un-CAPA'd MAJOR_GAP must be blocked
    st_c, _ = req("POST", f"/api/erm/bcm/exercises/{planned['id']}/complete", token, {"outcome": "NOT_MET", "rtoAchievedHours": None, "reportRichText": ""})
    check("complete blocked while MAJOR_GAP lacks CAPA (400)", st_c == 400, f"status={st_c}")
    if fid:
        st2, rc = req("POST", f"/api/erm/bcm/exercises/findings/{fid}/raise-capa", token)
        check("POST raise BC_EXERCISE CAPA from finding", st2 in (200, 201) and rc.get("capaId"), f"status={st2} body={rc}")
        st3, caps2 = req("GET", "/api/capa?sourceType=BC_EXERCISE", token)
        check("BC_EXERCISE CAPA count now 2", st3 == 200 and caps2["total"] == 2, caps2.get("total") if st3 == 200 else st3)

# 16. WRITE — raise SPOF as risk (T3-03) — capture for cleanup
raised_risk_code = None
if new_pid:
    st, rr = req("POST", f"/api/erm/bcm/processes/{new_pid}/raise-risk", token)
    check("POST raise SPOF as risk -> draft EnterpriseRisk", st == 200 and rr.get("riskCode"), f"status={st} body={rr}")
    raised_risk_code = rr.get("riskCode") if st == 200 else None
    NOTES.append(f"raised risk to clean up: {raised_risk_code}")

print(f"\n== RESULT: {PASS} passed, {FAIL} failed ==")
for n in NOTES:
    print("  note:", n)
if FAIL:
    raise SystemExit(1)
