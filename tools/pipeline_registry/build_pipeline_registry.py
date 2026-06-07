#!/usr/bin/env python3
"""Build or check the static pipeline registry SQLite DB from JSONL contracts.

Documentation: docs/tools/pipeline_registry/README.md

The builder reads contract JSONL files and the repository inventory. Build mode
atomically replaces the requested SQLite output after validation succeeds;
--check mode validates through a temporary database and leaves the target path
unchanged.
"""

from __future__ import annotations

import argparse
import re
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any


DATASETS = (
    ("artifact_classes.jsonl", "artifact_class"),
    ("repo_path_rules.jsonl", "repo_path_rule"),
    ("surfaces.jsonl", "surface"),
    ("surface_options.jsonl", "surface_option"),
    ("surface_option_effects.jsonl", "surface_option_effect"),
    ("surface_io.jsonl", "surface_io"),
    ("surface_dependencies.jsonl", "surface_dependency"),
)

KEY_COLUMNS = {
    "artifact_class": ("artifact_key",),
    "repo_path_rule": ("rule_key",),
    "surface": ("surface_key",),
    "surface_option": ("surface_key", "option_name", "option_kind"),
    "surface_option_effect": ("surface_key", "option_name", "option_kind", "effect_kind", "artifact_key"),
    "surface_io": ("surface_key", "artifact_key", "io_direction", "path_template"),
    "surface_dependency": ("surface_key", "dependency_surface_key", "dependency_kind", "condition_text"),
}

CURRENT_SURFACE_GLOBS = (
    "tools/scripts/*.sh",
    "tools/scripts/*.py",
    "tools/scripts/lib/*.sh",
    "tools/prompts/**/*.prompt",
    "tools/source_db_tools/*.py",
    "tools/source_db_tools/*.sh",
    "tools/collateral/*.py",
    "tools/common/*.py",
    "tools/pipeline_registry/*.py",
)

IGNORED_SURFACE_PATH_PARTS = (
    "/legacy/",
    "/artifacts/",
    "/old/",
    "/tests/",
    "__pycache__",
)

GIT_LS_FILES_TIMEOUT_SECONDS = 30

PATH_KINDS = {
    "surface",
    "config",
    "contract",
    "documentation",
    "source_data",
    "generated_data",
    "database",
    "operational",
    "environment",
    "test",
    "legacy",
    "unknown",
}

ALLOWED_VALUES = {
    "artifact_class": {
        "lineage_role": {"dataflow", "operational", "environment"},
    },
    "repo_path_rule": {
        "path_kind": PATH_KINDS,
    },
    "surface": {
        "surface_type": {
            "shell_script",
            "shell_library",
            "python_script",
            "python_module",
            "prompt",
            "config",
        },
        "lifecycle": {"live", "manual", "legacy", "archived", "experimental"},
        "entrypoint_kind": {"entrypoint", "helper", "tool", "prompt", "manual_helper", "contract"},
    },
    "surface_option": {
        "option_kind": {"positional", "flag", "env"},
    },
    "surface_option_effect": {
        "effect_kind": {
            "suppresses_write",
            "changes_provenance",
            "multiplies_outputs",
            "selects_input",
            "redirects_output",
            "narrows_output_scope",
            "overwrites_output",
            "changes_metadata",
            "adds_side_effect",
        },
    },
    "surface_io": {
        "io_direction": {"reads", "writes", "updates", "appends", "requires"},
    },
    "surface_dependency": {
        "dependency_kind": {"loads_helper", "uses_prompt", "calls", "executes", "reads_artifact_contract"},
    },
}


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant {value}")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for lineno, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line, parse_constant=reject_json_constant)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSON in {path}:{lineno}: {exc}") from exc
            except ValueError as exc:
                raise SystemExit(f"invalid JSON in {path}:{lineno}: {exc}") from exc
            if not isinstance(row, dict):
                raise SystemExit(f"invalid row in {path}:{lineno}: expected a JSON object")
            rows.append(row)
    return rows


def current_surface_paths(repo_root: Path) -> set[str]:
    found: set[str] = set()
    for glob in CURRENT_SURFACE_GLOBS:
        for path in repo_root.glob(glob):
            if not path.is_file():
                continue
            rel = path.relative_to(repo_root).as_posix()
            if any(part in rel for part in IGNORED_SURFACE_PATH_PARTS):
                continue
            found.add(rel)
    return found


