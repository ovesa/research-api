import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import settings
from app.models.paper import (
    Author,
    DomainValidationError,
    HELIOPHYSICS_ARXIV_CATEGORIES,
    HELIOPHYSICS_JOURNALS,
    HELIOPHYSICS_KEYWORDS,
    IdentifierType,
    PaperMetadata,
)


def _is_heliophysics_by_keywords(title: str, abstract: Optional[str]) -> bool:
    """Check if a paper is heliophysics-related using keyword matching.

    Used as a fallback for DOI lookups where the journal is not on the
    heliophysics whitelist. Broad journals like Nature or ApJ
    publish heliophysics papers so journal matching alone is not
    sufficient.

    Args:
        title (str): The paper title.
        abstract (str | None): The paper abstract. May be None if the
            external API did not return one.

    Returns:
        bool: True if any heliophysics keyword is found in the title
            or abstract, False otherwise.
    """
    text = f"{title} {abstract or ''}".lower()
    return any(keyword in text for keyword in HELIOPHYSICS_KEYWORDS)


def _is_heliophysics_by_journal(journal: Optional[str]) -> bool:
    """Check if a paper is heliophysics-related by its journal name.

    Args:
        journal (str | None): The journal name returned by the external API.

    Returns:
        bool: True if the journal is on the heliophysics whitelist,
            False otherwise or if journal is None.
    """
    if not journal:
        return False
    return journal.lower().strip() in HELIOPHYSICS_JOURNALS


async def _fetch_crossref(client: httpx.AsyncClient, doi: str) -> dict:
    """Fetch paper metadata from the CrossRef API by DOI.

    CrossRef is the primary source for DOI-based lookups. It returns
    publisher metadata including title, authors, journal, and date.
    Abstracts are frequently missing from CrossRef responses.

    Args:
        client (httpx.AsyncClient): The shared HTTP client for this request.
        doi (str): The DOI to look up. e.g. 10.1038/nature12373

    Returns:
        dict: The raw CrossRef message payload, or an empty dict if the
            request fails or the DOI is not found.
    """
    try:
        url = f"https://api.crossref.org/works/{doi}"
        response = await client.get(url)
        if response.status_code == 200:
            return response.json().get("message", {})
        return {}
    except Exception:
        return {}


async def _fetch_arxiv(client: httpx.AsyncClient, arxiv_id: str) -> dict:
    """Fetch paper metadata from the arXiv API by arXiv ID.
    see https://info.arxiv.org/help/api/index.html.

    arXiv is the primary source for preprint lookups. It returns
    title, authors, abstract, and subject categories. The categories
    are used for heliophysics domain validation.

    Args:
        client (httpx.AsyncClient): The shared HTTP client for this request.
        arxiv_id (str): The arXiv ID to look up. e.g. 2103.08049

    Returns:
        dict: Parsed arXiv entry as a dict, or an empty dict if the
            request fails or the ID is not found.
    """
    try:
        clean_id = arxiv_id.replace("arxiv:", "").strip()
        url = f"https://export.arxiv.org/api/query?id_list={clean_id}"
        response = await client.get(url)
        if response.status_code != 200:
            return {}

        # arXiv returns XML
        text = response.text
        if "<entry>" not in text:
            return {}

        def extract(tag: str) -> str:
            start = text.find(f"<{tag}>")
            end = text.find(f"</{tag}>")
            if start == -1 or end == -1:
                return ""
            return text[start + len(tag) + 2 : end].strip()

        # Extract all authors
        authors = []
        remaining = text
        while "<author>" in remaining:
            start = remaining.find("<author>")
            end = remaining.find("</author>")
            author_block = remaining[start:end]
            name = author_block.split("<name>")[1].split("</name>")[0].strip()
            authors.append(name)
            remaining = remaining[end + 9 :]

        # Extract categories
        categories = []
        remaining = text
        while 'term="' in remaining:
            start = remaining.find('term="') + 6
            end = remaining.find('"', start)
            categories.append(remaining[start:end])
            remaining = remaining[end + 1 :]

        return {
            "title": extract("title"),
            "abstract": extract("summary"),
            "authors": authors,
            "categories": categories,
            "published": extract("published"),
        }
    except Exception:
        return {}


