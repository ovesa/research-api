# Unit and integration tests for the heliophysics research paper API.
# These tests cover pure functions that require no database, Redis, or external API connections.
# They run fast and can be run anywhere.
# Integration tests cover API endpoints using FastAPI's test client.
# Database and cache calls are mocked so no real Postgres or Redis is needed as tests are fully self-contained.


from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.paper import (
    Author,
    IdentifierType,
    PaperLookupRequest,
    PaperMetadata,
)
from app.services.fetcher import (
    _is_heliophysics_by_journal,
    _is_heliophysics_by_keywords,
    _is_stellar_astrophysics,
)
from backfill import fix_url

######################################################
############# is_heliophysics_by_journal #############
######################################################


class TestIsHeliophysicsByJournal:
    """Tests for the journal whitelist validation function. Checks
    that known heliophysics journals are accepted, unknown journals
    are rejected, and edge cases like None and empty string are
    handled gracefully without crashing. Real data from CrossRef
    sometimes returns no journal at all, so these edge cases matter.
    """

    def test_known_heliophysics_journal(self):
        """Solar Physics is on the whitelist and should be accepted."""
        assert _is_heliophysics_by_journal("Solar Physics") is True

    def test_astrophysical_journal(self):
        """The Astrophysical Journal is on the whitelist and should
        be accepted.
        """
        assert _is_heliophysics_by_journal("The Astrophysical Journal") is True

    def test_case_insensitive(self):
        """Journal matching must be case-insensitive. CrossRef returns
        journals in inconsistent casing. Both lowercase and uppercase
        versions of a known journal must match.
        """
        assert _is_heliophysics_by_journal("solar physics") is True
        assert _is_heliophysics_by_journal("SOLAR PHYSICS") is True

    def test_unknown_journal(self):
        """A journal outside the heliophysics whitelist should be
        rejected.
        """
        assert _is_heliophysics_by_journal("Journal of Neuroscience") is False

    def test_none_journal(self):
        """None must be handled gracefully. CrossRef often omits the
        journal field.
        """
        assert _is_heliophysics_by_journal(None) is False

    def test_empty_string(self):
        """An empty string journal should be rejected, not crash."""
        assert _is_heliophysics_by_journal("") is False

    def test_nature_astronomy(self):
        """Nature Astronomy is on the whitelist and should be accepted."""
        assert _is_heliophysics_by_journal("Nature Astronomy") is True

    def test_space_weather(self):
        """Space Weather is on the whitelist and should be accepted."""
        assert _is_heliophysics_by_journal("Space Weather") is True


######################################################
############# is_heliophysics_by_keywords ############
######################################################


class TestIsHeliophysicsByKeywords:
    """Tests for the keyword fallback validation function. This
    function is used when journal matching fails. For example,
    when a paper is published in Nature or a broad journal. It
    checks whether heliophysics keywords appear in the title or
    abstract. Key behaviors to verify: keywords in either field
    trigger a match, unrelated text does not match, None abstract
    is handled gracefully (CrossRef frequently omits abstracts),
    and matching is case-insensitive so uppercase acronyms like
    SDO and MHD are caught.
    """

    def test_solar_wind_in_title(self):
        """A heliophysics keyword in the title should trigger a match."""
        assert _is_heliophysics_by_keywords("Solar wind dynamics", None) is True

    def test_keyword_in_abstract(self):
        """A heliophysics keyword in the abstract should trigger a match
        even if the title alone contains no keywords.
        """
        assert (
            _is_heliophysics_by_keywords(
                "A study of waves",
                "We observe coronal mass ejection events",
            )
            is True
        )

    def test_no_keywords(self):
        """Text with no heliophysics keywords should not match."""
        assert (
            _is_heliophysics_by_keywords(
                "Cancer biology research", "A study of tumor cells"
            )
            is False
        )

    def test_none_abstract(self):
        """None abstract must not crash. CrossRef frequently omits abstracts."""
        assert _is_heliophysics_by_keywords("Solar flare observations", None) is True

    def test_case_insensitive(self):
        """Keyword matching must be case-insensitive. The HELIOPHYSICS_KEYWORDS
        set contains uppercase acronyms like 'SDO' and 'MHD'. Lowercased input
        must still match them.
        """
        assert _is_heliophysics_by_keywords("sdo", None) is True

    def test_helioseismology_keyword(self):
        """Helioseismology is in the keyword set and should match."""
        assert _is_heliophysics_by_keywords("A helioseismology paper", None) is True

    def test_empty_title_and_abstract(self):
        """Empty strings for both fields should return False, not crash."""
        assert _is_heliophysics_by_keywords("", "") is False


