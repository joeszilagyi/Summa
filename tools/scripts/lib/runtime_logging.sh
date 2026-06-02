#!/usr/bin/env bash
set -euo pipefail

readonly RUNTIME_LOG_DEFAULT_MAX_BYTES=10485760
readonly RUNTIME_LOG_DEFAULT_KEEP_COUNT=20
readonly RUNTIME_LOG_ORIGIN_SCAN_MAX_DEPTH=12
readonly RUNTIME_LOG_WAIT_ATTEMPTS=50
readonly RUNTIME_LOG_TIMESTAMP_FORMAT='%Y-%m-%dT%H:%M:%SZ'

# Purpose:
# - Centralize index script logging setup and teardown so callers get timestamped stdout/stderr
#   plus a single JSON-like event log record format.
# Documentation: docs/operations/logging.md
# When changing log paths, rotation, traps, or event fields, update that document.
# Expected usage:
# - source tools/scripts/lib/runtime_logging.sh in a script
# - call runtime_log_init <repo_root> <tool_name> <language> early
# - call runtime_log_install_exit_trap [optional_cleanup_fn] to ensure restoration on exit/kill signals
# Behavior note: callers are expected to use a repository-local path in <repo_root>.

runtime_detect_run_origin() {
  if [[ -n "${INDEX_RUN_ORIGIN:-}" ]]; then
    printf '%s\n' "$INDEX_RUN_ORIGIN"
    return 0
  fi

  local pid="${PPID:-}"
  local depth=0
  local comm ppid

  while [[ -n "$pid" && "$pid" =~ ^[0-9]+$ && "$pid" != "1" && $depth -lt $RUNTIME_LOG_ORIGIN_SCAN_MAX_DEPTH ]]; do
    comm="$(ps -o comm= -p "$pid" 2>/dev/null | tr -d '[:space:]' || true)"
    case "$comm" in
      cron|crond|anacron)
        printf '%s\n' "cron"
        return 0
        ;;
      systemd|systemd-run|atd)
        printf '%s\n' "scheduled"
        return 0
        ;;
    esac

    ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]' || true)"
    [[ -n "$ppid" ]] || break
    pid="$ppid"
    depth=$((depth + 1))
  done

  printf '%s\n' "manual"
}

