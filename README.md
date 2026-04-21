# Heliophysics Paper API

I am a solar physics postdoc who got tired of manually scrolling through arXiv/ADS listings
full of stellar astrophysics papers looking for the handful that were actually relevant to
my research. Solar physics gets buried inside broader categories, and there is no clean
programmatic way to search across arXiv and journal databases at the same time. So I built
this API to fix that problem, and then kept going until it became a full research assistant
I use every day.

It is also my first real backend project. I used it as a deliberate way to learn FastAPI,
async Python, PostgreSQL, Redis, and the production patterns that separate a working script
from a working system.

---

## What it does

- Look up any heliophysics paper by DOI, arXiv ID, or NASA ADS bibcode and get normalized
  metadata back in a consistent format regardless of source
- Validate that papers are actually heliophysics related and reject everything else with a
  clear explanation of why
- Ingest papers from NASA ADS by keyword and date range, with two modes: focused keyword
  filtering or a broad sweep across 14 core heliophysics journals
- Full text search across the collection using Postgres native search with relevance ranking
- Filter by specific keywords with exact substring matching and optional match_all mode
- Extract structured research metadata from paper abstracts using Claude: methods, key
  findings, instruments, wave types, theoretical frameworks, open questions, data gaps,
  confidence scores, and more across 35+ fields per paper
- Ask plain English questions about the collection and get a detailed researcher level
  synthesis citing specific papers by author and year
- Explore citation relationships — find the papers your collection cites most, browse
  the reference list for any paper, and discover which foundational papers you haven't
  ingested yet
- Export the collection to BibTeX fetched directly from NASA ADS, with keyword and
  relevance filtering, ready to drop into a LaTeX document
- Three layer caching so repeated lookups are fast without hammering external APIs
- Scheduled ingestion keeps the collection fresh automatically without any manual work

The collection currently holds around 100 papers, mostly from NASA ADS, with Claude
extractions on roughly 40 of them. The research agent responds end to end in about 29
seconds for a cold query, which includes searching the collection, running any missing
extractions, and synthesizing a literature review with Claude. Queries where all papers
are already extracted are faster.

---

## Architecture

```text
Client
  |
  v
FastAPI (async) -- rate limited, structured logging, 2s SLA alerting
  |
  |-- Redis (~2ms for cached papers, shared hit/miss counters across workers)
  |
  |-- PostgreSQL (permanent storage, full text search, extraction cache, citation graph)
  |
  |-- Anthropic API (Claude Haiku for intent parsing, Claude Sonnet for synthesis)
  |
  └-- External APIs (all fired concurrently via asyncio.gather)
        |-- NASA ADS         (published paper metadata, BibTeX, citation counts, references)
        |-- arXiv            (preprint metadata)
        |-- CrossRef         (DOI metadata)
        └-- Semantic Scholar (citation count fallback)
```

Every request goes through three layers before hitting external APIs:

| Layer            | Typical latency |
|------------------|-----------------|
| Redis cache hit  | ~2ms            |
| Postgres hit     | ~14ms           |
| External API     | ~400ms          |

Claude extractions are cached in Postgres so the same paper is never processed twice,
no matter how many times the extraction endpoint or research agent is called.

Cache hit and miss counters live in Redis rather than in process memory, so they are
correct across multiple workers and survive server restarts.

---

## Tech stack

| Component      | Technology                                    |
|----------------|-----------------------------------------------|
| API            | FastAPI                                       |
| Database       | PostgreSQL + asyncpg                          |
| Cache          | Redis                                         |
| HTTP client    | httpx (async)                                 |
| Validation     | Pydantic v2                                   |
| Migrations     | Alembic                                       |
| Logging        | structlog                                     |
| Rate limiting  | slowapi                                       |
| AI             | Anthropic Claude (Haiku for parsing, Sonnet for synthesis) |
| Infrastructure | Docker + docker-compose                       |

---

## Technical decisions worth explaining

**asyncpg instead of SQLAlchemy.** Writing actual SQL made Postgres specific features
like full text search and `ON CONFLICT DO NOTHING` straightforward to reason about
directly. An ORM would have added indirection without adding anything I needed.

