import json
from datetime import datetime, timezone

import httpx
import structlog

from app.services.database import get_pool

logger = structlog.get_logger(__name__)


async def get_extraction(identifier: str) -> dict | None:
    """Return cached extraction for a paper, or None if not yet extracted.

    Args:
        identifier (str): ADS bibcode or arXiv ID of the paper.

    Returns:
        dict | None: Extraction result if cached, None otherwise.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM extractions WHERE identifier = $1",
            identifier,
        )
    if not row:
        return None
    return dict(row)


async def save_extraction(identifier: str, result: dict) -> None:
    """Save an extraction result to Postgres.

    Args:
        identifier (str): ADS bibcode or arXiv ID of the paper.
        result (dict): Structured extraction from Claude.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO extractions (identifier, methods, key_findings, data_type, instruments, extracted_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (identifier) DO UPDATE SET
                methods = EXCLUDED.methods,
                key_findings = EXCLUDED.key_findings,
                data_type = EXCLUDED.data_type,
                instruments = EXCLUDED.instruments,
                extracted_at = EXCLUDED.extracted_at
            """,
            identifier,
            json.dumps(result.get("methods")),
            json.dumps(result.get("key_findings")),
            result.get("data_type"),
            json.dumps(result.get("instruments")),
            datetime.now(timezone.utc),
        )


async def extract_abstract(identifier: str, title: str, abstract: str) -> dict:
    """Send a paper abstract to Claude and return structured extraction.
    Calls the Anthropic API with a structured prompt asking Claude to
    extract methods, key findings, data type, and instruments from the
    abstract. Returns parsed JSON.

    Args:
        identifier (str): Paper identifier for logging.
        title (str): Paper title.
        abstract (str): Paper abstract text.

    Returns:
        dict: Structured extraction with keys:
            - methods (list[str]): Methods used in the paper.
            - key_findings (list[str]): Main findings.
            - data_type (str): observational/theoretical/computational/review
            - instruments (list[str]): Instruments or datasets used.
    """
    log = logger.bind(identifier=identifier)
    log.info("extraction_started")

    prompt = f"""You are a heliophysics research assistant. Extract structured information from this paper abstract.

Title: {title}

Abstract: {abstract}

Return ONLY a JSON object with exactly these fields:
{{
  "methods": ["list of methods or techniques used"],
  "key_findings": ["list of main findings or results"],
  "data_type": "one of: observational, theoretical, computational, review",
  "instruments": ["list of instruments, telescopes, or datasets used, empty list if none"]
}}

Return only the JSON object, no explanation, no markdown, no code blocks."""

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": _get_api_key(),
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}],
            },
        )

    response.raise_for_status()
    data = response.json()
    raw_text = data["content"][0]["text"].strip()

    result = json.loads(raw_text)
    log.info("extraction_complete", data_type=result.get("data_type"))
    return result


def _get_api_key() -> str:
    """Get the Anthropic API key from settings."""
    from app.config import settings

    return settings.anthropic_api_key
