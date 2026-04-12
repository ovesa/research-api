# Research agent router.
# Provides a single endpoint POST /agent/query that accepts a plain-English
# research question, chains intent parsing → paper search → extraction →
# synthesis, and returns a detailed literature synthesis with paper citations.

import json
from datetime import datetime, timezone

import httpx
import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.database import get_pool
from app.services.extraction import extract_abstract, get_extraction, save_extraction

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])


#########################################
####### Request / Response models #######
#########################################


class AgentQuery(BaseModel):
    """Incoming research question from the user."""

    question: str


class PaperCard(BaseModel):
    """Minimal paper info returned alongside the synthesis."""

    identifier: str
    title: str
    authors: list[str]
    year: str
    journal: str | None
    url: str | None
    data_type: str | None
    relevance: str | None
    central_contribution: str | None
    researcher_summary: str | None


class AgentResponse(BaseModel):
    """Full response from the research agent."""

    question: str
    synthesis: str
    papers_used: list[PaperCard]
    paper_count: int
    timestamp: str


#########################################
########## Anthropic API helper #########
#########################################


def _get_api_key() -> str:
    from app.config import settings

    return settings.anthropic_api_key


async def _call_claude(prompt: str, max_tokens: int = 2000) -> str:
    """Call the Anthropic API and return the text response.

    Args:
        prompt (str): The prompt to send to Claude.
        max_tokens (int): Maximum tokens in the response.

    Returns:
        str: Claude's text response.
    """
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": _get_api_key(),
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
    response.raise_for_status()
    data = response.json()
    return data["content"][0]["text"].strip()


#########################################
########## Step 1: Parse intent #########
#########################################


async def _parse_intent(question: str) -> dict:
    """Use Claude to extract search parameters from a plain-English question.

    Args:
        question (str): The user's research question.

    Returns:
        dict: Structured search params with keywords, filters etc.
    """
    prompt = f"""You are an advanced heliophysics research assistant helping to search a 
database of solar physics papers focused on inertial modes and Rossby waves.

A researcher has asked: "{question}"

Extract search parameters to find relevant papers in the database.
Return ONLY a JSON object with these fields:

{{
  "keywords": ["list of 2-6 specific technical keywords to search for in titles and abstracts"],
  "data_types": ["list of relevant data types: observational, theoretical, computational, review, mixed — empty list means search all"],
  "wave_types": ["specific wave or mode types relevant to the question, empty list means all"],
  "instruments": ["specific instruments if mentioned, empty list means all"],
  "date_start": "YYYY-MM if a time period is mentioned, empty string otherwise",
  "date_end": "YYYY-MM if a time period is mentioned, empty string otherwise",
  "relevance_filter": "one of: primary, secondary, peripheral, or empty string for all",
  "query_intent": "one sentence describing what the researcher is trying to understand"
}}

Return only the JSON object. No explanation."""

    raw = await _call_claude(prompt, max_tokens=500)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        clean = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)


#########################################
########## Step 2: Search papers ########
#########################################


