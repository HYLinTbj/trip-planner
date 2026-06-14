#!/usr/bin/env bash
# Launch the trip-planner MCP server over stdio, for Claude Desktop on Windows.
# Claude Desktop (Windows) can't exec a Linux binary directly, so its config calls:
#   wsl.exe -d Ubuntu -- /home/hylin/trip-planner/scripts/mcp-server.sh [city]
#
# With a <city> arg it points the server at THAT city's pre-built engine + POIs +
# base (same data/cities.json mapping as serve-city.sh) — so e.g. `… denver` routes
# against Valhalla :8004, not the Kyoto :8002 default. No arg = Kyoto/:8002 default.
#
# We resolve env at the repo root, then cd to backend/ (app/mcp_server.py uses
# relative imports) and exec the venv Python as a module over FastMCP's stdio.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

set -a; [ -f .env ] && . ./.env; set +a   # keys/overrides first (LLM_*, …)
CITY="${1:-}"
if [ -n "$CITY" ]; then                    # per-city engine/POIs/base, like serve-city.sh
  eval "$(.venv/bin/python - "$CITY" <<'PY'
import json, os, sys
city = sys.argv[1]
c = json.load(open("data/cities.json"))[city]
print(f'export VALHALLA_URL=http://localhost:{c["port"]}')
pois = f'data/pois.{city}.json'
if os.path.exists(pois):
    print(f'export POIS_PATH={os.path.abspath(pois)}')
b = c["base"]
print(f'export BASE_LAT={b["lat"]}')
print(f'export BASE_LON={b["lon"]}')
print(f'export BASE_NAME="{b["name"]}"')
print(f'export CITY_LABEL="{c.get("label", "")}"')
print(f'export DEFAULT_CITY={city}')
PY
)"
fi

cd backend
exec ../.venv/bin/python -m app.mcp_server
