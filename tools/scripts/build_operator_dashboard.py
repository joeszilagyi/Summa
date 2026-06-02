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
    if status in {"pass", "available", "clean", "present", "ok"}:
        return "status-pass"
    if status in {"warn", "dirty", "not_found", "missing"}:
        return "status-warn"
    if status in {"fail", "invalid"}:
        return "status-fail"
    return "status-unknown"


def render_status_row(label: str, value: Any) -> str:
    return f"<tr><th>{esc(label)}</th><td><span class=\"pill {status_class(value)}\">{esc(value)}</span></td></tr>"


def render_workspaces(report: dict[str, Any]) -> str:
    rows = []
    for workspace in report.get("workspaces", []):
        rows.append(
            "<tr>"
            f"<td>{esc(workspace.get('workspace_id'))}</td>"
            f"<td>{esc(workspace.get('lifecycle_state'))}</td>"
            f"<td>{esc(workspace.get('schedule_posture'))}</td>"
            f"<td><span class=\"pill {status_class(workspace.get('workspace_root_status'))}\">{esc(workspace.get('workspace_root_status'))}</span></td>"
            f"<td><span class=\"pill {status_class(workspace.get('default_subject_manifest_status'))}\">{esc(workspace.get('default_subject_manifest_status'))}</span></td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan=\"5\" class=\"empty\">No resolved workspaces</td></tr>")
    return "\n".join(rows)


def render_databases(report: dict[str, Any]) -> str:
    rows = []
    for database in report.get("databases", []):
        rows.append(
            "<tr>"
            f"<td>{esc(database.get('path'))}</td>"
            f"<td><span class=\"pill {status_class(database.get('status'))}\">{esc(database.get('status'))}</span></td>"
            f"<td>{esc(database.get('schema_version'))}</td>"
            f"<td>{esc(database.get('user_version'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan=\"4\" class=\"empty\">No SQLite stores found</td></tr>")
    return "\n".join(rows)


def render_locks(report: dict[str, Any]) -> str:
    rows = []
    for lock in report.get("locks", []):
        rows.append(
            "<tr>"
            f"<td>{esc(lock.get('workspace_id'))}</td>"
            f"<td>{esc(lock.get('pid'))}</td>"
            f"<td>{esc(lock.get('heartbeat_at'))}</td>"
            f"<td><span class=\"pill {status_class(lock.get('status'))}\">{esc(lock.get('status'))}</span></td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan=\"4\" class=\"empty\">No active lock metadata</td></tr>")
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
        rows.append("<tr><td colspan=\"3\" class=\"empty\">No findings</td></tr>")
    return "\n".join(rows)


def render_dashboard(report: dict[str, Any], *, title: str) -> str:
    checks = report.get("checks", {})
    summary = report.get("summary", {})
    backup = report.get("backup_posture", {})
    migration = report.get("migration_posture", {})
    scheduler = report.get("scheduler", {})
    public_gates = report.get("public_gates", {})
    public_surfaces = public_gates.get("surfaces", {})
    public_rows = "\n".join(render_status_row(name, value) for name, value in sorted(public_surfaces.items()))

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
      <span>schema_version={esc(report.get('schema_version'))}</span>
      <span>status={esc(summary.get('status'))}</span>
      <span>findings={esc(summary.get('finding_count'))}</span>
      <span>operator_actions={esc(summary.get('operator_action_required_count'))}</span>
    </div>
  </header>
  <main>
    <section>
      <h2>Checks</h2>
      <div class="grid">
        <table><tbody>
          {''.join(render_status_row(name, value) for name, value in sorted(checks.items()))}
        </tbody></table>
        <table><tbody>
          {render_status_row('backup_policy', backup.get('policy_status'))}
          {render_status_row('backup_status', backup.get('status'))}
          {render_status_row('migration_status', migration.get('status'))}
          {render_status_row('scheduler_selector', scheduler.get('selector_status'))}
          {render_status_row('scheduler_status', scheduler.get('status'))}
        </tbody></table>
        <table><tbody>
          {public_rows}
        </tbody></table>
      </div>
    </section>
    <section>
      <h2>Workspaces</h2>
      <table><thead><tr><th>Workspace</th><th>Lifecycle</th><th>Schedule</th><th>Root</th><th>Manifest</th></tr></thead><tbody>
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
