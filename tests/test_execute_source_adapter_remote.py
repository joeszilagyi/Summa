from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from tools.scripts import execute_source_adapter as source_executor

pytestmark = pytest.mark.network_fixture

REPO_ROOT = Path(__file__).resolve().parents[1]
EXECUTOR = REPO_ROOT / "tools" / "scripts" / "execute_source_adapter.py"
VALIDATOR = REPO_ROOT / "tools" / "validators" / "validate_source_acquisition_execution.py"
PLANNER = REPO_ROOT / "tools" / "scripts" / "plan_remote_url_manifest_adapter.py"
ADAPTER = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "source_adapter_runtime"
    / "remote_url_manifest"
    / "source_adapter.json"
)


class FixtureHandler(BaseHTTPRequestHandler):
    request_paths: list[str] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        type(self).request_paths.append(self.path)
        if self.path == "/text":
            body = b"remote fixture text\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/plain":
            body = b"remote fixture text"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/html":
            body = b"<html><body>Remote fixture HTML</body></html>\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/binary":
            body = b"\x00\x01\x02\x03"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/oversize":
            body = b"0123456789"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/missing":
            body = b"missing\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/text")
            self.end_headers()
        elif self.path == "/redirect-lower":
            self.send_response(302)
            self.send_header("location", "/text")
            self.end_headers()
        elif self.path == "/redirect-out":
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/outside")
            self.end_headers()
        elif self.path == "/rate-limit":
            body = b"too many requests\n"
            self.send_response(429)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Retry-After", "120")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/slow":
            time.sleep(0.5)
            body = b"slow response\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with contextlib.suppress(BrokenPipeError):
                self.wfile.write(body)
        else:
            self.send_response(500)
            self.end_headers()


def fixture_server() -> tuple[ThreadingHTTPServer, str]:
    FixtureHandler.request_paths = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def test_read_limited_response_uses_incremental_buffering() -> None:
    chunks = [b"ab", b"cd", b"ef", b""]
    read_calls = {"count": 0}

    class FakeResponse:
        def read(self, size: int) -> bytes:
            del size
            read_calls["count"] += 1
            if chunks:
                return chunks.pop(0)
            return b""

    payload, truncated = source_executor.read_limited_response(
        FakeResponse(), max_response_bytes=6
    )

    assert payload == b"abcdef"
    assert truncated is False
    assert read_calls["count"] == 4


def test_read_limited_response_truncates_without_extra_copy() -> None:
    chunks = [b"abc", b"def", b"ghi"]

    class FakeResponse:
        def read(self, size: int) -> bytes:
            del size
            if chunks:
                return chunks.pop(0)
            return b""

    payload, truncated = source_executor.read_limited_response(
        FakeResponse(), max_response_bytes=5
    )

    assert payload == b"abcde"
    assert truncated is True


def test_remote_fetch_one_spools_captured_payload_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"\x00\x01\x02\x03"
    payload_spool_dir = tmp_path / "payload-spool"

    class FakeResponse:
        def __init__(self) -> None:
            self.status = 200
            self.headers = {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(body)),
            }
            self._offset = 0

        def getcode(self) -> int:
            return 200

        def read(self, size: int) -> bytes:
            if self._offset >= len(body):
                return b""
            chunk = body[self._offset : self._offset + size]
            self._offset += len(chunk)
            return chunk

    class FakeOpener:
        def open(self, request: Any, timeout: float) -> FakeResponse:
            del request, timeout
            return FakeResponse()

    monkeypatch.setattr(source_executor, "build_opener", lambda _handler: FakeOpener())

    fetch_result = source_executor.remote_fetch_one(
        url="https://example.test/binary",
        method="GET",
        user_agent="SummaRemoteTest/1.0",
        allowlist_hosts=[],
        allowlist_prefixes=[],
        timeout_seconds=2,
        max_response_bytes=1024,
        payload_spool_dir=payload_spool_dir,
    )

    assert fetch_result["status"] == "captured"
    assert fetch_result["payload_path"] is not None
    payload_path = Path(str(fetch_result["payload_path"]))
    assert payload_path.parent == payload_spool_dir
    assert payload_path.read_bytes() == body
    assert fetch_result["payload_sha256"] == hashlib.sha256(body).hexdigest()
    assert fetch_result["payload_byte_count"] == len(body)


