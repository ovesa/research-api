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
import structlog

logger = structlog.get_logger(__name__)


def _is_heliophysics_by_keywords(title: str, abstract: Optional[str]) -> bool:
    """Check if a paper is heliophysics-related using keyword matching.

    Used as a fallback for DOI lookups where the journal is not on the
    heliophysics whitelist. Broad journals like Nature or ApJ publish
    heliophysics papers so journal matching alone is not sufficient.

    Args:
        title (str): The paper title.
        abstract (str | None): The paper abstract. May be None if the
            external API did not return one.

    Returns:
        bool: True if any heliophysics keyword is found in the title
            or abstract, False otherwise.
    """
    text = f"{title} {abstract or ''}".lower()
    return any(keyword.lower() in text for keyword in HELIOPHYSICS_KEYWORDS)


target_phrases = {
    "inertial mode",
    "inertial wave",
    "rossby mode",
    "rossby wave",
    "inertial oscillation",
}

solar_indicators = {
    "solar",
    "the sun",
    "sun:",
    "on the sun",
    "in the sun",
    " sun ",
    "sunspot",
    "helioseismology",
    "solar interior",
    "solar convection zone",
    "solar wind",
    "solar corona",
    "solar cycle",
    "tachocline",
}

non_solar_indicators = {
    "solar-type star",
    "solar-type stars",
    "white dwarf",
    "white dwarfs",
    "accreting",
    "pre-main-sequence",
    "pms star",
    "cataclysmic variable",
    "dwarf novae",
    "neutron star",
    "exoplanet",
    "kepler star",
    "kic ",                    # Kepler Input Catalog IDs
    "Kepler",
    "TESS",
    "Gaia",

    # Earth/climate/atmosphere
    "monsoon",
    "sea ice",
    "precipitation",
    "heatwave",
    "heat wave",
    "tibetan plateau",
    "ocean",
    "climate",
    "el niño",
    "el nino",
    "enso",
    "ozone",
    "blocking",                # atmospheric blocking
    "earth system model",
    "sea surface temperature",
    "boreal",
    "paleoclimate",
    "milankovitch",
    "pleistocene",
    "holocene",
    "quaternary",
    "volcanic eruption",
    "greenness",
    "eurasia",
    "barents",
    "indian ocean",
    "pacific",
    "atlantic",
}

EXCLUDED_JOURNALS = {
    "journal of climate",
    "journal of geophysical research",
    "atmospheric research",
    "climate dynamics",
    "geophysical research letters",   # mostly earth science
    "atmospheric chemistry & physics",
    "atmosphere",
    "environmental research letters",
    "ocean-land-atmosphere research",
    "palaeogeography palaeoclimatology palaeoecology",
    "mausam",
    "EPSC-DPS Joint Meeting 2025",
}

def _is_excluded_journal(journal: Optional[str]) -> bool:
    """Return True if this journal is outside heliophysics scope."""
    if not journal:
        return False
    return journal.lower().strip() in EXCLUDED_JOURNALS

def _is_non_solar(title: str, abstract: Optional[str]) -> bool:
    """Return True if paper is clearly about non-solar objects."""
    text = f"{title} {abstract or ''}".lower()
    return any(indicator in text for indicator in non_solar_indicators)


def _has_target_phrase(title: str, abstract: Optional[str]) -> bool:
    text = f"{title} {abstract or ''}".lower()
    return any(phrase in text for phrase in target_phrases)


def _has_solar_indicator(title: str, abstract: Optional[str]) -> bool:
    text = f"{title} {abstract or ''}".lower()
    return any(indicator in text for indicator in solar_indicators)


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


def _is_stellar_astrophysics(title: str, abstract: Optional[str]) -> bool:
    """Check if a paper is stellar astrophysics rather than heliophysics.

    Used as a rejection filter after category and keyword validation.
    Some papers pass heliophysics keyword checks because they mention
    'plasma' or 'magnetic field' but are fundamentally about stellar
    evolution, stellar populations, or other non-solar topics.

    Args:
        title (str): The paper title.
        abstract (str | None): The paper abstract.

    Returns:
        bool: True if the paper appears to be stellar astrophysics
            rather than heliophysics, False otherwise.
    """
    stellar_title_keywords = {
        "red giant",
        "red giants",
        "white dwarf",
        "white dwarfs",
        "neutron star",
        "neutron stars",
        "black hole",
        "black holes",
        "exoplanet",
        "exoplanets",
        "galaxy",
        "galaxies",
        "globular cluster",
        "open cluster",
        "stellar population",
        "stellar evolution",
        "stellar mass",
        "star formation",
        "protostar",
        "supernova",
        "supernovae",
        "pulsar",
        "cepheid",
        "binary star",
        "binary stars",
        "massive star",
        "massive stars",
        "multiplicity",
        "asteroseismic",
        "asteroseismology",
        "kepler red",
        "milky way",
        "spectroscopic binary",
        "eclipsing binary",
        "variable star",
        "variable stars",
        "dwarf galaxy",
        "hot jupiter",
        "transiting",
        "secondary eclipse",
        "transit photometry",
        "astrosphere",
        "astrospheres",
        "plasma sheet",
        "interstellar medium",
        "lyman-alpha",
        "bow shock",
    }

    title_lower = title.lower()
    return any(keyword in title_lower for keyword in stellar_title_keywords)


