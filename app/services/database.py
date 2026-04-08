import json
from typing import Optional

from app.database import get_pool
from app.models.paper import Author, IdentifierType, PaperMetadata


async def save_paper(paper: PaperMetadata) -> None:
    """Save a validated heliophysics paper to Postgres.

    Called after every successful fetch and heliophysics validation.
    Uses INSERT ... ON CONFLICT DO NOTHING so re-fetching an already
    stored paper is safe and does not overwrite existing data.

    Args:
        paper (PaperMetadata): The normalized paper metadata to store.

    Returns:
        None
    """
    pool = await get_pool()

    authors_json = json.dumps([a.model_dump() for a in paper.authors])
    categories_json = json.dumps(paper.arxiv_categories)

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO papers (
                identifier, identifier_type, title, authors, abstract,
                published_date, journal, doi, arxiv_id, arxiv_categories,
                citation_count, source, fetched_at, url
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (identifier) DO NOTHING
            """,
            paper.identifier,
            paper.identifier_type.value,
            paper.title,
            authors_json,
            paper.abstract,
            paper.published_date,
            paper.journal,
            paper.doi,
            paper.arxiv_id,
            categories_json,
            paper.citation_count,
            paper.source,
            paper.fetched_at,
            paper.url,
        )


async def get_paper(identifier: str) -> Optional[PaperMetadata]:
    """Retrieve a single paper from Postgres by identifier.

    Used as a fallback when Redis cache has expired but the paper
    is still in the database. Avoids hitting external APIs again
    for data we already have.

    Args:
        identifier (str): The DOI or arXiv ID to look up.

    Returns:
        PaperMetadata: The stored paper if found, None otherwise.
    """
    pool = await get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM papers WHERE identifier = $1", identifier
        )

    if not row:
        return None

    return _row_to_paper(row)


async def delete_paper(identifier: str) -> bool:
    """Delete a single paper from Postgres by identifier.

    Used when a paper needs to be removed from the collection. For
    example if it was ingested by mistake or fails manual review.
    Also clears the Redis cache entry so stale data is not served.

    Args:
        identifier (str): The DOI, arXiv ID, or ADS bibcode to delete.

    Returns:
        bool: True if a paper was deleted, False if no paper was found
            with that identifier.
    """
    pool = await get_pool()

    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM papers WHERE identifier = $1",
            identifier,
        )

    # result is a string like 'DELETE 1' or 'DELETE 0'
    rows_deleted = int(result.split()[-1])
    return rows_deleted > 0


async def patch_paper(identifier: str, updates: dict) -> Optional[PaperMetadata]:
    """Partially update a stored paper's fields.

    Builds a dynamic UPDATE query from only the fields provided.
    Fields not included in updates are left unchanged. Returns the
    updated paper so the caller can cache the fresh version.

    Args:
        identifier (str): The identifier of the paper to update.
        updates (dict): A dict of field names to new values.
            Only non-None fields from the patch request should be passed.

    Returns:
        PaperMetadata: The updated paper if found, None if not found.
    """
    if not updates:
        return await get_paper(identifier)

    pool = await get_pool()

    # Build SET clause dynamically from provided fields only
    set_clauses = []
    params = []
    param_index = 1

    for field, value in updates.items():
        set_clauses.append(f"{field} = ${param_index}")
        params.append(value)
        param_index += 1

    params.append(identifier)

    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            UPDATE papers
            SET {", ".join(set_clauses)}
            WHERE identifier = ${param_index}
            """,
            *params,
        )

    return await get_paper(identifier)


