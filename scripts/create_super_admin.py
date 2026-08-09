"""Create (or promote) the organisation's Super Admin.

This portal is single-tenant: one organisation (e.g. Page Industries) with many
plants. The Super Admin owns that organisation — everything a System Admin can
do, plus deciding which licensed modules the organisation uses at all.

Idempotent on the email: creates the account if absent, promotes it if present.
Re-running with a password resets it; omit --password on an existing account to
promote without touching credentials.

Run from the backend root:
    python scripts/create_super_admin.py --password "S3cret!"
    python scripts/create_super_admin.py --email info@cgbindia.com \
        --password "S3cret!" --name "CGB Super Admin"

Prerequisites:
    1. RBAC seed has run, so the SUPER_ADMIN role exists
       (npm run db:seed-rbac, or python -m app.seed.seed_rbac)
    2. scripts/create_licensing_tables.py has run, so the
       OrganisationModuleEntitlement table exists
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal
from app.core.security import hash_password
from app.models.plant import Plant
from app.models.user import Role, User, UserRole

SUPER_ADMIN_ROLE_CODE = "SUPER_ADMIN"


async def run(args: argparse.Namespace) -> int:
    email = (args.email or get_settings().super_admin_email).strip().lower()
    async with AsyncSessionLocal() as db:
        # 1. The SUPER_ADMIN role must exist (run the RBAC seed first).
        role = (
            await db.execute(select(Role).where(Role.code == SUPER_ADMIN_ROLE_CODE))
        ).scalar_one_or_none()
        if role is None:
            print(
                f"ERROR: role {SUPER_ADMIN_ROLE_CODE} not found. Run the RBAC seed first "
                "(npm run db:seed-rbac).",
                file=sys.stderr,
            )
            return 1

        # 2. A home plant, so the account behaves like any other user. The Super
        #    Admin's authority is ALL_PLANTS regardless of which one this is.
        plant = (await db.execute(select(Plant).limit(1))).scalar_one_or_none()
        if plant is None:
            print(
                "ERROR: no plants exist yet. Run scripts/create_admin.py first to "
                "bootstrap a plant and System Admin.",
                file=sys.stderr,
            )
            return 1

        # 3. Create or promote.
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            if not args.password:
                print(
                    f"ERROR: {email} does not exist yet — --password is required to create it.",
                    file=sys.stderr,
                )
                return 1
            user = User(
                email=email,
                name=args.name,
                passwordHash=hash_password(args.password),
                role=SUPER_ADMIN_ROLE_CODE,
                plantId=plant.id,
                designation="Super Administrator",
            )
            db.add(user)
            await db.flush()
            print(f"Created Super Admin {user.email}")
        else:
            user.role = SUPER_ADMIN_ROLE_CODE
            if args.password:
                user.passwordHash = hash_password(args.password)
                print(f"Promoted {user.email} to {SUPER_ADMIN_ROLE_CODE} (password reset)")
            else:
                print(f"Promoted {user.email} to {SUPER_ADMIN_ROLE_CODE} (password unchanged)")

        # 4. Role assignment, unscoped — the Super Admin is not plant-bound.
        existing = (
            await db.execute(
                select(UserRole).where(UserRole.userId == user.id, UserRole.roleId == role.id)
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(UserRole(userId=user.id, roleId=role.id, scopeType=None, scopeValue=None))
            print(f"Assigned {SUPER_ADMIN_ROLE_CODE} role (all plants)")

        await db.commit()
        # ASCII only — the Windows console defaults to cp1252 and a stray arrow
        # raises UnicodeEncodeError *after* the commit, which reads like a failure.
        print("\nDone. Log in and go to Organisation > Modules to manage module access.")
        print(f"  email: {email}")
        return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Create or promote the organisation Super Admin")
    p.add_argument(
        "--email",
        default=None,
        help="Defaults to SUPER_ADMIN_EMAIL from settings (info@cgbindia.com)",
    )
    p.add_argument("--password", default=None, help="Required when creating a new account")
    p.add_argument("--name", default="Super Administrator")
    raise SystemExit(asyncio.run(run(p.parse_args())))


if __name__ == "__main__":
    main()
