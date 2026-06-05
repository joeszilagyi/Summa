#!/usr/bin/env python3
"""Simulate source-query-plan execution without touching live services.

Phase 3C is simulation only. This tool never calls external APIs, browses,
crawls, downloads payloads, creates real source captures, creates source_access
rows, or turns simulated outputs into real source candidates.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import source_query_plan  # noqa: E402


REPORT_SCHEMA_VERSION = "source-query-execution-simulation-report.v1"
EXPORT_SCHEMA_VERSION = "source-query-execution-simulation-export.v1"

SIMULATION_FIELDS = [
    "simulation_id",
    "query_plan_id",
    "topic_id",
    "locus_id",
    "query_family",
    "query_mode",
    "simulation_status",
    "blocked_reason",
    "simulation_scenario",
    "is_simulated",
    "execution_attempted",
    "external_calls_attempted",
    "network_access",
    "simulated_result_count",
    "simulated_unique_result_count",
    "simulated_duplicate_count",
    "simulated_failure_count",
    "simulated_lead_candidate_count",
    "source_candidates_created",
    "captures_created",
    "source_access_rows_created",
    "payloads_created",
    "started_at",
    "completed_at",
    "simulated_by",
    "confidence_score",
    "review_state",
    "notes",
]

SIMULATED_LEAD_CANDIDATE_FIELDS = [
    "simulated_lead_candidate_id",
    "simulation_id",
    "query_plan_id",
    "topic_id",
    "locus_id",
    "candidate_label",
    "candidate_locator",
    "expected_source_type",
    "duplicate_cluster_id",
    "is_simulated",
    "lead_status",
    "acquisition_status",
    "capture_status",
    "external_call_status",
    "confidence_score",
    "review_state",
    "notes",
]


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    return slug or "unnamed"


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise RuntimeError(f"db not found: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    if not table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def ensure_schema(conn: sqlite3.Connection) -> None:
    source_query_plan.ensure_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_query_execution_simulation (
          source_query_execution_simulation_pk INTEGER PRIMARY KEY,
          simulation_id TEXT NOT NULL UNIQUE,
          query_plan_id TEXT NOT NULL,
          topic_id TEXT NOT NULL,
          locus_id TEXT NOT NULL,
          query_family TEXT NOT NULL,
          query_mode TEXT NOT NULL,
          simulation_status TEXT NOT NULL,
          blocked_reason TEXT,
          simulation_scenario TEXT NOT NULL,
          is_simulated INTEGER NOT NULL DEFAULT 1,
          execution_attempted INTEGER NOT NULL DEFAULT 0,
          external_calls_attempted INTEGER NOT NULL DEFAULT 0,
          network_access INTEGER NOT NULL DEFAULT 0,
          simulated_result_count INTEGER NOT NULL DEFAULT 0,
          simulated_unique_result_count INTEGER NOT NULL DEFAULT 0,
          simulated_duplicate_count INTEGER NOT NULL DEFAULT 0,
          simulated_failure_count INTEGER NOT NULL DEFAULT 0,
          simulated_lead_candidate_count INTEGER NOT NULL DEFAULT 0,
          source_candidates_created INTEGER NOT NULL DEFAULT 0,
          captures_created INTEGER NOT NULL DEFAULT 0,
          source_access_rows_created INTEGER NOT NULL DEFAULT 0,
          payloads_created INTEGER NOT NULL DEFAULT 0,
          started_at TEXT NOT NULL,
          completed_at TEXT NOT NULL,
          simulated_by TEXT NOT NULL,
          confidence_score REAL NOT NULL,
          review_state TEXT NOT NULL,
          notes TEXT,
          record_last_updated TEXT NOT NULL,
          FOREIGN KEY(query_plan_id) REFERENCES source_query_plan(query_plan_id),
          FOREIGN KEY(locus_id) REFERENCES source_locus(locus_id)
        );

        CREATE TABLE IF NOT EXISTS simulated_source_lead_candidate (
          simulated_lead_candidate_pk INTEGER PRIMARY KEY,
          simulated_lead_candidate_id TEXT NOT NULL UNIQUE,
          simulation_id TEXT NOT NULL,
          query_plan_id TEXT NOT NULL,
          topic_id TEXT NOT NULL,
          locus_id TEXT NOT NULL,
          candidate_label TEXT NOT NULL,
          candidate_locator TEXT,
          expected_source_type TEXT,
          duplicate_cluster_id TEXT,
          is_simulated INTEGER NOT NULL DEFAULT 1,
          lead_status TEXT NOT NULL DEFAULT 'simulated',
          acquisition_status TEXT NOT NULL DEFAULT 'not_acquired',
          capture_status TEXT NOT NULL DEFAULT 'not_captured',
          external_call_status TEXT NOT NULL DEFAULT 'not_attempted',
          confidence_score REAL NOT NULL,
          review_state TEXT NOT NULL,
          notes TEXT,
          record_last_updated TEXT NOT NULL,
          FOREIGN KEY(simulation_id) REFERENCES source_query_execution_simulation(simulation_id),
          FOREIGN KEY(query_plan_id) REFERENCES source_query_plan(query_plan_id),
          FOREIGN KEY(locus_id) REFERENCES source_locus(locus_id)
        );
        CREATE INDEX IF NOT EXISTS ix_source_query_execution_sim_topic ON source_query_execution_simulation(topic_id, simulation_status, query_family, review_state);
        CREATE INDEX IF NOT EXISTS ix_source_query_execution_sim_plan ON source_query_execution_simulation(query_plan_id);
        CREATE INDEX IF NOT EXISTS ix_source_query_execution_sim_blocked ON source_query_execution_simulation(blocked_reason);
        CREATE INDEX IF NOT EXISTS ix_simulated_source_lead_candidate_sim ON simulated_source_lead_candidate(simulation_id, duplicate_cluster_id);
        CREATE INDEX IF NOT EXISTS ix_simulated_source_lead_candidate_topic ON simulated_source_lead_candidate(topic_id, review_state);
        """
    )


