# Heliophysics Paper API

I am a postdoc at Stanford working in solar physics, and I built this project while transitioning into industry. The problem it solves is one I ran into constantly during my research: there is no clean programmatic way to search and retrieve heliophysics papers across arXiv and journal databases. Solar physics gets buried inside stellar astrophysics categories on arXiv, and you end up manually scrolling through listings that have nothing to do with what you are looking for. So I built this API to fix that for myself, and then kept adding to it until it became something I would actually want to show in an interview.

This was also my first real backend project. I used it to learn FastAPI, async Python, Redis, PostgreSQL, and production patterns like structured logging and multi-layer caching.

---

## What it does

- Look up any heliophysics paper by DOI, arXiv ID, or NASA ADS bibcode and get normalized metadata back
- Validate that papers are actually heliophysics-related and reject everything else with a clear explanation
- Full text search across a curated collection using Postgres native search with relevance ranking
- Filter by specific keywords with exact substring matching and optional `match_all` mode
- Bulk ingest papers from arXiv, NASA ADS, or a specific date range using CLI tools
- Manually correct records with PATCH or remove them with DELETE
- Paginated and sortable list endpoint with navigation metadata baked into every response
- Rate limited lookup endpoint to protect upstream APIs from abuse
- Three-layer caching so repeated lookups are fast without hammering external APIs

---

## Architecture

```
Client
  |
  v
FastAPI (async) -- rate limited, structured logging, request tracing
  |
  |-- Redis Cache (~2ms for cached papers)
  |
  |-- PostgreSQL (permanent storage + full text search + GIN index)
  |
  └-- External APIs (fired concurrently via asyncio.gather)
        |-- CrossRef          (DOI metadata)
        |-- arXiv             (preprint metadata)
        |-- NASA ADS          (published paper metadata + citation counts)
        └-- Semantic Scholar  (citation count fallback)
```

Every request checks Redis first, then Postgres, then hits external APIs only if needed:

| Layer | Latency |
|---|---|
| Redis cache hit | ~2ms |
| Postgres hit | ~14ms |
| External API fetch | ~400ms |

That is roughly a 200x speedup from caching on repeated lookups, which shows up clearly in the request logs.

---

## Tech stack

| | |
|---|---|
| API | FastAPI |
| Database | PostgreSQL + asyncpg |
| Cache | Redis |
| HTTP | httpx (async) |
| Validation | Pydantic v2 |
| Migrations | Alembic |
| Logging | structlog |
| Rate limiting | slowapi |
| Testing | pytest + pytest-asyncio |
| Infrastructure | Docker + docker-compose |

---

## Quick start

You need Docker and Python 3.12+.

```bash
git clone https://github.com/ovesa/research-api.git
cd research-api

# start postgres and redis
docker compose up -d

# install dependencies
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# copy and fill in your environment variables
cp .env.example .env

# run migrations
alembic upgrade head

# start the api
uvicorn app.main:app --reload
```

Go to `http://localhost:8000/docs` and everything is interactive there.

You will need an API key for NASA ADS, which you can get for free at <https://ui.adsabs.harvard.edu/user/settings/token>

---

## API endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/papers/lookup` | Look up a single paper by DOI, arXiv ID, or ADS bibcode |
| `POST` | `/papers/bulk` | Look up up to 50 papers concurrently |
| `GET` | `/papers/` | List all stored papers with pagination and sorting |
| `GET` | `/papers/search` | Full text search with relevance ranking |
| `GET` | `/papers/filter` | Filter by explicit keywords with match_all support |
| `PATCH` | `/papers/{identifier}` | Partially update a stored paper |
| `DELETE` | `/papers/{identifier}` | Remove a paper from the collection |
| `POST` | `/papers/ingest/arxiv` | Ingest latest papers from heliophysics arXiv categories |
| `POST` | `/papers/ingest/ads` | Ingest papers from NASA ADS by date range and keywords |
| `POST` | `/papers/ingest/daterange` | Ingest arXiv papers submitted in a specific date range |
| `POST` | `/papers/ingest/ids` | Ingest a specific list of arXiv IDs |
| `GET` | `/papers/stats` | Collection statistics |
| `GET` | `/papers/metrics` | Cache hit/miss rates |
| `GET` | `/papers/health` | Health check |
| `GET` | `/health/live` | Liveness check |
| `GET` | `/health/ready` | Readiness check (verifies Postgres and Redis) |

---

## Usage

### Look up a paper

```bash
# by DOI
curl -X POST http://localhost:8000/papers/lookup \
  -H "Content-Type: application/json" \
  -d '{"identifier": "10.1007/s11207-021-01842-0", "identifier_type": "doi"}'

# by arXiv ID
curl -X POST http://localhost:8000/papers/lookup \
  -H "Content-Type: application/json" \
  -d '{"identifier": "2509.19847", "identifier_type": "arxiv"}'

# by ADS bibcode
curl -X POST http://localhost:8000/papers/lookup \
  -H "Content-Type: application/json" \
  -d '{"identifier": "2026ApJ...997L..22H", "identifier_type": "ads"}'
```

Identifiers are validated against regex patterns before any external API is called. A malformed DOI or arXiv ID gets rejected immediately with a clear 422 error rather than a slow failed fetch.

