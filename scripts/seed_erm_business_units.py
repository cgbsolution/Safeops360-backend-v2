"""Backfill EnterpriseRisk.businessUnit (Department / BU) for the demo estate so the
§1b Department-wise dashboard + §10e Department-wise report show a real spread.

Idempotent + non-destructive: only fills rows where businessUnit IS NULL/'' — it
never overwrites a value a user set. Department is derived from the risk category
name (keyword map) with a safe "Operations" fallback. Run:

    python -m scripts.seed_erm_business_units
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.erm import EnterpriseRisk, RiskCategory

# category-name keyword → Department / Business Unit
_MAP = [
    (("financial", "market", "credit", "treasury", "liquidity"), "Finance & Treasury"),
    (("supply", "operational", "operations", "production", "manufactur"), "Manufacturing & Supply Chain"),
    (("cyber", "technology", "it", "digital", "data"), "IT & Digital"),
    (("esg", "environment", "sustainab", "social"), "Compliance & Sustainability"),
    (("regulat", "legal", "compliance"), "Legal & Compliance"),
    (("people", "hr", "human", "workforce", "labour", "labor"), "Human Resources"),
    (("strateg", "reputation", "brand", "governance"), "Corporate Strategy"),
    (("health", "safety", "hse", "ehs", "fire"), "HSE / Operations"),
]


def _dept_for(cat_name: str | None) -> str:
    n = (cat_name or "").lower()
    for keys, dept in _MAP:
        if any(k in n for k in keys):
            return dept
    return "Operations"


async def main() -> int:
    async with AsyncSessionLocal() as db:
        cats = {c.id: c.name for c in (await db.execute(select(RiskCategory))).scalars().all()}
        risks = (await db.execute(select(EnterpriseRisk).where(EnterpriseRisk.isDeleted.is_(False)))).scalars().all()
        filled = 0
        by_dept: dict[str, int] = {}
        for r in risks:
            if r.businessUnit and r.businessUnit.strip():
                by_dept[r.businessUnit] = by_dept.get(r.businessUnit, 0) + 1
                continue
            dept = _dept_for(cats.get(r.categoryId))
            r.businessUnit = dept
            filled += 1
            by_dept[dept] = by_dept.get(dept, 0) + 1
        await db.commit()
        print(f"Filled businessUnit on {filled} risk(s). Spread: {dict(sorted(by_dept.items(), key=lambda x: -x[1]))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
