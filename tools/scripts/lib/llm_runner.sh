#!/usr/bin/env bash
# llm_runner.sh — shared shell LLM engine abstraction for the live gather runtime.
#
# Used by tools/scripts/run_topic_gather.py via llm_runner_bridge.sh. This
# library selects the configured Codex or Claude engine, runs the prompt, and
# stamps provenance on gather candidate output.
#
# Safety:
# - callers must wrap any untrusted source text before passing prompt_text here
# - this library does not validate or elevate LLM output into source material
#
# Shell library only; source it from another script after runtime_logging.sh.
#
# Usage in a caller script:
#   source "$SCRIPT_DIR/lib/llm_runner.sh"   # after runtime_logging.sh
#   # optionally: llm_runner_set_engine "claude"  (or via --agent flag)
#   llm_runner_init || fail "LLM engine unavailable"
#   llm_runner_run_quiet "$tmp" "$prompt" "$phase" "MyTool.sh"
#   # or: llm_runner_run_to_file "$tmp" "$prompt" "$output_file" "$phase" "MyTool.sh"
#   llm_runner_stamp_output "$output_file" "$place" "$facet" "$phase"
#
# Env var inputs (all optional; lib sets safe defaults):
#   LLM_ENGINE                codex | claude          (default: codex)
#   CODEX_MODEL               model string            (default: gpt-5.4-mini)
#   CODEX_REASONING_EFFORT    low | medium | high     (default: medium)
#   CODEX_MAX_OUTPUT_TOKENS    positive int            (default: 8192)
#   CODEX_MODEL_VERBOSITY      low | medium | high     (default: low)
#   CODEX_OUTPUT_SCHEMA_FILE   JSON schema path        (default: unset)
#   CLAUDE_MODEL              model string or alias   (default: sonnet)
#   CLAUDE_EFFORT             low | medium | high     (default: medium)
#   CLAUDE_MAX_BUDGET_USD     positive decimal budget  (default: 0.50)
#   CLAUDE_JSON_SCHEMA        JSON schema string       (default: {"type":"object","properties":{"text":{"type":"string"}},"required":["text"],"additionalProperties":false})
#
# Depends on: runtime_logging.sh (runtime_log_event must be defined before
#   llm_runner_run_quiet is called)

set -euo pipefail

# ---------------------------------------------------------------------------
# Public state — read these in callers; do not set them directly
# ---------------------------------------------------------------------------
LLM_RUNNER_ENGINE="${LLM_ENGINE:-codex}"
LLM_RUNNER_CODEX_MODEL="${CODEX_MODEL:-gpt-5.4-mini}"
LLM_RUNNER_CODEX_EFFORT="${CODEX_REASONING_EFFORT:-medium}"
LLM_RUNNER_CODEX_MAX_OUTPUT_TOKENS="${CODEX_MAX_OUTPUT_TOKENS:-8192}"
LLM_RUNNER_CODEX_MODEL_VERBOSITY="${CODEX_MODEL_VERBOSITY:-low}"
LLM_RUNNER_CODEX_OUTPUT_SCHEMA_FILE="${CODEX_OUTPUT_SCHEMA_FILE:-}"
LLM_RUNNER_CLAUDE_MODEL="${CLAUDE_MODEL:-sonnet}"
LLM_RUNNER_CLAUDE_EFFORT="${CLAUDE_EFFORT:-medium}"
LLM_RUNNER_CLAUDE_MAX_BUDGET_USD="${CLAUDE_MAX_BUDGET_USD:-0.50}"
LLM_RUNNER_CLAUDE_JSON_SCHEMA_DEFAULT='{"type":"object","properties":{"text":{"type":"string"}},"required":["text"],"additionalProperties":false}'
LLM_RUNNER_CLAUDE_JSON_SCHEMA="${CLAUDE_JSON_SCHEMA:-$LLM_RUNNER_CLAUDE_JSON_SCHEMA_DEFAULT}"

