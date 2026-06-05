from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from tools.scripts import execute_source_adapter as source_executor


REPO_ROOT = Path(__file__).resolve().parents[1]
EXECUTOR = REPO_ROOT / "tools" / "scripts" / "execute_source_adapter.py"
VALIDATOR = REPO_ROOT / "tools" / "validators" / "validate_source_acquisition_execution.py"
PLANNER = REPO_ROOT / "tools" / "scripts" / "plan_remote_url_manifest_adapter.py"
ADAPTER = REPO_ROOT / "tests" / "fixtures" / "source_adapter_runtime" / "remote_url_manifest" / "source_adapter.json"


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
        elif self.path == "/redirect-out":
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/outside")
            self.end_headers()
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


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def make_handoff(tmp_path: Path, urls: list[str]) -> Path:
    manifest = tmp_path / "remote-manifest.jsonl"
    manifest.write_text(
        "".join(json.dumps({"url": url, "title": f"entry {index}"}) + "\n" for index, url in enumerate(urls, start=1)),
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


def make_gate_request(tmp_path: Path, *, urls: list[str], allowed_prefix: str, dry_run: bool = False, max_actions: int | None = None) -> Path:
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
    max_response_bytes: int | None = None,
) -> subprocess.CompletedProcess[str]:
    args = [
        sys.executable,
        str(EXECUTOR),
        "--handoff",
        str(handoff),
        "--output",
        str(output),
        "--mode",
        "remote",
        "--network-safety-request",
        str(gate_request),
        "--run-id",
        output.name,
        "--created-at",
        "2026-06-03T12:34:56Z",
        "--timeout-seconds",
        "2",
    ]
    if max_response_bytes is not None:
        args.extend(["--max-response-bytes", str(max_response_bytes)])
    if allow_network:
        args.append("--allow-network")
    return subprocess.run(args, cwd=REPO_ROOT, text=True, capture_output=True, check=False)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_execute_remote_fetches_emits_denied_evidence_rows(tmp_path: Path) -> None:
    server, base_url = fixture_server()
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
            )
        )

        assert failed is True
        assert summary["urls_attempted"] == 1
        assert summary["urls_succeeded"] == 1
        assert summary["urls_denied"] == 2
        assert [record["status"] for record in capture_events] == ["completed", "denied", "denied"]
        assert [record["status"] for record in extraction_records] == ["completed", "denied", "denied"]
        assert capture_events[1]["failure_reason"] == "network_gate_action_missing"
        assert capture_events[2]["failure_reason"] == "unsupported_request_method"
        assert extraction_records[1]["failure_reason"] == "network_gate_action_missing"
        assert extraction_records[2]["failure_reason"] == "unsupported_request_method"
        assert text_artifacts["extracted-text/extraction-0001.txt"] == "remote fixture text\n"
        assert "payloads/capture-0001.bin" in binary_artifacts
    finally:
        server.shutdown()


def test_remote_executor_marks_denied_only_runs_as_network_attempted(tmp_path: Path, monkeypatch) -> None:
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
            {"url": "https://example.test/one", "status": "refused", "method": "GET"},
            {"url": "https://example.test/two", "status": "refused", "method": "GET"},
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

    execution_record, denial_record, capture_events, extraction_records, _, _ = source_executor.execute_remote_url_manifest(
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
    )

    assert denial_record is None
    assert execution_record["network_access_attempted"] is True
    assert execution_record["urls_denied"] == 2
    assert capture_events[0]["status"] == "denied"
    assert extraction_records[0]["status"] == "denied"


def test_gate_pass_with_explicit_opt_in_fetches_text_and_extracts(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/text"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(tmp_path, urls=[url], allowed_prefix=base_url)
        output = tmp_path / "remote-text-run"

        proc = run_executor(handoff=handoff, output=output, gate_request=gate_request, allow_network=True)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        execution = json.loads((output / "execution-record.json").read_text(encoding="utf-8"))
        captures = load_jsonl(output / "capture-events.jsonl")
        extractions = load_jsonl(output / "extraction-records.jsonl")
        assert execution["network_access_attempted"] is True
        assert execution["network_access_allowed"] is True
        assert execution["remote_live_fetch_enabled"] is True
        assert execution["urls_attempted"] == 1
        assert execution["urls_succeeded"] == 1
        assert captures[0]["http_status_code"] == 200
        assert captures[0]["network_access_attempted"] is True
        assert captures[0]["content_hash"] == hashlib.sha256(b"remote fixture text\n").hexdigest()
        assert captures[0]["byte_count"] == len(b"remote fixture text\n")
        assert captures[0]["content_length_header"] == str(len(b"remote fixture text\n"))
        assert captures[0]["transient_payload_path"] == "payloads/capture-0001.bin"
        assert (output / captures[0]["transient_payload_path"]).read_bytes() == b"remote fixture text\n"
        assert extractions[0]["capture_id"] == captures[0]["capture_id"]
        assert extractions[0]["status"] == "completed"
        assert (output / extractions[0]["extracted_text_path"]).read_text(encoding="utf-8") == "remote fixture text\n"
        assert FixtureHandler.request_paths == ["/text"]
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

        proc = run_executor(handoff=handoff, output=output, gate_request=gate_request, allow_network=True)

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


def test_validator_detects_mutated_extracted_text_artifact(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/plain"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(tmp_path, urls=[url], allowed_prefix=base_url)
        output = tmp_path / "remote-plain-mismatch-run"

        proc = run_executor(handoff=handoff, output=output, gate_request=gate_request, allow_network=True)

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

        proc = run_executor(handoff=handoff, output=output, gate_request=gate_request, allow_network=False)

        assert proc.returncode != 0
        execution = json.loads((output / "execution-record.json").read_text(encoding="utf-8"))
        assert execution["status"] == "denied"
        assert execution["network_access_attempted"] is False
        assert execution["network_access_denied_reason"] == "explicit --allow-network is required for remote execution"
        assert load_jsonl(output / "capture-events.jsonl") == []
        assert FixtureHandler.request_paths == []
    finally:
        server.shutdown()


def test_gate_refusal_denies_without_fetch(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/text"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(tmp_path, urls=[url], allowed_prefix="http://not-localhost.invalid/")
        output = tmp_path / "remote-gate-refused-run"

        proc = run_executor(handoff=handoff, output=output, gate_request=gate_request, allow_network=True)

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

        proc = run_executor(handoff=handoff, output=output, gate_request=gate_request, allow_network=True)

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


def test_redirect_inside_allowlist_is_captured(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/redirect"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(tmp_path, urls=[url], allowed_prefix=base_url)
        output = tmp_path / "remote-redirect-run"

        proc = run_executor(handoff=handoff, output=output, gate_request=gate_request, allow_network=True)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        captures = load_jsonl(output / "capture-events.jsonl")
        assert captures[0]["status"] == "completed"
        assert captures[0]["redirect_count"] == 1
        assert captures[0]["final_url"] == f"{base_url}/text"
        assert FixtureHandler.request_paths == ["/redirect", "/text"]
    finally:
        server.shutdown()


def test_redirect_outside_allowlist_is_not_followed(tmp_path: Path) -> None:
    server, base_url = fixture_server()
    try:
        url = f"{base_url}/redirect-out"
        handoff = make_handoff(tmp_path, [url])
        gate_request = make_gate_request(tmp_path, urls=[url], allowed_prefix=base_url)
        output = tmp_path / "remote-redirect-out-run"

        proc = run_executor(handoff=handoff, output=output, gate_request=gate_request, allow_network=True)

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
