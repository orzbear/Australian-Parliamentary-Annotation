#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 /absolute/path/to/initial.dump" >&2
  exit 1
fi

backup=$1
case "$backup" in
  /*) ;;
  *) echo "Backup path must be absolute" >&2; exit 1 ;;
esac
test -f "$backup"
test -s "$backup"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/common.sh"
. "$ENV_FILE"

table_count=$(compose exec -T postgres psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --tuples-only --no-align \
  --command "SELECT count(*) FROM pg_tables WHERE schemaname='public'" | tr -d '[:space:]')

if [ "$table_count" != "0" ]; then
  echo "Refusing initial restore: database already has $table_count public tables" >&2
  exit 1
fi

compose exec -T postgres pg_restore \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --exit-on-error \
  --no-owner \
  --no-acl < "$backup"

compose exec -T postgres sh /docker-entrypoint-initdb.d/10-hansard-roles.sh
echo "Initial restore complete. Run migration and verification before starting the web service."