readonly LLM_RUNNER_SUPPORTED_ENGINES="codex|claude"
readonly LLM_RUNNER_REQUIRED_RUNTIME_LOGGER="runtime_log_event"

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------
_LLM_RUNNER_INITIALIZED=0
_LLM_RUNNER_CODEX_ARGS=()

_llm_runner_require_runtime_logging() {
  local logger_type

  logger_type="$(type -t "$LLM_RUNNER_REQUIRED_RUNTIME_LOGGER" || true)"
  if [[ "$logger_type" != function && "$logger_type" != file ]]; then
    printf 'llm_runner: %s must be defined before running LLM commands\n' "$LLM_RUNNER_REQUIRED_RUNTIME_LOGGER" >&2
    return 1
  fi
}

_llm_runner_require_tmp_dir() {
  local tmp_dir="$1"

  if [[ ! -d "$tmp_dir" ]]; then
    printf 'llm_runner: tmp_dir "%s" does not exist\n' "$tmp_dir" >&2
    return 1
  fi
  if [[ ! -w "$tmp_dir" ]]; then
    printf 'llm_runner: tmp_dir "%s" is not writable\n' "$tmp_dir" >&2
    return 1
  fi
}

_llm_runner_require_output_dir() {
  local file_path="$1"
  local output_dir

  output_dir="$(dirname -- "$file_path")"
  if [[ "$output_dir" == "." ]]; then
    output_dir="$(pwd -P)"
  fi
  if [[ ! -d "$output_dir" ]]; then
    printf 'llm_runner: output directory "%s" does not exist\n' "$output_dir" >&2
    return 1
  fi
  if [[ ! -w "$output_dir" ]]; then
    printf 'llm_runner: output directory "%s" is not writable\n' "$output_dir" >&2
    return 1
  fi
}

# ---------------------------------------------------------------------------
# llm_runner_set_engine <engine>
#   Validate and set the engine. Call before llm_runner_init.
#   Typically wired to a --agent flag in the caller's arg parser.
# ---------------------------------------------------------------------------
llm_runner_set_engine() {
  local engine="$1"
  engine="${engine,,}"
  case "$engine" in
    codex|claude)
      LLM_RUNNER_ENGINE="$engine"
      ;;
    *)
      printf 'llm_runner: unsupported engine "%s" (supported: %s)\n' "$engine" "$LLM_RUNNER_SUPPORTED_ENGINES" >&2
      return 1
      ;;
  esac
}

# ---------------------------------------------------------------------------
# llm_runner_init
#   Validate that the selected engine binary is in PATH and build any
#   engine-specific arg arrays. Call once after all flags are parsed.
# ---------------------------------------------------------------------------
llm_runner_init() {
  _llm_runner_require_runtime_logging || return 1

  case "$LLM_RUNNER_ENGINE" in
    codex)
      command -v codex >/dev/null 2>&1 || {
        printf 'llm_runner: codex not found in PATH\n' >&2
        return 1
      }
      _LLM_RUNNER_CODEX_ARGS=(
        --skip-git-repo-check
        -s workspace-write
        -c "model=${LLM_RUNNER_CODEX_MODEL}"
        -c "model_reasoning_effort=${LLM_RUNNER_CODEX_EFFORT}"
        -c "model_max_output_tokens=${LLM_RUNNER_CODEX_MAX_OUTPUT_TOKENS}"
        -c "model_verbosity=${LLM_RUNNER_CODEX_MODEL_VERBOSITY}"
      )
      if [[ -n "$LLM_RUNNER_CODEX_OUTPUT_SCHEMA_FILE" ]]; then
        _LLM_RUNNER_CODEX_ARGS+=(--output-schema "$LLM_RUNNER_CODEX_OUTPUT_SCHEMA_FILE")
      fi
      ;;
    claude)
      command -v claude >/dev/null 2>&1 || {
        printf 'llm_runner: claude not found in PATH\n' >&2
        return 1
      }
      ;;
    *)
      printf 'llm_runner: unsupported engine "%s"\n' "$LLM_RUNNER_ENGINE" >&2
      return 1
      ;;
  esac
  _LLM_RUNNER_INITIALIZED=1
}

