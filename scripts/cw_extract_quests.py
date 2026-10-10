from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from cw_common import entity, evidence, iter_images, normalize_id


_SECTION_FIELDS = {
    "QuestInfo": "quest_info",
    "Check": "quest_check",
    "Act": "quest_act",
    "Say": "quest_say",
}
_INDEXED_REFERENCES = {
    "item": ("item_ref", "item"),
    "mob": ("mob", "mob"),
    "quest": ("quest", "quest"),
}


def _relative_resource(context: dict, path: Path) -> str:
    root = Path(context["data_root"]).resolve(strict=False)
    candidate = Path(path).resolve(strict=False)
    return candidate.relative_to(root).as_posix()


def _is_quest_resource(context: dict, path: Path) -> bool:
    try:
        parts = PurePosixPath(_relative_resource(context, path)).parts
    except (OSError, ValueError):
        return False
    if len(parts) != 3 or tuple(part.casefold() for part in parts[:2]) != ("quest", "questdata"):
        return False
    filename = parts[2].casefold()
    return filename.startswith("questdata_") and filename.endswith(".wz")


def _numeric_id(value: Any, kind: str) -> tuple[str, str] | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value < 0:
            return None
        raw_id = str(value)
    elif isinstance(value, str):
        raw_id = value
    else:
        return None

    if not raw_id or not raw_id.isascii() or not raw_id.isdigit():
        return None
    width = 7 if kind == "mob" else 20
    if len(raw_id.lstrip("0") or "0") > width:
        return None
    return raw_id, normalize_id(raw_id, kind)


def _image_quest_id(image_path: str) -> str | None:
    image = PurePosixPath(image_path)
    if len(image.parts) != 1 or image.suffix.casefold() != ".img":
        return None
    parsed = _numeric_id(image.stem, "quest")
    return parsed[0] if parsed else None


def _accept_image(resource: str, image: str) -> bool:
    parts = PurePosixPath(resource).parts
    if len(parts) != 3 or tuple(part.casefold() for part in parts[:2]) != ("quest", "questdata"):
        return False
    filename = parts[2].casefold()
    return filename.startswith("questdata_") and filename.endswith(".wz") and _image_quest_id(image) is not None


def _index_key(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value.isascii() and value.isdigit()


def _index_order(value: str) -> tuple[int, str]:
    return len(value), value


def _relation(
    image: dict,
    source_id: str,
    normalized_source_id: str,
    relation: str,
    target_kind: str,
    normalization_kind: str,
    raw_target_id: Any,
    node_path: str,
    raw_branch: dict,
) -> dict | None:
    parsed_target = _numeric_id(raw_target_id, normalization_kind)
    if parsed_target is None:
        return None
    return {
        "source_kind": "quest",
        "source_id": normalized_source_id,
        "relation": relation,
        "target_kind": target_kind,
        "target_id": parsed_target[1],
        "node_path": node_path,
        "raw": raw_branch,
        "sources": [evidence(image, role="quest", node=node_path, source_id=source_id)],
    }


def _stage_relations(image: dict, source_id: str, normalized_source_id: str, section: str, stages: Any) -> list[dict]:
    if not isinstance(stages, dict):
        return []

    relations = []
    for stage_index in sorted((key for key in stages if _index_key(key)), key=_index_order):
        stage = stages[stage_index]
        if not isinstance(stage, dict):
            continue
        prefix = f"{section}/{stage_index}"

        npc = _relation(
            image,
            source_id,
            normalized_source_id,
            f"quest_{section.casefold()}_npc",
            "npc",
            "npc",
            stage.get("npc"),
            f"{prefix}/npc",
            stage,
        )
        if npc is not None:
            relations.append(npc)

        for field, (target_kind, normalization_kind) in _INDEXED_REFERENCES.items():
            indexed = stage.get(field)
            if not isinstance(indexed, dict):
                continue
            for index in sorted((key for key in indexed if _index_key(key)), key=_index_order):
                entry = indexed[index]
                if not isinstance(entry, dict) or "id" not in entry:
                    continue
                relation = _relation(
                    image,
                    source_id,
                    normalized_source_id,
                    f"quest_{section.casefold()}_{field}",
                    target_kind,
                    normalization_kind,
                    entry["id"],
                    f"{prefix}/{field}/{index}/id",
                    entry,
                )
                if relation is not None:
                    relations.append(relation)

        if section == "Act" and "nextQuest" in stage:
            relation = _relation(
                image,
                source_id,
                normalized_source_id,
                "quest_next",
                "quest",
                "quest",
                stage["nextQuest"],
                f"{prefix}/nextQuest",
                stage,
            )
            if relation is not None:
                relations.append(relation)

    return relations


def _gap(kind: str, category: str, count: int, examples: list[dict]) -> dict:
    return {"kind": kind, "category": category, "count": count, "examples": examples[:3]}


def extract(context: dict) -> dict[str, Any]:
    paths = [Path(path) for path in context["files"] if _is_quest_resource(context, Path(path))]
    entities = []
    relations = []
    seen_resources = set()
    empty_examples = []
    parsed_images = 0
    empty_images = 0

    for image in iter_images(context, paths, _accept_image):
        parsed_images += 1
        seen_resources.add(image["resource"])
        raw_id = _image_quest_id(image["image"])
        if raw_id is None:
            continue
        normalized_id = normalize_id(raw_id, "quest")
        tree = image["tree"]
        if not tree:
            empty_images += 1
            empty_examples.append({"resource": image["resource"], "image": image["image"]})
            continue

        fields = {field: tree[section] for section, field in _SECTION_FIELDS.items() if section in tree}
        fields["raw"] = tree
        quest_info = tree.get("QuestInfo")
        name = quest_info.get("name") if isinstance(quest_info, dict) else None
        if not isinstance(name, str):
            name = None

        source_sections = [section for section in _SECTION_FIELDS if section in tree]
        sources = [evidence(image, role="quest", node=section, source_id=raw_id) for section in source_sections]
        if not sources:
            sources = [evidence(image, role="quest", node="", source_id=raw_id)]
        entities.append(entity("quest", raw_id, fields, sources, name=name))

        for section in ("Check", "Act"):
            relations.extend(_stage_relations(image, raw_id, normalized_id, section, tree.get(section)))

    resource_names = [_relative_resource(context, path) for path in paths]
    missing_resources = [resource for resource in resource_names if resource not in seen_resources]
    coverage = {
        "quest": {
            "selected_files": len(paths),
            "missing_files": int(not paths),
            "parsed_images": parsed_images,
            "missing_images": len(missing_resources),
            "empty_images": empty_images,
            "static_records": len(entities),
        }
    }

    gaps = []
    if not paths:
        gaps.append(_gap("quest", "source_absent", 1, [{"resource": "Quest/QuestData/QuestData_*.wz"}]))
    if missing_resources:
        gaps.append(_gap("quest", "image_absent", len(missing_resources), [{"resource": resource} for resource in missing_resources]))
    if empty_images:
        gaps.append(_gap("quest", "source_present_empty", empty_images, empty_examples))

    return {
        "entities": entities,
        "relations": relations,
        "placements": [],
        "coverage": coverage,
        "gaps": gaps,
    }
