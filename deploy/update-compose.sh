#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/okx-agent-crypto}"
SECRET_FILE="${OKX_AGENT_SECRET_FILE:-/etc/okx-agent-crypto/agent.env}"
BACKUP_PATH="${OKX_EXTERNAL_BACKUP_PATH:-}"

if [ "$(uname -s)" != "Linux" ]; then
  echo "Run this helper on the Ubuntu VM, not on the Mac." >&2
  exit 2
fi
if [ ! -f "$APP_DIR/compose.yaml" ]; then
  echo "No compose.yaml under APP_DIR=$APP_DIR" >&2
  exit 2
fi
if [ ! -f "$SECRET_FILE" ]; then
  echo "Missing Compose secret file: $SECRET_FILE" >&2
  exit 2
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Engine with the Compose v2 plugin is required." >&2
  exit 2
fi
if command -v systemctl >/dev/null 2>&1 && \
   systemctl is-active --quiet okx-trader.service; then
  echo "okx-trader.service is active; refusing to start a second trader." >&2
  echo "Complete the one-time migration in SETUP.md first." >&2
  exit 3
fi

cd "$APP_DIR"
export OKX_AGENT_SECRET_FILE="$SECRET_FILE"
compose=(docker compose -f compose.yaml)
if [ -n "$BACKUP_PATH" ]; then
  export OKX_EXTERNAL_BACKUP_PATH="$BACKUP_PATH"
  compose+=(-f deploy/compose.external-backup.yaml)
fi

"${compose[@]}" config --quiet
"${compose[@]}" build trader
"${compose[@]}" run --rm --no-deps trader python main.py check
"${compose[@]}" up -d --no-build --remove-orphans
"${compose[@]}" exec -T trader python main.py check
"${compose[@]}" ps

echo "Update complete. If status is PAUSED, explicitly run main.py resume."
