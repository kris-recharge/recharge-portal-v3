#!/usr/bin/env bash
# ── ReCharge Alaska Portal v3 — one-command VPS deploy ────────────────────────
# Run this ON THE VPS from the repo root (e.g. /opt/rca-v3):
#
#   ./deploy.sh
#
# It replaces the manual two-part flow (rebuild API container, then separately
# rebuild the web image and copy its dist to /srv/app for host Caddy):
#   1. git pull
#   2. docker compose build   (backend + frontend; VITE_* build args are
#      substituted from .env by compose automatically)
#   3. restart the API container on the new image
#   4. extract the freshly built frontend dist to /srv/app
#
# Requires: docker compose v2, .env present, /srv/app writable (sudo if not).

set -euo pipefail
cd "$(dirname "$0")"

WEB_ROOT="${WEB_ROOT:-/srv/app}"

echo "── 1/4  Pulling latest code ──────────────────────────────────────────────"
git pull --ff-only

echo "── 2/4  Building images (API + frontend) ─────────────────────────────────"
docker compose build

echo "── 3/4  Restarting API ───────────────────────────────────────────────────"
docker compose up -d rca_api_v3

echo "── 4/4  Publishing frontend dist to ${WEB_ROOT} ──────────────────────────"
# rca_web_v3 is a build-artifact image (restart: "no"); create a stopped
# container from the new image, copy the dist out, then remove it.
docker compose rm -sf rca_web_v3 >/dev/null 2>&1 || true
docker compose create rca_web_v3
docker cp "rca_web_v3:/srv/app/." "${WEB_ROOT}/"
docker compose rm -f rca_web_v3 >/dev/null

echo ""
echo "✅ Deploy complete."
echo "   API:      $(docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | grep rca_api_v3 || echo 'check: docker compose ps')"
echo "   Frontend: published to ${WEB_ROOT}"
echo "   Smoke test: https://dashboard.rechargealaska.net/app  +  /api/health"
