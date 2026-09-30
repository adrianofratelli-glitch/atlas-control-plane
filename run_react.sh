#!/bin/bash
# run_react.sh — start Torre (FastAPI + React/LeafyGreen).
# Uses portfolio-reserved ports and never stops an unrelated process.
cd "$(dirname "$0")"

# Activate venv (script relied on global python/pip, which don't exist on this machine)
[ -f venv/bin/activate ] && source venv/bin/activate

# Load .env
if [ -f .env ]; then export $(grep -v '^#' .env | xargs); echo "Loaded .env"; fi

# These defaults are reserved for Torre in the workspace-wide port registry.
export API_PORT="${API_PORT:-8765}"
export WEB_PORT="${WEB_PORT:-5290}"

for port in "$API_PORT" "$WEB_PORT"; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $port is already in use; no process was stopped."
    echo "Inspect it with: lsof -nP -iTCP:$port -sTCP:LISTEN"
    exit 1
  fi
done

# Backend dependencies
if ! python -c "import fastapi, mcp, jsonschema" 2>/dev/null; then
  echo "Installing backend dependencies..."; pip install -q -r requirements.txt
fi

echo "──────────────────────────────────────────────"
echo "Backend  (API) -> http://localhost:$API_PORT"
echo "Frontend (UI)  -> http://localhost:$WEB_PORT"
echo "──────────────────────────────────────────────"

UVICORN_ARGS=(api:app --port "$API_PORT")
[ "${POV_DEV:-0}" = "1" ] && UVICORN_ARGS+=(--reload)
uvicorn "${UVICORN_ARGS[@]}" &
BACK=$!
trap 'kill "$BACK" 2>/dev/null' EXIT

cd frontend
[ -d node_modules ] || npm install --silent
if [ "${POV_DEV:-0}" != "1" ] && {
  [ ! -f dist/index.html ] ||
  [ -n "$(find src -type f -newer dist/index.html -print -quit)" ] ||
  [ package-lock.json -nt dist/index.html ] ||
  [ vite.config.js -nt dist/index.html ];
}; then
  echo "Building optimized frontend..."
  npm run build
fi
if [ "${POV_DEV:-0}" = "1" ]; then
  npm run dev -- --host 127.0.0.1 --port "$WEB_PORT" --strictPort
else
  npm run preview -- --host 127.0.0.1 --port "$WEB_PORT" --strictPort
fi
