# Heliophysics Paper API

I'm a postdoc at Stanford University working in solar physics, and I built this project as part of my transition into industry. The problem this solves is one I ran into constantly during my research: there's no clean, programmatic way to search and retrieve heliophysics papers across arXiv and journal databases. While arXiv has subject fields, solar physics or heliophysics gets wrapped into stellar astrophysics. One has to manually dig through the listings. So, I created this API to fix that.

This is also my first serious backend project, built to learn the following tools: FastAPI, async Python, Redis, PostgreSQL, and production patterns like structured logging and multi-layer caching. I used Claude AI assistance during development the same way I would use Stack Overflow or documentation: as a tool to learn faster not to skip understanding things.

---

## **What it does**

- Look up any heliophysics/solar physics paper by DOI or arXiv ID and get clean, normalized metadata
- Automatically validates that papers are actually heliophysics-related and rejects everything else
- Full text search across a curated collection using Postgres native search
- Bulk ingests the latest papers from heliophysics arXiv categories automatically
- Three-layer caching so repeated lookups are fast without hammering external APIs

---

## **Architecture**

```
Client
  │
  ▼
FastAPI (async)
  │
  ├── Redis Cache (~2ms for cached papers)
  │
  ├── PostgreSQL (permanent storage + full text search)
  │
  └── External APIs (fired concurrently via asyncio.gather)
        ├── CrossRef          (DOI metadata)
        ├── arXiv             (preprint metadata)
        └── Semantic Scholar  (citation counts)
```

Every request checks Redis first, then Postgres, then hits the external APIs only if needed. In practice this means:

| Layer | Latency |
| --- | --- |
| Redis cache hit | ~2ms |
| Postgres hit | ~14ms |
| External API fetch | ~400ms |

That's a ~184x speedup from caching on repeated lookups, which is visible in the request logs.

---

## **Tech Stack**

| | |
| --- | --- |
| API | FastAPI |
| Database | PostgreSQL + asyncpg |
| Cache | Redis |
| HTTP | httpx (async) |
| Validation | Pydantic v2 |
| Migrations | Alembic |
| Logging | structlog |
| Infrastructure | Docker + docker-compose |

---

## **Quick Start**

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

# run migrations
alembic upgrade head

# start the api
uvicorn app.main:app --reload
```

Go to `http://localhost:8000/docs`; everything is interactive there.

---

## **Usage**

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
```

### Search

```bash
curl "http://localhost:8000/papers/search?q=solar+wind+magnetic+field"
curl "http://localhost:8000/papers/search?q=magnetohydrodynamic"
curl "http://localhost:8000/papers/search?q=coronal+mass+ejection"
```

Search uses Postgres `tsvector` with `ts_rank` scoring, where title matches rank higher than abstract matches, and stemming means `magnetohydrodynamic` matches `magnetohydrodynamics` automatically.

### Ingest latest papers

```bash
curl -X POST "http://localhost:8000/papers/ingest/arxiv?max_per_category=25"
```

Pulls the latest papers from `astro-ph.SR`, `physics.space-ph`, and `astro-ph.EP`, validates each one, and stores the ones that pass.

### Collection stats

```bash
curl http://localhost:8000/papers/stats
```

---

## **Heliophysics Validation**

This was the part I actually found interesting to build because I knew what the data should look like. The API rejects papers that aren't heliophysics-related before storing them.

For **arXiv papers**: the primary category must be `astro-ph.SR`. Secondary tags alone don't count because a plasma physics paper that happens to also be tagged `astro-ph.SR` gets rejected.

For **DOI papers**: the journal must be on a curated heliophysics whitelist, or heliophysics keywords must appear in the title or abstract. This handles cases where heliophysics papers get published in broad journals like Nature or ApJ.

Rejected papers come back with a clear explanation:

```json
{
  "identifier": "10.1038/nature12373",
  "reason": "Journal 'Nature' is not on the heliophysics whitelist and no heliophysics keywords were found in the title or abstract.",
  "title": "..."
}
```

---

## **Design Decisions**

A few things I made deliberate choices about:

**asyncpg over SQLAlchemy**: I wanted to write actual SQL rather than work through an ORM abstraction. It also made using Postgres full text search features (`tsvector`, `ts_rank`, GIN indexes) more straightforward.

**Postgres full text search over Elasticsearch**: Elasticsearch would add real operational complexity for a project at this scale. Postgres handles it well enough and keeps the stack simpler.

**Sequential category ingestion**: arXiv asks for a maximum of 4 requests per second. Firing all three category searches concurrently risks triggering rate limiting, so I search them sequentially with a pause in between.

**`identifier` as primary key**: DOIs and arXiv IDs are already globally unique. Using them directly as the primary key avoids a separate lookup and makes idempotent inserts with `ON CONFLICT DO NOTHING` trivial.

**Caching rejections in Redis but not Postgres**: if someone submits an invalid identifier repeatedly, I don't want to call CrossRef every time. Caching the rejection prevents that. But I don't store rejections in Postgres because that's meant to be a clean, curated collection.

---

## **Acknowledgements**

Thank you to arXiv for use of its open access interoperability.
