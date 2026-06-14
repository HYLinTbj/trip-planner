#!/usr/bin/env bash
# API container entrypoint: bring the DB schema up to date (idempotent — a no-op when
# already at head), then exec the server. `db` is gated healthy via compose depends_on,
# so the connection should be ready; we don't hard-fail the container on a migration
# hiccup (it would crash-loop), just log and serve.
set -e
echo ">> alembic upgrade head"
alembic upgrade head || echo "WARN: alembic upgrade failed — serving anyway"
exec "$@"
