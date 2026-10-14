#!/usr/bin/env python3
"""Re-read the client's attack frame delays and compare them with the built-in table.

`cw_attack_speed.py` carries the frame delays the attack interval is computed
from. This script regenerates them from a client so the table can be checked
rather than trusted, and so a client update can be picked up deliberately.

The data lives at ``Character/Character_000.wz -> 00002000.img``; every action
is a list of frames carrying a ``delay`` in milliseconds. A weapon image carries
sprite placement only and no ``delay``, which is why looking there finds
nothing.

Read-only: the client is opened for reading and never written.

Exit codes: 0 when the client agrees with the built-in table (or a table was
written), 1 when they disagree, 2 when the client cannot be read.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

CW_RAG_SCRIPTS = SKILL_ROOT.parent.parent / "scripts"

DATA_RELATIVE = Path("Character") / "Character_000.wz"
BODY_IMAGE_MARKER = "00002000"

# Only the actions a plain melee attack uses. Ranged, magic and the
# borrowed-frame composites are out of scope and are reported as skipped.
MELEE_ACTIONS = (
    "swingO1", "swingO2", "swingO3", "stabO1", "stabO2",
    "swingT1", "swingT2", "swingT3", "stabT1", "stabT2",
    "swingP1", "swingP2", "swingPF", "stabTF", "swingTF", "swingOF", "stabOF",
)


def import_parser():
    """Load the vendored WZ reader from the cw-rag repository."""
    for candidate in (CW_RAG_SCRIPTS, Path(r"J:\PigeonYang\skills\cw-rag\scripts")):
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
        package = candidate / "vendor" / "wzpy"
        if package.is_dir():
            try:
                from vendor.wzpy.wz_file import WzFile  # type: ignore
            except Exception as error:  # pragma: no cover - environment specific
                raise SystemExit(
                    "无法加载 WZ 解析器（wzpy）: %s\n"
                    "请确认本 Skill 位于 cw-rag 仓库内，或用 --cw-rag-scripts 指定 scripts 目录。" % error
                )
            return WzFile
    raise SystemExit(
        "找不到 cw-rag 的 scripts 目录，无法加载 vendored wzpy。\n"
        "用 --cw-rag-scripts 指定它的路径。"
    )


def to_plain(node):
    """Convert a wzpy property tree into plain dicts and scalars."""
    children = None
    if hasattr(node, "children") and callable(getattr(node, "children")):
        children = node.children()
    if children:
        return {child.name: to_plain(child) for child in children}
    if hasattr(node, "value"):
        return node.value
    return None


def read_frame_delays(client_data: Path, wz_file_cls) -> tuple[dict[str, list[int]], list[str]]:
    """Return ({action: [delays]}, all_action_names) from the body image."""
    archive_path = client_data / DATA_RELATIVE
    if not archive_path.is_file():
        raise SystemExit("找不到客户端文件: %s" % archive_path)

    archive = wz_file_cls.open(str(archive_path), region="BMS", writable=False)
    try:
        target = None
        with archive:
            for internal, image in archive.root.walk_images():
                name = internal.replace("\\", "/")
                if BODY_IMAGE_MARKER in name:
                    target = image
                    break
            if target is None:
                raise SystemExit("在 %s 里找不到 %s.img" % (archive_path.name, BODY_IMAGE_MARKER))
            tree = to_plain(target.parse())
    finally:
        pass

    if not isinstance(tree, dict):
        raise SystemExit("身体图像解析结果不是属性树")

    delays: dict[str, list[int]] = {}
    for action in MELEE_ACTIONS:
        node = tree.get(action)
        if not isinstance(node, dict):
            continue
        frames = []
        for key in sorted(node.keys()):
            value = node[key]
            if isinstance(value, dict) and "delay" in value:
                frames.append(int(value["delay"]))
        if frames:
            delays[action] = frames
    return delays, sorted(tree.keys())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从 CW 客户端提取攻击动作的帧延迟，并与内置表对比。")
    parser.add_argument("--client", required=True, type=Path,
                        help="客户端 Data 目录（其下应有 Character\\Character_000.wz）")
    parser.add_argument("--write", type=Path, default=None,
                        help="把提取结果写成 JSON，便于比对或归档")
    parser.add_argument("--cw-rag-scripts", type=Path, default=None,
                        help="cw-rag 的 scripts 目录（含 vendored wzpy）")
    args = parser.parse_args()

    global CW_RAG_SCRIPTS
    if args.cw_rag_scripts is not None:
        CW_RAG_SCRIPTS = args.cw_rag_scripts

    wz_file_cls = import_parser()
    from cw_attack_speed import FRAME_DELAYS  # noqa: E402

    delays, all_actions = read_frame_delays(args.client, wz_file_cls)

    print("客户端: %s" % (args.client / DATA_RELATIVE))
    print("身体图像动作总数: %d" % len(all_actions))
    print()

    print("== 近战动作的逐帧 delay（毫秒）==")
    print("  %-10s %-28s %8s  %s" % ("动作", "每帧 delay", "合计", "与内置表一致?"))
    disagreements = []
    for action in sorted(delays):
        frames = delays[action]
        builtin = FRAME_DELAYS.get(action)
        if builtin is None:
            mark = "内置表缺此动作"
            disagreements.append(action)
        elif list(builtin) == frames:
            mark = "一致"
        else:
            mark = "不一致：内置 %s" % builtin
            disagreements.append(action)
        print("  %-10s %-28s %8d  %s" % (action, str(frames), sum(frames), mark))

    missing_in_client = sorted(set(FRAME_DELAYS) - set(delays))
    if missing_in_client:
        print()
        print("客户端里没有这些内置动作: %s" % "、".join(missing_in_client))

    if args.write is not None:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(json.dumps(
            {"actions": delays, "all_action_names": all_actions},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print()
        print("已写出: %s" % args.write)

    print()
    if disagreements:
        print("结果: 有 %d 处与内置表不一致：%s"
              % (len(disagreements), "、".join(disagreements)))
        print("请更新 scripts/cw_attack_speed.py 的 FRAME_DELAYS，然后重跑测试。")
        return 1
    print("结果: 客户端与内置帧延迟表完全一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