**Postgres full text search instead of Elasticsearch.** Keeps the stack small without
sacrificing search quality at this scale. Postgres tsvector with ts_rank gives stemming
and relevance ranking out of the box. Adding Elasticsearch would have been the right
call at much higher volume.

**NASA ADS as the primary source.** ADS returns richer metadata than CrossRef for
astronomy papers. Abstracts are almost always present, author lists are complete, and
citation counts come back in the same response. CrossRef is good for DOI lookups but
sparse on everything else.

**Identifier as primary key.** DOIs and ADS bibcodes are globally unique. Using them
directly avoids a separate surrogate key lookup and makes idempotent inserts trivial.

**Extraction caching in Postgres.** Claude is slow and expensive relative to a database
read. Caching extractions means the research agent can synthesize across many papers
without calling Claude once per paper per query. The first query for a given set of
papers is slow. Everything after that is fast.

**Parallel extraction with a semaphore.** The research agent extracts multiple papers
concurrently using asyncio.gather with a semaphore capped at five parallel Claude calls.
Sequential extraction would have made the agent unusably slow on a cold query.

**Redis backed cache metrics.** In process counters reset on restart and give wrong
numbers when running multiple workers. Moving them to Redis INCR makes the metrics
endpoint reflect reality across the full deployment.

**tool_use for structured extraction.** The extraction endpoint uses the Anthropic
tool_use API rather than asking Claude to return JSON as free text. This forces Claude
through a typed schema and eliminates JSON parsing failures. The result arrives as a
Python dict with no parsing needed.

**Haiku for intent parsing, Sonnet for synthesis.** Intent parsing is a simple
classification task — converting plain English to search parameters. Haiku handles
this reliably at roughly 5x lower cost than Sonnet. Sonnet is reserved for the
synthesis step where multi-paper reasoning and research-level writing are required.

**Prompt versioning on extractions.** Every cached extraction row stores the
prompt_version string that produced it. When the extraction prompt improves, stale
rows can be selectively re-processed without touching rows that are already good:

```sql
SELECT identifier FROM extractions WHERE prompt_version = 'v2';
```

**Directed citation graph.** The related_papers table stores directed edges between
papers — which papers cite which. Only the citing paper requires a foreign key
constraint. The cited paper may not be in the collection yet, and that is intentional:
the graph still captures the relationship and the most-cited endpoint surfaces papers
worth ingesting next.

---

## Validation pipeline

Papers go through several rejection filters at ingestion time before anything is stored.

**Target phrase check.** The paper must mention inertial modes, Rossby waves, inertial
waves, or related terms in the title or abstract.

**Solar indicator check.** The paper must include solar context. A paper about Rossby
waves in Earth's atmosphere passes the phrase check but fails here.

**Non solar object filter.** Papers about white dwarfs, neutron stars, exoplanets,
cataclysmic variables, and similar objects are rejected even if they mention the target
phrases in passing.

**Journal blocklist.** Papers from journals outside heliophysics scope are rejected.
Journal of Geophysical Research Atmospheres and similar are blocked.

**Conference abstract filter.** AGU, AMS, and DPS meeting abstracts are filtered out.
Full conference proceedings with proper volume and page numbers are allowed.

Rejected papers return a structured explanation rather than a silent failure:

```json
{
  "identifier": "2024DPS....5631003M",
  "reason": "Paper is about non-solar objects (white dwarfs, other stars, planets).",
  "title": "..."
}
```

---

## Claude extraction

Extraction uses the Anthropic tool_use API with a typed schema. Claude is forced through
the schema and cannot return prose — the result arrives as a Python dict with no JSON
parsing step. Every extraction row stores a `prompt_version` string so stale extractions
can be identified and re-processed after prompt improvements.

The extraction prompt is written for solar inertial mode research specifically. It
includes field-level hallucination guards for the highest-risk fields:

- `instruments` — only instruments explicitly named in the abstract. SDO/HMI must be
  stated, not inferred from context
- `azimuthal_orders` — only m values stated numerically. Not inferred from wave type
- `numerical_values` — only numbers explicitly stated with units. No derivation
- `confirms_previous_work` / `contradicts_previous_work` — only when the abstract
  explicitly names a prior paper or author

The schema extracts 35+ structured fields per paper:

