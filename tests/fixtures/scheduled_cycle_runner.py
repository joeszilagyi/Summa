from __future__ import annotations

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--run-dir", required=True)
parser.add_argument("--run-id", required=True)
parser.add_argument("--workspace")
parser.add_argument("--subject")
parser.add_argument("--db")
parser.add_argument("--timestamp")
parser.add_argument("--mode")
parser.add_argument("--format")
parser.add_argument("--candidate-batch-fixture")
parser.add_argument("--execution-run-fixture")
parser.add_argument("--build-next-feedback-plan", action="store_true")
parser.add_argument("--skip-workspace-lock", action="store_true")
args = parser.parse_args()

run_dir = Path(args.run_dir)
run_dir.mkdir(parents=True, exist_ok=True)
payload = {
    "schema_version": "topic-cycle-run.v1",
    "run_id": args.run_id,
    "cycle_event_id": "cycle:" + args.run_id,
    "status": "completed",
}
(run_dir / "topic-cycle-run.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")
print(json.dumps(payload))
