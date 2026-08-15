#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/common.sh"

required='HANSARD_DOMAIN ACME_EMAIL POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD HANSARD_APP_DB_USER HANSARD_APP_DB_PASSWORD HANSARD_SESSION_SECRET HANSARD_BACKUP_DIR'
for name in $required; do
  value=$(sed -n "s/^${name}=//p" "$ENV_FILE" | tail -n 1)
  if [ -z "$value" ]; then
    echo "Missing required value: $name" >&2
    exit 1
  fi
  case "$value" in
    *YOUR_*|*GENERATE_*|*change-me*)
      echo "Placeholder remains in $name" >&2
      exit 1
      ;;
  esac
done

permissions=$(stat -c '%a' "$ENV_FILE")
if [ "$permissions" != "600" ]; then
  echo "$ENV_FILE must have permissions 600 (currently $permissions)" >&2
  exit 1
fi

domain=$(sed -n 's/^HANSARD_DOMAIN=//p' "$ENV_FILE" | tail -n 1)
resolved=$(getent ahostsv4 "$domain" | awk 'NR == 1 {print $1}')
if [ -z "$resolved" ]; then
  echo "The deployment domain does not resolve: $domain" >&2
  exit 1
fi

compose config --quiet
echo "Preflight OK: $domain resolves to $resolved and Compose configuration is valid."
