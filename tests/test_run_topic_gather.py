from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "tools" / "scripts"
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
DRIVER_PATH = SCRIPTS_DIR / "run_topic_gather.py"
WRAPPER_PATH = SCRIPTS_DIR / "Index_Run_Gather.sh"
VALIDATOR_PATH = VALIDATORS_DIR / "validate_gather_candidate_batch.py"
VALIDATOR_WRAPPER_PATH = SCRIPTS_DIR / "validate_gather_candidate_batch.py"
COMMON_PATH = REPO_ROOT / "tools" / "common" / "llm_source_text_wrapper.py"
HOSTILE_SOURCE_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "topic_gather" / "hostile_source.txt"
FIXED_CREATED_AT = "2026-06-03T12:34:56Z"


def load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


driver = load_module(DRIVER_PATH, "run_topic_gather_for_tests")
validator = load_module(VALIDATOR_PATH, "validate_gather_candidate_batch_for_tests")
wrapper_common = load_module(COMMON_PATH, "llm_source_text_wrapper_for_gather_tests")


def load_domain_pack(pack_id: str) -> dict[str, object]:
    return json.loads((REPO_ROOT / "config" / "domain_packs" / f"{pack_id}.json").read_text(encoding="utf-8"))


def write_manifest(
    workspace_root: Path,
    *,
    domain_pack: str = "general.v1",
    enabled_facets: list[str] | None = None,
    query_families: list[str] | None = None,
) -> Path:
    pack = load_domain_pack(domain_pack)
    manifest_path = workspace_root / ".indexer" / "subject_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "subject-manifest.v1",
                "subject_id": f"{domain_pack.split('.', 1)[0]}.fixture",
                "display_name": f"{domain_pack} Fixture Subject",
                "domain_pack": domain_pack,
                "scope_statement": "Synthetic gather driver fixture manifest.",
                "languages": ["en"],
                "aliases": [],
                "disambiguation_terms": [],
                "excluded_senses": [],
                "enabled_facets": enabled_facets or list(pack["enabled_facets"]),
                "query_families": query_families or [pack["query_families"][0]],
                "public_export_default": False,
                "legacy_substrate_paths": [],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def run_driver(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DRIVER_PATH), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def run_wrapper(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(WRAPPER_PATH), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def batch_path_for(workspace_root: Path, run_id: str) -> Path:
    return workspace_root / "runs" / "gather" / run_id / "gather-candidate-batch.json"


def prompt_path_for(workspace_root: Path, run_id: str) -> Path:
    return workspace_root / "runs" / "gather" / run_id / "rendered-prompt.txt"


def assert_text_only_inside_wrapped_blocks(prompt_text: str, hostile_text: str) -> None:
    template = wrapper_common.load_template()
    parsed_blocks = wrapper_common.parse_wrapped_blocks(prompt_text, template=template)

    assert parsed_blocks
    assert hostile_text in parsed_blocks[0].source_text

    outside_segments: list[str] = []
    cursor = 0
    for block in parsed_blocks:
        outside_segments.append(prompt_text[cursor:block.start_offset])
        cursor = block.end_offset
    outside_segments.append(prompt_text[cursor:])
    outside_text = "".join(outside_segments)

    assert hostile_text not in outside_text

    lower_prompt = prompt_text.lower()
    lower_outside = outside_text.lower()
    for phrase in (
        "ignore previous instructions",
        "system prompt",
        "developer message",
        "run shell command",
    ):
        assert phrase not in lower_outside
        spans = [match.span() for match in re.finditer(re.escape(phrase), lower_prompt)]
        assert spans
        for start, end in spans:
            assert any(start >= block.start_offset and end <= block.end_offset for block in parsed_blocks)


def write_fake_codex(bin_dir: Path) -> Path:
    log_path = bin_dir / "codex.log"
    script_path = bin_dir / "codex"
    script_path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
log_path="${FAKE_CODEX_LOG:?}"
{
  printf 'argc=%s\\n' "$#"
  idx=1
  for arg in "$@"; do
    printf 'arg[%s]=%s\\n' "$idx" "$arg"
    idx=$((idx + 1))
  done
} > "$log_path"
printf '%s' "${FAKE_CODEX_OUTPUT:-FAKE CODEX OUTPUT}"
""",
        encoding="utf-8",
    )
    script_path.chmod(0o755)
    return log_path


def test_run_topic_gather_dry_run_is_deterministic(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, enabled_facets=["sources"])
    run_id = "deterministic-dry-run"

    args = [
        "--subject",
        str(manifest_path),
        "--workspace",
        str(workspace_root),
        "--facet",
        "sources",
        "--mode",
        "dry-run",
        "--run-id",
        run_id,
        "--created-at",
        FIXED_CREATED_AT,
    ]

    first = run_driver(args)
    assert first.returncode == 0, first.stdout + first.stderr
    batch_path = batch_path_for(workspace_root, run_id)
    prompt_path = prompt_path_for(workspace_root, run_id)
    first_batch = batch_path.read_text(encoding="utf-8")
    first_prompt = prompt_path.read_text(encoding="utf-8")

    second = run_driver(args)
    assert second.returncode == 0, second.stdout + second.stderr
    assert batch_path.read_text(encoding="utf-8") == first_batch
    assert prompt_path.read_text(encoding="utf-8") == first_prompt

    report, exit_code = validator.validate_gather_candidate_batch(batch_path)
    assert exit_code == validator.EXIT_PASS, report


def test_run_topic_gather_wraps_hostile_source_text_only_inside_wrapper(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, enabled_facets=["sources"])
    run_id = "hostile-wrapper"

    proc = run_driver(
        [
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--facet",
            "sources",
            "--mode",
            "dry-run",
            "--run-id",
            run_id,
            "--created-at",
            FIXED_CREATED_AT,
            "--source-text-file",
            str(HOSTILE_SOURCE_FIXTURE),
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    prompt_text = prompt_path_for(workspace_root, run_id).read_text(encoding="utf-8")
    hostile_text = HOSTILE_SOURCE_FIXTURE.read_text(encoding="utf-8")
    assert_text_only_inside_wrapped_blocks(prompt_text, hostile_text)


def test_all_general_active_gather_bundles_are_selectable(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, domain_pack="general.v1")
    pack = load_domain_pack("general.v1")

    for facet in pack["enabled_facets"]:
        run_id = f"bundle-{facet}"
        proc = run_driver(
            [
                "--subject",
                str(manifest_path),
                "--workspace",
                str(workspace_root),
                "--facet",
                facet,
                "--mode",
                "dry-run",
                "--run-id",
                run_id,
                "--created-at",
                FIXED_CREATED_AT,
            ]
        )
        assert proc.returncode == 0, facet + proc.stdout + proc.stderr
        payload = json.loads(batch_path_for(workspace_root, run_id).read_text(encoding="utf-8"))
        assert payload["prompt_bundle"]["bundle_id"] == pack["prompt_bundles"][f"gather.{facet}"]["bundle_id"]


def test_missing_prompt_file_fails_clearly(tmp_path: Path, monkeypatch, capsys) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, enabled_facets=["sources"])
    real_resolver = driver.resolve_subject_runtime.resolve_prompt_bundles

    def fake_resolver(pack: dict[str, object], facets: list[str]) -> dict[str, dict[str, object]]:
        resolved = real_resolver(pack, facets)
        resolved["sources"]["resolved_phase_template_files"] = {
            "01a": "tools/prompts/general/missing.prompt",
            "01r": "tools/prompts/general/general.sources.review.prompt",
        }
        return resolved

    monkeypatch.setattr(driver.resolve_subject_runtime, "resolve_prompt_bundles", fake_resolver)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(DRIVER_PATH),
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--facet",
            "sources",
            "--mode",
            "dry-run",
            "--run-id",
            "missing-prompt",
            "--created-at",
            FIXED_CREATED_AT,
        ],
    )

    exit_code = driver.main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "prompt file not found" in captured.err


def test_gather_candidate_batch_validator_accepts_driver_output_and_rejects_mutation(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, enabled_facets=["sources"])
    run_id = "validator-roundtrip"

    proc = run_driver(
        [
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--facet",
            "sources",
            "--mode",
            "dry-run",
            "--run-id",
            run_id,
            "--created-at",
            FIXED_CREATED_AT,
        ]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    batch_path = batch_path_for(workspace_root, run_id)
    validate_ok = subprocess.run(
        [sys.executable, str(VALIDATOR_WRAPPER_PATH), str(batch_path)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert validate_ok.returncode == validator.EXIT_PASS, validate_ok.stdout + validate_ok.stderr

    mutated_path = tmp_path / "invalid-gather-candidate-batch.json"
    payload = json.loads(batch_path.read_text(encoding="utf-8"))
    payload["mode"] = "live"
    mutated_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    validate_fail = subprocess.run(
        [sys.executable, str(VALIDATOR_WRAPPER_PATH), str(mutated_path)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert validate_fail.returncode == validator.EXIT_VALIDATION_FAILED
    assert "LIVE_ENGINE_INVOCATION_REQUIRED" in validate_fail.stdout


def test_run_topic_gather_live_mode_uses_llm_runner_bridge_and_stamps_output(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, enabled_facets=["timeline"])
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_log = write_fake_codex(fake_bin)
    run_id = "live-fake-codex"
    fake_output = "FAKE CODEX CANDIDATE OUTPUT"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["FAKE_CODEX_LOG"] = str(fake_log)
    env["FAKE_CODEX_OUTPUT"] = fake_output

    proc = run_driver(
        [
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--facet",
            "timeline",
            "--mode",
            "live",
            "--engine",
            "codex",
            "--run-id",
            run_id,
            "--created-at",
            FIXED_CREATED_AT,
        ],
        env=env,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    batch_path = batch_path_for(workspace_root, run_id)
    report, exit_code = validator.validate_gather_candidate_batch(batch_path)
    assert exit_code == validator.EXIT_PASS, report

    payload = json.loads(batch_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "live"
    assert payload["raw_engine_output"] == fake_output
    assert payload["engine_output_ref"]
    assert payload["candidates"][0]["candidate_type"] == "raw_candidate_text"

    stamped_text = Path(payload["provenance"]["stamped_output_path"]).read_text(encoding="utf-8")
    assert "GENERATED_BY: codex" in stamped_text
    log_text = fake_log.read_text(encoding="utf-8")
    assert "arg[1]=exec" in log_text
    assert "--skip-git-repo-check" in log_text
    assert "workspace-write" in log_text


def test_run_topic_gather_keeps_network_access_flag_false(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, enabled_facets=["sources"])
    run_id = "no-network"

    proc = run_driver(
        [
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--facet",
            "sources",
            "--mode",
            "dry-run",
            "--run-id",
            run_id,
            "--created-at",
            FIXED_CREATED_AT,
            "--source-text-file",
            str(HOSTILE_SOURCE_FIXTURE),
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(batch_path_for(workspace_root, run_id).read_text(encoding="utf-8"))
    assert payload["provenance"]["network_access_attempted"] is False
    assert payload["raw_engine_output"] is None
    assert payload["engine_output_ref"] is None


def test_parse_stamp_footer_uses_footer_delimiter_length() -> None:
    text = (
        "prefix text that includes a footer-like separator\n"
        "---\n"
        "not a real footer yet\n"
        "and more body text\n"
        "\n---\n"
        "RUN_META_VERSION: 1\n"
        "GENERATED_BY: codex\n"
        "MODEL: test-model\n"
        "PLACE: gather\n"
        "FACET: sources\n"
        "PHASE: 01a\n"
        "RUN_TS: 2026-06-03T123456Z\n"
    )

    parsed = driver.parse_stamp_footer(text)

    assert parsed == {
        "run_meta_version": "1",
        "generated_by": "codex",
        "model": "test-model",
        "place": "gather",
        "facet": "sources",
        "phase": "01a",
        "run_ts": "2026-06-03T123456Z",
    }


def test_index_run_gather_wrapper_help_and_dry_run(tmp_path: Path) -> None:
    help_result = run_wrapper(["--help"])
    assert help_result.returncode == 0, help_result.stdout + help_result.stderr
    assert "Resolve one subject runtime" in help_result.stdout

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, enabled_facets=["sources"])
    run_id = "wrapper-dry-run"
    dry_run_result = run_wrapper(
        [
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--facet",
            "sources",
            "--mode",
            "dry-run",
            "--run-id",
            run_id,
            "--created-at",
            FIXED_CREATED_AT,
        ]
    )

    assert dry_run_result.returncode == 0, dry_run_result.stdout + dry_run_result.stderr
    assert batch_path_for(workspace_root, run_id).is_file()