######################################################
###############_is_stellar_astrophysics ##############
######################################################


class TestIsStellarAstrophysics:
    """Tests for the stellar astrophysics rejection filter. Some papers
    pass heliophysics keyword checks because they mention 'plasma' or
    'magnetic field', but are actually about stellar evolution, stellar
    populations, or other non-solar topics. This filter catches them.
    The most important tests are the negative cases. For example, solar
    and coronal papers must NOT be accidentally rejected by this filter.
    """

    def test_neutron_star_rejected(self):
        """Neutron star papers are stellar astrophysics and should be rejected."""
        assert (
            _is_stellar_astrophysics("Neutron star merger observations", None) is True
        )

    def test_black_hole_rejected(self):
        """Black hole papers are stellar astrophysics and should be rejected."""
        assert _is_stellar_astrophysics("Black hole accretion disk", None) is True

    def test_exoplanet_rejected(self):
        """Exoplanet papers are not heliophysics and should be rejected."""
        assert _is_stellar_astrophysics("Exoplanet transit photometry", None) is True

    def test_solar_paper_not_rejected(self):
        """Solar wind papers must NOT be rejected by the stellar filter."""
        assert (
            _is_stellar_astrophysics("Solar wind velocity measurements", None) is False
        )

    def test_coronal_paper_not_rejected(self):
        """Coronal papers must NOT be rejected by the stellar filter."""
        assert _is_stellar_astrophysics("Coronal mass ejection study", None) is False

    def test_case_insensitive(self):
        """Rejection filter must be case-insensitive."""
        assert _is_stellar_astrophysics("RED GIANT oscillations", None) is True

    def test_supernova_rejected(self):
        """Supernova papers are stellar astrophysics and should be rejected."""
        assert _is_stellar_astrophysics("Supernova remnant analysis", None) is True


######################################################
###################### fix_url #######################
######################################################


class TestFixUrl:
    """Tests for the URL derivation logic in backfill.py. fix_url constructs a
    URL from known identifiers without hitting any external API. arXiv papers
    get arxiv.org URLs, DOI papers get doi.org URLs, and ADS papers get ADS
    abstract page URLs. The priority test is key. If a paper has both an arxiv_id
    and a doi, arXiv takes priority since it links directly to the full text.
    """

    def test_arxiv_paper_gets_arxiv_url(self):
        """A paper with an arxiv_id should get an arxiv.org URL."""
        paper = {
            "arxiv_id": "2509.19847",
            "doi": None,
            "identifier": "2509.19847",
            "identifier_type": "arxiv",
        }
        assert fix_url(paper) == "https://arxiv.org/abs/2509.19847"

    def test_doi_paper_gets_doi_url(self):
        """A paper with a DOI and no arxiv_id should get a doi.org URL."""
        paper = {
            "arxiv_id": None,
            "doi": "10.1007/s11207-021-01842-0",
            "identifier": "10.1007/s11207-021-01842-0",
            "identifier_type": "doi",
        }
        assert fix_url(paper) == "https://doi.org/10.1007/s11207-021-01842-0"

    def test_ads_paper_gets_ads_url(self):
        """An ADS paper with no arxiv_id or doi should get an ADS abstract URL."""
        paper = {
            "arxiv_id": None,
            "doi": None,
            "identifier": "2025ApJ...123..456V",
            "identifier_type": "ads",
        }
        assert fix_url(paper) == "https://ui.adsabs.harvard.edu/abs/2025ApJ...123..456V"

    def test_arxiv_takes_priority_over_doi(self):
        """When both arxiv_id and doi are present, arxiv_id takes priority."""
        paper = {
            "arxiv_id": "2509.19847",
            "doi": "10.1007/test",
            "identifier": "2509.19847",
            "identifier_type": "arxiv",
        }
        assert fix_url(paper) == "https://arxiv.org/abs/2509.19847"

    def test_no_identifiers_returns_none(self):
        """A paper with no usable identifiers should return None, not crash."""
        paper = {
            "arxiv_id": None,
            "doi": None,
            "identifier": "unknown",
            "identifier_type": "doi",
        }
        assert fix_url(paper) is None


