"""Build and query a local Classic World SQLite snapshot."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

from cw_common import SourceError, flatten, integer_or_none, normalize_id
from cw_source import SCHEMA_VERSION, extract_snapshot, is_reparse_point, prepare_source, verify_source_unchanged

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
DATABASE_NAME = "catalog.sqlite"
EXPECTED_IDENTITY = {"edition": "classic-world", "region": "GLOBAL", "locale": "en"}
LEGACY_SCHEMA_VERSION = "1"
CURRENT_SCHEMA_VERSION = "2"
KINDS = ("mob", "map", "equipment", "item", "skill", "npc", "quest", "set", "recipe")
RELATION_TARGET_KINDS = (*KINDS, "item_ref")
MAX_LIMIT = 50
MAX_TEXT_LENGTH = 512
MAX_TEXT_TOKENS = 32
MAX_PATH_LENGTH = 1024
MAX_SOURCE_STRING = 4096
MAX_SOURCE_SUMMARIES = 3
MAX_RELATION_RAW_LEAVES = 16
MAX_GAP_GROUPS = 50
MAX_GAP_EXAMPLES = 3
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SIGNED_DECIMAL_RE = re.compile(r"-?[0-9]+\Z", re.ASCII)


class CwRagError(RuntimeError):
    pass


def emit(payload: dict[str, Any]) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    print(json.dumps(_cap_output_strings(payload), ensure_ascii=False, separators=(",", ":")))


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        emit({"status": "error", "reason": message})
        raise SystemExit(2)


def inventory(directory: Path) -> dict[str, int]:
    counts = {"wz": 0, "ms": 0}

    def raise_walk_error(error: OSError) -> None:
        raise error

    for current, directories, filenames in os.walk(directory, topdown=True, followlinks=False, onerror=raise_walk_error):
        current_path = Path(current)
        safe_directories = []
        for name in directories:
            child = current_path / name
            if not is_reparse_point(child):
                safe_directories.append(name)
        directories[:] = safe_directories
        for name in filenames:
            child = current_path / name
            if is_reparse_point(child):
                continue
            suffix = child.suffix.casefold().lstrip(".")
            if suffix in counts:
                counts[suffix] += 1
    return counts


def candidate_directory(root: Path, relative_parts: tuple[str, ...]) -> Path | None:
    current = root
    for part in relative_parts:
        current = current / part
        try:
            if is_reparse_point(current) or not current.is_dir():
                return None
        except FileNotFoundError:
            return None
    return current


def inspect_client(client: Path) -> tuple[dict[str, Any], int]:
    try:
        root = client.expanduser().resolve(strict=True)
    except FileNotFoundError:
        return {"status": "error", "reason": "client path does not exist"}, 2
    except (OSError, RuntimeError):
        return {"status": "error", "reason": "client path cannot be resolved"}, 2
    if not root.is_dir():
        return {"status": "error", "reason": "client path is not a directory"}, 2
    for parts in (("appdata", "Data"), ("Data",), ()):
        try:
            directory = candidate_directory(root, parts)
            if directory is None:
                continue
            counts = inventory(directory)
        except OSError:
            return {"status": "error", "reason": "client directory is unreadable"}, 2
        if sum(counts.values()):
            return {"status": "inspected", "data_directory": str(directory.resolve(strict=True)), "resource_counts": counts, "total_resource_files": sum(counts.values())}, 0
    return {"status": "error", "reason": "no .wz or .ms resources found"}, 2


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def validate_output_path(client_root: Path, data_root: Path, data_dir: Path) -> Path:
    output = data_dir.expanduser().resolve(strict=False)
    protected = (client_root, data_root)
    if any(_is_within(output, root) for root in protected):
        raise CwRagError("data directory must be outside the selected client installation and data tree")
    if output == PROJECT_ROOT or _is_within(output, SCRIPT_DIR):
        raise CwRagError("data directory cannot be the project root or a scripts directory")
    if output.exists() and not output.is_dir():
        raise CwRagError("data directory path is not a directory")
    return output


def _database_path(data_dir: Path) -> Path:
    return data_dir / DATABASE_NAME


def _connect_read_only(database: Path) -> sqlite3.Connection:
    if not database.exists() or not database.is_file():
        raise CwRagError(f"index database does not exist: {database}")
    try:
        connection = sqlite3.connect(database.resolve(strict=True).as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as error:
        raise CwRagError(f"cannot open index database: {error}") from error


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "metadata" not in tables:
        raise CwRagError("index has an unexpected schema; rebuild into a new data directory")
    try:
        values = {str(key): str(value) for key, value in connection.execute("SELECT key, value FROM metadata")}
    except sqlite3.Error as error:
        raise CwRagError(f"index metadata is unreadable: {error}") from error
    schema_version = values.get("schema_version")
    if schema_version not in (LEGACY_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION):
        raise CwRagError("index schema is unsupported; use a new data directory")
    for key, value in EXPECTED_IDENTITY.items():
        if values.get(key) != value:
            raise CwRagError("index source identity is not Classic World GLOBAL English")
    expected_tables = {"metadata", "entities", "placements", "entity_fts"}
    if schema_version == CURRENT_SCHEMA_VERSION:
        expected_tables.add("relations")
    if not expected_tables.issubset(tables):
        raise CwRagError("index has an unexpected schema; rebuild into a new data directory")
    required_metadata = {"entity_counts_json", "entity_count", "placement_count", "coverage_json", "source_revision", "parser_revision", "built_at"}
    if schema_version == CURRENT_SCHEMA_VERSION:
        required_metadata |= {"relation_count", "relation_counts_json", "unresolved_relation_count"}
    missing = required_metadata - values.keys()
    if missing:
        raise CwRagError("index metadata is incomplete; rebuild into a new data directory")
    return values


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _json_metadata(values: dict[str, str], key: str) -> Any:
    try:
        return json.loads(values[key])
    except (KeyError, json.JSONDecodeError, TypeError) as error:
        raise CwRagError(f"index metadata field {key} is unreadable") from error


def _validate_database(connection: sqlite3.Connection) -> dict[str, str]:
    values = _metadata(connection)
    schema_version = values["schema_version"]
    base_columns = {"kind", "normalized_id", "original_id", "name", "fields_json", "sources_json", "level", "hp", "exp", "physical_attack"}
    if not base_columns.issubset(_table_columns(connection, "entities")):
        raise CwRagError("index entity columns are incomplete; rebuild into a new data directory")
    if schema_version == CURRENT_SCHEMA_VERSION:
        if not {"family", "req_level", "req_job"}.issubset(_table_columns(connection, "entities")):
            raise CwRagError("index entity projections are incomplete; rebuild into a new data directory")
        if not {"source_kind", "source_id", "relation", "target_kind", "target_id", "node_path", "raw_json", "sources_json"}.issubset(_table_columns(connection, "relations")):
            raise CwRagError("index relation columns are incomplete; rebuild into a new data directory")
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise CwRagError("index database integrity check failed")
    expected_counts = _json_metadata(values, "entity_counts_json")
    if not isinstance(expected_counts, dict):
        raise CwRagError("index entity counts are unreadable")
    actual_counts = dict(connection.execute("SELECT kind, COUNT(*) FROM entities GROUP BY kind"))
    if any(not isinstance(kind, str) or not isinstance(count, int) for kind, count in expected_counts.items()):
        raise CwRagError("index entity counts are unreadable")
    if set(actual_counts) - set(expected_counts) or any(actual_counts.get(kind, 0) != count for kind, count in expected_counts.items()):
        raise CwRagError("index entity counts do not match snapshot metadata")
    if sum(actual_counts.values()) != int(values["entity_count"]):
        raise CwRagError("index entity total does not match snapshot metadata")
    if connection.execute("SELECT COUNT(*) FROM entity_fts").fetchone()[0] != int(values["entity_count"]):
        raise CwRagError("index full-text rows do not match the entity table")
    orphan_maps = connection.execute("""
        SELECT COUNT(*) FROM placements p
        LEFT JOIN entities e ON e.kind='map' AND e.normalized_id=p.map_id
        WHERE e.normalized_id IS NULL
    """).fetchone()[0]
    if orphan_maps:
        raise CwRagError("index placements contain unknown owning maps")
    placement_count = connection.execute("SELECT COUNT(*) FROM placements").fetchone()[0]
    if placement_count != int(values["placement_count"]):
        raise CwRagError("index placement count does not match snapshot metadata")
    if schema_version == CURRENT_SCHEMA_VERSION:
        _validate_relations(connection, values, placement_count)
    return values


def _validate_relations(connection: sqlite3.Connection, values: dict[str, str], placement_count: int) -> None:
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise CwRagError("index relation source endpoints contain unknown entities")
    relation_count = connection.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    if relation_count != int(values["relation_count"]):
        raise CwRagError("index relation total does not match snapshot metadata")
    actual_counts = dict(connection.execute("SELECT relation, COUNT(*) FROM relations GROUP BY relation"))
    expected_counts = _json_metadata(values, "relation_counts_json")
    if not isinstance(expected_counts, dict) or actual_counts != expected_counts:
        raise CwRagError("index relation counts do not match snapshot metadata")
    unresolved = connection.execute("""
        SELECT COUNT(*) FROM relations r
        LEFT JOIN entities e ON e.kind=r.target_kind AND e.normalized_id=r.target_id
        WHERE e.normalized_id IS NULL
    """).fetchone()[0]
    if unresolved != int(values["unresolved_relation_count"]):
        raise CwRagError("index unresolved relation count does not match snapshot metadata")
    projected = Counter(
        (row[0], row[1], row[2])
        for row in connection.execute("SELECT source_id, node_path, target_id FROM relations WHERE relation='map_mob'")
    )
    placements = Counter(
        (row[0], "life/" + row[1], row[2])
        for row in connection.execute("SELECT map_id, node_id, mob_id FROM placements")
    )
    if projected != placements or len(projected) != placement_count:
        raise CwRagError("index map_mob relations do not match the placements projection")


def _bounded_gap_summary(coverage: Any) -> Any:
    if not isinstance(coverage, dict):
        return coverage
    result = dict(coverage)
    gaps = result.get("gaps")
    if not isinstance(gaps, list):
        return result
    bounded = []
    for raw_gap in gaps[:MAX_GAP_GROUPS]:
        if not isinstance(raw_gap, dict):
            bounded.append(raw_gap)
            continue
        gap = dict(raw_gap)
        for key in ("examples", "ids", "addresses"):
            examples = gap.get(key)
            if isinstance(examples, list) and len(examples) > MAX_GAP_EXAMPLES:
                gap[key] = examples[:MAX_GAP_EXAMPLES]
                gap[key + "_omitted"] = len(examples) - MAX_GAP_EXAMPLES
        bounded.append(gap)
    result["gaps"] = bounded
    if len(gaps) > MAX_GAP_GROUPS:
        result["gaps_omitted"] = len(gaps) - MAX_GAP_GROUPS
    return result


def _summary(values: dict[str, str]) -> dict[str, Any]:
    schema_version = values["schema_version"]
    summary = {
        "schema_version": int(schema_version),
        "legacy_snapshot": schema_version == LEGACY_SCHEMA_VERSION,
        "rebuild_required": schema_version == LEGACY_SCHEMA_VERSION,
        "edition": values["edition"],
        "region": values["region"],
        "locale": values["locale"],
        "source_revision": values["source_revision"],
        "parser_revision": values["parser_revision"],
        "built_at": values["built_at"],
        "entity_counts": _json_metadata(values, "entity_counts_json"),
        "placement_count": int(values["placement_count"]),
        "coverage": _bounded_gap_summary(_json_metadata(values, "coverage_json")),
    }
    if schema_version == CURRENT_SCHEMA_VERSION:
        summary.update({
            "relation_count": int(values["relation_count"]),
            "relation_counts": _json_metadata(values, "relation_counts_json"),
            "unresolved_relation_count": int(values["unresolved_relation_count"]),
        })
    return _cap_output_strings(summary)


def _cap_string(value: str) -> str:
    return value[:MAX_SOURCE_STRING]


def _cap_output_strings(value: Any) -> Any:
    if isinstance(value, str):
        return _cap_string(value)
    if isinstance(value, dict):
        return {key: _cap_output_strings(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_cap_output_strings(child) for child in value]
    if isinstance(value, tuple):
        return [_cap_output_strings(child) for child in value]
    return value


def _readable_text(fields: dict[str, Any]) -> str:
    values: list[str] = []
    readable_roots = (
        "name_info", "static_info", "item_effects", "skill_static", "skill_book",
        "quest_info", "quest_check", "quest_act", "quest_say", "set_data", "recipe_data",
        "family", "section", "skill_resource_group", "recipe_group", "recipe_entry",
    )

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                values.append(str(key))
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            values.append(value)

    for key in readable_roots:
        if key in fields:
            visit(fields[key])
    return " ".join(values)


def _numeric_value(fields: dict[str, Any], name: str) -> int | None:
    info = fields.get("static_info")
    if not isinstance(info, dict):
        return None
    return integer_or_none(info.get("info/" + name))


def _relation_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _write_database(database: Path, temporary: Path, snapshot: dict[str, Any]) -> None:
    schema_version = str(snapshot.get("schema_version", ""))
    if schema_version not in (LEGACY_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION):
        raise CwRagError("staged snapshot has an unsupported schema version")
    entities = snapshot.get("entities", [])
    placements = snapshot.get("placements", [])
    relations = snapshot.get("relations", []) if schema_version == CURRENT_SCHEMA_VERSION else []
    if not isinstance(entities, list) or not isinstance(placements, list) or not isinstance(relations, list):
        raise CwRagError("staged snapshot contains invalid entity, placement, or relation lists")
    kinds = "','".join(KINDS)
    target_kinds = "','".join(RELATION_TARGET_KINDS)
    extra_columns = " family TEXT, req_level INTEGER, req_job INTEGER," if schema_version == CURRENT_SCHEMA_VERSION else ""
    projection_indexes = "CREATE INDEX entities_family ON entities(family); CREATE INDEX entities_req_level ON entities(req_level); CREATE INDEX entities_req_job ON entities(req_job);" if schema_version == CURRENT_SCHEMA_VERSION else ""
    relation_ddl = ""
    if schema_version == CURRENT_SCHEMA_VERSION:
        relation_ddl = f"""
            CREATE TABLE relations(
                source_kind TEXT NOT NULL CHECK(source_kind IN ('{kinds}')),
                source_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                target_kind TEXT NOT NULL CHECK(target_kind IN ('{target_kinds}')),
                target_id TEXT NOT NULL,
                node_path TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                sources_json TEXT NOT NULL,
                PRIMARY KEY(source_kind, source_id, relation, target_kind, target_id, node_path),
                FOREIGN KEY(source_kind, source_id) REFERENCES entities(kind, normalized_id)
            );
            CREATE INDEX relations_by_source ON relations(source_kind, source_id, relation, target_kind, target_id, node_path);
            CREATE INDEX relations_by_target ON relations(target_kind, target_id, relation, source_kind, source_id, node_path);
            CREATE INDEX relations_by_type ON relations(relation, source_kind, source_id, target_kind, target_id);
        """
    connection = sqlite3.connect(temporary)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(f"""
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE entities(
                kind TEXT NOT NULL CHECK(kind IN ('{kinds}')),
                normalized_id TEXT NOT NULL,
                original_id TEXT NOT NULL,
                name TEXT,
                fields_json TEXT NOT NULL,
                sources_json TEXT NOT NULL,
                level INTEGER,
                hp INTEGER,
                exp INTEGER,
                physical_attack INTEGER,
                {extra_columns}
                PRIMARY KEY(kind, normalized_id)
            );
            CREATE INDEX entities_kind_name ON entities(kind, name COLLATE NOCASE, normalized_id);
            CREATE INDEX entities_mob_stats ON entities(kind, level, hp);
            {projection_indexes}
            CREATE TABLE placements(
                map_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                mob_id TEXT NOT NULL,
                x INTEGER,
                y INTEGER,
                raw_json TEXT NOT NULL
            );
            CREATE INDEX placements_by_map ON placements(map_id, mob_id);
            CREATE INDEX placements_by_mob ON placements(mob_id, map_id);
            {relation_ddl}
            CREATE VIRTUAL TABLE entity_fts USING fts5(kind UNINDEXED, normalized_id UNINDEXED, name, readable_text);
        """)
        entity_counts = snapshot.get("entity_counts", {})
        if schema_version == CURRENT_SCHEMA_VERSION:
            entity_counts = {kind: int(entity_counts.get(kind, 0)) for kind in KINDS}
        relation_counts = dict(Counter(item["relation"] for item in relations))
        unresolved_relation_count = 0
        if schema_version == CURRENT_SCHEMA_VERSION:
            entity_keys = {(item["kind"], item["normalized_id"]) for item in entities}
            unresolved_relation_count = sum((item["target_kind"], item["target_id"]) not in entity_keys for item in relations)
        metadata = {
            "schema_version": schema_version,
            "edition": snapshot["edition"],
            "region": snapshot["region"],
            "locale": snapshot["locale"],
            "source_revision": snapshot["source_fingerprint"],
            "parser_revision": snapshot["parser_revision"],
            "built_at": snapshot["built_at"],
            "source_files_json": _relation_json(snapshot.get("source_files", [])),
            "entity_counts_json": _relation_json(entity_counts),
            "coverage_json": _relation_json(snapshot.get("coverage", {})),
            "entity_count": str(len(entities)),
            "placement_count": str(len(placements)),
        }
        if schema_version == CURRENT_SCHEMA_VERSION:
            metadata.update({
                "relation_count": str(len(relations)),
                "relation_counts_json": _relation_json(relation_counts),
                "unresolved_relation_count": str(unresolved_relation_count),
            })
        with connection:
            connection.executemany("INSERT INTO metadata(key,value) VALUES(?,?)", metadata.items())
            for entity in entities:
                fields = entity["fields"]
                numeric = (
                    _numeric_value(fields, "level"),
                    _numeric_value(fields, "maxHP"),
                    _numeric_value(fields, "exp"),
                    _numeric_value(fields, "PADamage"),
                )
                if schema_version == CURRENT_SCHEMA_VERSION:
                    family = fields.get("family") if isinstance(fields.get("family"), str) else None
                    req_level = _numeric_value(fields, "reqLevel") if entity["kind"] == "equipment" else None
                    req_job = _numeric_value(fields, "reqJob") if entity["kind"] == "equipment" else None
                    connection.execute("""
                        INSERT INTO entities(kind,normalized_id,original_id,name,fields_json,sources_json,level,hp,exp,physical_attack,family,req_level,req_job)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        entity["kind"], entity["normalized_id"], entity["original_id"], entity["name"],
                        _relation_json(fields), _relation_json(entity["sources"]), *numeric, family, req_level, req_job,
                    ))
                else:
                    connection.execute("""
                        INSERT INTO entities(kind,normalized_id,original_id,name,fields_json,sources_json,level,hp,exp,physical_attack)
                        VALUES(?,?,?,?,?,?,?,?,?,?)
                    """, (
                        entity["kind"], entity["normalized_id"], entity["original_id"], entity["name"],
                        _relation_json(fields), _relation_json(entity["sources"]), *numeric,
                    ))
                rowid = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                connection.execute("INSERT INTO entity_fts(rowid,kind,normalized_id,name,readable_text) VALUES(?,?,?,?,?)", (
                    rowid, entity["kind"], entity["normalized_id"], entity["name"] or "", _readable_text(fields),
                ))
            connection.executemany("INSERT INTO placements(map_id,node_id,mob_id,x,y,raw_json) VALUES(?,?,?,?,?,?)", (
                (item["map_id"], item["node_id"], item["mob_id"], item.get("x"), item.get("y"), item["raw_json"])
                for item in placements
            ))
            if schema_version == CURRENT_SCHEMA_VERSION:
                connection.executemany("""
                    INSERT INTO relations(source_kind,source_id,relation,target_kind,target_id,node_path,raw_json,sources_json)
                    VALUES(?,?,?,?,?,?,?,?)
                """, (
                    (item["source_kind"], item["source_id"], item["relation"], item["target_kind"], item["target_id"], item["node_path"], _relation_json(item["raw"]), _relation_json(item["sources"]))
                    for item in relations
                ))
        connection.execute("INSERT INTO entity_fts(entity_fts) VALUES('integrity-check')")
        connection.commit()
        values = _validate_database(connection)
        if values["source_revision"] != snapshot["source_fingerprint"]:
            raise CwRagError("staged index source revision validation failed")
        if values["parser_revision"] != snapshot["parser_revision"]:
            raise CwRagError("staged index parser revision validation failed")
    except (sqlite3.Error, KeyError, TypeError, ValueError) as error:
        if isinstance(error, CwRagError):
            raise
        raise CwRagError(f"cannot write staged index: {error}") from error
    finally:
        connection.close()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _schema1_backup(database: Path, expected_hash: str) -> Path:
    backup = database.with_name(f"catalog.schema1.{expected_hash}.sqlite")
    if backup.exists():
        if not backup.is_file() or _sha256_file(backup) != expected_hash:
            raise CwRagError(f"schema 1 backup path already exists with different contents: {backup}")
        return backup
    descriptor = None
    created = False
    try:
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with database.open("rb") as source, os.fdopen(descriptor, "wb") as destination:
            descriptor = None
            shutil.copyfileobj(source, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        if _sha256_file(backup) != expected_hash:
            raise CwRagError("schema 1 backup hash verification failed")
        return backup
    except FileExistsError:
        if backup.is_file() and _sha256_file(backup) == expected_hash:
            return backup
        raise CwRagError(f"schema 1 backup path already exists with different contents: {backup}")
    except Exception:
        if created:
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def build_index(client: Path, data_dir_arg: Path) -> dict[str, Any]:
    try:
        client_root = client.expanduser().resolve(strict=True)
    except FileNotFoundError as error:
        raise CwRagError("client path does not exist") from error
    except (OSError, RuntimeError) as error:
        raise CwRagError("client path cannot be resolved") from error
    if not client_root.is_dir():
        raise CwRagError("client path is not a directory")
    inspected, code = inspect_client(client_root)
    if code:
        raise CwRagError(inspected["reason"])
    data_root = Path(inspected["data_directory"]).resolve(strict=True)
    output_dir = validate_output_path(client_root, data_root, data_dir_arg)
    prepared = prepare_source(data_root)
    database = _database_path(output_dir)
    old_values = None
    old_hash = None
    if database.exists():
        if not database.is_file():
            raise CwRagError("catalog.sqlite exists but is not a file")
        with closing(_connect_read_only(database)) as connection:
            old_values = _validate_database(connection)
        old_hash = _sha256_file(database)
        if old_values["schema_version"] == CURRENT_SCHEMA_VERSION and old_values["source_revision"] == prepared["source_fingerprint"] and old_values["parser_revision"] == prepared["parser_revision"]:
            verify_source_unchanged(prepared)
            return {"status": "no_change", "database": str(database.resolve(strict=True)), "snapshot": _summary(old_values)}
    output_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".catalog-", suffix=".tmp", dir=output_dir)
    os.close(descriptor)
    temporary = Path(temporary_name)
    backup = None
    try:
        snapshot = extract_snapshot(data_root, prepared)
        if str(snapshot.get("schema_version")) != CURRENT_SCHEMA_VERSION:
            raise CwRagError("source extractor did not produce schema 2")
        verify_source_unchanged(prepared)
        _write_database(database, temporary, snapshot)
        if old_values and old_values["schema_version"] == LEGACY_SCHEMA_VERSION:
            if _sha256_file(database) != old_hash:
                raise CwRagError("existing schema 1 catalog changed before transition")
            backup = _schema1_backup(database, old_hash)
        verify_source_unchanged(prepared)
        os.replace(temporary, database)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        with closing(_connect_read_only(database)) as connection:
            values = _validate_database(connection)
    except Exception as error:
        suffix = f"; schema 1 backup: {backup}" if backup else ""
        raise CwRagError(f"publication/readback is uncertain: {error}{suffix}") from error
    result = {"status": "built", "database": str(database.resolve(strict=True)), "snapshot": _summary(values)}
    if backup:
        result["legacy_backup"] = str(backup)
    return result