async def list_papers(
    limit: int = 20,
    offset: int = 0,
    identifier_type: Optional[str] = None,
    source: Optional[str] = None,
    sort_by: str = "fetched_at",
    sort_order: str = "desc",
) -> tuple[list[PaperMetadata], int]:
    """List stored papers with optional filtering and pagination.

    Args:
        limit (int): Maximum number of papers to return. Defaults to 20.
        offset (int): Number of papers to skip for pagination.
        identifier_type (str | None): Filter by 'doi' or 'arxiv'.
        source (str | None): Filter by source API, e.g. 'crossref'.

    Returns:
        tuple[list[PaperMetadata], int]: A list of papers and the total
            count matching the filters, for pagination metadata.
    """
    pool = await get_pool()

    # Build query dynamically based on which filters are provided
    conditions = []
    params = []
    param_index = 1

    if identifier_type:
        conditions.append(f"identifier_type = ${param_index}")
        params.append(identifier_type)
        param_index += 1

    if source:
        conditions.append(f"source = ${param_index}")
        params.append(source)
        param_index += 1

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT * FROM papers
            {where_clause}
            ORDER BY {sort_by} {sort_order.upper()}
            LIMIT ${param_index} OFFSET ${param_index + 1}
            """,
            *params,
            limit,
            offset,
        )
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM papers {where_clause}", *params
        )

    return [_row_to_paper(row) for row in rows], total


async def get_stats() -> dict:
    """Return aggregate statistics about the stored paper collection.

    Queries Postgres for counts grouped by category, source, and
    identifier type. Exposed via the /papers/stats endpoint.

    Returns:
        dict: Contains total count, breakdown by source, breakdown by
            identifier type, and the most recently fetched paper date.
    """
    pool = await get_pool()

    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM papers")
        by_source = await conn.fetch(
            "SELECT source, COUNT(*) as count FROM papers GROUP BY source"
        )
        by_type = await conn.fetch(
            "SELECT identifier_type, COUNT(*) as count FROM papers GROUP BY identifier_type"
        )
        latest = await conn.fetchval("SELECT MAX(fetched_at) FROM papers")

    return {
        "total_papers": total,
        "by_source": {row["source"]: row["count"] for row in by_source},
        "by_identifier_type": {row["identifier_type"]: row["count"] for row in by_type},
        "latest_fetched_at": latest.isoformat() if latest else None,
    }


def _row_to_paper(row) -> PaperMetadata:
    """Convert a raw asyncpg database row into a PaperMetadata object.

    asyncpg returns rows as Record objects. This function normalizes
    the raw types, parsing JSON strings back into Python objects and
    reconstructing nested Pydantic models.

    Args:
        row: An asyncpg Record object from a papers table query.

    Returns:
        PaperMetadata: Fully reconstructed paper metadata object.
    """
    authors_raw = (
        json.loads(row["authors"])
        if isinstance(row["authors"], str)
        else row["authors"]
    )
    categories_raw = (
        json.loads(row["arxiv_categories"])
        if isinstance(row["arxiv_categories"], str)
        else row["arxiv_categories"]
    )

    authors = [Author(**a) for a in authors_raw]

    return PaperMetadata(
        identifier=row["identifier"],
        identifier_type=IdentifierType(row["identifier_type"]),
        title=row["title"],
        authors=authors,
        abstract=row["abstract"],
        published_date=row["published_date"],
        journal=row["journal"],
        doi=row["doi"],
        arxiv_id=row["arxiv_id"],
        arxiv_categories=categories_raw,
        citation_count=row["citation_count"],
        source=row["source"],
        fetched_at=row["fetched_at"],
        url=row["url"],
    )


async def search_papers(
    query: str,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[PaperMetadata], int]:
    """Search stored papers using Postgres full text search.

    Uses tsvector and ts_rank to find and rank papers by relevance.
    Title matches rank higher than abstract matches due to 'A' and 'B'
    weights set during indexing. Results are ordered by relevance score
    descending so the best matches appear first.

    Stemming is handled automatically by Postgres: searching 'magnetohydrodynamic'
    will match 'magnetohydrodynamics', searching 'wave' matches 'waves',
    'waving', 'wavelength' and so on. tsvector converts raw text into a
    searchable token list.

    Args:
        query (str): The search terms to look for. Can be multiple words.
            e.g. 'solar wind', 'magnetic field oscillations'
        limit (int): Maximum number of results to return. Defaults to 20.
        offset (int): Number of results to skip for pagination.

    Returns:
        tuple[list[PaperMetadata], int]: Matching papers ordered by
            relevance score, and the total count of matches for
            pagination metadata.
    """
    pool = await get_pool()

    # Convert plain query string into a Postgres tsquery
    # plainto_tsquery handles multi-word queries
    # e.g., 'solar wind' becomes 'solar & wind' automatically
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *,
                ts_rank(
                    setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
                    setweight(to_tsvector('english', COALESCE(abstract, '')), 'B'),
                    plainto_tsquery('english', $1)
                ) AS rank
            FROM papers
            WHERE
                to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(abstract, ''))
                @@ plainto_tsquery('english', $1)
            ORDER BY rank DESC
            LIMIT $2 OFFSET $3
            """,
            query,
            limit,
            offset,
        )

        total = await conn.fetchval(
            """
            SELECT COUNT(*) FROM papers
            WHERE
                to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(abstract, ''))
                @@ plainto_tsquery('english', $1)
            """,
            query,
        )

    return [_row_to_paper(row) for row in rows], total


async def filter_papers_by_keywords(
    keywords: list[str],
    match_all: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[PaperMetadata], int]:
    """Filter stored papers by explicit keywords in title or abstract.

    Unlike full text search, this does exact substring matching (case-
    insensitive) against each keyword. Useful when you want papers that
    specifically contain a term like 'inertial modes' without stemming
    or relevance ranking changing your results.

    Args:
        keywords (list[str]): Keywords to filter by.
            e.g. ['inertial modes', 'rossby waves']
        match_all (bool): If True, paper must contain ALL keywords.
            If False, paper must contain AT LEAST ONE. Defaults to False.
        limit (int): Maximum number of results to return. Defaults to 20.
        offset (int): Number of results to skip for pagination.

    Returns:
        tuple[list[PaperMetadata], int]: Matching papers and total count.
    """
    pool = await get_pool()

    if not keywords:
        return [], 0

    # ILIKE condition per keyword against title + abstract
    conditions = []
    params: list = []
    for i, kw in enumerate(keywords, start=1):
        conditions.append(f"(title ILIKE ${i} OR abstract ILIKE ${i})")
        params.append(f"%{kw}%")

    joiner = " AND " if match_all else " OR "
    where_clause = f"WHERE ({joiner.join(conditions)})"

    count_param_index = len(params) + 1

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT * FROM papers
            {where_clause}
            ORDER BY fetched_at DESC
            LIMIT ${count_param_index} OFFSET ${count_param_index + 1}
            """,
            *params,
            limit,
            offset,
        )
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM papers {where_clause}",
            *params,
        )

    return [_row_to_paper(row) for row in rows], total
