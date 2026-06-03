from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "tools" / "scripts" / "lib" / "llm_runner.sh"
BRIDGE_PATH = REPO_ROOT / "tools" / "scripts" / "lib" / "llm_runner_bridge.sh"
GATHER_DRIVER_PATH = REPO_ROOT / "tools" / "scripts" / "run_topic_gather.py"
GATHER_DOC_PATH = REPO_ROOT / "docs" / "scripts" / "index_run_gather.md"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_llm_runner_header_declares_live_gather_role() -> None:
    header = "\n".join(read_text(RUNNER_PATH).splitlines()[:14])

    assert "shared shell LLM engine abstraction for the live gather runtime" in header
    assert "run_topic_gather.py" in header
    assert "llm_runner_bridge.sh" in header
    assert "wrap any untrusted source text" in header
    assert "does not validate or elevate LLM output into source material" in header
    assert "legacy gather scripts" not in header


def test_llm_runner_has_live_nonlegacy_callers() -> None:
    result = subprocess.run(
        ["rg", "-l", "llm_runner", "tools", "tests"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    references = {line.strip() for line in result.stdout.splitlines() if line.strip()}
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
