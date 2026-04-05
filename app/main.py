from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.cache import close_redis, get_redis
from app.config import settings
from app.database import close_pool, get_pool
from app.routers import papers


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown of shared resources.

    FastAPI calls everything before the yield on startup and everything
    after the yield on shutdown.

    Startup:
        Warms up the Postgres connection pool and Redis client so the
        first request does not pay the connection cost. Without this,
        the first request after a cold start would be noticeably slower.

    Shutdown:
        Closes all Postgres connections and the Redis client cleanly.
        Without this, connections remain open on the server side after
        the app exits which can cause issues on frequent restarts.

    Args:
        app (FastAPI): The FastAPI application instance. Not used directly
            but required by the lifespan protocol.

    Yields:
        None: Control is yielded to FastAPI to handle requests.
    """
    # Startup
    await get_pool()
    await get_redis()
    print(f"*** {settings.app_name} started ***")
    print(f"*** Debug mode: {settings.debug} ***")

    yield

    # Shutdown
    await close_pool()
    await close_redis()
    print(f"*** {settings.app_name} shut down ***")


app = FastAPI(
    title=settings.app_name,
    description=(
        "A paper metadata API focusing on heliophysics/solar physics. "
        "Look up papers by DOI or arXiv ID and get normalized metadata "
        "including authors, abstracts, citation counts, and more. "
        "Only heliophysics-related papers are accepted based on keywords."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(papers.router)


@app.get("/health/live", tags=["health"])
async def liveness():
    """Confirm the application process is running.

    Used by container orchestrators like Kubernetes to know whether
    to restart the container. If this endpoint is unreachable the
    container is considered dead.

    Returns:
        dict: Static ok status.
    """
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def readiness():
    """Confirm the application is ready to serve requests.

    Checks that both Postgres and Redis are reachable. Used by container
    orchestrators to know whether to send traffic to this instance.
    Unlike liveness, a failed readiness check does not restart the
    container. It just stops sending it traffic until it recovers.

    Returns:
        dict: Status ok with confirmation that both services are connected.

    Raises:
        HTTPException: 503 if Postgres or Redis is unreachable.
    """
    from fastapi import HTTPException

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception:
        raise HTTPException(status_code=503, detail="Postgres unavailable")

    try:
        redis = await get_redis()
        await redis.ping()
    except Exception:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    return {
        "status": "ok",
        "postgres": "connected",
        "redis": "connected",
    }