runtime_require_non_negative_int() {
  local name="$1"
  local value="$2"

  if [[ ! "$value" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$name must be a non-negative integer: $value" >&2
    return 1
  fi
}

runtime_require_bash_identifier() {
  local label="$1"
  local value="$2"

  if [[ ! "$value" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    printf '%s\n' "$label must be a Bash identifier: $value" >&2
    return 1
  fi
}

runtime_rotate_monolithic_log() {
  local log_path="$1"
  local archive_dir="$2"
  local max_bytes="${3:-$RUNTIME_LOG_DEFAULT_MAX_BYTES}"
  local keep_count="${4:-$RUNTIME_LOG_DEFAULT_KEEP_COUNT}"

  mkdir -p -- "$archive_dir"
  [[ -f "$log_path" ]] || return 0

  local size
  size="$(wc -c < "$log_path" | tr -d '[:space:]')"
  [[ -n "$size" ]] || size=0

  if (( size < max_bytes )); then
    return 0
  fi

  # keep_count=0 means "rotate by truncating only"; do not keep tar archives in that mode.
  if (( keep_count <= 0 )); then
    : > "$log_path"
    return 0
  fi

  local idx next base tmpdir tar_src archive_tmp rc
  base="$(basename -- "$log_path")"
  tmpdir="$(mktemp -d)"
  if archive_tmp="$(mktemp "$archive_dir/${base}.0.tar.gz.tmp.XXXXXX")"; then
    :
  else
    rc=$?
    rm -rf -- "$tmpdir"
    return "$rc"
  fi
  tar_src="$tmpdir/$base"

  if cp -- "$log_path" "$tar_src"; then
    :
  else
    rc=$?
    rm -f -- "$archive_tmp"
    rm -rf -- "$tmpdir"
    return "$rc"
  fi

  if tar -C "$tmpdir" -czf "$archive_tmp" "$base"; then
    :
  else
    rc=$?
    rm -f -- "$archive_tmp"
    rm -rf -- "$tmpdir"
    return "$rc"
  fi
  rm -rf -- "$tmpdir"

  for (( idx=keep_count-1; idx>=0; idx-- )); do
    local existing="$archive_dir/${base}.${idx}.tar.gz"
    if [[ $idx -eq keep_count-1 ]]; then
      if [[ -f "$existing" ]]; then
        rm -f -- "$existing" || {
          rc=$?
          rm -f -- "$archive_tmp"
          return "$rc"
        }
      fi
    else
      next="$archive_dir/${base}.$((idx+1)).tar.gz"
      if [[ -f "$existing" ]]; then
        mv -- "$existing" "$next" || {
          rc=$?
          rm -f -- "$archive_tmp"
          return "$rc"
        }
      fi
    fi
  done

  mv -- "$archive_tmp" "$archive_dir/${base}.0.tar.gz" || {
    rc=$?
    rm -f -- "$archive_tmp"
    return "$rc"
  }
  : > "$log_path"
}

runtime_log_init() {
  local repo_root="$1"
  local tool_name="$2"
  local language="$3"

  export INDEX_REPO_ROOT="$repo_root"
  export INDEX_TOOL_NAME="$tool_name"
  export INDEX_TOOL_LANG="$language"
  export INDEX_RUN_ORIGIN="$(runtime_detect_run_origin)"
  export INDEX_ROOT_LOG="${INDEX_ROOT_LOG:-$repo_root/runtime/log-output/index-actions.log}"
  export INDEX_LOG_ARCHIVE_DIR="${INDEX_LOG_ARCHIVE_DIR:-$repo_root/runtime/backups/logs}"
  export INDEX_LOG_ROTATE_MAX_BYTES="${INDEX_LOG_ROTATE_MAX_BYTES:-$RUNTIME_LOG_DEFAULT_MAX_BYTES}"
  export INDEX_LOG_ROTATE_KEEP="${INDEX_LOG_ROTATE_KEEP:-$RUNTIME_LOG_DEFAULT_KEEP_COUNT}"
  export INDEX_LOG_ACTIVE="${INDEX_LOG_ACTIVE:-0}"
  runtime_require_non_negative_int "INDEX_LOG_ROTATE_MAX_BYTES" "$INDEX_LOG_ROTATE_MAX_BYTES"
  runtime_require_non_negative_int "INDEX_LOG_ROTATE_KEEP" "$INDEX_LOG_ROTATE_KEEP"

  mkdir -p -- "$(dirname -- "$INDEX_ROOT_LOG")"
  mkdir -p -- "$INDEX_LOG_ARCHIVE_DIR"
  touch -- "$INDEX_ROOT_LOG"
  runtime_rotate_monolithic_log \
    "$INDEX_ROOT_LOG" \
    "$INDEX_LOG_ARCHIVE_DIR" \
    "$INDEX_LOG_ROTATE_MAX_BYTES" \
    "$INDEX_LOG_ROTATE_KEEP"

  if [[ "${INDEX_LOG_ACTIVE:-0}" == "1" ]]; then
    return 0
  fi

  export INDEX_LOG_TMPDIR
  INDEX_LOG_TMPDIR="$(mktemp -d)"
  export INDEX_LOG_STDOUT_FIFO="$INDEX_LOG_TMPDIR/stdout.fifo"
  export INDEX_LOG_STDERR_FIFO="$INDEX_LOG_TMPDIR/stderr.fifo"

  mkfifo "$INDEX_LOG_STDOUT_FIFO" "$INDEX_LOG_STDERR_FIFO"

  exec {INDEX_LOG_ORIG_STDOUT_FD}>&1
  export INDEX_LOG_ORIG_STDOUT_FD
  exec {INDEX_LOG_ORIG_STDERR_FD}>&2
  export INDEX_LOG_ORIG_STDERR_FD

  (
    while IFS= read -r line || [[ -n "$line" ]]; do
      printf '%s tool=%s lang=%s origin=%s stream=stdout %s\n' \
        "$(date -u +"$RUNTIME_LOG_TIMESTAMP_FORMAT")" \
        "$tool_name" \
        "$language" \
        "$INDEX_RUN_ORIGIN" \
        "$line"
    done < "$INDEX_LOG_STDOUT_FIFO" | tee -a "$INDEX_ROOT_LOG" >&${INDEX_LOG_ORIG_STDOUT_FD}
  ) &
  export INDEX_LOG_STDOUT_PID=$!

  (
    while IFS= read -r line || [[ -n "$line" ]]; do
      printf '%s tool=%s lang=%s origin=%s stream=stderr %s\n' \
        "$(date -u +"$RUNTIME_LOG_TIMESTAMP_FORMAT")" \
        "$tool_name" \
        "$language" \
        "$INDEX_RUN_ORIGIN" \
        "$line"
    done < "$INDEX_LOG_STDERR_FIFO" | tee -a "$INDEX_ROOT_LOG" >&${INDEX_LOG_ORIG_STDERR_FD}
  ) &
  export INDEX_LOG_STDERR_PID=$!

  exec {INDEX_LOG_STDOUT_WRITER_FD}>"$INDEX_LOG_STDOUT_FIFO"
  export INDEX_LOG_STDOUT_WRITER_FD
  exec {INDEX_LOG_STDERR_WRITER_FD}>"$INDEX_LOG_STDERR_FIFO"
  export INDEX_LOG_STDERR_WRITER_FD

  exec 1>&${INDEX_LOG_STDOUT_WRITER_FD}
  exec 2>&${INDEX_LOG_STDERR_WRITER_FD}

  export INDEX_LOG_ACTIVE=1
}

runtime_wait_for_log_reader() {
  local pid="${1:-}"
  local attempt=0

  [[ -n "$pid" ]] || return 0

  while kill -0 "$pid" 2>/dev/null; do
    if [[ "$attempt" -ge $RUNTIME_LOG_WAIT_ATTEMPTS ]]; then
      kill "$pid" 2>/dev/null || true
      sleep 0.01
      kill -9 "$pid" 2>/dev/null || true
      break
    fi
    sleep 0.01
    attempt=$((attempt + 1))
  done

  wait "$pid" 2>/dev/null || true
}

runtime_log_teardown() {
  local rc="${1:-0}"

  if [[ "${INDEX_LOG_ACTIVE:-0}" != "1" ]]; then
    return 0
  fi

  if [[ -n "${INDEX_LOG_ORIG_STDOUT_FD:-}" ]]; then
    exec 1>&${INDEX_LOG_ORIG_STDOUT_FD}
  fi
  if [[ -n "${INDEX_LOG_ORIG_STDERR_FD:-}" ]]; then
    exec 2>&${INDEX_LOG_ORIG_STDERR_FD}
  fi

  if [[ -n "${INDEX_LOG_STDOUT_WRITER_FD:-}" ]]; then
    exec {INDEX_LOG_STDOUT_WRITER_FD}>&-
  fi
  if [[ -n "${INDEX_LOG_STDERR_WRITER_FD:-}" ]]; then
    exec {INDEX_LOG_STDERR_WRITER_FD}>&-
  fi

  runtime_wait_for_log_reader "${INDEX_LOG_STDOUT_PID:-}"
  runtime_wait_for_log_reader "${INDEX_LOG_STDERR_PID:-}"

  if [[ -n "${INDEX_LOG_ORIG_STDOUT_FD:-}" ]]; then
    exec {INDEX_LOG_ORIG_STDOUT_FD}>&-
  fi
  if [[ -n "${INDEX_LOG_ORIG_STDERR_FD:-}" ]]; then
    exec {INDEX_LOG_ORIG_STDERR_FD}>&-
  fi

  [[ -n "${INDEX_LOG_STDOUT_FIFO:-}" ]] && rm -f -- "$INDEX_LOG_STDOUT_FIFO"
  [[ -n "${INDEX_LOG_STDERR_FIFO:-}" ]] && rm -f -- "$INDEX_LOG_STDERR_FIFO"
  [[ -n "${INDEX_LOG_TMPDIR:-}" ]] && rm -rf -- "$INDEX_LOG_TMPDIR"

  export INDEX_LOG_ACTIVE=0
  return "$rc"
}

runtime_log_run_trap() {
  local rc="$1"
  local cleanup_fn="${2:-}"
  local fail_context="${3:-}"

  set +e +u

  if [[ -n "$cleanup_fn" ]]; then
    if declare -F "$cleanup_fn" >/dev/null 2>&1; then
      "$cleanup_fn"
    else
      printf '%s\n' "missing cleanup function for log trap: $cleanup_fn" >&2
    fi
  fi

  if [[ "$rc" -ne 0 ]]; then
    runtime_log_event FAIL "tool=${INDEX_TOOL_NAME:-unknown} exit_code=$rc${fail_context:+ $fail_context}"
  fi

  runtime_log_teardown "$rc" >/dev/null || true
  return "$rc"
}

runtime_log_run_exit_trap() {
  local rc=$?
  local cleanup_fn="${1:-}"

  runtime_log_run_trap "$rc" "$cleanup_fn"
  return "$rc"
}

runtime_log_run_signal_trap() {
  local signal_name="$1"
  local rc="$2"
  local cleanup_fn="${3:-}"

  trap - EXIT HUP INT TERM

  runtime_log_run_trap "$rc" "$cleanup_fn" "signal=$signal_name"
  exit "$rc"
}

runtime_log_install_exit_trap() {
  local cleanup_fn="${1:-}"
  local cleanup_arg=""

  if [[ -n "$cleanup_fn" ]]; then
    runtime_require_bash_identifier "cleanup function" "$cleanup_fn" || return 1
    cleanup_arg=" '$cleanup_fn'"
  fi

  trap "runtime_log_run_exit_trap$cleanup_arg" EXIT
  trap "runtime_log_run_signal_trap HUP 129$cleanup_arg" HUP
  trap "runtime_log_run_signal_trap INT 130$cleanup_arg" INT
  trap "runtime_log_run_signal_trap TERM 143$cleanup_arg" TERM
}

runtime_log_event() {
  local event="$1"
  shift || true
  local msg="${*:-}"
  printf '%s event=%s %s\n' "$(date -u +"$RUNTIME_LOG_TIMESTAMP_FORMAT")" "$event" "$msg"
}
