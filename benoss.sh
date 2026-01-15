#!/usr/bin/env bash
# benoss: manage Benoss Flask/Gunicorn service on Linux servers (e.g., Alibaba Cloud ECS)
# Usage: benoss {start|stop|status|restart|ip|logs|check}
#
# Notes:
# - Keep all ports in one place (PORTS section)
# - Project path default: /ben/Benoss (override with BENOSS_PROJECT_PATH env var)
# - Uses a PID file + lock to avoid double-start
# - Writes logs to /var/log/benoss by default (override with BENOSS_LOG_DIR)
#
set -Eeuo pipefail

########################################
# Paths
########################################
PROJECT_PATH="${BENOSS_PROJECT_PATH:-/ben/Benoss}"
VENV_PATH="${BENOSS_VENV_PATH:-$PROJECT_PATH/venv}"

# Runtime dirs (server-friendly defaults)
RUNTIME_DIR="${BENOSS_RUNTIME_DIR:-/var/run/benoss}"
LOG_DIR="${BENOSS_LOG_DIR:-/var/log/benoss}"

PID_FILE="${BENOSS_PID_FILE:-$RUNTIME_DIR/benoss.pid}"
LOCK_FILE="${BENOSS_LOCK_FILE:-$RUNTIME_DIR/benoss.lock}"
LOG_FILE="${BENOSS_LOG_FILE:-$LOG_DIR/benoss.log}"
INFO_FILE="${BENOSS_INFO_FILE:-$RUNTIME_DIR/ip.info}"

########################################
# Ports (single source of truth)
########################################
# Main HTTP port exposed by Gunicorn
APP_PORT="${BENOSS_APP_PORT:-5001}"
# Bind address (0.0.0.0 for server; change to 127.0.0.1 if you only reverse-proxy locally)
BIND_HOST="${BENOSS_BIND_HOST:-0.0.0.0}"

########################################
# Gunicorn knobs (safe defaults for single-host stateful apps)
########################################
GUNICORN_WORKERS="${GUNICORN_WORKERS:-1}"
GUNICORN_THREADS="${GUNICORN_THREADS:-8}"
GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-600}"
GUNICORN_GRACEFUL_TIMEOUT="${GUNICORN_GRACEFUL_TIMEOUT:-650}"

# Optional: preload app can save memory but may break if app depends on fork-time init
GUNICORN_PRELOAD="${GUNICORN_PRELOAD:-0}"   # 1 to enable, 0 to disable

########################################
# App entrypoint
########################################
# Edit these if your module/app object differs.
APP_MODULE="${BENOSS_APP_MODULE:-app:create_app()}"

########################################
# Helpers
########################################
die() { echo "❌ $*" >&2; exit 1; }
info() { echo "ℹ️  $*"; }
ok() { echo "✅ $*"; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"; }

ensure_dirs() {
  # Create runtime/log dirs with safe permissions
  umask 027
  mkdir -p "$RUNTIME_DIR" "$LOG_DIR"
}

python_bin() {
  local py="$VENV_PATH/bin/python"
  [[ -x "$py" ]] || die "Python not found at $py (create venv under $VENV_PATH or set BENOSS_VENV_PATH)"
  echo "$py"
}

gunicorn_bin() {
  local g="$VENV_PATH/bin/gunicorn"
  [[ -x "$g" ]] || die "Gunicorn not found at $g (pip install gunicorn in venv)"
  echo "$g"
}

is_running() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null
  else
    return 1
  fi
}

port_in_use() {
  # returns 0 if something is listening on APP_PORT
  if command -v ss >/dev/null 2>&1; then
    ss -ltn "( sport = :$APP_PORT )" 2>/dev/null | awk 'NR>1{print}' | grep -q .
  elif command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$APP_PORT" -sTCP:LISTEN -nP >/dev/null 2>&1
  else
    # best-effort: try /proc (may miss)
    grep -qi ":$(printf '%04X' "$APP_PORT")" /proc/net/tcp 2>/dev/null
  fi
}

get_ips() {
  # Prints "iface: ip" lines
  if command -v ip >/dev/null 2>&1; then
    ip -o -4 addr show scope global 2>/dev/null \
      | awk '{print $2 ": " $4}' \
      | sed 's#/.*##'
  elif command -v ifconfig >/dev/null 2>&1; then
    # fallback, less reliable
    ifconfig 2>/dev/null | awk '
      /^[a-zA-Z0-9]/ {iface=$1; gsub(":", "", iface)}
      /inet / && $2 != "127.0.0.1" {print iface ": " $2}
    '
  else
    echo "unknown: (no ip/ifconfig available)"
  fi
}

show_help() {
  cat <<EOF
Usage: $(basename "$0") {start|stop|status|restart|ip|logs|check}

Config via env vars:
  BENOSS_PROJECT_PATH=$PROJECT_PATH
  BENOSS_VENV_PATH=$VENV_PATH
  BENOSS_APP_PORT=$APP_PORT
  BENOSS_BIND_HOST=$BIND_HOST
  BENOSS_LOG_DIR=$LOG_DIR
  BENOSS_RUNTIME_DIR=$RUNTIME_DIR
  BENOSS_APP_MODULE=$APP_MODULE

Examples:
  BENOSS_APP_PORT=5004 $(basename "$0") start
  $(basename "$0") logs
EOF
}

