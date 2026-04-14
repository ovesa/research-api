#!/bin/bash
set -eou pipefail

cd ~/Documents/research-api
source venv/bin/activate

docker compose up -d

echo "Waiting for postgres to be ready..."
until docker compose exec postgres pg_isready -U researchapi -q; do
  sleep 1
done
sleep 2 
echo "Postgres is ready."

echo "Running migrations..."
DATABASE_URL=postgresql://researchapi:researchapi@localhost:5432/researchapi

echo "Starting API..."
uvicorn app.main:app --reload