def validate_surface_coverage(repo_root: Path, surfaces: list[dict[str, Any]]) -> None:
    surface_paths = {row["path"] for row in surfaces}
    missing_files = sorted(current_surface_paths(repo_root) - surface_paths)
    invalid_registry_paths = sorted(
        path for path in surface_paths if not (repo_root / path).is_file()
    )

    if missing_files:
        raise SystemExit(
            "registry is missing current surface paths:\n- " + "\n- ".join(missing_files)
        )
    if invalid_registry_paths:
        raise SystemExit(
            "registry references missing or non-file surface paths:\n- "
            + "\n- ".join(invalid_registry_paths)
        )


def validate_surface_path_alignment(
    *,
    repo_file_rows: list[dict[str, Any]],
    surfaces: list[dict[str, Any]],
) -> None:
    registered_surface_paths = {row["path"] for row in surfaces}
    classified_surface_paths = {
        row["repo_path"]
        for row in repo_file_rows
        if row["tracking_status"] == "current" and row["path_kind"] == "surface"
    }

    missing_registry_paths = sorted(classified_surface_paths - registered_surface_paths)
    misclassified_registry_paths = sorted(registered_surface_paths - classified_surface_paths)

    if missing_registry_paths:
        raise SystemExit(
            "repo_path_rules classify current files as surface but surfaces.jsonl is missing them:\n- "
            + "\n- ".join(missing_registry_paths)
        )
    if misclassified_registry_paths:
        raise SystemExit(
            "surfaces.jsonl paths are not classified as current surface files by repo_path_rules:\n- "
            + "\n- ".join(misclassified_registry_paths)
        )


def validate_dataset_keys(rows_by_table: dict[str, list[dict[str, Any]]]) -> None:
    for table, rows in rows_by_table.items():
        key_columns = KEY_COLUMNS.get(table)
        if key_columns is None:
            continue
        seen: set[tuple[Any, ...]] = set()
        for index, row in enumerate(rows, start=1):
            key = tuple(row.get(column) for column in key_columns)
            if any(value is None for value in key):
                raise SystemExit(
                    f"missing key column for {table} row {index}: "
                    + ", ".join(key_columns)
                )
            if key in seen:
                raise SystemExit(
                    f"duplicate {table} key at row {index}: "
                    + ", ".join(f"{column}={value!r}" for column, value in zip(key_columns, key, strict=True))
                )
            seen.add(key)


def validate_vocabularies(rows_by_table: dict[str, list[dict[str, Any]]]) -> None:
    for table, rows in rows_by_table.items():
        rules = ALLOWED_VALUES.get(table, {})
        for index, row in enumerate(rows, start=1):
            for column, allowed in rules.items():
                value = row.get(column)
                if value not in allowed:
                    allowed_text = ", ".join(sorted(allowed))
                    raise SystemExit(
                        f"invalid {table}.{column} at row {index}: {value!r}; "
                        f"expected one of: {allowed_text}"
                    )

            if table == "repo_path_rule":
                priority = row.get("priority")
                if (
                    not isinstance(priority, int)
                    or isinstance(priority, bool)
                    or priority < 0
                ):
                    raise SystemExit(
                        f"invalid repo_path_rule.priority at row {index}: {priority!r}; "
                        f"expected a non-negative integer"
                    )

            if table == "surface_option":
                required = row.get("required")
                if (
                    not isinstance(required, int)
                    or isinstance(required, bool)
                    or required not in (0, 1)
                ):
                    raise SystemExit(
                        f"invalid surface_option.required at row {index}: {required!r}; "
                        f"expected integer 0 or 1"
                    )


def require_known_reference(
    *,
    allowed_values: set[str],
    column: str,
    expected_text: str,
    index: int,
    row: dict[str, Any],
    table: str,
    allow_none: bool = False,
) -> None:
    value = row.get(column)
    if value is None and allow_none:
        return
    if value not in allowed_values:
        raise SystemExit(
            f"invalid {table}.{column} at row {index}: {value!r}; "
            f"expected {expected_text}"
        )


