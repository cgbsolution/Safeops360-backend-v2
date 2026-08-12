"""Per-recipient portal tokens with a ROLE - additive DDL.

Until now a portal token was one credential per audit for one supplier contact.
A supplier audit actually has several external parties who each need their own
link:

    SUPPLIER_MANAGER  the counterpart who answers for the audited factory —
                      the supplier-side stand-in for our plant manager
    CO_AUDITOR        an external auditor who CONDUCTS part of the audit
    AUDITEE           a factory-side owner who responds to findings

Two things change:

  1. `role` on the token, so one link can be told from another and the portal can
     decide what that holder may do.

  2. Several LIVE tokens per audit. `issue_token` previously revoked every prior
     live token for the audit, on the sound reasoning that two valid credentials
     mean revoking the leaked one does not close access. That reasoning is kept —
     it is just narrowed to the right key: re-issuing revokes the prior token for
     the SAME (audit, email, role), so one person's link can be rotated without
     cutting off everyone else on the same audit.

     A partial unique index now enforces that at the DATABASE, which the old
     rule never was — it lived only in `issue_token`. Nothing is dropped: the
     existing `ix_SupplierPortalToken_audit_live` is a plain lookup index for
     "the live tokens on this audit", and is more useful now, not less.

`contactRole` is deliberately NOT a Postgres enum. The existing table uses plain
text for `engagementKind` for the same reason — adding a value to an enum needs
DDL, and a role list that cannot grow without a migration is a role list that
gets worked around.

Additive + re-runnable. Never `prisma db push` (Cams* drift would drop tables).

    python scripts/add_portal_roles.py

WARNING: the backend .env points at PRODUCTION.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import get_settings  # noqa: E402

STMTS: list[str] = [
    # ── 1. The role ────────────────────────────────────────────────────────
    # Defaulted to SUPPLIER_MANAGER so every token issued before this migration
    # keeps working and keeps meaning what it meant: the supplier contact who
    # answers for the factory.
    '''
    ALTER TABLE "SupplierPortalToken"
      ADD COLUMN IF NOT EXISTS "role" TEXT NOT NULL DEFAULT 'SUPPLIER_MANAGER'
    ''',
    # Disciplines an external CO_AUDITOR is scoped to conduct. Empty/NULL means
    # "every discipline in scope", matching how `selectedDisciplineIds` on the
    # audit already treats an empty list.
    '''
    ALTER TABLE "SupplierPortalToken"
      ADD COLUMN IF NOT EXISTS "disciplineCodes" JSONB NOT NULL DEFAULT '[]'::jsonb
    ''',
    'CREATE INDEX IF NOT EXISTS "SupplierPortalToken_role_idx" ON "SupplierPortalToken" ("role")',

    # ── 2. One live credential per person per role per audit ───────────────
    #
    # ADDED, not replacing anything. `ix_SupplierPortalToken_audit_live` is a
    # plain btree on (auditId, revokedAt, expiresAt) — a LOOKUP index for "the
    # live tokens on this audit", which is exactly what the supplier panel
    # queries and is more useful now that an audit has several. The old
    # one-token-per-audit rule was never a database constraint; it lived only in
    # `issue_token`. So there is nothing to drop here.
    #
    # This index is the first time the rule is enforced by the database: at most
    # one live credential per (audit, email, role), so a caller that forgets to
    # revoke cannot leave two working links for one person — which is what makes
    # revoking a leaked one actually close access.
    '''
    CREATE UNIQUE INDEX IF NOT EXISTS "ix_SupplierPortalToken_audit_email_role_live"
      ON "SupplierPortalToken" ("auditId", lower("supplierContactEmail"), "role")
      WHERE "revokedAt" IS NULL
    ''',

    # ── 3. Attribution for external submissions ────────────────────────────
    # An external co-auditor's GRADE is not a comment — it is an audit verdict.
    # The interaction thread records who took every action, and for an external
    # party there is no `User` row to point at, so the token's identity is
    # recorded instead: which token, which email, which role. Without this the
    # thread would show a verdict with no discoverable author.
    '''
    ALTER TABLE "CheckpointInteraction"
      ADD COLUMN IF NOT EXISTS "externalTokenId" TEXT
    ''',
    '''
    ALTER TABLE "CheckpointInteraction"
      ADD COLUMN IF NOT EXISTS "externalActorEmail" TEXT
    ''',
    '''
    ALTER TABLE "CheckpointInteraction"
      ADD COLUMN IF NOT EXISTS "externalActorName" TEXT
    ''',
    'CREATE INDEX IF NOT EXISTS "CheckpointInteraction_extToken_idx" '
    'ON "CheckpointInteraction" ("externalTokenId")',
]

VERIFY = [
    (
        "SupplierPortalToken.role",
        """SELECT count(*) FROM information_schema.columns
           WHERE table_name='SupplierPortalToken' AND column_name='role'""",
    ),
    (
        "SupplierPortalToken.disciplineCodes",
        """SELECT count(*) FROM information_schema.columns
           WHERE table_name='SupplierPortalToken' AND column_name='disciplineCodes'""",
    ),
    (
        "per-person live-token unique index",
        """SELECT count(*) FROM pg_indexes
           WHERE indexname='ix_SupplierPortalToken_audit_email_role_live'""",
    ),
    (
        "existing per-audit lookup index preserved",
        """SELECT count(*) FROM pg_indexes
           WHERE indexname='ix_SupplierPortalToken_audit_live'""",
    ),
    (
        "CheckpointInteraction.externalActorEmail",
        """SELECT count(*) FROM information_schema.columns
           WHERE table_name='CheckpointInteraction' AND column_name='externalActorEmail'""",
    ),
]


def main() -> None:
    settings = get_settings()
    engine = create_engine(settings.sync_database_url, pool_pre_ping=True)
    with engine.begin() as conn:
        for stmt in STMTS:
            conn.execute(text(stmt))
            print(f"  ok  {' '.join(stmt.split())[:96]}")

    print("\nVerifying:")
    ok = True
    with engine.connect() as conn:
        for label, q in VERIFY:
            got = conn.execute(text(q)).scalar() or 0
            mark = "PASS" if got else "FAIL"
            if not got:
                ok = False
            print(f"  {mark}  {label}")
    print("\nDone." if ok else "\nSomething did not apply — re-read the output above.")


if __name__ == "__main__":
    main()
