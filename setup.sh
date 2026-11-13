#!/usr/bin/env bash
# One-time setup: creates settings.env with a strong random password and the
# ./data folder owned by the unprivileged container user (UID 10001).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f settings.env ]; then
  pw="$(head -c 48 /dev/urandom | base64 | tr -d '/+=\n' | head -c 32)"
  key="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  sed -e "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${pw}|" \
      -e "s|^SECRET_KEY=.*|SECRET_KEY=${key}|" settings.env.example > settings.env
  chmod 600 settings.env
  echo "Created settings.env"
  echo "  Admin password: ${pw}"
  echo "  (store it in your password manager; you can change it in settings.env)"
else
  echo "settings.env already exists - leaving it unchanged"
fi

mkdir -p data
if [ "$(stat -c %u data)" != "10001" ]; then
  echo "Setting owner of ./data to UID 10001 (may ask for sudo)"
  if [ "$(id -u)" = "0" ]; then chown -R 10001:10001 data; else sudo chown -R 10001:10001 data; fi
fi
chmod 750 data

echo "Done. Start with: docker compose up -d --build"