def status_index(data_dir: Path) -> dict[str, Any]:
    try:
        resolved = data_dir.expanduser().resolve(strict=True)
    except FileNotFoundError as error:
        raise CwRagError("data directory does not exist") from error
    if not resolved.is_dir():
        raise CwRagError("data path is not a directory")
    database = _database_path(resolved)
    with closing(_connect_read_only(database)) as connection:
        values = _validate_database(connection)
    return {"status": "available", "snapshot_only": True, "schema_version": int(values["schema_version"]), "legacy_snapshot": values["schema_version"] == LEGACY_SCHEMA_VERSION, "rebuild_required": values["schema_version"] == LEGACY_SCHEMA_VERSION, "database": str(database), "snapshot": _summary(values)}


def _parse_query_text(value: str) -> tuple[str, str | None]:
    if len(value) > MAX_TEXT_LENGTH:
        raise CwRagError(f"--text is limited to {MAX_TEXT_LENGTH} characters")
    if not value.strip():
        raise CwRagError("--text must contain at least one non-space character")
    tokens = _TOKEN_RE.findall(value)
    if len(tokens) > MAX_TEXT_TOKENS:
        raise CwRagError(f"--text is limited to {MAX_TEXT_TOKENS} quoted tokens")
    match = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens) if tokens else ""
    stripped = value.strip()
    exact_id = (stripped.lstrip("0") or "0") if stripped.isascii() and stripped.isdigit() else None
    return match, exact_id


