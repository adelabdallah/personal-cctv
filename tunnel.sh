#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "Missing .env file. Copy .env.example to .env first."
  exit 1
fi

PORT=$(grep -E '^PORT=' .env | cut -d= -f2- || echo "8000")

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared not found. Install with: brew install cloudflared"
  exit 1
fi

echo "Starting Cloudflare quick tunnel to http://127.0.0.1:${PORT}"
echo "Make sure ./run.sh is running in another terminal."
echo ""
echo "Watch for a boxed line containing https://....trycloudflare.com"
echo "Use ONLY that URL. Do NOT open argotunnel.com addresses."
echo ""

cloudflared tunnel --url "http://127.0.0.1:${PORT}" 2>&1 | while IFS= read -r line; do
  printf '%s\n' "$line"
  if [[ "$line" =~ (https://[a-z0-9-]+\.trycloudflare\.com) ]]; then
    url="${BASH_REMATCH[1]}"
    printf '\n%s\n' '=========================================='
    printf ' OPEN THIS URL IN YOUR BROWSER:\n'
    printf ' %s\n' "$url"
    printf '%s\n\n' '=========================================='
  fi
done