def simulation_id_for_plan(query_plan_id: str, started_at: str) -> str:
    return f"qsim:{slugify(query_plan_id.removeprefix('qplan:'))}:{slugify(started_at)}"


def load_query_plans(conn: sqlite3.Connection, topic_id: str | None = None) -> list[dict[str, Any]]:
    ensure_schema(conn)
    where = ""
    params: tuple[Any, ...] = ()
    if topic_id:
        where = "WHERE topic_id=?"
        params = (topic_id,)
    rows = conn.execute(
        f"""
        SELECT *
        FROM source_query_plan
        {where}
        ORDER BY topic_id, plan_status, query_family, query_plan_id
        """,
        params,
    ).fetchall()
    return [source_query_plan.row_to_plan(row) for row in rows]


def scenario_for_plan(plan: dict[str, Any], *, include_needs_review: bool) -> tuple[str, str, str | None]:
    if plan["plan_status"] == "deprecated" or plan["review_state"] == "deprecated":
        return "skipped_deprecated", "deprecated_plan", "plan_deprecated"
    if plan["locus_type"] == "unknown" or "unknown_locus" in str(plan["locus_id"]):
        return "manual_review", "unknown_locus_diagnostic", "unknown_locus_diagnostic"
    if plan["plan_status"] == "needs_review" and not include_needs_review:
        return "review_blocked", "needs_review_blocked", "plan_review_state_needs_review"
    query_family = str(plan["query_family"])
    if query_family in {"maps", "academic_literature"}:
        return "simulated_zero_results", "zero_results", None
    access_class = str(plan.get("expected_access_class") or "").casefold()
    cost_level = str(plan.get("cost_level") or "").casefold()
    if "subscription" in access_class or "restricted" in access_class or cost_level in {"medium", "high"}:
        return "simulated_access_blocked", "access_blocked", "access_blocked_or_paywalled"
    if query_family in {"government_records", "web_general"}:
        return "simulated_high_noise", "high_noise", "high_noise_results"
    if query_family in {"archives", "books"}:
        return "simulated_completed", "duplicate_cluster", None
    return "simulated_completed", "standard_results", None


def simulated_counts(simulation_status: str, scenario: str) -> tuple[int, int, int, int, int]:
    if simulation_status in {"review_blocked", "skipped_deprecated", "manual_review"}:
        return 0, 0, 0, 0, 0
    if scenario == "zero_results":
        return 0, 0, 0, 0, 0
    if scenario == "access_blocked":
        return 2, 0, 0, 1, 0
    if scenario == "high_noise":
        return 25, 10, 5, 2, 3
    if scenario == "duplicate_cluster":
        return 4, 2, 2, 0, 2
    return 3, 3, 0, 0, 2


