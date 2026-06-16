"""ERM Tier 3 (Controls · Vendor · Insurance) QA — TT-01..18 + eight-source CAPA.

Hits the live backend on :8077 as the relevant personas. Re-seed Tier 3 after.
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
    return (j or {}).get("access_token")


print("== ERM Tier 3 QA ==")
cro = login("anand.krishnan@safeops360.in")
ravi = login("ravi.menon@safeops360.in")
sneha = login("sneha.kulkarni@safeops360.in")
aditya = login("aditya.bose@safeops360.in")
worker = login("worker.it.nw@safeops360.in")
check("logins (CRO/Ravi/Sneha/Aditya/worker)", all([cro, ravi, sneha, aditya, worker]))

# ── TT-17 RBAC denial ─────────────────────────────────────────────────────────
check("worker DENIED controls dashboard (403)", req("GET", "/api/erm/controls/dashboard", worker)[0] == 403)
check("worker DENIED vendors (403)", req("GET", "/api/erm/vendors", worker)[0] == 403)
check("worker DENIED insurance (403)", req("GET", "/api/erm/insurance/policies", worker)[0] == 403)
# specialist cross-module isolation: Insurance Mgr cannot write controls
check("Insurance Mgr cannot create control (403)", req("POST", "/api/erm/controls", aditya, {"name": "x ins", "controlType": "PREVENTIVE", "nature": "MANUAL", "frequency": "MONTHLY", "category": "OPERATIONAL", "controlOwnerId": "x"})[0] == 403)

# ── CONTROLS dashboard + facts ────────────────────────────────────────────────
st, cd = req("GET", "/api/erm/controls/dashboard", ravi)
check("controls dashboard 200", st == 200, st)
if st == 200:
    check("dashboard keyControls=16", cd["keyControls"] == 16, cd.get("keyControls"))
    check("dashboard materialWeaknesses=1", cd["materialWeaknesses"] == 1, cd.get("materialWeaknesses"))
    check("dashboard effectivePct=87.5 (14/16)", abs(cd["effectivePct"] - 87.5) < 0.6, cd.get("effectivePct"))
    check("dashboard overdueTests=3", cd["overdueTests"] == 3, cd.get("overdueTests"))
    check("TT-04 unreported MW listed", len(cd["unreportedMaterialWeaknesses"]) == 1, cd.get("unreportedMaterialWeaknesses"))

# ── TT-05 matrix: 0011 no-primary + orphans ──────────────────────────────────
st, mx = req("GET", "/api/erm/controls/matrix", ravi)
check("matrix 200", st == 200, st)
if st == 200:
    r0011 = next((r for r in mx["rows"] if r["riskCode"] == "ERM-2026-0011"), None)
    check("TT-05 risk 0011 has NO primary control", r0011 is not None and not r0011["hasPrimaryControl"], r0011)
    check("TT-05 orphan controls listed", len(mx["orphanControls"]) >= 1, len(mx.get("orphanControls", [])))
    # TT-06 a primary-deficient risk exists (IT access → 0018, hedging → 0004)
    deficient_rows = [r for r in mx["rows"] if r["primaryControlDeficient"]]
    check("TT-06 primary-control-deficient flagged (>=1)", len(deficient_rows) >= 1, len(deficient_rows))

# ── controls list + a control for testing ────────────────────────────────────
st, cl = req("GET", "/api/erm/controls", ravi)
controls = cl["items"] if st == 200 else []
check("controls list total=22", st == 200 and cl["total"] == 22, cl.get("total") if st == 200 else st)
# pick an EFFECTIVE key control to test (Ravi is tester, owner != Ravi)
target = next((c for c in controls if c["isKeyControl"] and c["currentOperatingRating"] == "EFFECTIVE"), None)

# ── TT-01 segregation: test plan + test where tester == owner blocked ─────────
if target:
    # test-plan assigning the owner as tester → 400
    st, _ = req("POST", f"/api/erm/controls/{target['id']}/test-plans", ravi, {"testCycleLabel": "FY27-QA", "testMethod": "INQUIRY", "sampleSizePlanned": 5, "testFrequencyPerYear": 2, "assignedTesterId": target["controlOwnerId"], "scheduledDate": "2026-07-01T00:00:00Z"})
    check("TT-01 test-plan: tester==owner blocked (400)", st == 400, f"status={st}")
    # valid test-plan (Ravi != owner)
    st, _ = req("POST", f"/api/erm/controls/{target['id']}/test-plans", ravi, {"testCycleLabel": "FY27-QA", "testMethod": "INQUIRY", "sampleSizePlanned": 5, "testFrequencyPerYear": 2, "assignedTesterId": ravi, "scheduledDate": "2026-07-01T00:00:00Z"})
    check("TT-01 valid test-plan (Ravi!=owner) 201", st == 201, f"status={st}")

# a control owned BY Ravi would block him testing it — find/none; instead test the
# record-test segregation by using a CRO-token to record on a control CRO owns.
st, cl2 = req("GET", "/api/erm/controls", cro)
cro_owned = next((c for c in (cl2["items"] if st == 200 else []) if c["controlOwnerId"] and c["controlOwnerName"] and "anand" in (c["controlOwnerName"] or "").lower()), None)
# The "board-level risk appetite monitoring" control is owned by CRO (anand)
if cro_owned:
    st, _ = req("POST", f"/api/erm/controls/{cro_owned['id']}/tests", cro, {"testType": "OPERATING", "testDate": "2026-06-10T00:00:00Z", "method": "INQUIRY", "sampleSize": 5, "exceptionsFound": 0, "conclusion": "EFFECTIVE", "workpaperNotes": "qa"})
    check("TT-01 record-test on own control blocked (400)", st == 400, f"status={st} (owner cannot test own control)")

# ── TT-02 + TT-03: record DEFICIENT test → deficiency → CAPA gate → retest close
if target:
    st, t = req("POST", f"/api/erm/controls/{target['id']}/tests", ravi, {"testType": "OPERATING", "testDate": "2026-06-12T00:00:00Z", "method": "REPERFORMANCE", "sampleSize": 25, "exceptionsFound": 6, "conclusion": "SIGNIFICANT_DEFICIENCY", "workpaperNotes": "QA: exceptions found", "deficiencyDescription": "QA significant deficiency"})
    check("record SIGNIFICANT_DEFICIENCY test 201 + auto-deficiency", st == 201 and t.get("deficiencyId"), f"status={st}")
    st2, cdetail = req("GET", f"/api/erm/controls/{target['id']}", ravi)
    check("TT-02 operating rating rolled to DEFICIENT from latest test", st2 == 200 and cdetail["currentOperatingRating"] == "DEFICIENT", cdetail.get("currentOperatingRating") if st2 == 200 else st2)
    did = t.get("deficiencyId") if st == 201 else None
    if did:
        # TT-03: leaving OPEN without CAPA blocked
        st, _ = req("PATCH", f"/api/erm/controls/deficiencies/{did}?status=RETESTING", ravi)
        check("TT-03 SIGNIFICANT_DEFICIENCY can't leave OPEN w/o CAPA (400)", st == 400, f"status={st}")
        st, rc = req("POST", f"/api/erm/controls/deficiencies/{did}/raise-capa", ravi)
        check("TT-03 raise CONTROL_DEFICIENCY CAPA (200)", st == 200 and rc.get("capaId"), f"status={st}")
        # CLOSED without retest blocked
        st, _ = req("PATCH", f"/api/erm/controls/deficiencies/{did}?status=CLOSED", ravi)
        check("TT-03 CLOSE without passing retest blocked (400)", st == 400, f"status={st}")
        # passing retest then close
        req("POST", f"/api/erm/controls/{target['id']}/tests", ravi, {"testType": "OPERATING", "testDate": "2026-06-14T00:00:00Z", "method": "REPERFORMANCE", "sampleSize": 25, "exceptionsFound": 0, "conclusion": "EFFECTIVE", "workpaperNotes": "QA retest pass"})
        st, _ = req("PATCH", f"/api/erm/controls/deficiencies/{did}?status=CLOSED", ravi)
        check("TT-03 CLOSE after passing retest (200)", st == 200, f"status={st}")

# ── TT-04 material-weakness report is CRO-only ────────────────────────────────
st, defs = req("GET", "/api/erm/controls/deficiencies?severity=MATERIAL_WEAKNESS", cro)
mw = defs["items"][0] if st == 200 and defs["items"] else None
if mw:
    st, _ = req("POST", f"/api/erm/controls/deficiencies/{mw['id']}/report", ravi, {"auditCommitteeReference": "x"})
    check("TT-04 non-CRO cannot report MW (403)", st == 403, f"status={st}")
    st, r = req("POST", f"/api/erm/controls/deficiencies/{mw['id']}/report", cro, {"auditCommitteeReference": "Audit Committee 20-Jun-2026, Item 5"})
    check("TT-04 CRO reports MW (200, flag set)", st == 200 and r.get("reportedToAuditCommittee"), f"status={st}")

# ── VENDOR: TT-07 dual-lens, TT-10 ESG portfolio ──────────────────────────────
st, vd = req("GET", "/api/erm/vendors/dashboard", sneha)
check("vendor dashboard 200", st == 200, st)
if st == 200:
    check("TT-10 spend-weighted LAGGING ~10%", 8 <= vd["spendWeightedLaggingPct"] <= 12, vd.get("spendWeightedLaggingPct"))
    check("dual-lens distributions present", bool(vd["riskBandDistribution"]) and bool(vd["esgBandDistribution"]), vd)
st, ep = req("GET", "/api/erm/vendors/esg-portfolio", sneha)
check("TT-10 esg-portfolio aggregates + watchlist", st == 200 and ep["laggingSpendPct"] > 0 and len(ep["laggingWatchlist"]) == 1, ep.get("laggingSpendPct") if st == 200 else st)
st, vl = req("GET", "/api/erm/vendors", sneha)
vendors = vl["items"] if st == 200 else []
check("vendors total=16", st == 200 and vl["total"] == 16, vl.get("total") if st == 200 else st)
polymer = next((v for v in vendors if v["criticality"] == "STRATEGIC"), None)
if polymer:
    st, vdet = req("GET", f"/api/erm/vendors/{polymer['id']}", sneha)
    check("TT-07 vendor has both RISK + ESG current scores", st == 200 and vdet["currentRiskBand"] and vdet["currentEsgBand"], vdet.get("currentRiskBand") if st == 200 else st)
    # TT-08 approval blocked (open CRITICAL_GAP) unless CONDITIONAL+CRO
    st, _ = req("POST", f"/api/erm/vendors/{polymer['id']}/onboarding", sneha, {"onboardingStatus": "APPROVED", "note": "x"})
    check("TT-08 APPROVE strategic vendor w/ open gap blocked (400)", st == 400, f"status={st}")
    st, _ = req("POST", f"/api/erm/vendors/{polymer['id']}/onboarding", sneha, {"onboardingStatus": "CONDITIONAL", "note": "x"})
    check("TT-08 CONDITIONAL by non-CRO blocked (403)", st == 403, f"status={st}")
    st, _ = req("POST", f"/api/erm/vendors/{polymer['id']}/onboarding", cro, {"onboardingStatus": "CONDITIONAL", "note": "CRO accepts pending DR remediation"})
    check("TT-08 CONDITIONAL by CRO allowed (200)", st == 200, f"status={st}")
    # TT-11 raise-as-risk
    st, rr = req("POST", f"/api/erm/vendors/{polymer['id']}/raise-risk", sneha)
    check("TT-11 vendor raise-as-risk → SCM EnterpriseRisk", st == 200 and rr.get("riskCode"), f"status={st}")

# ── TT-09 vendor CRITICAL_GAP requires VENDOR_RISK CAPA (already seeded; verify it exists)
st, caps = req("GET", "/api/capa?sourceType=VENDOR_RISK", cro)
check("TT-09 VENDOR_RISK CAPA present", st == 200 and caps.get("total", 0) >= 1, caps.get("total") if st == 200 else st)

# ── INSURANCE: TT-13 status, TT-15 reconcile, TT-16 gap ───────────────────────
st, idash = req("GET", "/api/erm/insurance/dashboard", aditya)
check("insurance dashboard 200", st == 200, st)
if st == 200:
    check("TT-13 expiringSoon>=1 (Cyber)", idash["expiringSoon"] >= 1, idash.get("expiringSoon"))
    check("TT-16 uncoveredCriticalRisks>=1", idash["uncoveredCriticalRisks"] >= 1, idash.get("uncoveredCriticalRisks"))
st, pl = req("GET", "/api/erm/insurance/policies", aditya)
pols = pl["items"] if st == 200 else []
check("policies total=11", st == 200 and pl["total"] == 11, pl.get("total") if st == 200 else st)
check("TT-13 a LAPSED policy present", any(p["status"] == "LAPSED" for p in pols), [p["status"] for p in pols])
check("TT-13 Cyber EXPIRING_SOON", any(p["policyType"] == "CYBER" and p["status"] == "EXPIRING_SOON" for p in pols))
# TT-15 claim reconcile: find the SETTLED claim w/ a loss event
cyber = next((p for p in pols if p["policyType"] == "CYBER"), None)
# find settled claim via a policy detail that has claims
settled_claim = None
for p in pols:
    st, pd = req("GET", f"/api/erm/insurance/policies/{p['id']}", aditya)
    if st == 200:
        sc = next((c for c in pd["claims"] if c["status"] == "SETTLED" and c["lossEventId"]), None)
        if sc:
            settled_claim = sc; break
check("settled claim w/ loss event found", settled_claim is not None)
if settled_claim:
    st, rec = req("POST", f"/api/erm/insurance/claims/{settled_claim['id']}/reconcile-loss", aditya)
    check("TT-15 reconcile writes recovery to loss event", st == 200 and rec.get("recoveredInr") == settled_claim["settledAmountInr"], f"status={st} body={rec}")
# TT-16 coverage gap
st, gaps = req("GET", "/api/erm/insurance/coverage-gap", aditya)
check("coverage-gap list 200", st == 200 and len(gaps) >= 1, st)
if st == 200 and gaps:
    g = gaps[0]
    check("TT-16 gap: 3 not fully transferred", g["uncoveredCount"] == 3, g.get("uncoveredCount"))
    uncovered_line = next((ln for ln in g["lines"] if ln["gapType"] == "UNCOVERED"), None)
    if uncovered_line:
        st, tr = req("POST", f"/api/erm/insurance/coverage-gap/raise-transfer?riskId={uncovered_line['riskId']}", aditya)
        check("TT-16 raise-transfer creates RISK_TREATMENT CAPA", st == 200 and tr.get("capaId"), f"status={st}")
# gap UNINSURABLE_ACCEPTED needs note
st2, _ = req("POST", "/api/erm/insurance/coverage-gap", aditya, {"assessmentCycleLabel": "QA", "reviewDate": "2026-06-15T00:00:00Z", "lines": [{"riskId": "x", "isInsurable": False, "coveredByPolicyIds": [], "gapType": "UNINSURABLE_ACCEPTED", "gapNotes": ""}], "summaryNotes": ""})
check("coverage-gap UNINSURABLE_ACCEPTED w/o note rejected (400)", st2 == 400, f"status={st2}")

# ── TT-18 EIGHT-source CAPA regression ────────────────────────────────────────
st, cats = req("GET", "/api/capa/source-categories", cro)
codes = {c["code"] for c in cats} if st == 200 else set()
expected8 = {"RISK_TREATMENT", "COMPLIANCE", "BC_EXERCISE", "CONTROL_DEFICIENCY", "VENDOR_RISK"}
check("TT-18 all 3 ERM CAPA sources + 2 new present", expected8 <= codes, codes)
check("TT-18 >= 8 CAPA source categories total", len(codes) >= 8, len(codes))
st, vol = req("GET", "/api/capa/dashboard/volume-by-source", cro)
check("TT-18 volume-by-source aggregates 8 sources (200)", st == 200, st)
for src in ("CONTROL_DEFICIENCY", "VENDOR_RISK"):
    st, c = req("GET", f"/api/capa?sourceType={src}", cro)
    check(f"TT-18 CAPA filter {src} serializes", st == 200 and c.get("total", 0) >= 1, c.get("total") if st == 200 else st)

# ── TT-12 vendor master provider (module-owned; codes are VEN-*) ──────────────
check("TT-12 module-owned vendor codes (VEN-*)", all(v["vendorCode"].startswith("VEN-") for v in vendors), [v["vendorCode"] for v in vendors[:3]])

print(f"\n== TIER 3 QA RESULT: {P} passed, {F} failed ==")
if F:
    raise SystemExit(1)
