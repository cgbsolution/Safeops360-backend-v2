"""Give HSE Managers cross-plant access across BOTH Meridian works (NW + SW).

Per the PPE build prompt §10 ("HSE Manager can view cross-plant"), each user
holding the HSE_MANAGER role gets a PLANT-scoped UserRole entry for every
Meridian plant beyond their home plant. The permission service unions the
primary plantId with PLANT-scoped UserRole rows, so OWN_PLANT grants then
work at both sites, and the plant picker lists both.

Deliberately NOT an ALL_PLANTS grant — that would also expose the 10 other
industry demo tenants (Axiom Chemicals, MedCore Pharma, …).

Additive + idempotent (unique constraint on userId/roleId/scopeType/scopeValue;
existing rows are skipped). Run from the backend root:
    .venv/Scripts/python.exe scripts/grant_hse_manager_cross_plant.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.plant import Plant
from app.models.user import Role, User, UserRole


def main() -> int:
    settings = get_settings()
    engine = create_engine(settings.sync_database_url, future=True)
    with Session(engine) as s:
        role = s.execute(select(Role).where(Role.code == "HSE_MANAGER")).scalar_one_or_none()
        if role is None:
            print("HSE_MANAGER role not found")
            return 1

        meridian = s.execute(
            select(Plant.id, Plant.name).where(Plant.name.like("Meridian%"))
        ).all()
        if len(meridian) < 2:
            print(f"Expected 2 Meridian plants, found {len(meridian)}")
            return 1
        print("Meridian plants:", ", ".join(name for _, name in meridian))

        managers = s.execute(
            select(User)
            .join(UserRole, UserRole.userId == User.id)
            .where(UserRole.roleId == role.id)
        ).scalars().unique().all()
        print(f"HSE_MANAGER holders: {[u.name for u in managers]}")

        added = 0
        for u in managers:
            for plant_id, plant_name in meridian:
                if u.plantId == plant_id:
                    continue  # home plant already accessible via User.plantId
                exists = s.execute(
                    select(UserRole)
                    .where(UserRole.userId == u.id)
                    .where(UserRole.roleId == role.id)
                    .where(UserRole.scopeType == "PLANT")
                    .where(UserRole.scopeValue == plant_id)
                ).scalar_one_or_none()
                if exists is not None:
                    continue
                s.add(UserRole(
                    userId=u.id, roleId=role.id,
                    scopeType="PLANT", scopeValue=plant_id,
                ))
                added += 1
                print(f"  + {u.name} → {plant_name}")
        s.commit()
        print(f"Done. {added} cross-plant role scope(s) added (existing kept).")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
