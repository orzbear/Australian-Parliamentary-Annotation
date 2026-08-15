#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 /absolute/path/to/backup.dump" >&2
  exit 1
fi

backup=$1
case "$backup" in
  /*) ;;
  *) echo "Backup path must be absolute" >&2; exit 1 ;;
esac

test -f "$backup"
test -s "$backup"
if [ -f "$backup.sha256" ]; then
  (cd "$(dirname "$backup")" && sha256sum --check "$(basename "$backup").sha256")
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/common.sh"
compose exec -T postgres pg_restore --list < "$backup" > /dev/null
echo "Backup catalogue is readable: $backup"
