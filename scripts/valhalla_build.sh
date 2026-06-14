#!/usr/bin/env bash
# Build a Valhalla tileset INSIDE the official valhalla/valhalla image (3.7.x).
#
# Why this exists: the gis-ops/docker-valhalla image (which onboard_city.py uses)
# is frozen at Valhalla 3.5.1, whose tile builder core-dumps on dense-metro OSM
# `level=` (indoor) tags present in fresh extracts (e.g. the NYC tile 2/754983/0).
# 3.7.0 fixes it, but the official image has no auto-build entrypoint, so we drive
# the binaries ourselves — replicating the gis-ops flow.
#
#   docker run --rm -v <custom>:/custom_files -v <gtfs>:/gtfs_feeds \
#       -v <repo>/scripts:/scripts valhalla/valhalla \
#       bash /scripts/valhalla_build.sh [transit]
#
# All paths below are container-internal. Tiles + config land in /custom_files,
# which is bind-mounted to data/regions/<region>/custom on the host.
set -euo pipefail

CF=/custom_files
GTFS=/gtfs_feeds
CONFIG=$CF/valhalla.json
TILE_DIR=$CF/valhalla_tiles
ADMIN=$CF/admins.sqlite
TZDB=$CF/timezones.sqlite
TRANSIT_DIR=$CF/transit_tiles

# transit only when caller asked AND feeds are actually present (else ingest aborts)
have_gtfs=no
if [ "${1:-}" = "transit" ] && [ -d "$GTFS" ] && [ -n "$(ls -A "$GTFS" 2>/dev/null)" ]; then
  have_gtfs=yes
fi

echo ">> [1/6] build config (transit=$have_gtfs)"
cfg="--mjolnir-tile-dir $TILE_DIR --mjolnir-admin $ADMIN --mjolnir-timezone $TZDB"
cfg="$cfg --httpd-service-listen tcp://*:8002 --service-limits-status-allow-verbose true"
if [ "$have_gtfs" = "yes" ]; then
  cfg="$cfg --mjolnir-transit-dir $TRANSIT_DIR --mjolnir-transit-feeds-dir $GTFS"
fi
valhalla_build_config $cfg > "$CONFIG"

echo ">> [2/6] admins"
valhalla_build_admins -c "$CONFIG" $CF/*.osm.pbf

echo ">> [3/6] timezones"
valhalla_build_timezones > "$TZDB"

# Transit must be ingested AND converted to level-3 graph tiles BEFORE building the
# road tiles: convert_transit lays down standalone transit tiles, then build_tiles
# stitches them to the road network (egress/transit-connection edges). Running
# convert AFTER build_tiles persists nothing (it looks for a tile .tar we don't make).
if [ "$have_gtfs" = "yes" ]; then
  echo ">> [4/6] ingest transit feeds -> pbf tiles"
  valhalla_ingest_transit -c "$CONFIG"
  echo ">> [5/6] convert transit -> level-3 graph tiles"
  valhalla_convert_transit -c "$CONFIG"
else
  echo ">> [4/6] no transit feeds — skipping ingest"
  echo ">> [5/6] no transit feeds — skipping convert"
fi

echo ">> [6/6] build road tiles (stitches transit if present)"
valhalla_build_tiles -c "$CONFIG" $CF/*.osm.pbf

echo ">> BUILD COMPLETE ($(find "$TILE_DIR" -name '*.gph' | wc -l) tiles, $(find "$TILE_DIR/3" -name '*.gph' 2>/dev/null | wc -l) transit)"
