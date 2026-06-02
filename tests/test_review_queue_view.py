import json
import sqlite3
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "tools" / "scripts" / "build_review_queue_view.py"


def create_review_db(tmp_path: Path) -> Path:
    db = tmp_path / "review.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE work (
              work_id INTEGER PRIMARY KEY,
              work_type TEXT,
              title TEXT,
              review_state TEXT,
              confidence_score REAL
            );
            CREATE TABLE lead (
              lead_id INTEGER PRIMARY KEY,
              lead_kind TEXT,
              label_text TEXT,
              review_state TEXT
            );
            CREATE TABLE source_claim (
              source_claim_id INTEGER PRIMARY KEY,
              claim_text TEXT NOT NULL,
              claim_type TEXT,
              review_state TEXT,
              confidence_score REAL
            );
            INSERT INTO work (
              work_id, work_type, title, review_state, confidence_score
            ) VALUES
              (1, 'book', 'Pending Review Work', 'needs_review', 0.41),
              (2, 'book', 'Accepted Work', 'accepted', 0.95),
              (3, 'webpage', 'Proposed Work', 'proposed', 0.62);
            INSERT INTO lead (
              lead_id, lead_kind, label_text, review_state
            ) VALUES
              (1, 'source_lead', 'Pending source lead', 'needs_review');
            INSERT INTO source_claim (
              source_claim_id, claim_text, claim_type, review_state,
              confidence_score
            ) VALUES
              (1, 'Ambiguous claim text', 'factual', 'ambiguous', 0.25);
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db


def run_tool(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_review_queue_view_reports_pending_items_and_counts(tmp_path: Path) -> None:
    db = create_review_db(tmp_path)

    result = run_tool(["--db", str(db), "--format", "json"])

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "review-queue.v1"
    assert payload["filters"]["state"] == "pending_non_accepted"
    assert payload["counts"] == {
        "by_object_type": {"claim": 1, "lead": 1, "work": 2},
        "by_review_state": {"ambiguous": 1, "needs_review": 2, "proposed": 1},
        "returned_items": 4,
        "total_items": 4,
    }
    assert payload["truncated"] is False
    refs = {item["object_ref"] for item in payload["items"]}
    assert refs == {"claim:1", "lead:1", "work:1", "work:3"}
    assert "work:2" not in refs


def test_review_queue_view_honors_state_type_and_limit_filters(tmp_path: Path) -> None:
    db = create_review_db(tmp_path)

    result = run_tool(
        [
            "--db",
            str(db),
            "--state",
            "all",
            "--object-type",
            "work",
            "--limit",
            "2",
            "--format",
            "json",
        ]
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["filters"]["state"] == "all"
    assert payload["filters"]["object_type"] == "work"
    assert payload["counts"]["total_items"] == 3
    assert payload["counts"]["returned_items"] == 2
    assert payload["truncated"] is True
    assert [item["object_ref"] for item in payload["items"]] == ["work:1", "work:3"]


def test_review_queue_view_text_output_is_stable(tmp_path: Path) -> None:
    db = create_review_db(tmp_path)

    result = run_tool(
        [
            "--db",
            str(db),
            "--state",
            "all",
            "--object-type",
            "work",
            "--limit",
            "1",
            "--format",
            "text",
        ]
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_version=review-queue.v1" in result.stdout
    assert "state_filter=all" in result.stdout
    assert "object_type_filter=work" in result.stdout
    assert "total_items=3" in result.stdout
    assert "returned_items=1" in result.stdout
    assert "truncated=true" in result.stdout
    assert "item[0].object_ref=work:1" in result.stdout


def test_review_queue_view_rejects_missing_database(tmp_path: Path) -> None:
    missing_db = tmp_path / "missing.sqlite"

    result = run_tool(["--db", str(missing_db)])

    assert result.returncode == 1
    assert "review database not found" in result.stderr


def test_review_queue_view_rejects_invalid_limit(tmp_path: Path) -> None:
    db = create_review_db(tmp_path)

    result = run_tool(["--db", str(db), "--limit", "-1"])

    assert result.returncode == 1
    assert "limit must be non-negative" in result.stderr
