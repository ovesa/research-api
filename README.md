# Heliophysics Paper API

I am a postdoc at Stanford working in solar physics, and I built this project while transitioning into industry. The problem it solves is one I ran into constantly during my research: there is no clean programmatic way to search and retrieve heliophysics papers across arXiv and journal databases. Solar physics gets buried inside stellar astrophysics categories on arXiv, and you end up manually scrolling through listings that have nothing to do with what you are looking for. So I built this API to fix that for myself, and then kept adding to it until it became a full research assistant I actually use daily.

This was also my first real backend project. I used it to learn FastAPI, async Python, Redis, PostgreSQL, and production patterns like structured logging and multi-layer caching.

---

## What it does

- Look up any heliophysics paper by DOI, arXiv ID, or NASA ADS bibcode and get normalized metadata back
- Validate that papers are actually heliophysics-related and reject everything else with a clear explanation
- Ingest papers from NASA ADS by keyword and date range with two modes: focused keyword filtering or broad journal sweep
- Full text search across a curated collection using Postgres native search with relevance ranking
- Filter by specific keywords with exact substring matching and optional `match_all` mode
- **Extract structured research metadata from paper abstracts using Claude**  (methods, key findings, instruments, wave types, theoretical frameworks, open questions, numerical values, and more)
- **Research agent UI**: ask plain-English questions about your literature collection and get a detailed researcher-level synthesis citing specific papers
- **Export to BibTeX**: fetch official ADS BibTeX entries and write a `.bib` file for LaTeX, with keyword and relevance filtering
- Manually correct records with PATCH or remove them with DELETE
- Paginated and sortable list endpoint with navigation metadata baked into every response
- Rate limited lookup endpoint to protect upstream APIs from abuse
- Three-layer caching so repeated lookups are fast without hammering external APIs

---

## Architecture

```text
Client
  |
  v
FastAPI (async) -- rate limited, structured logging, request tracing
  |
  |-- Redis Cache (~2ms for cached papers)
  |
  |-- PostgreSQL (permanent storage + full text search + extractions)
  |
  |-- Anthropic API (Claude — abstract extraction + research synthesis)
  |
  └-- External APIs (fired concurrently via asyncio.gather)
        |-- NASA ADS          (published paper metadata + BibTeX + citation counts)
        |-- arXiv             (preprint metadata)
        |-- CrossRef          (DOI metadata)
        └-- Semantic Scholar  (citation count fallback)
```

Every request checks Redis first, then Postgres, then hits external APIs only if needed:

| Layer | Latency |
| --- | --- |
| Redis cache hit | ~2ms |
| Postgres hit | ~14ms |
| External API fetch | ~400ms |

Claude abstract extractions are cached in Postgres so the same paper is never processed twice regardless of how many times the extraction endpoint or research agent is called.

---

## Tech stack

| | |
| --- | --- |
| API | FastAPI |
| Database | PostgreSQL + asyncpg |
| Cache | Redis |
| HTTP | httpx (async) |
| Validation | Pydantic v2 |
| Migrations | Alembic |
| Logging | structlog |
| Rate limiting | slowapi |
| AI | Anthropic Claude (claude-sonnet-4) |
| Infrastructure | Docker + docker-compose |

---

## Quick start

You need Docker and Python 3.12+.

```bash
git clone https://github.com/ovesa/research-api.git
cd research-api

# copy and fill in your environment variables
cp .env.example .env
# add your NASA ADS token and Anthropic API key to .env

# start everything (Docker + migrations + API)
./start.sh
```

Go to `http://localhost:8000/docs` for the interactive API docs.

Open `agent.html` in your browser for the research assistant UI.

You will need:

