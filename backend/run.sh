#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
pip install -r requirements.txt --break-system-packages --quiet 2>/dev/null || pip install -r requirements.txt --quiet
echo "Starting Smart Market Watchlist on http://localhost:8000"
uvicorn app.main:app --host 0.0.0.0 --port 8000
