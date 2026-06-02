#!/usr/bin/env bash
set -euo pipefail

show_usage() {
  cat <<'EOF'
Usage: bash tools/scripts/Index-Backup.sh [--dry-run] [--help]

Options:
  --dry-run  Show planned backup target without creating any files.
  --help     Show this help text.
EOF
}

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run)
      DRY_RUN=1
      ;;
    --help|-h)
      show_usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $arg" >&2
      show_usage
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

source "$SCRIPT_DIR/lib/index_paths.sh"
source "$SCRIPT_DIR/lib/runtime_logging.sh"
runtime_log_init "$PROJECT_ROOT" "Index-Backup.sh" "sh"

SCRIPT="$(basename "$0")"
# Backups are intentionally limited to known Wi-Fi contexts to avoid accidental
# remote or guest-network snapshots.
ALLOWED_SSID_REGEX="${INDEX_BACKUP_ALLOWED_SSID_REGEX:-^(Moonbase-2|Moonbase-5)$}"
BACKUP_OUTPUT_DIR="${INDEX_BACKUP_OUTPUT_DIR:-$PROJECT_ROOT/runtime/backups}"
BACKUP_PREFIX="${BACKUP_PREFIX:-index-backup}"
TMP_ZIP=""

cleanup_tmp_zip() {
  [[ -n "${TMP_ZIP:-}" ]] && rm -f -- "$TMP_ZIP" || true
}

runtime_log_install_exit_trap cleanup_tmp_zip

if ! command -v nmcli >/dev/null 2>&1; then
  runtime_log_event FAIL "tool=$SCRIPT step=prereq missing=nmcli"
  exit 1
fi

CURRENT_SSID="$(nmcli -t -f active,ssid dev wifi 2>/dev/null | awk -F: '$1=="yes" {sub(/^yes:/, "", $0); print; exit}')"

if [[ ! "${CURRENT_SSID:-}" =~ $ALLOWED_SSID_REGEX ]]; then
  runtime_log_event SKIP "tool=$SCRIPT reason=ssid_mismatch current_ssid=${CURRENT_SSID:-none}"
  exit 0
fi

runtime_log_event CHECK "tool=$SCRIPT ssid_ok=${CURRENT_SSID:-none}"

if ! command -v zip >/dev/null 2>&1; then
  runtime_log_event FAIL "tool=$SCRIPT step=prereq missing=zip"
  exit 1
fi

if [[ ! -d "$PROJECT_ROOT" ]]; then
  runtime_log_event FAIL "tool=$SCRIPT step=project_root_check path=$PROJECT_ROOT reason=missing_dir"
  exit 1
fi

mkdir -p -- "$BACKUP_OUTPUT_DIR"

if [[ ! -w "$BACKUP_OUTPUT_DIR" ]]; then
  runtime_log_event FAIL "tool=$SCRIPT step=output_dir_check path=$BACKUP_OUTPUT_DIR reason=not_writable"
  exit 1
fi

ZIP_PATH="$(index_allocate_output_path "$BACKUP_OUTPUT_DIR" "$BACKUP_PREFIX" ".zip")" || {
  runtime_log_event FAIL "tool=$SCRIPT step=allocate_backup_path path=$BACKUP_OUTPUT_DIR"
  rm -f -- "$TMP_ZIP"
  exit 1
}
ZIP_NAME="$(basename -- "$ZIP_PATH")"
runtime_log_event CHECK "tool=$SCRIPT zip_path=$ZIP_PATH dry_run=$DRY_RUN"

if [[ "$DRY_RUN" == "1" ]]; then
  runtime_log_event SKIP "tool=$SCRIPT reason=dry_run zip_path=$ZIP_PATH"
  exit 0
fi

TMP_ZIP="$(mktemp "${TMPDIR:-/tmp}/${BACKUP_PREFIX}.XXXXXX.zip")"
rm -f -- "$TMP_ZIP"

runtime_log_event START "tool=$SCRIPT project_root=$PROJECT_ROOT zip_name=$ZIP_NAME"

if ! (
  cd "$PROJECT_ROOT" &&
  zip -rq "$TMP_ZIP" . \
    -x '.git/*' '*/.git/*' \
       'node_modules/*' '*/node_modules/*' \
       '__pycache__/*' '*/__pycache__/*' \
       '.pytest_cache/*' '*/.pytest_cache/*' \
       '.codex/*' '*/.codex/*'
); then
  runtime_log_event FAIL "tool=$SCRIPT step=zip target=$TMP_ZIP"
  exit 1
fi
runtime_log_event STEP "tool=$SCRIPT zip_created=$TMP_ZIP"

if ! index_copy_file_no_clobber "$TMP_ZIP" "$ZIP_PATH"; then
  runtime_log_event FAIL "tool=$SCRIPT step=copy target=$ZIP_PATH"
  rm -f -- "$TMP_ZIP"
  exit 1
fi
runtime_log_event STEP "tool=$SCRIPT copied_to=$ZIP_PATH"

rm -f -- "$TMP_ZIP"
runtime_log_event DONE "tool=$SCRIPT zip_name=$ZIP_NAME backup_path=$ZIP_PATH git_sync=disabled_runtime_backup"
