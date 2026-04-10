import asyncio
from dataclasses import dataclass
from app.config import settings
import httpx
import structlog

from app.models.paper import (
    HELIOPHYSICS_ARXIV_CATEGORIES,
    SOLAR_PHYSICS_JOURNAL_BIBSTEMS,
    DomainValidationError,
    is_conference_abstract,
)

from app.services.database import get_paper, save_paper
logger = structlog.get_logger(__name__)


@dataclass
class IngestionResult:
    """Summary of a bulk ingestion run.

    Returned after an ingestion job completes so the caller knows
    exactly what happened (e.g., how many papers were found, how many
    passed validation, how many were already in the database, and
    how many failed).

    Attributes:
        total_found (int): Total papers returned by the arXiv search.
        already_stored (int): Papers skipped because they were already
            in Postgres. Not re-fetched or re-validated.
        newly_ingested (int): Papers successfully fetched, validated,
            and saved to Postgres during this run.
        rejected (int): Papers that failed heliophysics validation.
            Counted but not stored.
        failed (int): Papers that errored during fetching. Network
            timeouts, malformed responses, etc.
        arxiv_ids (list[str]): The arXiv IDs that were newly ingested.
    """

    total_found: int
    already_stored: int
    newly_ingested: int
    rejected: int
    failed: int
    arxiv_ids: list[str]


def _make_ingestion_client() -> httpx.AsyncClient:
    """Create an httpx client configured for arXiv ingestion requests.

    Uses a longer timeout than the standard client because arXiv category
    search queries can be slow, especially when fetching many results.
    Sequential category searches are safer than concurrent ones to avoid
    triggering arXiv's rate limiter.

    Returns:
        httpx.AsyncClient: Configured client ready for use as a context manager.
    """
    return httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": "research-api/0.1 (heliophysics paper lookup)"},
    )


async def _search_arxiv_category(
    category: str,
    max_results: int = 25,
) -> list[str]:
    """Search arXiv for recent papers in a given category.

    Uses the arXiv API search endpoint to find recent submissions.
    Results are sorted by submission date descending so the most
    recent papers appear first.

    Args:
        category (str): The arXiv category to search.
            e.g. 'astro-ph.SR', 'physics.space-ph'
        max_results (int): Maximum number of results to return.
            Capped at 25 per category to avoid rate limiting.
            Defaults to 25.

    Returns:
        list[str]: List of arXiv IDs found in this category.
            Returns empty list on timeout or rate limit.
    """
    async with _make_ingestion_client() as client:
        url = (
            f"https://export.arxiv.org/api/query"
            f"?search_query=cat:{category}"
            f"+AND+(solar+OR+heliosphere+OR+corona+OR+magnetosphere+OR+space+weather)"
            f"&sortBy=submittedDate"
            f"&sortOrder=descending"
            f"&max_results={max_results}"
        )

        try:
            response = await client.get(url)
        except httpx.ReadTimeout:
            logger.warning(
                "arxiv_search_timeout",
                category=category,
            )
            return []

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            logger.warning(
                "arxiv_rate_limited",
                category=category,
                retry_after=retry_after,
            )
            return []

        if response.status_code != 200:
            logger.warning(
                "arxiv_search_failed",
                category=category,
                status_code=response.status_code,
            )
            return []

        text = response.text
        ids = []

        remaining = text
        while "<id>http://arxiv.org/abs/" in remaining:
            start = remaining.find("<id>http://arxiv.org/abs/") + 25
            end = remaining.find("</id>", start)
            if end == -1:
                break
            raw_id = remaining[start:end].strip()
            clean_id = raw_id.split("v")[0]
            ids.append(clean_id)
            remaining = remaining[end + 5 :]

        logger.info(
            "arxiv_search_complete",
            category=category,
            papers_found=len(ids),
        )
        return ids