def simulation_from_plan(
    plan: dict[str, Any],
    *,
    started_at: str,
    completed_at: str,
    simulated_by: str,
    include_needs_review: bool,
) -> dict[str, Any]:
    status, scenario, blocked_reason = scenario_for_plan(plan, include_needs_review=include_needs_review)
    result_count, unique_count, duplicate_count, failure_count, lead_count = simulated_counts(status, scenario)
    confidence = 0.0 if status in {"review_blocked", "skipped_deprecated", "manual_review"} else float(plan["confidence_score"])
    if plan["plan_status"] == "needs_review":
        confidence = min(confidence, 0.5)
    review_state = "needs_review" if status != "simulated_completed" or plan["review_state"] != "accepted" else "accepted"
    if status == "simulated_zero_results":
        review_state = "needs_review"
    return {
        "simulation_id": simulation_id_for_plan(str(plan["query_plan_id"]), started_at),
        "query_plan_id": plan["query_plan_id"],
        "topic_id": plan["topic_id"],
        "locus_id": plan["locus_id"],
        "query_family": plan["query_family"],
        "query_mode": plan["query_mode"],
        "simulation_status": status,
        "blocked_reason": blocked_reason,
        "simulation_scenario": scenario,
        "is_simulated": True,
        "execution_attempted": False,
        "external_calls_attempted": False,
        "network_access": False,
        "simulated_result_count": result_count,
        "simulated_unique_result_count": unique_count,
        "simulated_duplicate_count": duplicate_count,
        "simulated_failure_count": failure_count,
        "simulated_lead_candidate_count": lead_count,
        "source_candidates_created": 0,
        "captures_created": 0,
        "source_access_rows_created": 0,
        "payloads_created": 0,
        "started_at": started_at,
        "completed_at": completed_at,
        "simulated_by": simulated_by,
        "confidence_score": round(confidence, 4),
        "review_state": review_state,
        "notes": "SIMULATED ONLY. No query was executed and no external service, source lead, source access row, capture, or payload was created.",
    }


def lead_candidates_for_simulation(simulation: dict[str, Any], plan: dict[str, Any]) -> list[dict[str, Any]]:
    count = int(simulation["simulated_lead_candidate_count"])
    if count <= 0:
        return []
    candidates: list[dict[str, Any]] = []
    duplicate_cluster = None
    if int(simulation["simulated_duplicate_count"]) > 0:
        duplicate_cluster = f"simdup:{slugify(str(simulation['simulation_id']))}:cluster-1"
    for index in range(1, count + 1):
        cluster_id = duplicate_cluster if duplicate_cluster and index <= 2 else None
        candidates.append(
            {
                "simulated_lead_candidate_id": f"simlead:{slugify(str(simulation['simulation_id']))}:{index:02d}",
                "simulation_id": simulation["simulation_id"],
                "query_plan_id": simulation["query_plan_id"],
                "topic_id": simulation["topic_id"],
                "locus_id": simulation["locus_id"],
                "candidate_label": f"SIMULATED candidate {index} for {plan['query_family']} at {plan['query_target']}",
                "candidate_locator": f"simulated://{slugify(str(simulation['topic_id']))}/{slugify(str(simulation['query_plan_id']))}/{index:02d}",
                "expected_source_type": plan.get("expected_source_type") or "unknown",
                "duplicate_cluster_id": cluster_id,
                "is_simulated": True,
                "lead_status": "simulated",
                "acquisition_status": "not_acquired",
                "capture_status": "not_captured",
                "external_call_status": "not_attempted",
                "confidence_score": simulation["confidence_score"],
                "review_state": "needs_review",
                "notes": "SIMULATED lead candidate only; not a real source lead and not acquired.",
            }
        )
    return candidates


def upsert_simulation(conn: sqlite3.Connection, simulation: dict[str, Any]) -> None:
    columns = SIMULATION_FIELDS + ["record_last_updated"]
    values = {
        **simulation,
        "execution_attempted": 1 if simulation["execution_attempted"] else 0,
        "external_calls_attempted": 1 if simulation["external_calls_attempted"] else 0,
        "network_access": 1 if simulation["network_access"] else 0,
        "is_simulated": 1 if simulation["is_simulated"] else 0,
        "record_last_updated": simulation["completed_at"],
    }
    update_columns = [column for column in columns if column != "simulation_id"]
    sql = f"""
    INSERT INTO source_query_execution_simulation ({', '.join(columns)})
    VALUES ({', '.join('?' for _ in columns)})
    ON CONFLICT(simulation_id) DO UPDATE SET
      {', '.join(f'{column}=excluded.{column}' for column in update_columns)}
    """
    conn.execute(sql, [values[column] for column in columns])