########################################
# Commands
########################################
cmd_check() {
  need_cmd bash
  ensure_dirs
  [[ -d "$PROJECT_PATH" ]] || die "Project path not found: $PROJECT_PATH"
  [[ -d "$VENV_PATH" ]] || die "Venv not found: $VENV_PATH"
  local py g
  py="$(python_bin)"
  g="$(gunicorn_bin)"

  info "Project: $PROJECT_PATH"
  info "Python:  $py"
  info "Gunicorn:$g"
  info "Bind:    $BIND_HOST:$APP_PORT"
  info "Workers: $GUNICORN_WORKERS  Threads: $GUNICORN_THREADS"
  info "Logs:    $LOG_FILE"
  ok "Check passed."
}

cmd_start() {
  ensure_dirs

  [[ -d "$PROJECT_PATH" ]] || die "Project path not found: $PROJECT_PATH"
  cd "$PROJECT_PATH"

  # Basic dependency sanity
  local py g
  py="$(python_bin)"
  g="$(gunicorn_bin)"

  # Ensure single start via lock
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    die "Another benoss command is running (lock: $LOCK_FILE)"
  fi

  if is_running; then
    ok "Already running (PID=$(cat "$PID_FILE"))"
    return 0
  fi

  if port_in_use; then
    die "Port $APP_PORT is already in use. Set BENOSS_APP_PORT to another port or stop the conflicting service."
  fi

  # Build gunicorn args
  GUNICORN_ARGS=(
    "--bind" "${BIND_HOST}:${APP_PORT}"
    "--workers" "${GUNICORN_WORKERS}"
    "--threads" "${GUNICORN_THREADS}"
    "--timeout" "${GUNICORN_TIMEOUT}"
    "--graceful-timeout" "${GUNICORN_GRACEFUL_TIMEOUT}"
    "--access-logfile" "-"            # access logs into LOG_FILE via redirect below
    "--error-logfile" "-"             # error logs into LOG_FILE via redirect below
    "--log-level" "info"
    "--capture-output"
  )

  if [[ "$GUNICORN_PRELOAD" == "1" ]]; then
    GUNICORN_ARGS+=("--preload")
  fi

  info "Starting Gunicorn…"
  nohup "$g" "${GUNICORN_ARGS[@]}" "$APP_MODULE" >>"$LOG_FILE" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_FILE"

  # Give it a moment to crash if misconfigured
  sleep 1
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "❌ Start failed. Last 80 log lines:"
    tail -n 80 "$LOG_FILE" || true
    rm -f "$PID_FILE"
    exit 1
  fi

  # Save IPs for quick display
  get_ips > "$INFO_FILE" || true

  ok "Started (PID=$pid)"
  echo "🌐 Local:   http://127.0.0.1:${APP_PORT}"
  echo "🌐 LAN/WAN:"
  while read -r line; do
    iface="${line%%:*}"
    ip="${line#*: }"
    [[ -n "${ip:-}" ]] && echo "   - $iface: http://$ip:${APP_PORT}"
  done < "$INFO_FILE"
}

cmd_stop() {
  ensure_dirs

  if ! [[ -f "$PID_FILE" ]]; then
    ok "Not running (no PID file)."
    return 0
  fi

  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -z "${pid:-}" ]]; then
    rm -f "$PID_FILE"
    ok "PID file was empty; cleaned."
    return 0
  fi

  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    ok "Process not found; cleaned stale PID file."
    return 0
  fi

  info "Stopping (PID=$pid)…"
  kill -TERM "$pid" 2>/dev/null || true

  # Wait up to graceful timeout
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
    if (( waited >= GUNICORN_GRACEFUL_TIMEOUT )); then
      echo "⚠️  Still running after ${GUNICORN_GRACEFUL_TIMEOUT}s; sending SIGKILL…"
      kill -KILL "$pid" 2>/dev/null || true
      break
    fi
  done

  rm -f "$PID_FILE"
  ok "Stopped."
}

cmd_status() {
  ensure_dirs
  if is_running; then
    local pid; pid="$(cat "$PID_FILE")"
    ok "Running (PID=$pid) on ${BIND_HOST}:${APP_PORT}"
    if port_in_use; then
      info "Port $APP_PORT is listening."
    else
      info "PID exists but port not detected (could be binding to unix socket or ss unavailable)."
    fi
  else
    echo "❌ Not running."
    if [[ -f "$PID_FILE" ]]; then
      echo "   (stale PID file: $PID_FILE)"
    fi
    return 1
  fi
}

cmd_restart() {
  cmd_stop
  cmd_start
}

cmd_ip() {
  ensure_dirs
  echo "📡 IP addresses:"
  get_ips | while read -r line; do
    iface="${line%%:*}"
    ip="${line#*: }"
    echo "   - $iface: http://$ip:${APP_PORT}"
  done
}

cmd_logs() {
  ensure_dirs
  [[ -f "$LOG_FILE" ]] || die "Log file not found: $LOG_FILE"
  tail -n 200 -f "$LOG_FILE"
}

########################################
# Main
########################################
case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  status)  cmd_status ;;
  restart) cmd_restart ;;
  ip)      cmd_ip ;;
  logs)    cmd_logs ;;
  check)   cmd_check ;;
  -h|--help|help|"") show_help ;;
  *) die "Unknown command: $1 (try --help)" ;;
esac
