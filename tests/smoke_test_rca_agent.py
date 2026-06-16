"""End-to-end smoke test for the RCA Assistant agent.

This is NOT a pytest — it's a CLI script that drives a real agent
invocation against a running backend + real Anthropic API. Run after
deploying Commits 1-3 to verify the full pipeline:

  • POST  /api/agents/RCA_ASSISTANT/invoke      → 202 + invocation ID
  • POLL  /api/agent-invocations/{id}           → status RUNNING → PENDING_REVIEW
  • Print: reasoning, suggestion JSON, tool calls, hallucination flags

Per the brief's Commit 3 exit criteria:
  • Agent calls multiple tools           ← checked
  • Response is structurally well-formed ← reasoning/suggestion/confidence parsed
  • Suggestion is non-generic            ← human judgement; print + eyeball
  • Hallucination detection works        ← print hallucinationFlagged + details

Prerequisites:
  1. `npx prisma db push` from safeops_360/ has applied the schema
  2. `npm run db:seed-rbac` has seeded permissions
  3. `npm run db:seed-agents` has seeded the RCA_ASSISTANT row
  4. ANTHROPIC_API_KEY is set in safeops_360_bakend/.env
  5. Python backend running on http://localhost:8000
  6. A CLOSED incident exists in the DB (use seed-realistic-ops to seed)
  7. A user with AGENT.RCA_INVOKE permission (HSE_MANAGER, PLANT_HEAD,
     SAFETY_OFFICER for the incident's plant; CORPORATE_HSE / SYSTEM_ADMIN
     globally)

Usage:
  python tests/smoke_test_rca_agent.py \\
      --incident-id <cuid> \\
      --token <jwt-from-login> \\
      [--backend http://localhost:8000] \\
      [--poll-interval 3] \\
      [--max-wait 180]

Getting a JWT token:
  curl -X POST http://localhost:8000/api/auth/login \\
       -H 'content-type: application/json' \\
       -d '{"email":"hse.manager@lumshnong.safeops360.in","password":"..."}' \\
       | jq -r '.access_token'
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def _http(
    method: str, url: str, token: str, body: dict | None = None
) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8")
            return resp.getcode(), (json.loads(text) if text else {})
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"raw": text}
        return e.code, payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incident-id", required=True, help="Incident.id (cuid) to invoke against")
    parser.add_argument("--token", required=True, help="JWT access token from /api/auth/login")
    parser.add_argument("--backend", default="http://localhost:8000")
    parser.add_argument("--poll-interval", type=int, default=3)
    parser.add_argument("--max-wait", type=int, default=180)
    parser.add_argument(
        "--force-escalation",
        action="store_true",
        help="Use the escalation model (Opus) instead of the primary (Haiku).",
    )
    args = parser.parse_args()

    # 1. Kick off the invocation
    print(f"→ POST {args.backend}/api/agents/RCA_ASSISTANT/invoke")
    code, body = _http(
        "POST",
        f"{args.backend}/api/agents/RCA_ASSISTANT/invoke",
        args.token,
        body={
            "sourceModule": "INCIDENT",
            "sourceRecordId": args.incident_id,
            "forceEscalationModel": args.force_escalation,
        },
    )
    if code != 202:
        print(f"FAIL: invoke returned {code}: {json.dumps(body, indent=2)}")
        return 1
    invocation_id = body["invocationId"]
    invocation_number = body["invocationNumber"]
    print(f"  invocation: {invocation_number} (id={invocation_id})")

    # 2. Poll until status leaves RUNNING
    poll_url = f"{args.backend}/api/agent-invocations/{invocation_id}"
    elapsed = 0
    invocation: dict = {}
    while elapsed < args.max_wait:
        code, invocation = _http("GET", poll_url, args.token)
        if code != 200:
            print(f"FAIL: poll returned {code}: {json.dumps(invocation, indent=2)}")
            return 1
        status = invocation.get("status")
        print(f"  [{elapsed:>3}s] status={status}")
        if status != "RUNNING":
            break
        time.sleep(args.poll_interval)
        elapsed += args.poll_interval
    else:
        print(f"FAIL: timed out after {args.max_wait}s; last status={invocation.get('status')}")
        return 1

    print()
    print("=" * 70)
    print("  RCA ASSISTANT INVOCATION RESULT")
    print("=" * 70)

    # 3. Print the result for human eyeballing
    status = invocation.get("status")
    print(f"\nStatus              : {status}")
    print(f"Invocation number   : {invocation.get('invocationNumber')}")
    print(f"Model used          : {invocation.get('modelUsed')}")
    print(f"Input tokens        : {invocation.get('inputTokens')}")
    print(f"Output tokens       : {invocation.get('outputTokens')}")
    print(f"Total cost (USD)    : {invocation.get('totalCostUsd')}")
    print(f"Latency (ms)        : {invocation.get('latencyMs')}")
    print(f"Confidence          : {invocation.get('agentConfidence')}")
    print(f"Hallucination flag  : {invocation.get('hallucinationFlagged')}")
    if invocation.get("hallucinationDetails"):
        print("Hallucination detail:")
        print(json.dumps(invocation["hallucinationDetails"], indent=2))

    tool_calls = invocation.get("toolCalls") or []
    print(f"\nTool calls          : {len(tool_calls)}")
    for tc in tool_calls:
        marker = "✗" if tc.get("hadError") else "✓"
        print(f"  {marker} #{tc.get('sequence')} {tc.get('toolName')} ({tc.get('executionMs')}ms)")
        if tc.get("hadError"):
            print(f"    error: {tc.get('errorDetails')}")

    print("\n── REASONING ──")
    print(invocation.get("agentReasoning") or "(none)")

    print("\n── SUGGESTION ──")
    suggestion = invocation.get("agentSuggestion")
    if suggestion is None:
        print("(none)")
    else:
        print(json.dumps(suggestion, indent=2))

    # 4. Exit-criteria check
    print("\n── EXIT CRITERIA ──")
    checks = [
        ("Status reached PENDING_REVIEW", status == "PENDING_REVIEW"),
        ("Multiple tools called", len(tool_calls) >= 2),
        ("Reasoning extracted", bool(invocation.get("agentReasoning"))),
        ("Suggestion JSON parsed", suggestion is not None and "_unparsed" not in suggestion),
        ("Recommended method valid", suggestion is not None and suggestion.get("recommendedMethod") in {
            "FIVE_WHY", "FISHBONE", "FTA", "BOWTIE", "TAPROOT", "CAUSE_MAP"
        }),
        ("No hallucinations", not invocation.get("hallucinationFlagged")),
    ]
    all_pass = True
    for label, ok in checks:
        marker = "✓" if ok else "✗"
        print(f"  {marker} {label}")
        if not ok:
            all_pass = False

    return 0 if all_pass else 2


if __name__ == "__main__":
    sys.exit(main())
