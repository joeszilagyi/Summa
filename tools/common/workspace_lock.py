#!/usr/bin/env python3
"""Workspace-scoped advisory lock helper with heartbeat metadata."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOCK_ROOT = REPO_ROOT / "runtime" / "locks"
ID_SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


class WorkspaceLockError(RuntimeError):
    """Raised when a workspace lock cannot be acquired or managed."""


def safe_workspace_id(workspace_id: str) -> str:
    if not workspace_id or workspace_id.strip() != workspace_id:
        raise WorkspaceLockError("workspace_id must be a non-blank trimmed string")
    if any(char not in ID_SAFE_CHARS for char in workspace_id):
        raise WorkspaceLockError("workspace_id may contain only letters, numbers, dot, underscore, and hyphen")
    return workspace_id


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def lock_path_for(workspace_id: str, lock_root: Path = DEFAULT_LOCK_ROOT) -> Path:
    return lock_root / f"{safe_workspace_id(workspace_id)}.lock"


def metadata_for(*, workspace_id: str, command: str, lock_path: Path) -> dict[str, Any]:
    return {
        "schema_version": "workspace-lock.v1",
        "workspace_id": workspace_id,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "command": command,
        "lock_path": str(lock_path),
        "acquired_at": utc_now(),
        "heartbeat_at": utc_now(),
    }


def read_metadata(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stale_reason(path: Path, *, stale_after_seconds: int, now: float | None = None) -> str | None:
    current_time = time.time() if now is None else now
    metadata = read_metadata(path)
    if metadata is None:
        age = current_time - path.stat().st_mtime
        return "unreadable_metadata" if age >= stale_after_seconds else None

    pid = metadata.get("pid")
    host = metadata.get("host")
    if host == socket.gethostname() and isinstance(pid, int) and not pid_is_alive(pid):
        return "dead_pid"

    age = current_time - path.stat().st_mtime
    if age >= stale_after_seconds:
        return "heartbeat_expired"
    return None


def quarantine_stale_lock(path: Path, *, reason: str, quarantine_root: Path | None = None) -> Path:
    quarantine_dir = quarantine_root or path.parent / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    target = quarantine_dir / f"{path.name}.{int(time.time())}.{os.getpid()}.stale"
    if path.exists():
        shutil.move(str(path), str(target))
    audit = target.with_suffix(target.suffix + ".json")
    audit.write_text(
        json.dumps(
            {
                "schema_version": "workspace-lock-quarantine.v1",
                "original_lock_path": str(path),
                "quarantined_lock_path": str(target),
                "reason": reason,
                "quarantined_at": utc_now(),
                "quarantined_by_pid": os.getpid(),
                "host": socket.gethostname(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def write_metadata(handle, metadata: dict[str, Any]) -> None:
    metadata = dict(metadata)
    metadata["heartbeat_at"] = utc_now()
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


@contextmanager
def acquire_workspace_lock(
    workspace_id: str,
    *,
    command: str,
    lock_root: Path = DEFAULT_LOCK_ROOT,
    wait: bool = False,
    timeout_seconds: float = 0,
    stale_after_seconds: int = 3600,
    break_stale: bool = False,
) -> Iterator[Path]:
    lock_path = lock_path_for(workspace_id, lock_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()

    while True:
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            flags = fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(handle.fileno(), flags)
            except BlockingIOError as exc:
                handle.close()
                if not wait or (timeout_seconds and time.monotonic() - start >= timeout_seconds):
                    raise WorkspaceLockError(f"workspace lock is already held: {lock_path}") from exc
                time.sleep(0.1)
                continue

            reason = stale_reason(lock_path, stale_after_seconds=stale_after_seconds) if lock_path.stat().st_size else None
            if reason and break_stale:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                quarantine_stale_lock(lock_path, reason=reason)
                continue
            if reason and not break_stale:
                raise WorkspaceLockError(f"workspace lock appears stale ({reason}) and break_stale is false: {lock_path}")

            write_metadata(handle, metadata_for(workspace_id=workspace_id, command=command, lock_path=lock_path))
            try:
                yield lock_path
            finally:
                try:
                    handle.seek(0)
                    handle.truncate()
                    handle.flush()
                    os.fsync(handle.fileno())
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()
                    lock_path.unlink(missing_ok=True)
            return
        except Exception:
            if not handle.closed:
                with suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Acquire a workspace lock and optionally run a command.")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--lock-root", default=str(DEFAULT_LOCK_ROOT))
    parser.add_argument("--command-name", default="workspace-lock")
    parser.add_argument(
        "--command-timeout-seconds",
        type=float,
        default=0,
        help="Optional timeout for the wrapped command. Zero disables the timeout.",
    )
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=0)
    parser.add_argument("--stale-after-seconds", type=int, default=3600)
    parser.add_argument("--break-stale", action="store_true")
    parser.add_argument("--print-path", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        with acquire_workspace_lock(
            args.workspace_id,
            command=args.command_name or " ".join(command) or "workspace-lock",
            lock_root=Path(args.lock_root),
            wait=args.wait,
            timeout_seconds=args.timeout_seconds,
            stale_after_seconds=args.stale_after_seconds,
            break_stale=args.break_stale,
        ) as path:
            if args.print_path:
                print(path)
            if command:
                timeout_seconds = args.command_timeout_seconds if args.command_timeout_seconds > 0 else None
                try:
                    return subprocess.run(command, check=False, timeout=timeout_seconds).returncode
                except subprocess.TimeoutExpired:
                    timeout_text = f"{args.command_timeout_seconds:g}" if args.command_timeout_seconds > 0 else "0"
                    print(
                        f"Error: command timed out after {timeout_text} seconds: {' '.join(command)}",
                        file=sys.stderr,
                    )
                    return 124
            return 0
    except WorkspaceLockError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
