from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from cw_common import SourceError, evidence, iter_images, normalize_id


SOURCE_IDENTITY = {"edition": "classic-world", "region": "GLOBAL", "locale": "en"}
SCHEMA_VERSION = "2"
KINDS = ("mob", "map", "equipment", "item", "skill", "npc", "quest", "set", "recipe")
STRING_ROOTS = ("Eqp.img", "Cash.img", "Consume.img", "Etc.img", "Ins.img", "Pet.img", "Mob.img", "Npc.img", "Map.img", "Skill.img")
REQUIRED_STRING_ROOTS = frozenset(("Eqp.img", "Mob.img", "Map.img"))
EQUIPMENT_FAMILIES = frozenset(("Cap", "Coat", "Longcoat", "Pants", "Shoes", "Glove", "Cape", "Weapon", "Shield", "Accessory", "Ring", "PetEquip", "TamingMob", "Dragon"))
APPEARANCE_FAMILIES = frozenset(("Face", "Hair", "Morph"))
VENDOR_PARSER_FILES = ("__init__.py", "crypto.py", "reader.py", "properties.py", "wz_file.py", "wz_image.py", "chacha20.py", "ms_file_v2.py")
RELATION_TYPES = (
    "map_mob", "map_npc", "portal_target", "skill_req",
    "quest_check_npc", "quest_act_npc", "quest_check_item", "quest_act_item",
    "quest_check_mob", "quest_act_mob", "quest_check_quest", "quest_act_quest", "quest_next",
)
def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def is_reparse_point(path: Path) -> bool:
    file_stat = path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return path.is_symlink() or bool(reparse_flag and attributes & reparse_flag)


def _required_directory(root: Path, parts: tuple[str, ...]) -> Path:
    current = root
    for part in parts:
        current = current / part
        try:
            if is_reparse_point(current) or not current.is_dir():
                raise SourceError(f"Required source directory is missing or linked: {PurePosixPath(*parts).as_posix()}")
        except OSError as error:
            raise SourceError(f"Cannot inspect source directory {current}: {error}") from error
    return current


def _optional_directory(root: Path, parts: tuple[str, ...]) -> Path | None:
    current = root
    try:
        for part in parts:
            current = current / part
            if not current.exists() and not current.is_symlink():
                return None
            if is_reparse_point(current) or not current.is_dir():
                return None
    except OSError as error:
        raise SourceError(f"Cannot inspect optional source directory {current}: {error}") from error
    return current


def _sort_path(root: Path, path: Path) -> tuple[str, str]:
    relative = path.relative_to(root).as_posix()
    return relative.casefold(), relative


def _walk_files(base: Path, root: Path, matches) -> list[Path]:
    found: list[Path] = []

    def fail(error: OSError) -> None:
        raise error

    for current, directories, filenames in os.walk(base, topdown=True, followlinks=False, onerror=fail):
        current_path = Path(current)
        safe_directories = []
        for name in directories:
            child = current_path / name
            try:
                if name.casefold() != "_canvas" and not is_reparse_point(child):
                    safe_directories.append(name)
            except OSError as error:
                raise SourceError(f"Cannot inspect source path {child}: {error}") from error
        directories[:] = safe_directories
        for name in filenames:
            child = current_path / name
            try:
                if is_reparse_point(child) or not child.is_file():
                    continue
            except OSError as error:
                raise SourceError(f"Cannot inspect source file {child}: {error}") from error
            if matches(child, root):
                found.append(child)
    return sorted(found, key=lambda path: _sort_path(root, path))


def _direct_files(base: Path, root: Path, matches) -> list[Path]:
    found = []
    try:
        for child in base.iterdir():
            try:
                if is_reparse_point(child) or not child.is_file():
                    continue
            except OSError as error:
                raise SourceError(f"Cannot inspect source file {child}: {error}") from error
            if matches(child):
                found.append(child)
    except OSError as error:
        raise SourceError(f"Cannot read source directory {base}: {error}") from error
    return sorted(found, key=lambda path: _sort_path(root, path))