def _make_client() -> httpx.AsyncClient:
    """Create a configured httpx async client for external API calls.

    Centralizes client configuration so all external requests use the
    same timeout, redirect, and header settings. arXiv requires https
    and follows redirects. A descriptive User-Agent is good practice
    and helps API maintainers identify traffic sources.

    Returns:
        httpx.AsyncClient: Configured client ready for use as a context manager.
    """
    return httpx.AsyncClient(
        timeout=settings.external_api_timeout,
        follow_redirects=True,
        headers={"User-Agent": "research-api/0.1 (heliophysics paper lookup)"},
    )


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

    See https://info.arxiv.org/help/api/index.html for API documentation.
    arXiv is the primary source for preprint lookups. It returns title,
    authors, abstract, and subject categories. The categories are used
    for heliophysics domain validation.

    arXiv returns Atom XML: this function parses it manually to avoid
    pulling in a heavy XML parsing dependency.

    Args:
        client (httpx.AsyncClient): The shared HTTP client for this request.
        arxiv_id (str): The arXiv ID to look up. e.g. 2301.04380

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

        text = response.text

        if "<entry>" not in text:
            return {}

        # Isolate just the entry block so we don't grab feed-level tags
        entry_start = text.find("<entry>")
        entry_end = text.find("</entry>")
        entry_text = text[entry_start:entry_end]

        def extract(tag: str) -> str:
            """Extract text content between an XML tag pair.

            Args:
                tag (str): The XML tag name to extract content from.

            Returns:
                str: The text content between the tags, or empty string
                    if the tag is not found.
            """
            start = entry_text.find(f"<{tag}>")
            end = entry_text.find(f"</{tag}>")
            if start == -1 or end == -1:
                return ""
            return entry_text[start + len(tag) + 2 : end].strip()

        # Extract all author names
        authors = []
        remaining = text
        while "<author>" in remaining:
            start = remaining.find("<author>")
            end = remaining.find("</author>")
            if end == -1:
                break
            author_block = remaining[start:end]
            if "<name>" in author_block and "</name>" in author_block:
                name = author_block.split("<name>")[1].split("</name>")[0].strip()
                authors.append(name)
            remaining = remaining[end + 9 :]

        # Extract arXiv subject categories from term attributes
        # e.g. <category term="astro-ph.SR" scheme="..."/>
        categories = []
        remaining = text
        while 'term="' in remaining:
            start = remaining.find('term="') + 6
            end = remaining.find('"', start)
            if end == -1:
                break
            term = remaining[start:end]
            # Filter out scheme URLs that also contain term=" pattern
            if not term.startswith("http"):
                categories.append(term)
            remaining = remaining[end + 1 :]

        return {
            "title": extract("title"),
            "abstract": extract("summary"),
            "authors": authors,
            "categories": list(
                dict.fromkeys(categories)
            ),  # deduplicate preserving order
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
    and arXiv IDs. DOI is preferred when available.

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


async def _fetch_ads(
    client: httpx.AsyncClient,
    bibcode: str,
) -> dict:
    """Fetch paper metadata from the NASA ADS API by bibcode.

    ADS is the primary database for astronomy and astrophysics literature.
    It indexes papers from arXiv, all major journals, conference proceedings,
    and technical reports.

    Args:
        client (httpx.AsyncClient): The shared HTTP client for this request.
        bibcode (str): The ADS bibcode to look up.
            e.g. '2025ApJ...123..456V'

    Returns:
        dict: Parsed ADS paper metadata, or empty dict if not found.
    """
    try:
        from app.config import settings

        # URL encode the bibcode (it contains special characters)
        import urllib.parse

        encoded = urllib.parse.quote(bibcode, safe="")

        url = (
            f"https://api.adsabs.harvard.edu/v1/search/query"
            f"?q=bibcode:{encoded}"
            f"&fl=bibcode,title,author,abstract,pubdate,pub,doi,identifier,keyword,citation_count"
        )

        response = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {settings.ads_api_token}",
                "User-Agent": "research-api/0.1 (heliophysics paper lookup)",
            },
        )

        if response.status_code != 200:
            return {}

        data = response.json()
        docs = data.get("response", {}).get("docs", [])
        if not docs:
            return {}

        return docs[0]

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
        data (dict): The parsed arXiv entry dict from _fetch_arxiv.
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


