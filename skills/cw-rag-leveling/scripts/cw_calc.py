#!/usr/bin/env python3
"""Rank Classic World monsters for a normal-attack leveling route.

Reads a CW RAG snapshot (catalog.sqlite) read-only and ranks monsters by
"experience per hit": exp / ceil(hp / damage_after_defence).

The player's damage is NOT present in the client files, so --damage is required.
There is deliberately no fallback estimate: a guessed damage value silently
produces a wrong ranking, and this tool refuses to do that.

Monster defence is applied with the inverse-proportional rule used by this
client:

    effective = damage * 100 / (PDDamage + 100)

so PDDamage 100 halves the hit and PDDamage 500 leaves a sixth of it. This rule
comes from the meowdb community guide captured in the maplestory-classic-server
research notes, NOT from a client listing, so it is labelled as a candidate and
can be switched off with --no-defence. A zero PDDamage makes the rule an
identity, which is why it is consistent with every zero-defence measurement so
far - that agreement does not confirm the rule.

Client files stay read-only. This tool only reads the saved snapshot.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

from cw_attack_speed import attack_interval_ms, family_for_item_id

DATABASE_NAME = "catalog.sqlite"
MAX_STATIC_LEVEL = 110
SKIPPED_NAME_MARKERS = ("Test(", "Super ", "Ultra ")
DEFENCE_SCALE = 100.0


def connect(data_dir: Path) -> sqlite3.Connection:
    database = data_dir / DATABASE_NAME
    if not database.is_file():
        raise SystemExit(
            "找不到快照: %s\n请先用 cw-rag 的 build 生成快照，或传入正确的 --data-dir。"
            % database
        )
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def snapshot_summary(connection: sqlite3.Connection) -> dict:
    values = {row["key"]: row["value"] for row in connection.execute(
        "select key, value from metadata")}
    return {
        "schema_version": values.get("schema_version"),
        "edition": values.get("edition"),
        "region": values.get("region"),
        "built_at": values.get("built_at"),
    }


def _static_info(fields_json: str) -> dict:
    payload = json.loads(fields_json or "{}")
    return {
        key.split("/")[-1]: value
        for key, value in (payload.get("static_info") or {}).items()
    }


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def load_monsters(connection: sqlite3.Connection) -> list[dict]:
    map_names = {row["normalized_id"]: row["name"] for row in connection.execute(
        "select normalized_id, name from entities where kind = 'map'")}

    monsters = []
    for row in connection.execute(
        "select normalized_id, name, level, hp, exp, physical_attack, fields_json "
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
            "group by map_id order by n desc",
            (row["normalized_id"],),
        ).fetchall()

        info = _static_info(row["fields_json"])

        monsters.append({
            "id": row["normalized_id"],
            "name": name,
            "level": level,
            "hp": row["hp"],
            "exp": row["exp"],
            "atk": row["physical_attack"],
            # 客户端的原始字段，不做任何解释。为 0 时实测表明面板伤害直接生效；
            # 不为 0 时实际伤害会低于面板，但换算方式客户端没有提供。
            "pdd_raw": _as_int(info.get("PDDamage")),
            "maps": [
                {"id": p["map_id"], "name": map_names.get(p["map_id"]) or "(未命名)", "count": p["n"]}
                for p in placements
            ],
        })
    return monsters


def damage_after_defence(damage: float, pdd: int | None) -> float:
    """按 CW 的反比例减伤规则折算实际伤害。

    effective = damage * 100 / (PDDamage + 100)

    PDDamage 为 0 或缺失时是恒等变换。规则本身是候选值（来自社区指南，
    不是客户端清单），所以调用方可关闭。
    """
    pdd_value = pdd or 0
    if pdd_value <= 0:
        return float(damage)
    return damage * DEFENCE_SCALE / (pdd_value + DEFENCE_SCALE)


def hits_needed(hp: int | None, damage: float) -> int:
    if not hp or hp <= 0:
        return 1
    if damage <= 0:
        return hp
    return max(1, math.ceil(hp / damage))


def rank(monsters: list[dict], damage: int, min_nodes: int, max_hits: int,
         min_level: int, max_level: int, use_defence: bool = True,
         weapon_id: str | None = None, speed_tag: int | None = None,
         max_seconds: float | None = None) -> list[dict]:
    """排序怪物。

    给了 weapon_id 与 speed_tag 时，额外算出「击杀秒数」与「每秒经验」，
    并以每秒经验为主排序——那才是玩家真正的时间成本。
    """
    interval_ms = None
    if weapon_id and speed_tag:
        interval_ms = attack_interval_ms(weapon_id, speed_tag)

    ranked = []
    for monster in monsters:
        if monster["level"] > max_level or monster["level"] < min_level:
            continue
        if not monster["exp"]:
            continue
        maps = [m for m in monster["maps"] if m["count"] >= min_nodes]
        if not maps:
            continue

        pdd = monster.get("pdd_raw") if use_defence else None
        effective = damage_after_defence(damage, pdd)
        hits = hits_needed(monster["hp"], effective)
        if hits > max_hits:
            continue

        row = {
            **monster,
            "effective_damage": round(effective, 2),
            "defence_applied": bool(use_defence and (pdd or 0) > 0),
            "hits": hits,
            "exp_per_hit": monster["exp"] / hits,
            "best_maps": sorted(maps, key=lambda m: (-m["count"], m["name"])),
        }

        if interval_ms:
            seconds = hits * interval_ms / 1000.0
            if max_seconds is not None and seconds > max_seconds:
                continue
            row["attack_interval_ms"] = round(interval_ms, 1)
            row["seconds_per_kill"] = round(seconds, 2)
            row["exp_per_second"] = round(monster["exp"] / seconds, 3)
        ranked.append(row)

    # 主排序：有秒数就按每秒经验，否则退回经验/刀。
    # 并列时优先物防 0 的（无减伤不确定性），再按等级与名字。
    if interval_ms:
        ranked.sort(key=lambda m: (-m["exp_per_second"], m.get("pdd_raw") or 0,
                                   m["level"], m["name"]))
    else:
        ranked.sort(key=lambda m: (-m["exp_per_hit"], m.get("pdd_raw") or 0,
                                   m["level"], m["name"]))
    return ranked


def rank_with_fallback(monsters: list[dict], damage: int, min_nodes: int,
                       max_hits: int, min_level: int, max_level: int,
                       use_defence: bool = True, weapon_id: str | None = None,
                       speed_tag: int | None = None,
                       max_seconds: float | None = None) -> tuple[list[dict], str | None]:
    """排序；若条件过严导致空结果，逐级放宽并返回放宽说明。

    放宽顺序：最多几刀/几秒 → 地图密度 → 等级上限。永不返回空列表（除非快照本身没有怪）。
    """
    options = dict(use_defence=use_defence, weapon_id=weapon_id,
                   speed_tag=speed_tag, max_seconds=max_seconds)
    ranked = rank(monsters, damage, min_nodes, max_hits, min_level, max_level, **options)
    if ranked:
        return ranked, None

    attempts = [
        (min_nodes, 999, None, "已忽略「最多几刀/几秒」限制"),
        (1, 999, None, "已忽略「最多几刀/几秒」和「地图密度」限制"),
        (1, 999, None, "已忽略全部筛选，仅按等级范围"),
    ]
    for nodes, hits, seconds, note in attempts:
        relaxed_options = dict(options)
        relaxed_options["max_seconds"] = seconds
        ranked = rank(monsters, damage, nodes, hits, min_level, max_level, **relaxed_options)
        if ranked:
            return ranked, note

    ranked = rank(monsters, damage, 1, 999, 1, max_level, **options)
    if ranked:
        return ranked, "已忽略「等级下限」，因为该范围内没有怪"
    ranked = rank(monsters, damage, 1, 999, 1, MAX_STATIC_LEVEL, **options)
    if ranked:
        return ranked, "已忽略「等级上限」，因为原范围内没有怪"
    return ranked, None


def format_maps(maps: list[dict], limit: int = 2) -> str:
    return "、".join("%s×%d" % (m["name"], m["count"]) for m in maps[:limit])


def defence_note(monsters: list[dict], use_defence: bool) -> str:
    """说明防御规则是否启用，以及列表里有无受它影响的怪。"""
    if not use_defence:
        return ("未应用减伤：刀数按你给的原始伤害算。若目标物防不是 0，"
                "实际需要更多刀。")
    affected = [m for m in monsters if m.get("defence_applied")]
    if not affected:
        return "本页所有怪的物防字段都是 0，减伤未生效，等同按原始伤害计算。"
    worst = sorted(affected, key=lambda m: m["effective_damage"])[:4]
    listed = "、".join("%s(物防%s→每刀%.0f)" % (m["name"], m["pdd_raw"], m["effective_damage"])
                      for m in worst)
    return ("已按「× 100 ÷ (物防+100)」折算：%d 只怪受影响（如 %s）。"
            "该规则来自社区指南而非客户端清单，可用 --no-defence 关闭或按下表实测校验。"
            % (len(affected), listed))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按「经验/刀」为 CW 普通攻击练级路线排序怪物。")
    parser.add_argument("--data-dir", required=True, type=Path,
                        help="CW RAG 快照目录（含 catalog.sqlite）")
    parser.add_argument("--damage", required=True, type=int,
                        help="游戏内实测的单刀伤害（必填；客户端无伤害公式，不能估算）")
    parser.add_argument("--level", type=int, default=None,
                        help="角色等级；用于推算等级下限")
    parser.add_argument("--level-gap", type=int, default=15,
                        help="只考虑不低于（角色等级 − 此值）的怪，默认 15")
    parser.add_argument("--min-level", type=int, default=None)
    parser.add_argument("--max-level", type=int, default=None)
    parser.add_argument("--min-nodes", type=int, default=8,
                        help="单张地图至少要有多少只这种怪，默认 8")
    parser.add_argument("--max-hits", type=int, default=3,
                        help="最多几刀能杀，默认 3")
    parser.add_argument("--speed-tag", type=int, default=None,
                        help="武器攻速标签（1..8），配合 --weapon 输出击杀秒数与每秒经验")
    parser.add_argument("--weapon", type=str, default=None,
                        help="武器 ID（如 1302018），用于按客户端帧延迟算真实攻击间隔")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="最多几秒能杀（需 --weapon 与 --speed-tag）")
    parser.add_argument("--atk-cap", type=int, default=None,
                        help="只考虑攻击力不高于此值的怪（更安全）")
    parser.add_argument("--max-pdd", type=int, default=None,
                        help="只考虑物防字段不高于此值的怪（--max-pdd 0 即只打无减伤的）")
    parser.add_argument("--no-defence", action="store_true",
                        help="关闭减伤折算，刀数按原始伤害计算")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    if args.damage <= 0:
        parser.error("--damage 必须是正整数")

    if (args.weapon is None) != (args.speed_tag is None):
        parser.error("--weapon 与 --speed-tag 必须同时提供，才能计算真实攻击间隔")
    if args.weapon is not None and family_for_item_id(args.weapon) is None:
        parser.error("--weapon %s 不是本工具覆盖的近战武器类别" % args.weapon)

    damage = args.damage
    min_level = args.min_level
    if min_level is None:
        min_level = max(1, (args.level or 1) - args.level_gap)
    max_level = args.max_level if args.max_level is not None else MAX_STATIC_LEVEL

    connection = connect(args.data_dir)
    try:
        monsters = load_monsters(connection)
        summary = snapshot_summary(connection)
    finally:
        connection.close()

    ranked, relaxed = rank_with_fallback(
        monsters, damage, args.min_nodes, args.max_hits, min_level, max_level,
        use_defence=not args.no_defence, weapon_id=args.weapon,
        speed_tag=args.speed_tag, max_seconds=args.max_seconds)
    if args.atk_cap is not None:
        ranked = [m for m in ranked if (m["atk"] or 0) <= args.atk_cap]
    if args.max_pdd is not None:
        ranked = [m for m in ranked if (m["pdd_raw"] or 0) <= args.max_pdd]

    interval_ms = attack_interval_ms(args.weapon, args.speed_tag) if args.weapon else None

    if args.json:
        print(json.dumps({
            "snapshot": summary,
            "damage": damage,
            "damage_source": "玩家实测 %d" % damage,
            "defence_applied": not args.no_defence,
            "weapon_id": args.weapon,
            "speed_tag": args.speed_tag,
            "attack_interval_ms": round(interval_ms, 1) if interval_ms else None,
            "filters": {
                "min_level": min_level,
                "max_level": max_level,
                "min_nodes": args.min_nodes,
                "max_hits": args.max_hits,
                "max_seconds": args.max_seconds,
                "atk_cap": args.atk_cap,
                "max_pdd": args.max_pdd,
            },
            "relaxed": relaxed,
            "total": len(ranked),
            "monsters": ranked[:args.top],
        }, ensure_ascii=False, indent=2))
        return 0

    print("快照: %s / %s / schema %s（构建于 %s）"
          % (summary.get("edition"), summary.get("region"),
             summary.get("schema_version"), summary.get("built_at")))
    print("单刀伤害: 玩家实测 %d%s"
          % (damage, "（已按物防折算）" if not args.no_defence else "（未折算物防）"))
    if interval_ms:
        print("攻击间隔: 武器 %s 攻速 %s → %.0f ms/刀（客户端帧延迟算得）"
              % (args.weapon, args.speed_tag, interval_ms))
        print("排序依据: 每秒经验 = 每只经验 ÷ 击杀秒数")
    else:
        print("排序依据: 经验/刀（未提供 --weapon 与 --speed-tag，无法折算时间）")
    print("筛选: 等级 %s–%s，单图至少 %s 只，最多 %s 刀"
          % (min_level, max_level, args.min_nodes, args.max_hits))
    if relaxed:
        print("已自动放宽: %s" % relaxed)
    print()

    if not ranked:
        print("没有符合条件的怪。放宽 --max-hits / --max-seconds / --min-nodes，或调高 --max-level。")
        return 0

    within_two = [m for m in ranked if m["hits"] <= 2]
    if within_two:
        print("== 能一两刀砍死的怪 ==")
        for m in within_two[:args.top]:
            extra = ("  约 %.1f 秒/只" % m["seconds_per_kill"]) if interval_ms else ""
            print("  %-20s Lv%-4s %s血 %d刀 每只经验=%-5s%s  %s"
                  % (m["name"], m["level"], m["hp"], m["hits"], m["exp"], extra,
                     format_maps(m["best_maps"])))
    else:
        best = ranked[0]
        print("== 没有能一两刀砍死的怪（数值结构所致，不是装备问题）==")
        print("   最快也要 %d 刀（%s）。改用收益最高者。" % (best["hits"], best["name"]))
    print()

    if interval_ms:
        print("== 按每秒经验排序（前 %d 名）==" % min(args.top, len(ranked)))
        print("  %-20s %5s %8s %5s %8s %9s %10s %6s  %s"
              % ("怪名", "等级", "血量", "几刀", "击杀秒", "每秒经验", "每只经验", "物防", "推荐地图"))
        for m in ranked[:args.top]:
            pdd = m["pdd_raw"]
            print("  %-20s %5s %8s %5s %8.1f %9.3f %10s %6s  %s"
                  % (m["name"], m["level"], m["hp"], m["hits"], m["seconds_per_kill"],
                     m["exp_per_second"], m["exp"], "—" if pdd is None else pdd,
                     format_maps(m["best_maps"])))
    else:
        print("== 按经验/刀排序（前 %d 名）==" % min(args.top, len(ranked)))
        print("  %-20s %5s %8s %5s %7s %9s %8s %6s  %s"
              % ("怪名", "等级", "血量", "几刀", "每刀伤害", "经验/刀", "每只经验", "物防", "推荐地图"))
        for m in ranked[:args.top]:
            pdd = m["pdd_raw"]
            print("  %-20s %5s %8s %5s %7.1f %9.0f %8s %6s  %s"
                  % (m["name"], m["level"], m["hp"], m["hits"], m["effective_damage"],
                     m["exp_per_hit"], m["exp"], "—" if pdd is None else pdd,
                     format_maps(m["best_maps"])))
    print()
    print(defence_note(ranked[:args.top], use_defence=not args.no_defence))
    return 0


if __name__ == "__main__":
    sys.exit(main())