# ---------------------------------------------------------------------------
# Internal engine runners — not part of the public API
# ---------------------------------------------------------------------------
_llm_runner_exec_codex() {
  local tmp_dir="$1" prompt_text="$2" stdout_file="$3" stderr_file="$4"
  ( cd "$tmp_dir" && \
    printf '%s' "$prompt_text" | codex exec "${_LLM_RUNNER_CODEX_ARGS[@]}" - \
  ) >"$stdout_file" 2>"$stderr_file"
}

_llm_runner_exec_codex_json() {
  local tmp_dir="$1" prompt_text="$2" stdout_file="$3" stderr_file="$4"
  ( cd "$tmp_dir" && \
    printf '%s' "$prompt_text" | codex exec --json "${_LLM_RUNNER_CODEX_ARGS[@]}" - \
  ) >"$stdout_file" 2>"$stderr_file"
}

_llm_runner_exec_claude() {
  local tmp_dir="$1" prompt_text="$2" stdout_file="$3" stderr_file="$4"
  ( cd "$tmp_dir" && \
    printf '%s' "$prompt_text" | claude -p \
      --model "$LLM_RUNNER_CLAUDE_MODEL" \
      --effort "$LLM_RUNNER_CLAUDE_EFFORT" \
      --max-budget-usd "$LLM_RUNNER_CLAUDE_MAX_BUDGET_USD" \
      --output-format json \
      --json-schema "$LLM_RUNNER_CLAUDE_JSON_SCHEMA" \
  ) >"$stdout_file" 2>"$stderr_file"
}

