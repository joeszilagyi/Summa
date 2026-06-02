from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
VALIDATOR_PATH = VALIDATORS_DIR / "validate_source_locus_jsonl.py"
FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "validators"
    / "source_locus_jsonl"
    / "valid_minimal"
    / "inputs"
    / "DemoTopic_source_loci.jsonl"
)

if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

spec = importlib.util.spec_from_file_location("source_locus_validator_for_tests", VALIDATOR_PATH)
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validator)


def base_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "locus_id": "locus:test_topic:archive:example",
        "topic_id": "test_topic",
        "display_name": "Example Archive",
        "locus_type": "archive",
        "query_family": "archives",
        "parent_locus_id": None,
        "parent_org_id": None,
        "jurisdiction_place_id": None,
        "languages": ["en"],
        "time_coverage_start": None,
        "time_coverage_end": None,
        "access_class": "public_catalog_or_web",
        "access_url": None,
        "catalog_url": "https://example.org/catalog",
        "archive_url": None,
        "access_notes": "Fixture only.",
        "rights_posture": "metadata",
        "refetchability_status": "not_checked",
        "discovery_method": "manual_seed",
        "discovery_source": "unit_test",
        "discovered_at": "2026-04-28T00:00:00+00:00",
        "discovered_by": "pytest",
        "confidence_score": 0.8,
        "review_state": "accepted",
        "productivity_queries_run": 0,
        "productivity_leads_returned": 0,
        "productivity_unique_leads": 0,
        "productivity_captures_made": 0,
        "productivity_works_promoted": 0,
        "productivity_score": 0.0,
        "last_queried_at": None,
        "last_productive_at": None,
        "cooldown_until": None,
        "is_deprecated": False,
        "deprecation_reason": None,
        "notes": None,
    }
    record.update(overrides)
    return record


def write_jsonl(path: Path, *records: dict[str, object]) -> None:
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def write_raw_jsonl(path: Path, *lines: str) -> None:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def validate(path: Path) -> tuple[dict[str, object], int]:
    return validator.validate_source_locus_jsonl(path)


def test_valid_source_locus_fixture_passes() -> None:
    result, exit_code = validate(FIXTURE)
    assert exit_code == validator.EXIT_PASS
    assert result["counts"] == {"inspected": 2, "accepted": 2, "rejected": 0, "deferred": 1}
    assert result["errors"] == []
    assert result["warnings"][0]["code"] == "SOURCE_LOCUS_REVIEW_NEEDED"


def test_source_locus_validator_rejects_duplicate_locus_id(tmp_path: Path) -> None:
    target = tmp_path / "Test_source_loci.jsonl"
    write_jsonl(target, base_record(), base_record(display_name="Duplicate Archive"))
    result, exit_code = validate(target)
    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "SOURCE_LOCUS_ID_DUPLICATE"


def test_source_locus_validator_rejects_invalid_query_family(tmp_path: Path) -> None:
    target = tmp_path / "Test_source_loci.jsonl"
    write_jsonl(target, base_record(query_family="broad_crawling"))
    result, exit_code = validate(target)
    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "SOURCE_LOCUS_QUERY_FAMILY_INVALID"


def test_source_locus_validator_rejects_invalid_url(tmp_path: Path) -> None:
    target = tmp_path / "Test_source_loci.jsonl"
    write_jsonl(target, base_record(access_url="ftp://example.org/source"))
    result, exit_code = validate(target)
    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "SOURCE_LOCUS_URL_INVALID"


def test_source_locus_validator_rejects_deprecated_without_reason(tmp_path: Path) -> None:
    target = tmp_path / "Test_source_loci.jsonl"
    write_jsonl(target, base_record(is_deprecated=True, review_state="deprecated"))
    result, exit_code = validate(target)
    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "SOURCE_LOCUS_DEPRECATION_REASON_MISSING"


def test_source_locus_validator_rejects_unknown_locus_without_fallback_id(tmp_path: Path) -> None:
    target = tmp_path / "Test_source_loci.jsonl"
    write_jsonl(target, base_record(locus_id="locus:test_topic:unknown:plain", locus_type="unknown", query_family="unknown"))
    result, exit_code = validate(target)
    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "SOURCE_LOCUS_UNKNOWN_ID_INVALID"


def test_source_locus_validator_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    target = tmp_path / "Test_source_loci.jsonl"
    raw_line = json.dumps(base_record(), sort_keys=True)
    raw_line = raw_line.replace(
        '"review_state": "accepted"',
        '"review_state": "rejected", "review_state": "accepted"',
        1,
    )
    write_raw_jsonl(target, raw_line)

    result, exit_code = validate(target)

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "DUPLICATE_JSON_KEY"
    assert result["errors"][0]["line"] == 1
    assert result["errors"][0]["message"] == "duplicate JSON object key: review_state"


def test_source_locus_validator_rejects_nonstandard_json_constants(tmp_path: Path) -> None:
    target = tmp_path / "Test_source_loci.jsonl"
    raw_line = json.dumps(base_record(), sort_keys=True)
    raw_line = raw_line.replace('"confidence_score": 0.8', '"confidence_score": NaN', 1)
    write_raw_jsonl(target, raw_line)

    result, exit_code = validate(target)

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "JSONL_PARSE_ERROR"
    assert result["errors"][0]["line"] == 1
    assert result["errors"][0]["message"] == "invalid JSON syntax: invalid JSON constant NaN"
