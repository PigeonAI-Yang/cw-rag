from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any, Callable, Iterator


class SourceError(RuntimeError):
    pass


_KINDS = {"mob", "map", "equipment", "item", "skill", "npc", "quest", "set", "recipe"}
_SIGNED_INTEGER = re.compile(r"-?[0-9]+\Z", re.ASCII)
_SQLITE_MIN = -(2**63)
_SQLITE_MAX = 2**63 - 1


def _json_value(value: Any, address: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SourceError(f"Unsupported non-finite value at {address}")
        return value
    if isinstance(value, (tuple, list)):
        return [_json_value(child, f"{address}/{index}") for index, child in enumerate(value)]
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if not isinstance(key, (str, int)) or isinstance(key, bool):
                raise SourceError(f"Unsupported object key at {address}: {key!r}")
            name = str(key)
            result[name] = _json_value(child, f"{address}/{name}")
        return result
    raise SourceError(f"Unsupported value at {address}: {type(value).__name__}")


def _node_tree(node: Any, address: str) -> Any:
    type_name = getattr(node, "type_name", None)
    if type_name in ("Canvas", "Sound"):
        return _OMIT
    if type_name == "UOL":
        return {"$type": "UOL", "value": _json_value(node.value, address)}

    children = node.children()
    if children:
        result = {}
        for child in children:
            if not isinstance(child.name, str):
                raise SourceError(f"Unsupported property name at {address}: {child.name!r}")
            child_address = f"{address}/{child.name}"
            value = _node_tree(child, child_address)
            if value is not _OMIT:
                result[child.name] = value
        return result
    if type_name in ("Property", "SubProperty"):
        return {}
    return _json_value(node.value, address)


_OMIT = object()


def _resource_path(context: dict, path: Path) -> tuple[str, Path]:
    root = Path(context["data_root"])
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root.resolve(strict=True)).as_posix()
    except (OSError, ValueError) as error:
        raise SourceError(f"Image path is not a prepared source resource: {candidate}") from error

    prepared = {Path(item).resolve(strict=True) for item in context["files"]}
    if resolved not in prepared:
        raise SourceError(f"Image path is not a prepared source resource: {relative}")
    if relative not in context["file_hashes"]:
        raise SourceError(f"Prepared source hash is missing: {relative}")
    return relative, resolved


def _order(value: str) -> tuple[str, str]:
    return value.casefold(), value


def _wz_image_bytes(path: Path, image: Any, address: str) -> bytes:
    try:
        with path.open("rb") as stream:
            stream.seek(image.offset)
            raw = stream.read(image.size)
    except OSError as error:
        raise SourceError(f"Cannot read WZ image {address}: {error}") from error
    if len(raw) != image.size:
        raise SourceError(f"Incomplete WZ image bytes at {address}")
    return raw


def iter_images(context: dict, paths, accept: Callable[[str, str], bool]) -> Iterator[dict[str, Any]]:
    """Read selected WZ/MS images in stable order and retain their source hashes."""
    from vendor.wzpy.crypto import WzKey
    from vendor.wzpy.ms_file_v2 import MsFileV2
    from vendor.wzpy.wz_file import WzFile
    from vendor.wzpy.wz_image import WzImage

    selected = []
    for candidate in paths:
        relative, path = _resource_path(context, Path(candidate))
        selected.append((relative, path))
    selected.sort(key=lambda item: _order(item[0]))

    for relative, path in selected:
        file_sha256 = context["file_hashes"][relative]
        suffix = path.suffix.casefold()
        if suffix == ".wz":
            try:
                with path.open("rb") as stream:
                    magic = stream.read(4)
                if magic != b"PKG1":
                    raise SourceError(f"Unsupported WZ archive format in {relative}: expected PKG1")
                archive = WzFile.open(str(path), region="BMS", writable=False)
            except SourceError:
                raise
            except Exception as error:
                raise SourceError(f"Cannot open WZ archive {relative}: {error}") from error
            with archive:
                try:
                    images = [
                        (internal.replace("\\", "/"), image)
                        for internal, image in archive.root.walk_images()
                    ]
                except Exception as error:
                    raise SourceError(f"Cannot enumerate WZ archive {relative}: {error}") from error
                for internal, image in sorted(images, key=lambda item: _order(item[0])):
                    if not accept(relative, internal):
                        continue
                    address = f"{relative}/{internal}"
                    try:
                        root = image.parse()
                        if image.truncated or image.parse_warnings:
                            raise SourceError(f"Incomplete WZ image {address}: {image.parse_warnings}")
                        tree = _node_tree(root, address)
                        if tree is _OMIT:
                            tree = {}
                        raw = _wz_image_bytes(path, image, address)
                    except SourceError:
                        raise
                    except Exception as error:
                        raise SourceError(f"Cannot parse WZ image {address}: {error}") from error
                    if not isinstance(tree, dict):
                        raise SourceError(f"WZ image root is not a property tree at {address}")
                    yield {
                        "resource": relative,
                        "image": internal,
                        "tree": tree,
                        "file_sha256": file_sha256,
                        "image_sha256": hashlib.sha256(raw).hexdigest(),
                    }
            continue

        if suffix != ".ms":
            raise SourceError(f"Unsupported prepared source resource type: {relative}")
        try:
            pack = MsFileV2.open(str(path))
        except Exception as error:
            raise SourceError(f"Cannot open MS archive {relative}: {error}") from error
        with pack:
            entries = [(entry.name.replace("\\", "/"), entry) for entry in pack.entries]
            for internal, entry in sorted(entries, key=lambda item: _order(item[0])):
                if not accept(relative, internal):
                    continue
                address = f"{relative}/{internal}"
                try:
                    raw = pack.read_entry_data(entry)
                    image = WzImage.from_bytes(raw, key=WzKey.for_region("BMS"), name=internal)
                    root = image.parse()
                    if image.truncated or image.parse_warnings:
                        raise SourceError(f"Incomplete MS image {address}: {image.parse_warnings}")
                    tree = _node_tree(root, address)
                    if tree is _OMIT:
                        tree = {}
                except SourceError:
                    raise
                except Exception as error:
                    raise SourceError(f"Cannot parse MS image {address}: {error}") from error
                if not isinstance(tree, dict):
                    raise SourceError(f"MS image root is not a property tree at {address}")
                yield {
                    "resource": relative,
                    "image": internal,
                    "tree": tree,
                    "file_sha256": file_sha256,
                    "image_sha256": hashlib.sha256(raw).hexdigest(),
                }


