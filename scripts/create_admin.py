"""Create the first administrator for a fresh (clean) deployment.

A clean client install has the schema + RBAC roles (from seed-rbac) but no
users. This bootstraps:
  * a Plant (if none exists), so the admin has a home plant, and
  * a SYSTEM_ADMIN user with a bcrypt password, assigned the SYSTEM_ADMIN role.

That account can then log in, reach the Licence screen, manage users, and run
the rest of onboarding. Idempotent on the email (updates the password if the
user already exists).

Run from the backend root:
    python scripts/create_admin.py --email admin@acme.com --password "S3cret!" \
        --name "Acme Admin" --plant-name "Acme HQ" --plant-code HQ
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.core.security import hash_password
from app.models.plant import Plant
from app.models.user import Role, User, UserRole

ADMIN_ROLE_CODE = "SYSTEM_ADMIN"


async def run(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as db:
        # 1. Ensure a home plant.
        plant = (
            await db.execute(select(Plant).where(Plant.code == args.plant_code))
        ).scalar_one_or_none()
        if plant is None:
            plant = (await db.execute(select(Plant).limit(1))).scalar_one_or_none()
        if plant is None:
            plant = Plant(
                code=args.plant_code,
                name=args.plant_name,
                location=args.plant_name,
                state=args.plant_state,
                unitType="MANUFACTURING",
            )
            db.add(plant)
            await db.flush()
            print(f"Created plant {plant.code} — {plant.name}")

        # 2. The SYSTEM_ADMIN role must exist (run seed-rbac first).
        role = (
            await db.execute(select(Role).where(Role.code == ADMIN_ROLE_CODE))
        ).scalar_one_or_none()
        if role is None:
            print(
                f"ERROR: role {ADMIN_ROLE_CODE} not found. Run the RBAC seed first "
                "(npm run db:seed-rbac).",
                file=sys.stderr,
            )
            return 1

        # 3. Create or update the admin user.
        user = (
            await db.execute(select(User).where(User.email == args.email))
        ).scalar_one_or_none()
        if user is None:
            user = User(
                email=args.email,
                name=args.name,
                passwordHash=hash_password(args.password),
                role=ADMIN_ROLE_CODE,
                plantId=plant.id,
                designation="System Administrator",
            )
            db.add(user)
            await db.flush()
            print(f"Created admin user {user.email}")
        else:
            user.passwordHash = hash_password(args.password)
            user.role = ADMIN_ROLE_CODE
            print(f"Updated existing user {user.email} (password reset, role={ADMIN_ROLE_CODE})")

        # 4. Ensure the SYSTEM_ADMIN role assignment (ALL_PLANTS scope).
        existing = (
            await db.execute(
                select(UserRole).where(UserRole.userId == user.id, UserRole.roleId == role.id)
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(UserRole(userId=user.id, roleId=role.id, scopeType=None, scopeValue=None))
            print("Assigned SYSTEM_ADMIN role")

        await db.commit()
        print("\nDone. Log in with:")
        print(f"  email:    {args.email}")
        print("  password: (the one you supplied)")
        return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Create the first SYSTEM_ADMIN user")
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--name", default="Administrator")
    p.add_argument("--plant-name", default="Head Office")
    p.add_argument("--plant-code", default="HO")
    p.add_argument("--plant-state", default="—")
    raise SystemExit(asyncio.run(run(p.parse_args())))


if __name__ == "__main__":
    main()