def _row_result_legacy(row: sqlite3.Row, detail: bool, connection: sqlite3.Connection) -> dict[str, Any]:
    fields = json.loads(row["fields_json"])
    result: dict[str, Any] = {"type": row["kind"], "id": row["normalized_id"], "original_id": row["original_id"], "name": row["name"]}
    stats = {key: row[column] for key, column in (("level", "level"), ("hp", "hp"), ("exp", "exp"), ("physical_attack", "physical_attack")) if row[column] is not None}
    if stats:
        result["stats"] = stats
    if row["kind"] == "map" and not detail:
        result["fields"] = {key: fields[key] for key in ("name_info", "static_info") if key in fields}
    else:
        result["fields"] = fields
    result["sources"] = json.loads(row["sources_json"])
    if row["kind"] == "map":
        if "placements" in fields:
            result["placement_count"] = connection.execute("SELECT COUNT(*) FROM placements WHERE map_id=?", (row["normalized_id"],)).fetchone()[0]
        if "portals" in fields:
            result["portal_count"] = len({path.split("/", 2)[1] for path in fields["portals"] if path.count("/") >= 2})
    return result


def _related_legacy(connection: sqlite3.Connection, kind: str, normalized_id: str, limit: int) -> dict[str, Any]:
    if kind == "mob":
        rows = connection.execute("""
            SELECT p.map_id AS id, COUNT(*) AS placement_count, e.original_id, e.name, e.sources_json
            FROM placements p JOIN entities e ON e.kind='map' AND e.normalized_id=p.map_id
            WHERE p.mob_id=? GROUP BY p.map_id
            ORDER BY lower(COALESCE(e.name,'')), p.map_id
            LIMIT ?
        """, (normalized_id, limit + 1)).fetchall()
        total = connection.execute("SELECT COUNT(DISTINCT map_id) FROM placements WHERE mob_id=?", (normalized_id,)).fetchone()[0]
        entries = [{"type": "map", "id": row["id"], "original_id": row["original_id"], "name": row["name"], "placement_count": row["placement_count"], "sources": json.loads(row["sources_json"])} for row in rows[:limit]]
        return {"maps": {"matches": entries, "total": total, "returned": len(entries), "truncated": total > len(entries)}}
    rows = connection.execute("""
        SELECT p.mob_id AS id, COUNT(*) AS placement_count, e.original_id, e.name, e.sources_json, e.level, e.hp, e.exp, e.physical_attack
        FROM placements p LEFT JOIN entities e ON e.kind='mob' AND e.normalized_id=p.mob_id
        WHERE p.map_id=? GROUP BY p.mob_id
        ORDER BY lower(COALESCE(e.name,'')), p.mob_id
        LIMIT ?
    """, (normalized_id, limit + 1)).fetchall()
    total = connection.execute("SELECT COUNT(DISTINCT mob_id) FROM placements WHERE map_id=?", (normalized_id,)).fetchone()[0]
    entries = []
    for row in rows[:limit]:
        item: dict[str, Any] = {"type": "mob", "id": row["id"], "name": row["name"], "placement_count": row["placement_count"]}
        if row["original_id"] is not None:
            item["original_id"] = row["original_id"]
        stats = {key: row[column] for key, column in (("level", "level"), ("hp", "hp"), ("exp", "exp"), ("physical_attack", "physical_attack")) if row[column] is not None}
        if stats:
            item["stats"] = stats
        if row["sources_json"]:
            item["sources"] = json.loads(row["sources_json"])
        entries.append(item)
    return {"mobs": {"matches": entries, "total": total, "returned": len(entries), "truncated": total > len(entries)}}