_llm_runner_materialize_codex_json_output() {
  local event_file="$1" output_file="$2" usage_file="${3:-}"

  python3 - "$event_file" "$output_file" "$usage_file" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

event_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
usage_path = Path(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else None

try:
    raw_text = event_path.read_text(encoding="utf-8", errors="replace")
except OSError:
    raise SystemExit(1)

lines = [line for line in raw_text.splitlines() if line.strip()]
json_events: list[dict[str, object]] = []
for line in lines:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        output_path.write_text(raw_text, encoding="utf-8")
        if usage_path is not None:
            usage_path.unlink(missing_ok=True)
        raise SystemExit(0)
    if not isinstance(event, dict):
        output_path.write_text(raw_text, encoding="utf-8")
        if usage_path is not None:
            usage_path.unlink(missing_ok=True)
        raise SystemExit(0)
    json_events.append(event)

final_text = None
usage: dict[str, object] | None = None
for event in json_events:
    event_type = event.get("type")
    if event_type == "item.completed":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                final_text = text
            elif text is None:
                final_text = ""
            else:
                final_text = str(text)
    elif event_type == "turn.completed":
        usage_payload = event.get("usage")
        if isinstance(usage_payload, dict):
            usage = dict(usage_payload)

if final_text is None:
    raise SystemExit(1)

output_path.write_text(final_text, encoding="utf-8")
if usage_path is not None:
    if usage is None:
        usage_path.unlink(missing_ok=True)
    else:
        try:
            input_tokens = int(usage.get("input_tokens", 0) or 0)
        except (TypeError, ValueError):
            input_tokens = 0
        try:
            output_tokens = int(usage.get("output_tokens", 0) or 0)
        except (TypeError, ValueError):
            output_tokens = 0
        usage.setdefault("total_tokens", input_tokens + output_tokens)
        usage_payload = {
            "schema_version": "llm-usage.v1",
            "engine": "codex",
            "usage": usage,
        }
        usage_path.write_text(
            json.dumps(usage_payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

raise SystemExit(0)
PY
}

_llm_runner_materialize_claude_output() {
  local input_file="$1" output_file="$2"

  python3 - "$input_file" "$output_file" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

input_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])

try:
    raw_text = input_path.read_text(encoding="utf-8", errors="replace")
except OSError:
    raise SystemExit(1)

try:
    parsed = json.loads(raw_text)
except json.JSONDecodeError:
    output_path.write_text(raw_text, encoding="utf-8")
    raise SystemExit(0)

final_text = None
if isinstance(parsed, str):
    final_text = parsed
elif isinstance(parsed, dict):
    for key in ("text", "result"):
        value = parsed.get(key)
        if isinstance(value, str):
            final_text = value
            break
    if final_text is None:
        content = parsed.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            if parts:
                final_text = "".join(parts)
        if final_text is None:
            message = parsed.get("message")
            if isinstance(message, dict):
                for key in ("text", "result"):
                    value = message.get(key)
                    if isinstance(value, str):
                        final_text = value
                        break
                if final_text is None:
                    content = message.get("content")
                    if isinstance(content, list):
                        parts = []
                        for item in content:
                            if isinstance(item, str):
                                parts.append(item)
                            elif isinstance(item, dict):
                                text = item.get("text")
                                if isinstance(text, str):
                                    parts.append(text)
                        if parts:
                            final_text = "".join(parts)
elif isinstance(parsed, list):
    parts = []
    for item in parsed:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    if parts:
        final_text = "".join(parts)

if final_text is None:
    output_path.write_text(raw_text, encoding="utf-8")
else:
    output_path.write_text(final_text, encoding="utf-8")
raise SystemExit(0)
PY
}

# ---------------------------------------------------------------------------
# llm_runner_run_quiet <tmp_dir> <prompt_text> <phase> <tool_name>
#   Run the selected engine in <tmp_dir> with <prompt_text>.
#   Captures stderr; emits LLM_OK / LLM_FAIL log events via runtime_log_event.
#   Returns the engine exit code on failure.
#
#   <phase>      short tag used in log events and stderr filename
#   <tool_name>  caller identity for log events (e.g. "subject_Build_Place.sh")
# ---------------------------------------------------------------------------
llm_runner_run_quiet() {
  local tmp_dir="$1" prompt_text="$2" phase="$3" tool_name="${4:-llm_runner}"
  local stderr_file start_ts end_ts elapsed rc

  [[ "$_LLM_RUNNER_INITIALIZED" == "1" ]] || {
    printf 'llm_runner: llm_runner_init must be called before llm_runner_run_quiet\n' >&2
    return 1
  }

  _llm_runner_require_tmp_dir "$tmp_dir" || return 1

  stderr_file="$(mktemp "${tmp_dir%/}/llm.${phase}.stderr.XXXXXX")"
  : > "$stderr_file"
  start_ts="$(date +%s)"

  if "_llm_runner_exec_${LLM_RUNNER_ENGINE}" "$tmp_dir" "$prompt_text" "/dev/null" "$stderr_file"; then
    end_ts="$(date +%s)"
    elapsed=$((end_ts - start_ts))
    runtime_log_event LLM_OK \
      "tool=${tool_name} engine=${LLM_RUNNER_ENGINE} phase=${phase} elapsed=${elapsed}s"
    return 0
  else
    rc=$?
  fi

  end_ts="$(date +%s)"
  elapsed=$((end_ts - start_ts))
  runtime_log_event LLM_FAIL \
    "tool=${tool_name} engine=${LLM_RUNNER_ENGINE} phase=${phase} exit=${rc} elapsed=${elapsed}s stderr_file=${stderr_file}"
  printf 'LLM (%s) failed in phase: %s\n' "$LLM_RUNNER_ENGINE" "$phase" >&2
  printf 'Captured stderr: %s\n' "$stderr_file" >&2
  tail -n 80 "$stderr_file" >&2 || true
  return "$rc"
}

# ---------------------------------------------------------------------------
# llm_runner_run_to_file <tmp_dir> <prompt_text> <output_file> <phase> <tool_name>
#   Run the selected engine in <tmp_dir> with <prompt_text> and capture stdout
#   in <output_file>. Captures stderr and emits the same runtime log events as
#   llm_runner_run_quiet.
# ---------------------------------------------------------------------------
llm_runner_run_to_file() {
  local tmp_dir="$1" prompt_text="$2" output_file="$3" phase="$4" tool_name="${5:-llm_runner}"
  local stderr_file output_tmp_file event_tmp_file usage_tmp_file usage_file start_ts end_ts elapsed rc

  [[ "$_LLM_RUNNER_INITIALIZED" == "1" ]] || {
    printf 'llm_runner: llm_runner_init must be called before llm_runner_run_to_file\n' >&2
    return 1
  }

  _llm_runner_require_tmp_dir "$tmp_dir" || return 1
  _llm_runner_require_output_dir "$output_file" || return 1

  if [[ -e "$output_file" && ! -w "$output_file" ]]; then
    printf 'llm_runner: output file "%s" is not writable\n' "$output_file" >&2
    return 1
  fi

  output_tmp_file="$(mktemp "$(dirname -- "$output_file")/.$(basename -- "$output_file").tmp.XXXXXX")"
  event_tmp_file=""
  usage_tmp_file=""
  usage_file="${output_file}.usage.json"
  trap 'rm -f -- "$output_tmp_file"; if [[ -n "${event_tmp_file:-}" ]]; then rm -f -- "$event_tmp_file"; fi; if [[ -n "${usage_tmp_file:-}" ]]; then rm -f -- "$usage_tmp_file"; fi' RETURN
  stderr_file="$(mktemp "${tmp_dir%/}/llm.${phase}.stderr.XXXXXX")"
  : > "$stderr_file"
  start_ts="$(date +%s)"

  case "$LLM_RUNNER_ENGINE" in
    codex)
      event_tmp_file="$(mktemp "$(dirname -- "$output_file")/.$(basename -- "$output_file").events.XXXXXX")"
      usage_tmp_file="$(mktemp "$(dirname -- "$output_file")/.$(basename -- "$output_file").usage.XXXXXX")"
      if _llm_runner_exec_codex_json "$tmp_dir" "$prompt_text" "$event_tmp_file" "$stderr_file"; then
        if ! _llm_runner_materialize_codex_json_output \
          "$event_tmp_file" \
          "$output_tmp_file" \
          "$usage_tmp_file"
        then
          return 1
        fi
        if ! mv -- "$output_tmp_file" "$output_file"; then
          return 1
        fi
        if [[ -f "$usage_tmp_file" ]]; then
          if ! mv -- "$usage_tmp_file" "$usage_file"; then
            return 1
          fi
        else
          rm -f -- "$usage_file"
        fi
        rm -f -- "$event_tmp_file"
        trap - RETURN
        end_ts="$(date +%s)"
        elapsed=$((end_ts - start_ts))
        runtime_log_event LLM_OK \
          "tool=${tool_name} engine=${LLM_RUNNER_ENGINE} phase=${phase} elapsed=${elapsed}s output_file=${output_file}"
        return 0
      else
        rc=$?
      fi
      ;;
    claude)
      if _llm_runner_exec_claude "$tmp_dir" "$prompt_text" "$output_tmp_file" "$stderr_file"; then
        if ! _llm_runner_materialize_claude_output "$output_tmp_file" "$output_tmp_file"; then
          return 1
        fi
        if ! mv -- "$output_tmp_file" "$output_file"; then
          return 1
        fi
        rm -f -- "$usage_file"
        trap - RETURN
        end_ts="$(date +%s)"
        elapsed=$((end_ts - start_ts))
        runtime_log_event LLM_OK \
          "tool=${tool_name} engine=${LLM_RUNNER_ENGINE} phase=${phase} elapsed=${elapsed}s output_file=${output_file}"
        return 0
      else
        rc=$?
      fi
      ;;
  esac

  end_ts="$(date +%s)"
  elapsed=$((end_ts - start_ts))
  runtime_log_event LLM_FAIL \
    "tool=${tool_name} engine=${LLM_RUNNER_ENGINE} phase=${phase} exit=${rc} elapsed=${elapsed}s output_file=${output_file} stderr_file=${stderr_file}"
  printf 'LLM (%s) failed in phase: %s\n' "$LLM_RUNNER_ENGINE" "$phase" >&2
  printf 'Captured stderr: %s\n' "$stderr_file" >&2
  tail -n 80 "$stderr_file" >&2 || true
  return "$rc"
}

