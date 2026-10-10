from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from cw_common import entity, evidence, iter_images, normalize_id


def _skill_pack(relative_resource: str) -> bool:
    parts = PurePosixPath(relative_resource.replace("\\", "/")).parts
    if len(parts) != 2 or parts[0].casefold() != "packs":
        return False
    filename = parts[1].casefold()
    return filename.startswith("skill_") and filename.endswith(".ms")


def _numeric_skill_image(internal_image: str) -> bool:
    parts = PurePosixPath(internal_image.replace("\\", "/")).parts
    if len(parts) != 2 or parts[0] != "Skill":
        return False
    image_path = PurePosixPath(parts[1])
    return image_path.suffix.casefold() == ".img" and _is_ascii_digits(image_path.stem)


def _is_ascii_digits(value: str) -> bool:
    return bool(value) and value.isascii() and value.isdigit()


def _selected_skill_packs(context: dict[str, Any]) -> list[Path]:
    root = Path(context["data_root"])
    selected = []
    for candidate in context["files"]:
        path = Path(candidate)
        if _skill_pack(path.relative_to(root).as_posix()):
            selected.append(path)
    return selected


def _skill_book(context: dict[str, Any], normalized_skill_id: str, normalized_group: str) -> dict[str, str] | None:
    names = context.get("names", {})
    name_record = names.get(("skill", normalized_skill_id))
    if not isinstance(name_record, dict):
        return None
    name_info = name_record.get("name_info")
    if not isinstance(name_info, dict) or not isinstance(name_info.get("bookName"), str):
        return None
    return {"group": normalized_group, "title": name_info["bookName"]}


def extract(context: dict[str, Any]) -> dict[str, Any]:
    """Extract raw skill definitions and their literal prerequisite references."""
    skill_packs = _selected_skill_packs(context)
    entities: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    parsed_images = 0
    images_by_resource: dict[str, int] = {}
    empty_images: list[dict[str, str]] = []

    for image in iter_images(context, skill_packs, lambda resource, internal: _skill_pack(resource) and _numeric_skill_image(internal)):
        parsed_images += 1
        images_by_resource[image["resource"]] = images_by_resource.get(image["resource"], 0) + 1
        image_path = PurePosixPath(image["image"].replace("\\", "/"))
        raw_group = image_path.stem
        normalized_group = normalize_id(raw_group, "skill")
        skills = image["tree"].get("skill")
        if not isinstance(skills, dict):
            empty_images.append(evidence(image, role="static", node="", source_id=raw_group))
            continue

        image_record_count = 0
        for raw_skill_id, skill_tree in skills.items():
            if not isinstance(raw_skill_id, str) or not _is_ascii_digits(raw_skill_id):
                continue
            normalized_skill_id = normalize_id(raw_skill_id, "skill")
            node_path = f"skill/{raw_skill_id}"
            source = evidence(image, role="static", node=node_path, source_id=raw_skill_id)
            fields: dict[str, Any] = {
                "skill_static": skill_tree,
                "skill_resource_group": normalized_group,
                "raw": skill_tree,
            }
            book = _skill_book(context, normalized_skill_id, normalized_group)
            if book is not None:
                fields["skill_book"] = book
            entities.append(entity("skill", raw_skill_id, fields, [source]))
            image_record_count += 1

            requirements = skill_tree.get("req") if isinstance(skill_tree, dict) else None
            if not isinstance(requirements, dict):
                continue
            for target_raw_id, required_level in requirements.items():
                if not isinstance(target_raw_id, str) or not _is_ascii_digits(target_raw_id):
                    continue
                target_id = normalize_id(target_raw_id, "skill")
                requirement_path = f"{node_path}/req/{target_raw_id}"
                relation_source = evidence(image, role="static", node=requirement_path, source_id=raw_skill_id)
                relations.append({
                    "source_kind": "skill",
                    "source_id": normalized_skill_id,
                    "relation": "skill_req",
                    "target_kind": "skill",
                    "target_id": target_id,
                    "node_path": requirement_path,
                    "raw": {"required_level": required_level},
                    "sources": [relation_source],
                })

        if image_record_count == 0:
            empty_images.append(evidence(image, role="static", node="", source_id=raw_group))

    gaps: list[dict[str, Any]] = []
    if not skill_packs:
        gaps.append({"kind": "skill", "category": "source_absent", "count": 1, "examples": []})
    else:
        images_absent = [path.relative_to(Path(context["data_root"])).as_posix() for path in skill_packs if path.relative_to(Path(context["data_root"])).as_posix() not in images_by_resource]
        if images_absent:
            gaps.append({
                "kind": "skill",
                "category": "image_absent",
                "count": len(images_absent),
                "examples": [{"resource": resource, "file_sha256": context["file_hashes"][resource]} for resource in images_absent[:25]],
                "truncated": len(images_absent) > 25,
            })
        if empty_images:
            gaps.append({
                "kind": "skill",
                "category": "source_present_empty",
                "count": len(empty_images),
                "examples": empty_images[:25],
                "truncated": len(empty_images) > 25,
            })

    return {
        "entities": entities,
        "relations": relations,
        "placements": [],
        "coverage": {
            "skill": {
                "selected_file_count": len(skill_packs),
                "parsed_image_count": parsed_images,
                "static_record_count": len(entities),
            }
        },
        "gaps": gaps,
    }
