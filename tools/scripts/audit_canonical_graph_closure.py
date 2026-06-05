#!/usr/bin/env python3
"""Audit canonical graph closure without mutating the SQLite store."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.source_db_tools.canonical_graph_closure import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