def _normalize_ads(
    bibcode: str,
    data: dict,
) -> PaperMetadata:
    """Normalize a raw ADS response into a PaperMetadata object.

    ADS returns cleaner, more complete metadata than CrossRef for
    astronomy papers. Abstracts are almost always present. Author
    lists are complete. Keywords are included when available.

    Args:
        bibcode (str): The ADS bibcode that was looked up.
        data (dict): The raw ADS document from the search response.

    Returns:
        PaperMetadata: Normalized paper metadata.
    """
    # Extract authors
    raw_authors = data.get("author", [])
    authors = [Author(name=name) for name in raw_authors]

    # Extract title — ADS returns a list
    titles = data.get("title", [])
    title = titles[0] if titles else "Unknown Title"

    # Extract DOI if available
    doi = None
    identifiers = data.get("identifier", [])
    for ident in identifiers:
        if ident.startswith("10."):
            doi = ident
            break

    # Also check the doi field directly
    if not doi:
        doi_list = data.get("doi", [])
        doi = doi_list[0] if doi_list else None

    # Extract arXiv ID if available
    arxiv_id = None
    for ident in identifiers:
        if ident.startswith("arXiv:"):
            arxiv_id = ident.replace("arXiv:", "").strip()
            break

    # Extract publication date
    pubdate = data.get("pubdate", "")
    published_date = pubdate[:7] if pubdate else None  # YYYY-MM

    # Construct URL
    ads_url = f"https://ui.adsabs.harvard.edu/abs/{bibcode}"

    return PaperMetadata(
        identifier=bibcode,
        identifier_type=IdentifierType.ads,
        title=title,
        authors=authors,
        abstract=data.get("abstract"),
        published_date=published_date,
        journal=data.get("pub"),
        doi=doi,
        arxiv_id=arxiv_id,
        arxiv_categories=[],
        citation_count=data.get("citation_count"),
        source="ads",
        fetched_at=datetime.now(timezone.utc),
        url=ads_url,
    )


async def fetch_by_doi(doi: str) -> PaperMetadata | DomainValidationError:
    """Fetch and validate a heliophysics paper by DOI.

    Hits CrossRef and Semantic Scholar concurrently using asyncio.gather.
    Validates that the paper contains target phrases (inertial modes/waves,
    rossby modes/waves) and solar indicators before returning.

    Args:
        doi (str): The DOI to look up. e.g. 10.1038/nature12373

    Returns:
        PaperMetadata: Normalized metadata if paper passes validation.
        DomainValidationError: Rejection details if paper does not match.
    """
    import time

    log = logger.bind(identifier=doi, identifier_type="doi")
    log.info("fetch_started")
    start = time.perf_counter()

    async with _make_client() as client:
        crossref_data, semantic_data = await asyncio.gather(
            _fetch_crossref(client, doi),
            _fetch_semantic_scholar(client, doi, None),
        )

    duration_ms = round((time.perf_counter() - start) * 1000, 2)

    if not crossref_data:
        log.warning(
            "fetch_failed", reason="DOI not found in CrossRef", duration_ms=duration_ms
        )
        return DomainValidationError(
            identifier=doi,
            reason="DOI not found in CrossRef.",
        )

    titles = crossref_data.get("title", [])
    title = titles[0] if titles else None

    abstract = crossref_data.get("abstract")
    citation_count = semantic_data.get("citation_count")

    if not _has_target_phrase(title or "", abstract):
        log.warning("target_phrase_missing", duration_ms=duration_ms)
        return DomainValidationError(
            identifier=doi,
            reason="Paper does not contain target phrases (inertial modes/waves, rossby modes/waves).",
            title=title,
        )

    if not _has_solar_indicator(title or "", abstract):
        log.warning("solar_indicator_missing", duration_ms=duration_ms)
        return DomainValidationError(
            identifier=doi,
            reason="Paper does not appear to be solar/heliophysics-related.",
            title=title,
        )

    log.info("fetch_complete", duration_ms=duration_ms, source="crossref")
    return _normalize_crossref(doi, crossref_data, citation_count)


