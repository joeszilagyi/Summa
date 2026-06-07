"""Standards-profile crosswalk loading, export, and conformance reporting."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.atomic_write import atomic_write_json  # noqa: E402
from tools.source_db_tools import canonical_store  # noqa: E402

PROFILE_SCHEMA_VERSION = "standards-profile.v1"
EXPORT_SCHEMA_VERSION = "standards-profile-export.v1"
CONFORMANCE_SCHEMA_VERSION = "standards-profile-conformance-report.v1"
PROFILE_DIR = REPO_ROOT / "config" / "standards_profiles"
SUPPORTED_PROFILE_IDS = {
    "dcmi.v1",
    "premis.v1",
    "rico.v1",
    "nara_preservation_readiness.v1",
}
SUPPORTED_EXPORT_FORMATS = {
    "dcterms_json",
    "premis_profile_json",
    "rico_profile_json",
    "readiness_report_json",
}
PUBLIC_SAFE_PUBLICATION_STATES = {"public", "public_safe", "published", "release", "released"}
PRIVATE_PUBLICATION_STATES = {
    "private",
    "private_working",
    "local_only",
    "restricted",
    "blocked",
    "draft",
}
PRIVATE_SENTINEL_PATTERNS = (
    "PRIVATE_SENTINEL",
    "private sentinel",
    "/home/",
    "/Users/",
    "token=",
    "secret=",
    "password=",
)


class StandardsProfileError(RuntimeError):
    """Raised when a standards profile or export cannot be built safely."""


@dataclass(frozen=True)
class ExportResult:
    profile: dict[str, Any]
    export_payload: dict[str, Any]
    conformance_report: dict[str, Any]


def now_rfc3339() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_path(raw_path: str | Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (Path.cwd() / path).resolve()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def stable_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StandardsProfileError(f"could not load {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise StandardsProfileError(f"{label} must be a JSON object: {path}")
    return payload


def profile_path(profile_id: str) -> Path:
    if profile_id not in SUPPORTED_PROFILE_IDS:
        raise StandardsProfileError(f"unknown standards profile id: {profile_id}")
    return PROFILE_DIR / f"{profile_id}.json"


def load_profile(profile_id: str) -> dict[str, Any]:
    profile = load_json(profile_path(profile_id), label=f"standards profile {profile_id}")
    validate_profile_payload(profile)
    return profile


def validate_profile_payload(profile: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "profile_id",
        "external_standard",
        "export_format",
        "conformance_level",
        "summa_source_tables",
        "field_mappings",
        "required_fields",
        "optional_fields",
        "unsupported_fields",
        "lossy_mapping_rules",
        "validation_rules",
    }
    missing = sorted(required - set(profile))
    if missing:
        raise StandardsProfileError(
            "standards profile missing required keys: " + ", ".join(missing)
        )
    if profile["schema_version"] != PROFILE_SCHEMA_VERSION:
        raise StandardsProfileError(
            f"profile {profile.get('profile_id')} has unsupported schema_version"
        )
    if profile["profile_id"] not in SUPPORTED_PROFILE_IDS:
        raise StandardsProfileError(f"unsupported profile_id: {profile['profile_id']}")
    if profile["export_format"] not in SUPPORTED_EXPORT_FORMATS:
        raise StandardsProfileError(f"unsupported export_format: {profile['export_format']}")
    if profile["conformance_level"] not in {"experimental", "partial", "complete", "report-only"}:
        raise StandardsProfileError(f"invalid conformance level: {profile['conformance_level']}")
    mapping_ids = set()
    for index, mapping in enumerate(profile["field_mappings"]):
        if not isinstance(mapping, dict):
            raise StandardsProfileError(f"field_mappings[{index}] must be an object")
        for key in (
            "mapping_id",
            "summa_source",
            "external_target",
            "status",
            "cardinality",
            "lossy",
            "privacy",
            "validation",
        ):
            if key not in mapping:
                raise StandardsProfileError(f"field_mappings[{index}] missing {key}")
        mapping_ids.add(str(mapping["mapping_id"]))
    unknown_required = sorted(set(profile["required_fields"]) - mapping_ids)
    unknown_optional = sorted(set(profile["optional_fields"]) - mapping_ids)
    if unknown_required or unknown_optional:
        raise StandardsProfileError(
            "profile references unknown mapping ids: "
            + ", ".join(unknown_required + unknown_optional)
        )


def table_columns(
    conn: sqlite3.Connection,
    table: str,
    *,
    schema_cache: dict[str, set[str]] | None = None,
) -> set[str]:
    if schema_cache is not None and table in schema_cache:
        return schema_cache[table]
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if schema_cache is not None:
        schema_cache[table] = columns
    return columns


def grouped_rows_by_key(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...],
    *,
    key_field: str,
) -> dict[Any, list[sqlite3.Row]]:
    grouped: dict[Any, list[sqlite3.Row]] = {}
    for row in conn.execute(sql, params):
        grouped.setdefault(row[key_field], []).append(row)
    return grouped


def public_values_from_rows(
    rows: list[sqlite3.Row],
    *,
    include_private: bool,
    value_getter,
) -> tuple[list[str], int]:
    values: list[str] = []
    excluded_count = 0
    for row in rows:
        if not include_private and not _row_is_public(row):
            excluded_count += 1
            continue
        value = value_getter(row)
        if value:
            values.append(value)
    return values, excluded_count


def validate_profile_mappings(
    conn: sqlite3.Connection, profile: dict[str, Any]
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    tables = canonical_store.actual_tables(conn)
    schema_cache: dict[str, set[str]] = {}
    for mapping in profile["field_mappings"]:
        source = mapping["summa_source"]
        table = str(source["table"])
        field = str(source["field"])
        if table not in tables:
            errors.append(
                {
                    "code": "UNKNOWN_SUMMA_TABLE",
                    "mapping_id": mapping["mapping_id"],
                    "source": f"{table}.{field}",
                    "message": f"profile mapping references missing table: {table}",
                }
            )
            continue
        columns = table_columns(conn, table, schema_cache=schema_cache)
        if field not in columns:
            errors.append(
                {
                    "code": "UNKNOWN_SUMMA_FIELD",
                    "mapping_id": mapping["mapping_id"],
                    "source": f"{table}.{field}",
                    "message": f"profile mapping references missing field: {table}.{field}",
                }
            )
    return errors


def public_row_clause(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    placeholders = ", ".join(f"'{state}'" for state in sorted(PRIVATE_PUBLICATION_STATES))
    return (
        f"COALESCE({prefix}public_blocker, '') = '' "
        f"AND COALESCE({prefix}publication_state, 'public_safe') NOT IN ({placeholders})"
    )


def public_conditions_for_table(
    conn: sqlite3.Connection,
    table: str,
    alias: str = "",
    *,
    schema_cache: dict[str, set[str]] | None = None,
) -> list[str]:
    columns = table_columns(conn, table, schema_cache=schema_cache)
    prefix = f"{alias}." if alias else ""
    conditions: list[str] = []
    if "public_blocker" in columns:
        conditions.append(f"COALESCE({prefix}public_blocker, '') = ''")
    if "publication_state" in columns:
        placeholders = ", ".join(f"'{state}'" for state in sorted(PRIVATE_PUBLICATION_STATES))
        conditions.append(
            f"COALESCE({prefix}publication_state, 'public_safe') NOT IN ({placeholders})"
        )
    return conditions


def nonblank(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if value is not None and not isinstance(value, str):
        return str(value)
    return None


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    row_keys = set(row.keys())
    return {key: row[key] for key in row_keys}


def _row_is_public(row: sqlite3.Row) -> bool:
    row_keys = set(row.keys())
    if "public_blocker" in row_keys:
        blocker = row["public_blocker"]
        if isinstance(blocker, str) and blocker.strip():
            return False
    if "publication_state" in row_keys:
        state = row["publication_state"]
        if isinstance(state, str):
            state = state.strip()
            if state and state in PRIVATE_PUBLICATION_STATES:
                return False
    return True


def _split_public_rows(
    rows: list[sqlite3.Row], *, include_private: bool
) -> tuple[list[sqlite3.Row], int]:
    if include_private:
        return rows, 0
    public_rows: list[sqlite3.Row] = []
    excluded_count = 0
    for row in rows:
        if _row_is_public(row):
            public_rows.append(row)
        else:
            excluded_count += 1
    return public_rows, excluded_count


def record_privacy_exclusion(report_bits: dict[str, Any], table: str, excluded_count: int) -> None:
    if excluded_count <= 0:
        return
    for entry in report_bits["privacy_exclusions"]:
        if entry["table"] == table:
            entry["excluded_count"] += excluded_count
            return
    report_bits["privacy_exclusions"].append({"table": table, "excluded_count": excluded_count})


def parse_record_id(value: str | int | None, *, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if ":" in text:
        text = text.rsplit(":", 1)[1]
    if "-" in text:
        text = text.rsplit("-", 1)[1]
    try:
        parsed = int(text)
    except ValueError as exc:
        raise StandardsProfileError(f"{label} must be a numeric id or typed ref: {value}") from exc
    if parsed < 1:
        raise StandardsProfileError(f"{label} must be positive: {value}")
    return parsed


def privacy_exclusions_for_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    schema_cache: dict[str, set[str]] | None = None,
) -> int:
    columns = table_columns(conn, table, schema_cache=schema_cache)
    predicates: list[str] = []
    if "public_blocker" in columns:
        predicates.append("COALESCE(public_blocker, '') <> ''")
    if "publication_state" in columns:
        placeholders = ", ".join(f"'{state}'" for state in sorted(PRIVATE_PUBLICATION_STATES))
        predicates.append(f"COALESCE(publication_state, '') IN ({placeholders})")
    if not predicates:
        return 0
    return int(
        conn.execute(f"SELECT COUNT(*) FROM {table} WHERE " + " OR ".join(predicates)).fetchone()[0]
    )


def _iter_text_fragments(payload: Any):
    if isinstance(payload, str):
        yield payload
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield from _iter_text_fragments(key)
            yield from _iter_text_fragments(value)
        return
    if isinstance(payload, (list, tuple, set)):
        for item in payload:
            yield from _iter_text_fragments(item)


def has_private_sentinel(payload: Any) -> bool:
    for text in _iter_text_fragments(payload):
        lowered = text.lower()
        if any(pattern.lower() in lowered for pattern in PRIVATE_SENTINEL_PATTERNS):
            return True
    return False


def safe_base_uri(base_uri: str | None) -> str:
    if not base_uri:
        raise StandardsProfileError("profile rico.v1 requires --base-uri")
    parsed = urlparse(base_uri)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise StandardsProfileError(f"base URI must be an http(s) absolute URI: {base_uri}")
    return base_uri if base_uri.endswith("/") else base_uri + "/"


def node_uri(base_uri: str, *parts: Any) -> str:
    safe_parts = [quote(str(part).strip().replace(" ", "_"), safe="._-") for part in parts]
    return base_uri + "/".join(part for part in safe_parts if part)


def work_rows(
    conn: sqlite3.Connection, *, work_id: int | None, subject_id: str | None, include_private: bool
) -> tuple[list[sqlite3.Row], int]:
    where = []
    params: list[Any] = []
    if work_id is not None:
        where.append("work_id=?")
        params.append(work_id)
    if subject_id is not None:
        where.append("workspace_id=?")
        params.append(subject_id)
    sql = (
        "SELECT * FROM work"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY work_id"
    )
    rows = conn.execute(sql, tuple(params)).fetchall()
    public_rows, excluded_count = _split_public_rows(rows, include_private=include_private)
    if work_id is not None and not public_rows:
        raise StandardsProfileError(
            f"work not found or not public under current export policy: {work_id}"
        )
    return public_rows, excluded_count


def capture_rows(
    conn: sqlite3.Connection,
    *,
    capture_id: int | None,
    subject_id: str | None,
    include_private: bool,
) -> tuple[list[sqlite3.Row], int]:
    where = []
    params: list[Any] = []
    if capture_id is not None:
        where.append("capture_event_id=?")
        params.append(capture_id)
    if subject_id is not None:
        where.append("workspace_id=?")
        params.append(subject_id)
    sql = (
        "SELECT * FROM capture_event"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY capture_event_id"
    )
    rows = conn.execute(sql, tuple(params)).fetchall()
    public_rows, excluded_count = _split_public_rows(rows, include_private=include_private)
    if capture_id is not None and not public_rows:
        raise StandardsProfileError(
            f"capture event not found or not public under current export policy: {capture_id}"
        )
    return public_rows, excluded_count


def provenance_summary(conn: sqlite3.Connection, event_key: str | None) -> dict[str, Any] | None:
    if not event_key:
        return None
    row = conn.execute(
        """
        SELECT event_type, event_timestamp, tool_name, actor_type, actor_id
        FROM provenance_event
        WHERE provenance_event_key_v1=?
        """,
        (event_key,),
    ).fetchone()
    if row is None:
        return None
    return {
        "event_type": row["event_type"],
        "event_timestamp": row["event_timestamp"],
        "tool_name": row["tool_name"],
        "actor_type": row["actor_type"],
        "actor_id": row["actor_id"],
    }


def source_urls_for_work(
    conn: sqlite3.Connection, work_id: int, *, include_private: bool
) -> tuple[list[str], int]:
    where = ["work_id=?"]
    rows = conn.execute(
        "SELECT canonical_url, original_locator, public_blocker, publication_state FROM source_access WHERE "
        + " AND ".join(where)
        + " ORDER BY source_access_id",
        (work_id,),
    ).fetchall()
    values: list[str] = []
    excluded_count = 0
    for row in rows:
        if not include_private and not _row_is_public(row):
            excluded_count += 1
            continue
        value = nonblank(row["canonical_url"]) or nonblank(row["original_locator"])
        if value and value.startswith(("http://", "https://")):
            values.append(value)
    return values, excluded_count


def descriptions_for_work(
    conn: sqlite3.Connection, work_id: int, *, include_private: bool
) -> tuple[list[str], int]:
    where = ["about_object_ref=?"]
    params: list[Any] = [f"work:{work_id}"]
    rows = conn.execute(
        "SELECT public_summary, public_blocker, publication_state FROM source_claim WHERE "
        + " AND ".join(where)
        + " ORDER BY source_claim_id",
        tuple(params),
    ).fetchall()
    values: list[str] = []
    excluded_count = 0
    for row in rows:
        if not include_private and not _row_is_public(row):
            excluded_count += 1
            continue
        if value := nonblank(row["public_summary"]):
            values.append(value)
    return values, excluded_count


def subjects_for_work(
    conn: sqlite3.Connection, work_id: int, *, include_private: bool
) -> tuple[list[str], int]:
    where = ["from_object_ref=?"]
    params: list[Any] = [f"work:{work_id}"]
    rows = conn.execute(
        """
        SELECT target_label, predicate, public_blocker, publication_state
        FROM source_relationship
        WHERE {}
        ORDER BY source_relationship_id
        """.format(" AND ".join(where)),
        tuple(params),
    ).fetchall()
    values: list[str] = []
    excluded_count = 0
    for row in rows:
        if not include_private and not _row_is_public(row):
            excluded_count += 1
            continue
        label = nonblank(row["target_label"])
        if label:
            values.append(label)
    return values, excluded_count


def build_dcmi_export(
    conn: sqlite3.Connection,
    profile: dict[str, Any],
    *,
    work_id: int | None,
    subject_id: str | None,
    include_private: bool,
    generated_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rows, excluded_count = work_rows(
        conn, work_id=work_id, subject_id=subject_id, include_private=include_private
    )
    records: list[dict[str, Any]] = []
    report_bits = new_report_bits(profile)
    record_privacy_exclusion(report_bits, "work", excluded_count)
    work_ids = [int(row["work_id"]) for row in rows]
    work_refs = [f"work:{row['work_id']}" for row in rows]
    source_access_by_work: dict[Any, list[sqlite3.Row]] = {}
    source_claim_by_work_ref: dict[Any, list[sqlite3.Row]] = {}
    source_relationship_by_work_ref: dict[Any, list[sqlite3.Row]] = {}
    provenance_by_key: dict[Any, dict[str, Any]] = {}
    if work_ids:
        work_id_placeholders = ", ".join("?" for _ in work_ids)
        source_access_by_work = grouped_rows_by_key(
            conn,
            (
                "SELECT work_id, canonical_url, original_locator, public_blocker, publication_state "
                f"FROM source_access WHERE work_id IN ({work_id_placeholders}) "
                "ORDER BY work_id, source_access_id"
            ),
            tuple(work_ids),
            key_field="work_id",
        )
        source_claim_by_work_ref = grouped_rows_by_key(
            conn,
            (
                "SELECT about_object_ref, public_summary, public_blocker, publication_state "
                f"FROM source_claim WHERE about_object_ref IN ({', '.join('?' for _ in work_refs)}) "
                "ORDER BY source_claim_id"
            ),
            tuple(work_refs),
            key_field="about_object_ref",
        )
        source_relationship_by_work_ref = grouped_rows_by_key(
            conn,
            (
                "SELECT from_object_ref, target_label, predicate, public_blocker, publication_state "
                f"FROM source_relationship WHERE from_object_ref IN ({', '.join('?' for _ in work_refs)}) "
                "ORDER BY source_relationship_id"
            ),
            tuple(work_refs),
            key_field="from_object_ref",
        )
        provenance_refs = list(
            dict.fromkeys(
                nonblank(row["provenance_event_ref"])
                for row in rows
                if nonblank(row["provenance_event_ref"])
            )
        )
        if provenance_refs:
            provenance_placeholders = ", ".join("?" for _ in provenance_refs)
            for row in conn.execute(
                (
                    "SELECT provenance_event_key_v1, event_type, event_timestamp, tool_name, actor_type, actor_id "
                    f"FROM provenance_event WHERE provenance_event_key_v1 IN ({provenance_placeholders}) "
                    "ORDER BY provenance_event_id"
                ),
                tuple(provenance_refs),
            ):
                provenance_by_key[row["provenance_event_key_v1"]] = {
                    "event_type": row["event_type"],
                    "event_timestamp": row["event_timestamp"],
                    "tool_name": row["tool_name"],
                    "actor_type": row["actor_type"],
                    "actor_id": row["actor_id"],
                }
    for row in rows:
        work_ref = f"work:{row['work_id']}"
        work_id_value = int(row["work_id"])
        metadata: dict[str, Any] = {}
        title = nonblank(row["title"])
        if title:
            metadata["dcterms:title"] = title
            satisfy(report_bits, "dcmi.title")
        else:
            missing(report_bits, "dcmi.title", f"{work_ref} missing title")
        identifier = nonblank(row["work_key_v1"])
        if identifier:
            metadata["dcterms:identifier"] = [identifier]
            satisfy(report_bits, "dcmi.identifier.work")
        else:
            missing(report_bits, "dcmi.identifier.work", f"{work_ref} missing work_key_v1")
        if value := nonblank(row["work_type"]):
            metadata["dcterms:type"] = value
            optional(report_bits, "dcmi.type")
        urls, excluded_count = public_values_from_rows(
            source_access_by_work.get(work_id_value, []),
            include_private=include_private,
            value_getter=lambda source_row: (
                value
                if (
                    value := nonblank(source_row["canonical_url"])
                    or nonblank(source_row["original_locator"])
                )
                and value.startswith(("http://", "https://"))
                else None
            ),
        )
        record_privacy_exclusion(report_bits, "source_access", excluded_count)
        if urls:
            metadata["dcterms:source"] = urls
            optional(report_bits, "dcmi.source.url")
        if value := nonblank(row["first_seen_at"]):
            metadata["dcterms:date"] = value
            optional(report_bits, "dcmi.date")
        descriptions, excluded_count = public_values_from_rows(
            source_claim_by_work_ref.get(work_ref, []),
            include_private=include_private,
            value_getter=lambda claim_row: nonblank(claim_row["public_summary"]),
        )
        record_privacy_exclusion(report_bits, "source_claim", excluded_count)
        if descriptions:
            metadata["dcterms:description"] = descriptions
            optional(report_bits, "dcmi.description")
        subjects, excluded_count = public_values_from_rows(
            source_relationship_by_work_ref.get(work_ref, []),
            include_private=include_private,
            value_getter=lambda rel_row: nonblank(rel_row["target_label"]),
        )
        record_privacy_exclusion(report_bits, "source_relationship", excluded_count)
        if subjects:
            metadata["dcterms:subject"] = subjects
            optional(report_bits, "dcmi.subject")
        if value := nonblank(row["rights_posture"]):
            metadata["dcterms:rights"] = value
            optional(report_bits, "dcmi.rights")
        if provenance_ref := nonblank(row["provenance_event_ref"]):
            provenance = provenance_by_key.get(provenance_ref)
        else:
            provenance = None
        if provenance:
            metadata["dcterms:provenance"] = provenance
            optional(report_bits, "dcmi.provenance")
        records.append(
            {"record_type": "work", "summa_ref": work_ref, "metadata": metadata}
        )
    payload = base_export_payload(
        profile, generated_at=generated_at, include_private=include_private
    )
    payload["records"] = records
    return payload, report_bits


def build_premis_export(
    conn: sqlite3.Connection,
    profile: dict[str, Any],
    *,
    capture_id: int | None,
    subject_id: str | None,
    include_private: bool,
    generated_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    captures, excluded_count = capture_rows(
        conn, capture_id=capture_id, subject_id=subject_id, include_private=include_private
    )
    report_bits = new_report_bits(profile)
    record_privacy_exclusion(report_bits, "capture_event", excluded_count)
    objects: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    agents: dict[str, dict[str, Any]] = {}
    rights: list[dict[str, Any]] = []
    capture_work_ids = list(
        dict.fromkeys(int(row["work_id"]) for row in captures if row["work_id"] is not None)
    )
    provenance_refs = list(
        dict.fromkeys(
            nonblank(row["provenance_event_ref"])
            for row in captures
            if nonblank(row["provenance_event_ref"])
        )
    )
    provenance_by_key: dict[Any, dict[str, Any]] = {}
    work_rights_by_id: dict[Any, str | None] = {}
    if provenance_refs:
        provenance_placeholders = ", ".join("?" for _ in provenance_refs)
        for row in conn.execute(
            (
                "SELECT provenance_event_key_v1, event_type, event_timestamp, tool_name, actor_type, actor_id "
                f"FROM provenance_event WHERE provenance_event_key_v1 IN ({provenance_placeholders}) "
                "ORDER BY provenance_event_id"
            ),
            tuple(provenance_refs),
        ):
            provenance_by_key[row["provenance_event_key_v1"]] = {
                "event_type": row["event_type"],
                "event_timestamp": row["event_timestamp"],
                "tool_name": row["tool_name"],
                "actor_type": row["actor_type"],
                "actor_id": row["actor_id"],
            }
    if capture_work_ids:
        work_placeholders = ", ".join("?" for _ in capture_work_ids)
        work_rights_by_id = {
            int(work_id): nonblank(rows[0]["rights_posture"])
            for work_id, rows in grouped_rows_by_key(
                conn,
                f"SELECT work_id, rights_posture FROM work WHERE work_id IN ({work_placeholders}) ORDER BY work_id",
                tuple(capture_work_ids),
                key_field="work_id",
            ).items()
        }
    for row in captures:
        capture_ref = f"capture_event:{row['capture_event_id']}"
        obj: dict[str, Any] = {
            "object_identifier": capture_ref,
            "summa_ref": capture_ref,
            "size": row["byte_count"],
            "format": row["mime_type"],
        }
        satisfy(report_bits, "premis.object.identifier")
        if value := nonblank(row["content_hash"]):
            obj["fixity"] = {
                "message_digest_algorithm": "sha256-or-declared",
                "message_digest": value,
            }
            satisfy(report_bits, "premis.object.fixity")
        else:
            missing(report_bits, "premis.object.fixity", f"{capture_ref} missing content_hash")
        if row["byte_count"] is not None:
            optional(report_bits, "premis.object.size")
        if row["mime_type"] is not None:
            optional(report_bits, "premis.object.format")
        objects.append(obj)
        if captured_at := nonblank(row["captured_at"]):
            events.append(
                {
                    "event_identifier": f"event:{capture_ref}:capture",
                    "event_type": row["capture_method"] or "capture",
                    "event_datetime": captured_at,
                    "linked_object": capture_ref,
                }
            )
            satisfy(report_bits, "premis.event.capture")
        else:
            missing(report_bits, "premis.event.capture", f"{capture_ref} missing captured_at")
        if provenance_ref := nonblank(row["provenance_event_ref"]):
            provenance = provenance_by_key.get(provenance_ref)
        else:
            provenance = None
        if provenance:
            events.append({"event_identifier": f"event:{capture_ref}:provenance", **provenance})
            optional(report_bits, "premis.event.provenance")
            tool = nonblank(provenance.get("tool_name"))
            if tool:
                agents[tool] = {
                    "agent_identifier": f"agent:tool:{tool}",
                    "agent_name": tool,
                    "agent_type": "software",
                }
                optional(report_bits, "premis.agent.tool")
        if row["work_id"] is not None:
            rights_posture = work_rights_by_id.get(int(row["work_id"]))
            if rights_posture:
                rights.append(
                    {"linked_object": capture_ref, "rights_statement": rights_posture}
                )
                optional(report_bits, "premis.rights.posture")
    payload = base_export_payload(
        profile, generated_at=generated_at, include_private=include_private
    )
    payload["premis"] = {
        "objects": objects,
        "events": events,
        "agents": list(agents.values()),
        "rights": rights,
    }
    return payload, report_bits


def build_rico_export(
    conn: sqlite3.Connection,
    profile: dict[str, Any],
    *,
    subject_id: str | None,
    work_id: int | None,
    base_uri: str | None,
    include_private: bool,
    generated_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    base = safe_base_uri(base_uri)
    report_bits = new_report_bits(profile)
    nodes: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    where = []
    params: list[Any] = []
    if work_id is not None:
        where.append("work_id=?")
        params.append(work_id)
    if subject_id is not None:
        where.append("workspace_id=?")
        params.append(subject_id)
    work_sql = (
        "SELECT * FROM work" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY work_id"
    )
    work_excluded_count = 0
    work_included_count = 0
    for row in conn.execute(work_sql, tuple(params)):
        if not include_private and not _row_is_public(row):
            work_excluded_count += 1
            continue
        work_included_count += 1
        work_ref = f"work:{row['work_id']}"
        title = nonblank(row["title"])
        node = {
            "id": node_uri(base, "work", row["work_id"]),
            "type": "rico:RecordResource",
            "summa_ref": work_ref,
            "label": title,
            "identifier": row["work_key_v1"],
        }
        nodes.append(node)
        satisfy(report_bits, "rico.record_resource")
        if title:
            satisfy(report_bits, "rico.record_title")
        else:
            missing(report_bits, "rico.record_title", f"{work_ref} missing title")
    record_privacy_exclusion(report_bits, "work", work_excluded_count)
    if work_id is not None and work_included_count == 0:
        raise StandardsProfileError(f"work not found or not public under current export policy: {work_id}")
    auth_excluded_count = 0
    for row in conn.execute("SELECT * FROM authority_record ORDER BY authority_record_id"):
        if not include_private and not _row_is_public(row):
            auth_excluded_count += 1
            continue
        nodes.append(
            {
                "id": node_uri(base, "authority", row["authority_record_id"]),
                "type": "rico:Agent",
                "summa_ref": f"authority_record:{row['authority_record_id']}",
                "label": row["preferred_label"],
                "authority_type": row["authority_type"],
            }
        )
        optional(report_bits, "rico.agent")
    record_privacy_exclusion(report_bits, "authority_record", auth_excluded_count)
    rel_excluded_count = 0
    for row in conn.execute(
        "SELECT * FROM source_relationship ORDER BY source_relationship_id"
    ):
        if not include_private and not _row_is_public(row):
            rel_excluded_count += 1
            continue
        relations.append(
            {
                "id": node_uri(base, "relationship", row["source_relationship_id"]),
                "type": "rico:Relation",
                "summa_ref": f"source_relationship:{row['source_relationship_id']}",
                "predicate": row["predicate"],
                "from": row["from_object_ref"],
                "to": row["to_object_ref"],
                "label": row["target_label"],
            }
        )
        optional(report_bits, "rico.relation")
    record_privacy_exclusion(report_bits, "source_relationship", rel_excluded_count)
    for row in conn.execute(
        "SELECT provenance_event_id, event_type, event_timestamp FROM provenance_event ORDER BY provenance_event_id"
    ):
        nodes.append(
            {
                "id": node_uri(base, "event", row["provenance_event_id"]),
                "type": "rico:Event",
                "summa_ref": f"provenance_event:{row['provenance_event_id']}",
                "event_type": row["event_type"],
                "event_datetime": row["event_timestamp"],
            }
        )
        optional(report_bits, "rico.event")
    payload = base_export_payload(
        profile, generated_at=generated_at, include_private=include_private
    )
    payload["base_uri"] = base
    payload["rico_profile_json"] = {"nodes": nodes, "relations": relations}
    return payload, report_bits


def build_nara_readiness_report(
    conn: sqlite3.Connection,
    profile: dict[str, Any],
    *,
    subject_id: str | None,
    include_private: bool,
    generated_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    del include_private
    report_bits = new_report_bits(profile)
    subject_filter = "WHERE workspace_id=?" if subject_id else ""
    subject_params = (subject_id,) if subject_id else ()
    capture_metrics = conn.execute(
        """
        SELECT
            COUNT(*) AS capture_count,
            COALESCE(SUM(CASE WHEN content_hash IS NOT NULL AND TRIM(content_hash) <> '' THEN 1 ELSE 0 END), 0) AS fixity_count,
            COALESCE(SUM(CASE WHEN captured_at IS NOT NULL AND TRIM(captured_at) <> '' THEN 1 ELSE 0 END), 0) AS timestamp_count,
            COALESCE(SUM(CASE WHEN mime_type IS NOT NULL AND TRIM(mime_type) <> '' THEN 1 ELSE 0 END), 0) AS format_count,
            COALESCE(SUM(CASE WHEN payload_storage_policy_class IS NOT NULL AND TRIM(payload_storage_policy_class) <> '' THEN 1 ELSE 0 END), 0) AS payload_policy_count
        FROM capture_event
        """
        + (f" {subject_filter}" if subject_filter else ""),
        subject_params,
    ).fetchone()
    capture_count = int(capture_metrics[0])
    fixity_count = int(capture_metrics[1])
    timestamp_count = int(capture_metrics[2])
    provenance_count = int(conn.execute("SELECT COUNT(*) FROM provenance_event").fetchone()[0])
    format_count = int(capture_metrics[3])
    payload_policy_count = int(capture_metrics[4])
    history_count = int(conn.execute("SELECT COUNT(*) FROM review_state_history").fetchone()[0])
    checks = [
        readiness_check(
            "fixity_present",
            fixity_count > 0 and fixity_count == capture_count,
            fixity_count,
            capture_count,
        ),
        readiness_check("actions_recorded", provenance_count > 0, provenance_count, None),
        readiness_check(
            "capture_timestamps",
            timestamp_count > 0 and timestamp_count == capture_count,
            timestamp_count,
            capture_count,
        ),
        readiness_check(
            "format_recorded", format_count > 0, format_count, capture_count, required=False
        ),
        readiness_check(
            "raw_payload_policy_recorded",
            payload_policy_count > 0,
            payload_policy_count,
            capture_count,
            required=False,
        ),
        readiness_check(
            "review_audit_present", history_count > 0, history_count, None, required=False
        ),
        {
            "check_id": "transfer_package_present",
            "status": "not_applicable",
            "required": False,
            "evidence_count": 0,
            "expected_count": None,
            "message": "F33 readiness report does not build a NARA transfer package.",
        },
    ]
    if fixity_count:
        satisfy(report_bits, "nara.fixity.recorded")
    else:
        missing(report_bits, "nara.fixity.recorded", "no capture_event.content_hash values found")
    if provenance_count:
        satisfy(report_bits, "nara.actions.recorded")
    else:
        missing(report_bits, "nara.actions.recorded", "no provenance_event rows found")
    if timestamp_count:
        satisfy(report_bits, "nara.capture.timestamps")
    else:
        missing(report_bits, "nara.capture.timestamps", "no capture_event.captured_at values found")
    if format_count:
        optional(report_bits, "nara.format.recorded")
    if payload_policy_count:
        optional(report_bits, "nara.payload.policy")
    if history_count:
        optional(report_bits, "nara.review.audit")
    payload = base_export_payload(profile, generated_at=generated_at, include_private=False)
    payload["readiness_report"] = {
        "summary": {
            "capture_event_count": capture_count,
            "fixity_count": fixity_count,
            "provenance_event_count": provenance_count,
            "review_state_history_count": history_count,
            "claim": "readiness report only; not a NARA transfer package",
        },
        "checks": checks,
    }
    return payload, report_bits


def readiness_check(
    check_id: str,
    passed: bool,
    evidence_count: int,
    expected_count: int | None,
    *,
    required: bool = True,
) -> dict[str, Any]:
    status = "pass" if passed else ("fail" if required else "warn")
    return {
        "check_id": check_id,
        "status": status,
        "required": required,
        "evidence_count": evidence_count,
        "expected_count": expected_count,
    }


def base_export_payload(
    profile: dict[str, Any], *, generated_at: str, include_private: bool
) -> dict[str, Any]:
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "profile_id": profile["profile_id"],
        "profile_name": profile["profile_name"],
        "standard_name": profile["external_standard"]["name"],
        "standard_reference": profile["external_standard"]["reference_url"],
        "export_format": profile["export_format"],
        "generated_at": generated_at,
        "public_mode": not include_private,
        "internal_mode": include_private,
        "canonical_model_renamed": False,
    }


def new_report_bits(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "required_satisfied": set(),
        "required_missing": [],
        "optional_emitted": set(),
        "privacy_exclusions": [],
        "controlled_vocabulary_warnings": [],
        "cardinality_warnings": [],
        "mapping_errors": [],
        "unsupported_fields": list(profile.get("unsupported_fields", [])),
        "lossy_mappings": list(profile.get("lossy_mapping_rules", [])),
    }


def satisfy(report_bits: dict[str, Any], mapping_id: str) -> None:
    report_bits["required_satisfied"].add(mapping_id)


def missing(report_bits: dict[str, Any], mapping_id: str, reason: str) -> None:
    entry = {"mapping_id": mapping_id, "reason": reason}
    if entry not in report_bits["required_missing"]:
        report_bits["required_missing"].append(entry)


def optional(report_bits: dict[str, Any], mapping_id: str) -> None:
    report_bits["optional_emitted"].add(mapping_id)


def conformance_report(
    *,
    profile: dict[str, Any],
    export_payload: dict[str, Any],
    report_bits: dict[str, Any],
    db_path: Path,
    generated_at: str,
    scope: dict[str, Any],
    export_path: Path | None = None,
) -> dict[str, Any]:
    missing_ids = {item["mapping_id"] for item in report_bits["required_missing"]}
    for mapping_id in profile["required_fields"]:
        if mapping_id not in report_bits["required_satisfied"] and mapping_id not in missing_ids:
            report_bits["required_missing"].append(
                {"mapping_id": mapping_id, "reason": "required mapping was not emitted"}
            )
    validation_errors = validate_export_payload(export_payload, profile=profile)
    if has_private_sentinel(export_payload) and not export_payload.get("internal_mode"):
        validation_errors.append(
            {
                "code": "PRIVATE_SENTINEL_IN_PUBLIC_EXPORT",
                "message": "public export contains private sentinel data",
            }
        )
    required_missing = report_bits["required_missing"]
    validation_status = "fail" if required_missing or validation_errors else "pass"
    if profile["conformance_level"] == "report-only":
        conformance_status = "report_only"
    elif validation_status == "fail":
        conformance_status = "fail"
    elif (
        report_bits["lossy_mappings"]
        or report_bits["unsupported_fields"]
        or report_bits["controlled_vocabulary_warnings"]
    ):
        conformance_status = "pass_with_warnings"
    else:
        conformance_status = "pass"
    return {
        "schema_version": CONFORMANCE_SCHEMA_VERSION,
        "profile_id": profile["profile_id"],
        "standard_name": profile["external_standard"]["name"],
        "standard_version_or_reference": profile["external_standard"]["version_or_reference"],
        "export_artifact_path": None if export_path is None else str(export_path),
        "export_artifact_hash": None
        if export_path is None or not export_path.exists()
        else hash_file(export_path),
        "canonical_db_source": str(db_path),
        "scope": scope,
        "records_exported": record_count(export_payload),
        "required_fields_satisfied": sorted(report_bits["required_satisfied"]),
        "required_fields_missing": required_missing,
        "optional_fields_emitted": sorted(report_bits["optional_emitted"]),
        "unsupported_fields": report_bits["unsupported_fields"],
        "lossy_mappings": report_bits["lossy_mappings"],
        "privacy_exclusions": report_bits["privacy_exclusions"],
        "controlled_vocabulary_warnings": report_bits["controlled_vocabulary_warnings"],
        "cardinality_warnings": report_bits["cardinality_warnings"],
        "validation_errors": validation_errors,
        "validation_status": validation_status,
        "conformance_status": conformance_status,
        "limitations": list(profile.get("known_limitations", [])),
        "generated_at": generated_at,
    }


def record_count(export_payload: dict[str, Any]) -> int:
    if isinstance(export_payload.get("records"), list):
        return len(export_payload["records"])
    if isinstance(export_payload.get("premis"), dict):
        return len(export_payload["premis"].get("objects", []))
    if isinstance(export_payload.get("rico_profile_json"), dict):
        return len(export_payload["rico_profile_json"].get("nodes", []))
    if isinstance(export_payload.get("readiness_report"), dict):
        return len(export_payload["readiness_report"].get("checks", []))
    return 0


def validate_export_payload(
    export_payload: dict[str, Any], *, profile: dict[str, Any]
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if export_payload.get("schema_version") != EXPORT_SCHEMA_VERSION:
        errors.append(
            {
                "code": "INVALID_SCHEMA_VERSION",
                "message": "export payload schema_version is invalid",
            }
        )
    if export_payload.get("profile_id") != profile["profile_id"]:
        errors.append(
            {
                "code": "PROFILE_MISMATCH",
                "message": "export payload profile_id does not match profile",
            }
        )
    if profile["profile_id"] == "dcmi.v1":
        for record in export_payload.get("records", []):
            metadata = record.get("metadata", {})
            if not metadata.get("dcterms:title"):
                errors.append(
                    {
                        "code": "DCMI_TITLE_MISSING",
                        "message": f"{record.get('summa_ref')} missing dcterms:title",
                    }
                )
            if not metadata.get("dcterms:identifier"):
                errors.append(
                    {
                        "code": "DCMI_IDENTIFIER_MISSING",
                        "message": f"{record.get('summa_ref')} missing dcterms:identifier",
                    }
                )
    elif profile["profile_id"] == "premis.v1":
        premis = export_payload.get("premis", {})
        if not premis.get("objects"):
            errors.append(
                {
                    "code": "PREMIS_OBJECTS_MISSING",
                    "message": "PREMIS profile export has no objects",
                }
            )
        if not premis.get("events"):
            errors.append(
                {"code": "PREMIS_EVENTS_MISSING", "message": "PREMIS profile export has no events"}
            )
        for obj in premis.get("objects", []):
            if not obj.get("fixity"):
                errors.append(
                    {
                        "code": "PREMIS_FIXITY_MISSING",
                        "message": f"{obj.get('summa_ref')} missing fixity",
                    }
                )
    elif profile["profile_id"] == "rico.v1":
        graph = export_payload.get("rico_profile_json", {})
        for node in graph.get("nodes", []):
            node_id = str(node.get("id", ""))
            if not node_id.startswith(("http://", "https://")) or " " in node_id:
                errors.append(
                    {
                        "code": "RICO_NODE_ID_INVALID",
                        "message": f"invalid RiC-O profile node id: {node_id}",
                    }
                )
    return errors


def export_profile(
    *,
    db_path: Path,
    profile_id: str,
    output_path: Path | None = None,
    conformance_report_path: Path | None = None,
    work_id: str | int | None = None,
    capture_id: str | int | None = None,
    subject_id: str | None = None,
    base_uri: str | None = None,
    include_private: bool = False,
    generated_at: str | None = None,
    strict: bool = False,
) -> ExportResult:
    profile = load_profile(profile_id)
    db_path = resolve_path(db_path)
    if not db_path.exists():
        raise StandardsProfileError(f"canonical DB does not exist: {db_path}")
    canonical_store.check_canonical_store(db_path)
    timestamp = generated_at or now_rfc3339()
    parsed_work_id = parse_record_id(work_id, label="work id")
    parsed_capture_id = parse_record_id(capture_id, label="capture id")
    scope = {
        "work_id": parsed_work_id,
        "capture_id": parsed_capture_id,
        "subject_id": subject_id,
        "include_private": include_private,
        "base_uri": base_uri,
    }
    conn = canonical_store.connect_existing_read_only(db_path)
    try:
        mapping_errors = validate_profile_mappings(conn, profile)
        if mapping_errors:
            raise StandardsProfileError(
                f"profile {profile_id} has invalid Summa mapping references: {mapping_errors[0]['message']}"
            )
        if profile_id == "dcmi.v1":
            export_payload, report_bits = build_dcmi_export(
                conn,
                profile,
                work_id=parsed_work_id,
                subject_id=subject_id,
                include_private=include_private,
                generated_at=timestamp,
            )
        elif profile_id == "premis.v1":
            export_payload, report_bits = build_premis_export(
                conn,
                profile,
                capture_id=parsed_capture_id,
                subject_id=subject_id,
                include_private=include_private,
                generated_at=timestamp,
            )
        elif profile_id == "rico.v1":
            export_payload, report_bits = build_rico_export(
                conn,
                profile,
                subject_id=subject_id,
                work_id=parsed_work_id,
                base_uri=base_uri,
                include_private=include_private,
                generated_at=timestamp,
            )
        elif profile_id == "nara_preservation_readiness.v1":
            export_payload, report_bits = build_nara_readiness_report(
                conn,
                profile,
                subject_id=subject_id,
                include_private=include_private,
                generated_at=timestamp,
            )
        else:  # pragma: no cover - guarded by load_profile
            raise StandardsProfileError(f"unsupported profile id: {profile_id}")
    finally:
        conn.close()

    output_resolved = resolve_path(output_path) if output_path is not None else None
    if output_resolved is not None:
        output_resolved.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(output_resolved, export_payload)
    report = conformance_report(
        profile=profile,
        export_payload=export_payload,
        report_bits=report_bits,
        db_path=db_path,
        generated_at=timestamp,
        scope=scope,
        export_path=output_resolved,
    )
    report_resolved = (
        resolve_path(conformance_report_path) if conformance_report_path is not None else None
    )
    if report_resolved is not None:
        report_resolved.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(report_resolved, report)
    if strict and report["validation_status"] == "fail":
        raise StandardsProfileError(f"profile {profile_id} export failed conformance validation")
    return ExportResult(profile=profile, export_payload=export_payload, conformance_report=report)
