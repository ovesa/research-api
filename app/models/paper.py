from pydantic import BaseModel, field_validator
from typing import Optional
from datetime import datetime
from enum import Enum

# arXiv categories relevant to heliophysics
HELIOPHYSICS_ARXIV_CATEGORIES = {
    "astro-ph.SR",  # Solar and Stellar Astrophysics
    "physics.space-ph",  # Space Physics
}

# Journals that publish heliophysics research
HELIOPHYSICS_JOURNALS = {
    "the astrophysical journal",
    "the astrophysical journal letters",
    "the astrophysical journal supplement series",
    "astronomy and astrophysics",
    "solar physics",
    "space weather",
    "nature",
    "nature astronomy",
    "monthly notices of the royal astronomical society",
}

# Keywords that must appear in title or abstract for DOI lookups
# where journal matching alone is insufficient
# keywords I am particularly interested in
HELIOPHYSICS_KEYWORDS = {
    "solar wind",
    "solar flare",
    "coronal mass ejection",
    "cme",
    "photosphere",
    "chromosphere",
    "corona",
    "coronal",
    "space weather",
    "sunspot",
    "magnetohydrodynamics",
    "MHD",
    "SDO",
    "dynamo",
    "inertial modes",
    "rossby waves",
    "magnetic field",
    "gravity waves",
    "solar cycle",
    "solar physics",
    "solar interior",
    "helioseismology",
    "SDO/HMI",
    "HMI",
    "DKIST",
    "solar dynamo",
    "inertial modes",
    "helioseismology",
    "solar oscillation",
    "solar oscillations",
    "p-mode",
    "g-mode",
    "f-mode",
    "power spectrum",
    "doppler velocity",
    "eigenfrequency",
}


class IdentifierType(str, Enum):
    """The supported paper identifier formats.

    Attributes:
        doi: Digital Object Identifier. e.g. 10.1038/nature12373
        arxiv: arXiv preprint ID. e.g. 2103.08049
        ads: NASA ADS bibcode. e.g. 2021SoPh..296...84R
    """

    doi = "doi"
    arxiv = "arxiv"
    ads = "ads"


class Author(BaseModel):
    """Represents a single author on a paper.

    All fields except name are optional because external APIs are
    inconsistent. CrossRef sometimes returns only a name string while
    Semantic Scholar returns full affiliations. We normalize what we can.

    Attributes:
        name (str): Full name of the author.
        affiliation (str | None): Institution or organization. Not always
            returned by external APIs.
        orcid (str | None): ORCID identifier if available. Uniquely
            identifies a researcher across institutions.
    """

    name: str
    affiliation: Optional[str] = None
    orcid: Optional[str] = None


class PaperMetadata(BaseModel):
    """Normalized metadata structure returned for every heliophysics paper.

    This is the core output schema of the API. Data is pulled from one
    or more external sources and normalized into this consistent shape
    regardless of which source provided it. Only papers validated as
    heliophysics-relevant are stored and returned.

    Attributes:
        identifier (str): The DOI or arXiv ID used to look up this paper.
        identifier_type (IdentifierType): Whether it was a DOI or arXiv lookup.
        title (str): Full paper title.
        authors (list[Author]): Ordered list of authors.
        abstract (str | None): Full abstract text. Nullable because CrossRef
            frequently omits abstracts even for published papers.
        published_date (str | None): Publication date as a string. Format
            varies by source so stored normalized rather than as a datetime.
        journal (str | None): Journal or conference name if available.
        doi (str | None): DOI if known, even if lookup was via arXiv ID.
        arxiv_id (str | None): arXiv ID if known, even if lookup was via DOI.
        arxiv_categories (list[str]): arXiv subject categories. Used to
            validate heliophysics relevance for arXiv lookups.
        citation_count (int | None): From Semantic Scholar. Nullable because
            it is not always available especially for preprints.
        source (str): Which external API provided the primary data.
        fetched_at (datetime): When this record was retrieved from the source.
        is_heliophysics (bool): Whether this paper passed heliophysics
            domain validation. Always True for stored papers — included
            for transparency in the response.
        url (str | None): Direct link to the paper's abstract page.
            For arXiv papers: https://arxiv.org/abs/{arxiv_id}.
            For DOI papers: https://doi.org/{doi}.
    """

    identifier: str
    identifier_type: IdentifierType
    title: str
    authors: list[Author] = []
    abstract: Optional[str] = None
    published_date: Optional[str] = None
    journal: Optional[str] = None
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    arxiv_categories: list[str] = []
    citation_count: Optional[int] = None
    source: str
    fetched_at: datetime
    is_heliophysics: bool = True
    url: Optional[str] = None


