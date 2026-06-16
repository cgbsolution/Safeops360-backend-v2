"""Helpers shared by all models. Kept as `_base` (leading underscore) so it's
not re-exported from `app.models`.

Prisma uses cuid() defaults; Postgres-side we use a Python uuid4 hex by default
to keep IDs collision-free across the migration window. Existing rows keep
their cuid IDs — this only affects new inserts after the cutover.
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def gen_id() -> str:
    return uuid4().hex


class TimestampMixin:
    # camelCase to match Prisma's column naming convention. The DB has
    # `createdAt` / `updatedAt`; without these names matching, SQLAlchemy
    # generates `created_at` / `updated_at` SQL → Postgres "column does
    # not exist".
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # MUST be `default=func.now()` (NOT `server_default`). Prisma's
    # `@updatedAt` is client-managed — the DB column has NO DEFAULT.
    # A `server_default` would make SQLAlchemy omit updatedAt from the
    # INSERT and Postgres rejects it (NOT NULL, no default). Routes that
    # serialise the row via `model_validate` MUST `await db.refresh(x)`
    # first so the inline-NOW() value is loaded into memory.
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class IdMixin:
    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)


__all__ = ["Base", "IdMixin", "TimestampMixin", "gen_id"]
