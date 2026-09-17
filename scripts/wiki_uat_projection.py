#!/usr/bin/env python3
"""Shared primitives for deterministic Markdown-to-UAT projection tooling."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from local_env import load_local_env


ROOT = Path(__file__).resolve().parents[1]
TARGET_DATABASE = "AI_Animation_UAT"
PRODUCTION_DATABASE = "AI_Animation"
PROJECTION_SCHEMA = "wiki-uat-projection.v1"

ARTICLE_COLUMNS = [
    "article_id",
    "document_id",
    "vendor_article_id",
    "article_title",
    "content_title",
    "content_description",
    "topic",
    "category",
    "tone",
    "tone_sentiment",
    "event_type",
    "document_type_id",
    "document_type_name",
    "product_type",
    "article_status",
    "group_title",
    "news_type",
    "published_date",
    "vendor_indexed_time",
    "indexed_date_time",
    "last_updated",
    "uploaded_by",
    "last_updated_by",
]
COVERAGE_COLUMNS = [
    "coverage_id",
    "coverage_type",
    "display_name",
    "country",
    "media_outlet_category",
    "url",
]
MEDIA_COLUMNS = ["media_id", "file_name", "media_url", "media_type", "source"]


class ProjectionError(RuntimeError):
    """A validation or safety failure in the projection workflow."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_hash(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def _mysql_command(database: str = TARGET_DATABASE) -> tuple[list[str], dict[str, str]]:
    """Build a read/write client command without ever selecting production."""

    load_local_env()
    command = [
        os.environ.get("ISSUE_RADAR_MYSQL_PROGRAM", "mysql"),
        "--batch",
        "--skip-column-names",
        "--default-character-set=utf8mb4",
    ]
    defaults_file = os.environ.get("ISSUE_RADAR_MYSQL_DEFAULTS_FILE")
    login_path = os.environ.get("ISSUE_RADAR_MYSQL_LOGIN_PATH")
    host = os.environ.get("ISSUE_RADAR_MYSQL_HOST") or os.environ.get("DB_HOST")
    port = os.environ.get("ISSUE_RADAR_MYSQL_PORT") or os.environ.get("DB_PORT")
    user = os.environ.get("ISSUE_RADAR_MYSQL_USER") or os.environ.get("DB_USER")
    if defaults_file:
        command.insert(1, f"--defaults-extra-file={defaults_file}")
    if login_path:
        command.insert(1, f"--login-path={login_path}")
    if host:
        command.extend(["--host", host])
    if port:
        command.extend(["--port", str(port)])
    if user:
        command.extend(["--user", user])
    if database not in {TARGET_DATABASE, PRODUCTION_DATABASE}:
        raise ProjectionError(f"unsupported database target: {database}")
    command.append(database)

    environment = dict(os.environ)
    password = (
        os.environ.get("ISSUE_RADAR_MYSQL_PASSWORD")
        or os.environ.get("DB_PASSWORD")
    )
    if password:
        environment["MYSQL_PWD"] = password
    return command, environment


