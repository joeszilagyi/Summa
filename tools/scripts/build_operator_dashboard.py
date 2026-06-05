#!/usr/bin/env python3
"""Render a read-only static operator health dashboard from doctor JSON."""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doctor-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--title", default="Summa Operator Health")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args()


def load_doctor_report(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read doctor report: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("doctor report must contain a JSON object")
    if payload.get("schema_version") != "local-doctor-report.v1":
        raise ValueError("doctor report must use schema_version local-doctor-report.v1")
    return payload


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def status_class(value: Any) -> str:
    status = str(value).lower()
    if status in {"pass", "available", "clean", "present", "ok", "populated", "healthy", "no_rows"}:
        return "status-pass"
    if status in {
        "warn",
        "dirty",
        "not_found",
        "missing",
        "absent",
        "uninitialized",
        "initialized_empty",
        "accumulating",
        "insufficient_data",
        "review_lagging",
        "contradiction_spike",
        "stalled",
        "pass_with_unresolved",
        "warning",
        "unavailable",
    }:
        return "status-warn"
    if status in {"fail", "invalid"}:
        return "status-fail"
    return "status-unknown"


def render_status_row(label: str, value: Any) -> str:
    return f'<tr><th>{esc(label)}</th><td><span class="pill {status_class(value)}">{esc(value)}</span></td></tr>'


def render_value_row(label: str, value: Any) -> str:
    return f"<tr><th>{esc(label)}</th><td>{esc(value)}</td></tr>"


def render_workspaces(report: dict[str, Any]) -> str:
    rows = []
    for workspace in report.get("workspaces", []):
        saturation = (
            workspace.get("saturation") if isinstance(workspace.get("saturation"), dict) else {}
        )
        rows.append(
            "<tr>"
            f"<td>{esc(workspace.get('workspace_id'))}</td>"
            f"<td>{esc(workspace.get('lifecycle_state'))}</td>"
            f"<td>{esc(workspace.get('schedule_posture'))}</td>"
            f'<td><span class="pill {status_class(workspace.get("workspace_root_status"))}">{esc(workspace.get("workspace_root_status"))}</span></td>'
            f'<td><span class="pill {status_class(workspace.get("default_subject_manifest_status"))}">{esc(workspace.get("default_subject_manifest_status"))}</span></td>'
            f"<td>{esc(saturation.get('state', 'not_evaluated'))}</td>"
            f"<td>{esc(saturation.get('scheduler_action', 'run'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="7" class="empty">No resolved workspaces</td></tr>')
    return "\n".join(rows)


def render_databases(report: dict[str, Any]) -> str:
    rows = []
    for database in report.get("databases", []):
        rows.append(
            "<tr>"
            f"<td>{esc(database.get('path'))}</td>"
            f'<td><span class="pill {status_class(database.get("status"))}">{esc(database.get("status"))}</span></td>'
            f"<td>{esc(database.get('schema_version'))}</td>"
            f"<td>{esc(database.get('user_version'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="4" class="empty">No SQLite stores found</td></tr>')
    return "\n".join(rows)


def render_locks(report: dict[str, Any]) -> str:
    rows = []
    for lock in report.get("locks", []):
        rows.append(
            "<tr>"
            f"<td>{esc(lock.get('workspace_id'))}</td>"
            f"<td>{esc(lock.get('pid'))}</td>"
            f"<td>{esc(lock.get('heartbeat_at'))}</td>"
            f'<td><span class="pill {status_class(lock.get("status"))}">{esc(lock.get("status"))}</span></td>'
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="4" class="empty">No active lock metadata</td></tr>')
    return "\n".join(rows)


def render_findings(report: dict[str, Any]) -> str:
    rows = []
    for finding in report.get("findings", [])[:50]:
        rows.append(
            "<tr>"
            f"<td>{esc(finding.get('code'))}</td>"
            f"<td>{esc(finding.get('class'))}</td>"
            f"<td>{esc(finding.get('message'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="3" class="empty">No findings</td></tr>')
    return "\n".join(rows)


def render_canonical_family_counts(report: dict[str, Any]) -> str:
    canonical_store = report.get("canonical_store", {})
    rows = []
    for family, count in sorted(canonical_store.get("family_counts", {}).items()):
        rows.append(f"<tr><td>{esc(family)}</td><td>{esc(count)}</td></tr>")
    if not rows:
        rows.append(
            '<tr><td colspan="2" class="empty">No canonical family counts available</td></tr>'
        )
    return "\n".join(rows)


def render_canonical_table_counts(report: dict[str, Any]) -> str:
    canonical_store = report.get("canonical_store", {})
    rows = []
    for table_name, count in sorted(canonical_store.get("table_counts", {}).items()):
        rows.append(f"<tr><td>{esc(table_name)}</td><td>{esc(count)}</td></tr>")
    if not rows:
        rows.append(
            '<tr><td colspan="2" class="empty">No canonical table counts available</td></tr>'
        )
    return "\n".join(rows)


def render_canonical_notes(report: dict[str, Any]) -> str:
    canonical_store = report.get("canonical_store", {})
    warnings = canonical_store.get("warnings", [])
    errors = canonical_store.get("errors", [])
    notes: list[str] = []
    interpretation = canonical_store.get("recommended_interpretation")
    if interpretation:
        notes.append(f"interpretation: {interpretation}")
    notes.extend(f"warning: {item}" for item in warnings)
    notes.extend(f"error: {item}" for item in errors)
    rows = [f'<tr><td colspan="2">{esc(note)}</td></tr>' for note in notes if note]
    if not rows:
        rows.append('<tr><td colspan="2" class="empty">No canonical store notes</td></tr>')
    return "\n".join(rows)


def render_loop_health_cycle_rows(report: dict[str, Any]) -> str:
    loop = report.get("loop_health", {})
    rows = []
    for cycle in loop.get("per_cycle_metrics", [])[:8]:
        rows.append(
            "<tr>"
            f"<td>{esc(cycle.get('cycle_id'))}</td>"
            f"<td>{esc(cycle.get('cycle_depth'))}</td>"
            f"<td>{esc(cycle.get('new_reviewable_count'))}</td>"
            f"<td>{esc(cycle.get('new_accepted_count'))}</td>"
            f"<td>{esc(cycle.get('new_contradiction_count'))}</td>"
            f"<td>{esc(cycle.get('yield_score'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="6" class="empty">No loop cycle metrics available</td></tr>')
    return "\n".join(rows)


def render_loop_health_notes(report: dict[str, Any]) -> str:
    loop = report.get("loop_health", {})
    notes: list[str] = []
    notes.extend(f"warning: {item}" for item in loop.get("warnings", []))
    notes.extend(f"limitation: {item}" for item in loop.get("limitations", []))
    rows = [f'<tr><td colspan="2">{esc(note)}</td></tr>' for note in notes if note]
    if not rows:
        rows.append(
            '<tr><td colspan="2" class="empty">No loop-health warnings or limitations</td></tr>'
        )
    return "\n".join(rows)


def render_graph_closure_issues(report: dict[str, Any]) -> str:
    graph = report.get("graph_closure", {})
    rows = []
    for item in graph.get("top_issues", [])[:8]:
        rows.append(
            "<tr>"
            f"<td>{esc(item.get('table'))}</td>"
            f"<td>{esc(item.get('status'))}</td>"
            f"<td>{esc(item.get('code'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="3" class="empty">No graph-closure issues available</td></tr>')
    return "\n".join(rows)


def render_dashboard(report: dict[str, Any], *, title: str) -> str:
    checks = report.get("checks", {})
    summary = report.get("summary", {})
    backup = report.get("backup_posture", {})
    migration = report.get("migration_posture", {})
    scheduler = report.get("scheduler", {})
    public_gates = report.get("public_gates", {})
    public_surfaces = public_gates.get("surfaces", {})
    canonical_store = report.get("canonical_store", {})
    loop_health = report.get("loop_health", {})
    loop_aggregate = (
        loop_health.get("aggregate_metrics", {}) if isinstance(loop_health, dict) else {}
    )
    loop_backlog = loop_health.get("review_backlog", {}) if isinstance(loop_health, dict) else {}
    loop_contradictions = (
        loop_health.get("contradictions", {}) if isinstance(loop_health, dict) else {}
    )
    loop_resolution = (
        loop_health.get("ingestion_resolution", {}) if isinstance(loop_health, dict) else {}
    )
    graph_closure = report.get("graph_closure", {})
    public_rows = "\n".join(
        render_status_row(name, value) for name, value in sorted(public_surfaces.items())
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #1f2933;
      --muted: #667085;
      --line: #d7dde5;
      --band: #f6f8fb;
      --ok: #166534;
      --warn: #9a5b00;
      --fail: #b42318;
      --unknown: #475467;
    }}
    body {{ margin: 0; font: 14px/1.45 system-ui, -apple-system, Segoe UI, sans-serif; color: var(--ink); background: white; }}
    header {{ padding: 24px clamp(16px, 4vw, 48px); border-bottom: 1px solid var(--line); background: var(--band); }}
    main {{ padding: 20px clamp(16px, 4vw, 48px) 40px; }}
    h1 {{ margin: 0 0 6px; font-size: 24px; font-weight: 650; letter-spacing: 0; }}
    h2 {{ margin: 28px 0 10px; font-size: 16px; font-weight: 650; letter-spacing: 0; }}
    .summary {{ display: flex; flex-wrap: wrap; gap: 10px; color: var(--muted); }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 18px; }}
    table {{ width: 100%; border-collapse: collapse; border: 1px solid var(--line); background: white; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; overflow-wrap: anywhere; }}
    th {{ width: 38%; background: #fbfcfe; font-weight: 600; }}
    thead th {{ width: auto; color: var(--muted); }}
    .pill {{ display: inline-block; min-width: 58px; padding: 2px 8px; border: 1px solid currentColor; border-radius: 999px; font-size: 12px; text-align: center; }}
    .status-pass {{ color: var(--ok); }}
    .status-warn {{ color: var(--warn); }}
    .status-fail {{ color: var(--fail); }}
    .status-unknown {{ color: var(--unknown); }}
    .empty {{ color: var(--muted); font-style: italic; }}
  </style>
</head>
<body>
  <header>
    <h1>{esc(title)}</h1>
    <div class="summary">
      <span>schema_version={esc(report.get("schema_version"))}</span>
      <span>status={esc(summary.get("status"))}</span>
      <span>findings={esc(summary.get("finding_count"))}</span>
      <span>operator_actions={esc(summary.get("operator_action_required_count"))}</span>
    </div>
  </header>
  <main>
    <section>
      <h2>Checks</h2>
      <div class="grid">
        <table><tbody>
          {"".join(render_status_row(name, value) for name, value in sorted(checks.items()))}
        </tbody></table>
        <table><tbody>
          {render_status_row("backup_policy", backup.get("policy_status"))}
          {render_status_row("backup_status", backup.get("status"))}
          {render_status_row("migration_status", migration.get("status"))}
          {render_status_row("scheduler_selector", scheduler.get("selector_status"))}
          {render_status_row("scheduler_status", scheduler.get("status"))}
        </tbody></table>
        <table><tbody>
          {public_rows}
        </tbody></table>
      </div>
    </section>
    <section>
      <h2>Canonical Store</h2>
      <div class="grid">
        <table><tbody>
          {render_status_row("status", canonical_store.get("status"))}
          {render_value_row("schema_version", canonical_store.get("schema_version"))}
          {render_value_row("total_rows", canonical_store.get("total_rows"))}
          {render_value_row("last_ingest_type", canonical_store.get("last_ingest_event_type"))}
          {render_value_row("last_ingest_at", canonical_store.get("last_ingest_at"))}
          {render_value_row("last_provenance_at", canonical_store.get("last_provenance_event_at"))}
        </tbody></table>
        <table><thead><tr><th>Family</th><th>Rows</th></tr></thead><tbody>
          {render_canonical_family_counts(report)}
        </tbody></table>
        <table><thead><tr><th>Table</th><th>Rows</th></tr></thead><tbody>
          {render_canonical_table_counts(report)}
        </tbody></table>
      </div>
      <table><tbody>
        {render_canonical_notes(report)}
      </tbody></table>
    </section>
    <section>
      <h2>Loop Health</h2>
      <div class="grid">
        <table><tbody>
          {render_status_row("status", loop_health.get("health_status"))}
          {render_value_row("yield_trend", loop_aggregate.get("yield_trend"))}
          {render_value_row("lookback_cycles", loop_health.get("lookback_cycles"))}
          {render_value_row("reviewable_ingested", loop_resolution.get("reviewable_ingested_count"))}
          {render_value_row("review_decisions_applied", loop_resolution.get("review_decision_applied_count"))}
          {render_value_row("resolution_coverage", loop_resolution.get("resolution_coverage"))}
        </tbody></table>
        <table><tbody>
          {render_value_row("pending_review_count", loop_backlog.get("pending_review_count"))}
          {render_value_row("oldest_pending_age_days", loop_backlog.get("oldest_pending_age_days"))}
          {render_value_row("median_pending_age_days", loop_backlog.get("median_pending_age_days"))}
          {render_value_row("total_contradictions", loop_contradictions.get("total_contradictions"))}
          {render_value_row("new_contradictions", loop_contradictions.get("new_contradictions"))}
          {render_value_row("contradictions_per_new_source_claim", loop_contradictions.get("contradictions_per_new_source_claim"))}
        </tbody></table>
        <table><thead><tr><th>Cycle</th><th>Depth</th><th>Reviewable</th><th>Accepted</th><th>Contradictions</th><th>Yield</th></tr></thead><tbody>
          {render_loop_health_cycle_rows(report)}
        </tbody></table>
      </div>
      <table><tbody>
        {render_loop_health_notes(report)}
      </tbody></table>
    </section>
    <section>
      <h2>Graph Closure</h2>
      <div class="grid">
        <table><tbody>
          {render_status_row("status", graph_closure.get("status"))}
          {render_value_row("orphan_error_count", graph_closure.get("orphan_error_count"))}
          {render_value_row("unresolved_tracked_count", graph_closure.get("unresolved_tracked_count"))}
          {render_value_row("repairable_count", graph_closure.get("repairable_count"))}
          {render_value_row("quarantined_count", graph_closure.get("quarantined_count"))}
          {render_value_row("read_only", graph_closure.get("read_only"))}
        </tbody></table>
        <table><thead><tr><th>Table</th><th>Status</th><th>Code</th></tr></thead><tbody>
          {render_graph_closure_issues(report)}
        </tbody></table>
      </div>
    </section>
    <section>
      <h2>Workspaces</h2>
      <table><thead><tr><th>Workspace</th><th>Lifecycle</th><th>Schedule</th><th>Root</th><th>Manifest</th><th>Saturation</th><th>Action</th></tr></thead><tbody>
        {render_workspaces(report)}
      </tbody></table>
    </section>
    <section>
      <h2>Databases</h2>
      <table><thead><tr><th>Path</th><th>Status</th><th>Schema</th><th>User Version</th></tr></thead><tbody>
        {render_databases(report)}
      </tbody></table>
    </section>
    <section>
      <h2>Locks</h2>
      <table><thead><tr><th>Workspace</th><th>PID</th><th>Heartbeat</th><th>Status</th></tr></thead><tbody>
        {render_locks(report)}
      </tbody></table>
    </section>
    <section>
      <h2>Findings</h2>
      <table><thead><tr><th>Code</th><th>Class</th><th>Message</th></tr></thead><tbody>
        {render_findings(report)}
      </tbody></table>
    </section>
  </main>
</body>
</html>
"""


def build_dashboard(doctor_report: Path, output: Path, *, title: str) -> dict[str, Any]:
    report = load_doctor_report(doctor_report)
    body = render_dashboard(report, title=title)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
    return {
        "schema_version": "operator-dashboard-build-report.v1",
        "status": "pass",
        "output": str(output),
        "doctor_report": str(doctor_report),
        "read_only": True,
    }


def main() -> int:
    args = parse_args()
    try:
        report = build_dashboard(args.doctor_report, args.output, title=args.title)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write("\n".join(f"{key}={value}" for key, value in report.items()) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