async def _fetch_semantic_scholar(
    client: httpx.AsyncClient, doi: Optional[str], arxiv_id: Optional[str]
) -> dict:
    """Fetch citation count from Semantic Scholar.

    Semantic Scholar is used exclusively for citation counts which are
    not available from CrossRef or arXiv directly. It accepts both DOIs
    and arXiv IDs.

    Args:
        client (httpx.AsyncClient): The shared HTTP client for this request.
        doi (str | None): DOI to look up. Preferred over arXiv ID.
        arxiv_id (str | None): arXiv ID to look up if DOI is not available.

    Returns:
        dict: Contains 'citation_count' key if found, empty dict otherwise.
    """
    try:
        if doi:
            url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=citationCount"
        elif arxiv_id:
            clean_id = arxiv_id.replace("arxiv:", "").strip()
            url = f"https://api.semanticscholar.org/graph/v1/paper/ARXIV:{clean_id}?fields=citationCount"
        else:
            return {}

        response = await client.get(url)
        if response.status_code == 200:
            data = response.json()
            return {"citation_count": data.get("citationCount")}
        return {}
    except Exception:
        return {}


def _normalize_crossref(
    doi: str, data: dict, citation_count: Optional[int]
) -> PaperMetadata:
    """Normalize a raw CrossRef response into a PaperMetadata object.

    CrossRef returns nested, inconsistent structures. This function
    extracts and normalizes the fields we care about into the clean
    PaperMetadata schema.

    Args:
        doi (str): The DOI that was looked up.
        data (dict): The raw CrossRef message payload.
        citation_count (int | None): Citation count from Semantic Scholar.

    Returns:
        PaperMetadata: Normalized paper metadata.
    """
    # Extract authors
    raw_authors = data.get("author", [])
    authors = []
    for a in raw_authors:
        given = a.get("given", "")
        family = a.get("family", "")
        name = f"{given} {family}".strip()
        affiliation_list = a.get("affiliation", [])
        affiliation = affiliation_list[0].get("name") if affiliation_list else None
        authors.append(Author(name=name, affiliation=affiliation))

    # Extract journal name
    container = data.get("container-title", [])
    journal = container[0] if container else None

    # Extract published date
    date_parts = data.get("published", {}).get("date-parts", [[]])
    if date_parts and date_parts[0]:
        date_str = "-".join(str(p) for p in date_parts[0])
    else:
        date_str = None

    # Extract title
    titles = data.get("title", [])
    title = titles[0] if titles else "Unknown Title"

    # Extract abstract
    abstract = data.get("abstract")

    return PaperMetadata(
        identifier=doi,
        identifier_type=IdentifierType.doi,
        title=title,
        authors=authors,
        abstract=abstract,
        published_date=date_str,
        journal=journal,
        doi=doi,
        arxiv_id=None,
        arxiv_categories=[],
        citation_count=citation_count,
        source="crossref",
        fetched_at=datetime.now(timezone.utc),
    )

def _normalize_arxiv(
    arxiv_id: str, data: dict, citation_count: Optional[int]
) -> PaperMetadata:
    """Normalize a raw arXiv response into a PaperMetadata object.

    Args:
        arxiv_id (str): The arXiv ID that was looked up.
        data (dict): The parsed arXiv entry dict.
        citation_count (int | None): Citation count from Semantic Scholar.

    Returns:
        PaperMetadata: Normalized paper metadata.
    """
    authors = [Author(name=name) for name in data.get("authors", [])]

    published = data.get("published", "")
    published_date = published[:10] if published else None

    return PaperMetadata(
        identifier=arxiv_id,
        identifier_type=IdentifierType.arxiv,
        title=data.get("title", "Unknown Title"),
        authors=authors,
        abstract=data.get("abstract"),
        published_date=published_date,
        journal=None,
        doi=None,
        arxiv_id=arxiv_id.replace("arxiv:", "").strip(),
        arxiv_categories=data.get("categories", []),
        citation_count=citation_count,
        source="arxiv",
        fetched_at=datetime.now(timezone.utc),
    )