def run_mysql(sql: str, *, write: bool = False) -> str:
    """Run SQL only against the fixed UAT database.

    Callers must opt into writes. Production database names are rejected even
    inside comments or quoted identifiers to make accidental reuse fail closed.
    """

    lowered = sql.casefold()
    if PRODUCTION_DATABASE.casefold() in lowered.replace(
        TARGET_DATABASE.casefold(), ""
    ):
        raise ProjectionError("SQL references the prohibited production database")
    if not write:
        prohibited = (
            "insert ",
            "update ",
            "delete ",
            "replace ",
            "alter ",
            "drop ",
            "truncate ",
            "create ",
            "call ",
        )
        normalized = " ".join(lowered.split())
        if any(token in normalized for token in prohibited):
            raise ProjectionError("read-only query contains a mutating SQL keyword")

    command, environment = _mysql_command()
    try:
        result = subprocess.run(
            command,
            input=sql,
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProjectionError(f"MySQL client not found: {command[0]}") from exc
    if result.returncode:
        message = result.stderr.strip() or f"MySQL exited {result.returncode}"
        raise ProjectionError(message)
    return result.stdout


def run_production_readonly(sql: str) -> str:
    """Run a non-mutating baseline check against production."""

    lowered = " ".join(sql.casefold().split())
    prohibited = (
        "insert ",
        "update ",
        "delete ",
        "replace ",
        "alter ",
        "drop ",
        "truncate ",
        "create ",
        "call ",
    )
    if any(token in lowered for token in prohibited):
        raise ProjectionError("production connection is strictly read-only")
    command, environment = _mysql_command(PRODUCTION_DATABASE)
    result = subprocess.run(
        command,
        input=sql,
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    if result.returncode:
        message = result.stderr.strip() or f"MySQL exited {result.returncode}"
        raise ProjectionError(message)
    return result.stdout


def mysql_json_rows(select_sql: str) -> list[dict[str, Any]]:
    """Return JSON rows through line-safe base64 transport."""

    wrapped = (
        "SELECT REPLACE(TO_BASE64(CONVERT(row_json USING utf8mb4)), '\\n', '') "
        f"FROM ({select_sql.rstrip().rstrip(';')}) AS projection_rows;"
    )
    rows: list[dict[str, Any]] = []
    for line in run_mysql(wrapped).splitlines():
        if not line:
            continue
        try:
            decoded = base64.b64decode(line).decode("utf-8")
            value = json.loads(decoded)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProjectionError("UAT returned an invalid encoded JSON row") from exc
        if not isinstance(value, dict):
            raise ProjectionError("UAT JSON row is not an object")
        rows.append(value)
    return rows


def json_object_sql(columns: Iterable[str]) -> str:
    pairs = []
    for column in columns:
        pairs.extend([f"'{column}'", f"`{column}`"])
    return "JSON_OBJECT(" + ",".join(pairs) + ")"


def _id_filter(article_ids: Iterable[int] | None, alias: str = "") -> str:
    if article_ids is None:
        return ""
    values = sorted({int(value) for value in article_ids})
    if not values:
        return " WHERE 1=0"
    prefix = f"{alias}." if alias else ""
    return f" WHERE {prefix}`article_id` IN ({','.join(map(str, values))})"


def canonical_multiset(rows: Iterable[Any]) -> list[Any]:
    return sorted(rows, key=canonical_json)


def fetch_uat_projections(
    article_ids: Iterable[int] | None = None,
    identity_by_id: dict[int, dict[str, Any]] | None = None,
) -> dict[int, dict[str, Any]]:
    """Fetch complete canonical parent and child records from UAT."""

    selected_ids = None if article_ids is None else sorted({int(x) for x in article_ids})
    parent_rows = mysql_json_rows(
        "SELECT "
        + json_object_sql(ARTICLE_COLUMNS)
        + " AS row_json FROM `UAT_articles`"
        + _id_filter(selected_ids)
        + " ORDER BY `article_id`"
    )
    projections: dict[int, dict[str, Any]] = {}
    for article in parent_rows:
        article_id = int(article["article_id"])
        identity = (identity_by_id or {}).get(article_id, {})
        projections[article_id] = {
            "schemaVersion": PROJECTION_SCHEMA,
            "identity": {
                "wikiSourceId": identity.get("wikiSourceId"),
                "uatArticleId": article_id,
                "origin": identity.get("origin"),
            },
            "article": article,
            "coverage": [],
            "media": [],
            "tags": [],
            "userGroups": [],
        }

    child_specs = [
        ("coverage", "UAT_article_coverage", COVERAGE_COLUMNS),
        ("media", "UAT_article_media", MEDIA_COLUMNS),
        ("tags", "UAT_article_tags", ["tag"]),
        ("userGroups", "UAT_article_user_groups", ["user_group_id"]),
    ]
    for key, table, columns in child_specs:
        rows = mysql_json_rows(
            "SELECT JSON_OBJECT("
            "'article_id',`article_id`,'value',"
            + json_object_sql(columns)
            + f") AS row_json FROM `{table}`"
            + _id_filter(selected_ids)
            + " ORDER BY `article_id`,`id`"
        )
        grouped: dict[int, list[Any]] = defaultdict(list)
        for row in rows:
            article_id = int(row["article_id"])
            value = row["value"]
            if key == "tags":
                value = value["tag"]
            elif key == "userGroups":
                value = int(value["user_group_id"])
            grouped[article_id].append(value)
        for article_id, values in grouped.items():
            if article_id not in projections:
                raise ProjectionError(
                    f"{table} contains orphan article_id {article_id}"
                )
            projections[article_id][key] = canonical_multiset(values)
    return projections


def extract_projection(body: str) -> tuple[str, dict[str, Any] | None]:
    """Split a compiled/source body from its canonical projection section."""

    from ingest_cascade import split_database_projection

    source, rendered = split_database_projection(body)
    return source, json.loads(rendered) if rendered else None


def render_projection(projection: dict[str, Any]) -> str:
    if projection.get("schemaVersion") != PROJECTION_SCHEMA:
        raise ProjectionError("unsupported database projection schema")
    return (
        "## Database Projection\n"
        "```json\n"
        f"{canonical_json(projection)}\n"
        "```\n"
    )


def write_hashed_manifest(
    output_dir: Path,
    manifest: dict[str, Any],
    files: Iterable[Path],
) -> None:
    bundle_files = {}
    for path in sorted(files):
        relative = path.relative_to(output_dir).as_posix()
        bundle_files[relative] = {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    manifest["bundleFiles"] = bundle_files
    (output_dir / "bundle_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def verify_hashed_manifest(bundle_dir: Path) -> dict[str, Any]:
    manifest_path = bundle_dir / "bundle_manifest.json"
    if not manifest_path.is_file():
        raise ProjectionError(f"missing bundle manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_files = manifest.get("bundleFiles")
    if not isinstance(expected_files, dict):
        raise ProjectionError("bundle manifest has no bundleFiles map")

    actual_files = {
        path.relative_to(bundle_dir).as_posix()
        for path in bundle_dir.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if actual_files != set(expected_files):
        missing = sorted(set(expected_files) - actual_files)
        extra = sorted(actual_files - set(expected_files))
        raise ProjectionError(f"bundle file set mismatch; missing={missing}, extra={extra}")
    for relative, expected in expected_files.items():
        path = bundle_dir / relative
        if path.stat().st_size != expected["bytes"]:
            raise ProjectionError(f"bundle size mismatch: {relative}")
        if sha256_file(path) != expected["sha256"]:
            raise ProjectionError(f"bundle hash mismatch: {relative}")
    return manifest
