from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = REPO_ROOT / "tools"
INDEX_WRAPPER_ROOT = REPO_ROOT / "tools" / "scripts"
DOC_LABEL_PATTERN = re.compile(r"^\s*(?:#\s*)?Documentation:\s*(?P<rest>.*)$")
REPO_DOC_PATH_PATTERN = re.compile(
    r"(?P<path>(?:docs/|TRACKED_SURFACE\.md|README\.md|CONTRIBUTING\.md)"
    r"[A-Za-z0-9_./-]*(?:\.schema\.json|\.md|\.json|\.yml|\.yaml))"
)
PYTHON_TARGET_ASSIGNMENT_PATTERN = re.compile(
    r"^\s*(?:readonly\s+)?(?P<name>[A-Z_][A-Z0-9_]*)=(?P<value>\"[^\"]*\"|'[^']*')\s*$"
)
PYTHON_INVOCATION_PATTERN = re.compile(
    r"^\s*(?:exec\s+)?(?P<python>\"?\$[A-Z_][A-Z0-9_]*\"?|python3|python)\s+"
    r"(?P<target>\"[^\"]+\.py\"|'[^']+\.py'|\"\$[A-Z_][A-Z0-9_]*\"|\$[A-Z_][A-Z0-9_]*)"
    r"(?:\s|$)"
)
SHELL_VARIABLE_REFERENCE_PATTERN = re.compile(
    r"\$(?:{(?P<braced>[A-Z_][A-Z0-9_]*)}|(?P<plain>[A-Z_][A-Z0-9_]*))"
)
FOCUSED_CONTRIBUTING_DOCS = (
    "docs/README.md",
    "docs/repo-layout.md",
    "TRACKED_SURFACE.md",
    "docs/project/PUBLIC_PRIVATE_EXPORT_BOUNDARY.md",
    "docs/project/STORAGE_AND_PUBLICATION_POLICY.md",
    "docs/history/sqlite-tooling-history.md",
)
SCRIPT_DIR_VARIABLES = {"SCRIPT_DIR", "SELF_DIR", "THIS_SCRIPT_DIR"}


def iter_tool_files(suffix: str) -> Iterable[Path]:
    for path in TOOLS_ROOT.rglob(f"*{suffix}"):
        if "__pycache__" in path.parts:
            continue
        yield path


def extract_documentation_references(body: str) -> list[str]:
    references: list[str] = []
    lines = body.splitlines()
    for index, line in enumerate(lines):
        label_match = DOC_LABEL_PATTERN.match(line)
        if label_match is None:
            continue

        inline_match = REPO_DOC_PATH_PATTERN.search(label_match.group("rest"))
        if inline_match is not None:
            references.append(inline_match.group("path"))
            continue

        for following_line in lines[index + 1 :]:
            stripped = following_line.strip()
            if not stripped:
                continue
            next_line_match = REPO_DOC_PATH_PATTERN.search(stripped)
            if next_line_match is not None:
                references.append(next_line_match.group("path"))
            break
    return references


def assert_documentation_references_resolve(script_paths: Iterable[Path], *, label: str) -> None:
    found_any_reference = False
    missing_messages: list[str] = []

    for script_path in script_paths:
        relative_script_path = script_path.relative_to(REPO_ROOT)
        references = extract_documentation_references(
            script_path.read_text(encoding="utf-8")
        )
        if references:
            found_any_reference = True
        for relative_doc_path in references:
            resolved_doc_path = REPO_ROOT / relative_doc_path
            if not resolved_doc_path.is_file():
                missing_messages.append(
                    f"{relative_script_path}: documentation reference {relative_doc_path!r} "
                    f"resolved to missing file {resolved_doc_path}"
                )

    assert found_any_reference, f"no documentation references found while scanning {label}"
    assert not missing_messages, "missing tool documentation references:\n" + "\n".join(
        sorted(missing_messages)
    )


