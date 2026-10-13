#!/usr/bin/env python3
"""Export mobs.js and gear.js for the CW leveling web calculator.

Reads a CW RAG snapshot read-only and writes the two JavaScript data files that
web/cw-calculator.html loads. Client files are never touched.

Only fields with a settled meaning are exported: level, HP, EXP, physical
attack, and static spawn counts. Monster defence/evasion fields are deliberately
left out because their meaning is not established - see the Skill notes.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

DATABASE_NAME = "catalog.sqlite"
MAX_STATIC_LEVEL = 110
SKIPPED_NAME_MARKERS = ("Test(", "Super ", "Ultra ")
WARRIOR_REQ_JOB = 1
MAX_GEAR_LEVEL = 70
ARMOR_SLOTS = ("Cap", "Longcoat", "Shoes", "Glove", "Cape")


def connect(data_dir: Path) -> sqlite3.Connection:
    database = data_dir / DATABASE_NAME
    if not database.is_file():
        raise SystemExit("找不到快照: %s" % database)
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def static_info(fields_json: str) -> dict:
    payload = json.loads(fields_json or "{}")
    out = {}
    for key, value in (payload.get("static_info") or {}).items():
        out[key.split("/")[-1]] = value
    return out


def build_mobs(connection: sqlite3.Connection) -> list[dict]:
    map_names = {row["normalized_id"]: row["name"] for row in connection.execute(
        "select normalized_id, name from entities where kind = 'map'")}

    mobs = []
    for row in connection.execute(
        "select normalized_id, name, level, hp, exp, physical_attack "
        "from entities where kind = 'mob'"
    ):
        name = row["name"] or ""
        level = row["level"]
        if not level or level > MAX_STATIC_LEVEL:
            continue
        if any(marker in name for marker in SKIPPED_NAME_MARKERS):
            continue

        placements = connection.execute(
            "select map_id, count(*) as n from placements where mob_id = ? "
            "group by map_id having n > 0 order by n desc",
            (row["normalized_id"],),
        ).fetchall()

        mobs.append({
            "id": row["normalized_id"],
            "name": name,
            "level": level,
            "hp": row["hp"],
            "exp": row["exp"],
            "atk": row["physical_attack"],
            "maps": [
                {"name": map_names.get(p["map_id"]) or "(未命名)", "count": p["n"]}
                for p in placements
            ],
        })
    mobs.sort(key=lambda m: m["level"])
    return mobs


def build_gear(connection: sqlite3.Connection) -> dict:
    weapons, shields = [], []
    armor: dict[str, dict[int, dict]] = {slot: {} for slot in ARMOR_SLOTS}

    for row in connection.execute(
        "select normalized_id, name, family, req_level, req_job, fields_json "
        "from entities where kind = 'equipment'"
    ):
        info = static_info(row["fields_json"])
        level = row["req_level"]
        family = row["family"]

        if (family == "Weapon" and row["req_job"] == WARRIOR_REQ_JOB
                and level and level <= MAX_GEAR_LEVEL):
            weapons.append({
                "id": row["normalized_id"],
                "name": row["name"],
                "lv": level,
                "atk": info.get("incWAT") or info.get("incPAD") or 0,
                "spd": info.get("attackSpeed"),
                "str": info.get("reqSTR"),
                "two": info.get("islot") == "WpSi",
            })
        elif family == "Shield" and level is not None and level <= MAX_GEAR_LEVEL:
            shields.append({
                "id": row["normalized_id"],
                "name": row["name"],
                "lv": level,
                "pdd": info.get("incPDD") or 0,
                "str": info.get("reqSTR"),
                "mhp": info.get("incMHP") or 0,
            })
        elif family in ARMOR_SLOTS and level is not None and level <= MAX_GEAR_LEVEL:
            pdd = int(info.get("incPDD") or 0)
            eva = int(info.get("incEVA") or 0)
            current = armor[family].get(level)
            # 回避按 1 点折合 5 点防御排序，仅用于在同期候选中选一件
            score = pdd + eva * 5
            if current is None or score > current["_score"]:
                armor[family][level] = {
                    "id": row["normalized_id"],
                    "name": row["name"],
                    "lv": level,
                    "pdd": pdd,
                    "eva": eva,
                    "mhp": int(info.get("incMHP") or 0),
                    "_score": score,
                }

    weapons.sort(key=lambda w: (w["lv"], w["atk"]))
    shields.sort(key=lambda s: s["lv"])
    cleaned = {}
    for slot, by_level in armor.items():
        cleaned[slot] = [
            {k: v for k, v in item.items() if k != "_score"}
            for _, item in sorted(by_level.items())
        ]

    return {"weapons": weapons, "shields": shields, "armor": cleaned}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从 CW RAG 快照导出网页计算器需要的数据文件。")
    parser.add_argument("--data-dir", required=True, type=Path,
                        help="CW RAG 快照目录（含 catalog.sqlite）")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="输出目录，默认写在本 Skill 的 web 目录")
    args = parser.parse_args()

    out_dir = args.out_dir
    if out_dir is None:
        out_dir = Path(__file__).resolve().parent.parent / "web"
    out_dir.mkdir(parents=True, exist_ok=True)

    connection = connect(args.data_dir)
    try:
        mobs = build_mobs(connection)
        gear = build_gear(connection)
    finally:
        connection.close()

    mobs_js = out_dir / "mobs.js"
    gear_js = out_dir / "gear.js"
    mobs_js.write_text(
        "const MOBS = " + json.dumps(mobs, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8")
    gear_js.write_text(
        "const GEAR = " + json.dumps(gear, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8")

    print("已写出 %s（%d 只怪）" % (mobs_js, len(mobs)))
    print("已写出 %s（武器 %d，盾 %d，防具槽 %s）"
          % (gear_js, len(gear["weapons"]), len(gear["shields"]),
             "、".join(gear["armor"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