def _archive_match(path: Path) -> bool:
    return path.suffix.casefold() == ".wz" and "_" in path.name


def _select_tree(root: Path, parts: tuple[str, ...]) -> list[Path]:
    base = _optional_directory(root, parts)
    if base is None:
        return []
    return _walk_files(base, root, lambda path, _: _archive_match(path))


def select_source_files(data_root: Path) -> list[Path]:
    root = Path(data_root)
    string_dir = _required_directory(root, ("String",))
    packs_dir = _required_directory(root, ("Packs",))
    map_dir = _required_directory(root, ("Map", "Map"))

    strings = _direct_files(string_dir, root, lambda path: path.name.casefold().startswith("string_") and path.suffix.casefold() == ".wz")
    mob_packs = _direct_files(packs_dir, root, lambda path: path.name.casefold().startswith("mob_") and path.suffix.casefold() == ".ms")
    skill_packs = _direct_files(packs_dir, root, lambda path: path.name.casefold().startswith("skill_") and path.suffix.casefold() == ".ms")
    maps = _walk_files(map_dir, root, lambda path, _: _archive_match(path))

    for label, matches in (("String/String_*.wz", strings), ("Packs/Mob_*.ms", mob_packs), ("Map/Map/**/*_*.wz", maps)):
        if not matches:
            raise SourceError(f"Required source set is empty: {label}")

    character = _select_tree(root, ("Character",))
    items = _select_tree(root, ("Item",))
    npcs = _select_tree(root, ("Npc",))
    quest_dir = _optional_directory(root, ("Quest", "QuestData"))
    quests = _direct_files(quest_dir, root, lambda path: path.name.casefold().startswith("questdata_") and _archive_match(path)) if quest_dir else []
    etc_dir = _optional_directory(root, ("Etc",))
    etc = _direct_files(etc_dir, root, _archive_match) if etc_dir else []

    return sorted(
        [*strings, *mob_packs, *skill_packs, *maps, *character, *items, *npcs, *quests, *etc],
        key=lambda path: _sort_path(root, path),
    )


def _parser_paths(source_root: Path) -> list[Path]:
    parser_root = source_root / "vendor" / "wzpy"
    required = [source_root / "cw_source.py", source_root / "cw_common.py", source_root / "cw_rag.py"]
    required.extend(sorted(source_root.glob("cw_extract_*.py"), key=lambda path: path.name.casefold()))
    required.extend(parser_root / name for name in VENDOR_PARSER_FILES)
    missing = [path for path in required if not path.is_file()]
    if missing:
        raise SourceError("Parser source file is missing: " + ", ".join(path.name for path in missing))
    return sorted(required, key=lambda path: path.relative_to(source_root).as_posix())


