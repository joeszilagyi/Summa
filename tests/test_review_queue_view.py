import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tools.source_db_tools import review_queue

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "tools" / "scripts" / "build_review_queue_view.py"
CLI_TOOL = REPO_ROOT / "tools" / "source_db_tools" / "review_queue.py"


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
              confidence_score REAL,
              accepted_for_citation INTEGER NOT NULL DEFAULT 0,
              workspace_id TEXT,
              authority_level TEXT,
              public_blocker TEXT,
              record_last_updated TEXT
            );
            CREATE TABLE lead (
              lead_id INTEGER PRIMARY KEY,
              lead_kind TEXT,
              label_text TEXT,
              review_state TEXT,
              record_last_updated TEXT
            );
            CREATE TABLE source_claim (
              source_claim_id INTEGER PRIMARY KEY,
              claim_text TEXT NOT NULL,
              claim_type TEXT,
              review_state TEXT,
              confidence_score REAL,
              workspace_id TEXT,
              authority_level TEXT,
              public_blocker TEXT,
              record_last_updated TEXT
            );
            CREATE TABLE review_state_history (
              review_state_history_key_v1 TEXT PRIMARY KEY,
              target_namespace TEXT NOT NULL,
              target_id TEXT NOT NULL,
              previous_state TEXT,
              new_state TEXT NOT NULL,
              changed_by TEXT NOT NULL,
              changed_at TEXT NOT NULL,
              reason TEXT,
              note TEXT,
              source_namespace TEXT,
              source_id TEXT,
              source_tool TEXT,
              source_run_id TEXT,
              record_last_updated TEXT NOT NULL
            );
            CREATE TABLE provenance_event (
              provenance_event_id INTEGER PRIMARY KEY,
              provenance_event_key_v1 TEXT NOT NULL,
              object_namespace TEXT NOT NULL,
              object_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              actor_type TEXT,
              actor_id TEXT,
              actor_label TEXT,
              tool_name TEXT,
              tool_version TEXT,
              model_name TEXT,
              prompt_id TEXT,
              run_id TEXT,
              source_object_namespace TEXT,
              source_object_id TEXT,
              event_timestamp TEXT NOT NULL,
              confidence_score REAL,
              note_text TEXT,
              record_last_updated TEXT NOT NULL
            );
            INSERT INTO work (
              work_id, work_type, title, review_state, confidence_score,
              workspace_id, authority_level, public_blocker, record_last_updated
            ) VALUES
              (1, 'book', 'Pending Review Work', 'needs_review', 0.41, 'alpha_subject', 'primary', '', '2026-06-02T00:00:00Z'),
              (2, 'book', 'Accepted Work', 'accepted', 0.95, 'alpha_subject', 'primary', '', '2026-06-02T00:00:00Z'),
              (3, 'webpage', 'Proposed Work', 'proposed', 0.62, 'beta_subject', 'secondary', 'needs_public_evidence', '2026-06-02T00:00:00Z');
            INSERT INTO lead (
              lead_id, lead_kind, label_text, review_state, record_last_updated
            ) VALUES
              (1, 'source_lead', 'Pending source lead', 'needs_review', '2026-06-02T00:00:00Z');
            INSERT INTO source_claim (
              source_claim_id, claim_text, claim_type, review_state,
              confidence_score, workspace_id, authority_level, public_blocker,
              record_last_updated
            ) VALUES
              (1, 'Ambiguous claim text', 'factual', 'ambiguous', 0.25, 'alpha_subject', 'primary', 'authority_gap', '2026-06-02T00:00:00Z');
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


def run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI_TOOL), *args],
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
    assert payload["counts"]["by_object_type"] == {"claim": 1, "lead": 1, "work": 2}
    assert payload["counts"]["by_review_state"] == {"ambiguous": 1, "needs_review": 2, "proposed": 1}
    assert payload["counts"]["returned_items"] == 4
    assert payload["counts"]["total_items"] == 4
    assert payload["truncated"] is False
    refs = {item["object_ref"] for item in payload["items"]}
    assert refs == {"claim:1", "lead:1", "work:1", "work:3"}
    assert "work:2" not in refs