class PaperLookupRequest(BaseModel):
    """Request body for looking up a single heliophysics paper.

    Attributes:
        identifier (str): The DOI or arXiv ID to look up.
        identifier_type (IdentifierType): Explicitly declare which type it is.
            Required because some strings are ambiguous without context.

    Example:
        {"identifier": "10.1038/nature12373", "identifier_type": "doi"}
        {"identifier": "2103.08049", "identifier_type": "arxiv"}
    """

    identifier: str
    identifier_type: IdentifierType

    @field_validator("identifier")
    @classmethod
    def identifier_must_not_be_empty(cls, v: str) -> str:
        """Reject blank strings before hitting any external API.

        Args:
            v (str): The raw identifier value from the request.

        Returns:
            str: The stripped identifier.

        Raises:
            ValueError: If the identifier is blank or whitespace only.
        """
        if not v.strip():
            raise ValueError("identifier cannot be empty")
        return v.strip()


class BulkLookupRequest(BaseModel):
    """Request body for looking up multiple heliophysics papers in one call.

    All identifiers in a single bulk request must be the same type.
    Mixed DOI and arXiv batches should be sent as separate requests.

    Attributes:
        identifiers (list[str]): List of DOIs or arXiv IDs. Capped at 50
            to prevent runaway external API usage.
        identifier_type (IdentifierType): The type that applies to all
            identifiers in the list.
    """

    identifiers: list[str]
    identifier_type: IdentifierType

    @field_validator("identifiers")
    @classmethod
    def validate_identifiers(cls, v: list[str]) -> list[str]:
        """Enforce list size limits and strip whitespace from each identifier.

        Capping at 50 is a practical limit. Each identifier may trigger up
        to 3 concurrent external API calls. 50 identifiers means up to 150
        outbound requests per bulk submission which is already aggressive.

        Args:
            v (list[str]): The raw list of identifiers from the request.

        Returns:
            list[str]: Cleaned list with whitespace stripped from each entry.

        Raises:
            ValueError: If the list is empty or contains more than 50 items.
        """
        if len(v) == 0:
            raise ValueError("identifiers list cannot be empty")
        if len(v) > 50:
            raise ValueError("maximum 50 identifiers per bulk request")
        return [i.strip() for i in v]


class CacheStats(BaseModel):
    """Cache performance metadata exposed via the /metrics endpoint.

    Attributes:
        hits (int): Number of requests served from Redis cache.
        misses (int): Number of requests that required external API calls.
        hit_rate (float): Ratio of hits to total requests between 0 and 1.
    """

    hits: int
    misses: int
    hit_rate: float


class DomainValidationError(BaseModel):
    """Response body returned when a paper fails heliophysics validation.

    Attributes:
        identifier (str): The identifier that was looked up.
        reason (str): Human readable explanation of why it was rejected.
        title (str | None): The paper title if it was retrievable before
            rejection. Helps the user confirm they submitted the right paper.
    """

    identifier: str
    reason: str
    title: Optional[str] = None


class PaperPatchRequest(BaseModel):
    """Request body for partially updating a stored paper.

    All fields are optional. Only the fields provided will be updated.
    System-managed fields (identifier, identifier_type, source,
    fetched_at, is_heliophysics) cannot be changed via this endpoint.

    Attributes:
        title (str | None): Corrected paper title.
        abstract (str | None): Full abstract text.
        url (str | None): Direct link to the paper.
        doi (str | None): Digital Object Identifier.
        arxiv_id (str | None): arXiv preprint ID.
        journal (str | None): Journal or conference name.
        citation_count (int | None): Manual citation count override.
        published_date (str | None): Publication date string.
    """

    title: Optional[str] = None
    abstract: Optional[str] = None
    url: Optional[str] = None
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    journal: Optional[str] = None
    citation_count: Optional[int] = None
    published_date: Optional[str] = None
