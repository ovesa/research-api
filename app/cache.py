import redis.asyncio as aioredis
from app.config import settings

redis = None

async def get_redis():
    """Return the shared async Redis client, creating it on first call.

    Uses a module-level singleton for the same reason as the Postgres
    pool — one client shared across all requests rather than
    reconnecting each time.

    Returns:
        redis.asyncio.Redis: The shared Redis client.
    """
    global redis
    if redis is None:
        redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True
        )
    return redis

async def get_cached_paper(identifier: str) -> str | None:
    """Look up a paper in Redis by its identifier.

    Args:
        identifier (str): The DOI or arXiv ID used as the cache key.

    Returns:
        str | None: The cached JSON string if found, None if not in cache.
    """
    client = await get_redis()
    return await client.get(f"paper:{identifier}")

async def cache_paper(identifier: str, data: str) -> None:
    """Store a paper's JSON representation in Redis with a TTL.

    The TTL is set from config (default 24 hours). Papers are stable
    documents so a long TTL is safe and keeps external API calls
    to a minimum.

    Args:
        identifier (str): The DOI or arXiv ID used as the cache key.
        data (str): The JSON string to store in Redis.

    Returns:
        None
    """
    client = await get_redis()
    await client.setex(
        f"paper:{identifier}",
        settings.cache_ttl_seconds,
        data
    )

async def close_redis():
    """Close the Redis connection cleanly on app shutdown.

    Returns:
        None
    """
    global redis
    if redis:
        await redis.aclose()
        redis = None