async def _search_arxiv_date_range(
    category: str,
    start_date: str,
    end_date: str,
    max_results: int = 100,
) -> list[str]:
    """Search arXiv for papers in a category within a date range.

    Uses the arXiv API submittedDate filter to find papers submitted
    between two dates. Useful for backfilling historical data.

    Args:
        category (str): The arXiv category to search.
        start_date (str): Start date in YYYYMMDD format. e.g. '20240101'
        end_date (str): End date in YYYYMMDD format. e.g. '20240331'
        max_results (int): Maximum results to return. Defaults to 100.

    Returns:
        list[str]: List of arXiv IDs found in this category and date range.
            Returns empty list on timeout or rate limit.
    """
    async with _make_ingestion_client() as client:
        url = (
            f"https://export.arxiv.org/api/query"
            f"?search_query=cat:{category}"
            f"+AND+submittedDate:[{start_date}0000+TO+{end_date}2359]"
            f"&sortBy=submittedDate"
            f"&sortOrder=descending"
            f"&max_results={max_results}"
        )

        try:
            response = await client.get(url)
        except httpx.ReadTimeout:
            logger.warning(
                "arxiv_date_search_timeout",
                category=category,
                start_date=start_date,
                end_date=end_date,
            )
            return []

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            logger.warning(
                "arxiv_rate_limited",
                category=category,
                retry_after=retry_after,
                start_date=start_date,
                end_date=end_date,
            )
            return []

        if response.status_code != 200:
            logger.warning(
                "arxiv_date_search_failed",
                category=category,
                status_code=response.status_code,
                start_date=start_date,
                end_date=end_date,
            )
            return []

        text = response.text
        ids = []

        remaining = text
        while "<id>http://arxiv.org/abs/" in remaining:
            start = remaining.find("<id>http://arxiv.org/abs/") + 25
            end = remaining.find("</id>", start)
            if end == -1:
                break
            raw_id = remaining[start:end].strip()
            clean_id = raw_id.split("v")[0]
            ids.append(clean_id)
            remaining = remaining[end + 5 :]

        logger.info(
            "arxiv_date_search_complete",
            category=category,
            start_date=start_date,
            end_date=end_date,
            papers_found=len(ids),
        )
        return ids


async def _search_ads(
    query: str,
    start_date: str,
    end_date: str,
    max_results: int = 100,
) -> list[str]:
    """Search NASA ADS for heliophysics papers within a date range.

    Uses the ADS search API to find papers published in heliophysics
    journals between two dates. ADS is better than arXiv for finding
    published papers because it indexes all major journals directly.

    Args:
        query (str): ADS search query string containing keywords.
        start_date (str): Start date in YYYY-MM format. e.g. '2025-01'
        end_date (str): End date in YYYY-MM format. e.g. '2025-03'
        max_results (int): Maximum results to return. Defaults to 100.

    Returns:
        list[str]: List of ADS bibcodes found matching the query.
            Returns empty list on error or rate limit.
    """
    if not settings.ads_api_token:
        logger.warning("ads_token_missing")
        return []

    async with _make_ingestion_client() as client:
        full_query = f"({query}) AND pubdate:[{start_date} TO {end_date}]"

        try:
            response = await client.get(
                "https://api.adsabs.harvard.edu/v1/search/query",
                params={
                    "q": full_query,
                    "fl": "bibcode",
                    "rows": max_results,
                    "sort": "pubdate desc",
                },
                headers={
                    "Authorization": f"Bearer {settings.ads_api_token}",
                    "User-Agent": "research-api/0.1 (heliophysics paper lookup)",
                },
            )
        except httpx.ReadTimeout:
            logger.warning("ads_search_timeout", query=query)
            return []

        if response.status_code == 429:
            logger.warning(
                "ads_rate_limited",
                retry_after=response.headers.get("Retry-After", "unknown"),
            )
            return []

        if response.status_code != 200:
            logger.warning(
                "ads_search_failed",
                status_code=response.status_code,
                query=query,
            )
            return []

        data = response.json()
        docs = data.get("response", {}).get("docs", [])
        bibcodes = [doc["bibcode"] for doc in docs if "bibcode" in doc]

        logger.info(
            "ads_search_complete",
            query=query,
            start_date=start_date,
            end_date=end_date,
            papers_found=len(bibcodes),
        )
        return bibcodes


