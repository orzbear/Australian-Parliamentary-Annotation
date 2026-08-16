#!/bin/sh
set -eu
umask 077

DEPLOY_USER=hansard-deploy
DEPLOY_HOME=/var/lib/hansard-deploy
RELEASE_COMMAND=/usr/local/sbin/hansard-release
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this bootstrap as root." >&2
  exit 1
fi
if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
  echo "Usage: $0 /path/to/github-actions-deploy-key.pub" >&2
  exit 1
fi

public_key=$(cat "$1")
case "$public_key" in
  ssh-ed25519\ *) ;;
  *) echo "Only an Ed25519 public key is accepted." >&2; exit 1 ;;
esac

if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  useradd --create-home --home-dir "$DEPLOY_HOME" --shell /bin/bash "$DEPLOY_USER"
fi

install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$DEPLOY_HOME/.ssh"
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$DEPLOY_HOME/incoming"
printf 'restrict %s\n' "$public_key" > "$DEPLOY_HOME/.ssh/authorized_keys"
chown "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOY_HOME/.ssh/authorized_keys"
chmod 600 "$DEPLOY_HOME/.ssh/authorized_keys"

install -m 755 "$SCRIPT_DIR/hansard-release" "$RELEASE_COMMAND"
printf '%s ALL=(root) NOPASSWD: %s\n' "$DEPLOY_USER" "$RELEASE_COMMAND" \
  > /etc/sudoers.d/hansard-deploy
chmod 440 /etc/sudoers.d/hansard-deploy
visudo -cf /etc/sudoers.d/hansard-deploy

echo "Restricted GitHub deployment identity installed."
