#!/bin/sh
set -eu
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/common.sh"
. "$ENV_FILE"

mkdir -p "$HANSARD_BACKUP_DIR"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
target="$HANSARD_BACKUP_DIR/hansard-$timestamp.dump"
temporary="$target.partial"

if [ -e "$target" ] || [ -e "$temporary" ]; then
  echo "Backup target already exists; refusing to overwrite" >&2
  exit 1
fi

trap 'rm -f "$temporary"' EXIT HUP INT TERM
compose exec -T postgres pg_dump \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --format=custom \
  --compress=6 \
  --no-owner \
  --no-acl > "$temporary"

test -s "$temporary"
mv "$temporary" "$target"
sha256sum "$target" > "$target.sha256"
chmod 600 "$target" "$target.sha256"
trap - EXIT HUP INT TERM

echo "Created $target"
echo "Copy this dump and checksum to approved encrypted off-VPS storage."