def validate_references(rows_by_table: dict[str, list[dict[str, Any]]]) -> None:
    surface_keys = {row["surface_key"] for row in rows_by_table["surface"]}
    artifact_keys = {row["artifact_key"] for row in rows_by_table["artifact_class"]}
    option_keys = {
        (row["surface_key"], row["option_name"], row["option_kind"])
        for row in rows_by_table["surface_option"]
    }

    for index, row in enumerate(rows_by_table["surface_option"], start=1):
        require_known_reference(
            allowed_values=surface_keys,
            column="surface_key",
            expected_text="an existing surface.surface_key",
            index=index,
            row=row,
            table="surface_option",
        )

    for index, row in enumerate(rows_by_table["repo_path_rule"], start=1):
        require_known_reference(
            allowed_values=artifact_keys,
            allow_none=True,
            column="artifact_key",
            expected_text="an existing artifact_class.artifact_key",
            index=index,
            row=row,
            table="repo_path_rule",
        )

    for index, row in enumerate(rows_by_table["surface_io"], start=1):
        require_known_reference(
            allowed_values=surface_keys,
            column="surface_key",
            expected_text="an existing surface.surface_key",
            index=index,
            row=row,
            table="surface_io",
        )
        require_known_reference(
            allowed_values=artifact_keys,
            column="artifact_key",
            expected_text="an existing artifact_class.artifact_key",
            index=index,
            row=row,
            table="surface_io",
        )

    for index, row in enumerate(rows_by_table["surface_option_effect"], start=1):
        surface_key = row.get("surface_key")
        option_name = row.get("option_name")
        option_kind = row.get("option_kind")
        require_known_reference(
            allowed_values=surface_keys,
            column="surface_key",
            expected_text="an existing surface.surface_key",
            index=index,
            row=row,
            table="surface_option_effect",
        )
        if (surface_key, option_name, option_kind) not in option_keys:
            raise SystemExit(
                f"invalid surface_option_effect option tuple at row {index}: "
                f"{surface_key!r}, {option_name!r}, {option_kind!r}; "
                f"expected an existing surface_option row"
            )
        require_known_reference(
            allowed_values=artifact_keys,
            allow_none=True,
            column="artifact_key",
            expected_text="an existing artifact_class.artifact_key",
            index=index,
            row=row,
            table="surface_option_effect",
        )

    for index, row in enumerate(rows_by_table["surface_dependency"], start=1):
        require_known_reference(
            allowed_values=surface_keys,
            column="surface_key",
            expected_text="an existing surface.surface_key",
            index=index,
            row=row,
            table="surface_dependency",
        )
        require_known_reference(
            allowed_values=surface_keys,
            column="dependency_surface_key",
            expected_text="an existing surface.surface_key",
            index=index,
            row=row,
            table="surface_dependency",
        )


def load_inventory_file(inventory_file: Path) -> list[str]:
    if not inventory_file.exists():
        raise SystemExit(f"inventory file does not exist: {inventory_file}")
    if not inventory_file.is_file():
        raise SystemExit(f"inventory file is not a file: {inventory_file}")

    files: list[str] = []
    with inventory_file.open("r", encoding="utf-8") as handle:
        for lineno, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            path = PurePosixPath(line)
            if path.is_absolute() or ".." in path.parts:
                raise SystemExit(
                    f"invalid repo-relative path in inventory file {inventory_file}:{lineno}: {line!r}"
                )
            normalized = path.as_posix()
            if normalized in {"", "."}:
                raise SystemExit(
                    f"invalid repo-relative path in inventory file {inventory_file}:{lineno}: {line!r}"
                )
            files.append(normalized)
    return sorted(files)