def evidence(image: dict, *, role: str, node: str, source_id: str) -> dict[str, str]:
    return {
        "role": role,
        "resource": image["resource"],
        "image": image["image"],
        "node": node,
        "source_id": source_id,
        "file_sha256": image["file_sha256"],
        "image_sha256": image["image_sha256"],
    }


def _normalize_component(raw_id: str, kind: str, width: int, *, significant_limit: bool = False) -> str:
    if not isinstance(raw_id, str) or not raw_id or not raw_id.isascii() or not raw_id.isdigit():
        raise SourceError(f"Invalid {kind} ID: {raw_id!r}")
    significant = raw_id.lstrip("0") or "0"
    if (significant_limit and len(significant) > width) or (not significant_limit and len(raw_id) > width):
        raise SourceError(f"Invalid {kind} ID: {raw_id!r}")
    return significant


def normalize_id(raw_id: str, kind: str) -> str:
    if kind not in _KINDS:
        raise SourceError(f"Unsupported entity kind: {kind!r}")
    if kind == "recipe":
        parts = raw_id.split("/") if isinstance(raw_id, str) else []
        if len(parts) != 2:
            raise SourceError(f"Invalid recipe ID: {raw_id!r}")
        return "/".join(_normalize_component(part, kind, 20) for part in parts)
    if kind == "mob":
        return _normalize_component(raw_id, kind, 7, significant_limit=True)
    if kind == "map":
        return _normalize_component(raw_id, kind, 9, significant_limit=True)
    return _normalize_component(raw_id, kind, 20)


def flatten(value: Any, prefix: str = "") -> dict[str, object]:
    if isinstance(value, dict):
        return {
            path: leaf
            for key, child in value.items()
            for path, leaf in flatten(child, f"{prefix}{key}/").items()
        }
    if isinstance(value, (list, tuple)):
        return {
            path: leaf
            for index, child in enumerate(value)
            for path, leaf in flatten(child, f"{prefix}{index}/").items()
        }
    return {prefix.rstrip("/"): value}


def integer_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and _SIGNED_INTEGER.fullmatch(value):
        try:
            parsed = int(value)
        except ValueError:
            return None
    else:
        return None
    return parsed if _SQLITE_MIN <= parsed <= _SQLITE_MAX else None


def entity(kind: str, raw_id: str, fields: dict, sources: list, name: str | None = None) -> dict:
    if not isinstance(fields, dict):
        raise SourceError(f"Entity fields must be an object for {kind}/{raw_id}")
    if not isinstance(sources, list):
        raise SourceError(f"Entity sources must be a list for {kind}/{raw_id}")
    if name is not None and not isinstance(name, str):
        raise SourceError(f"Entity name must be a string for {kind}/{raw_id}")
    normalized_id = normalize_id(raw_id, kind)
    return {
        "kind": kind,
        "normalized_id": normalized_id,
        "original_id": raw_id,
        "name": name,
        "fields": dict(fields),
        "sources": list(sources),
    }
