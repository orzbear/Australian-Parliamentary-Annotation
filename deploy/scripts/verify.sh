#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/common.sh"
. "$ENV_FILE"

compose ps
compose exec -T web python -m hansard_annotator.db.cli connection-test
compose exec -T web python -m hansard_annotator.db.cli migration-status
compose exec -T web python -m hansard_annotator.product.cli verify

curl --fail --silent --show-error "https://$HANSARD_DOMAIN/health/live"
echo
curl --fail --silent --show-error "https://$HANSARD_DOMAIN/health/ready"
echo
echo "Application and database verification passed."
