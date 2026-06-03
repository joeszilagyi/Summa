from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import unquote, urlparse
import re

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPO_ROOT / "docs"

INLINE_LINK_RE = re.compile(r"(!?)\[[^\]]+\]\(([^)]+)\)")
REFERENCE_DEFINITION_RE = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)")
INLINE_CODE_RE = re.compile(r"`([^`]+)`")

KNOWN_TOP_LEVEL_FILES = {
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
    ".project_metadata",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "TRACKED_SURFACE.md",
    "pyproject.toml",
}
KNOWN_TOP_LEVEL_DIRS = {
    ".github/",
    "config/",
    "docs/",
    "index/",
    "tests/",
    "tools/",
}
KNOWN_REPO_PREFIXES = (
    ".github/",
    "config/",
    "docs/",
    "index/",
    "tests/",
    "tools/collateral/",
    "tools/common/",
    "tools/pipeline_registry/",
    "tools/prompts/",
    "tools/scripts/",
    "tools/source_db_tools/",
    "tools/validators/",
)
IGNORED_PLACEHOLDER_PREFIXES = (
    "$HOME/",
    "/tmp/",
    ".local/",
    "build/",
    "dbs/",
    "dist/",
    "foo/bar",
    "index/Places/",
    "out/",
    "path/to/",
    "runs/",
    "runtime/",
    "site-build/",
    "some/path",
    "test_corpora/",
    "workspace-roots/",
    "~/",
)
IGNORED_BASENAMES = {
    "example.json",
    "example.md",
    "example.yaml",
    "example.yml",
    "your-file.md",
}


@dataclass(frozen=True)
class MarkdownReference:
    kind: str
    source_path: Path
    line_number: int
    raw_text: str
    target_text: str


@dataclass(frozen=True)
class MarkdownReferenceFailure:
    reference: MarkdownReference
    resolved_path: Path | None
    reason: str

    def render(self) -> str:
        resolved = str(self.resolved_path) if self.resolved_path is not None else "<unresolved>"
        relative_source = self.reference.source_path.relative_to(REPO_ROOT)
        return (
            f"{relative_source}:{self.reference.line_number}: {self.reference.kind} "
            f"reference {self.reference.raw_text!r} -> {resolved}: {self.reason}"
        )


def iter_markdown_files() -> list[Path]:
    files = sorted(path for path in REPO_ROOT.glob("*.md") if path.is_file())
    if DOCS_ROOT.exists():
        files.extend(sorted(DOCS_ROOT.rglob("*.md")))
    return files


def iter_non_fenced_lines(text: str) -> Iterator[tuple[int, str]]:
    in_fence = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        yield line_number, line


def normalize_markdown_target(raw_target: str) -> str:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        return unquote(target[1 : target.index(">")])
    return unquote(re.split(r"\s+", target, maxsplit=1)[0])


def is_external_target(target: str) -> bool:
    if target.startswith("//"):
        return True
    parsed = urlparse(target)
    return bool(parsed.scheme)


def strip_fragment(target: str) -> str:
    return target.split("#", 1)[0]


def has_placeholder_shape(token: str) -> bool:
    if not token:
        return False
    if any(char in token for char in ("*", "?", "{", "}")):
        return True
    if "<" in token or ">" in token:
        return True
    if "..." in token:
        return True
    if token.startswith(IGNORED_PLACEHOLDER_PREFIXES):
        return True
    basename = Path(token.rstrip("/")).name
    if basename in IGNORED_BASENAMES:
        return True
    if basename.startswith("your-"):
        return True
    return False


def is_deliberate_backticked_path(token: str) -> bool:
    if not token or any(char.isspace() for char in token):
        return False
    if has_placeholder_shape(token):
        return False
    if token in KNOWN_TOP_LEVEL_FILES or token in KNOWN_TOP_LEVEL_DIRS:
        return True
    if token.startswith(("./", "../", "/")):
        return True
    if token.startswith(KNOWN_REPO_PREFIXES):
        return True
    return token.endswith(".md")


