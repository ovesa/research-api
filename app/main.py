from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.cache import close_redis, get_redis
from app.config import settings
from app.database import close_pool, get_pool
from app.logging_config import setup_logging
from app.middleware import RequestLoggingMiddleware
from app.routers import papers, agent 

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Set up logging before anything else
setup_logging(debug=settings.debug)
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown of shared resources.

    FastAPI calls everything before the yield on startup and everything
    after the yield on shutdown. Warms up Postgres and Redis connections
    so the first request does not pay the connection cost.

    Args:
        app (FastAPI): The FastAPI application instance.

    Yields:
        None: Control is yielded to FastAPI to handle requests.
    """
    # Startup
    await get_pool()
    await get_redis()
    logger.info(
        "application_started",
        app_name=settings.app_name,
        debug=settings.debug,
    )

    yield

    # Shutdown
    await close_pool()
    await close_redis()
    logger.info("application_stopped", app_name=settings.app_name)


limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title=settings.app_name,
    description=(
        "A heliophysics research paper metadata API. "
        "Look up papers by DOI or arXiv ID and get normalized metadata "
        "including authors, abstracts, citation counts, and more. "
        "Only heliophysics-related papers are accepted."
    ),
    version="0.1.0",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add request logging middleware
app.add_middleware(RequestLoggingMiddleware)

app.include_router(papers.router)
app.include_router(agent.router) 

@app.get("/health/live", tags=["health"])
async def liveness():
    """Confirm the application process is running.

    Returns:
        dict: Static ok status.
    """
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def readiness():
    """Confirm the application is ready to serve requests.

    Checks that both Postgres and Redis are reachable. Used by container
    orchestrators to know whether to send traffic to this instance.

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
