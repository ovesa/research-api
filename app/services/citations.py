import structlog

from app.services.database import get_pool

logger = structlog.get_logger(__name__)


async def save_citation_edges(
    citing_identifier: str, cited_identifiers: list[str], source: str = "ads"
) -> int:
    """Save directed citation edges from one paper to the papers it cites.
    Uses INSERT ... ON CONFLICT DO NOTHING so re-running on the same paper
    is safe because duplicate edges are silently skipped.

    Args:
        citing_identifier: The paper that contains the references.

        cited_identifiers: List of bibcodes this paper cites.

        source: Where the edge data came from. Defaults to 'ads'.

    Returns:
        Number of new edges inserted. Duplicates are not counted.
    """
    if not cited_identifiers:
        return 0

    pool = await get_pool()
    inserted = 0

    async with pool.acquire() as conn:
        for cited in cited_identifiers:
            # Skip self-references
            if cited == citing_identifier:
                continue
            result = await conn.execute(
                """
                INSERT INTO related_papers (citing_identifier, cited_identifier, source)
                VALUES ($1, $2, $3)
                ON CONFLICT (citing_identifier, cited_identifier) DO NOTHING
                """,
                citing_identifier,
                cited,
                source,
            )
            # asyncpg returns "INSERT 0 N" to parse N to count new rows
            if result.split()[-1] == "1":
                inserted += 1

    logger.info(
        "citation_edges_saved",
        citing=citing_identifier,
        total_cited=len(cited_identifiers),
        newly_inserted=inserted,
    )
    return inserted


async def get_references(identifier: str, limit: int = 20) -> list[dict]:
    """Get papers that this paper cites (its reference list). Only returns
    papers that exist in your papers table. Cited papers not yet in your
    database are silently excluded. This is intentional: you can only return
    metadata for papers you have.

    Args:
        identifier: The paper whose references you want.

        limit: Maximum number of references to return. Defaults to 20.

    Returns:
        List of paper dicts for papers this paper cites, ordered by
            published date descending.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                p.identifier,
                p.title,
                p.authors,
                p.published_date,
                p.journal,
                p.url,
                p.citation_count
            FROM related_papers r
            JOIN papers p ON p.identifier = r.cited_identifier
            WHERE r.citing_identifier = $1
            ORDER BY p.published_date DESC
            LIMIT $2
            """,
            identifier,
            limit,
        )
    return [dict(row) for row in rows]


async def get_citations(identifier: str, limit: int = 20) -> list[dict]:
    """Get papers in your database that cite this paper. Only returns papers
    that are in your papers table. Papers that cite this one but haven't been
    ingested yet won't appear here.

    Args:
        identifier: The paper you want to find citations for.

        limit: Maximum number of citing papers to return. Defaults to 20.

    Returns:
        List of paper dicts for papers that cite this paper, ordered by
            published date descending.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                p.identifier,
                p.title,
                p.authors,
                p.published_date,
                p.journal,
                p.url,
                p.citation_count
            FROM related_papers r
            JOIN papers p ON p.identifier = r.citing_identifier
            WHERE r.cited_identifier = $1
            ORDER BY p.published_date DESC
            LIMIT $2
            """,
            identifier,
            limit,
        )
    return [dict(row) for row in rows]

async def get_most_cited_in_collection(limit: int = 20) -> list[dict]:
    """Find the most-cited papers within your collection. Returns all 
    highly-cited bibcodes regardless of whether the cited paper has been 
    ingested. Papers that are in your database get full metadata. Papers 
    not yet ingested show just the identifier and citation count so you 
    can see what to ingest next.

    Args:
        limit: Number of top papers to return. Defaults to 20.

    Returns:
        List of dicts with citation count and whatever metadata is
        available. Papers not in your database have null title/journal.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                r.cited_identifier AS identifier,
                p.title,
                p.authors,
                p.published_date,
                p.journal,
                p.url,
                COUNT(r.citing_identifier) AS internal_citation_count,
                CASE WHEN p.identifier IS NOT NULL THEN true ELSE false END AS in_collection
            FROM related_papers r
            LEFT JOIN papers p ON p.identifier = r.cited_identifier
            GROUP BY
                r.cited_identifier, p.identifier, p.title, p.authors::text,
                p.published_date, p.journal, p.url
            ORDER BY internal_citation_count DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(row) for row in rows]


async def fetch_and_save_references(bibcode: str) -> int:
    """Fetch reference list from ADS for a bibcode and save citation edges.
    Makes a lightweight ADS API call requesting only the reference field,
    then saves directed edges to the related_papers table. Safe to re-run as
    duplicate edges are silently skipped.

    Args:
        bibcode: ADS bibcode of the paper to fetch references for.

    Returns:
        Number of new citation edges saved. Returns 0 if ADS token is missing,
            the paper has no references, or the request fails.
    """
    import httpx
    import urllib.parse
    from app.config import settings

    if not settings.ads_api_token:
        logger.warning("ads_token_missing_for_references", bibcode=bibcode)
        return 0

    encoded = urllib.parse.quote(bibcode, safe="")
    url = (
        f"https://api.adsabs.harvard.edu/v1/search/query"
        f"?q=bibcode:{encoded}"
        f"&fl=bibcode,reference"
    )

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {settings.ads_api_token}",
                    "User-Agent": "research-api/0.1 (heliophysics citation graph)",
                },
            )
        if response.status_code != 200:
            logger.warning(
                "ads_references_fetch_failed",
                bibcode=bibcode,
                status_code=response.status_code,
            )
            return 0

        docs = response.json().get("response", {}).get("docs", [])
        if not docs:
            return 0

        references = docs[0].get("reference", [])
        return await save_citation_edges(bibcode, references, source="ads")

    except Exception as e:
        logger.warning("fetch_references_failed", bibcode=bibcode, error=str(e))
        return 0
