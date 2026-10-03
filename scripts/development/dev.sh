#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
BACKEND_LOG_FILE="${BACKEND_LOG_FILE:-$ROOT_DIR/.logs/backend-dev.log}"
FORMA_AUTH_MODE="${FORMA_AUTH_MODE:-local}"
FORMA_DEPLOYMENT_MODE="${FORMA_DEPLOYMENT_MODE:-local}"
FORMA_DEVELOPMENT_MODE="${FORMA_DEVELOPMENT_MODE:-true}"
FORMA_DEV_MODE="${FORMA_DEV_MODE:-$FORMA_DEVELOPMENT_MODE}"
LOCAL_SECRETS_FILE="${FORMA_LOCAL_SECRETS_FILE:-$ROOT_DIR/.forma/local-secrets.env}"

# shellcheck source=scripts/development/dev-processes.sh
source "$ROOT_DIR/scripts/development/dev-processes.sh"

backend_pid=""
frontend_pid=""
cleaned_up="false"

log() {
  printf '[forma-dev] %s\n' "$*"
}

is_port_open() {
  local port="$1"
  ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}$"
}

wait_for_url() {
  local url="$1"
  local label="$2"
  local attempts="${3:-60}"
  local process_pid="${4:-}"

  for _ in $(seq 1 "$attempts"); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      log "$label is ready at $url"
      return 0
    fi
    if [ -n "$process_pid" ] && ! kill -0 "$process_pid" >/dev/null 2>&1; then
      log "$label process exited before becoming ready."
      wait "$process_pid" || true
      return 1
    fi
    sleep 1
  done

  log "$label did not become ready at $url"
  return 1
}

first_free_port() {
  local start_port="$1"
  local port="$start_port"

  while is_port_open "$port"; do
    port=$((port + 1))
    if [ "$port" -gt $((start_port + 20)) ]; then
      log "No free frontend port found from $start_port to $((start_port + 20))."
      exit 1
    fi
  done

  printf '%s' "$port"
}

cleanup() {
  if [ "$cleaned_up" = "true" ]; then
    return
  fi
  cleaned_up="true"

  log "Stopping services..."
  if [ -n "$frontend_pid" ]; then
    dev_stop_process_group "$frontend_pid" "frontend" "$DEV_FRONTEND_PID_FILE" || true
  fi
  if [ -n "$backend_pid" ]; then
    dev_stop_process_group "$backend_pid" "backend" "$DEV_BACKEND_PID_FILE" || true
  fi
  if [ -n "$frontend_pid" ] || [ -n "$backend_pid" ]; then
    wait ${frontend_pid:+"$frontend_pid"} ${backend_pid:+"$backend_pid"} >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM HUP

cd "$ROOT_DIR"

if [ -z "${FORMA_USER_SECRETS_KEY:-}" ] && [ -f "$LOCAL_SECRETS_FILE" ]; then
  while IFS='=' read -r secret_name secret_value; do
    if [ "$secret_name" = "FORMA_USER_SECRETS_KEY" ]; then
      FORMA_USER_SECRETS_KEY="$secret_value"
      break
    fi
  done < "$LOCAL_SECRETS_FILE"
fi

if [ -z "${FORMA_USER_SECRETS_KEY:-}" ]; then
  mkdir -p "$(dirname "$LOCAL_SECRETS_FILE")"
  FORMA_USER_SECRETS_KEY="$($PYTHON_BIN -c 'import secrets; print(secrets.token_urlsafe(48))')"
  printf 'FORMA_USER_SECRETS_KEY=%s\n' "$FORMA_USER_SECRETS_KEY" > "$LOCAL_SECRETS_FILE"
  chmod 600 "$LOCAL_SECRETS_FILE" 2>/dev/null || true
  log "Generated a local encryption key at $LOCAL_SECRETS_FILE"
fi

# Do not select an LLM here. The connected host agent (for example, OpenCode)
# authors the IR, while Forma compiles it deterministically. Server-side
# generation uses any explicit provider/model configuration already in the environment.
export FORMA_AUTH_MODE FORMA_DEPLOYMENT_MODE FORMA_DEVELOPMENT_MODE FORMA_DEV_MODE FORMA_USER_SECRETS_KEY

dev_cleanup_previous_session

if [ ! -x "$VENV_DIR/bin/python" ]; then
  log "Creating Python virtualenv at $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

if ! "$VENV_DIR/bin/python" -m uvicorn --version >/dev/null 2>&1; then
  log "Installing backend dependencies"
  "$VENV_DIR/bin/pip" install -r "$ROOT_DIR/apps/api/requirements.txt"
fi

if ! "$VENV_DIR/bin/python" -c 'import forma_cli' >/dev/null 2>&1; then
  log "Installing the forma-oss CLI"
  "$VENV_DIR/bin/pip" install -e "$ROOT_DIR"
fi

if [ ! -d "$ROOT_DIR/apps/web/node_modules" ]; then
  log "Installing frontend dependencies"
  (cd "$ROOT_DIR/apps/web" && npm install)
fi

if is_port_open "$BACKEND_PORT"; then
  if curl -fsS "http://$BACKEND_HOST:$BACKEND_PORT/api" >/dev/null 2>&1; then
    log "Backend already appears to be running at http://$BACKEND_HOST:$BACKEND_PORT"
    log "Backend logs are controlled by the existing backend process."
  else
    log "Port $BACKEND_PORT is already in use, but Forma did not respond there."
    exit 1
  fi
else
  mkdir -p "$(dirname "$BACKEND_LOG_FILE")"
  export BACKEND_LOG_FILE
  log "Starting backend at http://$BACKEND_HOST:$BACKEND_PORT"
  log "Backend log file: $BACKEND_LOG_FILE"
  dev_start_process_group backend_pid "$DEV_BACKEND_PID_FILE" "" \
    "$VENV_DIR/bin/python" -m uvicorn apps.api.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT"
  wait_for_url "http://$BACKEND_HOST:$BACKEND_PORT/api" "Backend" 60 "$backend_pid"
fi

FRONTEND_PORT="$(first_free_port "$FRONTEND_PORT")"
log "Starting frontend at http://$FRONTEND_HOST:$FRONTEND_PORT"
dev_start_process_group frontend_pid "$DEV_FRONTEND_PID_FILE" "" \
  bash -c 'cd "$1" && shift && exec "$@"' bash "$ROOT_DIR/apps/web" \
  npm run dev -- --hostname "$FRONTEND_HOST" --port "$FRONTEND_PORT"

wait_for_url "http://$FRONTEND_HOST:$FRONTEND_PORT/" "Frontend"

cat <<EOF

Forma is running:
  Backend:  http://$BACKEND_HOST:$BACKEND_PORT
  Frontend: http://$FRONTEND_HOST:$FRONTEND_PORT

Press Ctrl+C to stop both services.
EOF

wait