def collect_current_files(repo_root: Path, *, inventory_file: Path | None = None) -> list[str]:
    if inventory_file is not None:
        return load_inventory_file(inventory_file)
    if not (repo_root / ".git").exists():
        raise SystemExit(
            "git metadata is unavailable; supply --inventory-file with a newline-delimited "
            "repo-relative file inventory instead of scanning the filesystem"
        )

    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            capture_output=True,
            check=False,
            timeout=GIT_LS_FILES_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise SystemExit(
            "git executable not found while collecting repo inventory"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(
            f"git ls-files timed out after {GIT_LS_FILES_TIMEOUT_SECONDS}s "
            "while collecting repo inventory"
        ) from exc

    if result.returncode == 0:
        return sorted(
            path.decode("utf-8")
            for path in result.stdout.split(b"\0")
            if path
        )

    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    raise SystemExit(
        "git ls-files failed while collecting repo inventory"
        + (f": {stderr}" if stderr else "")
    )


def load_previous_repo_file_rows(output_db: Path) -> dict[str, dict[str, Any]]:
    if not output_db.exists():
        return {}

    conn = sqlite3.connect(output_db)
    try:
        rows = conn.execute(
            """
            SELECT repo_path, tracking_status, path_kind, rule_key, artifact_key
            FROM repo_file
            """
        ).fetchall()
    except sqlite3.DatabaseError:
        sys.stderr.write(
            f"warning: could not read previous repo_file rows from {output_db}; "
            "departed-file preservation will start from the current inventory\n"
        )
        return {}
    finally:
        conn.close()

    return {
        repo_path: {
            "repo_path": repo_path,
            "tracking_status": tracking_status,
            "path_kind": path_kind,
            "rule_key": rule_key,
            "artifact_key": artifact_key,
        }
        for repo_path, tracking_status, path_kind, rule_key, artifact_key in rows
    }


def classify_repo_path(
    repo_path: str,
    path_rules: list[dict[str, Any]],
) -> dict[str, Any]:
    for rule in path_rules:
        if glob_matches(repo_path, rule["glob"]):
            return {
                "path_kind": rule["path_kind"],
                "rule_key": rule["rule_key"],
                "artifact_key": rule.get("artifact_key"),
            }
    return {
        "path_kind": "unknown",
        "rule_key": None,
        "artifact_key": None,
    }


def glob_matches(repo_path: str, pattern: str) -> bool:
    """Match contract globs with '/'-aware *, ?, and recursive ** semantics."""
    path = PurePosixPath(repo_path).as_posix()
    regex = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                regex.append(".*")
                index += 2
                continue
            regex.append("[^/]*")
            index += 1
            continue
        if char == "?":
            regex.append("[^/]")
            index += 1
            continue
        regex.append(re.escape(char))
        index += 1
    return re.fullmatch("".join(regex), path) is not None


def build_repo_file_rows(
    *,
    current_paths: list[str],
    path_rules: list[dict[str, Any]],
    previous_rows: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current_set = set(current_paths)

    for repo_path in current_paths:
        classified = classify_repo_path(repo_path, path_rules)
        rows.append(
            {
                "repo_path": repo_path,
                "tracking_status": "current",
                **classified,
            }
        )

    for repo_path, previous in sorted(previous_rows.items()):
        if repo_path in current_set:
            continue
        rows.append(
            {
                "repo_path": repo_path,
                "tracking_status": "departed",
                "path_kind": previous.get("path_kind") or "unknown",
                "rule_key": previous.get("rule_key"),
                "artifact_key": previous.get("artifact_key"),
            }
        )

    return rows


def insert_rows(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = sorted({key for row in rows for key in row.keys()})
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})"

    values = []
    for row in rows:
        values.append([row.get(column) for column in columns])
    conn.executemany(sql, values)


def build_db(
    *,
    repo_root: Path,
    contracts_dir: Path,
    output_db: Path,
    schema_path: Path,
    inventory_file: Path | None = None,
) -> dict[str, Any]:
    if not repo_root.is_dir():
        raise SystemExit(f"repo root does not exist or is not a directory: {repo_root}")
    if not contracts_dir.is_dir():
        raise SystemExit(f"contracts directory does not exist or is not a directory: {contracts_dir}")
    if not schema_path.is_file():
        raise SystemExit(f"schema file does not exist or is not a file: {schema_path}")
    if output_db.exists() and output_db.is_dir():
        raise SystemExit(f"output db path is a directory: {output_db}")
    if output_db.parent.exists() and not output_db.parent.is_dir():
        raise SystemExit(f"output db parent is not a directory: {output_db.parent}")

    loaded = {}
    for name, _ in DATASETS:
        contract_path = contracts_dir / name
        if not contract_path.is_file():
            raise SystemExit(f"contract file missing or not a regular file: {contract_path}")
        loaded[name] = load_jsonl(contract_path)

    table_rows = {table: loaded[name] for name, table in DATASETS}
    validate_dataset_keys(table_rows)
    validate_vocabularies(table_rows)
    validate_surface_coverage(repo_root, loaded["surfaces.jsonl"])
    validate_references(table_rows)

    path_rules = sorted(
        table_rows["repo_path_rule"],
        key=lambda row: (row["priority"], row["rule_key"]),
    )
    current_files = collect_current_files(repo_root, inventory_file=inventory_file)
    previous_repo_rows = load_previous_repo_file_rows(output_db)
    repo_file_rows = build_repo_file_rows(
        current_paths=current_files,
        path_rules=path_rules,
        previous_rows=previous_repo_rows,
    )
    validate_surface_path_alignment(
        repo_file_rows=repo_file_rows,
        surfaces=table_rows["surface"],
    )

    output_db.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        prefix=f".{output_db.name}.",
        suffix=".tmp",
        dir=output_db.parent,
        delete=False,
    ) as temp_handle:
        temp_db = Path(temp_handle.name)

    try:
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(schema_path.read_text(encoding="utf-8"))

            for _, table in DATASETS:
                insert_rows(conn, table, table_rows[table])
            insert_rows(conn, "repo_file", repo_file_rows)

            current_file_count = sum(1 for row in repo_file_rows if row["tracking_status"] == "current")
            departed_file_count = sum(1 for row in repo_file_rows if row["tracking_status"] == "departed")
            unmapped_current_file_count = sum(
                1
                for row in repo_file_rows
                if row["tracking_status"] == "current" and row["path_kind"] == "unknown"
            )

            meta_rows = [
                ("built_at_utc", utc_now()),
                ("repo_root", str(repo_root)),
                ("contracts_dir", str(contracts_dir)),
                ("output_db", str(output_db)),
                ("surface_count", str(len(table_rows["surface"]))),
                ("artifact_class_count", str(len(table_rows["artifact_class"]))),
                ("repo_path_rule_count", str(len(table_rows["repo_path_rule"]))),
                ("current_repo_file_count", str(current_file_count)),
                ("departed_repo_file_count", str(departed_file_count)),
                ("unmapped_current_repo_file_count", str(unmapped_current_file_count)),
            ]
            conn.executemany(
                "INSERT INTO registry_meta(meta_key, meta_value) VALUES (?, ?)",
                meta_rows,
            )
            conn.commit()
        finally:
            conn.close()

        temp_db.replace(output_db)
    finally:
        if temp_db.exists():
            temp_db.unlink()

    return {
        "ok": True,
        "output_db": str(output_db),
        "contracts_dir": str(contracts_dir),
        "current_repo_file_count": current_file_count,
        "departed_repo_file_count": departed_file_count,
        "unmapped_current_repo_file_count": unmapped_current_file_count,
        "surface_count": len(table_rows["surface"]),
        "artifact_class_count": len(table_rows["artifact_class"]),
    }


