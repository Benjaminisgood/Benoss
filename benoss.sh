#!/usr/bin/env bash
# benoss: manage Benoss Flask/Gunicorn service (local or server)
# Usage: benoss {start|stop|status|restart|ip|logs|check|update|init|bootstrap}
#
# Notes:
# - Keep all ports in one place (PORTS section)
# - Project path default: this script's directory (override with BENOSS_PROJECT_PATH)
# - Uses a PID file + lock to avoid double-start
# - Runtime/log dirs default to .run/ and logs/ under the project (override with BENOSS_RUNTIME_DIR/BENOSS_LOG_DIR)
#
set -Eeuo pipefail

########################################
# Paths
########################################
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_PATH="${BENOSS_PROJECT_PATH:-$SCRIPT_DIR}"
VENV_PATH="${BENOSS_VENV_PATH:-$PROJECT_PATH/.venv}"

# Runtime dirs (local-friendly defaults)
RUNTIME_DIR="${BENOSS_RUNTIME_DIR:-$PROJECT_PATH/.run}"
LOG_DIR="${BENOSS_LOG_DIR:-$PROJECT_PATH/logs}"

PID_FILE="${BENOSS_PID_FILE:-$RUNTIME_DIR/benoss.pid}"
LOCK_FILE="${BENOSS_LOCK_FILE:-$RUNTIME_DIR/benoss.lock}"
LOG_FILE="${BENOSS_LOG_FILE:-$LOG_DIR/benoss.log}"
INFO_FILE="${BENOSS_INFO_FILE:-$RUNTIME_DIR/ip.info}"

########################################
# Ports (single source of truth)
########################################
# Main HTTP port exposed by Gunicorn
APP_PORT="${BENOSS_APP_PORT:-80}"
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
# Update/backup settings
########################################
REPO_URL="${BENOSS_REPO_URL:-https://github.com/Benjaminisgood/Benoss.git}"
BACKUP_ITEMS="${BENOSS_BACKUP_ITEMS:-.env data}"
DEFAULT_BACKUP_BASE="${HOME:-$PROJECT_PATH}/.benoss-backups"
BACKUP_BASE="${BENOSS_BACKUP_BASE:-$DEFAULT_BACKUP_BASE}"

# Env/dependency automation
ENV_FILE="${BENOSS_ENV_FILE:-$PROJECT_PATH/.env}"
ENV_EXAMPLE_FILE="${BENOSS_ENV_EXAMPLE_FILE:-$PROJECT_PATH/.env.example}"
PYPROJECT_FILE="${BENOSS_PYPROJECT_FILE:-$PROJECT_PATH/pyproject.toml}"
DEPS_STAMP_FILE="${BENOSS_DEPS_STAMP_FILE:-$RUNTIME_DIR/deps.sha256}"
AUTO_CREATE_VENV="${BENOSS_AUTO_CREATE_VENV:-1}"
AUTO_INSTALL_DEPS="${BENOSS_AUTO_INSTALL_DEPS:-1}"
AUTO_SYNC_ENV="${BENOSS_AUTO_SYNC_ENV:-1}"
AUTO_INIT_DB="${BENOSS_AUTO_INIT_DB:-1}"

DAILY_ARCHIVE_DIR="${BENOSS_DAILY_ARCHIVE_DIR:-$PROJECT_PATH/data/daily-archive}"
VECTOR_STORE_DIR="${BENOSS_VECTOR_STORE_DIR:-$PROJECT_PATH/data/vector-store}"

########################################
# Helpers
########################################
die() { echo "❌ $*" >&2; exit 1; }
info() { echo "ℹ️  $*"; }
ok() { echo "✅ $*"; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"; }

prompt_yes_no() {
  local prompt="$1"
  local reply=""

  [[ -t 0 ]] || return 1
  read -r -p "$prompt [y/N] " reply || return 1
  case "$reply" in
    [Yy]|[Yy][Ee][Ss]) return 0 ;;
    *) return 1 ;;
  esac
}

is_truthy() {
  case "$1" in
    1|[Yy]|[Yy][Ee][Ss]|[Tt][Rr][Uu][Ee]|[Oo][Nn]) return 0 ;;
    *) return 1 ;;
  esac
}