def test_execute_remote_fetches_reuses_one_opener_per_host_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"remote fixture text\n"
    opener_calls = {"count": 0}

    class FakeResponse:
        def __init__(self) -> None:
            self.status = 200
            self.headers = {
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Length": str(len(body)),
            }
            self._offset = 0

        def getcode(self) -> int:
            return 200

        def read(self, size: int) -> bytes:
            if self._offset >= len(body):
                return b""
            chunk = body[self._offset : self._offset + size]
            self._offset += len(chunk)
            return chunk

    class FakeOpener:
        def open(self, request: Any, timeout: float) -> FakeResponse:
            del request, timeout
            return FakeResponse()

    def build_opener_spy(_handler: Any) -> FakeOpener:
        opener_calls["count"] += 1
        return FakeOpener()

    monkeypatch.setattr(source_executor, "build_opener", build_opener_spy)
    records = [
        {
            "sequence": 1,
            "relative_path": "one",
            "preserved": {
                "original_locator": {"entry_url": "https://host-a.test/one"},
                "rights_posture": "public",
                "source_metadata": {"hazard_flags": []},
            },
            "source_specific": {"manifest_url": "https://host-a.test/manifest.json"},
        },
        {
            "sequence": 2,
            "relative_path": "two",
            "preserved": {
                "original_locator": {"entry_url": "https://host-a.test/two"},
                "rights_posture": "public",
                "source_metadata": {"hazard_flags": []},
            },
            "source_specific": {"manifest_url": "https://host-a.test/manifest.json"},
        },
    ]
    gate_report = {
        "planned_actions": [
            {
                "url": "https://host-a.test/one",
                "status": "planned",
                "method": "GET",
            },
            {
                "url": "https://host-a.test/two",
                "status": "planned",
                "method": "GET",
            },
        ],
        "checks": {
            "network_policy": {
                "user_agent": "SummaRemoteTest/1.0",
                "allow_http": True,
                "robots_mode": "respect_robots",
            },
            "rate_limits": {"min_interval_seconds": 0},
            "allowlist": {
                "hosts": [],
                "url_prefixes": ["https://host-a.test/"],
            },
        },
    }

    capture_events, extraction_records, text_artifacts, binary_artifacts, failed, summary = (
        source_executor.execute_remote_fetches(
            records=records,
            adapter_payload={"adapter_id": "remote_fixture", "workspace_id": "alpha_subject"},
            run_id="remote-opener-reuse-test",
            created_at="2026-06-03T12:34:56Z",
            handoff_hash="a" * 64,
            gate_report=gate_report,
            timeout_seconds=2,
            max_response_bytes=1024,
            payload_spool_dir=tmp_path / "payload-spool",
        )
    )

    assert opener_calls["count"] == 1
    assert failed is False
    assert summary["urls_attempted"] == 2
    assert summary["urls_succeeded"] == 2
    assert summary["urls_failed"] == 0
    assert [record["status"] for record in capture_events] == ["completed", "completed"]
    assert [record["status"] for record in extraction_records] == ["completed", "completed"]
    assert text_artifacts == {
        "extracted-text/extraction-0001.txt": "remote fixture text\n",
        "extracted-text/extraction-0002.txt": "remote fixture text\n",
    }
    assert binary_artifacts == {}


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def canonical_jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    return "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records
    ).encode("utf-8")


