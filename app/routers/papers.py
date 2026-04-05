import json
from datetime import datetime, timezone
from typing import Union

from fastapi import APIRouter, HTTPException

from app.cache import cache_paper, get_cached_paper
from app.models.paper import (
    BulkLookupRequest,
    CacheStats,
    DomainValidationError,
    IdentifierType,
    PaperLookupRequest,
    PaperMetadata,
)
from app.services.fetcher import fetch_by_arxiv, fetch_by_doi

router = APIRouter(prefix="/papers", tags=["papers"])

# In-memory cache hit/miss counters
_cache_hits = 0
_cache_misses = 0


@router.post(
    "/lookup",
    response_model=Union[PaperMetadata, DomainValidationError],
    summary="Look up a single heliophysics paper",
)
async def lookup_paper(request: PaperLookupRequest):
    """Look up a single paper by DOI or arXiv ID.

    Checks Redis cache first. On a cache miss, fetches from CrossRef
    or arXiv concurrently with Semantic Scholar, validates heliophysics
    relevance, and caches the result before returning.

    Args:
        request (PaperLookupRequest): Contains the identifier and its type.

    Returns:
        PaperMetadata: Full normalized metadata if the paper is found
            and passes heliophysics validation.
        DomainValidationError: Rejection details if the paper is not
            heliophysics-related or the identifier is not found.

    Raises:
        HTTPException: 500 if an unexpected error occurs during fetching.
    """
    global _cache_hits, _cache_misses

    # Check cache first
    cached = await get_cached_paper(request.identifier)
    if cached:
        _cache_hits += 1
        data = json.loads(cached)
        # DomainValidationError has no 'title' field as primary; check by key
        if "source" in data:
            return PaperMetadata(**data)
        return DomainValidationError(**data)

    _cache_misses += 1

    # Fetch from external APIs
    if request.identifier_type == IdentifierType.doi:
        result = await fetch_by_doi(request.identifier)
    else:
        result = await fetch_by_arxiv(request.identifier)

    # Cache the result regardless of whether it passed validation
    # Prevents hammering external APIs with repeated invalid lookups
    await cache_paper(request.identifier, json.dumps(result.model_dump(), default=str))

    return result


@router.post(
    "/bulk",
    response_model=list[Union[PaperMetadata, DomainValidationError]],
    summary="Look up multiple heliophysics papers in one call",
)
async def bulk_lookup(request: BulkLookupRequest):
    """Look up multiple papers concurrently by DOI or arXiv ID.

    All identifiers are fetched concurrently using asyncio.gather.
    Each result is independent, so one failed lookup does not affect others.
    Results are returned in the same order as the input identifiers.

    Args:
        request (BulkLookupRequest): Contains a list of identifiers and
            their shared type. Maximum 50 identifiers per request.

    Returns:
        list[PaperMetadata | DomainValidationError]: One result per
            identifier in the same order as the input list.
    """
    import asyncio

    async def fetch_one(identifier: str):
        """Fetch a single paper, checking cache first.

        Args:
            identifier (str): The DOI or arXiv ID to look up.

        Returns:
            PaperMetadata | DomainValidationError: The result for this
                identifier.
        """
        global _cache_hits, _cache_misses

        cached = await get_cached_paper(identifier)
        if cached:
            _cache_hits += 1
            data = json.loads(cached)
            if "source" in data:
                return PaperMetadata(**data)
            return DomainValidationError(**data)

        _cache_misses += 1

        if request.identifier_type == IdentifierType.doi:
            result = await fetch_by_doi(identifier)
        else:
            result = await fetch_by_arxiv(identifier)

        await cache_paper(identifier, json.dumps(result.model_dump(), default=str))
        return result

    results = await asyncio.gather(*[fetch_one(i) for i in request.identifiers])
    return list(results)


@router.get(
    "/metrics",
    response_model=CacheStats,
    summary="Cache performance statistics",
)
async def get_metrics():
    """Return cache hit and miss statistics.

    Exposes how effectively Redis is serving requests without hitting
    external APIs. A high hit rate means the cache is working well.
    In production this data would be tracked in Redis and exposed to
    Prometheus for time-series monitoring.

    Returns:
        CacheStats: Current hit count, miss count, and hit rate ratio.
    """
    total = _cache_hits + _cache_misses
    hit_rate = _cache_hits / total if total > 0 else 0.0
    return CacheStats(
        hits=_cache_hits,
        misses=_cache_misses,
        hit_rate=round(hit_rate, 4),
    )


@router.get(
    "/health",
    summary="Health check",
)
async def health():
    """Confirm the API is running.

    Returns:
        dict: Status ok with current server timestamp.
    """
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}
