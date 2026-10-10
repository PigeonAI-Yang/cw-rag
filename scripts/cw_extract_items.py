from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from cw_common import entity, evidence, flatten, iter_images


_EQUIPMENT_FAMILIES = (
    "Cap", "Coat", "Longcoat", "Pants", "Shoes", "Glove", "Cape",
    "Weapon", "Shield", "Accessory", "Ring", "PetEquip", "TamingMob", "Dragon",
)
_APPEARANCE_FAMILIES = ("Face", "Hair", "Morph")
_ITEM_FAMILIES = ("Consume", "Etc", "Install", "Cash", "Special", "Pet")
_FAMILY_REGISTRY = {
    **{("Character", family): ("equipment", "equipment") for family in _EQUIPMENT_FAMILIES},
    **{("Character", family): ("item", "appearance") for family in _APPEARANCE_FAMILIES},
    **{("Item", family): ("item", "pets" if family == "Pet" else "items") for family in _ITEM_FAMILIES},
}
_KINDS = ("equipment", "item", "set", "recipe")
_ETC_IMAGES = {"SetItemInfo.img": "set", "ItemMake.img": "recipe"}
_GAP_EXAMPLE_LIMIT = 3


def _relative_resource(context: dict[str, Any], candidate: Path) -> str | None:
    root = Path(context["data_root"]).resolve()
    path = Path(candidate)
    if not path.is_absolute():
        path = root / path
    try:
        return path.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return None


def _selected_resources(context: dict[str, Any]) -> tuple[dict, list[Path]]:
    family_resources = {}
    etc_resources = {}
    root = Path(context["data_root"]).resolve()

    for candidate in context["files"]:
        path = Path(candidate)
        relative = _relative_resource(context, path)
        if relative is None:
            continue
        parts = PurePosixPath(relative).parts
        if "_Canvas" in parts or path.suffix.casefold() != ".wz" or "_" not in path.stem:
            continue

        if len(parts) >= 3 and parts[0] in ("Character", "Item"):
            source_root = parts[0]
            family = parts[-2]
            registered = _FAMILY_REGISTRY.get((source_root, family))
            if registered is None:
                kind, section, known = "item", "items", False
            else:
                kind, section = registered
                known = True
            family_resources[relative] = {
                "path": path if path.is_absolute() else root / path,
                "root": source_root,
                "family": family,
                "kind": kind,
                "section": section,
                "known": known,
                "grouped": source_root == "Item" and family != "Pet",
            }
        elif len(parts) == 2 and parts[0] == "Etc":
            etc_resources[relative] = path if path.is_absolute() else root / path

    return family_resources, [etc_resources[key] for key in sorted(etc_resources)]


def _numeric_id(value: Any) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdigit()


def _has_payload(value: Any) -> bool:
    if isinstance(value, (dict, list, tuple)):
        return any(_has_payload(child) for child in value.values()) if isinstance(value, dict) else any(
            _has_payload(child) for child in value
        )
    return True


def _add_gap(gaps: dict, kind: str, category: str, example: dict | None = None) -> None:
    key = (kind, category)
    gap = gaps.setdefault(key, {"kind": kind, "category": category, "count": 0, "examples": []})
    gap["count"] += 1
    if example is not None and len(gap["examples"]) < _GAP_EXAMPLE_LIMIT:
        gap["examples"].append(example)


def _new_coverage() -> dict[str, dict[str, int]]:
    return {
        kind: {"selected_files": 0, "parsed_images": 0, "static_records": 0}
        for kind in _KINDS
    }


def _record_fields(record: Any, family: str, section: str) -> dict[str, Any]:
    fields: dict[str, Any] = {"family": family, "section": section, "raw": record}
    if isinstance(record, dict):
        if "info" in record:
            fields["static_info"] = flatten(record["info"], "info/")
        if "spec" in record:
            fields["item_effects"] = flatten(record["spec"], "spec/")
    return fields


def _empty_result() -> dict[str, Any]:
    return {
        "entities": [],
        "relations": [],
        "placements": [],
        "coverage": _new_coverage(),
        "gaps": [],
    }