async def _search_papers(params: dict) -> list[dict]:
    """Search the papers database using extracted intent params.

    Args:
        params (dict): Structured search params from _parse_intent.

    Returns:
        list[dict]: Matching papers with their extraction data.
    """
    pool = await get_pool()

    conditions = []
    query_params = []

    keywords = params.get("keywords", [])
    if keywords:
        keyword_conditions = []
        for keyword in keywords:
            query_params.append(f"%{keyword.lower()}%")
            query_params.append(f"%{keyword.lower()}%")
            keyword_conditions.append(
                "(LOWER(p.title) LIKE $"
                + str(len(query_params) - 1)
                + " OR LOWER(p.abstract) LIKE $"
                + str(len(query_params))
                + ")"
            )
        conditions.append("(" + " OR ".join(keyword_conditions) + ")")

    relevance = params.get("relevance_filter", "")
    if relevance:
        query_params.append(relevance)
        conditions.append(f"e.relevance_to_solar_inertial_modes = ${len(query_params)}")

    date_start = params.get("date_start", "")
    if date_start:
        query_params.append(date_start)
        conditions.append(f"p.published_date >= ${len(query_params)}")

    date_end = params.get("date_end", "")
    if date_end:
        query_params.append(date_end)
        conditions.append(f"p.published_date <= ${len(query_params)}")

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

    query = f"""
        SELECT
            p.identifier,
            p.title,
            p.authors,
            p.abstract,
            p.published_date,
            p.journal,
            p.url,
            e.data_type,
            e.relevance_to_solar_inertial_modes,
            e.central_contribution,
            e.researcher_summary,
            e.key_findings,
            e.wave_types,
            e.solar_region,
            e.azimuthal_orders,
            e.open_questions,
            e.theoretical_framework,
            e.methods,
            e.instruments,
            e.numerical_values,
            e.cycle_dependence,
            e.measured_quantities,
            e.constrained_quantities,
            e.detection_method,
            e.physical_parameters
        FROM papers p
        LEFT JOIN extractions e ON p.identifier = e.identifier
        {where_clause}
        ORDER BY p.published_date DESC
        LIMIT 20
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *query_params)

    return [dict(row) for row in rows]


#########################################
### Step 3: Ensure extractions exist ####
#########################################


async def _ensure_extractions(papers: list[dict]) -> list[dict]:
    """For any paper without an extraction, call Claude to extract it.
    Extractions are cached in Postgres so this only calls Claude for
    papers that haven't been processed yet.

    Args:
        papers (list[dict]): Papers from the database search.

    Returns:
        list[dict]: Same papers with extractions filled in.
    """
    enriched = []
    for paper in papers:
        if paper.get("central_contribution"):
            enriched.append(paper)
            continue

        if not paper.get("abstract"):
            enriched.append(paper)
            continue

        existing = await get_extraction(paper["identifier"])
        if existing:
            paper.update(existing)
            enriched.append(paper)
            continue

        try:
            result, raw_response = await extract_abstract(
                paper["identifier"],
                paper["title"],
                paper["abstract"],
            )
            await save_extraction(paper["identifier"], result, raw_response)
            paper.update(result)
        except Exception as e:
            logger.warning(
                "extraction_failed_in_agent",
                identifier=paper["identifier"],
                error=str(e),
            )

        enriched.append(paper)

    return enriched


#########################################
########### Step 4: Synthesize ##########
#########################################


async def _synthesize(question: str, papers: list[dict]) -> str:
    """Use Claude to synthesize a literature review from extracted paper data.

    Args:
        question (str): The original research question.
        papers (list[dict]): Papers with their extraction data.

    Returns:
        str: Detailed literature synthesis.
    """
    if not papers:
        return "No papers found in your database matching this question."

    # Build a structured summary of each paper for Claude
    paper_summaries = []
    for i, paper in enumerate(papers, 1):
        authors = paper.get("authors") or []
        if isinstance(authors, str):
            try:
                authors = json.loads(authors)
            except Exception:
                authors = []
        if isinstance(authors, list) and authors:
            first_author = authors[0]
            if isinstance(first_author, dict):
                author_str = first_author.get("name", "Unknown").split(",")[0]
            else:
                author_str = str(first_author).split(",")[0]
        else:
            author_str = "Unknown"

        year = (paper.get("published_date") or "")[:4] or "Unknown"

        summary_parts = [
            f"Paper {i}: {author_str} et al. ({year})",
            f"Title: {paper.get('title', 'Unknown')}",
            f"Journal: {paper.get('journal', 'Unknown')}",
            f"Data type: {paper.get('data_type', 'unknown')}",
            f"Relevance: {paper.get('relevance_to_solar_inertial_modes', 'unknown')}",
        ]

        if paper.get("central_contribution"):
            summary_parts.append(
                f"Central contribution: {paper['central_contribution']}"
            )

        if paper.get("researcher_summary"):
            summary_parts.append(f"Researcher summary: {paper['researcher_summary']}")

        if paper.get("key_findings"):
            findings = paper["key_findings"]
            if isinstance(findings, str):
                try:
                    findings = json.loads(findings)
                except Exception:
                    pass
            if isinstance(findings, list) and findings:
                finding_strs = []
                for f in findings[:5]:
                    if isinstance(f, dict):
                        finding_strs.append(
                            f"{f.get('finding', '')} [{f.get('confidence', '')}]"
                        )
                    else:
                        finding_strs.append(str(f))
                summary_parts.append(f"Key findings: {'; '.join(finding_strs)}")

        if paper.get("open_questions"):
            oq = paper["open_questions"]
            if isinstance(oq, str):
                try:
                    oq = json.loads(oq)
                except Exception:
                    pass
            if isinstance(oq, list) and oq:
                summary_parts.append(
                    f"Open questions: {'; '.join(str(q) for q in oq[:3])}"
                )

        if paper.get("wave_types"):
            wt = paper["wave_types"]
            if isinstance(wt, str):
                try:
                    wt = json.loads(wt)
                except Exception:
                    pass
            if isinstance(wt, list) and wt:
                summary_parts.append(f"Wave types: {', '.join(str(w) for w in wt)}")

        if paper.get("theoretical_framework"):
            tf = paper["theoretical_framework"]
            if isinstance(tf, str):
                try:
                    tf = json.loads(tf)
                except Exception:
                    pass
            if isinstance(tf, list) and tf:
                summary_parts.append(
                    f"Theoretical framework: {', '.join(str(t) for t in tf)}"
                )

        if paper.get("numerical_values"):
            nv = paper["numerical_values"]
            if isinstance(nv, str):
                try:
                    nv = json.loads(nv)
                except Exception:
                    pass
            if isinstance(nv, list) and nv:
                nv_strs = []
                for n in nv[:4]:
                    if isinstance(n, dict):
                        nv_strs.append(
                            f"{n.get('quantity', '')}: {n.get('value', '')} {n.get('unit', '')}"
                        )
                nv_strs = [s for s in nv_strs if s.strip(": ")]
                if nv_strs:
                    summary_parts.append(f"Numerical values: {'; '.join(nv_strs)}")

        paper_summaries.append("\n".join(summary_parts))

    papers_text = "\n\n---\n\n".join(paper_summaries)

    prompt = f"""You are Dr. HelioBot, a senior heliophysics researcher with over 30 years 
