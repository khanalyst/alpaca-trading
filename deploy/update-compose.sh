#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/alpaca-agent-trading}"
SECRET_FILE="${ALPACA_AGENT_SECRET_FILE:-/etc/alpaca-agent-trading/agent.env}"
BACKUP_PATH="${ALPACA_EXTERNAL_BACKUP_PATH:-}"

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
if command -v systemctl >/dev/null 2>&1 && \
   systemctl is-active --quiet alpaca-trader.service; then
  echo "alpaca-trader.service is active; refusing to start a second trader." >&2
  echo "Back up and reconcile the existing deployment, then stop its trader before continuing." >&2
  exit 3
fi

cd "$APP_DIR"
export ALPACA_AGENT_SECRET_FILE="$SECRET_FILE"

# Compose passes this value to Dockerfile's build argument and to every
# runtime service.  Never build or start from a dirty checkout, and never let
# a caller-provided declaration hide the exact commit that was checked out.
if ! git_commit="$(git rev-parse --verify 'HEAD^{commit}' 2>/dev/null)"; then
  echo "Unable to resolve the checked-out Git commit under APP_DIR=$APP_DIR" >&2
  exit 2
fi
if [ "${#git_commit}" -ne 40 ] && [ "${#git_commit}" -ne 64 ]; then
  echo "Git HEAD is not a full commit object id: $git_commit" >&2
  exit 2
fi
case "$git_commit" in
  *[!0-9a-fA-F]*)
    echo "Git HEAD is not a hexadecimal commit object id: $git_commit" >&2
    exit 2
    ;;
esac
git_commit="$(printf '%s' "$git_commit" | tr '[:upper:]' '[:lower:]')"
if ! git_status="$(git status --porcelain=v1 --untracked-files=all 2>/dev/null)"; then
  echo "Unable to verify Git checkout cleanliness under APP_DIR=$APP_DIR" >&2
  exit 2
fi
if [ -n "$git_status" ]; then
  echo "Git checkout under APP_DIR=$APP_DIR is dirty; refusing deployment." >&2
  exit 2
fi
if [ "${ALPACA_DEPLOYMENT_COMMIT+x}" = x ]; then
  declared_commit="$(printf '%s' "$ALPACA_DEPLOYMENT_COMMIT" | tr '[:upper:]' '[:lower:]')"
  if [ "$declared_commit" != "$git_commit" ]; then
    echo "ALPACA_DEPLOYMENT_COMMIT disagrees with checked-out Git HEAD." >&2
    exit 2
  fi
fi
export ALPACA_DEPLOYMENT_COMMIT="$git_commit"

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Engine with the Compose v2 plugin is required." >&2
  exit 2
fi

compose=(docker compose -f compose.yaml)
if [ -n "$BACKUP_PATH" ]; then
  export ALPACA_EXTERNAL_BACKUP_PATH="$BACKUP_PATH"
  compose+=(-f deploy/compose.external-backup.yaml)
fi

"${compose[@]}" config --quiet
"${compose[@]}" build trader
"${compose[@]}" run --rm --no-deps recorder \
  python deploy/recorder.py --out runtime/research/recorded --probe
"${compose[@]}" run --rm --no-deps trader python main.py check
"${compose[@]}" up -d --no-build --remove-orphans
"${compose[@]}" exec -T trader python main.py check
"${compose[@]}" ps

echo "Update complete. Keep the trader paper-only and confirm the session-close flatten check."