######################################################
############ PaperLookupRequest validation ###########
######################################################


class TestPaperLookupRequestValidation:
    """Tests for Pydantic model validation on paper lookup requests. Verifies
    that the regex checks added to PaperLookupRequest work correctly (i.e., valid
    identifiers pass, malformed ones raise exceptions, empty strings are rejected,
    and whitespace is stripped automatically by the field validator).
    """

    def test_valid_doi(self):
        """A well-formed DOI should be accepted without error."""
        req = PaperLookupRequest(
            identifier="10.1038/nature12373",
            identifier_type=IdentifierType.doi,
        )
        assert req.identifier == "10.1038/nature12373"

    def test_valid_arxiv(self):
        """A well-formed arXiv ID should be accepted without error."""
        req = PaperLookupRequest(
            identifier="2509.19847",
            identifier_type=IdentifierType.arxiv,
        )
        assert req.identifier == "2509.19847"

    def test_valid_arxiv_with_version(self):
        """A versioned arXiv ID like 2509.19847v2 should be accepted."""
        req = PaperLookupRequest(
            identifier="2509.19847v2",
            identifier_type=IdentifierType.arxiv,
        )
        assert req.identifier == "2509.19847v2"

    def test_invalid_doi_raises(self):
        """A string that doesn't start with '10.' should be rejected as a DOI."""
        with pytest.raises(Exception):
            PaperLookupRequest(
                identifier="not-a-doi",
                identifier_type=IdentifierType.doi,
            )

    def test_invalid_arxiv_raises(self):
        """A string that doesn't follow YYMM.NNNNN format should be rejected."""
        with pytest.raises(Exception):
            PaperLookupRequest(
                identifier="abc123",
                identifier_type=IdentifierType.arxiv,
            )

    def test_empty_identifier_raises(self):
        """A whitespace-only identifier should be rejected before regex checks."""
        with pytest.raises(Exception):
            PaperLookupRequest(
                identifier="   ",
                identifier_type=IdentifierType.arxiv,
            )

    def test_whitespace_stripped(self):
        """Leading and trailing whitespace should be stripped automatically."""
        req = PaperLookupRequest(
            identifier="  2509.19847  ",
            identifier_type=IdentifierType.arxiv,
        )
        assert req.identifier == "2509.19847"


######################################################
################# Integration tests ##################
######################################################

# TestClient makes HTTP requests to the app without starting a real server.
# Database and cache functions are mocked with AsyncMock so no real
# Postgres or Redis connection is required.
client = TestClient(app)


def make_paper(
    identifier: str = "2509.19847", identifier_type: str = "arxiv"
) -> PaperMetadata:
    """Create a sample PaperMetadata object for use in tests. Centralizes
    paper construction so tests don't repeat the same boilerplate. Pass a
    different identifier to create distinct papers for pagination tests.

    Args:
        identifier (str): The paper identifier. Defaults to a real arXiv ID.
        identifier_type (str): One of 'arxiv', 'doi', 'ads'. Defaults to 'arxiv'.

    Returns:
        PaperMetadata: A fully populated sample paper ready for use in assertions.
    """
    return PaperMetadata(
        identifier=identifier,
        identifier_type=IdentifierType(identifier_type),
        title="Atmospheric Gravity Waves Modulated by the Magnetic Field",
        authors=[Author(name="Oana Vesa")],
        abstract="A study of atmospheric gravity waves in the solar atmosphere.",
        published_date="2025-09-24",
        journal=None,
        doi=None,
        arxiv_id="2509.19847",
        arxiv_categories=["astro-ph.SR"],
        citation_count=5,
        source="arxiv",
        fetched_at=datetime.now(timezone.utc),
        url="https://arxiv.org/abs/2509.19847",
    )


