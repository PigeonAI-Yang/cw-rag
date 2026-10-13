from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import cw_rag

LEVELING_ROOT = REPOSITORY_ROOT / "skills" / "cw-rag-leveling"
EXPORT_SCRIPT = LEVELING_ROOT / "scripts" / "export_web_data.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load module at %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# cw_calc.py 用连字符以外的合法模块名加载，避免与 scripts/ 下其它模块重名
cw_calc = load_module(LEVELING_ROOT / "scripts" / "cw_calc.py", "cw_calc_module")
export_web_data = load_module(EXPORT_SCRIPT, "export_web_data_module")


def evidence(index: int) -> dict:
    return {
        "file_sha256": "%064x" % index,
        "image": "Mob.img",
        "image_sha256": "%064x" % (index + 1000),
        "node": str(index),
        "resource": "String/String_000.wz",
        "role": "name",
        "source_id": str(index),
    }


class LevelingFixture(unittest.TestCase):
    """合成快照：五只正常怪 + 两类必须跳过的怪，覆盖练级排序的主要分支。

    刻意包含：同名但不同 ID 的怪（验证按 ID 而不是按名字区分）。
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.data_dir = self.root / "index"
        self.data_dir.mkdir()
        self.database = self.data_dir / "catalog.sqlite"
        self.write_catalog(self.data_dir, self.snapshot())
        self._open_connections: list = []

    def tearDown(self) -> None:
        for connection in self._open_connections:
            connection.close()
        self.temporary_directory.cleanup()

    def open_snapshot(self, data_dir: Path | None = None) -> list[dict]:
        """打开快照并登记连接，确保 tearDown 时关闭（Windows 上否则删不掉文件）。"""
        connection = cw_calc.connect(data_dir or self.data_dir)
        self._open_connections.append(connection)
        return cw_calc.load_monsters(connection)

    @staticmethod
    def snapshot(monsters=None) -> dict:
        if monsters is None:
            monsters = [
                # (id, name, level, hp, exp, attack, PDDamage)
                ("100", "Snail", 1, 40, 2, 3, 0),
                ("200", "Slime", 6, 100, 12, 13, 10),
                ("300", "Jr. Necki", 21, 300, 39, 92, 0),
                # 同名不同 ID：验证不按名字合并
                ("800300", "Jr. Necki", 25, 900, 80, 139, 0),
                # 高级厚血怪：用来触发「无法一两刀」
                ("999", "Drake", 50, 4000, 133, 287, 15),
                # 高物防怪：验证会被告警而不是被算进刀数
                ("888", "Iron Hog", 28, 500, 52, 136, 500),
                # 测试怪：必须被跳过
                ("9990000", "Test(Basic)", 30, 2100000000, None, 166, 0),
                # 超怪：必须被跳过
                ("91", "Super Slime", 200, 9999, 0, 1, 0),
            ]
        entities = []
        for index, (mob_id, name, level, hp, exp, attack, pdd) in enumerate(monsters):
            # 列投影由 cw_rag._numeric_value 从 fields["static_info"]["info/<name>"]
            # 读取，字段名为 level / maxHP / exp / PADamage。
            static_info = {
                "info/level": str(level),
                "info/maxHP": str(hp),
                "info/PADamage": str(attack),
                "info/PDDamage": str(pdd),
            }
            if exp is not None:
                static_info["info/exp"] = str(exp)
            entities.append({
                "kind": "mob",
                "normalized_id": mob_id,
                "original_id": mob_id,
                "name": name,
                "fields": {"name_info": {"desc": name}, "static_info": static_info},
                "sources": [evidence(index)],
            })
        # 两张地图：一张够密，一张不够密
        entities.append({
            "kind": "map", "normalized_id": "1000", "original_id": "0000001000",
            "name": "Dense Field", "fields": {}, "sources": [evidence(50)],
        })
        entities.append({
            "kind": "map", "normalized_id": "2000", "original_id": "0000002000",
            "name": "Sparse Field", "fields": {}, "sources": [evidence(51)],
        })

        placements = []
        for node in range(10):
            placements.append({"map_id": "1000", "node_id": str(node), "mob_id": "100",
                               "x": None, "y": None, "raw_json": "{\"id\":\"100\"}"})
        for node in range(10):
            placements.append({"map_id": "1000", "node_id": str(100 + node), "mob_id": "200",
                               "x": None, "y": None, "raw_json": "{\"id\":\"200\"}"})
        for node in range(10):
            placements.append({"map_id": "1000", "node_id": str(200 + node), "mob_id": "999",
                               "x": None, "y": None, "raw_json": "{\"id\":\"999\"}"})
        # 高物防怪也要有刷怪点，才能进入排序
        for node in range(8):
            placements.append({"map_id": "1000", "node_id": str(300 + node), "mob_id": "888",
                               "x": None, "y": None, "raw_json": "{\"id\":\"888\"}"})
        # 只有 1 只，测 --min-nodes
        placements.append({"map_id": "2000", "node_id": "0", "mob_id": "300",
                           "x": None, "y": None, "raw_json": "{\"id\":\"300\"}"})

        # schema 2 要求每个 placement 都有对应的 map_mob 关系，否则校验不通过
        relations = [
            {
                "source_kind": "map", "source_id": p["map_id"], "relation": "map_mob",
                "target_kind": "mob", "target_id": p["mob_id"],
                "node_path": "life/%s" % p["node_id"],
                "raw": {"id": p["mob_id"], "type": "m"},
                "sources": [evidence(int(p["node_id"]) % 40)],
            }
            for p in placements
        ]

        counts = {"equipment": 0, "item": 0, "map": 2, "mob": len(monsters),
                  "npc": 0, "quest": 0, "recipe": 0, "set": 0, "skill": 0}
        return {
            "schema_version": "2", "edition": "classic-world", "region": "GLOBAL", "locale": "en",
            "source_fingerprint": "a" * 64, "parser_revision": "b" * 64,
            "built_at": "2026-10-10T00:00:00Z",
            "source_files": [], "entity_counts": counts, "placement_count": len(placements),
            "coverage": {"entities": counts, "gaps": []},
            "entities": entities, "placements": placements, "relations": relations,
        }

    @staticmethod
    def write_catalog(data_dir: Path, snapshot: dict) -> Path:
        data_dir.mkdir(parents=True, exist_ok=True)
        database = data_dir / "catalog.sqlite"
        temporary = data_dir / ".leveling-fixture.tmp"
        cw_rag._write_database(database, temporary, snapshot)
        temporary.replace(database)
        return database

    def run_calc(self, *args):
        """以进程内方式运行 cw_calc.main，返回 (exit_code, stdout)。"""
        buffer = io.StringIO()
        previous = sys.argv
        sys.argv = ["cw_calc.py", *[str(a) for a in args]]
        try:
            with redirect_stdout(buffer):
                code = cw_calc.main()
        finally:
            sys.argv = previous
        return code, buffer.getvalue()

    def run_calc_json(self, *args) -> dict:
        code, out = self.run_calc(*args, "--json")
        self.assertEqual(code, 0)
        return json.loads(out)


class TestHitsNeeded(unittest.TestCase):
    def test_one_hit_when_damage_exceeds_hp(self) -> None:
        self.assertEqual(cw_calc.hits_needed(100, 250), 1)

    def test_two_hits_when_damage_is_half(self) -> None:
        self.assertEqual(cw_calc.hits_needed(100, 50), 2)

    def test_rounds_up_on_remainder(self) -> None:
        self.assertEqual(cw_calc.hits_needed(101, 50), 3)

    def test_missing_hp_is_one_hit(self) -> None:
        self.assertEqual(cw_calc.hits_needed(None, 10), 1)
        self.assertEqual(cw_calc.hits_needed(0, 10), 1)


class TestRanking(LevelingFixture):
    def test_ranks_by_experience_per_hit_descending(self) -> None:
        monsters = [
            {"id": "a", "name": "A", "level": 10, "hp": 100, "exp": 50, "atk": 1,
             "maps": [{"id": "m", "name": "M", "count": 10}]},
            {"id": "b", "name": "B", "level": 10, "hp": 100, "exp": 30, "atk": 1,
             "maps": [{"id": "m", "name": "M", "count": 10}]},
        ]
        ranked = cw_calc.rank(monsters, damage=50, min_nodes=1, max_hits=99,
                              min_level=1, max_level=100)
        self.assertEqual([m["id"] for m in ranked], ["a", "b"])
        self.assertEqual(ranked[0]["hits"], 2)
        self.assertEqual(ranked[0]["exp_per_hit"], 25)

    def test_filters_by_max_hits(self) -> None:
        monsters = [
            {"id": "easy", "name": "Easy", "level": 10, "hp": 50, "exp": 10, "atk": 1,
             "maps": [{"id": "m", "name": "M", "count": 10}]},
            {"id": "hard", "name": "Hard", "level": 10, "hp": 5000, "exp": 900, "atk": 1,
             "maps": [{"id": "m", "name": "M", "count": 10}]},
        ]
        ranked = cw_calc.rank(monsters, damage=100, min_nodes=1, max_hits=1,
                              min_level=1, max_level=100)
        self.assertEqual([m["id"] for m in ranked], ["easy"])

    def test_filters_by_min_nodes(self) -> None:
        monsters = [{"id": "a", "name": "A", "level": 10, "hp": 50, "exp": 10, "atk": 1,
                     "maps": [{"id": "m", "name": "M", "count": 3}]}]
        self.assertEqual(
            cw_calc.rank(monsters, 100, min_nodes=5, max_hits=9, min_level=1, max_level=100), [])
        self.assertEqual(
            len(cw_calc.rank(monsters, 100, min_nodes=3, max_hits=9, min_level=1, max_level=100)), 1)

    def test_filters_by_level_range(self) -> None:
        monsters = [{"id": "a", "name": "A", "level": 40, "hp": 50, "exp": 10, "atk": 1,
                     "maps": [{"id": "m", "name": "M", "count": 10}]}]
        self.assertEqual(
            cw_calc.rank(monsters, 100, 1, 9, min_level=1, max_level=30), [])
        self.assertEqual(
            len(cw_calc.rank(monsters, 100, 1, 9, min_level=1, max_level=40)), 1)

    def test_zero_exp_monsters_are_excluded(self) -> None:
        monsters = [{"id": "a", "name": "A", "level": 10, "hp": 50, "exp": 0, "atk": 1,
                     "maps": [{"id": "m", "name": "M", "count": 10}]}]
        self.assertEqual(cw_calc.rank(monsters, 100, 1, 9, 1, 100), [])


class TestFallbackNeverEmpty(LevelingFixture):
    def test_impossible_filters_still_return_rows(self) -> None:
        monsters = self.open_snapshot()
        ranked, relaxed = cw_calc.rank_with_fallback(
            monsters, damage=1, min_nodes=999, max_hits=1, min_level=1, max_level=100)
        self.assertTrue(ranked, "放宽后仍应有结果")
        self.assertIsNotNone(relaxed, "应说明放宽了什么")

    def test_empty_catalog_returns_empty_without_relaxation(self) -> None:
        empty_dir = self.root / "empty"
        self.write_catalog(empty_dir, self.snapshot(monsters=[]))
        monsters = self.open_snapshot(empty_dir)
        ranked, relaxed = cw_calc.rank_with_fallback(monsters, 100, 1, 9, 1, 100)
        self.assertEqual(ranked, [])
        self.assertIsNone(relaxed)


class TestSnapshotReading(LevelingFixture):
    def test_skips_test_and_super_monsters(self) -> None:
        monsters = self.open_snapshot()
        names = {m["name"] for m in monsters}
        self.assertNotIn("Test(Basic)", names)
        self.assertNotIn("Super Slime", names)
        self.assertIn("Snail", names)

    def test_same_name_monsters_stay_separate_rows(self) -> None:
        monsters = self.open_snapshot()
        necki = sorted(m["id"] for m in monsters if m["name"] == "Jr. Necki")
        self.assertEqual(necki, ["300", "800300"])

    def test_spawn_counts_and_map_names_are_attached(self) -> None:
        monsters = {m["id"]: m for m in self.open_snapshot()}
        snail = monsters["100"]
        self.assertEqual(snail["maps"][0]["name"], "Dense Field")
        self.assertEqual(snail["maps"][0]["count"], 10)

    def test_missing_database_reports_clearly(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            cw_calc.connect(self.root / "not-a-snapshot")
        self.assertIn("找不到快照", str(caught.exception))


class TestDamageIsRequired(LevelingFixture):
    """客户端没有伤害公式，任何估算都会偏乐观，因此 --damage 是必填的。"""

    def test_missing_damage_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_calc("--data-dir", self.data_dir, "--level", 10)

    def test_zero_damage_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_calc("--data-dir", self.data_dir, "--damage", 0)

    def test_negative_damage_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_calc("--data-dir", self.data_dir, "--damage", -5)

    def test_there_is_no_estimate_function(self) -> None:
        self.assertFalse(hasattr(cw_calc, "estimate_damage"),
                         "不应存在任何伤害估算函数")

    def test_measured_damage_is_used(self) -> None:
        payload = self.run_calc_json("--data-dir", self.data_dir, "--damage", 120)
        self.assertEqual(payload["damage"], 120)
        self.assertEqual(payload["damage_source"], "玩家实测 120")


class TestCommandLineOutput(LevelingFixture):
    def test_reports_no_two_hit_monster_honestly(self) -> None:
        # 伤害 100 对 4000 血的 Drake 需要 40 刀，走放宽路径
        code, out = self.run_calc("--data-dir", self.data_dir, "--min-level", 50,
                                  "--max-level", 50, "--damage", 100)
        self.assertEqual(code, 0)
        self.assertIn("没有能一两刀砍死的怪", out)
        self.assertIn("数值结构", out)

    def test_json_contains_snapshot_metadata(self) -> None:
        payload = self.run_calc_json("--data-dir", self.data_dir, "--level", 10,
                                     "--damage", 120, "--top", 3)
        self.assertEqual(payload["snapshot"]["edition"], "classic-world")
        self.assertEqual(payload["snapshot"]["schema_version"], "2")
        self.assertIn("filters", payload)
        self.assertIn("monsters", payload)

    def test_atk_cap_filters_unsafe_monsters(self) -> None:
        payload = self.run_calc_json("--data-dir", self.data_dir, "--level", 1,
                                     "--damage", 1000, "--atk-cap", 50, "--top", 99)
        for monster in payload["monsters"]:
            self.assertLessEqual(monster["atk"], 50)

    def test_level_gap_shifts_the_floor(self) -> None:
        wide = self.run_calc_json("--data-dir", self.data_dir, "--level", 30,
                                  "--damage", 1000, "--level-gap", 29, "--top", 99)
        narrow = self.run_calc_json("--data-dir", self.data_dir, "--level", 30,
                                    "--damage", 1000, "--level-gap", 5, "--top", 99)
        wide_levels = {m["level"] for m in wide["monsters"]}
        narrow_levels = {m["level"] for m in narrow["monsters"]}
        self.assertTrue(min(wide_levels) < min(narrow_levels) if narrow_levels else True)


class TestWebDataExport(LevelingFixture):
    def build(self, out_dir: Path) -> tuple[list, dict]:
        previous = sys.argv
        sys.argv = ["export_web_data.py", "--data-dir", str(self.data_dir),
                    "--out-dir", str(out_dir)]
        try:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(export_web_data.main(), 0)
        finally:
            sys.argv = previous

        mobs_src = (out_dir / "mobs.js").read_text(encoding="utf-8")
        gear_src = (out_dir / "gear.js").read_text(encoding="utf-8")
        self.assertTrue(mobs_src.startswith("const MOBS = "))
        self.assertTrue(gear_src.startswith("const GEAR = "))
        mobs = json.loads(mobs_src[len("const MOBS = "):].rstrip().rstrip(";"))
        gear = json.loads(gear_src[len("const GEAR = "):].rstrip().rstrip(";"))
        return mobs, gear

    def test_export_matches_the_snapshot(self) -> None:
        mobs, gear = self.build(self.root / "web")
        by_id = {m["id"]: m for m in mobs}
        self.assertEqual(set(by_id), {"100", "200", "300", "800300", "999", "888"})
        self.assertEqual(by_id["100"]["hp"], 40)
        self.assertEqual(by_id["100"]["exp"], 2)
        self.assertEqual(by_id["999"]["hp"], 4000)

    def test_export_carries_raw_defense_field(self) -> None:
        mobs, _ = self.build(self.root / "web-pdd")
        by_id = {m["id"]: m for m in mobs}
        self.assertEqual(by_id["100"]["pdd"], 0)
        self.assertEqual(by_id["888"]["pdd"], 500)
        self.assertEqual(by_id["999"]["pdd"], 15)

    def test_export_excludes_skipped_monsters(self) -> None:
        mobs, _ = self.build(self.root / "web2")
        names = {m["name"] for m in mobs}
        self.assertNotIn("Test(Basic)", names)
        self.assertNotIn("Super Slime", names)

    def test_export_creates_output_directory(self) -> None:
        target = self.root / "nested" / "web"
        self.assertFalse(target.exists())
        self.build(target)
        self.assertTrue((target / "mobs.js").is_file())
        self.assertTrue((target / "gear.js").is_file())


class TestMonsterDefenseIsShownNotModelled(LevelingFixture):
    """物防字段只展示和告警，不参与刀数计算。

    客户端没有给出物防如何进入伤害公式，所以本工具不编换算。
    """

    def test_defense_field_is_read_from_snapshot(self) -> None:
        monsters = {m["id"]: m for m in self.open_snapshot()}
        self.assertEqual(monsters["100"]["pdd_raw"], 0)
        self.assertEqual(monsters["888"]["pdd_raw"], 500)

    def test_hits_ignore_defense(self) -> None:
        monsters = self.open_snapshot()
        # Snail: 40 血, 物防 0；Iron Hog: 500 血, 物防 500
        # 两者刀数都只由血量/伤害决定，物防不参与
        ranked = cw_calc.rank(monsters, damage=50, min_nodes=1, max_hits=99,
                              min_level=1, max_level=100)
        by_id = {m["id"]: m for m in ranked}
        self.assertIn("100", by_id)
        self.assertIn("888", by_id)
        self.assertEqual(by_id["100"]["hits"], 1)   # ceil(40/50)
        self.assertEqual(by_id["888"]["hits"], 10)  # ceil(500/50)，物防 500 不影响

    def test_defense_note_flags_positive_defense(self) -> None:
        monsters = self.open_snapshot()
        note = cw_calc.defense_note(monsters)
        self.assertIn("物防", note)
        self.assertIn("Iron Hog", note)

    def test_defense_note_says_clear_when_all_zero(self) -> None:
        clean = [{"name": "A", "pdd_raw": 0}, {"name": "B", "pdd_raw": None}]
        note = cw_calc.defense_note(clean)
        self.assertIn("都是 0", note)

    def test_max_pdd_filter_excludes_hard_monsters(self) -> None:
        payload = self.run_calc_json("--data-dir", self.data_dir, "--damage", 50,
                                     "--max-pdd", 0, "--top", 99)
        self.assertTrue(payload["monsters"], "应仍有物防为 0 的怪")
        for monster in payload["monsters"]:
            self.assertFalse(monster.get("pdd_raw"),
                             "物防应被过滤为 0，实际=%s" % monster.get("pdd_raw"))

    def test_cli_output_has_defense_column(self) -> None:
        code, out = self.run_calc("--data-dir", self.data_dir, "--damage", 50)
        self.assertEqual(code, 0)
        self.assertIn("物防", out)


class TestWebCalculatorAssets(unittest.TestCase):
    def setUp(self) -> None:
        self.html_path = LEVELING_ROOT / "web" / "cw-calculator.html"

    def test_calculator_page_exists(self) -> None:
        self.assertTrue(self.html_path.is_file(), "网页计算器文件缺失")

    def test_calculator_references_both_data_files(self) -> None:
        html = self.html_path.read_text(encoding="utf-8")
        self.assertIn('src="mobs.js"', html)
        self.assertIn('src="gear.js"', html)

    def test_every_referenced_element_id_exists(self) -> None:
        import re
        html = self.html_path.read_text(encoding="utf-8")
        defined = set(re.findall(r'\bid="([^"]+)"', html))
        used = set(re.findall(r"\$\('([^']+)'\)", html))
        self.assertTrue(used, "未找到任何控件引用，检查方式可能已失效")
        self.assertEqual(used - defined, set(), "脚本引用了不存在的控件")

    def test_div_tags_are_balanced(self) -> None:
        import re
        html = self.html_path.read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"<div\b", html)), len(re.findall(r"</div>", html)))

    def test_calculator_does_not_estimate_damage(self) -> None:
        html = self.html_path.read_text(encoding="utf-8")
        self.assertNotIn("estimateDamage", html,
                         "网页不应保留任何伤害估算函数")
        self.assertNotIn("* 0.8", html,
                         "网页不应保留估算用的系数")

    def test_calculator_asks_for_measured_damage(self) -> None:
        html = self.html_path.read_text(encoding="utf-8")
        self.assertIn("请先填", html)
        self.assertIn("单刀伤害", html)

    def test_calculator_shows_defense_column(self) -> None:
        html = self.html_path.read_text(encoding="utf-8")
        self.assertIn("物防", html)


class TestSkillDocumentation(unittest.TestCase):
    def test_child_skill_declares_name_and_description(self) -> None:
        text = (LEVELING_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: cw-rag-leveling", text)
        self.assertIn("description:", text)

    def test_child_skill_states_the_damage_limit(self) -> None:
        text = (LEVELING_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("没有伤害计算公式", text)
        self.assertIn("必填", text)

    def test_parent_skill_links_to_child(self) -> None:
        text = (REPOSITORY_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("cw-rag-leveling", text)
        self.assertTrue((REPOSITORY_ROOT / "skills" / "cw-rag-leveling" / "SKILL.md").is_file())

    def test_generated_web_data_is_gitignored(self) -> None:
        text = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("skills/cw-rag-leveling/web/mobs.js", text)
        self.assertIn("skills/cw-rag-leveling/web/gear.js", text)


if __name__ == "__main__":
    unittest.main()
