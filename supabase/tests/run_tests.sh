#!/usr/bin/env bash
# Apply the migrations to a throwaway Postgres cluster and run the RLS tests.
# Requires a local postgres (initdb / pg_ctl / psql on PATH).
#
# Everything lives inside one mktemp directory that this script creates and
# deletes on exit. It never touches an existing cluster, and never the repo.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATIONS="$HERE/../migrations"
WORKDIR="$(mktemp -d)"
PGDATA="$WORKDIR/pgdata"
export PGHOST="$WORKDIR/socket" PGPORT="${PGPORT:-55432}" PGDATABASE=postgres

cleanup() {
  pg_ctl -D "$PGDATA" stop -m immediate >/dev/null 2>&1 || true
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

mkdir -p "$WORKDIR/socket"
initdb -D "$PGDATA" -U postgres --auth=trust >/dev/null
pg_ctl -D "$PGDATA" -o "-p $PGPORT -k '$WORKDIR/socket' -c listen_addresses=''" -w start >/dev/null

psql -U postgres -v ON_ERROR_STOP=1 -q -f "$HERE/bootstrap.sql"
for m in "$MIGRATIONS"/*.sql; do
  echo "  applying $(basename "$m")"
  psql -U postgres -v ON_ERROR_STOP=1 -q -f "$m"
done
psql -U postgres -v ON_ERROR_STOP=1 -q -f "$HERE/rls_test.sql"
