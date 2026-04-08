# Unit tests for the heliophysics research paper API
# These tests cover pure functions that require no database, Redis, or external API connections.
# They run fast and can be run anywhere.


import pytest
from app.models.paper import (
    IdentifierType,
    PaperLookupRequest,
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
    # Given this journal name, is the output correct?

    def test_known_heliophysics_journal(self):
        assert _is_heliophysics_by_journal("Solar Physics") is True

    def test_astrophysical_journal(self):
        assert _is_heliophysics_by_journal("The Astrophysical Journal") is True

    def test_case_insensitive(self):
        assert _is_heliophysics_by_journal("solar physics") is True
        assert _is_heliophysics_by_journal("SOLAR PHYSICS") is True

    def test_unknown_journal(self):
        assert _is_heliophysics_by_journal("Journal of Neuroscience") is False

    def test_none_journal(self):
        assert _is_heliophysics_by_journal(None) is False

    def test_empty_string(self):
        assert _is_heliophysics_by_journal("") is False

    def test_nature_astronomy(self):
        assert _is_heliophysics_by_journal("Nature Astronomy") is True

    def test_space_weather(self):
        assert _is_heliophysics_by_journal("Space Weather") is True


######################################################
############# is_heliophysics_by_keywords ############
######################################################


class TestIsHeliophysicsByKeywords:
    # Check that keywords in either the title or abstract trigger a match
    # and that unrelated text does not.

    def test_solar_wind_in_title(self):
        assert _is_heliophysics_by_keywords("Solar wind dynamics", None) is True

    def test_keyword_in_abstract(self):
        assert (
            _is_heliophysics_by_keywords(
                "A study of waves",
                "We observe coronal mass ejection events",
            )
            is True
        )

    def test_no_keywords(self):
        assert (
            _is_heliophysics_by_keywords(
                "Cancer biology research", "A study of tumor cells"
            )
            is False
        )

    def test_none_abstract(self):
        assert _is_heliophysics_by_keywords("Solar flare observations", None) is True

    def test_case_insensitive(self):
        assert _is_heliophysics_by_keywords("sdo", None) is True

    def test_helioseismology_keyword(self):
        assert _is_heliophysics_by_keywords("A helioseismology paper", None) is True

    def test_empty_title_and_abstract(self):
        assert _is_heliophysics_by_keywords("", "") is False


######################################################
###############_is_stellar_astrophysics ##############
######################################################


class TestIsStellarAstrophysics:
    # Test rejection filter (i.e., papers that slip through
    # the keyword matching but are actually stellar astrophysics
    # and not solar physics)

    def test_neutron_star_rejected(self):
        assert (
            _is_stellar_astrophysics("Neutron star merger observations", None) is True
        )

    def test_black_hole_rejected(self):
        assert _is_stellar_astrophysics("Black hole accretion disk", None) is True

    def test_exoplanet_rejected(self):
        assert _is_stellar_astrophysics("Exoplanet transit photometry", None) is True

    def test_solar_paper_not_rejected(self):
        assert (
            _is_stellar_astrophysics("Solar wind velocity measurements", None) is False
        )

    def test_coronal_paper_not_rejected(self):
        assert _is_stellar_astrophysics("Coronal mass ejection study", None) is False

    def test_case_insensitive(self):
        assert _is_stellar_astrophysics("RED GIANT oscillations", None) is True

    def test_supernova_rejected(self):
        assert _is_stellar_astrophysics("Supernova remnant analysis", None) is True


######################################################
###################### fix_url #######################
######################################################


class TestFixUrl:
    # Test the URL logic from backfill.py

    def test_arxiv_paper_gets_arxiv_url(self):
        paper = {
            "arxiv_id": "2509.19847",
            "doi": None,
            "identifier": "2509.19847",
            "identifier_type": "arxiv",
        }
        assert fix_url(paper) == "https://arxiv.org/abs/2509.19847"

    def test_doi_paper_gets_doi_url(self):
        paper = {
            "arxiv_id": None,
            "doi": "10.1007/s11207-021-01842-0",
            "identifier": "10.1007/s11207-021-01842-0",
            "identifier_type": "doi",
        }
        assert fix_url(paper) == "https://doi.org/10.1007/s11207-021-01842-0"

    def test_ads_paper_gets_ads_url(self):
        paper = {
            "arxiv_id": None,
            "doi": None,
            "identifier": "2025ApJ...123..456V",
            "identifier_type": "ads",
        }
        assert fix_url(paper) == "https://ui.adsabs.harvard.edu/abs/2025ApJ...123..456V"

    def test_arxiv_takes_priority_over_doi(self):
        paper = {
            "arxiv_id": "2509.19847",
            "doi": "10.1007/test",
            "identifier": "2509.19847",
            "identifier_type": "arxiv",
        }
        assert fix_url(paper) == "https://arxiv.org/abs/2509.19847"

    def test_no_identifiers_returns_none(self):
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
    # Test Pydantic model validation: the regex checks on
    # identifiers

    def test_valid_doi(self):
        req = PaperLookupRequest(
            identifier="10.1038/nature12373",
            identifier_type=IdentifierType.doi,
        )
        assert req.identifier == "10.1038/nature12373"

    def test_valid_arxiv(self):
        req = PaperLookupRequest(
            identifier="2509.19847",
            identifier_type=IdentifierType.arxiv,
        )
        assert req.identifier == "2509.19847"

    def test_valid_arxiv_with_version(self):
        req = PaperLookupRequest(
            identifier="2509.19847v2",
            identifier_type=IdentifierType.arxiv,
        )
        assert req.identifier == "2509.19847v2"

    def test_invalid_doi_raises(self):
        with pytest.raises(Exception):
            PaperLookupRequest(
                identifier="not-a-doi",
                identifier_type=IdentifierType.doi,
            )

    def test_invalid_arxiv_raises(self):
        with pytest.raises(Exception):
            PaperLookupRequest(
                identifier="abc123",
                identifier_type=IdentifierType.arxiv,
            )

    def test_empty_identifier_raises(self):
        with pytest.raises(Exception):
            PaperLookupRequest(
                identifier="   ",
                identifier_type=IdentifierType.arxiv,
            )

    def test_whitespace_stripped(self):
        req = PaperLookupRequest(
            identifier="  2509.19847  ",
            identifier_type=IdentifierType.arxiv,
        )
        assert req.identifier == "2509.19847"
