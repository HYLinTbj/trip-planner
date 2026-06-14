#!/usr/bin/env bash
# Boston transit DEMO — runs the app against the Boston/MBTA Valhalla on :8003 with
# Boston POIs, so you can try the "transit" Mode in the UI. The Kyoto/Kansai setup is
# untouched (this is just env overrides).
#
# First bring up the Boston engine (built during the transit spike; serves foot/car/
# transit from data/spike-boston):
#   docker run -d --name valhalla-boston -p 8003:8002 --cpuset-cpus=0-3 \
#     -v "$PWD/data/spike-boston/custom:/custom_files" \
#     -v "$PWD/data/spike-boston/gtfs:/gtfs_feeds" \
#     ghcr.io/gis-ops/docker-valhalla/valhalla:latest
# then: bash scripts/serve-boston.sh  ->  http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a
source .venv/bin/activate

export VALHALLA_URL="${VALHALLA_URL:-http://localhost:8003}"        # Boston engine (foot/car/transit)
export POIS_PATH="${POIS_PATH:-$PWD/data/pois.boston.json}"         # Boston demo POIs
export VALHALLA_DEPART="${VALHALLA_DEPART:-2026-06-09T10:00}"       # representative weekday (within MBTA feed window)

curl -sf -m 3 "$VALHALLA_URL/status" >/dev/null 2>&1 \
  || echo "⚠  Boston Valhalla not reachable at $VALHALLA_URL — start the container (see header)."
echo "Boston demo: engine $VALHALLA_URL | POIs $POIS_PATH | transit depart $VALHALLA_DEPART"
echo "Tip: in the sidebar set Base lat/lon to South Station (42.3519 / -71.0552) and pick Mode = transit."
exec uvicorn --app-dir backend app.main:app --port 8000 --reload