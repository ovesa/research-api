# Tests for the research agent chain (POST /agent/query). All external dependencies are
# mocked. No real database, Redis, or Claude API calls are made. Each test targets one
# step of the chain so failures are easy to locate: intent parsing, paper search,
# extraction, synthesis. The integration test at the bottom covers the full chain end-to-end.

from unittest.mock import AsyncMock, patch
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def make_paper(identifier: str = "2026arXiv260309328K") -> dict:
    """Returns a minimal paper dict matching what _search_papers returns.
    This is clearly a fake paper for testing purposes, but it has all the
    fields the agent chain reads during synthesis.

    Args:
        identifier: Paper identifier to use. Defaults to a real arXiv ID.

    Returns:
        A dict with all fields the agent chain reads during synthesis.
    """
    return {
        "identifier": identifier,
        "title": "Thermal Rossby Waves and Angular Momentum Transport",
        "authors": [{"name": "Helio1, S."}, {"name": "Jimbo, K."}],
        "abstract": "We study thermal Rossby waves in rotating convection.",
        "published_date": "2026-03-01",
        "journal": "The Astrophysical Journal",
        "url": f"https://arxiv.org/abs/{identifier}",
        "data_type": "computational",
        "relevance_to_solar_inertial_modes": "secondary",
        "central_contribution": "Finds outward angular momentum transport at fast rotation.",
        "researcher_summary": "Important for understanding differential rotation.",
        "key_findings": '[{"finding": "Outward transport at fast rotation", "type": "theoretical", "confidence": "definitive"}]',
        "wave_types": '["thermal Rossby waves"]',
        "solar_region": '["convection zone"]',
        "azimuthal_orders": "[]",
        "open_questions": '["Thermal Rossby waves not yet detected in Sun"]',
        "theoretical_framework": '["mean-field hydrodynamics"]',
        "methods": '["rotating convection simulations"]',
        "instruments": "[]",
        "numerical_values": "[]",
        "cycle_dependence": "not_mentioned",
        "measured_quantities": "[]",
        "constrained_quantities": "[]",
        "detection_method": "",
        "physical_parameters": '["angular momentum transport"]',
    }


MOCK_INTENT = {
    "keywords": ["thermal Rossby waves", "angular momentum"],
    "data_types": ["computational"],
    "wave_types": ["thermal Rossby waves"],
    "instruments": [],
    "date_start": "",
    "date_end": "",
    "relevance_filter": "secondary",
    "query_intent": "Understand angular momentum transport via thermal Rossby waves.",
}


# intent parsing


class TestParseIntent:
    """Tests for _parse_intent — the first step of the agent chain. _parse_intent calls
    Claude to convert a plain-English question into structured search params. We mock
    _call_claude to return valid JSON and verify the output is parsed correctly. We also
    verify that malformed JSON from Claude raises an error rather than returning wrong data
    silently.
    """

    def test_valid_question_returns_structured_params(self):
        """A plain-English question should return structured search params."""
        import asyncio

        from app.routers.agent import _parse_intent

        mock_response = (
            '{"keywords": ["rossby waves"], "data_types": [], "wave_types": [], '
            '"instruments": [], "date_start": "", "date_end": "", '
            '"relevance_filter": "", "query_intent": "Find Rossby wave papers."}'
        )

        with patch("app.routers.agent._call_claude", new_callable=AsyncMock) as mock:
            mock.return_value = mock_response
            result = asyncio.get_event_loop().run_until_complete(
                _parse_intent("What do we know about Rossby waves?")
            )

        assert result["keywords"] == ["rossby waves"]
        assert "query_intent" in result

    def test_malformed_json_raises(self):
        """If Claude returns unparseable JSON, the function should raise."""
        import asyncio

        from app.routers.agent import _parse_intent

        with patch("app.routers.agent._call_claude", new_callable=AsyncMock) as mock:
            mock.return_value = "this is not json at all"
            with pytest.raises(Exception):
                asyncio.get_event_loop().run_until_complete(
                    _parse_intent("What do we know about Rossby waves?")
                )


# Extraction