def resolve_shell_target_expression(
    expression: str, *, wrapper_path: Path, variable_paths: dict[str, Path]
) -> Path | None:
    raw = expression.strip().strip("'\"")
    unresolved_names: set[str] = set()

    def replace_variable(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("plain")
        assert name is not None
        resolved = variable_paths.get(name)
        if resolved is None:
            unresolved_names.add(name)
            return match.group(0)
        return str(resolved)

    substituted = SHELL_VARIABLE_REFERENCE_PATTERN.sub(replace_variable, raw)
    if unresolved_names:
        return None

    candidate_path = Path(substituted)
    if not candidate_path.is_absolute():
        candidate_path = wrapper_path.parent / candidate_path
    return candidate_path.resolve(strict=False)


def resolve_index_wrapper_target(wrapper_path: Path) -> tuple[str, Path, int]:
    wrapper_dir = wrapper_path.parent.resolve()
    variable_paths: dict[str, Path] = {
        "REPO_ROOT": REPO_ROOT.resolve(),
        **{name: wrapper_dir for name in SCRIPT_DIR_VARIABLES},
    }

    lines = wrapper_path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        assignment_match = PYTHON_TARGET_ASSIGNMENT_PATTERN.match(line)
        if assignment_match is None:
            continue
        target_path = resolve_shell_target_expression(
            assignment_match.group("value"),
            wrapper_path=wrapper_path,
            variable_paths=variable_paths,
        )
        if target_path is None or target_path.suffix != ".py":
            continue
        variable_paths[assignment_match.group("name")] = target_path

    for line_number, line in enumerate(lines, start=1):
        invocation_match = PYTHON_INVOCATION_PATTERN.match(line)
        if invocation_match is None:
            continue
        target_expression = invocation_match.group("target")
        resolved_target = resolve_shell_target_expression(
            target_expression,
            wrapper_path=wrapper_path,
            variable_paths=variable_paths,
        )
        if resolved_target is None:
            raise AssertionError(
                f"{wrapper_path.relative_to(REPO_ROOT)}:{line_number}: could not resolve "
                f"Python target expression {target_expression!r} from line {line!r}"
            )
        return target_expression, resolved_target, line_number

    raise AssertionError(
        f"{wrapper_path.relative_to(REPO_ROOT)}: no statically resolvable Python target found"
    )


def test_extract_documentation_references_supports_shell_usage_patterns() -> None:
    shell_body = """#!/usr/bin/env bash
# Documentation: docs/scripts/index_run_gather.md
usage() {
  cat <<'EOF_USAGE'
Documentation:
  docs/scripts/index_build_knowledge_tree.md
Documentation: `docs/scripts/index_execute_source_adapter.md`.
EOF_USAGE
}
"""

    references = extract_documentation_references(shell_body)
    assert references == [
        "docs/scripts/index_run_gather.md",
        "docs/scripts/index_build_knowledge_tree.md",
        "docs/scripts/index_execute_source_adapter.md",
    ]


def test_tools_python_documentation_references_resolve() -> None:
    assert_documentation_references_resolve(
        iter_tool_files(".py"),
        label="tools/**/*.py",
    )


def test_tools_shell_documentation_references_resolve() -> None:
    assert_documentation_references_resolve(
        iter_tool_files(".sh"),
        label="tools/**/*.sh",
    )


def test_shell_wrapper_targets_resolve() -> None:
    failures: list[str] = []

    for wrapper_path in sorted(INDEX_WRAPPER_ROOT.glob("Index_*.sh")):
        target_expression, resolved_target, line_number = resolve_index_wrapper_target(
            wrapper_path
        )
        relative_wrapper = wrapper_path.relative_to(REPO_ROOT)

        if resolved_target == wrapper_path.resolve():
            failures.append(
                f"{relative_wrapper}:{line_number}: target expression {target_expression!r} "
                f"resolved back to the wrapper itself ({resolved_target})"
            )
            continue

        if not resolved_target.is_relative_to(REPO_ROOT.resolve()):
            failures.append(
                f"{relative_wrapper}:{line_number}: target expression {target_expression!r} "
                f"resolved outside the repository ({resolved_target})"
            )
            continue

        if resolved_target.suffix != ".py":
            failures.append(
                f"{relative_wrapper}:{line_number}: target expression {target_expression!r} "
                f"resolved to a non-Python file ({resolved_target})"
            )
            continue

        if not resolved_target.is_file():
            failures.append(
                f"{relative_wrapper}:{line_number}: target expression {target_expression!r} "
                f"resolved to missing file {resolved_target}"
            )

    assert not failures, "missing or invalid shell wrapper targets:\n" + "\n".join(
        failures
    )


def test_index_build_knowledge_tree_wrapper_documentation_reference_resolves() -> None:
    wrapper_path = REPO_ROOT / "tools" / "scripts" / "Index_Build_Knowledge_Tree.sh"
    wrapper_body = wrapper_path.read_text(encoding="utf-8")

    assert "docs/scripts/index_build_knowledge_tree.md" in wrapper_body
    assert (REPO_ROOT / "docs" / "scripts" / "index_build_knowledge_tree.md").is_file()


def test_contributing_known_doc_references_exist() -> None:
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

    for relative_path in FOCUSED_CONTRIBUTING_DOCS:
        assert relative_path in contributing
        assert (REPO_ROOT / relative_path).is_file()


def test_focused_doc_cross_links_resolve() -> None:
    markdown_link_pattern = re.compile(r"\[[^\]]+\]\(([^)#]+)(?:#[^)]+)?\)")

    for relative_path in FOCUSED_CONTRIBUTING_DOCS:
        doc_path = REPO_ROOT / relative_path
        body = doc_path.read_text(encoding="utf-8")
        for raw_target in markdown_link_pattern.findall(body):
            if "://" in raw_target or raw_target.startswith("mailto:"):
                continue
            resolved = (doc_path.parent / raw_target).resolve()
            assert resolved.exists(), f"{relative_path}: {raw_target}"
