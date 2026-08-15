#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/common.sh"

compose --profile tools run --rm migrate
compose exec -T postgres sh /docker-entrypoint-initdb.d/10-hansard-roles.sh
echo "Migration and application-role grants completed."
