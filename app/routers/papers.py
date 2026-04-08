import json
from datetime import datetime, timezone
from typing import Optional, Union

from fastapi import APIRouter, HTTPException, Query, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.cache import cache_paper, get_cached_paper
from app.models.paper import (
    BulkLookupRequest,
    CacheStats,
    DomainValidationError,
    IdentifierType,
    PaperLookupRequest,
    PaperMetadata,
    PaperPatchRequest,
)
from app.services.database import (
    delete_paper,
    filter_papers_by_keywords,
    get_paper,
    get_stats,
    list_papers,
    patch_paper,
    save_paper,
    search_papers,
)
from app.services.fetcher import fetch_by_ads, fetch_by_arxiv, fetch_by_doi
from app.services.ingestion import (
    ingest_by_ids,
    ingest_date_range,
    ingest_from_ads,
    ingest_latest_heliophysics,
)

router = APIRouter(prefix="/papers", tags=["papers"])
limiter = Limiter(key_func=get_remote_address)
# In-memory cache hit/miss counters
# In production these would live in Redis so they persist across restarts
_cache_hits = 0
_cache_misses = 0


def pagination_meta(total: int, limit: int, offset: int) -> dict:
    """Calculate pagination metadata for list responses.

    Args:
        total (int): Total number of matching records.
        limit (int): Page size.
        offset (int): Current offset.

    Returns:
        dict: Pagination metadata including page counts and nav flags.
    """
    import math

    total_pages = math.ceil(total / limit) if total > 0 else 1
    current_page = (offset // limit) + 1
    has_next = offset + limit < total
    has_prev = offset > 0

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "total_pages": total_pages,
        "current_page": current_page,
        "has_next": has_next,
        "has_prev": has_prev,
        "next_offset": offset + limit if has_next else None,
        "prev_offset": max(offset - limit, 0) if has_prev else None,
    }


@router.post(
    "/lookup",
    response_model=Union[PaperMetadata, DomainValidationError],
    summary="Look up a single heliophysics paper",
)
@limiter.limit("30/minute")
async def lookup_paper(request: Request, request_body: PaperLookupRequest):
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
    cached = await get_cached_paper(request_body.identifier)
    if cached:
        _cache_hits += 1
        data = json.loads(cached)
        if "source" in data:
            return PaperMetadata(**data)
        return DomainValidationError(**data)

    # (2) Postgres
    stored = await get_paper(request_body.identifier)
    if stored:
        _cache_hits += 1
        # Re-populate Redis so next request is even faster
        await cache_paper(
            request_body.identifier, json.dumps(stored.model_dump(), default=str)
        )
        return stored

    _cache_misses += 1

    # (3) External APIs
    if request_body.identifier_type == IdentifierType.doi:
        result = await fetch_by_doi(request_body.identifier)
    elif request_body.identifier_type == IdentifierType.arxiv:
        result = await fetch_by_arxiv(request_body.identifier)
    else:
        result = await fetch_by_ads(request_body.identifier)
    # Cache the result regardless of validation outcome
    await cache_paper(
        request_body.identifier, json.dumps(result.model_dump(), default=str)
    )

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
        elif request.identifier_type == IdentifierType.arxiv:
            result = await fetch_by_arxiv(identifier)
        else:
            result = await fetch_by_ads(identifier)

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
    sort_by: str = Query(
        default="fetched_at",
        description="Field to sort by. One of: fetched_at, published_date, citation_count, title",
    ),
    sort_order: str = Query(
        default="desc",
        description="Sort direction: asc or desc",
    ),
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
    # Whitelist allowed sort fields to prevent SQL injection
    allowed_sort_fields = {"fetched_at", "published_date", "citation_count", "title"}
    if sort_by not in allowed_sort_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sort_by field. Must be one of: {', '.join(sorted(allowed_sort_fields))}",
        )
    if sort_order.lower() not in ("asc", "desc"):
        raise HTTPException(
            status_code=400,
            detail="sort_order must be 'asc' or 'desc'",
        )

    papers, total = await list_papers(
        limit=limit,
        offset=offset,
        identifier_type=identifier_type,
        source=source,
        sort_by=sort_by,
        sort_order=sort_order.lower(),
    )
    return {
        **pagination_meta(total, limit, offset),
        "papers": [p.model_dump() for p in papers],
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
        **pagination_meta(total, limit, offset),
        "papers": [p.model_dump() for p in papers],
    }