def upsert_lead_candidate(conn: sqlite3.Connection, candidate: dict[str, Any], *, updated_at: str) -> None:
    columns = SIMULATED_LEAD_CANDIDATE_FIELDS + ["record_last_updated"]
    values = {
        **candidate,
        "is_simulated": 1 if candidate["is_simulated"] else 0,
        "record_last_updated": updated_at,
    }
    update_columns = [column for column in columns if column != "simulated_lead_candidate_id"]
    sql = f"""
    INSERT INTO simulated_source_lead_candidate ({', '.join(columns)})
    VALUES ({', '.join('?' for _ in columns)})
    ON CONFLICT(simulated_lead_candidate_id) DO UPDATE SET
      {', '.join(f'{column}=excluded.{column}' for column in update_columns)}
    """
    conn.execute(sql, [values[column] for column in columns])


def row_to_simulation(row: sqlite3.Row) -> dict[str, Any]:
    simulation = {field: row[field] for field in SIMULATION_FIELDS}
    for key in ("is_simulated", "execution_attempted", "external_calls_attempted", "network_access"):
        simulation[key] = bool(simulation[key])
    return simulation


def row_to_lead_candidate(row: sqlite3.Row) -> dict[str, Any]:
    candidate = {field: row[field] for field in SIMULATED_LEAD_CANDIDATE_FIELDS}
    candidate["is_simulated"] = bool(candidate["is_simulated"])
    return candidate


def counts_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "(missing)")
        counts[value] = counts.get(value, 0) + 1
    return counts


def build_reports(simulations: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    failure_warning_log: list[dict[str, Any]] = []
    for simulation in simulations:
        if simulation["simulation_status"] in {"review_blocked", "skipped_deprecated", "manual_review", "simulated_access_blocked", "simulated_high_noise"}:
            failure_warning_log.append(
                {
                    "simulation_id": simulation["simulation_id"],
                    "query_plan_id": simulation["query_plan_id"],
                    "code": str(simulation["blocked_reason"] or simulation["simulation_status"]).upper(),
                    "severity": "warning",
                    "is_simulated": True,
                    "message": f"SIMULATED {simulation['simulation_status']} for {simulation['query_plan_id']}",
                }
            )
    duplicate_candidates = [candidate for candidate in candidates if candidate.get("duplicate_cluster_id")]
    return {
        "aggregate_simulation_summary": {
            "total_simulations": len(simulations),
            "simulated_result_count": sum(int(item["simulated_result_count"]) for item in simulations),
            "simulated_unique_result_count": sum(int(item["simulated_unique_result_count"]) for item in simulations),
            "simulated_duplicate_count": sum(int(item["simulated_duplicate_count"]) for item in simulations),
            "simulated_failure_count": sum(int(item["simulated_failure_count"]) for item in simulations),
            "simulated_lead_candidate_count": len(candidates),
            "by_topic": counts_by(simulations, "topic_id"),
            "by_status": counts_by(simulations, "simulation_status"),
            "by_query_family": counts_by(simulations, "query_family"),
            "by_query_mode": counts_by(simulations, "query_mode"),
            "by_review_state": counts_by(simulations, "review_state"),
            "by_blocked_reason": counts_by([item for item in simulations if item.get("blocked_reason")], "blocked_reason"),
        },
        "blocked_plan_report": [
            item for item in simulations if item["simulation_status"] in {"review_blocked", "skipped_deprecated", "manual_review"}
        ],
        "simulated_duplicate_lead_candidate_report": duplicate_candidates,
        "failure_warning_log": failure_warning_log,
        "simulated_productivity_report": {
            "real_productivity_counters_updated": False,
            "simulated_results": sum(int(item["simulated_result_count"]) for item in simulations),
            "simulated_unique_results": sum(int(item["simulated_unique_result_count"]) for item in simulations),
            "simulated_lead_candidates": len(candidates),
        },
    }


def run_simulations(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    started_at: str,
    completed_at: str,
    simulated_by: str,
    include_needs_review: bool = False,
    write: bool = True,
) -> dict[str, Any]:
    ensure_schema(conn)
    plans = load_query_plans(conn, topic_id)
    simulations: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for plan in plans:
        simulation = simulation_from_plan(
            plan,
            started_at=started_at,
            completed_at=completed_at,
            simulated_by=simulated_by,
            include_needs_review=include_needs_review,
        )
        plan_candidates = lead_candidates_for_simulation(simulation, plan)
        simulations.append(simulation)
        candidates.extend(plan_candidates)
        if write:
            upsert_simulation(conn, simulation)
            for candidate in plan_candidates:
                upsert_lead_candidate(conn, candidate, updated_at=completed_at)
    if write:
        conn.commit()
    reports = build_reports(simulations, candidates)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "operation": "run-simulations",
        "topic_id": topic_id,
        "is_simulated": True,
        "include_needs_review": include_needs_review,
        "execution_attempted": False,
        "external_calls_attempted": False,
        "network_access": False,
        "source_candidates_created": 0,
        "captures_created": 0,
        "source_access_rows_created": 0,
        "payloads_created": 0,
        "per_topic_simulation_report": reports["aggregate_simulation_summary"],
        "per_plan_simulation_report": simulations,
        "simulated_lead_candidates": candidates,
        **reports,
    }


