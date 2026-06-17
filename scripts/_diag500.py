import asyncio, traceback
from sqlalchemy import select
from app.core.db import AsyncSessionLocal, settings
from app.models.user import User
from app.models.plant import Plant, Area
from app.routers.hira import for_ptw


async def main():
    print("BACKEND DB:", settings.async_database_url.split("@")[-1])
    async with AsyncSessionLocal() as db:
        admin = (await db.execute(select(User).where(User.email == "admin@safeops360.in"))).scalars().first()
        nw = (await db.execute(select(Plant).where(Plant.code == "NW"))).scalars().first()
        print("admin id:", admin and admin.id, "| NW plant id:", nw and nw.id)
        if not admin or not nw:
            print("!! admin/NW missing in backend DB — backend is on a DIFFERENT database")
            return
        area = (await db.execute(select(Area).where(Area.plantId == nw.id))).scalars().first()
        print("area:", area and area.name)
        try:
            res = await for_ptw(plant_id=nw.id, area_id=area.id if area else None, permit_type="HOT_WORK", user=admin, db=db)
            print("OK — for_ptw returned count =", res.count)
        except Exception:
            print("---- for_ptw raised ----")
            traceback.print_exc()


asyncio.run(main())