- `central_contribution` — one sentence summary of the main result
- `relevance_to_solar_inertial_modes` — primary, secondary, or peripheral
- `data_type` — observational, theoretical, computational, review, or mixed
- `confidence` — low, medium, or high based on abstract quality and field completeness
- `key_findings` — structured with type (detection, measurement, constraint, theoretical,
  null result) and confidence (definitive, tentative, marginal)
- `wave_types`, `solar_region`, `azimuthal_orders` — domain specific classification
- `measured_quantities` versus `constrained_quantities` — an important scientific distinction
- `theoretical_framework` — physical models used
- `numerical_values` — structured as quantity, value, unit for queryability
- `cycle_dependence`, `solar_cycle_phase`, `solar_activity_level`
- `dispersion_relation_discussed`, `eigenfunction_computed`
- `open_questions` — explicitly unresolved questions raised by the paper
- `data_gaps` — limitations or missing data the authors themselves identify, distinct
  from open questions
- `researcher_summary` — two to three sentence expert commentary on why the paper matters

Example extraction:

```json
{
  "central_contribution": "...",
  "relevance_to_solar_inertial_modes": "primary",
  "data_type": "observational",
  "confidence": "high",
  "methods": ["time-distance helioseismology"],
  "key_findings": [
    {"finding": "...", "type": "measurement", "confidence": "definitive"}
  ],
  "instruments": ["SDO/HMI", "GONG"],
  "wave_types": ["high-latitude inertial modes"],
  "azimuthal_orders": ["m=1"],
  "numerical_values": [
    {"quantity": "tracking latitude", "value": "65", "unit": "degrees"}
  ],
  "open_questions": ["need for deeper understanding of internal dynamics of low-m modes"],
  "data_gaps": ["analysis limited to cycle 24 only"],
  "researcher_summary": "..."
}
```

---

## Research agent

The agent chains four steps when you ask a question. Intent parsing uses Claude Haiku
for cost efficiency. Synthesis uses Claude Sonnet for research-level reasoning.

1. **Haiku** parses the question into structured search parameters (keywords, date range,
   data type, instruments)
2. The API runs a full text search with those parameters
3. Any papers without extractions get extracted concurrently, up to five at a time.
   Postgres cache means no paper is ever extracted twice
4. **Sonnet** synthesizes a literature review from the extracted data, with explicit
   instructions to identify contradictions between papers, flag model-dependent results
   versus observationally confirmed findings, and surface what remains unknown

The synthesis prompt includes confidence and data_gaps from each paper so Claude knows
when to treat an extraction cautiously.

It returns a written summary citing papers by author and year, followed by paper cards
with ADS links.

Questions it handles well:

- What are the open questions in inertial mode research?
- Summarize what we know about the solar cycle dependence of Rossby waves
- What theoretical frameworks have been used to model inertial modes?

```bash
curl -X POST "http://localhost:8000/agent/query" \
  -H "Content-Type: application/json" \
  -d '{"question": "What are the open questions in inertial mode research?"}'
```

Or open `agent.html` in a browser for a UI.

---

## Citation graph

Every paper ingested from NASA ADS automatically has its reference list fetched and
stored as directed edges in the `related_papers` table. The citing paper is the one
being ingested. The cited papers may or may not be in the collection — the graph stores
the edge either way.

```bash
# Find the most-cited papers in your collection
# Papers with in_collection: false are worth ingesting next
curl http://localhost:8000/papers/graph/most-cited

# What does this paper cite?
curl http://localhost:8000/papers/2021A%26A...652L...6G/references

# What papers in your collection cite this one?
curl http://localhost:8000/papers/2018NatAs...2..568L/citations
```

To backfill citation edges for papers already in the collection:

```bash
python backfill.py --target citation_graph
```

---

## Scheduled ingestion

A cron job runs every Monday at 7am and pulls the last 3 days of ADS papers matching
the target keywords. Logs go to `logs/ingest_ads_<timestamp>.log`.

```bash
# run manually
./scheduled_ingest.sh ads

# check logs
ls logs/
```

---

## Quick start

You need Docker and Python 3.12+.

```bash
git clone https://github.com/ovesa/research-api.git
cd research-api
cp .env.example .env
# add your NASA ADS token and Anthropic API key to .env
./start.sh
```

