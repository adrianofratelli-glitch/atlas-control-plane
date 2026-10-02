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
STRESS_PID=""
FRONT_PID=""
cleanup() {
  [ -n "$STRESS_PID" ] && kill "$STRESS_PID" 2>/dev/null
  [ -n "$FRONT_PID" ] && kill "$FRONT_PID" 2>/dev/null
  kill "$BACK" 2>/dev/null
}
trap cleanup EXIT
trap 'exit 0' INT TERM

# Bounded, read-only demo load. Failure never prevents opening the UI.
if [ "${TORRE_STRESS:-1}" = "1" ]; then
  mkdir -p .assistant-state
  python -u stress_readonly.py --minutes "${STRESS_MINUTES:-6}" --workers "${STRESS_WORKERS:-6}" \
    --output .assistant-state/startup-stress.json > .assistant-state/startup-stress.log 2>&1 &
  STRESS_PID=$!
  echo "Stress somente leitura iniciado (${STRESS_MINUTES:-6} min, até ${STRESS_WORKERS:-6} workers)."
  echo "Log: .assistant-state/startup-stress.log · desativar: TORRE_STRESS=0"
fi

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
  node node_modules/vite/bin/vite.js --host 127.0.0.1 --port "$WEB_PORT" --strictPort &
  FRONT_PID=$!
else
  node node_modules/vite/bin/vite.js preview --host 127.0.0.1 --port "$WEB_PORT" --strictPort &
  FRONT_PID=$!
fi

wait "$FRONT_PID"
