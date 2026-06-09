import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from tools.scripts import build_review_queue_view as review_queue_view
from tools.source_db_tools import review_queue

REPO_ROOT = Path(__file__).resolve().parents[1]


def create_review_core_db(tmp_path: Path) -> Path:
    db = tmp_path / "review-core.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE lead (
              lead_id INTEGER PRIMARY KEY,
              lead_kind TEXT,
              label_text TEXT,
              review_state TEXT,
              record_last_updated TEXT
            );
            CREATE TABLE work (
              work_id INTEGER PRIMARY KEY,
              work_type TEXT,
              title TEXT,
              review_state TEXT,
              confidence_score REAL,
              accepted_for_citation INTEGER NOT NULL DEFAULT 0,
              reviewed_by TEXT,
              reviewed_at TEXT,
              promotion_state TEXT,
              workspace_id TEXT,
              authority_level TEXT,
              public_blocker TEXT,
              record_last_updated TEXT
            );
            CREATE TABLE work_identifier (
              work_identifier_id INTEGER PRIMARY KEY,
              scheme TEXT,
              value TEXT,
              review_state TEXT,
              record_last_updated TEXT
            );
            CREATE TABLE authority_identifier (
              authority_identifier_id INTEGER PRIMARY KEY,
              scheme TEXT,
              value TEXT,
              review_state TEXT,
              record_last_updated TEXT
            );
            CREATE TABLE authority_record (
              authority_record_id INTEGER PRIMARY KEY,
              preferred_label TEXT,
              authority_type TEXT,
              authority_status TEXT,
              review_state TEXT,
              record_last_updated TEXT
            );
            CREATE TABLE work_subject (
              work_subject_id INTEGER PRIMARY KEY,
              source_note TEXT,
              subject_role TEXT,
              review_state TEXT,
              record_last_updated TEXT
            );
            CREATE TABLE extraction_highlight (
              highlight_id INTEGER PRIMARY KEY,
              text_excerpt TEXT,
              review_state TEXT,
              record_last_updated TEXT
            );
            CREATE TABLE extraction_detected_entity (
              detected_entity_id INTEGER PRIMARY KEY,
              entity_label TEXT,
              entity_type TEXT,
              review_state TEXT,
              workspace_id TEXT,
              authority_tier TEXT,
              public_blocked INTEGER NOT NULL DEFAULT 0,
              record_last_updated TEXT
            );
            CREATE TABLE source_relationship (
              source_relationship_id INTEGER PRIMARY KEY,
              predicate TEXT,
              target_label TEXT,
              to_object_ref TEXT,
              review_state TEXT,
              workspace_id TEXT,
              authority_level TEXT,
              public_blocker TEXT,
              record_last_updated TEXT
            );
            CREATE TABLE source_claim (
              source_claim_id INTEGER PRIMARY KEY,
              claim_text TEXT,
              claim_type TEXT,
              review_state TEXT,
              confidence_score REAL,
              workspace_id TEXT,
              authority_level TEXT,
              public_blocker TEXT,
              record_last_updated TEXT
            );
            CREATE TABLE topic_extension (
              topic_extension_id INTEGER PRIMARY KEY,
              topic_id TEXT,
              extension_type TEXT,
              review_state TEXT,
              record_last_updated TEXT
            );
            CREATE TABLE source_access (
              source_access_id INTEGER PRIMARY KEY,
              original_locator TEXT,
              review_state TEXT,
              workspace_id TEXT,
              authority_tier TEXT,
              public_blocked INTEGER NOT NULL DEFAULT 0,
              retention_policy_id TEXT,
              record_last_updated TEXT
            );
            CREATE TABLE capture_event (
              capture_event_id INTEGER PRIMARY KEY,
              capture_method TEXT,
              review_state TEXT,
              authority_status TEXT,
              publication_state TEXT,
              retention_policy_id TEXT,
              record_last_updated TEXT
            );
            CREATE TABLE extraction_record (
              extraction_id INTEGER PRIMARY KEY,
              summary_short TEXT,
              review_state TEXT,
              retention_policy_id TEXT,
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
            INSERT INTO lead VALUES
              (1, 'source_lead', 'Pending source lead', 'needs_review', '2026-06-02T00:00:00Z');
            INSERT INTO work VALUES
              (1, 'book', 'Blocked Work', 'needs_review', 0.40, 0, NULL, NULL, NULL, 'alpha_subject', 'primary', 'blocked', '2026-06-02T00:00:00Z'),
              (2, 'book', 'Open Work', 'needs_review', 0.45, 0, NULL, NULL, NULL, 'alpha_subject', 'primary', '', '2026-06-02T00:00:00Z'),
              (3, 'book', 'Accepted Work', 'accepted', 0.95, 1, NULL, NULL, NULL, 'alpha_subject', 'primary', '', '2026-06-02T00:00:00Z');
            INSERT INTO work_identifier VALUES
              (1, 'isbn', '123', 'needs_review', '2026-06-02T00:00:00Z');
            INSERT INTO authority_identifier VALUES
              (1, 'viaf', '42', 'needs_review', '2026-06-02T00:00:00Z');
            INSERT INTO authority_record VALUES
              (1, 'Some Authority', 'person', 'verified', 'needs_review', '2026-06-02T00:00:00Z');
            INSERT INTO work_subject VALUES
              (1, 'topic note', 'author', 'proposed', '2026-06-02T00:00:00Z');
            INSERT INTO extraction_highlight VALUES
              (1, 'Highlight text', 'ambiguous', '2026-06-02T00:00:00Z');
            INSERT INTO extraction_detected_entity VALUES
              (1, 'Entity', 'person', 'needs_review', 'alpha_subject', 'gold', 0, '2026-06-02T00:00:00Z');
            INSERT INTO source_relationship VALUES
              (1, 'related_to', 'Target', 'work:1', 'needs_review', 'alpha_subject', 'primary', 'blocked', '2026-06-02T00:00:00Z');
            INSERT INTO source_claim VALUES
              (1, 'Claim', 'factual', 'ambiguous', 0.20, 'alpha_subject', 'primary', 'authority_gap', '2026-06-02T00:00:00Z');
            INSERT INTO topic_extension VALUES
              (1, 'topic:1', 'subtopic', 'proposed', '2026-06-02T00:00:00Z');
            INSERT INTO source_access VALUES
              (1, 'loc1', 'needs_review', 'alpha_subject', 'gold', 1, 'rp-source', '2026-06-02T00:00:00Z'),
              (2, 'loc2', 'needs_review', 'alpha_subject', 'gold', 0, 'rp-source', '2026-06-02T00:00:00Z');
            INSERT INTO capture_event VALUES
              (1, 'scanner', 'needs_review', 'gold', 'blocked', 'rp-capture', '2026-06-02T00:00:00Z'),
              (2, 'scanner', 'needs_review', 'gold', 'released', 'rp-capture', '2026-06-02T00:00:00Z');
            INSERT INTO extraction_record VALUES
              (1, 'short', 'needs_review', 'rp-extract', '2026-06-02T00:00:00Z');
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_review_queue_core_helpers_and_error_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = create_review_core_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(FileNotFoundError, match="review database does not exist"):
            review_queue.connect(tmp_path / "missing.sqlite")
        with pytest.raises(ValueError, match="review database path is not a file"):
            review_queue.connect(tmp_path)

        assert review_queue.canonical_type("Work") == "work"
        assert review_queue.canonical_type("source-lead") == "lead"
        assert review_queue.expand_object_type(None)[0] == "lead"
        assert review_queue.expand_object_type("identifier") == ["work_identifier", "authority_identifier"]
        assert review_queue.pending_filter_sql(None)[0].startswith("COALESCE(review_state, '') NOT IN")
        assert review_queue.pending_filter_sql("all") == ("1=1", [])
        assert review_queue.pending_filter_sql("needs_review") == ("COALESCE(review_state, '') = ?", ["needs_review"])
        assert review_queue.optional_column_expr({"authority_tier", "workspace_id"}, "workspace_id", "authority_tier") == "workspace_id"
        assert review_queue.optional_column_expr(set(), "workspace_id", "authority_tier") == "NULL"
        assert review_queue.public_blocker_expr({"public_blocker"}) == "NULLIF(public_blocker, '')"
        assert "public_blocked" in review_queue.public_blocker_expr({"public_blocked"})
        assert "publication_state" in review_queue.public_blocker_expr({"publication_state"})
        assert review_queue.public_blocker_expr(set()) == "NULL"
        where, params = review_queue.apply_optional_filter("1=1", [], "workspace_id", None)
        assert where == "1=1" and params == []
        where, params = review_queue.apply_optional_filter("1=1", [], "NULL", "alpha")
        assert where == "0=1" and params == []
        where, params = review_queue.apply_optional_filter("1=1", [], "workspace_id", "any")
        assert where == "(1=1) AND workspace_id IS NOT NULL" and params == []
        where, params = review_queue.apply_optional_filter("1=1", [], "workspace_id", "none")
        assert where == "(1=1) AND workspace_id IS NULL" and params == []
        where, params = review_queue.apply_optional_filter("1=1", [], "workspace_id", "alpha")
        assert where == "(1=1) AND workspace_id = ?" and params == ["alpha"]
        assert review_queue.review_item_sort_key({"confidence_score": None, "object_type": "x", "object_pk": 2}) > review_queue.review_item_sort_key({"confidence_score": 0.5, "object_type": "x", "object_pk": 1})
        assert review_queue.promotion_state_for_review("accepted") == "accepted_for_citation"
        assert review_queue.promotion_state_for_review("rejected") == "rejected"
        assignments, params = review_queue.review_outcome_update_sql(
            {"reviewed_by", "reviewed_at", "accepted_for_citation", "promotion_state"},
            new_state="accepted",
            changed_by="tester",
            changed_at="2026-06-02T00:00:00Z",
        )
        assert assignments == [
            "review_state=?",
            "record_last_updated=?",
            "reviewed_by=?",
            "reviewed_at=?",
            "accepted_for_citation=MAX(COALESCE(accepted_for_citation, 0), ?)",
            "promotion_state=?",
        ]
        assert params == ["accepted", "2026-06-02T00:00:00Z", "tester", "2026-06-02T00:00:00Z", 1, "accepted_for_citation"]

        review_queue.render_json({"x": 1})
        assert capsys.readouterr().out.strip() == '{\n  "x": 1\n}'
        review_queue.render_list_text(
            [
                {
                    "object_ref": "work:1",
                    "review_state": "needs_review",
                    "confidence_score": 0.4,
                    "source_type": "book",
                    "workspace_id": "alpha",
                    "authority_level": "primary",
                    "public_blocker": "blocked",
                    "label": "Blocked Work",
                }
            ]
        )
        assert "work:1\tneeds_review\t0.40\tbook\talpha\tprimary\tblocked\tBlocked Work" in capsys.readouterr().out

        parser = review_queue.build_arg_parser()
        assert parser is not None

        pending_rows = review_queue.list_review_items(conn)
        refs = {row["object_ref"] for row in pending_rows}
        assert "work:3" not in refs
        assert {"lead:1", "work:1", "work:2", "source_access:1", "source_access:2"}.issubset(refs)

        limited_rows = review_queue.list_review_items(conn, state="all", limit=3)
        assert len(limited_rows) == 3

        retention_rows = review_queue.list_review_items(conn, object_type="retention_override", state="all")
        assert {row["object_ref"] for row in retention_rows} == {"source_access:1", "source_access:2", "capture_event:1", "capture_event:2", "extraction_record:1"}

        work_any = review_queue.list_review_items(
            conn,
            object_type="work",
            state="all",
            min_confidence=0.3,
            max_confidence=0.5,
            source_type="book",
            workspace_id="alpha_subject",
            authority_level="primary",
            public_blocker="any",
        )
        assert [row["object_ref"] for row in work_any] == ["work:1"]

        work_none = review_queue.list_review_items(
            conn,
            object_type="work",
            state="all",
            min_confidence=0.3,
            max_confidence=0.5,
            source_type="book",
            workspace_id="alpha_subject",
            authority_level="primary",
            public_blocker="none",
        )
        assert [row["object_ref"] for row in work_none] == ["work:2"]

        source_access_none = review_queue.list_review_items(
            conn,
            object_type="source_access",
            state="all",
            public_blocker="none",
        )
        assert [row["object_ref"] for row in source_access_none] == ["source_access:2"]

        capture_any = review_queue.list_review_items(
            conn,
            object_type="capture_event",
            state="all",
            public_blocker="any",
        )
        assert [row["object_ref"] for row in capture_any] == ["capture_event:1"]

        capture_none = review_queue.list_review_items(
            conn,
            object_type="capture_event",
            state="all",
            public_blocker="none",
        )
        assert [row["object_ref"] for row in capture_none] == ["capture_event:2"]

        no_rows = review_queue.list_review_items(
            conn,
            object_type="extraction_record",
            state="all",
            workspace_id="alpha_subject",
        )
        assert no_rows == []

        light = review_queue.fetch_review_object(conn, "work:1")
        full = review_queue.fetch_review_object(conn, "work:1", full_row=True)
        assert light["object_ref"] == "work:1"
        assert full["work_id"] == 1
        with pytest.raises(ValueError, match="review object not found"):
            review_queue.fetch_review_object(conn, "work:999")

        dry_run = review_queue.change_review_state(
            conn,
            "work:2",
            new_state="accepted",
            changed_by="tester",
            reason="validated",
            note="ready",
            run_id="run-1",
            dry_run=True,
        )
        assert dry_run["dry_run"] is True
        assert dry_run["previous_state"] == "needs_review"

        accept = review_queue.change_review_state(
            conn,
            "work:2",
            new_state="accepted",
            changed_by="tester",
            reason="validated",
            note="ready",
            run_id="run-1",
        )
        assert accept["dry_run"] is False

        demote = review_queue.change_review_state(
            conn,
            "work:2",
            new_state="demoted",
            changed_by="tester",
            reason="follow up",
            run_id="run-2",
        )
        assert demote["previous_state"] == "accepted"

        work_row = conn.execute(
            "SELECT review_state, accepted_for_citation, reviewed_by, reviewed_at, promotion_state FROM work WHERE work_id=2"
        ).fetchone()
        history_count = conn.execute("SELECT COUNT(*) AS count FROM review_state_history").fetchone()["count"]
        provenance_count = conn.execute("SELECT COUNT(*) AS count FROM provenance_event").fetchone()["count"]
        assert work_row["review_state"] == "demoted"
        assert int(work_row["accepted_for_citation"]) == 1
        assert work_row["reviewed_by"] == "tester"
        assert work_row["promotion_state"] == "demoted"
        assert history_count >= 2
        assert provenance_count >= 2

        with pytest.raises(ValueError, match="unsupported review state transition"):
            review_queue.change_review_state(conn, "work:2", new_state="invalid")
        with pytest.raises(ValueError, match="review object not found"):
            review_queue.change_review_state(conn, "work:999", new_state="accepted")

        def fail_record_event(*args: object, **kwargs: object) -> None:
            raise RuntimeError("provenance write failed")

        monkeypatch.setattr(review_queue.provenance_events, "record_event", fail_record_event)
        with pytest.raises(RuntimeError, match="provenance write failed"):
            review_queue.change_review_state(
                conn,
                "work:1",
                new_state="accepted",
                changed_by="tester",
                run_id="run-rollback",
            )
        rollback_row = conn.execute(
            "SELECT review_state, accepted_for_citation FROM work WHERE work_id=1"
        ).fetchone()
        assert rollback_row["review_state"] == "needs_review"
        assert int(rollback_row["accepted_for_citation"]) == 0
    finally:
        conn.close()


def test_review_queue_rejects_invalid_target_identifiers_and_table_presence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = create_review_core_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        monkeypatch.setitem(
            review_queue.TARGETS,
            "work",
            review_queue.ReviewTarget("work", "work bad", "work_id"),
        )
        with pytest.raises(ValueError, match="invalid review target table"):
            review_queue.fetch_review_object(conn, "work:1")
        monkeypatch.setitem(
            review_queue.TARGETS,
            "work",
            review_queue.ReviewTarget("work", "work", "work-id"),
        )
        with pytest.raises(ValueError, match="invalid review target primary key column"):
            review_queue.fetch_review_object(conn, "work:1")
        monkeypatch.setitem(
            review_queue.TARGETS,
            "work",
            review_queue.ReviewTarget("work", "work", "work_id", "bad state"),
        )
        with pytest.raises(ValueError, match="invalid review target state column"):
            review_queue.change_review_state(conn, "work:1", new_state="accepted")
        monkeypatch.setattr(review_queue, "table_exists", lambda *_: False)
        monkeypatch.setitem(
            review_queue.TARGETS,
            "work",
            review_queue.ReviewTarget("work", "work", "work_id", "review_state"),
        )
        with pytest.raises(ValueError, match="review target table does not exist"):
            review_queue.fetch_review_object(conn, "work:1")
        with pytest.raises(ValueError, match="review target table does not exist"):
            review_queue.change_review_state(conn, "work:1", new_state="accepted")
    finally:
        conn.close()


def test_review_queue_view_payload_render_and_main_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = create_review_core_db(tmp_path)
    args = argparse.Namespace(
        db=str(db),
        state="all",
        object_type="work",
        min_confidence=0.3,
        max_confidence=0.5,
        source_type="book",
        workspace_id="alpha_subject",
        authority_level="primary",
        public_blocker="any",
        limit=2,
        full_counts=False,
        format="json",
    )

    payload = review_queue_view.build_review_queue_payload(args)
    assert payload["schema_version"] == "review-queue.v1"
    assert payload["filters"]["full_counts"] is False
    assert payload["counts"]["returned_items"] == 1
    assert payload["counts"]["total_items"] == 1
    assert payload["truncated"] is False
    assert payload["items"][0]["object_ref"] == "work:1"

    full_args = argparse.Namespace(**{**vars(args), "full_counts": True, "limit": 1})
    full_payload = review_queue_view.build_review_queue_payload(full_args)
    assert full_payload["filters"]["full_counts"] is True
    assert full_payload["counts"]["total_items"] == 1
    assert full_payload["truncated"] is True

    rendered = review_queue_view.render_text(payload)
    assert "schema_version=review-queue.v1" in rendered
    assert "object_type_filter=work" in rendered
    assert "returned_items=1" in rendered
    assert "item[0].object_ref=work:1" in rendered

    assert review_queue_view.resolve_db_path(str(db)) == db
    assert review_queue_view.count_key(None) == "(empty)"
    assert review_queue_view.count_by(payload["items"], "review_state") == {"needs_review": 1}
    assert review_queue_view.text_value("a\nb\tc") == "a b c"
    normalized = review_queue_view.normalize_item(payload["items"][0])
    assert normalized["available_actions"]["writer_surface"].endswith("review_queue.py")
    assert review_queue_view.review_command("accept", normalized["object_ref"]).endswith("accept work:1")
    assert review_queue_view.review_command("accept", None) is None

    conn = review_queue_view.connect_read_only(db)
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        conn.close()

    def fail_connect(*args: object, **kwargs: object) -> None:
        raise sqlite3.Error("boom")

    monkeypatch.setattr(review_queue_view.sqlite3, "connect", fail_connect)
    with pytest.raises(review_queue_view.ReviewQueueViewError, match="cannot open review database read-only"):
        review_queue_view.connect_read_only(db)
    monkeypatch.undo()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_review_queue_view.py",
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
        ],
    )
    assert review_queue_view.main() == 0
    assert "schema_version=review-queue.v1" in capsys.readouterr().out

    invalid = tmp_path / "invalid.sqlite"
    invalid.write_text("not a database", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_review_queue_view.py", "--db", str(invalid), "--format", "json"],
    )
    assert review_queue_view.main() == 1
    assert "Error:" in capsys.readouterr().err


def test_review_queue_main_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    db = create_review_core_db(tmp_path)

    assert review_queue.main([str(db), "list", "--state", "all", "--format", "json"]) == 0
    list_payload = json.loads(capsys.readouterr().out)
    assert list_payload["count"] > 0

    assert review_queue.main([str(db), "show", "work:1", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["object_ref"] == "work:1"

    assert review_queue.main([str(db), "accept", "work:2", "--format", "text"]) == 0
    assert "work:2: needs_review -> accepted" in capsys.readouterr().out

    missing = tmp_path / "missing.sqlite"
    assert review_queue.main([str(missing), "list"]) == 3
    assert "review file error" in capsys.readouterr().err

    invalid = tmp_path / "invalid.sqlite"
    invalid.write_text("not a database", encoding="utf-8")
    assert review_queue.main([str(invalid), "list"]) == 4
    assert "review database error" in capsys.readouterr().err

    assert review_queue.main([str(db), "show", "missing"]) == 2
    assert "review error" in capsys.readouterr().err

    def boom(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("boom")

    monkeypatch.setattr(review_queue, "list_review_items", boom)
    assert review_queue.main([str(db), "list"]) == 5
    assert "review unexpected error" in capsys.readouterr().err