@router.get(
    "/filter",
    summary="Filter papers by specific keywords",
)
async def filter_by_keywords(
    keywords: str = Query(
        ...,
        description="Comma-separated keywords e.g. 'inertial modes,rossby waves,helioseismology'",
    ),
    match_all: bool = Query(
        default=False,
        description="If true, paper must contain ALL keywords. If false, ANY keyword matches.",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Filter stored papers by one or more explicit keywords.

    Uses case-insensitive substring matching against title and abstract.
    Unlike /search, there is no stemming or relevance ranking. It is also
    case insensitive.

    Args:
        keywords (str): Comma-separated list of keywords to filter by.
        match_all (bool): If True, only papers containing ALL keywords
            are returned. If False, papers containing ANY keyword match.
            Defaults to False.
        limit (int): Number of results to return. Between 1 and 100.
        offset (int): Number of results to skip for pagination.

    Returns:
        dict: Matching papers, total count, and the parsed keyword list.

    Example:
        GET /papers/filter?keywords=inertial+modes,rossby+waves
        GET /papers/filter?keywords=helioseismology,solar+wind&match_all=true
    """
    parsed = [kw.strip() for kw in keywords.split(",") if kw.strip()]

    if not parsed:
        raise HTTPException(status_code=400, detail="At least one keyword is required")

    papers, total = await filter_papers_by_keywords(
        keywords=parsed,
        match_all=match_all,
        limit=limit,
        offset=offset,
    )

    return {
        "keywords": parsed,
        "match_all": match_all,
        **pagination_meta(total, limit, offset),
        "papers": [p.model_dump() for p in papers],
    }


@router.delete(
    "/{identifier}",
    summary="Delete a paper from the collection",
)
async def remove_paper(identifier: str):
    """Delete a single paper by its identifier.

    Removes the paper from Postgres and invalidates its Redis cache
    entry so stale data is not served after deletion. Returns 404 if
    no paper with that identifier exists.

    This endpoint is useful for removing papers that were ingested by
    mistake, failed manual domain review, or are otherwise unwanted.

    Args:
        identifier (str): The DOI, arXiv ID, or ADS bibcode to delete.
            e.g. '2501.19169', '10.1007/s11207-021-01842-0'

    Returns:
        dict: Confirmation message and the deleted identifier.

    Raises:
        HTTPException 404: If no paper with that identifier exists.
    """
    from app.cache import delete_cached_paper

    deleted = await delete_paper(identifier)

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"No paper found with identifier '{identifier}'",
        )

    await delete_cached_paper(identifier)

    return {
        "deleted": True,
        "identifier": identifier,
    }


@router.patch(
    "/{identifier}",
    response_model=PaperMetadata,
    summary="Partially update a stored paper",
)
async def update_paper(identifier: str, request: PaperPatchRequest):
    """Partially update fields on a stored paper.

    Only the fields provided in the request body are updated. All
    other fields are left unchanged. Useful for manually correcting
    a title, filling in a missing abstract, fixing a URL, or adding
    a DOI that was missing at ingestion time.

    After updating, the Redis cache entry is refreshed so subsequent
    lookups return the corrected data immediately.

    Args:
        identifier (str): The DOI, arXiv ID, or ADS bibcode to update.
        request (PaperPatchRequest): Fields to update. Only non-null
            fields in the request body will be applied.

    Returns:
        PaperMetadata: The full updated paper metadata.

    Raises:
        HTTPException 404: If no paper with that identifier exists.
        HTTPException 400: If no fields are provided to update.
    """
    import json

    # Extract only the fields that were explicitly provided
    updates = {
        field: value
        for field, value in request.model_dump().items()
        if value is not None
    }

    if not updates:
        raise HTTPException(
            status_code=400,
            detail="At least one field must be provided to update.",
        )

    updated = await patch_paper(identifier, updates)

    if not updated:
        raise HTTPException(
            status_code=404,
            detail=f"No paper found with identifier '{identifier}'",
        )

    # Refresh Redis cache with the updated data
    await cache_paper(identifier, json.dumps(updated.model_dump(), default=str))

    return updated


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


@router.post(
    "/ingest/ads",
    summary="Ingest heliophysics papers from NASA ADS",
)
async def ingest_from_ads_endpoint(
    start_date: str = Query(
        ..., description="Start date in YYYY-MM format e.g. 2025-01"
    ),
    end_date: str = Query(..., description="End date in YYYY-MM format e.g. 2025-03"),
    max_results: int = Query(default=100),
    keywords: str = Query(
        default=("inertial modes OR rossby waves OR helioseismology"),
    ),
):
    """Ingest heliophysics papers from NASA ADS within a date range.

    Searches ADS across core heliophysics journals for papers published
    between the given dates. ADS is preferred over arXiv for published
    papers because it has explicit journal coverage and richer metadata.

    Args:
        start_date (str): Start date in YYYY-MM format. e.g. '2025-01'
        end_date (str): End date in YYYY-MM format. e.g. '2025-03'
        max_results (int): Maximum papers to retrieve. Defaults to 100.
        keywords (str): Search keywords. Defaults to core heliophysics terms.

    Returns:
        dict: Ingestion summary with counts and newly ingested bibcodes.
    """
    result = await ingest_from_ads(
        start_date=start_date,
        end_date=end_date,
        keywords=keywords,
        max_results=max_results,
    )
    return {
        "start_date": start_date,
        "end_date": end_date,
        "total_found": result.total_found,
        "newly_ingested": result.newly_ingested,
        "already_stored": result.already_stored,
        "rejected": result.rejected,
        "failed": result.failed,
        "bibcodes": result.arxiv_ids,
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