async def fetch_by_doi(doi: str) -> PaperMetadata | DomainValidationError:
    """Fetch and validate a heliophysics paper by DOI.

    Hits CrossRef and Semantic Scholar concurrently using asyncio.gather.
    Validates the result against heliophysics journal and keyword lists
    before returning. Rejects papers that do not match.

    Args:
        doi (str): The DOI to look up. e.g. 10.1038/nature12373

    Returns:
        PaperMetadata: Normalized metadata if the paper is heliophysics-related.
        DomainValidationError: Rejection details if the paper does not match.
    """
    async with httpx.AsyncClient(timeout=settings.external_api_timeout) as client:
        # Hit CrossRef and Semantic Scholar at the same time
        crossref_data, semantic_data = await asyncio.gather(
            _fetch_crossref(client, doi),
            _fetch_semantic_scholar(client, doi, None),
        )

    if not crossref_data:
        return DomainValidationError(
            identifier=doi,
            reason="DOI not found in CrossRef.",
        )

    titles = crossref_data.get("title", [])
    title = titles[0] if titles else None

    container = crossref_data.get("container-title", [])
    journal = container[0] if container else None
    abstract = crossref_data.get("abstract")
    citation_count = semantic_data.get("citation_count")

    # Validate heliophysics relevance
    if not _is_heliophysics_by_journal(journal):
        if not _is_heliophysics_by_keywords(title or "", abstract):
            return DomainValidationError(
                identifier=doi,
                reason=(
                    f"Paper does not appear to be heliophysics-related. "
                    f"Journal '{journal}' is not on the heliophysics whitelist "
                    f"and no heliophysics keywords were found in the title or abstract."
                ),
                title=title,
            )

    return _normalize_crossref(doi, crossref_data, citation_count)


async def fetch_by_arxiv(arxiv_id: str) -> PaperMetadata | DomainValidationError:
    """Fetch and validate a heliophysics paper by arXiv ID.

    Hits arXiv and Semantic Scholar concurrently using asyncio.gather.
    Validates the result against heliophysics arXiv category list
    before returning. Rejects papers outside heliophysics categories.

    Args:
        arxiv_id (str): The arXiv ID to look up. e.g. 2103.08049

    Returns:
        PaperMetadata: Normalized metadata if the paper is heliophysics-related.
        DomainValidationError: Rejection details if the paper does not match.
    """
    clean_id = arxiv_id.replace("arxiv:", "").strip()

    async with httpx.AsyncClient(timeout=settings.external_api_timeout) as client:
        # Hit arXiv and Semantic Scholar at the same time
        arxiv_data, semantic_data = await asyncio.gather(
            _fetch_arxiv(client, clean_id),
            _fetch_semantic_scholar(client, None, clean_id),
        )

    if not arxiv_data:
        return DomainValidationError(
            identifier=arxiv_id,
            reason="arXiv ID not found.",
        )

    categories = arxiv_data.get("categories", [])
    citation_count = semantic_data.get("citation_count")

    # Validate heliophysics relevance by arXiv category
    matching_categories = [c for c in categories if c in HELIOPHYSICS_ARXIV_CATEGORIES]

    if not matching_categories:
        return DomainValidationError(
            identifier=arxiv_id,
            reason=(
                f"Paper categories {categories} do not include any "
                f"heliophysics categories {list(HELIOPHYSICS_ARXIV_CATEGORIES)}."
            ),
            title=arxiv_data.get("title"),
        )

    return _normalize_arxiv(clean_id, arxiv_data, citation_count)
