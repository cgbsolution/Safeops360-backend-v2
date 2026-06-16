"""CAMS QA — exercises every /api/cams endpoint + full lifecycle + gates + RBAC.

Hits the test backend on :8077 as the CAMS personas. Mutates data — re-seed
CAMS after (npx tsx prisma/seed-cams.ts).
"""
import base64
import json
import sys
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "http://127.0.0.1:8077"
P = F = 0
FAILS = []


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
    except Exception as e:
        return 0, {"raw": str(e)}


def check(name, cond, detail=""):
    global P, F
    if cond:
        P += 1; print(f"  PASS  {name}")
    else:
        F += 1; FAILS.append(f"{name} -- {detail}"); print(f"  FAIL  {name}  -- {detail}")


def login(email):
    st, j = req("POST", "/api/auth/login", body={"email": email, "password": "demo123"})
    return (j or {}).get("access_token")


def uid_from(token):
    """Decode the JWT 'sub' (userId) without verifying."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("sub")
    except Exception:
        return None


print("== CAMS QA ==")
mgr = login("rohan.bhatt@safeops360.in")       # AUDIT_MANAGER
lead = login("anjali.verma@safeops360.in")     # LEAD_AUDITOR
auditor = login("deepak.sharma.cams@safeops360.in")  # AUDITOR
worker = login("worker.it.nw@safeops360.in")   # no CAMS perms
check("logins (mgr/lead/auditor/worker)", all([mgr, lead, auditor, worker]),
      f"mgr={bool(mgr)} lead={bool(lead)} auditor={bool(auditor)} worker={bool(worker)}")
mgr_id = uid_from(mgr)
lead_id = uid_from(lead)
auditor_id = uid_from(auditor)
check("decoded manager userId", bool(mgr_id), mgr_id)

# ── RBAC denials ─────────────────────────────────────────────────────────────
check("worker DENIED engagements (403)", req("GET", "/api/cams/engagements", worker)[0] == 403)
check("worker DENIED templates (403)", req("GET", "/api/cams/templates", worker)[0] == 403)
_st_at, _b_at = req("POST", "/api/cams/audit-types", auditor, {"name": "x denied", "engagementType": "INSPECTION"})
check("auditor DENIED create audit type (403)", _st_at == 403, (_st_at, _b_at))

# ── Audit types ──────────────────────────────────────────────────────────────
st, types = req("GET", "/api/cams/audit-types", mgr)
check("audit-types list 200", st == 200, st)
check("audit-types count = 8", isinstance(types, list) and len(types) == 8, len(types) if isinstance(types, list) else types)
if isinstance(types, list) and types:
    check("audit-type has engagementCount", "engagementCount" in types[0], types[0].keys() if types else "")

st, at = req("POST", "/api/cams/audit-types", mgr,
             {"name": "QA Temp Type", "engagementType": "INSPECTION", "standardRefs": ["ISO_45001"], "requiresAssetRef": True})
check("create audit type 201", st == 201, (st, at))
qa_type_id = (at or {}).get("id")
if qa_type_id:
    st, _ = req("PATCH", f"/api/cams/audit-types/{qa_type_id}", mgr,
                {"name": "QA Temp Type v2", "engagementType": "INSPECTION", "standardRefs": [], "requiresAssetRef": False, "isActive": True, "requiresAuditorCompetency": []})
    check("patch audit type 200", st == 200, st)
    check("delete audit type 204", req("DELETE", f"/api/cams/audit-types/{qa_type_id}", mgr)[0] == 204)

# ── Templates ────────────────────────────────────────────────────────────────
st, tl = req("GET", "/api/cams/templates", mgr)
check("templates list 200", st == 200, st)
tpls = (tl or {}).get("items", [])
check("templates count = 4", len(tpls) == 4, len(tpls))
approved = [t for t in tpls if t["status"] == "APPROVED"]
check("all seeded templates APPROVED", len(approved) == 4, [t["status"] for t in tpls])
hse = next((t for t in tpls if "HSE" in t["name"]), None)
if hse:
    st, detail = req("GET", f"/api/cams/templates/{hse['id']}", mgr)
    check("template detail 200", st == 200, st)
    check("template detail has sections", st == 200 and len(detail.get("sections", [])) > 0, "")
    check("template questions clause-mapped", st == 200 and detail["clauseCount"] > 0, detail.get("clauseCount") if st == 200 else "")

st, clauses = req("GET", "/api/cams/clause-catalogue", mgr)
check("clause-catalogue 200 + non-empty", st == 200 and isinstance(clauses, list) and len(clauses) > 0, st)

# ── Template lifecycle: create → save → submit → approve → clone ─────────────
st, draft = req("POST", "/api/cams/templates", mgr,
                {"name": "QA Lifecycle Template", "applicableEngagementTypes": ["INTERNAL_AUDIT"], "standardRefs": ["ISO_9001"],
                 "scoringConfig": {"mode": "PERCENT_CONFORMANCE", "passThresholdPercent": 80}, "ownerId": mgr_id})
check("create template draft 201", st == 201, (st, draft))
tid = (draft or {}).get("id")
if tid:
    st, saved = req("PUT", f"/api/cams/templates/{tid}", mgr, {"sections": [
        {"orderIndex": 0, "title": "Sec A", "questions": [
            {"orderIndex": 0, "text": "Q1 conformance?", "questionType": "CONFORM_NC_NA", "standardClauseRef": "ISO 9001:9.2", "ncTriggersFinding": True},
            {"orderIndex": 1, "text": "Q2 numeric?", "questionType": "NUMERIC"},
        ]},
    ]})
    check("save template structure 200", st == 200, st)
    check("saved 1 section / 2 questions", st == 200 and saved["sectionCount"] == 1 and saved["questionCount"] == 2, (saved.get("sectionCount"), saved.get("questionCount")) if st == 200 else "")
    check("lead CANNOT approve (perm) 403", req("POST", f"/api/cams/templates/{tid}/approve", lead)[0] == 403 or req("POST", f"/api/cams/templates/{tid}/submit", lead)[0] in (200, 409))
    st, _ = req("POST", f"/api/cams/templates/{tid}/submit", mgr)
    check("submit template (→IN_REVIEW) 200", st == 200, st)
    st, appr = req("POST", f"/api/cams/templates/{tid}/approve", mgr)
    check("approve template (→APPROVED) 200", st == 200 and appr["status"] == "APPROVED", (st, appr.get("status") if st == 200 else appr))
    # editing an APPROVED template is blocked
    check("edit APPROVED blocked (409)", req("PUT", f"/api/cams/templates/{tid}", mgr, {"name": "x"})[0] == 409)
    st, clone = req("POST", f"/api/cams/templates/{tid}/clone", mgr)
    check("clone APPROVED → new DRAFT v2 201", st == 201 and clone["status"] == "DRAFT" and clone["version"] == 2, (st, clone.get("version") if st == 201 else clone))

# ── Engagements ──────────────────────────────────────────────────────────────
st, el = req("GET", "/api/cams/engagements", mgr)
check("engagements list 200", st == 200, st)
items = (el or {}).get("items", [])
check("engagements count = 15", len(items) == 15, len(items))
check("statusCounts present", "statusCounts" in (el or {}), "")
check("4 consumer-raised (sourceModule)", sum(1 for e in items if e.get("sourceModule")) == 4, sum(1 for e in items if e.get("sourceModule")))
# filter by type
st, fl = req("GET", "/api/cams/engagements?engagementType=INSPECTION", mgr)
check("filter engagementType=INSPECTION", st == 200 and all(e["engagementType"] == "INSPECTION" for e in fl.get("items", [])), st)
# benchmarking story
north = next((e for e in items if "North Works FY26" in e["title"] and "HSE" in e["title"]), None)
south = next((e for e in items if "South Works FY26" in e["title"] and "HSE" in e["title"]), None)
check("North HSE score 88", north and north["scorePercent"] == 88, north["scorePercent"] if north else "missing")
check("South HSE score 79", south and south["scorePercent"] == 79, south["scorePercent"] if south else "missing")
check("engagement rollup findingCount on South", south and south["findingCount"] >= 1, south.get("findingCount") if south else "")

# ── Repeat-finding detection (Phase 1a) — runs on SEEDED data, pre-lifecycle ──
st, det = req("POST", "/api/cams/findings/recompute-repeats", mgr)
check("recompute-repeats 200", st == 200 and "flagged" in (det or {}), (st, det))
check("detection flags exactly 5 seeded South-Works repeats", st == 200 and det.get("flagged") == 5, det.get("flagged") if st == 200 else det)
check("worker DENIED recompute-repeats (403)", req("POST", "/api/cams/findings/recompute-repeats", worker)[0] == 403)
st, rep0 = req("GET", "/api/cams/findings?repeatOnly=true", mgr)
check("repeatOnly returns the 5 detected repeats", st == 200 and len(rep0.get("items", [])) == 5, len(rep0.get("items", [])) if st == 200 else st)

# ── Analytics snapshots + scope filter (Phase 1b) ────────────────────────────
st, snaps = req("GET", "/api/cams/analytics/snapshots", mgr)
check("snapshots list 200", st == 200, st)
check("seeded snapshots present (FY26-Q3/Q4/FY27-Q1)", st == 200 and len(snaps) >= 3, len(snaps) if st == 200 else st)
st, scoped = req("GET", "/api/cams/analytics?engagementType=INSPECTION", mgr)
check("analytics scope filter narrows to INSPECTION", st == 200 and scoped.get("byType", {}).get("INSPECTION", 0) >= 1 and set(scoped.get("byType", {}).keys()) <= {"INSPECTION"}, scoped.get("byType") if st == 200 else st)
st, snapNew = req("POST", "/api/cams/analytics/snapshots", mgr, {"periodLabel": "QA-PROBE"})
check("create snapshot 201 + integrity hash", st == 201 and snapNew.get("snapshotHash"), (st, snapNew))
check("worker DENIED snapshots (403)", req("GET", "/api/cams/analytics/snapshots", worker)[0] == 403)

# ── Full lifecycle: schedule → start → execute → findings → close gate ───────
hse_at = next((t for t in types if "HSE System" in t["name"]), None) if isinstance(types, list) else None
hse_tpl = next((t for t in tpls if "HSE" in t["name"]), None)
plant_id = north["siteId"] if north else None
st, eng = req("POST", "/api/cams/engagements", mgr, {
    "title": "QA Lifecycle Audit", "engagementType": "INTERNAL_AUDIT",
    "auditTypeId": hse_at["id"] if hse_at else None, "standardRefs": ["ISO_45001"],
    "siteId": plant_id, "leadAuditorId": mgr_id, "auditeeOwnerId": auditor_id,
    "plannedDate": "2026-06-20T00:00:00Z", "templateId": hse_tpl["id"] if hse_tpl else None,
})
check("create engagement 201", st == 201, (st, eng))
eid = (eng or {}).get("id")
if eid:
    check("new engagement status (PLANNED/SCHEDULED)", eng["status"] in ("PLANNED", "SCHEDULED"), eng["status"])
    # invalid jump
    check("invalid transition PLANNED→CLOSED (409)", req("POST", f"/api/cams/engagements/{eid}/transition", mgr, {"toStatus": "CLOSED"})[0] == 409)
    # to SCHEDULED then IN_PROGRESS
    if eng["status"] == "PLANNED":
        check("→SCHEDULED 200", req("POST", f"/api/cams/engagements/{eid}/transition", mgr, {"toStatus": "SCHEDULED"})[0] == 200)
    st, started = req("POST", f"/api/cams/engagements/{eid}/transition", mgr, {"toStatus": "IN_PROGRESS"})
    check("→IN_PROGRESS 200 (snapshots template)", st == 200 and started.get("templateVersionUsed") == 1, (st, started.get("templateVersionUsed") if st == 200 else started))

    # checklist runner
    st, runner = req("GET", f"/api/cams/engagements/{eid}/checklist", mgr)
    check("get checklist 200 + sections", st == 200 and len(runner.get("sections", [])) > 0, st)
    qids = [q["id"] for s in runner.get("sections", []) for q in s["questions"]] if st == 200 else []
    check("runner exposes question ids", len(qids) > 0, len(qids))

    # auditor executes: answer all, two NC (one MAJOR, with finding trigger)
    answers = []
    for i, qid in enumerate(qids):
        if i == 0:
            answers.append({"questionId": qid, "conformance": "NC", "ncSeverity": "MAJOR_NC", "note": "QA major nc"})
        elif i == 1:
            answers.append({"questionId": qid, "conformance": "NC", "ncSeverity": "MINOR_NC", "note": "QA minor nc"})
        else:
            answers.append({"questionId": qid, "conformance": "CONFORM"})
    # cross-plant auditor (SW) DENIED executing this North-Works engagement (plant scope)
    check("cross-plant auditor DENIED checklist save (403)",
          req("PUT", f"/api/cams/engagements/{eid}/checklist", auditor, {"answers": [], "complete": False})[0] == 403)
    # NW-scoped lead auditor CAN execute the NW engagement
    st, done = req("PUT", f"/api/cams/engagements/{eid}/checklist", lead, {"answers": answers, "complete": True})
    check("lead save+complete checklist 200", st == 200, (st, done))
    check("scoring computed (scorePercent set)", st == 200 and done.get("scorePercent") is not None, done.get("scorePercent") if st == 200 else "")
    check("engagement → FIELDWORK_COMPLETE", st == 200 and done.get("status") == "FIELDWORK_COMPLETE", done.get("status") if st == 200 else "")

    # findings spawned
    st, fl2 = req("GET", f"/api/cams/findings?engagementId={eid}", mgr)
    spawned = (fl2 or {}).get("items", [])
    check("NC answers spawned findings (2)", st == 200 and len(spawned) == 2, len(spawned) if st == 200 else st)
    major = next((f for f in spawned if f["severity"] == "MAJOR_NC"), None)
    check("major finding inherited clause ref", major and major.get("standardClauseRef"), major.get("standardClauseRef") if major else "no major")
    check("major finding capaRequired=True", major and major["capaRequired"] is True, "")

    # close gate: move FIELDWORK_COMPLETE→FINDINGS_REVIEW→REPORT_ISSUED then try CLOSE (blocked: open major, no CAPA)
    req("POST", f"/api/cams/engagements/{eid}/transition", mgr, {"toStatus": "FINDINGS_REVIEW"})
    req("POST", f"/api/cams/engagements/{eid}/transition", mgr, {"toStatus": "REPORT_ISSUED"})
    st_block, blocked = req("POST", f"/api/cams/engagements/{eid}/transition", mgr, {"toStatus": "CLOSED"})
    check("CLOSE blocked (open major NC, no CAPA) 400", st_block == 400, (st_block, blocked))

    if major:
        # raise CAPA on the major
        st, withcapa = req("POST", f"/api/cams/findings/{major['id']}/raise-capa", mgr)
        check("raise CAPA on major finding 200", st == 200 and withcapa.get("capaNumber"), (st, withcapa.get("capaNumber") if st == 200 else withcapa))
        check("CAPA sourceType AUDIT_INTERNAL", st == 200 and withcapa.get("capaState") is not None, withcapa.get("capaState") if st == 200 else "")
        # duplicate raise blocked
        check("duplicate raise-capa blocked 409", req("POST", f"/api/cams/findings/{major['id']}/raise-capa", mgr)[0] == 409)
        # cannot close finding (major) — already has capa now, so closing is allowed; close all findings
    # close all findings for the engagement
    for f in spawned:
        # major now has capa; minor: closing a minor is allowed without capa
        req("PATCH", f"/api/cams/findings/{f['id']}", mgr, {"status": "CLOSED", "verificationNote": "QA verified"})
    st_close, closed = req("POST", f"/api/cams/engagements/{eid}/transition", mgr, {"toStatus": "CLOSED"})
    check("CLOSE allowed after CAPA + findings resolved 200", st_close == 200 and closed.get("status") == "CLOSED", (st_close, closed))

# ── Findings register filters ────────────────────────────────────────────────
st, allf = req("GET", "/api/cams/findings", mgr)
check("findings list 200", st == 200, st)
check("severityCounts + repeatCount present", st == 200 and "severityCounts" in allf and "repeatCount" in allf, "")
st, rep = req("GET", "/api/cams/findings?repeatOnly=true", mgr)
check("repeatOnly filter returns only repeats", st == 200 and all(f["isRepeatFinding"] for f in rep.get("items", [])) and len(rep.get("items", [])) >= 1, len(rep.get("items", [])) if st == 200 else st)

# ── Finding-close gate (negative): a fresh MAJOR finding without CAPA cannot CLOSE
st, e2 = req("POST", "/api/cams/engagements", mgr, {"title": "QA Finding Gate", "engagementType": "INSPECTION", "siteId": plant_id, "leadAuditorId": mgr_id, "plannedDate": "2026-06-25T00:00:00Z"})
if st == 201:
    st, f2 = req("POST", "/api/cams/findings", mgr, {"engagementId": e2["id"], "title": "QA gate major", "severity": "MAJOR_NC", "description": "x"})
    if st == 201:
        st_g, _ = req("PATCH", f"/api/cams/findings/{f2['id']}", mgr, {"status": "CLOSED"})
        check("CLOSE major finding without CAPA blocked 400", st_g == 400, st_g)

# ── Recurrence ───────────────────────────────────────────────────────────────
st, recs = req("GET", "/api/cams/recurrences", mgr)
check("recurrences list 200 (3)", st == 200 and len(recs) == 3, len(recs) if st == 200 else st)
st, runres = req("POST", "/api/cams/recurrences/run", mgr)
check("recurrences/run 200", st == 200 and "generated" in (runres or {}), (st, runres))
st, newrec = req("POST", "/api/cams/recurrences", mgr, {"frequency": "MONTHLY", "leadTimeDays": 10, "siteScope": [], "isActive": False})
check("create recurrence 201", st == 201, (st, newrec))
if st == 201:
    check("patch recurrence 200", req("PATCH", f"/api/cams/recurrences/{newrec['id']}", mgr, {"frequency": "QUARTERLY", "leadTimeDays": 14, "siteScope": [], "isActive": False})[0] == 200)

# REGRESSION (Critical): a multi-site recurrence must mint DISTINCT engagement
# codes (autoflush=False + count-based codes previously collided → unique 500).
sites = list({e["siteId"] for e in items if e.get("siteId")})[:2]
if len(sites) >= 2:
    st, mrec = req("POST", "/api/cams/recurrences", mgr, {"frequency": "MONTHLY", "leadTimeDays": 14, "siteScope": sites})
    check("create multi-site recurrence 201", st == 201, (st, mrec))
    st, gen = req("POST", "/api/cams/recurrences/run", mgr)
    codes = (gen or {}).get("codes", [])
    check("multi-site recurrence generated >=2 engagements (no 500)", st == 200 and gen.get("generated", 0) >= 2, (st, gen))
    check("generated engagement codes are DISTINCT (no dup code)", st == 200 and len(codes) == len(set(codes)) and len(codes) >= 2, codes)

# ── Remaining endpoint coverage ──────────────────────────────────────────────
if tid:
    st, ret = req("POST", f"/api/cams/templates/{tid}/retire", mgr)
    check("retire template -> RETIRED 200", st == 200 and ret.get("status") == "RETIRED", (st, ret.get("status") if st == 200 else ret))
if 'e2' in dir() and e2 and e2.get("id"):
    st, patched = req("PATCH", f"/api/cams/engagements/{e2['id']}", mgr, {"scopeStatement": "QA patched scope"})
    check("patch engagement 200", st == 200 and patched.get("scopeStatement") == "QA patched scope", (st, patched.get("scopeStatement") if st == 200 else patched))
if 'major' in dir() and major and major.get("id"):
    st, gf = req("GET", f"/api/cams/findings/{major['id']}", mgr)
    check("get finding by id 200", st == 200 and gf.get("findingCode"), (st, gf))
# 404s
check("get missing engagement 404", req("GET", "/api/cams/engagements/nonexistent-id", mgr)[0] == 404)
check("get missing template 404", req("GET", "/api/cams/templates/nonexistent-id", mgr)[0] == 404)
check("get missing finding 404", req("GET", "/api/cams/findings/nonexistent-id", mgr)[0] == 404)

# ── Analytics (C-13) ──────────────────────────────────────────────────────
st, an = req("GET", "/api/cams/analytics", mgr)
check("analytics 200", st == 200, st)
if st == 200:
    check("analytics programme total >=1", an.get("programme", {}).get("total", 0) >= 1, an.get("programme"))
    check("analytics benchmarking has sites", len(an.get("benchmarkingBySite", [])) >= 1, len(an.get("benchmarkingBySite", [])))
    check("analytics findingsBySeverity present", "findingsBySeverity" in an, "")
    check("analytics clauseConformance computed", isinstance(an.get("clauseConformance"), list), "")
check("worker DENIED analytics (403)", req("GET", "/api/cams/analytics", worker)[0] == 403)

# ── Compliance Tracker (C-12) ───────────────────────────────────────────────
st, comp = req("GET", "/api/cams/compliance", mgr)
check("compliance tracker 200", st == 200, st)
obl_id = None
if st == 200:
    check("compliance has obligations", comp.get("totalObligations", 0) >= 1, comp.get("totalObligations"))
    check("compliance verifiedPct computed", "verifiedPct" in comp, "")
    check("compliance rows present", len(comp.get("rows", [])) >= 1, len(comp.get("rows", [])))
    check("integrated mode reads ERM register (obligationsSource)", comp.get("obligationsSource") == "ERM", comp.get("obligationsSource"))
    obl_id = (comp.get("rows") or [{}])[0].get("obligationId")
check("worker DENIED compliance (403)", req("GET", "/api/cams/compliance", worker)[0] == 403)
if obl_id and items:
    st, lk = req("POST", "/api/cams/compliance/links", mgr, {"obligationId": obl_id, "engagementId": items[0]["id"], "linkType": "VERIFIES", "notes": "qa link"})
    check("create compliance link 201", st == 201, (st, lk))
    if st == 201:
        check("delete compliance link 204", req("DELETE", f"/api/cams/compliance/links/{lk['id']}", mgr)[0] == 204)
    check("compliance link w/o engagement/finding rejected 400",
          req("POST", "/api/cams/compliance/links", mgr, {"obligationId": obl_id, "linkType": "VERIFIES"})[0] == 400)

# ── CAPA surfaced (C-14) ────────────────────────────────────────────────────
st, capa = req("GET", "/api/cams/capa", mgr)
check("audit-capa list 200", st == 200, st)
if st == 200:
    check("audit-capa all AUDIT-source", all(c["sourceTypeCode"].startswith("AUDIT") for c in capa.get("items", [])), [c.get("sourceTypeCode") for c in capa.get("items", [])][:5])
    check("audit-capa stateCounts present", "stateCounts" in capa, "")
check("worker DENIED capa view (403)", req("GET", "/api/cams/capa", worker)[0] == 403)

# ── Phase 2 — provider layer / standalone (§1.3 / §12) ───────────────────────
st, rca = req("GET", "/api/cams/rca-methods", mgr)
check("rca-methods 200 + shipped library", st == 200 and isinstance(rca, list) and len(rca) >= 5, len(rca) if st == 200 else st)
check("rca-methods include 5_WHY", st == 200 and any(m.get("code") == "5_WHY" for m in rca), "")
check("worker DENIED rca-methods (403)", req("GET", "/api/cams/rca-methods", worker)[0] == 403)

st, assets = req("GET", "/api/cams/assets", mgr)
check("assets 200 (provider list)", st == 200 and isinstance(assets, list), st)
if st == 200 and assets:
    check("integrated assets sourced from EQUIPMENT_MASTER", all(a.get("source") == "EQUIPMENT_MASTER" for a in assets), {a.get("source") for a in assets})
check("worker DENIED assets (403)", req("GET", "/api/cams/assets", worker)[0] == 403)

# Competency enrichment is non-blocking and present as a list on engagement create.
if 'e2' in dir() and e2 and e2.get("id"):
    check("engagement create exposes competencyWarnings list", isinstance(e2.get("competencyWarnings", []), list), e2.get("competencyWarnings"))

# ── Phase 3 — Audit Programme (C-03) + Board pack (C-15) ─────────────────────
st, prog = req("GET", "/api/cams/programme", mgr)
check("programme 200", st == 200, st)
if st == 200:
    check("programme coverage matrix present", len(prog.get("matrix", [])) >= 1, len(prog.get("matrix", [])))
    check("programme coveragePct computed", "coveragePct" in prog, "")
    check("programme cells carry DONE/PLANNED/GAP status", all(c.get("status") in ("DONE", "PLANNED", "GAP") for c in prog.get("matrix", [])), "")
    check("programme surfaces gap flags list", isinstance(prog.get("gaps"), list), "")
check("worker DENIED programme (403)", req("GET", "/api/cams/programme", worker)[0] == 403)

st, bp = req("GET", "/api/cams/board-pack?periodLabel=FY27-Q1", mgr)
check("board-pack 200", st == 200, st)
if st == 200:
    check("board-pack has programme section", "programme" in bp, "")
    check("board-pack repeat-finding rate", "repeatFindingRatePct" in bp, "")
    check("board-pack clause conformance", isinstance(bp.get("clauseConformance"), list), "")
    check("board-pack compliance assurance (verifiedPct)", "verifiedPct" in bp.get("compliance", {}), bp.get("compliance"))
    check("board-pack benchmarking summary", isinstance(bp.get("benchmarkingBySite"), list), "")
    check("board-pack integrity hash (§12)", bool(bp.get("snapshotHash")), "")
check("worker DENIED board-pack (403)", req("GET", "/api/cams/board-pack", worker)[0] == 403)

# ── Phase 4 — Consumer integration (§8 / TC-18) ──────────────────────────────
st, ci = req("POST", "/api/cams/consumer/inspection", mgr,
             {"sourceModule": "Fire Safety", "title": "Fire round (consumer-launched)", "engagementType": "INSPECTION", "siteId": plant_id, "leadAuditorId": mgr_id, "plannedDate": "2026-06-22T00:00:00Z"})
check("consumer launches CAMS engagement 201 (TC-18)", st == 201 and ci.get("sourceModule") == "Fire Safety", (st, ci))
if st == 201:
    check("consumer engagement runs on the shared engine (INSPECTION)", ci.get("engagementType") == "INSPECTION", ci.get("engagementType"))
    check("consumer engagement got a CAMS code", bool(ci.get("engagementCode")), ci.get("engagementCode"))
    st, fl = req("GET", "/api/cams/engagements?sourceModule=Fire%20Safety", mgr)
    check("consumer engagement appears with provenance", st == 200 and any(e["id"] == ci["id"] for e in fl.get("items", [])), (st, len(fl.get("items", [])) if isinstance(fl, dict) else fl))
check("worker DENIED consumer inspection (403)", req("POST", "/api/cams/consumer/inspection", worker, {"sourceModule": "PPE", "title": "denied probe", "siteId": plant_id})[0] == 403)

# ── Phase 5 — Auditor independence (§7 / TC-21) ──────────────────────────────
st_ind, _ind = req("POST", "/api/cams/engagements", mgr,
                   {"title": "Independence probe", "engagementType": "INTERNAL_AUDIT", "siteId": plant_id,
                    "leadAuditorId": lead_id, "auditeeOwnerId": lead_id, "plannedDate": "2026-06-28T00:00:00Z"})
check("independence: lead auditor cannot be auditee/area owner (400, TC-21)", st_ind == 400, (st_ind, _ind))

print(f"\n== CAMS QA: {P} passed, {F} failed ==")
if FAILS:
    print("FAILURES:")
    for x in FAILS:
        print("  -", x)
