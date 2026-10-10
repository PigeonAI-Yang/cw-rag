from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

import cw_common


def _relative_resources(context: dict) -> list[tuple[str, Path]]:
    root = Path(context["data_root"]).resolve(strict=False)
    resources = []
    for value in context["files"]:
        path = Path(value).resolve(strict=False)
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        resources.append((relative, path))
    return resources


def _selected_paths(context: dict, kind: str) -> list[Path]:
    selected = []
    for relative, path in _relative_resources(context):
        parts = PurePosixPath(relative).parts
        suffix = path.suffix.casefold()
        if kind == "mob":
            matches = (
                len(parts) == 2
                and parts[0].casefold() == "packs"
                and path.name.casefold().startswith("mob_")
                and suffix == ".ms"
            )
        elif kind == "map":
            matches = (
                len(parts) >= 3
                and parts[0].casefold() == "map"
                and parts[1].casefold() == "map"
                and suffix == ".wz"
                and all(part.casefold() != "_canvas" for part in parts)
            )
        else:
            matches = (
                len(parts) >= 2
                and parts[0].casefold() == "npc"
                and suffix == ".wz"
                and all(part.casefold() != "_canvas" for part in parts)
            )
        if matches:
            selected.append((relative.casefold(), relative, path))
    return [path for _, _, path in sorted(selected)]


def _numeric_stem(image_path: str) -> str | None:
    path = PurePosixPath(image_path.replace("\\", "/"))
    stem = path.stem
    if path.suffix.casefold() != ".img" or not stem or not stem.isascii() or not stem.isdigit():
        return None
    return stem


def _accept(kind: str):
    def accept(_resource: str, image_path: str) -> bool:
        if _numeric_stem(image_path) is None:
            return False
        if kind == "mob":
            parts = PurePosixPath(image_path.replace("\\", "/")).parts
            return len(parts) == 2 and parts[0] == "Mob"
        return all(part.casefold() != "_canvas" for part in PurePosixPath(image_path.replace("\\", "/")).parts)

    return accept


