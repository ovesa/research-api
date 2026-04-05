import asyncio
from dataclasses import dataclass

import structlog

from app.models.paper import HELIOPHYSICS_ARXIV_CATEGORIES, DomainValidationError
from app.services.database import get_paper, save_paper
from app.services.fetcher import _fetch_arxiv, _make_client

logger = structlog.get_logger(__name__)


@dataclass
class IngestionResult:
    """Summary of a bulk ingestion paper run from source.

    Returned after an ingestion job completes so the caller knows
    exactly what happened (i.e., how many papers were found, how many
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
            e.g. ['2509.19847', '2509.18234', ...]
    """
    async with _make_client() as client:
        url = (
            f"https://export.arxiv.org/api/query"
            f"?search_query=cat:{category}"
            f"&sortBy=submittedDate"
            f"&sortOrder=descending"
            f"&max_results={max_results}"
        )
        response = await client.get(url)

        if response.status_code != 200:
            logger.warning(
                "arxiv_search_failed",
                category=category,
                status_code=response.status_code,
            )
            return []

        text = response.text
        ids = []

        # Extract arXiv IDs from <id> tags in the Atom feed
        # Format: http://arxiv.org/abs/2509.19847v1
        remaining = text
        while "<id>http://arxiv.org/abs/" in remaining:
            start = remaining.find("<id>http://arxiv.org/abs/") + 25
            end = remaining.find("</id>", start)
            if end == -1:
                break
            raw_id = remaining[start:end].strip()
            # Strip version suffix e.g. v1, v2
            clean_id = raw_id.split("v")[0]
            ids.append(clean_id)
            remaining = remaining[end + 5 :]

        logger.info(
            "arxiv_search_complete",
            category=category,
            papers_found=len(ids),
        )
        return ids


async def ingest_latest_heliophysics(
    max_per_category: int = 25,
) -> IngestionResult:
    """Fetch and store the latest heliophysics papers from arXiv.

    Searches all heliophysics arXiv categories concurrently, deduplicates
    the results, skips papers already in Postgres, and fetches new ones
    concurrently. Rate limiting is applied between fetches to respect
    arXiv's guidelines of no more than 4 requests per second.

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

    # Search all heliophysics categories concurrently
    category_results = await asyncio.gather(
        *[
            _search_arxiv_category(category, max_per_category)
            for category in HELIOPHYSICS_ARXIV_CATEGORIES
        ]
    )

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