def make_handoff(tmp_path: Path, urls: list[str]) -> Path:
    manifest = tmp_path / "remote-manifest.jsonl"
    manifest.write_text(
        "".join(
            json.dumps({"url": url, "title": f"entry {index}"}) + "\n"
            for index, url in enumerate(urls, start=1)
        ),
        encoding="utf-8",
    )
    handoff = tmp_path / "handoff.jsonl"
    proc = subprocess.run(
        [
            sys.executable,
            str(PLANNER),
            "--adapter",
            str(ADAPTER),
            "--manifest-jsonl",
            str(manifest),
            "--handoff-jsonl",
            str(handoff),
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return handoff


def make_gate_request(
    tmp_path: Path,
    *,
    urls: list[str],
    allowed_prefix: str,
    dry_run: bool = False,
    max_actions: int | None = None,
) -> Path:
    payload = {
        "schema_version": "network-safety-gate-request.v1",
        "executor_name": "tools/scripts/execute_source_adapter.py",
        "workspace_id": "alpha_subject",
        "dry_run": dry_run,
        "allowlist": {
            "hosts": [],
            "url_prefixes": [allowed_prefix],
        },
        "rate_limits": {
            "max_requests_per_minute": 20,
            "min_interval_seconds": 0,
        },
        "side_effect_budget": {
            "max_actions": max_actions if max_actions is not None else len(urls),
            "max_side_effect_units": max_actions if max_actions is not None else len(urls),
        },
        "network_policy": {
            "user_agent": "SummaRemoteTest/1.0",
            "robots_mode": "respect_robots",
            "allow_http": True,
        },
        "dirty_worktree_policy": {
            "require_clean_worktree": False,
            "repo_root": None,
        },
        "planned_actions": [
            {
                "action_id": f"fetch-{index}",
                "action_kind": "fetch_payload",
                "url": url,
                "method": "GET",
                "side_effect_units": 1,
            }
            for index, url in enumerate(urls, start=1)
        ],
    }
    return write_json(tmp_path / "gate-request.json", payload)


def run_executor(
    *,
    handoff: Path,
    output: Path,
    gate_request: Path,
    allow_network: bool,
    dry_run: bool = False,
    suppress_execution_record_stdout: bool = False,
    max_response_bytes: int | None = None,
    timeout_seconds: float = 2,
) -> subprocess.CompletedProcess[str]:
    args = [
        sys.executable,
        str(EXECUTOR),
        "--handoff",
        str(handoff),
        "--adapter",
        str(ADAPTER),
        "--output",
        str(output),
        "--workspace-root",
        str(output.parent),
        "--mode",
        "remote",
        "--network-safety-request",
        str(gate_request),
        "--run-id",
        output.name,
        "--created-at",
        "2026-06-03T12:34:56Z",
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    if dry_run:
        args.append("--dry-run")
    if suppress_execution_record_stdout:
        args.append("--suppress-execution-record-stdout")
    if max_response_bytes is not None:
        args.extend(["--max-response-bytes", str(max_response_bytes)])
    if allow_network:
        args.append("--allow-network")
    return subprocess.run(args, cwd=REPO_ROOT, text=True, capture_output=True, check=False)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def open_targets_under(root: Path) -> list[Path]:
    fd_root = Path("/proc/self/fd")
    targets: list[Path] = []
    if not fd_root.exists():
        return targets
    for fd_path in fd_root.iterdir():
        try:
            target = Path(fd_path.readlink())
        except OSError:
            continue
        try:
            target.relative_to(root)
        except ValueError:
            continue
        targets.append(target)
    return targets


def test_execute_remote_fetches_emits_denied_evidence_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, base_url = fixture_server()
    original_helper = source_executor.build_remote_denied_extraction_record
    helper_calls = {"count": 0}

    def helper_spy(*args: object, **kwargs: object) -> dict[str, Any]:
        helper_calls["count"] += 1
        return original_helper(*args, **kwargs)

    monkeypatch.setattr(source_executor, "build_remote_denied_extraction_record", helper_spy)
    try:
        records = [
            {
                "sequence": 1,
                "relative_path": "one",
                "preserved": {
                    "original_locator": {"entry_url": f"{base_url}/text"},
                    "rights_posture": "public",
                    "source_metadata": {"hazard_flags": []},
                },
                "source_specific": {"manifest_url": f"{base_url}/manifest.json"},
            },
            {
                "sequence": 2,
                "relative_path": "two",
                "preserved": {
                    "original_locator": {"entry_url": f"{base_url}/html"},
                    "rights_posture": "public",
                    "source_metadata": {"hazard_flags": []},
                },
                "source_specific": {"manifest_url": f"{base_url}/manifest.json"},
            },
            {
                "sequence": 3,
                "relative_path": "three",
                "preserved": {
                    "original_locator": {"entry_url": f"{base_url}/plain"},
                    "rights_posture": "public",
                    "source_metadata": {"hazard_flags": []},
                },
                "source_specific": {"manifest_url": f"{base_url}/manifest.json"},
            },
        ]
        gate_report = {
            "planned_actions": [
                {
                    "url": f"{base_url}/text",
                    "status": "planned",
                    "method": "GET",
                },
                {
                    "url": f"{base_url}/plain",
                    "status": "planned",
                    "method": "POST",
                },
            ],
            "checks": {
                "network_policy": {
                    "user_agent": "SummaRemoteTest/1.0",
                    "allow_http": True,
                    "robots_mode": "respect_robots",
                },
                "rate_limits": {"min_interval_seconds": 0},
                "allowlist": {"hosts": [], "url_prefixes": [base_url]},
            },
        }
        capture_events, extraction_records, text_artifacts, binary_artifacts, failed, summary = (
            source_executor.execute_remote_fetches(
                records=records,
                adapter_payload={"adapter_id": "remote_fixture", "workspace_id": "alpha_subject"},
                run_id="remote-denial-test",
                created_at="2026-06-03T12:34:56Z",
                handoff_hash="a" * 64,
                gate_report=gate_report,
                timeout_seconds=2,
                max_response_bytes=1024,
                payload_spool_dir=tmp_path / "payload-spool",
            )
        )

        assert failed is True
        assert summary["urls_attempted"] == 1
        assert summary["urls_succeeded"] == 1
        assert summary["urls_denied"] == 2
        assert [record["status"] for record in capture_events] == ["completed", "denied", "denied"]
        assert [record["status"] for record in extraction_records] == [
            "completed",
            "denied",
            "denied",
        ]
        assert capture_events[1]["failure_reason"] == "network_gate_action_missing"
        assert capture_events[2]["failure_reason"] == "unsupported_request_method"
        assert extraction_records[1]["failure_reason"] == "network_gate_action_missing"
        assert extraction_records[2]["failure_reason"] == "unsupported_request_method"
        assert helper_calls["count"] == 2
        assert text_artifacts["extracted-text/extraction-0001.txt"] == "remote fixture text\n"
        assert binary_artifacts == {}
    finally:
        server.shutdown()


def test_remote_executor_marks_denied_only_runs_as_network_attempted(
    tmp_path: Path, monkeypatch
) -> None:
    records = [
        {
            "sequence": 1,
            "relative_path": "one",
            "preserved": {
                "original_locator": {"entry_url": "https://example.test/one"},
                "rights_posture": "public",
                "source_metadata": {"hazard_flags": []},
            },
            "source_specific": {"manifest_url": "https://example.test/manifest.json"},
        },
        {
            "sequence": 2,
            "relative_path": "two",
            "preserved": {
                "original_locator": {"entry_url": "https://example.test/two"},
                "rights_posture": "public",
                "source_metadata": {"hazard_flags": []},
            },
            "source_specific": {"manifest_url": "https://example.test/manifest.json"},
        },
    ]
    gate_report = {
        "schema_version": "network-safety-gate-report.v1",
        "decision": "allow",
        "execution_allowed": True,
        "counts": {"errors": 0, "warnings": 0},
        "planned_actions": [
            {
                "action_id": "fetch-1",
                "action_kind": "fetch_payload",
                "url": "https://example.test/one",
                "status": "refused",
                "method": "GET",
            },
            {
                "action_id": "fetch-2",
                "action_kind": "fetch_payload",
                "url": "https://example.test/two",
                "status": "refused",
                "method": "GET",
            },
        ],
        "checks": {
            "network_policy": {
                "user_agent": "SummaRemoteTest/1.0",
                "allow_http": True,
                "robots_mode": "respect_robots",
            },
            "rate_limits": {"min_interval_seconds": 0},
            "allowlist": {"hosts": [], "url_prefixes": ["https://example.test/"]},
        },
    }
    gate_request_path = tmp_path / "gate-request.json"
    gate_request_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(source_executor, "load_request", lambda _path: {})
    monkeypatch.setattr(source_executor, "evaluate_request", lambda _payload: gate_report)

    (
        execution_record,
        denial_record,
        capture_events,
        extraction_records,
        _gate_report,
        _expected_urls,
        text_artifacts,
        binary_artifacts,
    ) = source_executor.execute_remote_url_manifest(
        records=records,
        run_id="remote-denial-only",
        created_at="2026-06-03T12:34:56Z",
        handoff_path=tmp_path / "handoff.jsonl",
        handoff_hash="a" * 64,
        adapter_payload={"adapter_id": "remote_fixture", "workspace_id": "alpha_subject"},
        gate_request_path=gate_request_path,
        dry_run=False,
        allow_network=True,
        timeout_seconds=2,
        max_response_bytes=1024,
        payload_spool_dir=tmp_path / "payload-spool",
    )

    assert denial_record is None
    assert execution_record["network_access_attempted"] is True
    assert execution_record["urls_denied"] == 2
    assert "_text_artifacts" not in execution_record
    assert "_binary_artifacts" not in execution_record
    assert text_artifacts == {}
    assert binary_artifacts == {}
    assert capture_events[0]["status"] == "denied"
    assert extraction_records[0]["status"] == "denied"


def test_remote_executor_rejects_gate_report_mismatch_before_network_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = [
        {
            "sequence": 1,
            "relative_path": "one",
            "preserved": {
                "original_locator": {"entry_url": "https://example.test/one"},
                "rights_posture": "public",
                "source_metadata": {"hazard_flags": []},
            },
            "source_specific": {"manifest_url": "https://example.test/manifest.json"},
        },
        {
            "sequence": 2,
            "relative_path": "two",
            "preserved": {
                "original_locator": {"entry_url": "https://example.test/two"},
                "rights_posture": "public",
                "source_metadata": {"hazard_flags": []},
            },
            "source_specific": {"manifest_url": "https://example.test/manifest.json"},
        },
    ]
    base_gate_report = {
        "schema_version": "network-safety-gate-report.v1",
        "decision": "allow",
        "execution_allowed": True,
        "counts": {"errors": 0, "warnings": 0},
        "checks": {
            "network_policy": {
                "user_agent": "SummaRemoteTest/1.0",
                "allow_http": True,
                "robots_mode": "respect_robots",
            },
            "rate_limits": {"min_interval_seconds": 0},
            "allowlist": {"hosts": [], "url_prefixes": ["https://example.test/"]},
        },
    }
    gate_request_path = tmp_path / "gate-request.json"
    gate_request_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(source_executor, "load_request", lambda _path: {})

    wrong_method_report = {
        **base_gate_report,
        "planned_actions": [
            {
                "action_id": "fetch-1",
                "action_kind": "fetch_payload",
                "url": "https://example.test/one",
                "status": "planned",
                "method": "POST",
            },
            {
                "action_id": "fetch-2",
                "action_kind": "fetch_payload",
                "url": "https://example.test/two",
                "status": "planned",
                "method": "GET",
            },
        ],
    }
    monkeypatch.setattr(source_executor, "evaluate_request", lambda _payload: wrong_method_report)

    with pytest.raises(
        source_executor.SourceAcquisitionError,
        match="missing planned actions for: https://example.test/one",
    ):
        source_executor.execute_remote_url_manifest(
            records=records,
            run_id="remote-gate-order-mismatch",
            created_at="2026-06-03T12:34:56Z",
            handoff_path=tmp_path / "handoff.jsonl",
            handoff_hash="a" * 64,
            adapter_payload={"adapter_id": "remote_fixture", "workspace_id": "alpha_subject"},
            gate_request_path=gate_request_path,
            dry_run=False,
            allow_network=True,
            timeout_seconds=2,
            max_response_bytes=1024,
            payload_spool_dir=tmp_path / "payload-spool",
        )

    extra_action_report = {
        **base_gate_report,
        "planned_actions": [
            {
                "action_id": "fetch-1",
                "action_kind": "fetch_payload",
                "url": "https://example.test/one",
                "status": "planned",
                "method": "GET",
            },
            {
                "action_id": "fetch-2",
                "action_kind": "fetch_payload",
                "url": "https://example.test/two",
                "status": "planned",
                "method": "GET",
            },
            {
                "action_id": "fetch-3",
                "action_kind": "fetch_payload",
                "url": "https://example.test/extra",
                "status": "planned",
                "method": "GET",
            },
        ],
    }
    monkeypatch.setattr(source_executor, "evaluate_request", lambda _payload: extra_action_report)

    with pytest.raises(
        source_executor.SourceAcquisitionError,
        match="includes unexpected planned actions for: https://example.test/extra",
    ):
        source_executor.execute_remote_url_manifest(
            records=records,
            run_id="remote-gate-extra-action",
            created_at="2026-06-03T12:34:56Z",
            handoff_path=tmp_path / "handoff.jsonl",
            handoff_hash="a" * 64,
            adapter_payload={"adapter_id": "remote_fixture", "workspace_id": "alpha_subject"},
            gate_request_path=gate_request_path,
            dry_run=False,
            allow_network=True,
            timeout_seconds=2,
            max_response_bytes=1024,
            payload_spool_dir=tmp_path / "payload-spool",
        )


def test_execute_remote_fetches_runs_hosts_concurrently_and_rates_each_host_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [
        {
            "sequence": 1,
            "relative_path": "a-one",
            "preserved": {
                "original_locator": {"entry_url": "https://host-a.test/one"},
                "rights_posture": "public",
                "source_metadata": {"hazard_flags": []},
            },
            "source_specific": {"manifest_url": "https://host-a.test/manifest.json"},
        },
        {
            "sequence": 2,
            "relative_path": "b-one",
            "preserved": {
                "original_locator": {"entry_url": "https://host-b.test/one"},
                "rights_posture": "public",
                "source_metadata": {"hazard_flags": []},
            },
            "source_specific": {"manifest_url": "https://host-b.test/manifest.json"},
        },
        {
            "sequence": 3,
            "relative_path": "a-two",
            "preserved": {
                "original_locator": {"entry_url": "https://host-a.test/two"},
                "rights_posture": "public",
                "source_metadata": {"hazard_flags": []},
            },
            "source_specific": {"manifest_url": "https://host-a.test/manifest.json"},
        },
    ]
    gate_report = {
        "schema_version": "network-safety-gate-report.v1",
        "decision": "allow",
        "execution_allowed": True,
        "counts": {"errors": 0, "warnings": 0},
        "planned_actions": [
            {
                "action_id": "fetch-1",
                "action_kind": "fetch_payload",
                "url": "https://host-a.test/one",
                "status": "planned",
                "method": "GET",
            },
            {
                "action_id": "fetch-2",
                "action_kind": "fetch_payload",
                "url": "https://host-b.test/one",
                "status": "planned",
                "method": "GET",
            },
            {
                "action_id": "fetch-3",
                "action_kind": "fetch_payload",
                "url": "https://host-a.test/two",
                "status": "planned",
                "method": "GET",
            },
        ],
        "checks": {
            "network_policy": {
                "user_agent": "SummaRemoteTest/1.0",
                "allow_http": True,
                "robots_mode": "respect_robots",
            },
            "rate_limits": {"min_interval_seconds": 0.25},
            "allowlist": {
                "hosts": [],
                "url_prefixes": ["https://host-a.test/", "https://host-b.test/"],
            },
        },
    }
    host_a_started = threading.Event()
    host_b_completed = threading.Event()
    sleep_calls: list[float] = []
    fetch_calls: list[str] = []

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        assert host_a_started.is_set()
        assert host_b_completed.wait(timeout=1.0), (
            "host B did not complete while host A was sleeping"
        )

    def fake_remote_fetch_one(
        *,
        url: str,
        method: str,
        user_agent: str,
        allowlist_hosts: list[str],
        allowlist_prefixes: list[str],
        timeout_seconds: float,
        max_response_bytes: int,
        payload_spool_dir: Path,
        opener: Any | None = None,
    ) -> dict[str, Any]:
        del (
            method,
            user_agent,
            allowlist_hosts,
            allowlist_prefixes,
            timeout_seconds,
            max_response_bytes,
            payload_spool_dir,
            opener,
        )
        fetch_calls.append(url)
        if "host-a.test" in url and url.endswith("/one"):
            host_a_started.set()
        if "host-b.test" in url:
            host_b_completed.set()
        return {
            "status": "captured",
            "failure_reason": None,
            "http_status_code": 200,
            "final_url": url,
            "redirect_count": 0,
            "attempted_urls": [url],
            "payload": b"remote fixture text\n",
            "truncated": False,
            "headers": {"Content-Type": "text/plain; charset=utf-8", "Content-Length": "20"},
        }

    monkeypatch.setattr(source_executor, "remote_fetch_one", fake_remote_fetch_one)
    monkeypatch.setattr(source_executor.time, "sleep", fake_sleep)

    capture_events, extraction_records, text_artifacts, binary_artifacts, failed, summary = (
        source_executor.execute_remote_fetches(
            records=records,
            adapter_payload={"adapter_id": "remote_fixture", "workspace_id": "alpha_subject"},
            run_id="remote-concurrent-hosts",
            created_at="2026-06-03T12:34:56Z",
            handoff_hash="a" * 64,
            gate_report=gate_report,
            timeout_seconds=2,
            max_response_bytes=1024,
            payload_spool_dir=tmp_path / "payload-spool",
        )
    )

    assert failed is False
    assert summary["urls_attempted"] == 3
    assert summary["urls_succeeded"] == 3
    assert summary["urls_failed"] == 0
    assert summary["urls_denied"] == 0
    assert sleep_calls == [0.25]
    assert len(fetch_calls) == 3
    assert fetch_calls[0] in {"https://host-a.test/one", "https://host-b.test/one"}
    assert fetch_calls[1] in {"https://host-a.test/one", "https://host-b.test/one"}
    assert fetch_calls[0] != fetch_calls[1]
    assert fetch_calls[2] == "https://host-a.test/two"
    assert [record["final_url"] for record in capture_events] == [
        "https://host-a.test/one",
        "https://host-b.test/one",
        "https://host-a.test/two",
    ]
    assert [record["status"] for record in extraction_records] == [
        "completed",
        "completed",
        "completed",
    ]
    assert text_artifacts == {
        "extracted-text/extraction-0001.txt": "remote fixture text\n",
        "extracted-text/extraction-0002.txt": "remote fixture text\n",
        "extracted-text/extraction-0003.txt": "remote fixture text\n",
    }
    assert binary_artifacts == {}


def test_gate_pass_with_explicit_opt_in_fetches_text_and_extracts(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/text"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(tmp_path, urls=[url], allowed_prefix=base_url)
        output = tmp_path / "remote-text-run"

        proc = run_executor(
            handoff=handoff, output=output, gate_request=gate_request, allow_network=True
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        execution = json.loads((output / "execution-record.json").read_text(encoding="utf-8"))
        captures = load_jsonl(output / "capture-events.jsonl")
        extractions = load_jsonl(output / "extraction-records.jsonl")
        assert execution["network_access_attempted"] is True
        assert execution["network_access_allowed"] is True
        assert execution["remote_live_fetch_enabled"] is True
        assert execution["urls_attempted"] == 1
        assert execution["urls_succeeded"] == 1
        assert (output / "execution-record.json").read_bytes() == canonical_json_bytes(execution)
        assert (output / "capture-events.jsonl").read_bytes() == canonical_jsonl_bytes(captures)
        assert (output / "extraction-records.jsonl").read_bytes() == canonical_jsonl_bytes(
            extractions
        )
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        assert (output / "manifest.json").read_bytes() == canonical_json_bytes(manifest)
        assert captures[0]["http_status_code"] == 200
        assert captures[0]["network_access_attempted"] is True
        assert captures[0]["content_hash"] == hashlib.sha256(b"remote fixture text\n").hexdigest()
        assert captures[0]["byte_count"] == len(b"remote fixture text\n")
        assert captures[0]["content_length_header"] == str(len(b"remote fixture text\n"))
        assert captures[0]["transient_payload_path"] is None
        assert captures[0]["payload_retention_policy"] == "hash_only"
        assert not (output / "payloads").exists()
        assert extractions[0]["capture_id"] == captures[0]["capture_id"]
        assert extractions[0]["status"] == "completed"
        assert (output / extractions[0]["extracted_text_path"]).read_text(
            encoding="utf-8"
        ) == "remote fixture text\n"
        assert FixtureHandler.request_paths == ["/text"]
    finally:
        server.shutdown()


def test_gate_pass_marks_unsupported_content_type_as_failed(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/binary"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(tmp_path, urls=[url], allowed_prefix=base_url)
        output = tmp_path / "remote-html-run"

        proc = run_executor(
            handoff=handoff, output=output, gate_request=gate_request, allow_network=True
        )

        assert proc.returncode == source_executor.EXIT_STATE_UNSAFE, proc.stdout + proc.stderr
        execution = json.loads((output / "execution-record.json").read_text(encoding="utf-8"))
        captures = load_jsonl(output / "capture-events.jsonl")
        extractions = load_jsonl(output / "extraction-records.jsonl")
        assert execution["status"] == "failed"
        assert execution["urls_attempted"] == 1
        assert execution["urls_succeeded"] == 1
        assert captures[0]["status"] == "completed"
        assert captures[0]["transient_payload_path"] == "payloads/capture-0001.bin"
        assert captures[0]["payload_retention_policy"] == "transient_run_artifact"
        assert (output / "payloads" / "capture-0001.bin").read_bytes() == b"\x00\x01\x02\x03"
        assert extractions[0]["status"] == "failed"
        assert extractions[0]["failure_reason"] == "unsupported_content_type"
        assert extractions[0]["extracted_text_path"] is None
        assert FixtureHandler.request_paths == ["/binary"]
    finally:
        server.shutdown()


def test_gate_pass_writes_exact_text_without_trailing_newline(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        body = b"remote fixture text"
        url = f"{base_url}/plain"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(tmp_path, urls=[url], allowed_prefix=base_url)
        output = tmp_path / "remote-plain-run"

        proc = run_executor(
            handoff=handoff, output=output, gate_request=gate_request, allow_network=True
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        extractions = load_jsonl(output / "extraction-records.jsonl")
        extracted_path = output / extractions[0]["extracted_text_path"]
        assert extracted_path.read_bytes() == body
        assert extractions[0]["content_hash"] == hashlib.sha256(body).hexdigest()
        assert extractions[0]["byte_count_out"] == len(body)

        validator_proc = subprocess.run(
            [sys.executable, str(VALIDATOR), str(output)],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        assert validator_proc.returncode == 0, validator_proc.stdout + validator_proc.stderr
    finally:
        server.shutdown()


def test_write_execution_artifacts_closes_files_before_validation(tmp_path: Path) -> None:
    output = tmp_path / "local-run"
    handoff_path = tmp_path / "handoff.jsonl"
    handoff_path.write_text("{}\n", encoding="utf-8")
    handoff_hash = hashlib.sha256(handoff_path.read_bytes()).hexdigest()
    payload_source = tmp_path / "payload-source.bin"
    payload_source.write_bytes(b"closed before validation\n")
    execution_record = source_executor.dry_run_execution_record(
        run_id="dry-run-close-check",
        created_at="2026-06-03T12:34:56Z",
        handoff_path=handoff_path,
        handoff_hash=handoff_hash,
        adapter_payload={"adapter_id": "runtime_local_file", "workspace_id": "alpha_subject"},
        adapter_type="local_source",
        executor_mode="local",
        local_input_paths=[],
        gate_report=None,
        planned_actions=[],
    )

    source_executor.write_execution_artifacts(
        output_dir=output,
        execution_record=execution_record,
        capture_events=[],
        extraction_records=[],
        denial_record=None,
        gate_report=None,
        text_artifacts={"extracted-text/extraction-0001.txt": "closed before validation\n"},
        binary_artifacts={"payloads/capture-0001.bin": payload_source},
    )

    assert open_targets_under(output) == []
    assert (output / "extracted-text" / "extraction-0001.txt").read_text(encoding="utf-8") == (
        "closed before validation\n"
    )
    assert (output / "payloads" / "capture-0001.bin").read_bytes() == b"closed before validation\n"

    validator_proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(output / "execution-record.json")],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert validator_proc.returncode == 0, validator_proc.stdout + validator_proc.stderr


def test_validator_detects_mutated_extracted_text_artifact(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/plain"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(tmp_path, urls=[url], allowed_prefix=base_url)
        output = tmp_path / "remote-plain-mismatch-run"

        proc = run_executor(
            handoff=handoff, output=output, gate_request=gate_request, allow_network=True
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        extractions = load_jsonl(output / "extraction-records.jsonl")
        extracted_path = output / extractions[0]["extracted_text_path"]
        extracted_path.write_text("remote fixture text\n", encoding="utf-8")

        validator_proc = subprocess.run(
            [sys.executable, str(VALIDATOR), str(output)],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        assert validator_proc.returncode == 1, validator_proc.stdout + validator_proc.stderr
        assert "EXTRACTED_TEXT_HASH_MISMATCH" in validator_proc.stdout
        assert "EXTRACTED_TEXT_BYTE_COUNT_MISMATCH" in validator_proc.stdout
    finally:
        server.shutdown()


def test_missing_allow_network_denies_without_fetch(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/text"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(tmp_path, urls=[url], allowed_prefix=base_url)
        output = tmp_path / "remote-denied-run"
        expected_handoff_hash = hashlib.sha256(handoff.read_bytes()).hexdigest()

        proc = run_executor(
            handoff=handoff, output=output, gate_request=gate_request, allow_network=False
        )

        assert proc.returncode != 0
        execution = json.loads((output / "execution-record.json").read_text(encoding="utf-8"))
        denial_record = json.loads((output / "denial-record.json").read_text(encoding="utf-8"))
        assert execution["status"] == "denied"
        assert execution["adapter_id"] == "runtime_remote_url_manifest"
        assert execution["input_handoff_hash"] == expected_handoff_hash
        assert execution["canonical_persistence_attempted"] is False
        assert execution["network_access_attempted"] is False
        assert (
            execution["network_access_denied_reason"]
            == "explicit --allow-network is required for remote execution"
        )
        assert denial_record["status"] == "denied"
        assert denial_record["adapter_id"] == "runtime_remote_url_manifest"
        assert denial_record["input_handoff_hash"] == expected_handoff_hash
        assert denial_record["canonical_persistence_attempted"] is False
        assert (
            denial_record["network_access_denied_reason"]
            == "explicit --allow-network is required for remote execution"
        )
        assert denial_record["considered_urls"] == [url]
        assert load_jsonl(output / "capture-events.jsonl") == []
        assert FixtureHandler.request_paths == []
    finally:
        server.shutdown()


def test_remote_dry_run_sets_no_canonical_persistence_and_no_payload_retention(
    tmp_path: Path,
) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/text"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(
            tmp_path, urls=[url], allowed_prefix=base_url, dry_run=True
        )
        output = tmp_path / "remote-dry-run"

        proc = run_executor(
            handoff=handoff,
            output=output,
            gate_request=gate_request,
            allow_network=True,
            dry_run=True,
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        execution = json.loads(proc.stdout)
        assert execution["status"] == "dry_run"
        assert execution["canonical_persistence_attempted"] is False
        assert execution["network_access_attempted"] is False
        assert execution["capture_event_count"] == 0
        assert execution["extraction_record_count"] == 0
        assert not output.exists()
    finally:
        server.shutdown()


def test_remote_dry_run_suppresses_execution_record_stdout_when_requested(
    tmp_path: Path,
) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/text"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(
            tmp_path, urls=[url], allowed_prefix=base_url, dry_run=True
        )
        output = tmp_path / "remote-dry-run-suppressed"

        proc = run_executor(
            handoff=handoff,
            output=output,
            gate_request=gate_request,
            allow_network=True,
            dry_run=True,
            suppress_execution_record_stdout=True,
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout == ""
        assert not output.exists()
    finally:
        server.shutdown()


def test_gate_refusal_denies_without_fetch(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/text"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(
            tmp_path, urls=[url], allowed_prefix="http://not-localhost.invalid/"
        )
        output = tmp_path / "remote-gate-refused-run"

        proc = run_executor(
            handoff=handoff, output=output, gate_request=gate_request, allow_network=True
        )

        assert proc.returncode != 0
        execution = json.loads((output / "execution-record.json").read_text(encoding="utf-8"))
        assert execution["status"] == "denied"
        assert execution["network_access_attempted"] is False
        assert execution["network_access_denied_reason"] == "network safety gate denied execution"
        assert FixtureHandler.request_paths == []
    finally:
        server.shutdown()


def test_remote_http_failure_records_attempt_without_successful_extraction(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/missing"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(tmp_path, urls=[url], allowed_prefix=base_url)
        output = tmp_path / "remote-404-run"

        proc = run_executor(
            handoff=handoff, output=output, gate_request=gate_request, allow_network=True
        )

        assert proc.returncode != 0
        execution = json.loads((output / "execution-record.json").read_text(encoding="utf-8"))
        captures = load_jsonl(output / "capture-events.jsonl")
        extractions = load_jsonl(output / "extraction-records.jsonl")
        assert execution["network_access_attempted"] is True
        assert execution["status"] == "failed"
        assert captures[0]["http_status_code"] == 404
        assert captures[0]["status"] == "failed"
        assert captures[0]["failure_reason"] == "http_status_404"
        assert extractions[0]["status"] == "failed"
        assert extractions[0]["failure_reason"] == "http_status_404"
    finally:
        server.shutdown()


def test_remote_http_429_records_hostile_status_without_retry(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/rate-limit"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(tmp_path, urls=[url], allowed_prefix=base_url)
        output = tmp_path / "remote-429-run"

        proc = run_executor(
            handoff=handoff, output=output, gate_request=gate_request, allow_network=True
        )

        assert proc.returncode != 0
        execution = json.loads((output / "execution-record.json").read_text(encoding="utf-8"))
        captures = load_jsonl(output / "capture-events.jsonl")
        extractions = load_jsonl(output / "extraction-records.jsonl")
        assert execution["status"] == "failed"
        assert execution["network_access_attempted"] is True
        assert captures[0]["status"] == "failed"
        assert captures[0]["failure_reason"] == "http_status_429"
        assert captures[0]["http_status_code"] == 429
        assert extractions[0]["status"] == "failed"
        assert extractions[0]["failure_reason"] == "http_status_429"
        assert FixtureHandler.request_paths == ["/rate-limit"]
    finally:
        server.shutdown()


def test_remote_timeout_seconds_applies_to_stalled_response(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/slow"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(tmp_path, urls=[url], allowed_prefix=base_url)
        output = tmp_path / "remote-timeout-run"

        proc = run_executor(
            handoff=handoff,
            output=output,
            gate_request=gate_request,
            allow_network=True,
            timeout_seconds=0.1,
        )

        assert proc.returncode != 0
        execution = json.loads((output / "execution-record.json").read_text(encoding="utf-8"))
        captures = load_jsonl(output / "capture-events.jsonl")
        extractions = load_jsonl(output / "extraction-records.jsonl")
        assert execution["status"] == "failed"
        assert execution["network_access_attempted"] is True
        assert captures[0]["status"] == "failed"
        assert captures[0]["failure_reason"] == "network_error:TimeoutError"
        assert extractions[0]["status"] == "failed"
        assert extractions[0]["failure_reason"] == "network_error:TimeoutError"
        assert FixtureHandler.request_paths == ["/slow"]
    finally:
        server.shutdown()


def test_remote_partial_failure_remains_coherent_and_validator_clean(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        urls = [f"{base_url}/text", f"{base_url}/missing"]
        handoff = make_handoff(tmp_path, urls)
        gate_request = make_gate_request(tmp_path, urls=urls, allowed_prefix=base_url)
        output = tmp_path / "remote-partial-failure-run"

        proc = run_executor(
            handoff=handoff, output=output, gate_request=gate_request, allow_network=True
        )

        assert proc.returncode != 0
        execution = json.loads((output / "execution-record.json").read_text(encoding="utf-8"))
        captures = load_jsonl(output / "capture-events.jsonl")
        extractions = load_jsonl(output / "extraction-records.jsonl")
        assert execution["status"] == "failed"
        assert execution["network_access_attempted"] is True
        assert execution["urls_attempted"] == 2
        assert execution["urls_succeeded"] == 1
        assert execution["urls_failed"] == 1
        assert captures[0]["status"] == "completed"
        assert captures[1]["status"] == "failed"
        assert extractions[0]["status"] == "completed"
        assert extractions[1]["status"] == "failed"
        assert captures[0]["transient_payload_path"] is None
        assert captures[0]["payload_retention_policy"] == "hash_only"
        assert not (output / "payloads").exists()
        assert captures[1]["failure_reason"] == "http_status_404"
        assert extractions[1]["failure_reason"] == "http_status_404"

        validator_proc = subprocess.run(
            [sys.executable, str(VALIDATOR), str(output)],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert validator_proc.returncode == 0, validator_proc.stdout + validator_proc.stderr
    finally:
        server.shutdown()


def test_redirect_inside_allowlist_is_captured(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/redirect"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(tmp_path, urls=[url], allowed_prefix=base_url)
        output = tmp_path / "remote-redirect-run"

        proc = run_executor(
            handoff=handoff, output=output, gate_request=gate_request, allow_network=True
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        captures = load_jsonl(output / "capture-events.jsonl")
        assert captures[0]["status"] == "completed"
        assert captures[0]["redirect_count"] == 1
        assert captures[0]["final_url"] == f"{base_url}/text"
        assert FixtureHandler.request_paths == ["/redirect", "/text"]
    finally:
        server.shutdown()


def test_redirect_with_lowercase_location_header_is_captured(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/redirect-lower"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(tmp_path, urls=[url], allowed_prefix=base_url)
        output = tmp_path / "remote-redirect-lower-run"

        proc = run_executor(
            handoff=handoff, output=output, gate_request=gate_request, allow_network=True
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        captures = load_jsonl(output / "capture-events.jsonl")
        assert captures[0]["status"] == "completed"
        assert captures[0]["redirect_count"] == 1
        assert captures[0]["final_url"] == f"{base_url}/text"
        assert FixtureHandler.request_paths == ["/redirect-lower", "/text"]
    finally:
        server.shutdown()


def test_redirect_outside_allowlist_is_not_followed(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/redirect-out"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(tmp_path, urls=[url], allowed_prefix=base_url)
        output = tmp_path / "remote-redirect-out-run"

        proc = run_executor(
            handoff=handoff, output=output, gate_request=gate_request, allow_network=True
        )

        assert proc.returncode != 0
        captures = load_jsonl(output / "capture-events.jsonl")
        assert captures[0]["status"] == "failed"
        assert captures[0]["failure_reason"] == "redirect_target_not_allowlisted"
        assert captures[0]["redirect_target"] == "http://example.invalid/outside"
        assert FixtureHandler.request_paths == ["/redirect-out"]
    finally:
        server.shutdown()


def test_remote_oversize_response_is_bounded_and_not_extracted(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/oversize"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(tmp_path, urls=[url], allowed_prefix=base_url)
        output = tmp_path / "remote-oversize-run"

        proc = run_executor(
            handoff=handoff,
            output=output,
            gate_request=gate_request,
            allow_network=True,
            max_response_bytes=4,
        )

        assert proc.returncode != 0
        execution = json.loads((output / "execution-record.json").read_text(encoding="utf-8"))
        captures = load_jsonl(output / "capture-events.jsonl")
        extractions = load_jsonl(output / "extraction-records.jsonl")
        assert execution["network_access_attempted"] is True
        assert captures[0]["status"] == "failed"
        assert captures[0]["failure_reason"] == "response_exceeds_max_bytes"
        assert captures[0]["byte_count"] == 0
        assert extractions[0]["status"] == "failed"
        assert extractions[0]["failure_reason"] == "response_exceeds_max_bytes"
    finally:
        server.shutdown()
