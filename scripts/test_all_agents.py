"""Smoke-test every AI agent end-to-end (real Anthropic call).

For each agent: start_invocation -> run_invocation on a real source record,
then report the landing status + any error. PENDING_REVIEW = healthy (advisory
agents land there awaiting human review); ERRORED = broken.

    .venv/Scripts/python.exe scripts/test_all_agents.py
"""

from __future__ import annotations

import asyncio
import sys
import traceback

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import select  # noqa: E402

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.models.agent import AgentInvocation  # noqa: E402
from app.services.agents import agent_service  # noqa: E402

ADMIN = "cmpwi9hs900108nbk11fy5z14"

TESTS = [
    ("RCA_ASSISTANT", "INCIDENT", "cmpwia9cx00nh8nbk5l1242bh"),
    ("TRIAGE_AGENT", "INCIDENT", "cmpwia9cx00nh8nbk5l1242bh"),
    ("HIRA_ASSISTANT", "HIRA", "cmpwjajto0005ja27eellq3t0"),
    ("CAPA_ASSISTANT", "CAPA", "cmpwlhuum0001bga81m89p17v"),
    ("PERMIT_RISK_REVIEWER", "PTW", "cmpwi9t04006o8nbkvmd67qmk"),
]

HEALTHY = {"PENDING_REVIEW", "COMPLETED", "SUCCEEDED", "SUCCESS"}


async def run_one(agent_code: str, module: str, rid: str) -> tuple[str, str, str]:
    try:
        async with AsyncSessionLocal() as db:
            inv = await agent_service.start_invocation(
                db=db,
                agent_code=agent_code,
                source_module=module,
                source_record_id=rid,
                user_id=ADMIN,
                force_escalation_model=False,
            )
            await db.commit()
            inv_id = inv.id
    except Exception as e:  # noqa: BLE001
        return (agent_code, "START_FAILED", f"{type(e).__name__}: {e}"[:220])

    try:
        async with AsyncSessionLocal() as db:
            await agent_service.run_invocation(db=db, invocation_id=inv_id)
    except Exception as e:  # noqa: BLE001
        return (agent_code, "RUN_THREW", f"{type(e).__name__}: {e}"[:220])

    async with AsyncSessionLocal() as db:
        inv = (
            await db.execute(select(AgentInvocation).where(AgentInvocation.id == inv_id))
        ).scalar_one()
        detail = " ".join(filter(None, [inv.errorType or "", (inv.errorDetails or "")]))
        return (agent_code, inv.status, detail[:180])


async def main() -> int:
    print("Testing 5 agents (real Anthropic calls)…\n")
    worst = 0
    for code, module, rid in TESTS:
        try:
            agent_code, status, detail = await run_one(code, module, rid)
        except Exception:  # noqa: BLE001
            print(f"FAIL {code:<22} crashed:")
            traceback.print_exc()
            worst = 1
            continue
        ok = status in HEALTHY
        worst = max(worst, 0 if ok else 1)
        mark = "OK  " if ok else "FAIL"
        print(f"{mark} {agent_code:<22} status={status:<14} {detail}")
    print("\nDone." + ("" if worst == 0 else "  Some agents FAILED — see above."))
    return worst


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