# ---------------------------------------------------------------------------
# llm_runner_stamp_output <file> <place> <facet> <phase>
#   Append a versioned provenance footer to an LLM output file.
#   Footer is separated by --- so forward-scanning parsers are unaffected.
#   Idempotent: skips files that already contain GENERATED_BY.
# ---------------------------------------------------------------------------
_llm_runner_has_stamp_footer() {
  local file="$1"

  local -a tail_lines=()
  local line

  [[ -r "$file" ]] || return 1

  while IFS= read -r line || [[ -n "$line" ]]; do
    tail_lines+=("$line")
    if (( ${#tail_lines[@]} > 8 )); then
      tail_lines=("${tail_lines[@]:1}")
    fi
  done < "$file" || return 1

  (( ${#tail_lines[@]} == 8 )) || return 1
  [[ "${tail_lines[0]}" == "---" ]] || return 1
  [[ "${tail_lines[1]}" == "RUN_META_VERSION: run-body-footer.v1" ]] || return 1
  case "${tail_lines[2]}" in
    GENERATED_BY:*) ;;
    *) return 1 ;;
  esac
  case "${tail_lines[3]}" in
    MODEL:*) ;;
    *) return 1 ;;
  esac
  case "${tail_lines[4]}" in
    PLACE:*) ;;
    *) return 1 ;;
  esac
  case "${tail_lines[5]}" in
    FACET:*) ;;
    *) return 1 ;;
  esac
  case "${tail_lines[6]}" in
    PHASE:*) ;;
    *) return 1 ;;
  esac
  case "${tail_lines[7]}" in
    RUN_TS:*) ;;
    *) return 1 ;;
  esac
  return 0
}