Go to `http://localhost:8000/docs` for the interactive API docs.
Open `agent.html` in your browser for the research assistant UI.

You will need a NASA ADS API key (free at <https://ui.adsabs.harvard.edu/user/settings/token>)
and an Anthropic API key (<https://console.anthropic.com>).

### Manual setup

```bash
docker compose up -d
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

---

## API reference

### Papers

| Method   | Endpoint                           | Description                                          |
|----------|------------------------------------|------------------------------------------------------|
| `POST`   | `/papers/lookup`                   | Look up a single paper by DOI, arXiv ID, or bibcode  |
| `POST`   | `/papers/bulk`                     | Look up up to 50 papers concurrently                 |
| `GET`    | `/papers/`                         | List all papers with pagination and sorting          |
| `GET`    | `/papers/search`                   | Full text search with relevance ranking              |
| `GET`    | `/papers/filter`                   | Filter by keywords with match_all support            |
| `PATCH`  | `/papers/{identifier}`             | Partially update a stored paper                      |
| `DELETE` | `/papers/{identifier}`             | Remove a paper from the collection                   |
| `POST`   | `/papers/{identifier}/extract`     | Extract structured metadata from abstract            |
| `GET`    | `/papers/{identifier}/references`  | Papers this paper cites (in your collection)         |
| `GET`    | `/papers/{identifier}/citations`   | Papers in your collection that cite this one         |
| `GET`    | `/papers/graph/most-cited`         | Most-cited papers across the citation graph          |
| `POST`   | `/papers/ingest/arxiv`             | Ingest latest papers from heliophysics arXiv cats    |
| `POST`   | `/papers/ingest/ads`               | Ingest from ADS by date range, keywords, and mode    |
| `POST`   | `/papers/ingest/daterange`         | Ingest arXiv papers from a specific date range       |
| `POST`   | `/papers/ingest/ids`               | Ingest a specific list of arXiv IDs                  |
| `GET`    | `/papers/stats`                    | Collection statistics                                |
| `GET`    | `/papers/metrics`                  | Cache hit/miss rates (Redis backed)                  |
| `GET`    | `/papers/health`                   | Health check                                         |

### Agent

| Method | Endpoint        | Description                                       |
|--------|-----------------|---------------------------------------------------|
| `POST` | `/agent/query`  | Ask a research question, get a literature review  |

### Health

| Method | Endpoint         | Description                              |
|--------|------------------|------------------------------------------|
| `GET`  | `/health/live`   | Liveness check                           |
| `GET`  | `/health/ready`  | Readiness check (Postgres and Redis)     |

---

## Testing

```bash
pytest tests/ -v
```

64 tests covering unit tests for every validation function, integration tests for every
API endpoint, and agent chain tests covering all four steps of the research agent pipeline.
External APIs, the database, Redis, and the Anthropic API are all mocked so the full
suite runs in under a second with no external dependencies.

The domain validation tests are the most important ones. The filtering logic has a lot
of edge cases: papers that mention Rossby waves in passing but are really about earth
science, papers from broad journals that happen to be about the Sun, conference abstracts
that look like papers. Every rejection rule has tests.

The agent chain tests cover each step independently (i.e., intent parsing, extraction cache
hits and misses, synthesis with empty and populated paper lists) as well as a full
end-to-end integration test that mocks all four steps together.

---

## Maintenance

```bash
# backfill missing citation counts, URLs, or citation graph edges
python backfill.py --target citations
python backfill.py --target urls
python backfill.py --target citation_graph

# find and remove duplicates (papers ingested from both arXiv and ADS)
python deduplicate.py --dry-run
python deduplicate.py --merge

# extract metadata for all papers in the collection
python extract_all.py

# export to BibTeX
python export_bibtex.py --keywords "inertial modes,rossby waves" --output refs.bib
python export_bibtex.py --relevance primary --output primary.bib

# re-process extractions produced by an old prompt version
# SELECT identifier FROM extractions WHERE prompt_version = 'v2';
# then delete those rows and run extract_all.py
```

---

## Acknowledgements

Thank you to arXiv for use of its open access interoperability, and to NASA ADS.