def resolve_backticked_path(source_path: Path, token: str) -> Path:
    if token in KNOWN_TOP_LEVEL_FILES or token in KNOWN_TOP_LEVEL_DIRS or token.startswith(
        KNOWN_REPO_PREFIXES
    ):
        candidate = REPO_ROOT / token.rstrip("/")
    elif token.startswith("/"):
        candidate = REPO_ROOT / token.lstrip("/")
    else:
        candidate = source_path.parent / token.rstrip("/")
    return candidate.resolve(strict=False)


def resolve_markdown_target(source_path: Path, target: str) -> Path:
    target_path = strip_fragment(target).rstrip("/")
    if target.startswith("/"):
        candidate = REPO_ROOT / target_path.lstrip("/")
    else:
        candidate = source_path.parent / target_path
    return candidate.resolve(strict=False)


def ensure_inside_repo(path: Path) -> bool:
    try:
        path.relative_to(REPO_ROOT)
        return True
    except ValueError:
        return False


def collect_markdown_references(markdown_path: Path) -> list[MarkdownReference]:
    references: list[MarkdownReference] = []
    text = markdown_path.read_text(encoding="utf-8")
    for line_number, line in iter_non_fenced_lines(text):
        for match in INLINE_LINK_RE.finditer(line):
            kind = "image link" if match.group(1) else "markdown link"
            raw_reference = match.group(0)
            target = normalize_markdown_target(match.group(2))
            if not target or target.startswith("#") or is_external_target(target):
                continue
            if has_placeholder_shape(target):
                continue
            references.append(
                MarkdownReference(kind, markdown_path, line_number, raw_reference, target)
            )

        definition_match = REFERENCE_DEFINITION_RE.match(line)
        if definition_match:
            target = normalize_markdown_target(definition_match.group(1))
            if target and not target.startswith("#") and not is_external_target(target):
                if not has_placeholder_shape(target):
                    references.append(
                        MarkdownReference(
                            "reference definition",
                            markdown_path,
                            line_number,
                            line.strip(),
                            target,
                        )
                    )

        for match in INLINE_CODE_RE.finditer(line):
            token = match.group(1).strip()
            if not is_deliberate_backticked_path(token):
                continue
            references.append(
                MarkdownReference("backticked path", markdown_path, line_number, token, token)
            )
    return references


def validate_markdown_reference(reference: MarkdownReference) -> MarkdownReferenceFailure | None:
    if reference.kind == "backticked path":
        resolved = resolve_backticked_path(reference.source_path, reference.target_text)
    else:
        resolved = resolve_markdown_target(reference.source_path, reference.target_text)

    if not ensure_inside_repo(resolved):
        return MarkdownReferenceFailure(reference, resolved, "resolved outside repository root")
    if not resolved.exists():
        kind = "directory" if reference.target_text.endswith("/") else "file"
        return MarkdownReferenceFailure(reference, resolved, f"missing {kind}")
    return None


def collect_markdown_reference_failures(markdown_path: Path) -> list[MarkdownReferenceFailure]:
    failures: list[MarkdownReferenceFailure] = []
    for reference in collect_markdown_references(markdown_path):
        failure = validate_markdown_reference(reference)
        if failure is not None:
            failures.append(failure)
    return failures


def test_markdown_internal_references_resolve() -> None:
    failures: list[MarkdownReferenceFailure] = []
    for markdown_path in iter_markdown_files():
        failures.extend(collect_markdown_reference_failures(markdown_path))

    if failures:
        rendered = "\n".join(failure.render() for failure in failures)
        pytest.fail(f"Markdown internal references must resolve:\n{rendered}")


def test_markdown_reference_checker_skips_placeholders_and_fences(tmp_path: Path) -> None:
    markdown = tmp_path / "doc.md"
    sibling = tmp_path / "sibling.md"
    sibling.write_text("# ok\n", encoding="utf-8")
    markdown.write_text(
        "\n".join(
            [
                "Use `path/to/file` as a placeholder.",
                "See `sibling.md` for the real doc.",
                "```bash",
                "cat README.md",
                "```",
                "And `<output-dir>/site-build/` stays symbolic.",
            ]
        ),
        encoding="utf-8",
    )

    references = collect_markdown_references(markdown)
    assert [reference.target_text for reference in references] == ["sibling.md"]
