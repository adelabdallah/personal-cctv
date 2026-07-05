#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r requirements.txt

if [[ ! -f .env ]]; then
  echo "Missing .env file. Copy .env.example to .env and set your password."
  exit 1
fi

HOST=$(grep -E '^HOST=' .env | cut -d= -f2- || echo "127.0.0.1")
PORT=$(grep -E '^PORT=' .env | cut -d= -f2- || echo "8000")

echo "Starting CCTV server on http://${HOST}:${PORT}"
exec uvicorn app.main:app --host "$HOST" --port "$PORT"