def _empty_tree(value: Any) -> bool:
    if isinstance(value, dict):
        return all(_empty_tree(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return all(_empty_tree(child) for child in value)
    return False


def _reference_id(value: Any, kind: str) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        raw_id = str(value) if value >= 0 else None
    elif isinstance(value, str) and value and value.isascii() and value.isdigit():
        raw_id = value
    else:
        raw_id = None
    return cw_common.normalize_id(raw_id, kind) if raw_id is not None else None


def _source_address(source: dict) -> str:
    return f"{source['resource']}/{source['image']}"


def _merge_entity(entities: dict, record: dict) -> None:
    key = (record["kind"], record["normalized_id"])
    previous = entities.get(key)
    if previous is None:
        entities[key] = record
        return
    if previous["fields"] != record["fields"]:
        first = _source_address(previous["sources"][0])
        second = _source_address(record["sources"][0])
        raise cw_common.SourceError(
            f"Conflicting {record['kind']} static definition for {record['normalized_id']} at {first} and {second}"
        )
    for source in record["sources"]:
        if source not in previous["sources"]:
            previous["sources"].append(source)


def _merge_relation(relations: dict, record: dict) -> None:
    key = tuple(
        record[field]
        for field in ("source_kind", "source_id", "relation", "target_kind", "target_id", "node_path")
    )
    previous = relations.get(key)
    if previous is None:
        relations[key] = record
        return
    if previous["raw"] != record["raw"]:
        first = _source_address(previous["sources"][0])
        second = _source_address(record["sources"][0])
        raise cw_common.SourceError(f"Conflicting relation at {first}/{record['node_path']} and {second}/{record['node_path']}")
    for source in record["sources"]:
        if source not in previous["sources"]:
            previous["sources"].append(source)


def _append_relation(
    relations: dict,
    image: dict,
    *,
    source_id: str,
    source_original_id: str,
    relation: str,
    target_kind: str,
    target_id: str,
    node_path: str,
    raw: dict,
) -> None:
    source = cw_common.evidence(image, role="static", node=node_path, source_id=source_original_id)
    _merge_relation(
        relations,
        {
            "source_kind": "map",
            "source_id": source_id,
            "relation": relation,
            "target_kind": target_kind,
            "target_id": target_id,
            "node_path": node_path,
            "raw": raw,
            "sources": [source],
        },
    )


def _actor_fields(tree: dict, *, kind: str) -> dict:
    fields = {"raw": tree}
    info = tree.get("info")
    if isinstance(info, dict):
        fields["static_info"] = cw_common.flatten(info, "info/")
    if kind == "map":
        life = tree.get("life")
        portal = tree.get("portal")
        if isinstance(life, dict):
            fields["placements"] = cw_common.flatten(life, "life/")
        if isinstance(portal, dict):
            fields["portals"] = cw_common.flatten(portal, "portal/")
    return fields


def _add_image(context: dict, image: dict, kind: str, actors: dict, relations: dict, placements: dict) -> None:
    raw_id = _numeric_stem(image["image"])
    if raw_id is None:
        return
    tree = image["tree"]
    fields = _actor_fields(tree, kind=kind)
    source = cw_common.evidence(image, role="static", node="", source_id=raw_id)
    record = cw_common.entity(kind, raw_id, fields, [source])
    _merge_entity(actors, record)
    if kind != "map":
        return

    normalized_map_id = record["normalized_id"]
    life = tree.get("life")
    if isinstance(life, dict):
        for node_id, node in life.items():
            if not isinstance(node, dict) or node.get("type") not in ("m", "n"):
                continue
            target_kind = "mob" if node["type"] == "m" else "npc"
            target_id = _reference_id(node.get("id"), target_kind)
            if target_id is None:
                raise cw_common.SourceError(
                    f"Invalid placed {target_kind} ID in {_source_address(source)}/life/{node_id}: {node.get('id')!r}"
                )
            node_path = f"life/{node_id}"
            _append_relation(
                relations,
                image,
                source_id=normalized_map_id,
                source_original_id=raw_id,
                relation=f"map_{target_kind}",
                target_kind=target_kind,
                target_id=target_id,
                node_path=node_path,
                raw=node,
            )
            if target_kind == "mob":
                placement = {
                    "map_id": normalized_map_id,
                    "node_id": str(node_id),
                    "mob_id": target_id,
                    "x": cw_common.integer_or_none(node.get("x")),
                    "y": cw_common.integer_or_none(node.get("y")),
                    "raw_json": json.dumps(node, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                }
                placements[(normalized_map_id, str(node_id), target_id)] = placement

    portals = tree.get("portal")
    if isinstance(portals, dict):
        for node_id, node in portals.items():
            if not isinstance(node, dict):
                continue
            target_id = _reference_id(node.get("tm"), "map")
            if target_id is None:
                continue
            _append_relation(
                relations,
                image,
                source_id=normalized_map_id,
                source_original_id=raw_id,
                relation="portal_target",
                target_kind="map",
                target_id=target_id,
                node_path=f"portal/{node_id}",
                raw=node,
            )


def _coverage(kind: str, paths: list[Path], images: list[dict], records: dict) -> tuple[dict, list[dict]]:
    result = {
        "selected_files": len(paths),
        "parsed_images": len(images),
        "static_records": sum(record["kind"] == kind for record in records.values()),
        "empty_images": sum(_empty_tree(image["tree"]) for image in images),
    }
    gaps = []
    if not paths:
        gaps.append({"kind": kind, "category": "source_absent", "count": 1})
    elif not images:
        gaps.append({"kind": kind, "category": "image_absent", "count": 1})
    empty = [image for image in images if _empty_tree(image["tree"])]
    if empty:
        gaps.append(
            {
                "kind": kind,
                "category": "source_present_empty",
                "count": len(empty),
                "examples": [
                    cw_common.evidence(image, role="static", node="", source_id=_numeric_stem(image["image"]))
                    for image in empty[:3]
                ],
                "omitted": max(0, len(empty) - 3),
            }
        )
    return result, gaps


def extract(context: dict) -> dict:
    selected = {kind: _selected_paths(context, kind) for kind in ("mob", "map", "npc")}
    images = {kind: list(cw_common.iter_images(context, paths, _accept(kind))) for kind, paths in selected.items()}
    actors: dict[tuple[str, str], dict] = {}
    relations: dict[tuple, dict] = {}
    placements: dict[tuple[str, str, str], dict] = {}

    for kind in ("mob", "map", "npc"):
        for image in images[kind]:
            _add_image(context, image, kind, actors, relations, placements)

    coverage = {}
    gaps = []
    for kind in ("mob", "map", "npc"):
        coverage[kind], kind_gaps = _coverage(kind, selected[kind], images[kind], actors)
        gaps.extend(kind_gaps)

    return {
        "entities": [actors[key] for key in sorted(actors)],
        "relations": [relations[key] for key in sorted(relations)],
        "placements": [placements[key] for key in sorted(placements)],
        "coverage": coverage,
        "gaps": gaps,
    }