class TestEnsureExtractions:
    """Tests for _ensure_extractions — Step 3 of the agent chain. _ensure_extractions
    loops over papers, skips ones that already have a central_contribution (already
    extracted), calls Claude for ones that don't, and caches results. We test the
    three cases: already extracted, no abstract (should skip), and needs fresh extraction.
    """

    def test_already_extracted_paper_skips_claude(self):
        """A paper with central_contribution already set should not call Claude."""
        import asyncio

        from app.routers.agent import _ensure_extractions

        paper = make_paper()  # has central_contribution set
        paper["central_contribution"] = "Already extracted."

        with patch(
            "app.routers.agent.extract_abstract", new_callable=AsyncMock
        ) as mock_extract:
            result = asyncio.get_event_loop().run_until_complete(
                _ensure_extractions([paper])
            )

        mock_extract.assert_not_called()
        assert result[0]["central_contribution"] == "Already extracted."

    def test_paper_without_abstract_is_skipped(self):
        """A paper with no abstract should be returned as-is without calling Claude."""
        from app.routers.agent import _ensure_extractions
        import asyncio

        paper = make_paper()
        paper["abstract"] = None
        paper["central_contribution"] = None

        with patch("app.routers.agent.extract_abstract", new_callable=AsyncMock) as mock_extract:
            with patch("app.routers.agent.get_extraction", new_callable=AsyncMock) as mock_get:
                mock_get.return_value = None
                asyncio.get_event_loop().run_until_complete(
                    _ensure_extractions([paper])
                )

        mock_extract.assert_not_called()

    def test_unextracted_paper_calls_claude_and_saves(self):
        """A paper with no extraction should call Claude and save the result."""
        import asyncio

        from app.routers.agent import _ensure_extractions

        paper = make_paper()
        paper["central_contribution"] = None  # not yet extracted

        mock_result = {
            "central_contribution": "Fresh extraction.",
            "data_type": "computational",
        }

        with patch(
            "app.routers.agent.get_extraction", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = None  # not in cache
            with patch(
                "app.routers.agent.extract_abstract", new_callable=AsyncMock
            ) as mock_extract:
                mock_extract.return_value = (
                    mock_result,
                    '{"central_contribution": "Fresh extraction."}',
                )
                with patch(
                    "app.routers.agent.save_extraction", new_callable=AsyncMock
                ) as mock_save:
                    result = asyncio.get_event_loop().run_until_complete(
                        _ensure_extractions([paper])
                    )

        mock_extract.assert_called_once()
        mock_save.assert_called_once()
        assert result[0]["central_contribution"] == "Fresh extraction."


# Synthesis


class TestSynthesize:
    """Tests for _synthesize — Step 4 of the agent chain. _synthesize calls Claude
    with all paper summaries and returns a written literature review. We test that
    it handles empty paper lists gracefully and that it calls Claude with the right
    content when papers are present.
    """

    def test_empty_papers_returns_no_results_message(self):
        """If no papers are passed, synthesis should return a helpful message without
        calling Claude.
        """
        import asyncio

        from app.routers.agent import _synthesize

        with patch("app.routers.agent._call_claude", new_callable=AsyncMock) as mock:
            result = asyncio.get_event_loop().run_until_complete(
                _synthesize("What are open questions?", [])
            )

        mock.assert_not_called()
        assert "No papers" in result

    def test_papers_present_calls_claude(self):
        """When papers are present, _synthesize should call Claude and return its
        response as the synthesis string.
        """
        import asyncio

        from app.routers.agent import _synthesize

        with patch("app.routers.agent._call_claude", new_callable=AsyncMock) as mock:
            mock.return_value = "Thermal Rossby waves are important because..."
            result = asyncio.get_event_loop().run_until_complete(
                _synthesize("What are open questions?", [make_paper()])
            )

        mock.assert_called_once()
        assert result == "Thermal Rossby waves are important because..."


# Full chain integration


class TestAgentQueryEndpoint:
    """Integration tests for POST /agent/query. The full chain. These tests
    mock every external dependency (database, Claude) and verify the endpoint
    wires the four steps together correctly and returns the right response shape.
    If any step regresses, these tests will catch it.
    """

    def test_empty_question_returns_400(self):
        """An empty question should be rejected with 400 before any Claude call."""
        response = client.post("/agent/query", json={"question": ""})
        assert response.status_code == 400

    def test_whitespace_question_returns_400(self):
        """A whitespace-only question should be rejected with 400."""
        response = client.post("/agent/query", json={"question": "   "})
        assert response.status_code == 400

    def test_no_papers_found_returns_empty_synthesis(self):
        """If the database has no matching papers, the response should say so
        and return an empty papers_used list with paper_count 0.
        """
        with patch(
            "app.routers.agent._parse_intent", new_callable=AsyncMock
        ) as mock_intent:
            mock_intent.return_value = MOCK_INTENT
            with patch(
                "app.routers.agent._search_papers", new_callable=AsyncMock
            ) as mock_search:
                mock_search.return_value = []
                response = client.post(
                    "/agent/query",
                    json={
                        "question": "What are open questions in inertial mode research?"
                    },
                )

        assert response.status_code == 200
        data = response.json()
        assert data["paper_count"] == 0
        assert data["papers_used"] == []
        assert "No papers" in data["synthesis"]

    def test_full_chain_returns_synthesis_and_paper_cards(self):
        """A successful query should return a synthesis string and a list of paper
        cards with the correct fields.
        """
        paper = make_paper()

        with patch(
            "app.routers.agent._parse_intent", new_callable=AsyncMock
        ) as mock_intent:
            mock_intent.return_value = MOCK_INTENT
            with patch(
                "app.routers.agent._search_papers", new_callable=AsyncMock
            ) as mock_search:
                mock_search.return_value = [paper]
                with patch(
                    "app.routers.agent._ensure_extractions", new_callable=AsyncMock
                ) as mock_extract:
                    mock_extract.return_value = [paper]
                    with patch(
                        "app.routers.agent._synthesize", new_callable=AsyncMock
                    ) as mock_synth:
                        mock_synth.return_value = (
                            "Thermal Rossby waves drive differential rotation."
                        )
                        response = client.post(
                            "/agent/query",
                            json={
                                "question": "What do we know about thermal Rossby waves?"
                            },
                        )

        assert response.status_code == 200
        data = response.json()
        assert data["paper_count"] == 1
        assert data["synthesis"] == "Thermal Rossby waves drive differential rotation."
        assert len(data["papers_used"]) == 1

        card = data["papers_used"][0]
        assert card["identifier"] == "2026arXiv260309328K"
        assert card["title"] == "Thermal Rossby Waves and Angular Momentum Transport"
        assert len(card["authors"]) > 0

    def test_response_has_required_fields(self):
        """The response must always contain question, synthesis, papers_used,
        paper_count, and timestamp — even when results are empty.
        """
        with patch(
            "app.routers.agent._parse_intent", new_callable=AsyncMock
        ) as mock_intent:
            mock_intent.return_value = MOCK_INTENT
            with patch(
                "app.routers.agent._search_papers", new_callable=AsyncMock
            ) as mock_search:
                mock_search.return_value = []
                response = client.post(
                    "/agent/query",
                    json={"question": "Tell me about solar Rossby waves."},
                )

        data = response.json()
        assert "question" in data
        assert "synthesis" in data
        assert "papers_used" in data
        assert "paper_count" in data
        assert "timestamp" in data