def test_review_queue_list_items_pushes_limit_into_each_target_query(tmp_path: Path) -> None:
    db = create_review_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    executed_sql: list[str] = []
    conn.set_trace_callback(executed_sql.append)
    try:
        rows = review_queue.list_review_items(conn, state="all", limit=1)
    finally:
        conn.close()

    assert [row["object_ref"] for row in rows] == ["claim:1"]
    target_selects = [
        sql
        for sql in executed_sql
        if ("FROM work" in sql or "FROM lead" in sql or "FROM source_claim" in sql)
    ]
    assert any("FROM work" in sql and "LIMIT 1" in sql for sql in target_selects)
    assert any("FROM lead" in sql and "LIMIT 1" in sql for sql in target_selects)
    assert any("FROM source_claim" in sql and "LIMIT 1" in sql for sql in target_selects)


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
    assert payload["filters"]["full_counts"] is False
    assert payload["counts"]["total_items"] == 2
    assert payload["counts"]["returned_items"] == 2
    assert payload["truncated"] is True
    assert [item["object_ref"] for item in payload["items"]] == ["work:1", "work:3"]


def test_review_queue_view_can_compute_full_counts_explicitly(tmp_path: Path) -> None:
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
            "--full-counts",
            "--format",
            "json",
        ]
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["filters"]["full_counts"] is True
    assert payload["counts"]["total_items"] == 3
    assert payload["counts"]["returned_items"] == 2
    assert payload["truncated"] is True
    assert [item["object_ref"] for item in payload["items"]] == ["work:1", "work:3"]


def test_review_queue_view_and_cli_share_workspace_authority_and_public_blocker_filters(tmp_path: Path) -> None:
    db = create_review_db(tmp_path)

    view_result = run_tool(
        [
            "--db",
            str(db),
            "--state",
            "all",
            "--workspace-id",
            "alpha_subject",
            "--authority-level",
            "primary",
            "--public-blocker",
            "any",
            "--format",
            "json",
        ]
    )
    cli_result = run_cli(
        [
            str(db),
            "list",
            "--state",
            "all",
            "--workspace-id",
            "alpha_subject",
            "--authority-level",
            "primary",
            "--public-blocker",
            "any",
            "--format",
            "json",
        ]
    )

    assert view_result.returncode == 0, view_result.stdout + view_result.stderr
    assert cli_result.returncode == 0, cli_result.stdout + cli_result.stderr

    view_payload = json.loads(view_result.stdout)
    cli_payload = json.loads(cli_result.stdout)

    assert view_payload["filters"]["workspace_id"] == "alpha_subject"
    assert view_payload["filters"]["authority_level"] == "primary"
    assert view_payload["filters"]["public_blocker"] == "any"
    assert view_payload["counts"]["by_workspace_id"] == {"alpha_subject": 1}
    assert view_payload["counts"]["by_authority_level"] == {"primary": 1}
    assert view_payload["counts"]["by_public_blocker"] == {"authority_gap": 1}
    assert [item["object_ref"] for item in view_payload["items"]] == ["claim:1"]
    assert [item["object_ref"] for item in cli_payload["items"]] == ["claim:1"]
    assert view_payload["items"][0]["available_actions"]["writer_surface"] == "tools/source_db_tools/review_queue.py"
    assert (
        view_payload["items"][0]["available_actions"]["commands"]["accept"]
        == "python3 tools/source_db_tools/review_queue.py __DB_PATH__ accept claim:1"
    )


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
    assert "workspace_id_filter=-" in result.stdout
    assert "authority_level_filter=-" in result.stdout
    assert "public_blocker_filter=-" in result.stdout
    assert "full_counts=false" in result.stdout
    assert "total_items=1" in result.stdout
    assert "returned_items=1" in result.stdout
    assert "truncated=true" in result.stdout
    assert "writer_surface=tools/source_db_tools/review_queue.py" in result.stdout
    assert "item[0].object_ref=work:1" in result.stdout
    assert "item[0].workspace_id=alpha_subject" in result.stdout
    assert "item[0].authority_level=primary" in result.stdout
    assert "item[0].public_blocker=-" in result.stdout


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


