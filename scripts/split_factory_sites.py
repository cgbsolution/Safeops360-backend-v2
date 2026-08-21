"""Give three factory profiles their own Site, instead of the Site they were
mapped onto by mistake.

Background — the Add Factory wizard's Site field is optional for an in-house
factory ("leave blank and one is created from the factory's own name"). Three
Page factories were added with an EXISTING site picked instead:

    Page Industries Limited (Unit-1) Bommanahalli_MD  ->  AXM (Bengaluru Innerwear)
    Page industries unit 4                            ->  ISL (Mandya Socks)
    page industries delhi                             ->  CCS (Dobaspet Garment)

Nothing failed and nothing looked wrong: the Facilities dashboard lists
FactoryProfile rows, so all three appeared. The audit "Owning site" dropdown
lists Plant rows, and no Plant had been created — so the three factories were
missing there, under their own names, while quietly present under AXM / ISL /
CCS. This script closes that gap by doing what leaving the Site blank would
have done at creation time.

Per profile it:
  1. provisions a Plant from the factory's own identity (same `_slug_code` /
     `_unique_plant_code` the service uses, so the result is byte-identical to
     the code path being repaired),
  2. repoints FactoryProfile.siteId,
  3. repoints every child row that denormalises siteId AND carries a
     factoryProfileId — those belong to the factory and must follow it,
  4. grants the new Site to the creator and to estate-wide users, via the same
     `grant_site_access` the router now calls.

Deliberately NOT moved: site-level rows with no factoryProfileId
(CalendarBooking, IndependenceEvent, LossEvent, ProgrammeScopeUnit). Those
predate the factory profiles and belong to the real AXM / ISL / CCS sites —
moving them would hand one site's history to another.

Dry run by default. Pass --apply to write.

    python scripts/split_factory_sites.py
    python scripts/split_factory_sites.py --apply
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, select, text  # noqa: E402

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.models.factory import FactoryProfile  # noqa: E402
from app.models.plant import Plant  # noqa: E402
from app.services import factory as svc  # noqa: E402

# The three profiles, by factoryCode — matched on the code rather than a hard
# id so a re-run against another environment still finds the right rows.
TARGET_CODES = ["(Unit-1)_Bommanahalli_Blr", "K C HALLI", "FAC-0017"]


async def _child_tables(db) -> list[str]:
    """Tables carrying BOTH siteId and factoryProfileId — the factory's own rows."""
    def _sync(conn):
        insp = inspect(conn)
        out = []
        for t in insp.get_table_names(schema="public"):
            cols = {c["name"] for c in insp.get_columns(t, schema="public")}
            if {"siteId", "factoryProfileId"} <= cols:
                out.append(t)
        return sorted(out)

    return await db.run_sync(lambda s: _sync(s.connection()))


async def main(apply: bool) -> None:
    async with AsyncSessionLocal() as db:
        tables = await _child_tables(db)
        print(f"child tables carrying siteId + factoryProfileId: {len(tables)}")

        profiles = (
            await db.execute(
                select(FactoryProfile).where(FactoryProfile.factoryCode.in_(TARGET_CODES))
            )
        ).scalars().all()
        if len(profiles) != len(TARGET_CODES):
            found = {p.factoryCode for p in profiles}
            print(f"!! expected {len(TARGET_CODES)} profiles, found {len(profiles)}: {sorted(found)}")

        for p in profiles:
            old = await db.get(Plant, p.siteId)
            old_label = f"{old.code} — {old.name}" if old else "(none)"
            code = await svc._unique_plant_code(db, svc._slug_code(p.factoryCode or p.factoryName))

            print(f"\n{'=' * 78}\n{p.factoryName}  [{p.factoryCode}]")
            print(f"  current site : {old_label}")
            print(f"  new site     : {code} — {p.factoryName}")
            print(f"                 {p.city or '—'}, {p.state or '—'} · {p.primaryIndustry or 'Factory'}")

            if not apply:
                for t in tables:
                    n = (
                        await db.execute(
                            text(f'SELECT count(*) FROM "{t}" WHERE "factoryProfileId" = :pid'),
                            {"pid": p.id},
                        )
                    ).scalar()
                    if n:
                        print(f"  would move   : {n:>3} × {t}")
                continue

            plant = Plant(
                code=code,
                name=p.factoryName,
                location=p.city or "—",
                state=p.state or "—",
                unitType=p.primaryIndustry or "Factory",
            )
            db.add(plant)
            await db.flush()

            for t in tables:
                res = await db.execute(
                    text(f'UPDATE "{t}" SET "siteId" = :new WHERE "factoryProfileId" = :pid'),
                    {"new": plant.id, "pid": p.id},
                )
                if res.rowcount:
                    print(f"  moved        : {res.rowcount:>3} × {t}")

            p.siteId = plant.id
            granted = await svc.grant_site_access(db, plant_id=plant.id, created_by=p.createdBy)
            print(f"  access       : {granted} UserRole grant(s) written")

        if apply:
            await db.commit()
            print(f"\n{'=' * 78}\ncommitted. total plants now:", (await db.execute(text('SELECT count(*) FROM "Plant"'))).scalar())
        else:
            print(f"\n{'=' * 78}\nDRY RUN — nothing written. Re-run with --apply.")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