async def _search_ads_broad(
    start_date: str,
    end_date: str,
    max_results: int = 500,
) -> list[str]:
    """Search NASA ADS for ALL solar physics papers from core journals.
    Broad-mode ingestion: sweeps every paper published in the configured set of
    heliophysics journals (SOLAR_PHYSICS_JOURNAL_BIBSTEMS) within the given
    date range, with no keyword filter.

    Args:
        start_date (str): Start date in YYYY-MM format. e.g. '2025-01'
        end_date (str): End date in YYYY-MM format. e.g. '2025-03'
        max_results (int): Maximum total results. Defaults to 500.

    Returns:
        list[str]: List of ADS bibcodes from the target journals.
    """
    if not settings.ads_api_token:
        logger.warning("ads_token_missing")
        return []

    bibstem_clause = " OR ".join(
        f"bibstem:{b}" for b in sorted(SOLAR_PHYSICS_JOURNAL_BIBSTEMS)
    )
    full_query = f"({bibstem_clause}) AND pubdate:[{start_date} TO {end_date}]"

    async with _make_ingestion_client() as client:
        try:
            response = await client.get(
                "https://api.adsabs.harvard.edu/v1/search/query",
                params={
                    "q": full_query,
                    "fl": "bibcode",
                    "rows": max_results,
                    "sort": "pubdate desc",
                },
                headers={
                    "Authorization": f"Bearer {settings.ads_api_token}",
                    "User-Agent": "research-api/0.1 (heliophysics paper lookup)",
                },
            )
        except httpx.ReadTimeout:
            logger.warning(
                "ads_broad_search_timeout",
                start_date=start_date,
                end_date=end_date,
            )
            return []

        if response.status_code == 429:
            logger.warning(
                "ads_rate_limited",
                retry_after=response.headers.get("Retry-After", "unknown"),
            )
            return []

        if response.status_code != 200:
            logger.warning(
                "ads_broad_search_failed",
                status_code=response.status_code,
            )
            return []

        data = response.json()
        docs = data.get("response", {}).get("docs", [])
        bibcodes = [doc["bibcode"] for doc in docs if "bibcode" in doc]

        logger.info(
            "ads_broad_search_complete",
            start_date=start_date,
            end_date=end_date,
            journals=len(SOLAR_PHYSICS_JOURNAL_BIBSTEMS),
            papers_found=len(bibcodes),
        )
        return bibcodes


async def ingest_from_ads(
    start_date: str,
    end_date: str,
    keywords: str = ("inertial modes OR rossby waves OR helioseismology"),
    max_results: int = 100,
    mode: str = "keyword",
) -> IngestionResult:
    """Ingest heliophysics papers from NASA ADS within a date range.

    Searches ADS for papers published in core heliophysics journals
    between the given dates. ADS is preferred over arXiv for finding
    published papers because it has explicit journal coverage and
    richer metadata including abstracts and citation counts.

    Args:
        start_date (str): Start date in YYYY-MM format. e.g. '2025-01'
        end_date (str): End date in YYYY-MM format. e.g. '2025-03'
        keywords (str): Search keywords to narrow results. Defaults
            to core heliophysics terms.
        max_results (int): Maximum papers to retrieve. Defaults to 100.
        mode (str): Ingestion mode. Either "keyword" or "broad".

    Returns:
        IngestionResult: Summary of what was ingested, skipped,
            rejected, and failed.
    """
    log = logger.bind(start_date=start_date, end_date=end_date, mode=mode)
    log.info(
        "ads_ingestion_started",
        keywords=keywords if mode == "keyword" else "(broad — all journals)",
    )

    if mode == "broad":
        effective_max = max_results if max_results != 100 else 500
        bibcodes = await _search_ads_broad(start_date, end_date, effective_max)
    else:
        bibcodes = await _search_ads(keywords, start_date, end_date, max_results)
    log.info("ads_ids_collected", total_unique=len(bibcodes))

    result = IngestionResult(
        total_found=len(bibcodes),
        already_stored=0,
        newly_ingested=0,
        rejected=0,
        failed=0,
        arxiv_ids=[],
    )

    for bibcode in bibcodes:
        try:
            if is_conference_abstract(bibcode):
                result.rejected += 1
                log.info("ads_ingestion_skipped", bibcode=bibcode, reason="conference_abstract")
                continue
            existing = await get_paper(bibcode)
            if existing:
                result.already_stored += 1
                log.info(
                    "ads_ingestion_skipped", bibcode=bibcode, reason="already_stored"
                )
                continue

            from app.services.fetcher import fetch_by_ads

            paper = await fetch_by_ads(bibcode)

            if isinstance(paper, DomainValidationError):
                result.rejected += 1
                log.info(
                    "ads_ingestion_rejected",
                    bibcode=bibcode,
                    reason=paper.reason,
                )
                continue

            await save_paper(paper)
            result.newly_ingested += 1
            result.arxiv_ids.append(bibcode)
            log.info("ads_ingestion_saved", bibcode=bibcode, title=paper.title)

            # ADS rate limit
            await asyncio.sleep(0.25)

        except Exception as e:
            result.failed += 1
            log.error("ads_ingestion_error", bibcode=bibcode, error=str(e))

    log.info(
        "ads_ingestion_complete",
        total_found=result.total_found,
        newly_ingested=result.newly_ingested,
        rejected=result.rejected,
        failed=result.failed,
    )
    return result


