import json
from datetime import datetime, timezone
from typing import Optional, Union

from fastapi import APIRouter, HTTPException, Query

from app.cache import cache_paper, get_cached_paper
from app.models.paper import (
    BulkLookupRequest,
    CacheStats,
    DomainValidationError,
    IdentifierType,
    PaperLookupRequest,
    PaperMetadata,
)
from app.services.database import get_paper, get_stats, list_papers, save_paper
from app.services.fetcher import fetch_by_arxiv, fetch_by_doi

router = APIRouter(prefix="/papers", tags=["papers"])

# In-memory cache hit/miss counters
# In production these would live in Redis so they persist across restarts
_cache_hits = 0
_cache_misses = 0


@router.post(
    "/lookup",
    response_model=Union[PaperMetadata, DomainValidationError],
    summary="Look up a single heliophysics paper",
)
async def lookup_paper(request: PaperLookupRequest):
    """Look up a single paper by DOI or arXiv ID.

    Checks three layers in order before hitting external APIs:
        (1) Redis cache: fastest, returns in under 1ms
        (2) Postgres: fast, avoids external API call for known papers
        (3) External APIs: CrossRef or arXiv fetched concurrently

    On a successful fetch the paper is saved to Postgres and cached
    in Redis. Rejected papers are cached but not saved to Postgres.

    Args:
        request (PaperLookupRequest): Contains the identifier and type.

    Returns:
        PaperMetadata: Full normalized metadata if paper is found and
            passes heliophysics validation.
        DomainValidationError: Rejection details if the paper is not
            heliophysics-related or the identifier is not found.
    """
    global _cache_hits, _cache_misses

    # (1) Redis cache
    cached = await get_cached_paper(request.identifier)
    if cached:
        _cache_hits += 1
        data = json.loads(cached)
        if "source" in data:
            return PaperMetadata(**data)
        return DomainValidationError(**data)

    # (2) Postgres
    stored = await get_paper(request.identifier)
    if stored:
        _cache_hits += 1
        # Re-populate Redis so next request is even faster
        await cache_paper(
            request.identifier, json.dumps(stored.model_dump(), default=str)
        )
        return stored

    _cache_misses += 1

    # (3) External APIs
    if request.identifier_type == IdentifierType.doi:
        result = await fetch_by_doi(request.identifier)
    else:
        result = await fetch_by_arxiv(request.identifier)

    # Cache the result regardless of validation outcome
    # This prevents hammering external APIs with repeated invalid lookups
    await cache_paper(request.identifier, json.dumps(result.model_dump(), default=str))

    # Only save to Postgres if the paper passed heliophysics validation
    if isinstance(result, PaperMetadata):
        await save_paper(result)

    return result


@router.post(
    "/bulk",
    response_model=list[Union[PaperMetadata, DomainValidationError]],
    summary="Look up multiple heliophysics papers in one call",
)
async def bulk_lookup(request: BulkLookupRequest):
    """Look up multiple papers concurrently by DOI or arXiv ID.

    All identifiers are fetched concurrently using asyncio.gather.
    Each result is independent, so one failed lookup does not affect
    others. Results are returned in the same order as the input.

    Args:
        request (BulkLookupRequest): Contains a list of identifiers
            and their shared type. Maximum 50 identifiers per request.

    Returns:
        list[PaperMetadata | DomainValidationError]: One result per
            identifier in the same order as the input list.
    """
    import asyncio

    async def fetch_one(identifier: str):
        """Fetch a single paper through all three cache layers.

        Args:
            identifier (str): The DOI or arXiv ID to look up.

        Returns:
            PaperMetadata | DomainValidationError: The result for
                this identifier.
        """
        global _cache_hits, _cache_misses

        # (1) Redis
        cached = await get_cached_paper(identifier)
        if cached:
            _cache_hits += 1
            data = json.loads(cached)
            if "source" in data:
                return PaperMetadata(**data)
            return DomainValidationError(**data)

        # (2) Postgres
        stored = await get_paper(identifier)
        if stored:
            _cache_hits += 1
            await cache_paper(identifier, json.dumps(stored.model_dump(), default=str))
            return stored

        _cache_misses += 1

        # (3) External APIs
        if request.identifier_type == IdentifierType.doi:
            result = await fetch_by_doi(identifier)
        else:
            result = await fetch_by_arxiv(identifier)

        await cache_paper(identifier, json.dumps(result.model_dump(), default=str))

        if isinstance(result, PaperMetadata):
            await save_paper(result)

        return result

    results = await asyncio.gather(*[fetch_one(i) for i in request.identifiers])
    return list(results)


@router.get(
    "/",
    summary="List all stored heliophysics papers",
)
async def list_all_papers(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    identifier_type: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None),
):
    """List all papers stored in Postgres with pagination and filtering.

    Args:
        limit (int): Number of papers to return. Between 1 and 100.
        offset (int): Number of papers to skip for pagination.
        identifier_type (str | None): Filter by 'doi' or 'arxiv'.
        source (str | None): Filter by source API e.g. 'crossref'.

    Returns:
        dict: Contains papers list, total count, limit, and offset
            for the client to construct pagination.
    """
    papers, total = await list_papers(
        limit=limit,
        offset=offset,
        identifier_type=identifier_type,
        source=source,
    )
    return {
        "papers": [p.model_dump() for p in papers],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get(
    "/stats",
    summary="Collection statistics",
)
async def collection_stats():
    """Return aggregate statistics about the stored paper collection.

    Queries Postgres directly for counts and breakdowns. Useful for
    README screenshots and demonstrating the API is actually being used.

    Returns:
        dict: Total papers, breakdown by source, breakdown by identifier
            type, and timestamp of the most recently fetched paper.
    """
    return await get_stats()


@router.get(
    "/metrics",
    response_model=CacheStats,
    summary="Cache performance statistics",
)
async def get_metrics():
    """Return cache hit and miss statistics.

    Args:
        None

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
    """Confirm the papers router is reachable.

    Returns:
        dict: Status ok with current server timestamp.
    """
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}