compute_file_sha256() {
  local file="$1"
  local host_py

  [[ -f "$file" ]] || return 1

  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
    return 0
  fi

  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
    return 0
  fi

  host_py="$(host_python_bin)" || return 1
  "$host_py" - "$file" <<'PY'
import hashlib
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = path.read_bytes()
print(hashlib.sha256(data).hexdigest())
PY
}

current_deps_signature() {
  compute_file_sha256 "$PYPROJECT_FILE"
}

update_deps_stamp() {
  local sig
  sig="$(current_deps_signature 2>/dev/null || true)"
  [[ -n "$sig" ]] || return 0
  mkdir -p "$(dirname "$DEPS_STAMP_FILE")"
  echo "$sig" > "$DEPS_STAMP_FILE"
}

deps_stamp_outdated() {
  local current stored
  current="$(current_deps_signature 2>/dev/null || true)"
  [[ -n "$current" ]] || return 0
  stored="$(cat "$DEPS_STAMP_FILE" 2>/dev/null || true)"
  [[ "$current" != "$stored" ]]
}

sync_env_missing_keys() {
  local key line
  local -a missing_keys=()

  [[ -f "$ENV_EXAMPLE_FILE" ]] || return 0
  [[ -f "$ENV_FILE" ]] || return 0

  while IFS= read -r key; do
    [[ -n "$key" ]] || continue
    if ! grep -Eq "^[[:space:]]*#?[[:space:]]*${key}=" "$ENV_FILE"; then
      missing_keys+=("$key")
    fi
  done < <(sed -nE 's/^[[:space:]]*#?[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=.*/\1/p' "$ENV_EXAMPLE_FILE" | awk '!seen[$0]++')

  if (( ${#missing_keys[@]} == 0 )); then
    return 0
  fi

  info "Appending ${#missing_keys[@]} missing env key(s) to $ENV_FILE"
  {
    echo
    echo "# --- Auto-appended from .env.example on $(date '+%Y-%m-%d %H:%M:%S') ---"
    for key in "${missing_keys[@]}"; do
      line="$(grep -E "^[[:space:]]*#?[[:space:]]*${key}=" "$ENV_EXAMPLE_FILE" | head -n 1 || true)"
      if [[ -z "$line" ]]; then
        echo "# ${key}="
      elif [[ "$line" =~ ^[[:space:]]*# ]]; then
        echo "$line"
      else
        echo "# $line"
      fi
    done
  } >> "$ENV_FILE"
}

ensure_env_file() {
  if ! is_truthy "$AUTO_SYNC_ENV"; then
    return 0
  fi

  if [[ ! -f "$ENV_EXAMPLE_FILE" ]]; then
    info "$ENV_EXAMPLE_FILE not found; skipping env sync."
    return 0
  fi

  if [[ ! -f "$ENV_FILE" ]]; then
    cp "$ENV_EXAMPLE_FILE" "$ENV_FILE" || die "Failed to create $ENV_FILE from $ENV_EXAMPLE_FILE."
    ok "Created $ENV_FILE from $ENV_EXAMPLE_FILE"
    return 0
  fi

  sync_env_missing_keys
}

ensure_local_storage_dirs() {
  mkdir -p "$DAILY_ARCHIVE_DIR" "$VECTOR_STORE_DIR" || die "Cannot create local archive/vector directories."
}

host_python_bin() {
  if command -v python3 >/dev/null 2>&1; then
    echo "python3"
    return 0
  fi

  if command -v python >/dev/null 2>&1; then
    echo "python"
    return 0
  fi

  return 1
}

install_deps() {
  local py="$VENV_PATH/bin/python"
  info "Installing/updating Python dependencies..."
  "$py" -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 || true
  "$py" -m pip install -e "$PROJECT_PATH" || die "Dependency install failed."
  update_deps_stamp
  ok "Dependencies are up to date."
}

create_venv() {
  local host_py
  host_py="$(host_python_bin)" || die "Python not found (need python3/python to create venv)."

  info "Creating virtualenv at $VENV_PATH"
  "$host_py" -m venv "$VENV_PATH" || die "Failed to create venv at $VENV_PATH."

  info "Installing dependencies..."
  install_deps
}

ensure_venv() {
  if [[ -x "$VENV_PATH/bin/python" ]]; then
    return 0
  fi

  info "Virtualenv not found at $VENV_PATH."
  if is_truthy "$AUTO_CREATE_VENV"; then
    create_venv
    return 0
  fi

  if prompt_yes_no "Create a new virtualenv and install dependencies"; then
    create_venv
    return 0
  fi

  info "Venv not found at $VENV_PATH. Start/init cancelled (create it or set BENOSS_VENV_PATH)."
  return 1
}

ensure_deps() {
  local py="$VENV_PATH/bin/python"
  local need_install=0
  local -a missing=()
  local pkg

  for pkg in gunicorn Flask Flask-SQLAlchemy oss2 python-dotenv requests; do
    "$py" -m pip show "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
  done

  if (( ${#missing[@]} > 0 )); then
    need_install=1
    info "Missing dependencies: ${missing[*]}"
  fi

  if deps_stamp_outdated; then
    need_install=1
    info "Dependency signature changed: $PYPROJECT_FILE"
  fi

  if (( need_install == 0 )); then
    return 0
  fi

  if is_truthy "$AUTO_INSTALL_DEPS"; then
    install_deps
    return 0
  fi

  if prompt_yes_no "Install dependencies now"; then
    install_deps
    return 0
  fi

  die "Dependencies missing (run $(basename "$0") init or install deps)."
}

copy_path() {
  local src="$1"
  local dest="$2"

  if [[ -d "$src" ]]; then
    rm -rf "$dest"
    mkdir -p "$(dirname "$dest")"
    cp -R "$src" "$dest"
  else
    mkdir -p "$(dirname "$dest")"
    cp -p "$src" "$dest"
  fi
}

is_preserved_rel() {
  local rel="$1"
  shift
  local keep

  for keep in "$@"; do
    if [[ "$rel" == "$keep" || "$rel" == "$keep/"* ]]; then
      return 0
    fi
  done

  return 1
}

ensure_dirs() {
  # Create runtime/log dirs with safe permissions
  umask 027
  mkdir -p "$RUNTIME_DIR" "$LOG_DIR" || die "Cannot create runtime/log dirs. Set BENOSS_RUNTIME_DIR/BENOSS_LOG_DIR to writable paths."
  [[ -w "$RUNTIME_DIR" && -w "$LOG_DIR" ]] || die "Runtime/log dirs not writable. Set BENOSS_RUNTIME_DIR/BENOSS_LOG_DIR to writable paths."
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

acquire_lock() {
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
      die "Another benoss command is running (lock: $LOCK_FILE)"
    fi
    return 0
  fi

  # Fallback for macOS (no flock): mkdir lock with PID
  local lock_dir="${LOCK_FILE}.d"
  if mkdir "$lock_dir" 2>/dev/null; then
    echo "$$" > "$lock_dir/pid"
    return 0
  fi

  local lock_pid=""
  if [[ -f "$lock_dir/pid" ]]; then
    lock_pid="$(cat "$lock_dir/pid" 2>/dev/null || true)"
  fi

  if [[ -z "$lock_pid" ]] || ! kill -0 "$lock_pid" 2>/dev/null; then
    rm -f "$lock_dir/pid" 2>/dev/null || true
    rmdir "$lock_dir" 2>/dev/null || true
    if mkdir "$lock_dir" 2>/dev/null; then
      echo "$$" > "$lock_dir/pid"
      return 0
    fi
  fi

  die "Another benoss command is running (lock: $LOCK_FILE)"
}

release_lock() {
  if command -v flock >/dev/null 2>&1; then
    exec 9>&- || true
    return 0
  fi

  local lock_dir="${LOCK_FILE}.d"
  rm -f "$lock_dir/pid" 2>/dev/null || true
  rmdir "$lock_dir" 2>/dev/null || true
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

pids_on_port() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$APP_PORT" -sTCP:LISTEN -t 2>/dev/null | sort -u
    return 0
  fi

  if command -v ss >/dev/null 2>&1; then
    ss -ltnp "( sport = :$APP_PORT )" 2>/dev/null \
      | awk 'NR>1{print}' \
      | grep -o 'pid=[0-9]\+' \
      | cut -d= -f2 \
      | sort -u
    return 0
  fi

  return 0
}

handle_port_conflict() {
  local -a pids=()
  local pid

  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pids+=("$pid")
  done < <(pids_on_port)

  if (( ${#pids[@]} == 0 )); then
    return 1
  fi

  info "Port $APP_PORT is in use by PID(s): ${pids[*]}"
  if ! prompt_yes_no "Force terminate these process(es)"; then
    return 1
  fi

  for pid in "${pids[@]}"; do
    kill -KILL "$pid" 2>/dev/null || true
  done

  sleep 1
  port_in_use && return 1

  ok "Port $APP_PORT freed."
  return 0
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
Usage: $(basename "$0") {start|stop|status|restart|ip|logs|check|update|init|bootstrap}

Config via env vars:
  BENOSS_PROJECT_PATH=$PROJECT_PATH
  BENOSS_VENV_PATH=$VENV_PATH
  BENOSS_APP_PORT=$APP_PORT
  BENOSS_BIND_HOST=$BIND_HOST
  BENOSS_LOG_DIR=$LOG_DIR
  BENOSS_RUNTIME_DIR=$RUNTIME_DIR
  BENOSS_APP_MODULE=$APP_MODULE
  BENOSS_REPO_URL=$REPO_URL
  BENOSS_BACKUP_ITEMS=$BACKUP_ITEMS
  BENOSS_BACKUP_BASE=$BACKUP_BASE
  BENOSS_AUTO_CREATE_VENV=$AUTO_CREATE_VENV
  BENOSS_AUTO_INSTALL_DEPS=$AUTO_INSTALL_DEPS
  BENOSS_AUTO_SYNC_ENV=$AUTO_SYNC_ENV
  BENOSS_AUTO_INIT_DB=$AUTO_INIT_DB
  BENOSS_DAILY_ARCHIVE_DIR=$DAILY_ARCHIVE_DIR
  BENOSS_VECTOR_STORE_DIR=$VECTOR_STORE_DIR

Examples:
  BENOSS_APP_PORT=80 $(basename "$0") start
  $(basename "$0") init
  $(basename "$0") bootstrap
  BENOSS_BACKUP_ITEMS=".env data uploads" $(basename "$0") update
  $(basename "$0") logs
  $(basename "$0") logs --tail 200 --grep ERROR
  $(basename "$0") logs --rotate
EOF
}

########################################
# Commands
########################################
cmd_check() {
  need_cmd bash
  ensure_dirs
  [[ -d "$PROJECT_PATH" ]] || die "Project path not found: $PROJECT_PATH"
  ensure_env_file
  ensure_local_storage_dirs
  if ! ensure_venv; then
    info "Check stopped: no virtualenv available."
    return 1
  fi
  ensure_deps
  local py g
  py="$(python_bin)"
  g="$(gunicorn_bin)"

  info "Project: $PROJECT_PATH"
  info "Runtime: $RUNTIME_DIR"
  info "Python:  $py"
  info "Gunicorn:$g"
  info "Bind:    $BIND_HOST:$APP_PORT"
  info "Workers: $GUNICORN_WORKERS  Threads: $GUNICORN_THREADS"
  info "Logs:    $LOG_FILE"
  ok "Check passed."
}

cmd_update() {
  need_cmd git
  ensure_dirs
  [[ -d "$PROJECT_PATH" ]] || die "Project path not found: $PROJECT_PATH"

  acquire_lock
  trap 'release_lock' EXIT

  local was_running=0
  if is_running; then
    was_running=1
    info "Service is running; stopping for update."
    cmd_stop
  fi

  local timestamp backup_dir
  timestamp="$(date +%Y%m%d-%H%M%S)"
  backup_dir="${BACKUP_BASE%/}/$timestamp"
  mkdir -p "$backup_dir" || die "Cannot create backup dir: $backup_dir"

  local -a backup_items=()
  local item normalized
  for item in $BACKUP_ITEMS; do
    normalized="${item#./}"
    normalized="${normalized%/}"
    [[ -n "$normalized" ]] || continue
    if [[ -e "$PROJECT_PATH/$normalized" ]]; then
      backup_items+=("$normalized")
    else
      info "Skip missing path: $normalized"
    fi
  done

  if (( ${#backup_items[@]} == 0 )); then
    die "No backup items found. Set BENOSS_BACKUP_ITEMS to existing paths."
  fi

  info "Backing up: ${backup_items[*]}"
  for item in "${backup_items[@]}"; do
    copy_path "$PROJECT_PATH/$item" "$backup_dir/$item"
  done
  ok "Backup saved to $backup_dir"

  local tmp_dir
  tmp_dir="$(mktemp -d -t benoss-update.XXXXXX)"
  info "Cloning $REPO_URL"
  if ! git clone --depth 1 "$REPO_URL" "$tmp_dir"; then
    rm -rf "$tmp_dir" || true
    die "Git clone failed."
  fi

  local -a preserve_items=()
  preserve_items+=("${backup_items[@]}")

  local rel
  if [[ "$RUNTIME_DIR" == "$PROJECT_PATH/"* ]]; then
    rel="${RUNTIME_DIR#$PROJECT_PATH/}"
    rel="${rel%/}"
    preserve_items+=("$rel")
  fi
  if [[ "$LOG_DIR" == "$PROJECT_PATH/"* ]]; then
    rel="${LOG_DIR#$PROJECT_PATH/}"
    rel="${rel%/}"
    preserve_items+=("$rel")
  fi
  if [[ "$BACKUP_BASE" == "$PROJECT_PATH/"* ]]; then
    rel="${BACKUP_BASE#$PROJECT_PATH/}"
    rel="${rel%/}"
    preserve_items+=("$rel")
  fi

  if command -v rsync >/dev/null 2>&1; then
    local -a rsync_excludes=()
    local keep
    for keep in "${preserve_items[@]}"; do
      rsync_excludes+=("--exclude=/$keep")
    done
    if ! rsync -a --delete "${rsync_excludes[@]}" "$tmp_dir/" "$PROJECT_PATH/"; then
      rm -rf "$tmp_dir" || true
      die "Rsync failed."
    fi
  else
    info "rsync not found; replacing files manually."
    local entry rel_entry
    shopt -s dotglob nullglob
    for entry in "$PROJECT_PATH"/*; do
      rel_entry="${entry#$PROJECT_PATH/}"
      if is_preserved_rel "$rel_entry" "${preserve_items[@]}"; then
        continue
      fi
      rm -rf "$entry" || die "Failed to remove $entry"
    done
    shopt -u dotglob nullglob
    if ! cp -R "$tmp_dir"/. "$PROJECT_PATH"/; then
      rm -rf "$tmp_dir" || true
      die "Copy failed."
    fi
  fi

  for item in "${backup_items[@]}"; do
    copy_path "$backup_dir/$item" "$PROJECT_PATH/$item"
  done

  rm -rf "$tmp_dir" || true

  if (( was_running )); then
    info "Service was stopped for update. Run $(basename "$0") start to start again."
  fi

  ok "Update complete."
}

cmd_start() {
  ensure_dirs

  [[ -d "$PROJECT_PATH" ]] || die "Project path not found: $PROJECT_PATH"
  cd "$PROJECT_PATH"
  ensure_env_file
  ensure_local_storage_dirs

  # Basic dependency sanity
  local py g
  if ! ensure_venv; then
    info "Start stopped: virtualenv missing."
    return 1
  fi
  ensure_deps
  py="$(python_bin)"
  g="$(gunicorn_bin)"

  # Ensure single start via lock
  acquire_lock
  trap 'release_lock' EXIT

  if is_running; then
    ok "Already running (PID=$(cat "$PID_FILE"))"
    release_lock
    return 0
  fi

  if port_in_use; then
    if ! handle_port_conflict; then
      die "Port $APP_PORT is already in use. Set BENOSS_APP_PORT to another port or stop the conflicting service."
    fi
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

  release_lock
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

cmd_init() {
  ensure_dirs
  [[ -d "$PROJECT_PATH" ]] || die "Project path not found: $PROJECT_PATH"
  cd "$PROJECT_PATH"

  if ! ensure_venv; then
    info "Init stopped: virtualenv missing."
    return 1
  fi
  ensure_deps
  ensure_env_file
  ensure_local_storage_dirs

  if is_truthy "$AUTO_INIT_DB"; then
    info "Initializing database..."
    "$VENV_PATH/bin/python" -m flask --app app init-db || die "Database init failed."
    ok "Database initialized."
  elif prompt_yes_no "Initialize database now (flask init-db)"; then
    "$VENV_PATH/bin/python" -m flask --app app init-db || die "Database init failed."
    ok "Database initialized."
  fi

  ok "Init complete."
}

cmd_bootstrap() {
  cmd_init
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
  local tail_lines=200
  local mode="follow"
  local grep_pat=""

  while (( $# > 0 )); do
    case "$1" in
      --tail)
        [[ $# -ge 2 ]] || die "Missing value for --tail"
        tail_lines="$2"
        shift 2
        ;;
      --follow|-f)
        mode="follow"
        shift
        ;;
      --print)
        mode="print"
        shift
        ;;
      --grep)
        [[ $# -ge 2 ]] || die "Missing value for --grep"
        grep_pat="$2"
        shift 2
        ;;
      --clear)
        mode="clear"
        shift
        ;;
      --rotate)
        mode="rotate"
        shift
        ;;
      --path)
        mode="path"
        shift
        ;;
      --size)
        mode="size"
        shift
        ;;
      --help|-h)
        cat <<EOF
Usage: $(basename "$0") logs [options]
  --tail N        Show last N lines (default: 200)
  --follow, -f    Follow logs (default)
  --print         Print once and exit
  --grep PATTERN  Filter output by PATTERN
  --clear         Truncate log file
  --rotate        Rotate log file with timestamp
  --path          Show log file path
  --size          Show log file size
EOF
        return 0
        ;;
      *)
        die "Unknown logs option: $1"
        ;;
    esac
  done

  case "$mode" in
    path)
      echo "$LOG_FILE"
      return 0
      ;;
    size)
      if [[ -f "$LOG_FILE" ]]; then
        if command -v du >/dev/null 2>&1; then
          du -h "$LOG_FILE"
        else
          wc -c "$LOG_FILE"
        fi
      else
        echo "0 $LOG_FILE"
      fi
      return 0
      ;;
    clear)
      : > "$LOG_FILE" || die "Cannot clear log file: $LOG_FILE"
      ok "Log cleared: $LOG_FILE"
      return 0
      ;;
    rotate)
      local ts rotated
      ts="$(date +%Y%m%d-%H%M%S)"
      rotated="${LOG_FILE}.${ts}"
      if [[ -f "$LOG_FILE" ]]; then
        mv "$LOG_FILE" "$rotated" || die "Failed to rotate log to $rotated"
        : > "$LOG_FILE" || die "Cannot create log file: $LOG_FILE"
        ok "Log rotated: $rotated"
      else
        : > "$LOG_FILE" || die "Cannot create log file: $LOG_FILE"
        ok "Log created: $LOG_FILE"
      fi
      return 0
      ;;
  esac

  [[ -f "$LOG_FILE" ]] || die "Log file not found: $LOG_FILE"

  if [[ -n "$grep_pat" ]]; then
    if command -v rg >/dev/null 2>&1; then
      if [[ "$mode" == "follow" ]]; then
        tail -n "$tail_lines" -f "$LOG_FILE" | rg --line-buffered "$grep_pat"
      else
        tail -n "$tail_lines" "$LOG_FILE" | rg "$grep_pat"
      fi
    else
      if [[ "$mode" == "follow" ]]; then
        tail -n "$tail_lines" -f "$LOG_FILE" | grep -E "$grep_pat"
      else
        tail -n "$tail_lines" "$LOG_FILE" | grep -E "$grep_pat"
      fi
    fi
  else
    if [[ "$mode" == "follow" ]]; then
      tail -n "$tail_lines" -f "$LOG_FILE"
    else
      tail -n "$tail_lines" "$LOG_FILE"
    fi
  fi
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
  update)  cmd_update ;;
  init)    cmd_init ;;
  bootstrap) cmd_bootstrap ;;
  -h|--help|help|"") show_help ;;
  *) die "Unknown command: $1 (try --help)" ;;
esac
