#!/usr/bin/env python3
"""Evaluate a network safety gate request without performing network access."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (
    REPO_ROOT,
    REPO_ROOT / "tools" / "common",
    REPO_ROOT / "tools" / "validators",
):
    candidate_text = str(candidate)
    if candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)

from tools.common.network_safety_gate import evaluate_request, load_request  # noqa: E402
from tools.validators.common import (  # noqa: E402
    EXIT_INPUT_UNAVAILABLE,
    EXIT_PASS,
    EXIT_VALIDATION_FAILED,
    add_report_args,
    resolve_report_root,
    write_json,
    write_text,
)

SCRIPT_PATH = "tools/scripts/evaluate_network_safety_gate.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="Path to the network safety gate request JSON file.")
    add_report_args(parser)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args()


def render_text(report: dict[str, object]) -> str:
    lines = [
        f"schema_version={report['schema_version']}",
        f"executor_name={report['executor_name']}",
        f"decision={report['decision']}",
        f"dry_run={str(report['dry_run']).lower()}",
        f"execution_allowed={str(report['execution_allowed']).lower()}",
    ]
    counts = report["counts"]
    assert isinstance(counts, dict)
    lines.append(
        "planned_actions={planned_actions} refused_actions={refused_actions} total_side_effect_units={total_side_effect_units} errors={errors} warnings={warnings}".format(
            **counts
        )
    )
    planned_actions = report.get("planned_actions")
    assert isinstance(planned_actions, list)
    for index, action in enumerate(planned_actions):
        assert isinstance(action, dict)
        lines.append(
            f"action[{index}]={action['action_id']} kind={action['action_kind']} method={action['method']} status={action['status']} host={action['host']}"
        )
    errors = report.get("errors")
    assert isinstance(errors, list)
    for index, error in enumerate(errors):
        assert isinstance(error, dict)
        lines.append(f"error[{index}]={error['code']} message={error['message']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    report_root = resolve_report_root(target, report_root=args.report_root)
    try:
        payload = load_request(target)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_INPUT_UNAVAILABLE

    report = evaluate_request(payload)
    text_report = render_text(report)
    write_json(args.report_json, report, root=report_root)
    write_text(args.report_text, text_report, root=report_root)

    if args.format == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(text_report)
    return EXIT_PASS if report["decision"] in {"allow", "dry_run"} else EXIT_VALIDATION_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