def export_simulations(conn: sqlite3.Connection, topic_id: str | None = None) -> dict[str, Any]:
    ensure_schema(conn)
    where = ""
    params: tuple[Any, ...] = ()
    if topic_id:
        where = "WHERE topic_id=?"
        params = (topic_id,)
    sim_rows = conn.execute(
        f"""
        SELECT *
        FROM source_query_execution_simulation
        {where}
        ORDER BY topic_id, simulation_status, query_family, simulation_id
        """,
        params,
    ).fetchall()
    candidate_rows = conn.execute(
        f"""
        SELECT *
        FROM simulated_source_lead_candidate
        {where}
        ORDER BY topic_id, duplicate_cluster_id, simulated_lead_candidate_id
        """,
        params,
    ).fetchall()
    simulations = [row_to_simulation(row) for row in sim_rows]
    candidates = [row_to_lead_candidate(row) for row in candidate_rows]
    reports = build_reports(simulations, candidates)
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "topic_id": topic_id,
        "is_simulated": True,
        "execution_attempted": False,
        "external_calls_attempted": False,
        "network_access": False,
        "source_candidates_created": 0,
        "captures_created": 0,
        "source_access_rows_created": 0,
        "payloads_created": 0,
        "source_query_execution_simulations": simulations,
        "simulated_lead_candidates": candidates,
        **reports,
    }


def side_effect_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "lead_rows": count_rows(conn, "lead"),
        "source_access_rows": count_rows(conn, "source_access"),
        "capture_event_rows": count_rows(conn, "capture_event"),
    }


def write_json(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Simulate source-query-plan execution without network access.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run local simulations for stored query plans.")
    run.add_argument("--db", required=True, type=Path)
    run.add_argument("--topic-id", required=True)
    run.add_argument("--started-at", help="Simulation start time in ISO 8601 format.")
    run.add_argument("--completed-at", help="Simulation completion time in ISO 8601 format.")
    run.add_argument("--simulated-by", default="codex_phase3c")
    run.add_argument("--include-needs-review", action="store_true")
    run.add_argument("--dry-run", action="store_true", help="Do not write simulation rows to the DB.")
    run.add_argument("--report-json", type=Path)

    export = subparsers.add_parser("export", help="Export stored simulations and simulated lead candidates.")
    export.add_argument("--db", required=True, type=Path)
    export.add_argument("--topic-id")
    export.add_argument("--report-json", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(args.db)
        if args.command == "run":
            started_at = args.started_at or now_iso()
            completed_at = args.completed_at or now_iso()
            payload = run_simulations(
                conn,
                topic_id=args.topic_id,
                started_at=started_at,
                completed_at=completed_at,
                simulated_by=args.simulated_by,
                include_needs_review=args.include_needs_review,
                write=not args.dry_run,
            )
        elif args.command == "export":
            payload = export_simulations(conn, args.topic_id)
        else:  # pragma: no cover - argparse prevents this.
            raise RuntimeError(f"unknown command: {args.command}")
    except Exception as exc:
        print(f"source-query-execution-simulation error: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()
    write_json(args.report_json, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