def parser_revision(source_root: Path | None = None) -> str:
    source_root = Path(source_root or Path(__file__).parent).resolve()
    digest = hashlib.sha256()
    for path in _parser_paths(source_root):
        relative = path.relative_to(source_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def prepare_source(data_root: Path) -> dict[str, Any]:
    try:
        root = Path(data_root).resolve(strict=True)
    except OSError as error:
        raise SourceError(f"Cannot resolve source data directory {data_root}: {error}") from error
    if not root.is_dir():
        raise SourceError(f"Source data path is not a directory: {root}")
    files = select_source_files(root)
    records = [
        {"relative_path": path.relative_to(root).as_posix(), "sha256": sha256_file(path)}
        for path in files
    ]
    revision = parser_revision()
    fingerprint = sha256_bytes(encoded({"schema_version": SCHEMA_VERSION, "parser_revision": revision, "source_files": records, **SOURCE_IDENTITY}))
    return {"data_root": root, "files": files, "records": records, "parser_revision": revision, "source_fingerprint": fingerprint}


def verify_source_unchanged(prepared: dict[str, Any]) -> None:
    root: Path = prepared["data_root"]
    current_files = select_source_files(root)
    expected_names = [record["relative_path"] for record in prepared["records"]]
    actual_names = [path.relative_to(root).as_posix() for path in current_files]
    if actual_names != expected_names:
        raise SourceError("Selected source file set changed during extraction")
    actual_hashes = [sha256_file(path) for path in current_files]
    expected_hashes = [record["sha256"] for record in prepared["records"]]
    if actual_hashes != expected_hashes:
        raise SourceError("Selected source files changed during extraction")


def _name_leaves(tree: dict[str, Any], prefix: str = ""):
    for key in sorted(tree, key=lambda value: (str(value).casefold(), str(value))):
        value = tree[key]
        if not isinstance(value, dict):
            continue
        if isinstance(key, str) and key.isascii() and key.isdigit() and ("name" in value or "mapName" in value):
            yield key, value, prefix + key
        else:
            yield from _name_leaves(value, prefix + str(key) + "/")


def _source_address(source: dict[str, Any]) -> str:
    return "/".join(part for part in (source.get("resource"), source.get("image"), source.get("node")) if part)


def _source_key(source: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(source.get(key, "")) for key in ("role", "resource", "image", "node", "source_id", "file_sha256", "image_sha256"))


def _merged_sources(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {}
    for group in groups:
        for source in group:
            unique[_source_key(source)] = source
    return [unique[key] for key in sorted(unique)]


def _conflict(kind: str, normalized_id: str, existing: dict[str, Any], incoming: dict[str, Any], what: str) -> SourceError:
    left = _source_address(existing.get("sources", [{}])[0]) if existing.get("sources") else "unknown source"
    right = _source_address(incoming.get("sources", [{}])[0]) if incoming.get("sources") else "unknown source"
    return SourceError(f"Conflicting {what} for {kind}/{normalized_id}: {left} and {right}")


def _add_name(names: dict[tuple[str, str], dict[str, Any]], kind: str, raw_id: str, value: dict[str, Any], source: dict[str, Any], *, family: str | None = None, family_unresolved: bool = False, section: str | None = None) -> None:
    normalized_id = normalize_id(raw_id, kind)
    key = (kind, normalized_id)
    name_key = "mapName" if kind == "map" else "name"
    raw_name = value.get(name_key)
    name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
    name_info = {key: child for key, child in value.items() if key != name_key}
    incoming = {
        "original_id": raw_id,
        "name": name,
        "name_info": name_info,
        "sources": [source],
        "family": family,
        "family_unresolved": family_unresolved,
        "section": section,
    }
    previous = names.get(key)
    if previous is None:
        names[key] = incoming
        return
    if previous["name"] is not None and name is not None and previous["name"] != name:
        raise _conflict(kind, normalized_id, previous, incoming, "name")
    if previous["name_info"] != name_info:
        raise _conflict(kind, normalized_id, previous, incoming, "name properties")
    if previous.get("family") is not None and family is not None and previous["family"] != family:
        raise _conflict(kind, normalized_id, previous, incoming, "name family")
    if previous.get("section") is not None and section is not None and previous["section"] != section:
        raise _conflict(kind, normalized_id, previous, incoming, "name section")
    if previous["name"] is None:
        previous["name"] = name
    if previous.get("family") is None:
        previous["family"] = family
    if previous.get("section") is None:
        previous["section"] = section
    previous["family_unresolved"] = bool(previous.get("family_unresolved")) and family_unresolved
    previous["sources"] = _merged_sources(previous["sources"], [source])


def _string_name_kind(root_name: str, node_path: str) -> tuple[str, str | None, bool]:
    if root_name != "Eqp.img":
        return ("item" if root_name in ("Cash.img", "Consume.img", "Etc.img", "Ins.img", "Pet.img") else
                "mob" if root_name == "Mob.img" else
                "npc" if root_name == "Npc.img" else
                "map" if root_name == "Map.img" else
                "skill" if root_name == "Skill.img" else "item", None, False)
    parts = node_path.split("/")
    try:
        marker_index = parts.index("ClassicWorld")
    except ValueError:
        return "equipment", None, True
    family = parts[marker_index + 1] if marker_index + 1 < len(parts) else None
    if family in EQUIPMENT_FAMILIES:
        return "equipment", family, False
    if family in APPEARANCE_FAMILIES:
        return "item", family, False
    return "equipment", family, True


def _string_section(root_name: str, kind: str, family: str | None) -> str | None:
    if root_name == "Eqp.img":
        return "appearance" if family in APPEARANCE_FAMILIES else "equipment"
    if root_name == "Pet.img":
        return "pets"
    return "items" if kind == "item" else None


def _parse_name_archives(data_root: Path, files: list[Path], file_hashes: dict[str, str]) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    string_files = [path for path in files if path.parent.name.casefold() == "string" and path.name.casefold().startswith("string_")]
    context = {"data_root": data_root, "files": files, "file_hashes": file_hashes, "names": {}}
    names: dict[tuple[str, str], dict[str, Any]] = {}
    root_stats = {root: {"parsed_images": 0, "name_records": 0, "examples": []} for root in STRING_ROOTS}
    found_roots: set[str] = set()
    marker_found = False

    def accept(_: str, internal: str) -> bool:
        return PurePosixPath(internal).name.casefold() in {root.casefold() for root in STRING_ROOTS}

    for image in iter_images(context, string_files, accept):
        raw_root = PurePosixPath(image["image"]).name
        root_name = next((root for root in STRING_ROOTS if root.casefold() == raw_root.casefold()), None)
        if root_name is None:
            continue
        found_roots.add(root_name)
        root_stats[root_name]["parsed_images"] += 1
        if len(root_stats[root_name]["examples"]) < 3:
            root_stats[root_name]["examples"].append({
                "resource": image["resource"],
                "image": image["image"],
                "file_sha256": image["file_sha256"],
                "image_sha256": image["image_sha256"],
            })
        tree = image["tree"]
        search_tree = tree
        prefix = ""
        if root_name == "Eqp.img":
            marker_found = marker_found or "ClassicWorld" in tree
            world_tree = tree.get("ClassicWorld")
            if not isinstance(world_tree, dict):
                continue
            search_tree = world_tree
            prefix = "ClassicWorld/"

        for raw_id, value, node_path in _name_leaves(search_tree, prefix):
            kind, family, family_unresolved = _string_name_kind(root_name, node_path)
            extra = dict(value)
            if kind == "skill" and len(raw_id) > 4:
                group_raw = raw_id[:-4]
                group_tree = tree.get(group_raw)
                book_name = group_tree.get("bookName") if isinstance(group_tree, dict) else None
                if isinstance(book_name, str) and book_name.strip():
                    extra["bookName"] = book_name
            source = evidence(image, role="name", node=node_path, source_id=raw_id)
            _add_name(names, kind, raw_id, extra, source, family=family, family_unresolved=family_unresolved,
                      section=_string_section(root_name, kind, family))
            root_stats[root_name]["name_records"] += 1

    missing_required = sorted(REQUIRED_STRING_ROOTS - found_roots)
    if missing_required:
        raise SourceError("Required String image roots are missing: " + ", ".join(missing_required))
    if not marker_found:
        raise SourceError("Selected String archives do not contain the ClassicWorld marker in Eqp.img")
    if not any(kind == "mob" for kind, _ in names):
        raise SourceError("Required String name root contains no supported monster names")
    if not any(kind == "map" for kind, _ in names):
        raise SourceError("Required String name root contains no supported map names")

    gaps = []
    coverage = {}
    for root_name in STRING_ROOTS:
        stats = root_stats[root_name]
        if root_name not in found_roots:
            status = "source_absent"
            if root_name not in REQUIRED_STRING_ROOTS:
                gaps.append({"kind": _string_name_kind(root_name, "")[0], "category": "image_absent", "count": 1, "examples": [root_name]})
        elif stats["name_records"] == 0:
            status = "source_present_empty"
            if root_name not in REQUIRED_STRING_ROOTS:
                gaps.append({"kind": _string_name_kind(root_name, "")[0], "category": "source_present_empty", "count": 1, "examples": stats["examples"]})
        else:
            status = "source_present"
        coverage[root_name] = {**stats, "selected_files": len(string_files), "status": status}
    return names, coverage, gaps


def _domain_results(context: dict[str, Any]) -> list[dict[str, Any]]:
    import cw_extract_actors
    import cw_extract_items
    import cw_extract_quests
    import cw_extract_skills

    outputs = []
    for module in (cw_extract_actors, cw_extract_items, cw_extract_skills, cw_extract_quests):
        result = module.extract(context)
        if not isinstance(result, dict):
            raise SourceError(f"{module.__name__}.extract returned a non-object result")
        for key in ("entities", "relations", "placements", "coverage", "gaps"):
            if key not in result:
                raise SourceError(f"{module.__name__}.extract omitted {key}")
        if not isinstance(result["entities"], list) or not isinstance(result["relations"], list) or not isinstance(result["placements"], list) or not isinstance(result["coverage"], dict) or not isinstance(result["gaps"], list):
            raise SourceError(f"{module.__name__}.extract returned invalid collection types")
        outputs.append(result)
    return outputs


def _add_static_entity(records: dict[tuple[str, str], dict[str, Any]], incoming: dict[str, Any]) -> None:
    try:
        kind = incoming["kind"]
        original_id = incoming["original_id"]
        normalized_id = incoming["normalized_id"]
        fields = incoming["fields"]
        sources = incoming["sources"]
    except (KeyError, TypeError) as error:
        raise SourceError("Domain entity does not match the entity contract") from error
    if kind not in KINDS or not isinstance(original_id, str) or not isinstance(normalized_id, str) or normalize_id(original_id, kind) != normalized_id:
        raise SourceError(f"Invalid domain entity identity: {kind}/{normalized_id}")
    if not isinstance(fields, dict) or not isinstance(sources, list):
        raise SourceError(f"Invalid domain entity fields or sources: {kind}/{normalized_id}")
    key = (kind, normalized_id)
    candidate = {"kind": kind, "normalized_id": normalized_id, "original_id": original_id,
                 "name": incoming.get("name"), "fields": copy.deepcopy(fields), "sources": _merged_sources(sources)}
    previous = records.get(key)
    if previous is None:
        records[key] = candidate
        return
    if previous["fields"] != candidate["fields"]:
        raise _conflict(kind, normalized_id, previous, candidate, "static definition")
    if previous["name"] is not None and candidate["name"] is not None and previous["name"] != candidate["name"]:
        raise _conflict(kind, normalized_id, previous, candidate, "name")
    if previous["name"] is None:
        previous["name"] = candidate["name"]
    previous["sources"] = _merged_sources(previous["sources"], candidate["sources"])


def _resolve_eqp_name_kinds(names: dict[tuple[str, str], dict[str, Any]], static: dict[tuple[str, str], dict[str, Any]]) -> None:
    for key, value in list(names.items()):
        if not value.get("family_unresolved") or key[0] != "equipment":
            continue
        candidates = [kind for kind in ("equipment", "item") if (kind, key[1]) in static]
        if len(candidates) == 1:
            target = (candidates[0], key[1])
            if target == key:
                value["family_unresolved"] = False
                continue
            value["family_unresolved"] = False
            value["section"] = static[target]["fields"].get("section", "equipment" if target[0] == "equipment" else "items")
            existing = names.get(target)
            if existing is None:
                names[target] = value
            else:
                if existing["name"] is not None and value["name"] is not None and existing["name"] != value["name"]:
                    raise _conflict(target[0], target[1], existing, value, "name")
                if existing["name_info"] != value["name_info"]:
                    raise _conflict(target[0], target[1], existing, value, "name properties")
                if existing["name"] is None:
                    existing["name"] = value["name"]
                existing["sources"] = _merged_sources(existing["sources"], value["sources"])
                if existing.get("family") is None:
                    existing["family"] = value.get("family")
                if existing.get("section") is None:
                    existing["section"] = value.get("section")
            del names[key]


def _relation_address(relation: dict[str, Any]) -> str:
    sources = relation.get("sources", [])
    if sources:
        return _source_address(sources[0])
    return str(relation.get("node_path", "unknown source"))


def _add_relation(relations: dict[tuple[str, ...], dict[str, Any]], incoming: dict[str, Any]) -> None:
    required = ("source_kind", "source_id", "relation", "target_kind", "target_id", "node_path", "raw", "sources")
    if any(key not in incoming for key in required):
        raise SourceError("Domain relation does not match the relation contract")
    if not all(isinstance(incoming[key], str) for key in required[:6]) or not isinstance(incoming["raw"], dict) or not isinstance(incoming["sources"], list):
        raise SourceError("Domain relation contains invalid field types")
    key = tuple(incoming[name] for name in required[:6])
    candidate = {name: copy.deepcopy(incoming[name]) for name in required}
    previous = relations.get(key)
    if previous is None:
        relations[key] = candidate
        return
    if previous["raw"] != candidate["raw"]:
        raise SourceError(f"Conflicting relation at {_relation_address(previous)} and {_relation_address(candidate)}")
    previous["sources"] = _merged_sources(previous["sources"], candidate["sources"])


def _resolve_relations(relations: dict[tuple[str, ...], dict[str, Any]], entity_keys: set[tuple[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resolved = []
    gaps = []
    for relation in sorted(relations.values(), key=lambda item: tuple(item[key] for key in ("source_kind", "source_id", "relation", "target_kind", "target_id", "node_path"))):
        source_key = (relation["source_kind"], relation["source_id"])
        if source_key not in entity_keys:
            raise SourceError(f"Relation source endpoint is missing at {_relation_address(relation)}: {source_key[0]}/{source_key[1]}")
        item = dict(relation)
        if item["target_kind"] == "item_ref":
            candidates = [kind for kind in ("equipment", "item") if (kind, item["target_id"]) in entity_keys]
            if len(candidates) == 1:
                item["target_kind"] = candidates[0]
                item["resolved"] = True
            else:
                item["resolved"] = False
                category = "ambiguous_item_ref" if len(candidates) > 1 else "unresolved_target"
                gaps.append({"kind": "item_ref", "category": category, "count": 1, "examples": [_relation_address(relation)]})
        else:
            item["resolved"] = (item["target_kind"], item["target_id"]) in entity_keys
            if not item["resolved"]:
                gaps.append({"kind": item["target_kind"], "category": "unresolved_target", "count": 1, "examples": [_relation_address(relation)]})
        resolved.append(item)
    return resolved, gaps


def _check_placements(placements: list[dict[str, Any]], relations: list[dict[str, Any]]) -> None:
    expected = Counter((row["source_id"], row["target_id"]) for row in relations if row["relation"] == "map_mob")
    actual = Counter()
    for row in placements:
        for key in ("map_id", "node_id", "mob_id", "x", "y", "raw_json"):
            if key not in row:
                raise SourceError("Domain placement does not match the placement contract")
        if not isinstance(row["map_id"], str) or not isinstance(row["node_id"], str) or not isinstance(row["mob_id"], str):
            raise SourceError("Domain placement identity fields must be strings")
        actual[(row["map_id"], row["mob_id"])] += 1
    if actual != expected:
        raise SourceError("Map mob relations and placement projection do not match")


def _bounded_gaps(gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for gap in gaps:
        kind = str(gap.get("kind", "source"))
        category = str(gap.get("category", "unknown"))
        try:
            count = max(0, int(gap.get("count", 1)))
        except (TypeError, ValueError):
            count = 1
        key = (kind, category)
        target = grouped.setdefault(key, {"kind": kind, "category": category, "count": 0, "examples": []})
        target["count"] += count
        examples = gap.get("examples", [])
        if not isinstance(examples, list):
            examples = [examples]
        for example in examples:
            if isinstance(example, dict):
                value = copy.deepcopy(example)
            else:
                value = str(example)
            if value not in target["examples"] and len(target["examples"]) < 3:
                target["examples"].append(value)
    return [grouped[key] for key in sorted(grouped)]


def extract_snapshot(data_root: Path, prepared: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        root = Path(data_root).resolve(strict=True)
    except OSError as error:
        raise SourceError(f"Cannot resolve source data directory {data_root}: {error}") from error
    prepared = prepared or prepare_source(root)
    if root != prepared["data_root"]:
        raise SourceError("Prepared source belongs to a different data directory")
    files: list[Path] = prepared["files"]
    file_hashes = {record["relative_path"]: record["sha256"] for record in prepared["records"]}
    names, name_root_coverage, name_gaps = _parse_name_archives(root, files, file_hashes)
    context = {"data_root": root, "files": files, "file_hashes": file_hashes, "names": names}
    domain_results = _domain_results(context)

    static_records: dict[tuple[str, str], dict[str, Any]] = {}
    relations: dict[tuple[str, ...], dict[str, Any]] = {}
    placements: list[dict[str, Any]] = []
    domain_gaps = list(name_gaps)
    source_coverage: dict[str, dict[str, int]] = {}
    emitted_static_counts = {kind: 0 for kind in KINDS}
    for result in domain_results:
        domain_gaps.extend(result["gaps"])
        for incoming in result["entities"]:
            _add_static_entity(static_records, incoming)
            emitted_static_counts[incoming["kind"]] += 1
        for incoming in result["relations"]:
            _add_relation(relations, incoming)
        placements.extend(copy.deepcopy(result["placements"]))
        for kind, counts in result["coverage"].items():
            if kind not in KINDS or not isinstance(counts, dict):
                raise SourceError(f"Domain coverage contains an invalid kind: {kind}")
            normalized = {}
            for field, legacy_field in (("selected_files", "selected_file_count"), ("parsed_images", "parsed_image_count"), ("static_records", "static_record_count")):
                value = counts.get(field, counts.get(legacy_field))
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise SourceError(f"Domain coverage {kind}/{field} must be a non-negative integer")
                normalized[field] = value
            if kind in source_coverage:
                raise SourceError(f"Multiple domain modules own coverage for {kind}")
            source_coverage[kind] = normalized

    for kind in KINDS:
        source_coverage.setdefault(kind, {"selected_files": 0, "parsed_images": 0, "static_records": 0})
    static_counts = {kind: sum(1 for candidate_kind, _ in static_records if candidate_kind == kind) for kind in KINDS}
    for kind, counts in source_coverage.items():
        if counts["static_records"] != emitted_static_counts[kind]:
            raise SourceError(f"Domain coverage static record count disagrees for {kind}: {counts['static_records']} != {emitted_static_counts[kind]}")

    _resolve_eqp_name_kinds(names, static_records)
    for (kind, normalized_id), name_record in names.items():
        if name_record.get("family_unresolved"):
            domain_gaps.append({"kind": kind, "category": "name_family_unresolved", "count": 1, "examples": [_source_address(name_record["sources"][0])]})

    assembled = []
    entity_keys = set(names) | set(static_records)
    for kind, normalized_id in sorted(entity_keys):
        name_record = names.get((kind, normalized_id))
        static_record = static_records.get((kind, normalized_id))
        fields: dict[str, Any] = copy.deepcopy(static_record["fields"]) if static_record else {}
        name = static_record.get("name") if static_record else None
        sources = list(static_record["sources"]) if static_record else []
        original_id = static_record.get("original_id") if static_record else None
        if name_record:
            if name is not None and name_record["name"] is not None and name != name_record["name"]:
                raise _conflict(kind, normalized_id, static_record, name_record, "name")
            original_id = name_record["original_id"]
            if name_record["name"] is not None:
                name = name_record["name"]
            if name_record["name_info"]:
                fields["name_info"] = copy.deepcopy(name_record["name_info"])
            source_mismatch = False
            if name_record.get("family") is not None:
                static_family = fields.get("family")
                if static_family is not None and static_family != name_record["family"]:
                    source_mismatch = True
                elif static_family is None:
                    fields["family"] = name_record["family"]
            if name_record.get("section") is not None:
                static_section = fields.get("section")
                if static_section is not None and static_section != name_record["section"]:
                    source_mismatch = True
                elif static_section is None:
                    fields["section"] = name_record["section"]
            if source_mismatch:
                static_source = _source_address(static_record["sources"][0]) if static_record and static_record["sources"] else ""
                domain_gaps.append({"kind": kind, "category": "name_static_mismatch", "count": 1,
                                   "examples": [_source_address(name_record["sources"][0]), static_source]})
            sources = _merged_sources(name_record["sources"], sources)
        assembled.append({"kind": kind, "normalized_id": normalized_id, "original_id": original_id, "name": name,
                          "fields": fields, "sources": sources})

    entity_counts = {kind: sum(1 for item in assembled if item["kind"] == kind) for kind in KINDS}
    named_keys = {
        (item["kind"], item["normalized_id"])
        for item in assembled
        if isinstance(item["name"], str) and item["name"].strip()
    }
    name_counts = {kind: sum(1 for candidate_kind, _ in named_keys if candidate_kind == kind) for kind in KINDS}
    named_without_static = {kind: sum(1 for key in named_keys if key[0] == kind and key not in static_records) for kind in KINDS}
    static_without_name = {kind: sum(1 for key in static_records if key[0] == kind and key not in named_keys) for kind in KINDS}
    for kind in KINDS:
        mismatch_count = named_without_static[kind] + static_without_name[kind]
        if mismatch_count:
            examples = []
            for item in assembled:
                key = (item["kind"], item["normalized_id"])
                if key[0] == kind and key in named_keys and key not in static_records and item["sources"]:
                    examples.append(_source_address(item["sources"][0]))
            for key, record in static_records.items():
                if key[0] == kind and key not in named_keys and record["sources"]:
                    examples.append(_source_address(record["sources"][0]))
            domain_gaps.append({"kind": kind, "category": "name_static_mismatch", "count": mismatch_count, "examples": examples})

    resolved_relations, relation_gaps = _resolve_relations(relations, entity_keys)
    domain_gaps.extend(relation_gaps)
    _check_placements(placements, resolved_relations)

    relation_counts = {relation: 0 for relation in RELATION_TYPES}
    for relation in resolved_relations:
        relation_counts[relation["relation"]] = relation_counts.get(relation["relation"], 0) + 1
    gaps = _bounded_gaps(domain_gaps)
    coverage = {
        "by_kind": {
            kind: {
                **source_coverage[kind],
                "names": name_counts[kind],
                "entities": entity_counts[kind],
                "named_without_static": named_without_static[kind],
                "static_without_name": static_without_name[kind],
            }
            for kind in KINDS
        },
        "entities": entity_counts,
        "named": name_counts,
        "static": static_counts,
        "named_without_static": named_without_static,
        "static_without_name": static_without_name,
        "name_roots": name_root_coverage,
        "selected_source_files": len(files),
        "relation_count": len(resolved_relations),
        "relations": relation_counts,
        "placements": len(placements),
        "gaps": gaps,
    }
    verify_source_unchanged(prepared)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        **SOURCE_IDENTITY,
        "schema_version": SCHEMA_VERSION,
        "parser_revision": prepared["parser_revision"],
        "source_fingerprint": prepared["source_fingerprint"],
        "built_at": now,
        "source_files": prepared["records"],
        "entity_counts": entity_counts,
        "relation_count": len(resolved_relations),
        "relation_counts": relation_counts,
        "placement_count": len(placements),
        "coverage": coverage,
        "gaps": gaps,
        "entities": assembled,
        "relations": resolved_relations,
        "placements": placements,
    }
