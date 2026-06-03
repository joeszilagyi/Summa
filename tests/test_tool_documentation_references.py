from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = REPO_ROOT / "tools"
DOC_PATH_PATTERN = re.compile(
    r"(?:Documentation:\s*`{0,2}|# Documentation:\s*)([^`\s]+(?:\.md|\.json|\.yml|\.yaml|\.schema\.json))"
)


def test_tools_python_documentation_references_resolve() -> None:
    referenced_paths: set[str] = set()

    for module_path in TOOLS_ROOT.rglob("*.py"):
        if "__pycache__" in module_path.parts:
            continue
        matches = DOC_PATH_PATTERN.findall(module_path.read_text(encoding="utf-8"))
        referenced_paths.update(matches)

    assert referenced_paths
    missing = [relative_path for relative_path in sorted(referenced_paths) if not (REPO_ROOT / relative_path).exists()]
    assert missing == []
