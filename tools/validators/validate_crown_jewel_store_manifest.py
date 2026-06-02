#!/usr/bin/env python3
"""Validate crown-jewel-store-manifest.v1 payloads."""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.crown_jewel_store_manifest import (  # noqa: E402
    CrownJewelStoreManifestError,
    load_manifest,
)


EXIT_PASS = 0
EXIT_FAIL = 1


def validate(path: Path) -> tuple[dict, int]:
    try:
        load_manifest(path)
    except CrownJewelStoreManifestError as exc:
        return {"schema_version": "validator-report.v1", "status": "fail", "errors": [{"message": str(exc)}]}, EXIT_FAIL
    return {"schema_version": "validator-report.v1", "status": "pass", "errors": []}, EXIT_PASS


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("Usage: validate_crown_jewel_store_manifest.py <manifest.json>", file=sys.stderr)
        return EXIT_FAIL
    report, exit_code = validate(Path(args[0]))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
