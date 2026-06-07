from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "tools" / "scripts" / "lib" / "llm_runner.sh"
BRIDGE_PATH = REPO_ROOT / "tools" / "scripts" / "lib" / "llm_runner_bridge.sh"
GATHER_DRIVER_PATH = REPO_ROOT / "tools" / "scripts" / "run_topic_gather.py"
GATHER_DOC_PATH = REPO_ROOT / "docs" / "scripts" / "index_run_gather.md"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def file_contains_text(path: Path, needle: str) -> bool:
    try:
        return needle in read_text(path)
    except UnicodeDecodeError:
        return False


def test_llm_runner_header_declares_live_gather_role() -> None:
    header = "\n".join(read_text(RUNNER_PATH).splitlines()[:14])

    assert "shared shell LLM engine abstraction for the live gather runtime" in header
    assert "run_topic_gather.py" in header
    assert "llm_runner_bridge.sh" in header
    assert "wrap any untrusted source text" in header
    assert "does not validate or elevate LLM output into source material" in header
    assert "legacy gather scripts" not in header


def test_llm_runner_has_live_nonlegacy_callers() -> None:
    references = {
        path.relative_to(REPO_ROOT).as_posix()
        for root in (REPO_ROOT / "tools", REPO_ROOT / "tests")
        for path in root.rglob("*")
        if path.is_file() and file_contains_text(path, "llm_runner")
    }
    runner_rel = RUNNER_PATH.relative_to(REPO_ROOT).as_posix()
    assert runner_rel in references
    assert len(references) > 1

    nonlegacy_refs = {
        ref
        for ref in references
        if ref != runner_rel and not ref.startswith("tools/scripts/legacy/")
    }
    assert "tools/scripts/lib/llm_runner_bridge.sh" in nonlegacy_refs
    assert "tools/scripts/run_topic_gather.py" in nonlegacy_refs
    assert "tests/test_run_topic_gather.py" in nonlegacy_refs


def test_live_gather_runtime_reaches_llm_runner_through_bridge() -> None:
    driver_text = read_text(GATHER_DRIVER_PATH)
    bridge_text = read_text(BRIDGE_PATH)

    assert "LLM_RUNNER_BRIDGE_PATH" in driver_text
    assert "invoke_llm_runner_bridge" in driver_text
    assert 'readonly LLM_RUNNER_LIB="$SELF_DIR/llm_runner.sh"' in bridge_text
    assert 'source "$LLM_RUNNER_LIB"' in bridge_text


def test_gather_doc_describes_llm_runner_as_live_engine_path() -> None:
    doc_text = read_text(GATHER_DOC_PATH)

    assert "live mode uses the shared `llm_runner.sh` abstraction" in doc_text
    assert "legacy gather scripts" not in doc_text


def _run_llm_runner_with_fake_engine(
    tmp_path: Path,
    *,
    engine: str,
    prompt_text: str,
    exit_code: int = 0,
    use_run_to_file: bool = False,
    output_file: Path | None = None,
) -> tuple[list[str], str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_file = tmp_path / f"{engine}.args"
    stdin_file = tmp_path / f"{engine}.stdin"
    fake_engine = bin_dir / engine
    fake_engine.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\n' "$@" > "$ARGS_FILE"
            cat > "$STDIN_FILE"
            printf 'engine-output\n'
            exit "$EXIT_CODE"
            """
        ),
        encoding="utf-8",
    )
    fake_engine.chmod(0o755)

    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(prompt_text, encoding="utf-8")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    output_path = output_file or (tmp_path / "output.txt")
    output_path.write_text("existing output\n", encoding="utf-8")
    if use_run_to_file:
        runner_call = (
            f'llm_runner_run_to_file "{work_dir}" "$prompt_text" "{output_path}" "phase" "pytest"'
        )
    else:
        runner_call = f'llm_runner_run_quiet "{work_dir}" "$prompt_text" "phase" "pytest"'
    script = textwrap.dedent(
        f"""\
        set -euo pipefail
        runtime_log_event() {{
          :
        }}
        export PATH="{bin_dir}:$PATH"
        source "{RUNNER_PATH}"
        llm_runner_set_engine "{engine}"
        llm_runner_init
        prompt_text="$(<"{prompt_file}")"
        {runner_call}
        """
    )
    proc = subprocess.run(
        ["bash", "-lc", script],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "ARGS_FILE": str(args_file),
            "STDIN_FILE": str(stdin_file),
            "EXIT_CODE": str(exit_code),
        },
    )
    if exit_code == 0:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    elif use_run_to_file:
        assert "LLM (" in proc.stderr, proc.stdout + proc.stderr
    else:
        assert proc.returncode == exit_code, proc.stdout + proc.stderr
    args = args_file.read_text(encoding="utf-8").splitlines()
    stdin = stdin_file.read_text(encoding="utf-8")
    output = output_path.read_text(encoding="utf-8")
    return args, stdin, output


def test_llm_runner_uses_stdin_for_codex_prompt_transport(tmp_path: Path) -> None:
    prompt_text = "x" * 200_000
    args, stdin, _output = _run_llm_runner_with_fake_engine(
        tmp_path,
        engine="codex",
        prompt_text=prompt_text,
    )

    assert args[0] == "exec"
    assert args[-1] == "-"
    assert stdin == prompt_text


def test_llm_runner_uses_stdin_for_claude_prompt_transport(tmp_path: Path) -> None:
    prompt_text = "y" * 200_000
    args, stdin, _output = _run_llm_runner_with_fake_engine(
        tmp_path,
        engine="claude",
        prompt_text=prompt_text,
    )

    assert args == ["-p", "--model", "sonnet", "--effort", "high"]
    assert stdin == prompt_text


def test_llm_runner_run_to_file_preserves_existing_output_on_failure(tmp_path: Path) -> None:
    prompt_text = "z" * 10_000
    output_file = tmp_path / "result.txt"
    args, stdin, output = _run_llm_runner_with_fake_engine(
        tmp_path,
        engine="codex",
        prompt_text=prompt_text,
        exit_code=2,
        use_run_to_file=True,
        output_file=output_file,
    )

    assert args[0] == "exec"
    assert args[-1] == "-"
    assert stdin == prompt_text
    assert output == "existing output\n"


def test_llm_runner_run_to_file_writes_output_on_success(tmp_path: Path) -> None:
    prompt_text = "q" * 10_000
    output_file = tmp_path / "result.txt"
    args, stdin, output = _run_llm_runner_with_fake_engine(
        tmp_path,
        engine="claude",
        prompt_text=prompt_text,
        use_run_to_file=True,
        output_file=output_file,
    )

    assert args[0] == "-p"
    assert stdin == prompt_text
    assert output == "engine-output\n"