### Search

```bash
curl "http://localhost:8000/papers/search?q=solar+wind+magnetic+field"
curl "http://localhost:8000/papers/search?q=magnetohydrodynamic&limit=5&offset=0"
```

Uses Postgres `tsvector` with `ts_rank` scoring. Title matches rank higher than abstract matches. Stemming is handled automatically, so `magnetohydrodynamic` matches `magnetohydrodynamics`. Every response includes pagination metadata: `total_pages`, `has_next`, `has_prev`, and `next_offset`.

### Filter by keywords

```bash
# any keyword matches (default)
curl "http://localhost:8000/papers/filter?keywords=inertial+modes,rossby+waves"

# all keywords must match
curl "http://localhost:8000/papers/filter?keywords=helioseismology,solar&match_all=true"
```

Unlike search, filtering uses exact case-insensitive substring matching with no stemming or relevance ranking. Useful when you want papers that specifically contain a term rather than anything semantically related to it.

### List and sort

```bash
# sort by citation count
curl "http://localhost:8000/papers/?sort_by=citation_count&sort_order=desc&limit=10"

# sort alphabetically
curl "http://localhost:8000/papers/?sort_by=title&sort_order=asc"
```

Allowed sort fields: `fetched_at`, `published_date`, `citation_count`, `title`.

### Ingest papers

```bash
# interactive CLI (prompts for everything)
python ingest.py

# non-interactive
python ingest.py --source ads --start 2025-01 --end 2026-04
python ingest.py --source arxiv --max 50
python ingest.py --source ads --keywords "inertial modes,rossby waves"
```

### Backfill missing data

```bash
# dry run first to see what would be fixed
python backfill.py --dry-run --target all

# fix missing URLs
python backfill.py --target urls

# refresh stale citation counts
# tries Semantic Scholar first, falls back to NASA ADS
python backfill.py --target citations
```

### Deduplicate

```bash
# preview what would be merged
python deduplicate.py --dry-run

# auto-merge records sharing the same DOI, keeping the richer one
python deduplicate.py --merge
```

### Patch and delete

```bash
# fix a missing journal
curl -X PATCH "http://localhost:8000/papers/2512.16028" \
  -H "Content-Type: application/json" \
  -d '{"journal": "The Astrophysical Journal"}'

# remove a paper
curl -X DELETE "http://localhost:8000/papers/2509.19847"
```

---

## Heliophysics validation

This was the part I found most interesting to build because I actually know what the data should look like from my research background.

For **arXiv papers**: the primary category must be `astro-ph.SR` or `physics.space-ph`. A secondary tag alone does not qualify. A plasma physics paper that also carries a heliophysics tag gets rejected.

For **DOI and ADS papers**: the journal must be on a curated heliophysics whitelist, or heliophysics keywords must appear in the title or abstract. This handles papers published in broad journals like Nature or ApJ.

There is also a secondary filter that catches papers slipping through keyword matching because they mention plasma or magnetic fields but are actually about neutron stars, black holes, exoplanets, or stellar evolution. Those get rejected even if they pass the primary checks.

Rejected papers come back with a clear explanation:

```json
{
  "identifier": "10.1038/nature12373",
  "reason": "Journal 'Nature' is not on the heliophysics whitelist and no heliophysics keywords were found in the title or abstract.",
  "title": "..."
}
```

---

## Testing

```bash
pytest tests/ -v
```

52 tests covering unit tests for all domain validation functions and integration tests for every API endpoint. External APIs, the database, and Redis are all mocked so tests run in under a second with no external dependencies.

The test suite caught two real bugs during development: a missing keyword (`magnetosphere`) in the validation set, and a case-sensitivity bug where uppercase acronyms like `SDO` and `MHD` in the keyword list were never matching lowercased input text.

---

## Design decisions

**asyncpg over SQLAlchemy**: I wanted to write actual SQL rather than work through an ORM abstraction. It also made using Postgres full text search features (`tsvector`, `ts_rank`, GIN indexes) more straightforward to reason about directly.

**Postgres full text search over Elasticsearch**: Elasticsearch adds real operational complexity for a project at this scale. Postgres handles it well and keeps the stack simple.

**NASA ADS as primary source for published papers**: ADS returns richer metadata than CrossRef for astronomy papers. Abstracts are almost always present, author lists are complete, and citation counts come back directly without needing a separate Semantic Scholar call.

**Sequential arXiv category ingestion**: arXiv asks for a maximum of 4 requests per second. Firing all category searches concurrently would risk triggering rate limiting, so they run sequentially with a pause between each one.

**`identifier` as primary key**: DOIs and arXiv IDs are already globally unique. Using them directly as the primary key avoids a separate lookup step and makes idempotent inserts with `ON CONFLICT DO NOTHING` trivial.

**Caching rejections in Redis but not Postgres**: if someone submits an invalid identifier repeatedly, the rejection is cached so the external API is not called again. But rejections are not stored in Postgres because that table is meant to be a curated collection of validated papers only.

**ADS fallback for citation counts**: Semantic Scholar does not index recent preprints reliably. When it returns nothing, the backfill tool falls back to NASA ADS, which tends to have citation data even for papers a few months old.

---

## Acknowledgements

Thank you to arXiv for use of its open access interperability.
