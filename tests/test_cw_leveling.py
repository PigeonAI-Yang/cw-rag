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
                # (id, name, level, hp, exp, attack)
                ("100", "Snail", 1, 40, 2, 3),
                ("200", "Slime", 6, 100, 12, 13),
                ("300", "Jr. Necki", 21, 300, 39, 92),
                # 同名不同 ID：验证不按名字合并
                ("800300", "Jr. Necki", 25, 900, 80, 139),
                # 高级厚血怪：用来触发「无法一两刀」
                ("999", "Drake", 50, 4000, 133, 287),
                # 测试怪：必须被跳过
                ("9990000", "Test(Basic)", 30, 2100000000, None, 166),
                # 超怪：必须被跳过
                ("91", "Super Slime", 200, 9999, 0, 1),
            ]
        entities = []
        for index, (mob_id, name, level, hp, exp, attack) in enumerate(monsters):
            # 列投影由 cw_rag._numeric_value 从 fields["static_info"]["info/<name>"]
            # 读取，字段名为 level / maxHP / exp / PADamage。
            static_info = {
                "info/level": str(level),
                "info/maxHP": str(hp),
                "info/PADamage": str(attack),
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


class TestPlaceholderDamageIsLabelled(LevelingFixture):
    def test_missing_damage_is_flagged_as_not_measured(self) -> None:
        payload = self.run_calc_json("--data-dir", self.data_dir, "--level", 10)
        self.assertFalse(payload["damage_is_measured"])
        self.assertIn("占位值", payload["damage_source"])

    def test_measured_damage_is_flagged_as_measured(self) -> None:
        payload = self.run_calc_json("--data-dir", self.data_dir, "--level", 10,
                                     "--damage", 120)
        self.assertTrue(payload["damage_is_measured"])
        self.assertEqual(payload["damage"], 120)

    def test_zero_damage_is_treated_as_not_measured(self) -> None:
        payload = self.run_calc_json("--data-dir", self.data_dir, "--level", 10, "--damage", 0)
        self.assertFalse(payload["damage_is_measured"])

    def test_placeholder_text_warns_it_is_not_evidence(self) -> None:
        code, out = self.run_calc("--data-dir", self.data_dir, "--level", 10)
        self.assertEqual(code, 0)
        self.assertIn("占位值", out)
        self.assertIn("不能当作玩家真实伤害", out)


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
        code = export_web_data.main.__wrapped__ if hasattr(export_web_data.main, "__wrapped__") else None
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
        self.assertEqual(set(by_id), {"100", "200", "300", "800300", "999"})
        self.assertEqual(by_id["100"]["hp"], 40)
        self.assertEqual(by_id["100"]["exp"], 2)
        self.assertEqual(by_id["999"]["hp"], 4000)

    def test_export_excludes_unverified_and_skipped_monsters(self) -> None:
        mobs, _ = self.build(self.root / "web2")
        names = {m["name"] for m in mobs}
        self.assertNotIn("Test(Basic)", names)
        self.assertNotIn("Super Slime", names)
        # 防御/闪避字段含义未证实，不得导出
        for monster in mobs:
            self.assertNotIn("pdd", monster)
            self.assertNotIn("eva", monster)

    def test_export_creates_output_directory(self) -> None:
        target = self.root / "nested" / "web"
        self.assertFalse(target.exists())
        self.build(target)
        self.assertTrue((target / "mobs.js").is_file())
        self.assertTrue((target / "gear.js").is_file())


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


class TestSkillDocumentation(unittest.TestCase):
    def test_child_skill_declares_name_and_description(self) -> None:
        text = (LEVELING_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: cw-rag-leveling", text)
        self.assertIn("description:", text)

    def test_child_skill_states_the_damage_limit(self) -> None:
        text = (LEVELING_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("没有伤害计算公式", text)
        self.assertIn("占位值", text)

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