def test_review_queue_writer_records_history_and_provenance(tmp_path: Path) -> None:
    db = create_review_db(tmp_path)

    result = run_cli(
        [
            str(db),
            "accept",
            "work:1",
            "--changed-by",
            "reviewer.alex",
            "--reason",
            "validated against source",
            "--note",
            "ready for citation",
            "--run-id",
            "review-run-1",
            "--format",
            "json",
        ]
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["object_ref"] == "work:1"
    assert payload["previous_state"] == "needs_review"
    assert payload["new_state"] == "accepted"
    assert payload["dry_run"] is False

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        work_row = conn.execute("SELECT review_state, reviewed_by, accepted_for_citation FROM work WHERE work_id=1").fetchone()
    except sqlite3.OperationalError:
        work_row = conn.execute("SELECT review_state FROM work WHERE work_id=1").fetchone()
    history_row = conn.execute(
        "SELECT previous_state, new_state, changed_by, reason, note, source_tool, source_run_id FROM review_state_history"
    ).fetchone()
    provenance_row = conn.execute(
        "SELECT event_type, actor_type, actor_id, tool_name, run_id, note_text FROM provenance_event"
    ).fetchone()
    conn.close()

    assert work_row["review_state"] == "accepted"
    assert history_row["previous_state"] == "needs_review"
    assert history_row["new_state"] == "accepted"
    assert history_row["changed_by"] == "reviewer.alex"
    assert history_row["reason"] == "validated against source"
    assert history_row["note"] == "ready for citation"
    assert history_row["source_tool"] == "tools/source_db_tools/review_queue.py"
    assert history_row["source_run_id"] == "review-run-1"
    assert provenance_row["event_type"] == "reviewed"
    assert provenance_row["actor_type"] == "human"
    assert provenance_row["actor_id"] == "reviewer.alex"
    assert provenance_row["tool_name"] == "tools/source_db_tools/review_queue.py"
    assert provenance_row["run_id"] == "review-run-1"
    assert provenance_row["note_text"] == "ready for citation"


def test_review_queue_preserves_accepted_for_citation_on_later_demote(tmp_path: Path) -> None:
    db = create_review_db(tmp_path)

    accept_result = run_cli(
        [
            str(db),
            "accept",
            "work:2",
            "--changed-by",
            "reviewer.alex",
            "--run-id",
            "review-run-2-accept",
        ]
    )
    assert accept_result.returncode == 0, accept_result.stdout + accept_result.stderr

    demote_result = run_cli(
        [
            str(db),
            "demote",
            "work:2",
            "--changed-by",
            "reviewer.alex",
            "--reason",
            "follow-up review",
            "--run-id",
            "review-run-2-demote",
        ]
    )
    assert demote_result.returncode == 0, demote_result.stdout + demote_result.stderr

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        work_row = conn.execute(
            "SELECT review_state, accepted_for_citation FROM work WHERE work_id=2"
        ).fetchone()
    finally:
        conn.close()

    assert work_row["review_state"] == "demoted"
    assert int(work_row["accepted_for_citation"]) == 1


def test_review_queue_rolls_back_when_provenance_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = create_review_db(tmp_path)

    def fail_record_event(*args: object, **kwargs: object) -> None:
        raise RuntimeError("provenance write failed")

    monkeypatch.setattr(review_queue.provenance_events, "record_event", fail_record_event)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    with pytest.raises(RuntimeError, match="provenance write failed"):
        review_queue.change_review_state(
            conn,
            "work:1",
            new_state="accepted",
            changed_by="reviewer.alex",
            run_id="review-run-rollback",
        )

    try:
        work_row = conn.execute(
            "SELECT review_state, accepted_for_citation FROM work WHERE work_id=1"
        ).fetchone()
        history_count = conn.execute("SELECT COUNT(*) AS count FROM review_state_history").fetchone()["count"]
        provenance_count = conn.execute("SELECT COUNT(*) AS count FROM provenance_event").fetchone()["count"]
    finally:
        conn.close()

    assert work_row["review_state"] == "needs_review"
    assert int(work_row["accepted_for_citation"]) == 0
    assert history_count == 0
    assert provenance_count == 0