async def ingest_latest_heliophysics(
    max_per_category: int = 25,
) -> IngestionResult:
    """Fetch and store the latest heliophysics papers from arXiv.

    Searches all heliophysics arXiv categories sequentially with a
    1 second pause between each to respect arXiv rate limits. Deduplicates
    results across categories, skips papers already in Postgres, and
    fetches new ones with rate limiting between each fetch.

    Args:
        max_per_category (int): Maximum papers to fetch per arXiv
            category. Defaults to 25. Total papers fetched will be
            at most max_per_category * number of categories, minus
            duplicates and already-stored papers.

    Returns:
        IngestionResult: Summary of what was found, skipped, ingested,
            rejected, and failed during this run.
    """
    log = logger.bind(max_per_category=max_per_category)
    log.info("ingestion_started", categories=list(HELIOPHYSICS_ARXIV_CATEGORIES))

    # Search categories sequentially to avoid triggering rate limits
    category_results = []
    for category in HELIOPHYSICS_ARXIV_CATEGORIES:
        results = await _search_arxiv_category(category, max_per_category)
        category_results.append(results)
        await asyncio.sleep(1)  # pause between category searches

    # Flatten and deduplicate as same paper can appear in multiple categories
    all_ids = list(
        dict.fromkeys(
            arxiv_id for category_ids in category_results for arxiv_id in category_ids
        )
    )

    log.info("ingestion_ids_collected", total_unique=len(all_ids))

    result = IngestionResult(
        total_found=len(all_ids),
        already_stored=0,
        newly_ingested=0,
        rejected=0,
        failed=0,
        arxiv_ids=[],
    )

    # Process each paper
    # check Postgres first to avoid redundant fetches
    for arxiv_id in all_ids:
        try:
            # Skip if already in database
            existing = await get_paper(arxiv_id)
            if existing:
                result.already_stored += 1
                log.info(
                    "ingestion_skipped", arxiv_id=arxiv_id, reason="already_stored"
                )
                continue

            # Fetch from arXiv
            from app.services.fetcher import fetch_by_arxiv

            paper = await fetch_by_arxiv(arxiv_id)

            if isinstance(paper, DomainValidationError):
                result.rejected += 1
                log.info(
                    "ingestion_rejected",
                    arxiv_id=arxiv_id,
                    reason=paper.reason,
                )
                continue

            # Save to Postgres
            await save_paper(paper)
            result.newly_ingested += 1
            result.arxiv_ids.append(arxiv_id)
            log.info("ingestion_saved", arxiv_id=arxiv_id, title=paper.title)

            # Respect arXiv rate limit: max 4 requests per second
            await asyncio.sleep(0.25)

        except Exception as e:
            result.failed += 1
            log.error("ingestion_error", arxiv_id=arxiv_id, error=str(e))

    log.info(
        "ingestion_complete",
        total_found=result.total_found,
        already_stored=result.already_stored,
        newly_ingested=result.newly_ingested,
        rejected=result.rejected,
        failed=result.failed,
    )

    return result


