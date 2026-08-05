from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()


def _is_transaction_pooler(url: str) -> bool:
    """Supabase exposes two poolers:
      :5432  session pooler   — SUPPORTS prepared statements
      :6543  transaction pool — does NOT support prepared statements
    We need to disable asyncpg's prepared-statement cache only on the
    transaction pooler. On the session pooler (which is what the dev URL uses)
    keeping the cache on is a major performance win.
    """
    return ":6543/" in url or url.endswith(":6543")


_async_url = settings.async_database_url
_disable_pstmt_cache = _is_transaction_pooler(_async_url)

# pool_size + max_overflow tuned for a single uvicorn worker handling a
# typical small dashboard load. With Supabase pooler latency at ~30-50ms
# RTT from India, we want enough connections to keep concurrent requests
# from waiting on the pool. pool_recycle=1800 (30 min) avoids Supabase's
# idle-connection reaper.
engine = create_async_engine(
    _async_url,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    connect_args=(
        {"statement_cache_size": 0, "prepared_statement_cache_size": 0}
        if _disable_pstmt_cache
        # Default cache sizes — asyncpg defaults to 100 prepared statements.
        # Each cached statement saves ~30-50ms of round-trip on subsequent
        # calls. This is the single biggest perf win for this app.
        else {}
    ),
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""


async def get_db() -> AsyncGenerator[AsyncSession, Any]:
    """FastAPI dependency that yields a transactional session per request.

    After the business commit, drains any audit events the ORM layer captured
    during the request (P1-1). The drain uses a separate session, so an
    audit-write failure never rolls back the business change.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
            try:
                from app.services.audit_log import drain_audit

                await drain_audit(session)
            except Exception:  # noqa: BLE001 — audit must never break the request
                pass
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def warmup() -> None:
    """Open one connection at app start so the first user-facing request
    doesn't pay the TCP+TLS+auth handshake. Cheap, but visibly improves the
    first page-load after a deploy."""
    from sqlalchemy import text

    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