llm_runner_stamp_output() {
  local file="$1" place="$2" facet="$3" phase="$4"
  local model_str tmp_file output_dir
  local footer_schema_version="run-body-footer.v1"

  _llm_runner_require_output_dir "$file" || return 1
  _llm_runner_has_stamp_footer "$file" && return 0

  if [[ -e "$file" && ! -w "$file" ]]; then
    printf 'llm_runner: output file "%s" is not writable\n' "$file" >&2
    return 1
  fi

  tmp_file="$(mktemp "$(dirname -- "$file")/.$(basename -- "$file").stamp.XXXXXX")"
  trap 'rm -f "$tmp_file"' RETURN

  if [[ -f "$file" ]]; then
    cat -- "$file" > "$tmp_file"
  else
    : > "$tmp_file"
  fi

  case "$LLM_RUNNER_ENGINE" in
    codex)
      model_str="$LLM_RUNNER_CODEX_MODEL"
      ;;
    claude)
      model_str="$LLM_RUNNER_CLAUDE_MODEL"
      ;;
    *)
      model_str="unknown"
      ;;
  esac

  if ! printf '\n---\nRUN_META_VERSION: %s\nGENERATED_BY: %s\nMODEL: %s\nPLACE: %s\nFACET: %s\nPHASE: %s\nRUN_TS: %s\n' \
      "$footer_schema_version" "$LLM_RUNNER_ENGINE" "$model_str" "$place" "$facet" "$phase" \
      "$(date -u +%Y-%m-%dT%H%M%SZ)" >> "$tmp_file"; then
    return 1
  fi
  if ! mv -- "$tmp_file" "$file"; then
    return 1
  fi
  trap - RETURN
}