async def _process_arxiv_ids(
    all_ids: list[str],
    log: structlog.BoundLogger,
) -> IngestionResult:
    """Fetch, validate and save a list of arXiv IDs.

    Shared processing logic used by both ingest_latest_heliophysics
    and ingest_date_range. Checks Postgres before fetching, validates
    heliophysics relevance, and saves passing papers.

    Args:
        all_ids (list[str]): Deduplicated list of arXiv IDs to process.
        log (structlog.BoundLogger): Logger with context already bound.

    Returns:
        IngestionResult: Summary of what was ingested, skipped,
            rejected, and failed.
    """
    result = IngestionResult(
        total_found=len(all_ids),
        already_stored=0,
        newly_ingested=0,
        rejected=0,
        failed=0,
        arxiv_ids=[],
    )

    for arxiv_id in all_ids:
        try:
            existing = await get_paper(arxiv_id)
            if existing:
                result.already_stored += 1
                log.info(
                    "ingestion_skipped", arxiv_id=arxiv_id, reason="already_stored"
                )
                continue

            from app.services.fetcher import fetch_by_arxiv

            paper = await fetch_by_arxiv(arxiv_id)

            if isinstance(paper, DomainValidationError):
                result.rejected += 1
                log.info(
                    "ingestion_rejected",
                    arxiv_id=arxiv_id,
                    reason=paper.reason,
                )
                continue

            await save_paper(paper)
            result.newly_ingested += 1
            result.arxiv_ids.append(arxiv_id)
            log.info("ingestion_saved", arxiv_id=arxiv_id, title=paper.title)

            await asyncio.sleep(0.25)

        except Exception as e:
            result.failed += 1
            log.error("ingestion_error", arxiv_id=arxiv_id, error=str(e))

    return result


async def ingest_date_range(
    start_date: str,
    end_date: str,
    max_per_category: int = 100,
) -> IngestionResult:
    """Ingest heliophysics papers from arXiv within a date range.

    Searches all heliophysics categories for papers submitted between
    the given dates. Useful for backfilling historical data.

    Args:
        start_date (str): Start date in YYYYMMDD format. e.g. '20240101'
        end_date (str): End date in YYYYMMDD format. e.g. '20240331'
        max_per_category (int): Maximum papers per category. Defaults
            to 100.

    Returns:
        IngestionResult: Summary of what was ingested, skipped,
            rejected, and failed.
    """
    log = logger.bind(start_date=start_date, end_date=end_date)
    log.info(
        "date_range_ingestion_started", categories=list(HELIOPHYSICS_ARXIV_CATEGORIES)
    )

    category_results = []
    for category in HELIOPHYSICS_ARXIV_CATEGORIES:
        results = await _search_arxiv_date_range(
            category, start_date, end_date, max_per_category
        )
        category_results.append(results)
        await asyncio.sleep(1)

    all_ids = list(
        dict.fromkeys(
            arxiv_id for category_ids in category_results for arxiv_id in category_ids
        )
    )

    log.info("date_range_ids_collected", total_unique=len(all_ids))
    result = await _process_arxiv_ids(all_ids, log)

    log.info(
        "date_range_ingestion_complete",
        total_found=result.total_found,
        newly_ingested=result.newly_ingested,
        rejected=result.rejected,
        failed=result.failed,
    )
    return result


async def ingest_by_ids(arxiv_ids: list[str]) -> IngestionResult:
    """Fetch and store a specific list of arXiv papers.

    Used when you have a known list of arXiv IDs to ingest rather
    than searching by category. Skips papers already in Postgres.

    Args:
        arxiv_ids (list[str]): List of arXiv IDs to ingest.
            e.g. ['2509.19847', '2301.04380']

    Returns:
        IngestionResult: Summary of what was ingested, skipped,
            rejected, and failed.
    """
    log = logger.bind(total_ids=len(arxiv_ids))
    log.info("ingestion_by_ids_started")

    result = IngestionResult(
        total_found=len(arxiv_ids),
        already_stored=0,
        newly_ingested=0,
        rejected=0,
        failed=0,
        arxiv_ids=[],
    )

    for arxiv_id in arxiv_ids:
        try:
            existing = await get_paper(arxiv_id)
            if existing:
                result.already_stored += 1
                continue

            from app.services.fetcher import fetch_by_arxiv

            paper = await fetch_by_arxiv(arxiv_id)

            if isinstance(paper, DomainValidationError):
                result.rejected += 1
                continue

            await save_paper(paper)
            result.newly_ingested += 1
            result.arxiv_ids.append(arxiv_id)
            log.info("ingestion_saved", arxiv_id=arxiv_id, title=paper.title)

            await asyncio.sleep(0.25)

        except Exception as e:
            result.failed += 1
            log.error("ingestion_error", arxiv_id=arxiv_id, error=str(e))

    log.info(
        "ingestion_complete",
        newly_ingested=result.newly_ingested,
        rejected=result.rejected,
        failed=result.failed,
    )

    return result