async def fetch_by_arxiv(arxiv_id: str) -> PaperMetadata | DomainValidationError:
    """Fetch and validate a heliophysics paper by arXiv ID.

    Hits arXiv and Semantic Scholar concurrently using asyncio.gather.
    Validates the result against heliophysics arXiv category list
    before returning. Rejects papers outside heliophysics categories.

    Args:
        arxiv_id (str): The arXiv ID to look up. e.g. 2301.04380

    Returns:
        PaperMetadata: Normalized metadata if paper is heliophysics-related.
        DomainValidationError: Rejection details if paper does not match.
    """
    import time

    clean_id = arxiv_id.replace("arxiv:", "").strip()
    log = logger.bind(identifier=clean_id, identifier_type="arxiv")
    log.info("fetch_started")
    start = time.perf_counter()

    async with _make_client() as client:
        arxiv_data, semantic_data = await asyncio.gather(
            _fetch_arxiv(client, clean_id),
            _fetch_semantic_scholar(client, None, clean_id),
        )

    duration_ms = round((time.perf_counter() - start) * 1000, 2)

    if not arxiv_data:
        log.warning(
            "fetch_failed", reason="arXiv ID not found", duration_ms=duration_ms
        )
        return DomainValidationError(
            identifier=arxiv_id,
            reason="arXiv ID not found.",
        )

    categories = arxiv_data.get("categories", [])
    citation_count = semantic_data.get("citation_count")

    primary_category = categories[0] if categories else ""
    matching_categories = [c for c in categories if c in HELIOPHYSICS_ARXIV_CATEGORIES]

    if (
        primary_category not in HELIOPHYSICS_ARXIV_CATEGORIES
        and len(matching_categories) < 2
    ):
        log.warning(
            "heliophysics_validation_failed",
            primary_category=primary_category,
            categories=categories,
            duration_ms=duration_ms,
        )
        return DomainValidationError(
            identifier=arxiv_id,
            reason=(
                f"Primary category '{primary_category}' is not a heliophysics category. "
                f"Paper must have a heliophysics primary category or multiple "
                f"heliophysics categories. Found: {categories}."
            ),
            title=arxiv_data.get("title"),
        )

    log.info("fetch_complete", duration_ms=duration_ms, source="arxiv")
    return _normalize_arxiv(clean_id, arxiv_data, citation_count)


async def fetch_by_ads(bibcode: str) -> PaperMetadata | DomainValidationError:
    """Fetch and validate a heliophysics paper by ADS bibcode.

    Hits the NASA ADS API and validates that the paper contains target
    phrases (inertial modes/waves, rossby modes/waves) and solar indicators
    before returning.

    Args:
        bibcode (str): The ADS bibcode to look up.
            e.g. '2025ApJ...123..456V'

    Returns:
        PaperMetadata: Normalized metadata if paper passes validation.
        DomainValidationError: Rejection details if paper does not match.
    """
    import time

    log = logger.bind(identifier=bibcode, identifier_type="ads")
    log.info("fetch_started")
    start = time.perf_counter()

    async with _make_client() as client:
        ads_data = await _fetch_ads(client, bibcode)

    duration_ms = round((time.perf_counter() - start) * 1000, 2)

    if not ads_data:
        log.warning(
            "fetch_failed", reason="bibcode not found in ADS", duration_ms=duration_ms
        )
        return DomainValidationError(
            identifier=bibcode,
            reason="Bibcode not found in NASA ADS.",
        )

    titles = ads_data.get("title", [])
    title = titles[0] if titles else ""
    abstract = ads_data.get("abstract")
    journal = ads_data.get("pub")
    if _is_excluded_journal(journal):
        return DomainValidationError(
            identifier=bibcode,
            reason=f"Journal '{journal}' is not a heliophysics journal.",
            title=title,
        )

    if not _has_target_phrase(title, abstract):
        log.warning("target_phrase_missing", duration_ms=duration_ms)
        return DomainValidationError(
            identifier=bibcode,
            reason="Paper does not contain target phrases (inertial modes/waves, rossby modes/waves).",
            title=title,
        )

    if not _has_solar_indicator(title, abstract):
        log.warning("solar_indicator_missing", duration_ms=duration_ms)
        return DomainValidationError(
            identifier=bibcode,
            reason="Paper does not appear to be solar/heliophysics-related.",
            title=title,
        )
    if _is_non_solar(title, abstract):
        log.warning("non_solar_object", duration_ms=duration_ms)
        return DomainValidationError(
            identifier=bibcode,
            reason="Paper is about non-solar objects (white dwarfs, other stars, planets).",
            title=title,
        )
    log.info("fetch_complete", duration_ms=duration_ms, source="ads")
    return _normalize_ads(bibcode, ads_data)
