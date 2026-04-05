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
from app.services.database import (
    get_paper,
    get_stats,
    list_papers,
    save_paper,
    search_papers,
)
from app.services.fetcher import fetch_by_ads, fetch_by_arxiv, fetch_by_doi
from app.services.ingestion import (
    IngestionResult,
    ingest_by_ids,
    ingest_date_range,
    ingest_latest_heliophysics,
)

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
    elif request.identifier_type == IdentifierType.arxiv:
        result = await fetch_by_arxiv(request.identifier)
    else:
        result = await fetch_by_ads(request.identifier)
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
    "/search",
    summary="Full text search across heliophysics papers",
)
async def search(
    q: str = Query(..., min_length=2, description="Search terms e.g. 'solar wind'"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Search stored papers by title and abstract using full text search.

    Uses Postgres tsvector search with ts_rank relevance scoring.
    Title matches rank higher than abstract matches. Results are
    ordered by relevance score descending so best matches appear first.

    Stemming is handled automatically; thus, searching 'wave' matches
    'waves', 'wavelength', 'waving'. Searching 'magnetohydrodynamic'
    matches 'magnetohydrodynamics'. This is significantly better than
    a LIKE query for scientific text.

    Args:
        q (str): Search terms. Minimum 2 characters. Multi-word queries
            like 'solar wind' are handled automatically.
        limit (int): Number of results to return. Between 1 and 100.
        offset (int): Number of results to skip for pagination.

    Returns:
        dict: Contains matching papers ordered by relevance, total
            match count, the original query, limit, and offset.

    Example:
        GET /papers/search?q=solar+wind
        GET /papers/search?q=magnetic+field&limit=5
        GET /papers/search?q=atmospheric+gravity+waves
    """
    if not q.strip():
        raise HTTPException(status_code=400, detail="Search query cannot be empty")

    papers, total = await search_papers(
        query=q.strip(),
        limit=limit,
        offset=offset,
    )

    return {
        "query": q.strip(),
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


@router.post(
    "/ingest/arxiv",
    summary="Collect latest heliophysics papers from arXiv",
)
async def ingest_from_arxiv(max_per_category: int = 25):
    """Fetch and store the latest heliophysics papers from arXiv.

    Searches all heliophysics arXiv categories concurrently, deduplicates
    results, skips papers already stored, and fetches new ones with rate
    limiting to respect arXiv's guidelines.

    This endpoint can take several minutes to complete depending on how
    many new papers are found. In production this would be triggered by
    a scheduled job rather than a direct API call.

    Args:
        max_per_category (int): Maximum papers to fetch per arXiv
            category. Defaults to 25. Total will be at most
            max_per_category * 3 categories minus duplicates.

    Returns:
        dict: Ingestion summary including total found, newly ingested,
            already stored, rejected, and failed counts plus the list
            of newly ingested arXiv IDs.
    """
    result = await ingest_latest_heliophysics(max_per_category=max_per_category)
    return {
        "total_found": result.total_found,
        "newly_ingested": result.newly_ingested,
        "already_stored": result.already_stored,
        "rejected": result.rejected,
        "failed": result.failed,
        "arxiv_ids": result.arxiv_ids,
    }


@router.post(
    "/ingest/ids",
    summary="Collect a specific list of arXiv papers",
)
async def ingest_specific_ids(arxiv_ids: list[str]):
    """Fetch and store a specific list of arXiv papers by ID.

    Useful when you have a curated list of papers to add rather than
    pulling the latest from arXiv categories. Skips papers already
    in Postgres.

    Args:
        arxiv_ids (list[str]): List of arXiv IDs to ingest.
            e.g. ['2509.19847', '2301.04380']

    Returns:
        dict: Ingestion summary with counts and newly ingested IDs.
    """
    result = await ingest_by_ids(arxiv_ids)
    return {
        "total_found": result.total_found,
        "newly_ingested": result.newly_ingested,
        "already_stored": result.already_stored,
        "rejected": result.rejected,
        "failed": result.failed,
        "arxiv_ids": result.arxiv_ids,
    }


@router.post(
    "/ingest/daterange",
    summary="Ingest heliophysics papers from a specific date range",
)
async def ingest_date_range_endpoint(
    start_date: str = Query(
        ..., description="Start date in YYYYMMDD format e.g. 20250101"
    ),
    end_date: str = Query(..., description="End date in YYYYMMDD format e.g. 20250131"),
    max_per_category: int = Query(default=100),
):
    """Ingest papers from arXiv submitted between two dates.

    Useful for backfilling data. Run once per month to
    build up a collection. Each run checks Postgres first so it is
    safe to re-run; already stored papers are skipped.

    Args:
        start_date (str): Start date in YYYYMMDD format.
        end_date (str): End date in YYYYMMDD format.
        max_per_category (int): Maximum papers per category. Defaults to 100.

    Returns:
        dict: Ingestion summary with counts and newly ingested IDs.
    """
    result = await ingest_date_range(
        start_date=start_date,
        end_date=end_date,
        max_per_category=max_per_category,
    )
    return {
        "start_date": start_date,
        "end_date": end_date,
        "total_found": result.total_found,
        "newly_ingested": result.newly_ingested,
        "already_stored": result.already_stored,
        "rejected": result.rejected,
        "failed": result.failed,
        "arxiv_ids": result.arxiv_ids,
    }


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
