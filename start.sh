#!/bin/bash
set -eou pipefail

cd ~/Documents/research-api
source venv/bin/activate

docker compose up -d

echo "Waiting for postgres to be ready..."
until docker compose exec postgres pg_isready -U researchapi -q; do
  sleep 1
done
echo "Postgres is ready."

echo "Running migrations..."
alembic upgrade head

echo "Starting API..."
uvicorn app.main:app --reload