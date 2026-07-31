#!/usr/bin/env bash
# Run a LOCAL dev backend for testing the OAuth provider flows end to end,
# without touching the production aganeti-api service (port 8000, cloud URLs).
#
# Why the URL overrides matter: /auth/{provider}/connect builds its redirect_uri
# from BACKEND_URL and the callback bounces the browser to FRONTEND_URL. With the
# production values, consent finishes on papayaoyster.com instead of your dev UI.
#
#   BACKEND_URL  -> where the provider sends the user back  (must be registered
#                   in Azure / Google console EXACTLY as it appears here)
#   FRONTEND_URL -> where the backend sends the user after storing the tokens
#
# Azure only accepts http:// redirect URIs for localhost, so browse through an
# SSH tunnel rather than the LAN IP:
#   ssh -L 3000:localhost:3000 -L 8001:localhost:8001 matrix@<this-host>
#
# Usage:  ./scripts/dev_backend.sh [port]
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${1:-8001}"

export BACKEND_URL="http://localhost:${PORT}"
export FRONTEND_URL="http://localhost:3000"
# No inbox polling / Telegram bot in dev — the production instance already does
# that, and two pollers on one mailbox double the Graph calls and the triage LLM.
export RUN_BACKGROUND="${RUN_BACKGROUND:-false}"

# Real Supabase login is required by default. To bypass it for local testing run
#   DEV_AUTH_BYPASS=true ./scripts/dev_backend.sh
# and set VITE_DEV_AUTH_BYPASS=true in frontend/.env.development.local. It is
# honoured ONLY for loopback requests, and deliberately lives here rather than in
# .env so the systemd unit — which reads .env — can never inherit it.
export DEV_AUTH_BYPASS="${DEV_AUTH_BYPASS:-false}"
export DEV_AUTH_USER="${DEV_AUTH_USER:-user_1}"

echo "dev backend  : http://localhost:${PORT}"
echo "callback URIs: ${BACKEND_URL}/auth/microsoft/callback"
echo "               ${BACKEND_URL}/auth/google/callback"
echo "returns to   : ${FRONTEND_URL}"
if [ "${DEV_AUTH_BYPASS}" = "true" ]; then
  echo "auth         : BYPASSED (loopback only) as ${DEV_AUTH_USER}"
fi
echo

# --reload defaults to watching the whole working directory, which here means
# .venv/, node_modules/, qdrant_data/ and memory/ — tens of thousands of inotify
# watches, enough to starve vite ("ENOSPC: System limit for number of file
# watchers reached"). Watch only the Python source we actually edit.
exec .venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port "${PORT}" --reload \
  --reload-dir backend --reload-dir config --reload-dir integrations \
  --reload-dir reports --reload-dir tasks --reload-dir scheduler
