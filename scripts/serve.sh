#!/usr/bin/env bash
# Launch the trip planner (API + web UI) at http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a   # load LLM_* (and any OSRM/Nominatim overrides) from a gitignored .env
source .venv/bin/activate
# Routing-engine reachability (ROUTING_ENGINE=valhalla default | osrm).
if [ "${ROUTING_ENGINE:-valhalla}" = "osrm" ]; then
  for pf in "foot ${OSRM_FOOT_URL:-http://localhost:5000}" "car ${OSRM_CAR_URL:-http://localhost:5001}"; do
    set -- $pf
    curl -sf -m 3 "$2/table/v1/$1/135.7588,34.9858" >/dev/null 2>&1 \
      || echo "⚠  OSRM ($1) not reachable at $2 — PROFILE=$1 bash scripts/build_osrm.sh && docker compose -f docker-compose.osrm.yml up -d"
  done
else
  VURL="${VALHALLA_URL:-http://localhost:8002}"
  curl -sf -m 3 "$VURL/status" >/dev/null 2>&1 \
    || echo "⚠  Valhalla not reachable at $VURL — run: bash scripts/build_valhalla.sh"
fi

exec uvicorn --app-dir backend app.main:app --port 8000 --reload