class TestHealthEndpoints:
    """Tests for health check endpoints. These endpoints don't touch the database
    so no mocking is needed. They should always return 200 with status 'ok' as
    long as the app starts correctly.
    """

    def test_liveness(self):
        """GET /health/live should return 200 confirming the process is running."""
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_papers_health(self):
        """GET /papers/health should return 200 confirming the router is reachable."""
        response = client.get("/papers/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestSearchEndpoint:
    """Tests for the full text search endpoint GET /papers/search. search_papers
    is mocked to avoid hitting the database. Tests verify response structure,
    pagination metadata, and input validation.
    """

    def test_search_returns_results(self):
        """A valid search query should return papers with correct pagination metadata."""
        with patch("app.routers.papers.search_papers", new_callable=AsyncMock) as mock:
            mock.return_value = ([make_paper()], 1)
            response = client.get("/papers/search?q=solar+wind")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["current_page"] == 1
        assert data["has_next"] is False
        assert len(data["papers"]) == 1
        assert (
            data["papers"][0]["title"]
            == "Atmospheric Gravity Waves Modulated by the Magnetic Field"
        )

    def test_search_requires_minimum_length(self):
        """A single character query should be rejected with 422 by FastAPI validation."""
        response = client.get("/papers/search?q=a")
        assert response.status_code == 422

    def test_search_empty_query_rejected(self):
        """A whitespace-only query should be rejected with 400 by the endpoint."""
        response = client.get("/papers/search?q=   ")
        assert response.status_code == 400

    def test_search_pagination_metadata(self):
        """Pagination metadata should be calculated correctly for multi-page results."""
        papers = [make_paper(f"209{i}.19847") for i in range(5)]
        with patch("app.routers.papers.search_papers", new_callable=AsyncMock) as mock:
            mock.return_value = (papers, 25)
            response = client.get("/papers/search?q=solar&limit=5&offset=0")

        data = response.json()
        assert data["total"] == 25
        assert data["total_pages"] == 5
        assert data["has_next"] is True
        assert data["has_prev"] is False
        assert data["next_offset"] == 5


class TestFilterEndpoint:
    """Tests for the keyword filter endpoint GET /papers/filter. filter_papers_by_keywords
    is mocked to avoid hitting the database. Tests verify keyword parsing, match_all
    parameter handling, and empty keyword rejection.
    """

    def test_filter_returns_results(self):
        """A valid keyword filter should return papers and echo the parsed keywords."""
        with patch(
            "app.routers.papers.filter_papers_by_keywords", new_callable=AsyncMock
        ) as mock:
            mock.return_value = ([make_paper()], 1)
            response = client.get("/papers/filter?keywords=solar+wind")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert "keywords" in data
        assert data["keywords"] == ["solar wind"]

    def test_filter_empty_keywords_rejected(self):
        """An empty keywords parameter should be rejected with 400."""
        response = client.get("/papers/filter?keywords=")
        assert response.status_code == 400

    def test_filter_match_all_parameter(self):
        """match_all=true should be accepted and echoed back in the response."""
        with patch(
            "app.routers.papers.filter_papers_by_keywords", new_callable=AsyncMock
        ) as mock:
            mock.return_value = ([], 0)
            response = client.get("/papers/filter?keywords=solar,wind&match_all=true")

        assert response.status_code == 200
        assert response.json()["match_all"] is True


class TestDeleteEndpoint:
    """Tests for the delete endpoint DELETE /papers/{identifier}. Both delete_paper
    (database) and delete_cached_paper (Redis) are mocked. Note: delete_cached_paper
    is patched at app.cache rather than app.routers.papers because it is imported
    inside the function body.
    """

    def test_delete_existing_paper(self):
        """Deleting an existing paper should return 200 with deleted=true."""
        with (
            patch(
                "app.routers.papers.delete_paper", new_callable=AsyncMock
            ) as mock_delete,
            patch("app.cache.delete_cached_paper", new_callable=AsyncMock),
        ):
            mock_delete.return_value = True
            response = client.delete("/papers/2509.19847")

        assert response.status_code == 200
        assert response.json()["deleted"] is True
        assert response.json()["identifier"] == "2509.19847"

    def test_delete_nonexistent_paper_returns_404(self):
        """Deleting a paper that doesn't exist should return 404."""
        with patch(
            "app.routers.papers.delete_paper", new_callable=AsyncMock
        ) as mock_delete:
            mock_delete.return_value = False
            response = client.delete("/papers/fake-identifier")

        assert response.status_code == 404


class TestPatchEndpoint:
    """Tests for the partial update endpoint PATCH /papers/{identifier}. patch_paper
    (database) and cache_paper (Redis refresh) are mocked. Tests verify that field
    updates are reflected in the response, empty bodies are rejected, and missing
    papers return 404.
    """

    def test_patch_updates_field(self):
        """A valid patch request should return the updated paper."""
        updated = make_paper()
        updated.journal = "Solar Physics"

        with (
            patch(
                "app.routers.papers.patch_paper", new_callable=AsyncMock
            ) as mock_patch,
            patch("app.routers.papers.cache_paper", new_callable=AsyncMock),
        ):
            mock_patch.return_value = updated
            response = client.patch(
                "/papers/2509.19847",
                json={"journal": "Solar Physics"},
            )

        assert response.status_code == 200
        assert response.json()["journal"] == "Solar Physics"

    def test_patch_empty_body_returns_400(self):
        """An empty patch body should be rejected with 400 before hitting the database."""
        response = client.patch("/papers/2509.19847", json={})
        assert response.status_code == 400

    def test_patch_nonexistent_paper_returns_404(self):
        """Patching a paper that doesn't exist should return 404."""
        with patch(
            "app.routers.papers.patch_paper", new_callable=AsyncMock
        ) as mock_patch:
            mock_patch.return_value = None
            response = client.patch(
                "/papers/fake-identifier",
                json={"journal": "Solar Physics"},
            )
        assert response.status_code == 404


class TestListEndpoint:
    """Tests for the list endpoint GET /papers/. list_papers is mocked to avoid
    hitting the database. Tests verify that sorting validation rejects invalid
    fields and orders before touching the database, and that all whitelisted sort
    fields are accepted.
    """

    def test_list_returns_papers(self):
        """A basic list request should return papers with pagination metadata."""
        with patch("app.routers.papers.list_papers", new_callable=AsyncMock) as mock:
            mock.return_value = ([make_paper()], 1)
            response = client.get("/papers/")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["papers"]) == 1

    def test_list_invalid_sort_field_rejected(self):
        """An unrecognised sort_by field should be rejected with 400."""
        response = client.get("/papers/?sort_by=invalid_field")
        assert response.status_code == 400

    def test_list_invalid_sort_order_rejected(self):
        """A sort_order value other than 'asc' or 'desc' should be rejected with 400."""
        response = client.get("/papers/?sort_order=sideways")
        assert response.status_code == 400

    def test_list_valid_sort_fields(self):
        """All whitelisted sort fields should be accepted and return 200."""
        with patch("app.routers.papers.list_papers", new_callable=AsyncMock) as mock:
            mock.return_value = ([], 0)
            for field in ["fetched_at", "published_date", "citation_count", "title"]:
                response = client.get(f"/papers/?sort_by={field}")
                assert response.status_code == 200
