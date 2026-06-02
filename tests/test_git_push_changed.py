import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "Git_Push_Changed.sh"


def make_fake_git(
    tmp_path: Path,
    *,
    top_level: Path | None = None,
    diff_exit_code: int = 1,
    status_output: str = " M changed.txt",
) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "git.log"
    top_level = top_level or REPO_ROOT
    git_path = bin_dir / "git"
    git_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f"LOG_PATH={str(log_path)!r}",
                f"TOP_LEVEL={str(top_level)!r}",
                f"DIFF_EXIT_CODE={diff_exit_code!r}",
                f"STATUS_OUTPUT={status_output!r}",
                'if [[ "${1:-}" == "-C" ]]; then',
                '  cd "$2"',
                '  shift 2',
                "fi",
                'printf "%s|%s|%s\\n" "$PWD" "$1" "$*" >> "$LOG_PATH"',
                'if [[ "$1" == "rev-parse" && "${2:-}" == "--show-toplevel" ]]; then',
                '  printf "%s\\n" "$TOP_LEVEL"',
                "  exit 0",
                "fi",
                'if [[ "$1" == "rev-parse" && "${2:-}" == "--abbrev-ref" ]]; then',
                '  printf "%s\\n" "main"',
                "  exit 0",
                "fi",
                'if [[ "$1" == "remote" && "${2:-}" == "get-url" ]]; then',
                '  printf "%s\\n" "git@example.test/repo.git"',
                "  exit 0",
                "fi",
                'if [[ "$1" == "status" ]]; then',
                '  printf "%s" "$STATUS_OUTPUT"',
                "  exit 0",
                "fi",
                'if [[ "$1" == "diff" && "${2:-}" == "--cached" ]]; then',
                '  exit "$DIFF_EXIT_CODE"',
                "fi",
                "exit 0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    git_path.chmod(0o755)
    return bin_dir, log_path


def run_script(
    tmp_path: Path,
    bin_dir: Path,
    *,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_runs_git_commands_from_repo_root_even_when_invoked_elsewhere(
    tmp_path: Path,
) -> None:
    bin_dir, log_path = make_fake_git(tmp_path)
    outside_cwd = tmp_path / "outside"
    outside_cwd.mkdir()

    result = run_script(tmp_path, bin_dir, cwd=outside_cwd)

    assert result.returncode == 0, result.stdout + result.stderr
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert lines[:6] == [
        f"{REPO_ROOT}|rev-parse|rev-parse --show-toplevel",
        f"{REPO_ROOT}|rev-parse|rev-parse --abbrev-ref HEAD",
        f"{REPO_ROOT}|remote|remote get-url origin",
        f"{REPO_ROOT}|status|status --short --untracked-files=normal",
        f"{REPO_ROOT}|add|add -A .",
        f"{REPO_ROOT}|diff|diff --cached --quiet --exit-code",
    ]
    assert lines[6].startswith(f"{REPO_ROOT}|commit|commit -m sync ")
    assert lines[7] == f"{REPO_ROOT}|rev-parse|rev-parse --abbrev-ref @{{upstream}}"
    assert lines[8] == f"{REPO_ROOT}|push|push origin main"


def test_mismatched_git_toplevel_fails_before_mutating_commands(tmp_path: Path) -> None:
    wrong_root = tmp_path / "wrong-root"
    wrong_root.mkdir()
    bin_dir, log_path = make_fake_git(tmp_path, top_level=wrong_root)

    result = run_script(tmp_path, bin_dir, cwd=tmp_path)

    assert result.returncode != 0
    assert "resolved repo root does not match git toplevel" in result.stderr.lower()
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        f"{REPO_ROOT}|rev-parse|rev-parse --show-toplevel"
    ]


def test_noop_run_skips_commit_and_push(tmp_path: Path) -> None:
    bin_dir, log_path = make_fake_git(tmp_path, diff_exit_code=0, status_output="")

    result = run_script(tmp_path, bin_dir, cwd=tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "No changes found to commit." in result.stderr
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        f"{REPO_ROOT}|rev-parse|rev-parse --show-toplevel",
        f"{REPO_ROOT}|rev-parse|rev-parse --abbrev-ref HEAD",
        f"{REPO_ROOT}|remote|remote get-url origin",
        f"{REPO_ROOT}|status|status --short --untracked-files=normal",
    ]
