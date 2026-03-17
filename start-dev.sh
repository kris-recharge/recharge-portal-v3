#!/usr/bin/env bash
# ── ReCharge Alaska v3 — dev server launcher ──────────────────────────────────
# Usage: ./start-dev.sh
# Starts FastAPI (port 8000) and Vite (port 5173) in separate Terminal tabs.
# Both servers stay alive as long as their Terminal tabs are open.

DIR="$(cd "$(dirname "$0")" && pwd)"

# ── FastAPI ───────────────────────────────────────────────────────────────────
osascript <<APPLE
tell application "Terminal"
  activate
  do script "cd \"$DIR\" && source .venv/bin/activate && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"
end tell
APPLE

# ── Vite ─────────────────────────────────────────────────────────────────────
osascript <<APPLE
tell application "Terminal"
  activate
  do script "cd \"$DIR/web\" && npm run dev"
end tell
APPLE

echo "✅  Opened FastAPI (port 8000) and Vite (port 5173) in new Terminal tabs."
echo "    → http://localhost:5173"