def check_db(
    *,
    repo_root: Path,
    contracts_dir: Path,
    output_db: Path,
    schema_path: Path,
    inventory_file: Path | None = None,
) -> dict[str, Any]:
    if output_db.exists() and output_db.is_dir():
        raise SystemExit(f"output db path is a directory: {output_db}")
    if output_db.parent.exists() and not output_db.parent.is_dir():
        raise SystemExit(f"output db parent is not a directory: {output_db.parent}")

    with tempfile.TemporaryDirectory(prefix="pipeline-registry-check-") as temp_dir:
        temp_output_db = Path(temp_dir) / "pipeline_registry.sqlite"
        if output_db.is_file():
            shutil.copy2(output_db, temp_output_db)
        summary = build_db(
            repo_root=repo_root,
            contracts_dir=contracts_dir,
            output_db=temp_output_db,
            schema_path=schema_path,
            inventory_file=inventory_file,
        )

    summary["mode"] = "check"
    summary["wrote_output_db"] = False
    summary["target_output_db"] = str(output_db)
    summary["output_db"] = None
    return summary


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent.parent
    contracts_dir = script_dir / "contracts"
    schema_path = script_dir / "schema.sql"
    default_output = repo_root / "dbs" / "global" / "pipeline_registry.sqlite"

    parser = argparse.ArgumentParser(
        description="Build the auditable pipeline registry SQLite DB from JSONL contracts.",
        epilog=(
            "Examples:\n"
            "  python3 tools/pipeline_registry/build_pipeline_registry.py\n"
            "  python3 tools/pipeline_registry/build_pipeline_registry.py --check\n\n"
            "By default the tool reads JSONL contracts and the repository inventory, "
            "then atomically replaces --output-db after validation succeeds. "
            "--check validates the same inputs through a temporary database without "
            "replacing --output-db."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--repo-root",
        default=str(repo_root),
        help="repository root whose current tracked and untracked files are inventoried",
    )
    parser.add_argument(
        "--inventory-file",
        default=None,
        help=(
            "Optional newline-delimited repo-relative file inventory to use when Git metadata "
            "is unavailable."
        ),
    )
    parser.add_argument(
        "--contracts-dir",
        default=str(contracts_dir),
        help="directory containing pipeline registry JSONL contracts",
    )
    parser.add_argument(
        "--schema",
        default=str(schema_path),
        help="SQLite schema used to materialize the registry",
    )
    parser.add_argument(
        "--output-db",
        default=str(default_output),
        help="SQLite path replaced after a successful build",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate through a temporary database without replacing --output-db",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    builder = check_db if args.check else build_db
    summary = builder(
        repo_root=Path(args.repo_root),
        contracts_dir=Path(args.contracts_dir),
        output_db=Path(args.output_db),
        schema_path=Path(args.schema),
        inventory_file=None if args.inventory_file is None else Path(args.inventory_file),
    )
    if not args.check:
        summary["mode"] = "build"
        summary["wrote_output_db"] = True
    sys.stdout.write(json.dumps(summary, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