- A NASA ADS API key (free at <https://ui.adsabs.harvard.edu/user/settings/token>)
- An Anthropic API key (<https://console.anthropic.com> used for extraction and the research agent)

### Manual setup

```bash
docker compose up -d
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
alembic upgrade heads
uvicorn app.main:app --reload
```

---

## API endpoints

### Papers

| Method | Endpoint | Description |
| --- | --- | --- |
| `POST` | `/papers/lookup` | Look up a single paper by DOI, arXiv ID, or ADS bibcode |
| `POST` | `/papers/bulk` | Look up up to 50 papers concurrently |
| `GET` | `/papers/` | List all stored papers with pagination and sorting |
| `GET` | `/papers/search` | Full text search with relevance ranking |
| `GET` | `/papers/filter` | Filter by explicit keywords with match_all support |
| `PATCH` | `/papers/{identifier}` | Partially update a stored paper |
| `DELETE` | `/papers/{identifier}` | Remove a paper from the collection |
| `POST` | `/papers/{identifier}/extract` | Extract structured metadata from abstract using Claude |
| `POST` | `/papers/ingest/arxiv` | Ingest latest papers from heliophysics arXiv categories |
| `POST` | `/papers/ingest/ads` | Ingest from NASA ADS by date range, keywords, and mode |
| `POST` | `/papers/ingest/daterange` | Ingest arXiv papers submitted in a specific date range |
| `POST` | `/papers/ingest/ids` | Ingest a specific list of arXiv IDs |
| `GET` | `/papers/stats` | Collection statistics |
| `GET` | `/papers/metrics` | Cache hit/miss rates |
| `GET` | `/papers/health` | Health check |

### Research Agent

| Method | Endpoint | Description |
| --- | --- | --- |
| `POST` | `/agent/query` | Ask a plain-English research question, get a literature synthesis |

### Health

| Method | Endpoint | Description |
| --- | --- | --- |
| `GET` | `/health/live` | Liveness check |
| `GET` | `/health/ready` | Readiness check (verifies Postgres and Redis) |

---

## Usage

### Ingest papers

```bash
# interactive CLI (prompts for everything)
python ingest.py

# focused keyword ingestion
python ingest.py --source ads --start 2023-01 --end 2026-05 \
  --keywords "inertial modes,rossby waves,inertial waves" --max 100

# broad journal sweep — all papers from ApJ, A&A, SoPh, JGRA etc.
python ingest.py --source ads --start 2024-01 --end 2026-05 --mode broad

# from arXiv
python ingest.py --source arxiv --max 50
```

Two ingestion modes are available for ADS:

- **keyword** (default): filters by abstract keyword expression. Only papers mentioning your terms are retrieved.
- **broad**: sweeps all papers published in 14 core heliophysics journals with no keyword filter. Use this to build a complete baseline collection.

### Extract structured metadata

```bash
curl -X POST "http://localhost:8000/papers/2025ApJ...989...26D/extract"
```

Returns a rich structured extraction including:

```json
{
  "central_contribution": "...",
  "relevance_to_solar_inertial_modes": "primary",
  "data_type": "observational",
  "methods": ["time-distance helioseismology", "..."],
  "key_findings": [{"finding": "...", "type": "measurement", "confidence": "definitive"}],
  "instruments": ["SDO/HMI", "GONG"],
  "wave_types": ["high-latitude inertial modes"],
  "solar_region": ["polar region", "convection zone"],
  "azimuthal_orders": ["m=1"],
  "physical_parameters": ["differential rotation", "phase velocity"],
  "measured_quantities": ["mode power", "mode lifetime"],
  "constrained_quantities": ["differential rotation profile"],
  "theoretical_framework": [],
  "numerical_values": [{"quantity": "tracking latitude", "value": "65", "unit": "degrees"}],
  "solar_cycle_phase": "",
  "cycle_dependence": "yes",
  "open_questions": ["need for deeper understanding of internal dynamics of low-m modes"],
  "researcher_summary": "...",
}
```

Extractions are cached in Postgres. Calling the endpoint again returns the cached result instantly without calling Claude.

### Research agent

Open `agent.html` in your browser. Ask questions like:

- *What are the open questions in inertial mode research?*
- *Summarize what we know about the solar cycle dependence of Rossby waves*
- *What are the differences between observationally identified inertial modes?*
- *What theoretical frameworks have been used to model inertial modes?*
- *How could I expand on current work in this field?*

The agent chains four steps: intent parsing → paper search → extraction (cached) → Claude synthesis. Returns a detailed literature review citing papers by author and year, followed by paper cards with ADS links.

Or call the endpoint directly:

```bash
curl -X POST "http://localhost:8000/agent/query" \
  -H "Content-Type: application/json" \
  -d '{"question": "What are the open questions in inertial mode research?"}'
```

### Export to BibTeX

Fetches official BibTeX entries directly from NASA ADS:

```bash
# export everything
python export_bibtex.py --output refs.bib

# filter by keyword
python export_bibtex.py --keywords "inertial modes,rossby waves" --output inertial.bib

# filter by Claude extraction relevance
python export_bibtex.py --relevance primary --output primary.bib

# combine filters
python export_bibtex.py --keywords "inertial modes" --relevance primary \
  --start 2023-01 --output recent_primary.bib
```

### Look up a paper

```bash
# by ADS bibcode
curl -X POST http://localhost:8000/papers/lookup \
  -H "Content-Type: application/json" \
  -d '{"identifier": "2025ApJ...989...26D", "identifier_type": "ads"}'

# by arXiv ID
curl -X POST http://localhost:8000/papers/lookup \
  -H "Content-Type: application/json" \
  -d '{"identifier": "2509.19847", "identifier_type": "arxiv"}'

# by DOI
curl -X POST http://localhost:8000/papers/lookup \
  -H "Content-Type: application/json" \
  -d '{"identifier": "10.1007/s11207-021-01842-0", "identifier_type": "doi"}'
```

### Search and filter

```bash
# full text search
curl "http://localhost:8000/papers/search?q=solar+inertial+modes"

# keyword filter
curl "http://localhost:8000/papers/filter?keywords=inertial+modes,rossby+waves"

# sort by citation count
curl "http://localhost:8000/papers/?sort_by=citation_count&sort_order=desc&limit=10"
```

### Backfill and deduplicate

```bash
# dry run to preview
python backfill.py --dry-run --target all

# fix missing URLs and citations
python backfill.py --target urls
python backfill.py --target citations

# deduplicate
python deduplicate.py --dry-run
python deduplicate.py --merge
```

---

## Heliophysics validation

Papers are validated at ingestion time through several layers:

**Domain validation**: papers must contain target phrases (inertial modes, rossby waves, inertial waves etc.) and solar indicators (solar, the Sun, sunspot, helioseismology etc.) in title or abstract.

**Non-solar filter**: papers about white dwarfs, accreting systems, pre-main-sequence stars, cataclysmic variables, exoplanets, or Earth/climate science are rejected even if they mention Rossby waves or inertial modes in passing.

**Journal blocklist**: papers from journals outside the heliophysics scope (Journal of Climate, Journal of Geophysical Research Atmospheres, Atmospheric Research etc.) are rejected.

**Conference abstract filter**: AGU meeting abstracts, AMS meeting abstracts, DPS abstracts, and ADS confE/confP entries are filtered out. Full conference proceedings papers with proper volume and page numbers are allowed.

Rejected papers return a clear explanation:

```json
{
  "identifier": "2024DPS....5631003M",
  "reason": "Paper is about non-solar objects (white dwarfs, other stars, planets).",
  "title": "..."
}
```

---

## Claude abstract extraction

The extraction prompt is designed specifically for solar inertial mode research. It uses a detailed heliophysics expert persona familiar with the domain topic and extracts 30+ structured fields per paper including:

- `central_contribution`: one sentence summary of the paper's main contribution
- `relevance_to_solar_inertial_modes`: primary / secondary / peripheral
- `data_type`: observational / theoretical / computational / review / mixed
- `key_findings`: structured with type (detection, measurement, constraint, theoretical, null_result) and confidence (definitive, tentative, marginal)
- `wave_types`, `solar_region`, `azimuthal_orders`: domain-specific classification
- `measured_quantities` vs `constrained_quantities`: important scientific distinction
- `theoretical_framework`: physical models used
- `numerical_values`: structured as `{quantity, value, unit}` for queryability
- `cycle_dependence`, `solar_cycle_phase`, `solar_activity_level`
- `dispersion_relation_discussed`, `eigenfunction_computed`
- `open_questions`: explicitly unresolved questions from the paper
- `researcher_summary`: 2-3 sentence expert commentary on why the paper matters

All extractions are cached in Postgres. The research agent uses cached extractions so Claude is never called twice for the same paper.

---

## Database schema

Two main tables:

**papers**: one row per ingested paper with identifier, title, authors (JSON), abstract, published_date, journal, doi, arxiv_id, citation_count, source, url.

**extractions**: one row per Claude extraction, foreign-keyed to papers with CASCADE delete. Contains all 30+ structured fields plus the raw Claude response for debugging.

---

## Design decisions

**asyncpg over SQLAlchemy**: writing actual SQL makes Postgres-specific features like full text search straightforward to reason about directly.

**Postgres full text search over Elasticsearch**: keeps the stack simple without sacrificing search quality at this scale.

**NASA ADS as primary source**: ADS returns richer metadata than CrossRef for astronomy papers — abstracts are almost always present, author lists are complete, and citation counts come back directly.

**Extraction caching**: Claude is expensive and slow relative to a database lookup. Caching extractions in Postgres means the research agent can synthesize across many papers without calling Claude once per paper per query.

**`identifier` as primary key**: DOIs and ADS bibcodes are already globally unique. Using them directly avoids a separate lookup step and makes idempotent inserts with `ON CONFLICT DO NOTHING` trivial.

**Two-mode ADS ingestion**: keyword mode for focused collection building around specific topics; broad mode for sweeping entire journals to ensure nothing is missed.

---

## Testing

```bash
pytest tests/ -v
```

52 tests covering unit tests for all domain validation functions and integration tests for every API endpoint. External APIs, the database, and Redis are all mocked so tests run in under a second with no external dependencies.

---

## Acknowledgements

Thank you to arXiv for use of its open access interoperability, and to NASA ADS for providing a free API with BibTeX export.
