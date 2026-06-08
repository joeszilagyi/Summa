#!/usr/bin/env bash
set -euo pipefail

readonly SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly LLM_RUNNER_LIB="$SELF_DIR/llm_runner.sh"

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

runtime_log_event() {
  :
}

usage() {
  cat <<'EOF_USAGE'
Usage:
  llm_runner_bridge.sh run --prompt-file <path> --tmp-dir <path> --output-file <path> --phase <phase> [--engine <engine>] [--tool-name <name>] [--stamped-output-file <path> --stamp-place <place> --stamp-facet <facet> --stamp-phase <phase>]
  llm_runner_bridge.sh stamp --file <path> --place <place> --facet <facet> --phase <phase> [--engine <engine>]

This bridge exposes llm_runner.sh shell functions as stable subprocess commands
for Python callers without creating a second engine abstraction.
EOF_USAGE
}

[[ -r "$LLM_RUNNER_LIB" ]] || fail "missing llm_runner library: $LLM_RUNNER_LIB"
# shellcheck disable=SC1091
source "$LLM_RUNNER_LIB"

subcommand="${1:-}"
[[ -n "$subcommand" ]] || {
  usage
  exit 1
}
shift || true

engine=""
tool_name="llm_runner_bridge.sh"

case "$subcommand" in
  run)
    prompt_file=""
    tmp_dir=""
    output_file=""
    phase=""
    stamped_output_file=""
    stamp_place=""
    stamp_facet=""
    stamp_phase=""
    while [[ $# -gt 0 ]]; do
      case "${1-}" in
        --prompt-file)
          prompt_file="${2-}"
          shift 2
          ;;
        --tmp-dir)
          tmp_dir="${2-}"
          shift 2
          ;;
        --output-file)
          output_file="${2-}"
          shift 2
          ;;
        --phase)
          phase="${2-}"
          shift 2
          ;;
        --engine)
          engine="${2-}"
          shift 2
          ;;
        --tool-name)
          tool_name="${2-}"
          shift 2
          ;;
        --stamped-output-file)
          stamped_output_file="${2-}"
          shift 2
          ;;
        --stamp-place)
          stamp_place="${2-}"
          shift 2
          ;;
        --stamp-facet)
          stamp_facet="${2-}"
          shift 2
          ;;
        --stamp-phase)
          stamp_phase="${2-}"
          shift 2
          ;;
        -h|--help)
          usage
          exit 0
          ;;
        *)
          fail "unknown run option: ${1-}"
          ;;
      esac
    done
    [[ -n "$prompt_file" ]] || fail "--prompt-file is required"
    [[ -r "$prompt_file" ]] || fail "prompt file is not readable: $prompt_file"
    [[ -n "$tmp_dir" ]] || fail "--tmp-dir is required"
    [[ -n "$output_file" ]] || fail "--output-file is required"
    [[ -n "$phase" ]] || fail "--phase is required"
    if [[ -n "$stamped_output_file" ]]; then
      [[ -n "$stamp_place" ]] || fail "--stamp-place is required with --stamped-output-file"
      [[ -n "$stamp_facet" ]] || fail "--stamp-facet is required with --stamped-output-file"
      [[ -n "$stamp_phase" ]] || fail "--stamp-phase is required with --stamped-output-file"
    fi
    [[ -z "$engine" ]] || llm_runner_set_engine "$engine"
    llm_runner_init
    prompt_text="$(<"$prompt_file")"
    llm_runner_run_to_file "$tmp_dir" "$prompt_text" "$output_file" "$phase" "$tool_name"
    if [[ -n "$stamped_output_file" ]]; then
      if [[ "$stamped_output_file" != "$output_file" ]]; then
        cp -- "$output_file" "$stamped_output_file"
      fi
      llm_runner_stamp_output "$stamped_output_file" "$stamp_place" "$stamp_facet" "$stamp_phase"
    fi
    ;;
  stamp)
    file_path=""
    place=""
    facet=""
    phase=""
    while [[ $# -gt 0 ]]; do
      case "${1-}" in
        --file)
          file_path="${2-}"
          shift 2
          ;;
        --place)
          place="${2-}"
          shift 2
          ;;
        --facet)
          facet="${2-}"
          shift 2
          ;;
        --phase)
          phase="${2-}"
          shift 2
          ;;
        --engine)
          engine="${2-}"
          shift 2
          ;;
        -h|--help)
          usage
          exit 0
          ;;
        *)
          fail "unknown stamp option: ${1-}"
          ;;
      esac
    done
    [[ -n "$file_path" ]] || fail "--file is required"
    [[ -n "$place" ]] || fail "--place is required"
    [[ -n "$facet" ]] || fail "--facet is required"
    [[ -n "$phase" ]] || fail "--phase is required"
    [[ -z "$engine" ]] || llm_runner_set_engine "$engine"
    llm_runner_stamp_output "$file_path" "$place" "$facet" "$phase"
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    fail "unknown subcommand: $subcommand"
    ;;
esac