def extract(context: dict[str, Any]) -> dict[str, Any]:
    """Extract equipment, items, sets, and recipes from prepared source images."""
    result = _empty_result()
    coverage = result["coverage"]
    gaps: dict[tuple[str, str], dict] = {}
    family_resources, etc_paths = _selected_resources(context)

    for relative, source in family_resources.items():
        coverage[source["kind"]]["selected_files"] += 1

    family_paths = [family_resources[key]["path"] for key in sorted(family_resources)]
    for image in iter_images(context, family_paths, lambda _resource, _image: True):
        relative = image["resource"].replace("\\", "/")
        source = family_resources[relative]
        kind = source["kind"]
        coverage[kind]["parsed_images"] += 1
        tree = image["tree"]
        image_stem = PurePosixPath(image["image"]).stem

        if not tree:
            _add_gap(
                gaps,
                kind,
                "source_present_empty",
                evidence(image, role="raw_wz_image", node=".", source_id=image_stem),
            )
            continue

        if source["grouped"]:
            records = ((raw_id, record, raw_id) for raw_id, record in tree.items())
        else:
            records = ((image_stem, tree, ".") for _ in (0,))

        for raw_id, record, node in records:
            if not _numeric_id(raw_id) or not _has_payload(record):
                continue
            source_evidence = evidence(
                image, role="raw_wz_image", node=node, source_id=raw_id
            )
            fields = _record_fields(record, source["family"], source["section"])
            result["entities"].append(
                entity(kind, raw_id, fields, [source_evidence])
            )
            coverage[kind]["static_records"] += 1
            if not source["known"]:
                _add_gap(
                    gaps,
                    kind,
                    "unclassified_family",
                    source_evidence | {"family": source["family"]},
                )

    for kind in ("equipment", "item"):
        if coverage[kind]["selected_files"] == 0:
            roots = "Character/" if kind == "equipment" else "Character/appearance or Item/"
            _add_gap(gaps, kind, "source_absent", {"resource_root": roots})
        elif coverage[kind]["parsed_images"] == 0:
            _add_gap(gaps, kind, "image_absent", {"resource_root": "Character/ or Item/"})

    for kind in ("set", "recipe"):
        coverage[kind]["selected_files"] = len(etc_paths)
    if not etc_paths:
        _add_gap(gaps, "set", "source_absent", {"resource_root": "Etc/"})
        _add_gap(gaps, "recipe", "source_absent", {"resource_root": "Etc/"})

    seen_etc_images: set[str] = set()
    if etc_paths:
        def accept_etc(_resource: str, image_name: str) -> bool:
            return PurePosixPath(image_name).name in _ETC_IMAGES

        for image in iter_images(context, etc_paths, accept_etc):
            anchor = PurePosixPath(image["image"]).name
            kind = _ETC_IMAGES[anchor]
            seen_etc_images.add(anchor)
            coverage[kind]["parsed_images"] += 1
            tree = image["tree"]
            image_stem = PurePosixPath(image["image"]).stem

            if not tree:
                _add_gap(
                    gaps,
                    kind,
                    "source_present_empty",
                    evidence(image, role="raw_wz_image", node=".", source_id=image_stem),
                )
                continue

            if kind == "set":
                records = (
                    (raw_id, record, raw_id)
                    for raw_id, record in tree.items()
                    if _numeric_id(raw_id) and _has_payload(record)
                )
            else:
                records = (
                    (f"{group_id}/{entry_id}", record, f"{group_id}/{entry_id}", group_id, entry_id)
                    for group_id, group in tree.items()
                    if _numeric_id(group_id) and isinstance(group, dict)
                    for entry_id, record in group.items()
                    if _numeric_id(entry_id) and _has_payload(record)
                )

            for row in records:
                if kind == "set":
                    raw_id, record, node = row
                    fields = {"set_data": record, "raw": record}
                else:
                    raw_id, record, node, group_id, entry_id = row
                    fields = {
                        "recipe_group": group_id,
                        "recipe_entry": entry_id,
                        "recipe_data": record,
                        "raw": record,
                    }
                source_evidence = evidence(
                    image, role="raw_wz_image", node=node, source_id=raw_id
                )
                result["entities"].append(
                    entity(kind, raw_id, fields, [source_evidence])
                )
                coverage[kind]["static_records"] += 1
                _add_gap(
                    gaps,
                    kind,
                    "reference_shape_unmapped",
                    source_evidence,
                )

    for anchor, kind in _ETC_IMAGES.items():
        if etc_paths and anchor not in seen_etc_images:
            _add_gap(gaps, kind, "image_absent", {"resource_root": "Etc/", "image": anchor})

    result["gaps"] = [gaps[key] for key in sorted(gaps)]
    return result