experience studying solar interior dynamics, inertial modes, and Rossby waves. You have deep 
expertise in helioseismology, MHD theory, and solar dynamics.

A researcher has asked: "{question}"

Below is structured data extracted from {len(papers)} papers in their personal literature database.
Use this data to write a detailed, researcher-level synthesis that directly answers the question.

PAPERS:
{papers_text}

Write a synthesis that:
- Directly and specifically answers the research question
- Identifies agreements and contradictions between papers
- Highlights key numerical results and measurements
- Points out what remains unknown or contested
- Suggests specific directions for future research or how to expand on this work
- Cites papers by author and year e.g. (Duvall et al. 2025)
- Is written at the level of a Nature Astronomy or ApJ paper discussion section
- Is detailed but concise. Aim for 4-6 paragraphs
- Does NOT invent results not present in the paper data above

Write the synthesis now:"""

    return await _call_claude(prompt, max_tokens=3000)


#########################################
############# Main endpoint #############
#########################################


@router.post("/query", response_model=AgentResponse)
async def query_agent(body: AgentQuery):
    """Run a research question through the full agent chain.
    Chains: intent parsing → paper search → extraction → synthesis.
    All extractions are cached so Claude is never called twice for
    the same paper.

    Args:
        body (AgentQuery): The research question.

    Returns:
        AgentResponse: Synthesis + list of papers used.

    Raises:
        HTTPException: 400 if question is empty.
        HTTPException: 500 if the chain fails.
    """
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    log = logger.bind(question=question[:80])
    log.info("agent_query_started")

    try:
        # Step 1: parse intent
        log.info("agent_step_1_parse_intent")
        params = await _parse_intent(question)
        log.info("agent_intent_parsed", keywords=params.get("keywords"))

        # Step 2: search papers
        log.info("agent_step_2_search_papers")
        papers = await _search_papers(params)
        log.info("agent_papers_found", count=len(papers))

        if not papers:
            return AgentResponse(
                question=question,
                synthesis="No papers found in your database matching this question. "
                "Try ingesting more papers or broadening your question.",
                papers_used=[],
                paper_count=0,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        # Step 3: ensure extractions exist
        log.info("agent_step_3_ensure_extractions")
        papers = await _ensure_extractions(papers)

        # Step 4: synthesize
        log.info("agent_step_4_synthesize")
        synthesis = await _synthesize(question, papers)

        # Build paper cards
        paper_cards = []
        for paper in papers:
            authors = paper.get("authors") or []
            if isinstance(authors, str):
                try:
                    authors = json.loads(authors)
                except Exception:
                    authors = []
            author_names = []
            for a in authors[:3]:
                if isinstance(a, dict):
                    author_names.append(a.get("name", ""))
                else:
                    author_names.append(str(a))
            if len(authors) > 3:
                author_names.append("et al.")

            year = (paper.get("published_date") or "")[:4] or "Unknown"

            paper_cards.append(
                PaperCard(
                    identifier=paper["identifier"],
                    title=paper.get("title") or "Unknown",
                    authors=author_names,
                    year=year,
                    journal=paper.get("journal"),
                    url=paper.get("url"),
                    data_type=paper.get("data_type"),
                    relevance=paper.get("relevance_to_solar_inertial_modes"),
                    central_contribution=paper.get("central_contribution"),
                    researcher_summary=paper.get("researcher_summary"),
                )
            )

        log.info("agent_query_complete", papers_used=len(paper_cards))

        return AgentResponse(
            question=question,
            synthesis=synthesis,
            papers_used=paper_cards,
            paper_count=len(paper_cards),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    except Exception as e:
        log.error("agent_query_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Agent error: {str(e)}")
