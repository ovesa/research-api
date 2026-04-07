#!/bin/bash
cd ~/Documents/research-api
source venv/bin/activate
docker compose up -d
echo "Waiting for services to be ready..."
sleep 3
uvicorn app.main:app --reload