def _coerce_query_id(value: str, kind: str) -> str:
    try:
        return normalize_id(value.strip(), kind)
    except (SourceError, AttributeError) as error:
        raise CwRagError(f"invalid {kind} ID: {value!r}") from error


def _validate_nonnegative(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CwRagError(f"{label} must be a non-negative integer")
    return value


def _validate_query_options(*, kind: str | None, entity_id: str | None, text: str | None, min_level: int | None, max_level: int | None, max_hp: int | None, limit: int, family: str | None, min_req_level: int | None, max_req_level: int | None, req_job_raw: int | None, skill_level: int | None, detail: bool, field_prefix: str | None, field: str | None, value_offset: int | None, offset: int, related: bool, related_offset: int | None) -> tuple[str | None, str | None]:
    if kind is not None and kind not in KINDS:
        raise CwRagError(f"unsupported kind: {kind}")
    _validate_nonnegative(limit, "--limit")
    if not 1 <= limit <= MAX_LIMIT:
        raise CwRagError(f"--limit must be between 1 and {MAX_LIMIT}")
    _validate_nonnegative(offset, "--offset")
    if related_offset is not None:
        _validate_nonnegative(related_offset, "--related-offset")
    if not related and related_offset is not None:
        raise CwRagError("--related-offset requires --related")
    if entity_id is not None and kind is None:
        raise CwRagError("--kind is required with --id")
    if entity_id is not None and text is not None:
        raise CwRagError("--id and --text cannot be used together")
    if any(value is not None for value in (min_level, max_level, max_hp)) and kind != "mob":
        raise CwRagError("monster level and HP filters require --kind mob")
    if min_level is not None:
        _validate_nonnegative(min_level, "--min-level")
    if max_level is not None:
        _validate_nonnegative(max_level, "--max-level")
    if max_hp is not None:
        _validate_nonnegative(max_hp, "--max-hp")
    if min_level is not None and max_level is not None and min_level > max_level:
        raise CwRagError("--min-level cannot exceed --max-level")
    if family is not None and (kind not in ("equipment", "item") or not family):
        raise CwRagError("--family requires --kind equipment or item and a non-empty family")
    if any(value is not None for value in (min_req_level, max_req_level, req_job_raw)) and kind != "equipment":
        raise CwRagError("equipment requirement filters require --kind equipment")
    for value, label in ((min_req_level, "--min-req-level"), (max_req_level, "--max-req-level")):
        if value is not None:
            _validate_nonnegative(value, label)
    if min_req_level is not None and max_req_level is not None and min_req_level > max_req_level:
        raise CwRagError("--min-req-level cannot exceed --max-req-level")
    if req_job_raw is not None and (isinstance(req_job_raw, bool) or integer_or_none(req_job_raw) is None):
        raise CwRagError("--req-job-raw must fit a signed SQLite integer")
    if skill_level is not None:
        if isinstance(skill_level, bool) or not isinstance(skill_level, int) or skill_level <= 0:
            raise CwRagError("--skill-level must be a positive integer")
        if kind != "skill" or entity_id is None:
            raise CwRagError("--skill-level requires --kind skill and an exact --id")
    if detail and (kind is None or entity_id is None):
        raise CwRagError("--detail requires --kind and an exact --id")
    if field_prefix is not None:
        if not detail:
            raise CwRagError("--field-prefix requires --detail")
        _validate_path(field_prefix, "--field-prefix")
    if field is not None:
        if not detail:
            raise CwRagError("--field requires --detail")
        _validate_path(field, "--field")
    if field is not None and field_prefix is not None:
        raise CwRagError("--field and --field-prefix cannot be used together")
    if value_offset is not None:
        _validate_nonnegative(value_offset, "--value-offset")
        if field is None:
            raise CwRagError("--value-offset requires an exact --field")
    if detail and text is not None:
        raise CwRagError("--detail requires an exact --id and cannot be combined with --text")
    normalized_id = _coerce_query_id(entity_id, kind) if entity_id is not None and kind is not None else None
    fts_query = _parse_query_text(text)[0] if text is not None else None
    return normalized_id, fts_query


def _validate_path(value: str, label: str) -> None:
    if not value or len(value) > MAX_PATH_LENGTH:
        raise CwRagError(f"{label} must contain 1 to {MAX_PATH_LENGTH} characters")


def _normal_query_where(*, kind: str | None, normalized_id: str | None, text: str | None, fts_query: str | None, min_level: int | None, max_level: int | None, max_hp: int | None, family: str | None, min_req_level: int | None, max_req_level: int | None, req_job_raw: int | None) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if kind is not None:
        where.append("e.kind=?")
        params.append(kind)
    if normalized_id is not None:
        where.append("e.normalized_id=?")
        params.append(normalized_id)
    elif text is not None:
        text_where = ["lower(e.name)=lower(?)"]
        text_params: list[Any] = [text.strip()]
        stripped = text.strip()
        if stripped.isascii() and stripped.isdigit():
            text_where.append("e.normalized_id=?")
            text_params.append(stripped.lstrip("0") or "0")
        if fts_query:
            text_where.append("e.rowid IN (SELECT rowid FROM entity_fts WHERE entity_fts MATCH ?)")
            text_params.append(fts_query)
        where.append("(" + " OR ".join(text_where) + ")")
        params.extend(text_params)
    for clause, value in (("e.level>=?", min_level), ("e.level<=?", max_level), ("e.hp<=?", max_hp), ("e.req_level>=?", min_req_level), ("e.req_level<=?", max_req_level), ("e.req_job=?", req_job_raw)):
        if value is not None:
            where.append(clause)
            params.append(value)
    if family is not None:
        where.append("e.family=?")
        params.append(family)
    return (" WHERE " + " AND ".join(where) if where else ""), params


def _source_summaries(sources: Any) -> dict[str, Any]:
    records = sources if isinstance(sources, list) else []
    return {
        "sources": _cap_output_strings(records[:MAX_SOURCE_SUMMARIES]),
        "source_count": len(records),
        "sources_truncated": len(records) > MAX_SOURCE_SUMMARIES,
    }


def _schema2_row_result(row: sqlite3.Row, connection: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": row["kind"],
        "id": row["normalized_id"],
        "original_id": row["original_id"],
        "name": row["name"],
        "detail_available": True,
    }
    stats = {key: row[column] for key, column in (("level", "level"), ("hp", "hp"), ("exp", "exp"), ("physical_attack", "physical_attack"), ("req_level", "req_level"), ("req_job", "req_job")) if row[column] is not None}
    if stats:
        result["stats"] = stats
    if row["family"] is not None:
        result["family"] = row["family"]
    result.update(_source_summaries(json.loads(row["sources_json"])))
    if row["kind"] == "map":
        result["placement_count"] = connection.execute("SELECT COUNT(*) FROM placements WHERE map_id=?", (row["normalized_id"],)).fetchone()[0]
        fields = json.loads(row["fields_json"])
        portals = fields.get("portals")
        if isinstance(portals, dict):
            result["portal_count"] = len({path.split("/", 2)[1] for path in portals if path.count("/") >= 2})
    return result


def _all_detail_leaves(fields: dict[str, Any], sources: list[Any]) -> dict[str, Any]:
    return {**flatten(fields, "fields/"), **flatten(sources, "sources/")}


def _detail_page(leaves: dict[str, Any], *, limit: int, offset: int, field_prefix: str | None = None, field: str | None = None, value_offset: int | None = None, allowed_roots: tuple[str, ...] | None = None) -> dict[str, Any]:
    paths = sorted(path for path in leaves if (field is None or path == field) and (field_prefix is None or path.startswith(field_prefix)) and (allowed_roots is None or any(path == root or path.startswith(root + "/") for root in allowed_roots)))
    total = len(paths)
    returned_paths = paths[offset:offset + limit]
    items = []
    for path in returned_paths:
        value = leaves[path]
        item: dict[str, Any] = {"path": path}
        if isinstance(value, str):
            start = value_offset or 0 if field is not None else 0
            end = min(len(value), start + MAX_SOURCE_STRING)
            item["value"] = value[start:end]
            if start > 0 or end < len(value):
                item.update({
                    "original_length": len(value),
                    "value_offset": start,
                    "value_next_offset": end if end < len(value) else None,
                })
        else:
            if value_offset is not None and field is not None:
                raise CwRagError("--value-offset requires an exact field whose stored value is a string")
            item["value"] = value
        items.append(item)
    truncated = total > offset + len(items)
    return {
        "fields": _cap_output_strings(items),
        "field_total": total,
        "field_returned": len(items),
        "field_offset": offset,
        "field_truncated": truncated,
        "field_next_offset": offset + len(items) if truncated else None,
    }


def _skill_level_info(fields: dict[str, Any], level: int) -> tuple[bool, Any, str | None, Any, str | None]:
    skill_static = fields.get("skill_static")
    skill_static = skill_static if isinstance(skill_static, dict) else {}
    levels = skill_static.get("level")
    levels = levels if isinstance(levels, dict) else {}
    key = str(level)
    present = key in levels
    value = levels.get(key)
    name_info = fields.get("name_info")
    name_info = name_info if isinstance(name_info, dict) else {}
    description_key = None
    if present:
        hs = value.get("hs") if isinstance(value, dict) else None
        hs_key = str(hs) if isinstance(hs, (str, int)) and not isinstance(hs, bool) else None
        if hs_key is not None and hs_key in name_info:
            description_key = hs_key
        else:
            fallback = "h" + key
            if fallback in name_info:
                description_key = fallback
    description = name_info.get(description_key) if description_key is not None else None
    common_prefix = "fields/skill_static/common" if "common" in skill_static else None
    return present, value, description_key, description, common_prefix


def _skill_level_page(fields: dict[str, Any], sources: list[Any], level: int, *, limit: int, offset: int, field_prefix: str | None) -> dict[str, Any]:
    present, value, description_key, description, common_prefix = _skill_level_info(fields, level)
    leaves: dict[str, Any] = {}
    base = f"fields/skill_static/level/{level}"
    if present:
        leaves.update(flatten(value, base + "/"))
        if description_key is not None:
            leaves.update(flatten(description, f"fields/name_info/{description_key}/"))
    roots = (base, f"fields/name_info/{description_key}") if description_key is not None else (base,)
    page = _detail_page(leaves, limit=limit, offset=offset, field_prefix=field_prefix, allowed_roots=roots)
    return {
        "level": level,
        "explicit_level_present": present,
        "description_key": description_key,
        "common_available_prefix": common_prefix,
        **page,
    }


def _related_raw_summary(raw: Any) -> dict[str, Any]:
    tree = raw if isinstance(raw, (dict, list, tuple)) else {"value": raw}
    leaves = flatten(tree)
    paths = sorted(leaves)
    selected = paths[:MAX_RELATION_RAW_LEAVES]
    return {
        "fields": _cap_output_strings([{"path": path, "value": leaves[path]} for path in selected]),
        "field_total": len(paths),
        "field_truncated": len(paths) > len(selected),
    }


def _endpoint(kind: str, normalized_id: str, original_id: str | None, name: str | None) -> dict[str, Any]:
    return {"kind": kind, "id": normalized_id, "original_id": original_id, "name": name}


def _relation_page(connection: sqlite3.Connection, kind: str, normalized_id: str, direction: str, limit: int, offset: int) -> dict[str, Any]:
    if direction == "incoming":
        condition = "r.target_kind=? AND r.target_id=?"
        order = "r.relation, r.source_kind, r.source_id, r.node_path"
    else:
        condition = "r.source_kind=? AND r.source_id=?"
        order = "r.relation, r.target_kind, r.target_id, r.node_path"
    parameters = (kind, normalized_id)
    total = connection.execute(f"SELECT COUNT(*) FROM relations r WHERE {condition}", parameters).fetchone()[0]
    rows = connection.execute(f"""
        SELECT r.*, s.original_id AS source_original_id, s.name AS source_name,
               t.original_id AS target_original_id, t.name AS target_name,
               t.normalized_id IS NOT NULL AS resolved
        FROM relations r
        JOIN entities s ON s.kind=r.source_kind AND s.normalized_id=r.source_id
        LEFT JOIN entities t ON t.kind=r.target_kind AND t.normalized_id=r.target_id
        WHERE {condition}
        ORDER BY {order}
        LIMIT ? OFFSET ?
    """, (*parameters, limit, offset)).fetchall()
    matches = []
    for row in rows:
        evidence_values = json.loads(row["sources_json"])
        evidence_summary = evidence_values[:1] if isinstance(evidence_values, list) else []
        matches.append({
            "direction": direction,
            "relation": row["relation"],
            "source": _endpoint(row["source_kind"], row["source_id"], row["source_original_id"], row["source_name"]),
            "target": _endpoint(row["target_kind"], row["target_id"], row["target_original_id"], row["target_name"]),
            "resolved": bool(row["resolved"]),
            "node_path": row["node_path"],
            "raw": _related_raw_summary(json.loads(row["raw_json"])),
            "sources": _cap_output_strings(evidence_summary),
            "source_count": len(evidence_values) if isinstance(evidence_values, list) else 0,
            "sources_truncated": isinstance(evidence_values, list) and len(evidence_values) > 1,
        })
    truncated = total > offset + len(matches)
    return {
        "matches": _cap_output_strings(matches),
        "total": total,
        "returned": len(matches),
        "offset": offset,
        "next_offset": offset + len(matches) if truncated else None,
        "truncated": truncated,
    }


def _placement_related_page(connection: sqlite3.Connection, kind: str, normalized_id: str, limit: int, offset: int) -> tuple[str, dict[str, Any]] | None:
    if kind == "mob":
        total = connection.execute("SELECT COUNT(DISTINCT map_id) FROM placements WHERE mob_id=?", (normalized_id,)).fetchone()[0]
        rows = connection.execute("""
            SELECT p.map_id AS id, COUNT(*) AS placement_count, e.original_id, e.name, e.sources_json
            FROM placements p JOIN entities e ON e.kind='map' AND e.normalized_id=p.map_id
            WHERE p.mob_id=? GROUP BY p.map_id
            ORDER BY lower(COALESCE(e.name,'')), p.map_id LIMIT ? OFFSET ?
        """, (normalized_id, limit, offset)).fetchall()
        key = "maps"
        matches = []
        for row in rows:
            sources = json.loads(row["sources_json"])
            matches.append({"type": "map", "id": row["id"], "original_id": row["original_id"], "name": row["name"], "placement_count": row["placement_count"], **_source_summaries(sources)})
    elif kind == "map":
        total = connection.execute("SELECT COUNT(DISTINCT mob_id) FROM placements WHERE map_id=?", (normalized_id,)).fetchone()[0]
        rows = connection.execute("""
            SELECT p.mob_id AS id, COUNT(*) AS placement_count, e.original_id, e.name, e.sources_json, e.level, e.hp, e.exp, e.physical_attack
            FROM placements p LEFT JOIN entities e ON e.kind='mob' AND e.normalized_id=p.mob_id
            WHERE p.map_id=? GROUP BY p.mob_id
            ORDER BY lower(COALESCE(e.name,'')), p.mob_id LIMIT ? OFFSET ?
        """, (normalized_id, limit, offset)).fetchall()
        key = "mobs"
        matches = []
        for row in rows:
            item: dict[str, Any] = {"type": "mob", "id": row["id"], "name": row["name"], "placement_count": row["placement_count"]}
            if row["original_id"] is not None:
                item["original_id"] = row["original_id"]
            stats = {name: row[column] for name, column in (("level", "level"), ("hp", "hp"), ("exp", "exp"), ("physical_attack", "physical_attack")) if row[column] is not None}
            if stats:
                item["stats"] = stats
            if row["sources_json"]:
                item.update(_source_summaries(json.loads(row["sources_json"])))
            matches.append(item)
    else:
        return None
    truncated = total > offset + len(matches)
    return key, {
        "matches": _cap_output_strings(matches),
        "total": total,
        "returned": len(matches),
        "offset": offset,
        "next_offset": offset + len(matches) if truncated else None,
        "truncated": truncated,
    }


def _related_schema2(connection: sqlite3.Connection, kind: str, normalized_id: str, limit: int, offset: int) -> dict[str, Any]:
    result = {
        "incoming": _relation_page(connection, kind, normalized_id, "incoming", limit, offset),
        "outgoing": _relation_page(connection, kind, normalized_id, "outgoing", limit, offset),
    }
    placement_page = _placement_related_page(connection, kind, normalized_id, limit, offset)
    if placement_page:
        key, page = placement_page
        result[key] = page
    return result


def _legacy_query(connection: sqlite3.Connection, values: dict[str, str], *, kind: str | None, entity_id: str | None, text: str | None, fts_query: str | None, normalized_id: str | None, min_level: int | None, max_level: int | None, max_hp: int | None, limit: int, related: bool) -> dict[str, Any]:
    where, params = _normal_query_where(kind=kind, normalized_id=normalized_id, text=text, fts_query=fts_query, min_level=min_level, max_level=max_level, max_hp=max_hp, family=None, min_req_level=None, max_req_level=None, req_job_raw=None)
    total = connection.execute("SELECT COUNT(*) FROM entities e" + where, params).fetchone()[0]
    select_params = list(params)
    rank_sql = "0.0 AS rank"
    joins = ""
    if text is not None and fts_query:
        joins = " LEFT JOIN entity_fts ON entity_fts.rowid=e.rowid"
        rank_sql = "COALESCE(bm25(entity_fts), 1000000.0) AS rank"
    if entity_id is not None:
        order_sql = "e.normalized_id"
    elif any(value is not None for value in (min_level, max_level, max_hp)):
        order_sql = "e.level IS NULL,e.level,e.name COLLATE NOCASE,e.normalized_id"
    elif text is not None:
        stripped = text.strip()
        text_id = (stripped.lstrip("0") or "0") if stripped.isascii() and stripped.isdigit() else ""
        select_params.extend([stripped, text_id])
        order_sql = "CASE WHEN lower(e.name)=lower(?) THEN 0 WHEN e.normalized_id=? THEN 0 ELSE 1 END,rank,e.name COLLATE NOCASE,e.normalized_id"
    else:
        order_sql = "e.kind,e.name COLLATE NOCASE,e.normalized_id"
    select_params.append(limit)
    rows = connection.execute(f"""
        SELECT e.*, {rank_sql} FROM entities e{joins}{where}
        ORDER BY {order_sql} LIMIT ?
    """, select_params).fetchall()
    matches = []
    for row in rows:
        result = _row_result_legacy(row, entity_id is not None and kind == "map", connection)
        if related:
            result["related"] = _related_legacy(connection, row["kind"], row["normalized_id"], limit)
        matches.append(result)
    return {
        "status": "ok",
        "snapshot_only": True,
        "snapshot": _summary(values),
        "filters": {"kind": kind, "id": normalized_id, "text": text, "min_level": min_level, "max_level": max_level, "max_hp": max_hp, "limit": limit, "related": related},
        "total": total,
        "returned": len(matches),
        "truncated": total > len(matches),
        "matches": matches,
    }


def query_index(data_dir: Path, *, kind: str | None, entity_id: str | None, text: str | None, min_level: int | None, max_level: int | None, max_hp: int | None, limit: int, related: bool, family: str | None = None, min_req_level: int | None = None, max_req_level: int | None = None, req_job_raw: int | None = None, skill_level: int | None = None, detail: bool = False, field_prefix: str | None = None, field: str | None = None, value_offset: int | None = None, offset: int = 0, related_offset: int | None = None) -> dict[str, Any]:
    normalized_id, fts_query = _validate_query_options(
        kind=kind, entity_id=entity_id, text=text, min_level=min_level, max_level=max_level, max_hp=max_hp,
        limit=limit, family=family, min_req_level=min_req_level, max_req_level=max_req_level,
                req_job_raw=req_job_raw, skill_level=skill_level, detail=detail, field_prefix=field_prefix,
                field=field, value_offset=value_offset, offset=offset, related=related, related_offset=related_offset,
    )
    try:
        resolved = data_dir.expanduser().resolve(strict=True)
    except FileNotFoundError as error:
        raise CwRagError("data directory does not exist") from error
    if not resolved.is_dir():
        raise CwRagError("data path is not a directory")
    with closing(_connect_read_only(_database_path(resolved))) as connection:
        values = _validate_database(connection)
        if values["schema_version"] == LEGACY_SCHEMA_VERSION:
            new_options = (
                kind not in (None, "mob", "map") or family is not None or min_req_level is not None or max_req_level is not None or
                req_job_raw is not None or skill_level is not None or detail or field_prefix is not None or field is not None or
                value_offset is not None or offset != 0 or related_offset is not None
            )
            if new_options:
                raise CwRagError("this query option requires a schema 2 rebuild")
            return _legacy_query(connection, values, kind=kind, entity_id=entity_id, text=text, fts_query=fts_query, normalized_id=normalized_id, min_level=min_level, max_level=max_level, max_hp=max_hp, limit=limit, related=related)
        where, params = _normal_query_where(
            kind=kind, normalized_id=normalized_id, text=text, fts_query=fts_query,
            min_level=min_level, max_level=max_level, max_hp=max_hp, family=family,
            min_req_level=min_req_level, max_req_level=max_req_level, req_job_raw=req_job_raw,
        )
        total = connection.execute("SELECT COUNT(*) FROM entities e" + where, params).fetchone()[0]
        select_params = list(params)
        joins = ""
        rank_sql = "0.0 AS rank"
        if text is not None and fts_query:
            joins = " LEFT JOIN entity_fts ON entity_fts.rowid=e.rowid"
            rank_sql = "COALESCE(bm25(entity_fts), 1000000.0) AS rank"
        if normalized_id is not None:
            order_sql = "e.normalized_id"
        elif any(value is not None for value in (min_level, max_level, max_hp, min_req_level, max_req_level)):
            order_sql = "e.level IS NULL,e.level,e.req_level IS NULL,e.req_level,e.name COLLATE NOCASE,e.normalized_id"
        elif text is not None:
            stripped = text.strip()
            text_id = (stripped.lstrip("0") or "0") if stripped.isascii() and stripped.isdigit() else ""
            select_params.extend([stripped, text_id])
            order_sql = "CASE WHEN lower(e.name)=lower(?) THEN 0 WHEN e.normalized_id=? THEN 0 ELSE 1 END,rank,e.name COLLATE NOCASE,e.normalized_id"
        else:
            order_sql = "e.kind,e.name COLLATE NOCASE,e.normalized_id"
        leaf_mode = detail or skill_level is not None
        match_offset = 0 if leaf_mode else offset
        select_params.extend([1 if leaf_mode else limit, match_offset])
        rows = connection.execute(f"""
            SELECT e.*, {rank_sql} FROM entities e{joins}{where}
            ORDER BY {order_sql} LIMIT ? OFFSET ?
        """, select_params).fetchall()
        matches = []
        for row in rows:
            result = _schema2_row_result(row, connection)
            fields = json.loads(row["fields_json"])
            sources = json.loads(row["sources_json"])
            leaves = _all_detail_leaves(fields, sources)
            if detail:
                allowed_roots = None
                if skill_level is not None:
                    present, _, description_key, _, _ = _skill_level_info(fields, skill_level)
                    level_root = f"fields/skill_static/level/{skill_level}"
                    allowed_roots = (level_root, f"fields/name_info/{description_key}") if present and description_key is not None else ((level_root,) if present else tuple())
                if value_offset is not None and field is not None and (field not in leaves or not isinstance(leaves[field], str)):
                    raise CwRagError("--value-offset requires an exact field whose stored value is a string")
                result["detail"] = _detail_page(leaves, limit=limit, offset=offset, field_prefix=field_prefix, field=field, value_offset=value_offset, allowed_roots=allowed_roots)
            if skill_level is not None:
                result["skill_level"] = _skill_level_page(fields, sources, skill_level, limit=limit, offset=offset, field_prefix=field_prefix if detail else None)
            if related:
                result["related"] = _related_schema2(connection, row["kind"], row["normalized_id"], limit, related_offset or 0)
            matches.append(result)
    return {
        "status": "ok",
        "snapshot_only": True,
        "snapshot": _summary(values),
        "filters": {
            "kind": kind, "id": normalized_id, "text": text, "min_level": min_level, "max_level": max_level,
            "max_hp": max_hp, "family": family, "min_req_level": min_req_level, "max_req_level": max_req_level,
            "req_job_raw": req_job_raw, "skill_level": skill_level, "detail": detail,
            "field_prefix": field_prefix, "field": field, "value_offset": value_offset or 0,
            "limit": limit, "offset": offset, "related": related, "related_offset": related_offset or 0,
        },
        "total": total,
        "returned": len(matches),
        "truncated": total > offset + len(matches) if not leaf_mode else total > len(matches),
        "matches": _cap_output_strings(matches),
    }


def nonnegative_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from error
    if number < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return number


def signed_integer(value: str) -> int:
    if not _SIGNED_DECIMAL_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("must be a signed integer")
    number = int(value)
    if integer_or_none(number) is None:
        raise argparse.ArgumentTypeError("must fit a signed SQLite integer")
    return number


def positive_integer(value: str) -> int:
    number = nonnegative_integer(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def positive_limit(value: str) -> int:
    number = nonnegative_integer(value)
    if not 1 <= number <= MAX_LIMIT:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_LIMIT}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="cw_rag", description="Build and query a local Classic World snapshot.")
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)
    inspect_parser = subparsers.add_parser("inspect", help="count local .wz and .ms resource files", description="Read-only inventory of local MapleStory Classic World resource files.")
    inspect_parser.add_argument("--client", required=True, type=Path, metavar="PATH", help="client installation root or data directory")
    build_parser_arg = subparsers.add_parser("build", help="extract and index supported Classic World data")
    build_parser_arg.add_argument("--client", required=True, type=Path, metavar="PATH", help="client installation root or data directory")
    build_parser_arg.add_argument("--data-dir", required=True, type=Path, metavar="PATH", help="local output directory for catalog.sqlite")
    status_parser = subparsers.add_parser("status", help="show the saved local snapshot")
    status_parser.add_argument("--data-dir", required=True, type=Path, metavar="PATH")
    query_parser = subparsers.add_parser("query", help="search or browse saved Classic World records")
    query_parser.add_argument("--data-dir", required=True, type=Path, metavar="PATH")
    query_parser.add_argument("--kind", choices=KINDS, help="restrict results to one entity kind")
    query_parser.add_argument("--id", metavar="ID", help="exact ID lookup; requires --kind")
    query_parser.add_argument("--text", help="English source text or ID to search")
    query_parser.add_argument("--min-level", type=nonnegative_integer, metavar="N")
    query_parser.add_argument("--max-level", type=nonnegative_integer, metavar="N")
    query_parser.add_argument("--max-hp", type=nonnegative_integer, metavar="N")
    query_parser.add_argument("--family", help="exact equipment or item source family")
    query_parser.add_argument("--min-req-level", type=nonnegative_integer, metavar="N")
    query_parser.add_argument("--max-req-level", type=nonnegative_integer, metavar="N")
    query_parser.add_argument("--req-job-raw", type=signed_integer, metavar="INTEGER")
    query_parser.add_argument("--skill-level", type=positive_integer, metavar="N")
    query_parser.add_argument("--detail", action="store_true", help="return paginated scalar fields for an exact record")
    query_parser.add_argument("--field-prefix", metavar="PATH", help="literal detail path prefix")
    query_parser.add_argument("--field", metavar="PATH", help="one exact detail path")
    query_parser.add_argument("--value-offset", type=nonnegative_integer, default=None, metavar="N")
    query_parser.add_argument("--limit", type=positive_limit, default=10, metavar="1-50")
    query_parser.add_argument("--offset", type=nonnegative_integer, default=0, metavar="N")
    query_parser.add_argument("--related", action="store_true", help="include bounded incoming and outgoing static relations")
    query_parser.add_argument("--related-offset", type=nonnegative_integer, default=None, metavar="N")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "inspect":
            payload, code = inspect_client(args.client)
            emit(payload)
            return code
        if args.command == "build":
            emit(build_index(args.client, args.data_dir))
            return 0
        if args.command == "status":
            emit(status_index(args.data_dir))
            return 0
        if args.command == "query":
            emit(query_index(
                args.data_dir, kind=args.kind, entity_id=args.id, text=args.text,
                min_level=args.min_level, max_level=args.max_level, max_hp=args.max_hp,
                limit=args.limit, related=args.related, family=args.family,
                min_req_level=args.min_req_level, max_req_level=args.max_req_level,
                req_job_raw=args.req_job_raw, skill_level=args.skill_level, detail=args.detail,
                field_prefix=args.field_prefix, field=args.field, value_offset=args.value_offset,
                offset=args.offset, related_offset=args.related_offset,
            ))
            return 0
        raise CwRagError("unknown command")
    except SystemExit:
        raise
    except Exception as error:
        emit({"status": "error", "reason": str(error) or type(error).__name__, "error_type": type(error).__name__})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
