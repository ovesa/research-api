import asyncpg

from app.config import settings

pool = None


async def get_pool():
    """Return the shared asyncpg connection pool, creating it on first call.
    Uses a module level singleton so the pool is created once at startup
    and reused across all requests. Opening a fresh connection per request
    would be slow and exhaust Postgres connection limits under load.

    Pool sizing:
        min_size=2: Always keep 2 connections warm and ready.
        max_size=10: Never exceed 10 simultaneous connections.

    Returns:
        asyncpg.Pool: The shared connection pool.
    """
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=10)
    return pool


async def close_pool():
    """Gracefully close all connections in the pool. Called during
    application shutdown via the FastAPI lifespan handler. Without this,
    connections may remain open on the Postgres side after the app exits.

    Returns:
        None
    """
    global pool
    if pool:
        await pool.close()
        pool = None
