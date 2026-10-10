from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cw_common
import cw_source
from vendor.wzpy.wz_file import WzFile


def _source(role: str, resource: str, image: str, node: str, source_id: str) -> dict:
    return {
        "role": role,
        "resource": resource,
        "image": image,
        "node": node,
        "source_id": source_id,
        "file_sha256": "a" * 64,
        "image_sha256": "b" * 64,
    }


def _domain(entities=(), relations=(), placements=(), coverage=None, gaps=()) -> dict:
    return {
        "entities": list(entities),
        "relations": list(relations),
        "placements": list(placements),
        "coverage": coverage or {},
        "gaps": list(gaps),
    }


class _EmptyNode:
    type_name = "SubProperty"
    name = "root"
    value = None

    def children(self):
        return []


class _ValueNode:
    def __init__(self, name, type_name, value=None, children=()):
        self.name = name
        self.type_name = type_name
        self.value = value
        self._children = list(children)

    def children(self):
        return self._children


class _FakeImage:
    offset = 4
    size = 5
    truncated = False
    parse_warnings = []

    def __init__(self):
        self.parse_calls = 0

    def parse(self):
        self.parse_calls += 1
        return _EmptyNode()


class _FakeRoot:
    def __init__(self, images):
        self.images = images

    def walk_images(self):
        yield from self.images


class _FakeArchive:
    def __init__(self, images):
        self.root = _FakeRoot(images)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True


class TestCwSource(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _data_root(self, name: str = "client") -> Path:
        root = self.root / name / "Data"
        for relative in ("String", "Packs", "Map/Map"):
            (root / relative).mkdir(parents=True, exist_ok=True)
        (root / "String" / "String_000.wz").write_bytes(b"string")
        (root / "Packs" / "Mob_00000.ms").write_bytes(b"mob")
        (root / "Map" / "Map" / "Map0_000.wz").write_bytes(b"map")
        return root

    def test_selector_includes_optional_sources_and_excludes_canvas_directories(self) -> None:
        root = self._data_root()
        files = {
            "Packs/Skill_00000.ms": b"skill",
            "Character/Weapon/Weapon_000.wz": b"weapon",
            "Character/Weapon/_Canvas/Weapon_000.wz": b"canvas",
            "Item/Consume/Consume_000.wz": b"consume",
            "Npc/Npc_000.wz": b"npc",
            "Quest/QuestData/QuestData_000.wz": b"quest",
            "Etc/Etc_000.wz": b"etc",
        }
        for relative, contents in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)

        selected = [path.relative_to(root).as_posix() for path in cw_source.select_source_files(root)]
        self.assertEqual(selected, [
            "Character/Weapon/Weapon_000.wz",
            "Etc/Etc_000.wz",
            "Item/Consume/Consume_000.wz",
            "Map/Map/Map0_000.wz",
            "Npc/Npc_000.wz",
            "Packs/Mob_00000.ms",
            "Packs/Skill_00000.ms",
            "Quest/QuestData/QuestData_000.wz",
            "String/String_000.wz",
        ])

    def test_string_marker_and_required_mob_map_identity_are_preserved(self) -> None:
        root = self._data_root()
        files = cw_source.select_source_files(root)
        hashes = {path.relative_to(root).as_posix(): "a" * 64 for path in files}
        fixtures = [
            {"resource": "String/String_000.wz", "image": "Eqp.img", "tree": {"ClassicWorld": {"Weapon": {"01302000": {"name": "Sword"}}}}, "file_sha256": "a" * 64, "image_sha256": "b" * 64},
            {"resource": "String/String_000.wz", "image": "Mob.img", "tree": {"900": {"name": "Slime"}}, "file_sha256": "a" * 64, "image_sha256": "c" * 64},
            {"resource": "String/String_000.wz", "image": "Map.img", "tree": {"10002031": {"mapName": "Forest"}}, "file_sha256": "a" * 64, "image_sha256": "d" * 64},
        ]
        with patch.object(cw_source, "iter_images", return_value=iter(fixtures)):
            names, root_coverage, gaps = cw_source._parse_name_archives(root, files, hashes)
        self.assertEqual(names[("equipment", "1302000")]["name"], "Sword")
        self.assertEqual(names[("equipment", "1302000")]["section"], "equipment")
        self.assertEqual(names[("mob", "900")]["name"], "Slime")
        self.assertEqual(names[("map", "10002031")]["name"], "Forest")
        self.assertEqual(root_coverage["Eqp.img"]["status"], "source_present")
        self.assertEqual(root_coverage["Skill.img"]["status"], "source_absent")
        self.assertTrue(any(gap["category"] == "image_absent" and gap["kind"] == "skill" for gap in gaps))

        invalid = [dict(fixtures[0], tree={"OtherWorld": {}}), *fixtures[1:]]
        with patch.object(cw_source, "iter_images", return_value=iter(invalid)):
            with self.assertRaisesRegex(cw_common.SourceError, "ClassicWorld marker"):
                cw_source._parse_name_archives(root, files, hashes)

        invalid = [fixtures[0], dict(fixtures[1], tree={}), fixtures[2]]
        with patch.object(cw_source, "iter_images", return_value=iter(invalid)):
            with self.assertRaisesRegex(cw_common.SourceError, "monster names"):
                cw_source._parse_name_archives(root, files, hashes)

    def test_eqp_name_kind_uses_unique_static_match_and_marks_unmatched_names(self) -> None:
        source = _source("name", "String/String_000.wz", "Eqp.img", "ClassicWorld/OldFamily/123", "123")
        names = {
            ("equipment", "123"): {
                "original_id": "123", "name": "Hat", "name_info": {}, "sources": [source],
                "family": "OldFamily", "family_unresolved": True, "section": "equipment",
            },
        }
        static = {
            ("item", "123"): {
                "kind": "item", "normalized_id": "123", "original_id": "123", "name": None,
                "fields": {"family": "Pet", "section": "pets"},
                "sources": [_source("static", "Item/Pet/Pet_000.wz", "123.img", "123", "123")],
            },
        }
        cw_source._resolve_eqp_name_kinds(names, static)
        self.assertNotIn(("equipment", "123"), names)
        self.assertFalse(names[("item", "123")]["family_unresolved"])
        self.assertEqual(names[("item", "123")]["section"], "pets")

        orphan_names = {
            ("equipment", "456"): {
                "original_id": "456", "name": "Unknown item", "name_info": {}, "sources": [source],
                "family": "OldFamily", "family_unresolved": True, "section": "equipment",
            },
        }
        cw_source._resolve_eqp_name_kinds(orphan_names, {})
        self.assertTrue(orphan_names[("equipment", "456")]["family_unresolved"])

    def test_skill_book_and_level_strings_are_retained_in_name_info(self) -> None:
        root = self._data_root()
        files = cw_source.select_source_files(root)
        hashes = {path.relative_to(root).as_posix(): "a" * 64 for path in files}
        fixtures = [
            {"resource": "String/String_000.wz", "image": "Eqp.img", "tree": {"ClassicWorld": {}}, "file_sha256": "a" * 64, "image_sha256": "b" * 64},
            {"resource": "String/String_000.wz", "image": "Mob.img", "tree": {"900": {"name": "Slime"}}, "file_sha256": "a" * 64, "image_sha256": "c" * 64},
            {"resource": "String/String_000.wz", "image": "Map.img", "tree": {"100": {"mapName": "Forest"}}, "file_sha256": "a" * 64, "image_sha256": "d" * 64},
            {"resource": "String/String_000.wz", "image": "Skill.img", "tree": {"100": {"bookName": "Warrior", "1000000": {"name": "Power Strike", "desc": "Strike", "h1": "100%"}}}, "file_sha256": "a" * 64, "image_sha256": "e" * 64},
        ]
        with patch.object(cw_source, "iter_images", return_value=iter(fixtures)):
            names, _, _ = cw_source._parse_name_archives(root, files, hashes)
        skill = names[("skill", "1000000")]
        self.assertEqual(skill["name"], "Power Strike")
        self.assertEqual(skill["name_info"], {"desc": "Strike", "h1": "100%", "bookName": "Warrior"})

    def test_identical_string_duplicates_merge_evidence_and_conflicts_name_both_addresses(self) -> None:
        root = self._data_root()
        second = root / "String" / "String_001.wz"
        second.write_bytes(b"second")
        files = cw_source.select_source_files(root)
        hashes = {path.relative_to(root).as_posix(): "a" * 64 for path in files}
        base = [
            {"resource": "String/String_000.wz", "image": "Eqp.img", "tree": {"ClassicWorld": {}}, "file_sha256": "a" * 64, "image_sha256": "b" * 64},
            {"resource": "String/String_000.wz", "image": "Mob.img", "tree": {"900": {"name": "Slime"}}, "file_sha256": "a" * 64, "image_sha256": "c" * 64},
            {"resource": "String/String_000.wz", "image": "Map.img", "tree": {"100": {"mapName": "Forest"}}, "file_sha256": "a" * 64, "image_sha256": "d" * 64},
            {"resource": "String/String_001.wz", "image": "Mob.img", "tree": {"0000900": {"name": "Slime"}}, "file_sha256": "a" * 64, "image_sha256": "e" * 64},
        ]
        with patch.object(cw_source, "iter_images", return_value=iter(base)):
            names, _, _ = cw_source._parse_name_archives(root, files, hashes)
        self.assertEqual(len(names[("mob", "900")]["sources"]), 2)

        conflicting = [*base[:3], dict(base[3], tree={"0000900": {"name": "Different"}})]
        with patch.object(cw_source, "iter_images", return_value=iter(conflicting)):
            with self.assertRaisesRegex(cw_common.SourceError, "String/String_000.wz/Mob.img/900.*String/String_001.wz/Mob.img/0000900"):
                cw_source._parse_name_archives(root, files, hashes)

    def test_empty_selected_image_is_yielded_and_archive_closes_early(self) -> None:
        root = self.root / "Data"
        root.mkdir()
        path = root / "String_000.wz"
        path.write_bytes(b"PKG1IMAGE")
        empty = _FakeImage()
        rejected = _FakeImage()
        archive = _FakeArchive([("z.img", rejected), ("a.img", empty)])
        context = {"data_root": root, "files": [path], "file_hashes": {"String_000.wz": "a" * 64}, "names": {}}
        accepted = []
        with patch.object(WzFile, "open", return_value=archive):
            iterator = cw_common.iter_images(context, [path], lambda relative, internal: accepted.append(internal) is None and internal == "a.img")
            record = next(iterator)
            self.assertEqual(record["tree"], {})
            self.assertEqual(record["image_sha256"], hashlib.sha256(b"IMAGE").hexdigest())
            self.assertEqual(accepted, ["a.img"])
            self.assertEqual(empty.parse_calls, 1)
            self.assertEqual(rejected.parse_calls, 0)
            iterator.close()
        self.assertTrue(archive.closed)

    def test_ms_images_use_bms_and_normalize_entry_paths(self) -> None:
        root = self.root / "Data"
        root.mkdir()
        path = root / "Skill_00000.ms"
        path.write_bytes(b"pack")

        class Entry:
            name = "Skill\\100.img"

        class Pack:
            entries = [Entry()]
            closed = False

            def read_entry_data(self, _entry):
                return b"decoded-image"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self.closed = True

        class ParsedImage:
            truncated = False
            parse_warnings = []

            def parse(self):
                return _EmptyNode()

        pack = Pack()
        with patch("vendor.wzpy.ms_file_v2.MsFileV2.open", return_value=pack), patch(
            "vendor.wzpy.wz_image.WzImage.from_bytes", return_value=ParsedImage()
        ) as parse_bytes, patch("vendor.wzpy.crypto.WzKey.for_region") as region_key:
            context = {"data_root": root, "files": [path], "file_hashes": {"Skill_00000.ms": "a" * 64}, "names": {}}
            records = list(cw_common.iter_images(context, [path], lambda _resource, internal: internal == "Skill/100.img"))
        self.assertEqual(records[0]["image"], "Skill/100.img")
        self.assertEqual(records[0]["tree"], {})
        self.assertEqual(records[0]["image_sha256"], hashlib.sha256(b"decoded-image").hexdigest())
        self.assertTrue(pack.closed)
        region_key.assert_called_once_with("BMS")
        self.assertEqual(parse_bytes.call_args.kwargs["name"], "Skill/100.img")

    def test_scalar_tree_preserves_uol_type_and_rejects_non_json_values(self) -> None:
        tree = _ValueNode("root", "SubProperty", children=[
            _ValueNode("number", "Int", 17),
            _ValueNode("vector", "Vector", (3, -2)),
            _ValueNode("link", "UOL", "../target"),
            _ValueNode("empty", "SubProperty"),
            _ValueNode("ignored", "Canvas", b"pixel data"),
        ])
        self.assertEqual(cw_common._node_tree(tree, "source.img"), {
            "number": 17,
            "vector": [3, -2],
            "link": {"$type": "UOL", "value": "../target"},
            "empty": {},
        })
        with self.assertRaisesRegex(cw_common.SourceError, "source.img/bad"):
            cw_common._node_tree(_ValueNode("bad", "Opaque", object()), "source.img/bad")

    def test_common_normalization_flatten_and_projection_preserve_types(self) -> None:
        self.assertEqual(cw_common.normalize_id("0000900", "mob"), "900")
        self.assertEqual(cw_common.normalize_id("00000000000000000042", "item"), "42")
        self.assertEqual(cw_common.normalize_id("0002/0003", "recipe"), "2/3")
        with self.assertRaises(cw_common.SourceError):
            cw_common.normalize_id("0" * 20 + "1", "item")
        self.assertEqual(cw_common.flatten({"value": (1, "2", True), "empty": {}}), {
            "value/0": 1,
            "value/1": "2",
            "value/2": True,
        })
        self.assertEqual(cw_common.integer_or_none(True), None)
        self.assertEqual(cw_common.integer_or_none("-12"), -12)
        self.assertIsNone(cw_common.integer_or_none("9223372036854775808"))
        self.assertEqual(cw_common.entity("recipe", "01/002", {}, [])["normalized_id"], "1/2")

    def test_source_set_mutation_is_detected(self) -> None:
        root = self._data_root("mutation")
        prepared = cw_source.prepare_source(root)
        (root / "String" / "String_000.wz").write_bytes(b"changed")
        with self.assertRaises(cw_common.SourceError):
            cw_source.verify_source_unchanged(prepared)

    def test_parser_revision_tracks_new_domain_modules_and_bytes(self) -> None:
        source_root = self.root / "scripts"
        vendor = source_root / "vendor" / "wzpy"
        vendor.mkdir(parents=True)
        for name in ("cw_source.py", "cw_common.py", "cw_rag.py"):
            (source_root / name).write_text(name, encoding="utf-8")
        for name in cw_source.VENDOR_PARSER_FILES:
            (vendor / name).write_text(name, encoding="utf-8")
        initial = cw_source.parser_revision(source_root)
        new_module = source_root / "cw_extract_items.py"
        new_module.write_text("items", encoding="utf-8")
        added = cw_source.parser_revision(source_root)
        new_module.write_text("items changed", encoding="utf-8")
        changed = cw_source.parser_revision(source_root)
        new_module.unlink()
        removed = cw_source.parser_revision(source_root)
        self.assertNotEqual(initial, added)
        self.assertNotEqual(added, changed)
        self.assertEqual(initial, removed)

    def test_snapshot_assembles_duplicate_entities_resolves_item_ref_and_keeps_zero_counts(self) -> None:
        root = self._data_root("assembly")
        prepared = cw_source.prepare_source(root)
        source = _source("static", "Packs/Mob_00000.ms", "Mob/900.img", "info", "900")
        name_source = _source("name", "String/String_000.wz", "Mob.img", "900", "900")
        map_source = _source("static", "Map/Map/Map0_000.wz", "100.img", "", "100")
        item_source = _source("static", "Item/Etc/Etc_000.wz", "100.img", "100", "100")
        quest_source = _source("static", "Quest/QuestData/QuestData_000.wz", "1000.img", "QuestInfo", "1000")
        map_mob = {
            "source_kind": "map", "source_id": "100", "relation": "map_mob", "target_kind": "mob", "target_id": "900",
            "node_path": "life/0", "raw": {"type": "m", "id": "900"}, "sources": [map_source],
        }
        item_ref = {
            "source_kind": "quest", "source_id": "1000", "relation": "quest_act_item", "target_kind": "item_ref", "target_id": "100",
            "node_path": "Act/0/item/0", "raw": {"id": "100", "count": -1}, "sources": [quest_source],
        }
        unresolved = {
            "source_kind": "quest", "source_id": "1000", "relation": "quest_next", "target_kind": "quest", "target_id": "9999",
            "node_path": "Act/0/nextQuest", "raw": {"nextQuest": "9999"}, "sources": [quest_source],
        }
        actors = _domain(
            entities=[
                cw_common.entity("mob", "0000900", {"static_info": {"info/hp": 10}}, [source]),
                cw_common.entity("mob", "900", {"static_info": {"info/hp": 10}}, [_source("static", "Packs/Mob_00000.ms", "Mob/900-copy.img", "info", "900")]),
                cw_common.entity("map", "100", {}, [map_source]),
            ],
            relations=[map_mob],
            placements=[{"map_id": "100", "node_id": "0", "mob_id": "900", "x": None, "y": 1, "raw_json": '{"id":"900","type":"m"}'}],
            coverage={
                "mob": {"selected_files": 1, "parsed_images": 1, "static_records": 2},
                "map": {"selected_files": 1, "parsed_images": 1, "static_records": 1},
                "npc": {"selected_files": 0, "parsed_images": 0, "static_records": 0},
            },
        )
        items = _domain(
            entities=[cw_common.entity("item", "100", {"family": "Etc", "item_effects": {"spec/hp": 5}}, [item_source])],
            coverage={
                "equipment": {"selected_files": 0, "parsed_images": 0, "static_records": 0},
                "item": {"selected_files": 1, "parsed_images": 1, "static_records": 1},
                "set": {"selected_files": 0, "parsed_images": 0, "static_records": 0},
                "recipe": {"selected_files": 0, "parsed_images": 0, "static_records": 0},
            },
            gaps=[{"kind": "set", "category": "source_present_empty", "count": 1, "examples": ["Etc/SetItemInfo.img"]}],
        )
        skills = _domain(coverage={"skill": {"selected_files": 0, "parsed_images": 0, "static_records": 0}})
        quests = _domain(
            entities=[cw_common.entity("quest", "1000", {"quest_info": {"name": "Test"}}, [quest_source], name="Test")],
            relations=[item_ref, unresolved],
            coverage={"quest": {"selected_files": 1, "parsed_images": 1, "static_records": 1}},
        )
        name_records = {
            ("mob", "900"): {"original_id": "900", "name": "Slime", "name_info": {}, "sources": [name_source], "family": None, "family_unresolved": False},
            ("map", "100"): {"original_id": "100", "name": "Forest", "name_info": {}, "sources": [_source("name", "String/String_000.wz", "Map.img", "100", "100")], "family": None, "family_unresolved": False},
        }
        name_roots = {root_name: {"parsed_images": 0, "name_records": 0, "status": "source_present_empty"} for root_name in cw_source.STRING_ROOTS}
        domains = [actors, items, skills, quests]
        with patch.object(cw_source, "_parse_name_archives", return_value=(name_records, name_roots, [])), patch.object(cw_source, "_domain_results", return_value=domains):
            snapshot = cw_source.extract_snapshot(root, prepared)
        self.assertEqual(snapshot["schema_version"], "2")
        self.assertEqual(snapshot["entity_counts"], {"mob": 1, "map": 1, "equipment": 0, "item": 1, "skill": 0, "npc": 0, "quest": 1, "set": 0, "recipe": 0})
        mob = next(item for item in snapshot["entities"] if item["kind"] == "mob")
        self.assertEqual(sum(source["role"] == "static" for source in mob["sources"]), 2)
        self.assertEqual(sum(source["role"] == "name" for source in mob["sources"]), 1)
        self.assertEqual(snapshot["coverage"]["by_kind"]["mob"]["static_records"], 2)
        self.assertEqual(snapshot["coverage"]["by_kind"]["mob"]["entities"], 1)
        self.assertEqual(snapshot["relation_count"], 3)
        self.assertEqual(snapshot["coverage"]["by_kind"]["set"]["static_records"], 0)
        item_edge = next(edge for edge in snapshot["relations"] if edge["relation"] == "quest_act_item")
        self.assertEqual((item_edge["target_kind"], item_edge["target_id"], item_edge["resolved"]), ("item", "100", True))
        unresolved_edge = next(edge for edge in snapshot["relations"] if edge["relation"] == "quest_next")
        self.assertFalse(unresolved_edge["resolved"])
        self.assertTrue(any(gap["category"] == "unresolved_target" for gap in snapshot["gaps"]))

        duplicate = cw_common.entity("mob", "900", {"static_info": {"info/hp": 11}}, [_source("static", "Packs/Mob_00000.ms", "Mob/other.img", "info", "900")])
        with self.assertRaisesRegex(cw_common.SourceError, "Conflicting static definition.*Mob/900.img.*Mob/other.img"):
            cw_source._add_static_entity({("mob", "900"): {"kind": "mob", "normalized_id": "900", "original_id": "900", "name": None,
                                                                "fields": {"static_info": {"info/hp": 10}}, "sources": [source]}}, duplicate)

    def test_snapshot_integrates_real_domain_extractors_from_literal_images(self) -> None:
        import cw_extract_actors
        import cw_extract_items
        import cw_extract_quests
        import cw_extract_skills

        root = self._data_root("actual-domains")
        resources = {
            "Packs/Skill_00000.ms": b"skill",
            "Character/Weapon/Weapon_000.wz": b"weapon",
            "Item/Consume/Consume_000.wz": b"consume",
            "Npc/Npc_000.wz": b"npc",
            "Quest/QuestData/QuestData_000.wz": b"quest",
            "Etc/Etc_000.wz": b"etc",
        }
        for relative, contents in resources.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)

        prepared = cw_source.prepare_source(root)
        trees = {
            "String/String_000.wz": {
                "Eqp.img": {"ClassicWorld": {"Weapon": {"01302000": {"name": "Sword"}}}},
                "Mob.img": {"900": {"name": "Slime"}},
                "Map.img": {"100": {"mapName": "Forest"}},
                "Npc.img": {"901": {"name": "Guide"}},
                "Consume.img": {"02000000": {"name": "Potion"}},
                "Skill.img": {
                    "100": {
                        "bookName": "Warrior",
                        "1000000": {"name": "Power Strike", "h1": "100%"},
                        "1000001": {"name": "Improved"},
                    }
                },
            },
            "Packs/Mob_00000.ms": {"Mob/900.img": {"info": {"hp": 10}}},
            "Packs/Skill_00000.ms": {
                "Skill/100.img": {
                    "skill": {
                        "1000000": {"common": {"maxLevel": 10}, "req": {"1000001": 3}},
                        "1000001": {"common": {"maxLevel": 5}},
                    }
                }
            },
            "Map/Map/Map0_000.wz": {
                "100.img": {
                    "info": {"returnMap": 100},
                    "life": {
                        "0": {"type": "m", "id": "900", "x": "5", "y": 6},
                        "1": {"type": "n", "id": "901", "x": 1, "y": 2},
                    },
                    "portal": {"0": {"tm": 100, "tn": "sp"}},
                }
            },
            "Npc/Npc_000.wz": {"901.img": {"info": {"speak": "Welcome"}}},
            "Character/Weapon/Weapon_000.wz": {"01302000.img": {"info": {"incWAT": 17, "incPAD": 2}}},
            "Item/Consume/Consume_000.wz": {
                "0200.img": {
                    "02000000": {"spec": {"hp": 100}},
                    "02000001": {"spec": {"hp": 250}},
                }
            },
            "Etc/Etc_000.wz": {"SetItemInfo.img": {}, "ItemMake.img": {}},
            "Quest/QuestData/QuestData_000.wz": {
                "1000.img": {
                    "QuestInfo": {"name": "First Quest"},
                    "Check": {
                        "0": {
                            "npc": 901,
                            "item": {"0": {"id": "02000000", "count": 1}},
                            "mob": {"0": {"id": "900", "count": 2}},
                        }
                    },
                    "Act": {
                        "0": {
                            "item": {"0": {"id": "02000001", "count": -1}},
                            "nextQuest": "1001",
                        }
                    },
                },
                "1001.img": {"QuestInfo": {"name": "Second Quest"}},
            },
        }

        def fake_iter_images(context, paths, accept):
            for path in sorted(map(Path, paths), key=lambda item: item.relative_to(context["data_root"]).as_posix().casefold()):
                relative = path.relative_to(context["data_root"]).as_posix()
                for internal, tree in sorted(trees.get(relative, {}).items(), key=lambda item: (item[0].casefold(), item[0])):
                    if not accept(relative, internal):
                        continue
                    image_bytes = f"{relative}\0{internal}".encode("utf-8")
                    yield {
                        "resource": relative,
                        "image": internal,
                        "tree": tree,
                        "file_sha256": context["file_hashes"][relative],
                        "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
                    }

        with (
            patch.object(cw_source, "iter_images", side_effect=fake_iter_images),
            patch.object(cw_common, "iter_images", side_effect=fake_iter_images),
            patch.object(cw_extract_items, "iter_images", side_effect=fake_iter_images),
            patch.object(cw_extract_skills, "iter_images", side_effect=fake_iter_images),
            patch.object(cw_extract_quests, "iter_images", side_effect=fake_iter_images),
        ):
            snapshot = cw_source.extract_snapshot(root, prepared)

        entities = {(item["kind"], item["normalized_id"]): item for item in snapshot["entities"]}
        self.assertEqual(entities[("equipment", "1302000")]["name"], "Sword")
        self.assertEqual(entities[("item", "2000000")]["name"], "Potion")
        self.assertEqual(entities[("skill", "1000000")]["fields"]["skill_book"], {"group": "100", "title": "Warrior"})
        self.assertEqual(entities[("quest", "1000")]["name"], "First Quest")
        self.assertEqual(snapshot["coverage"]["named"]["quest"], 2)
        self.assertEqual(snapshot["coverage"]["by_kind"]["quest"]["names"], 2)
        self.assertEqual(snapshot["coverage"]["by_kind"]["quest"]["static_without_name"], 0)
        self.assertEqual(snapshot["coverage"]["by_kind"]["quest"]["named_without_static"], 0)
        self.assertFalse(any(gap["kind"] == "quest" and gap["category"] == "name_static_mismatch" for gap in snapshot["gaps"]))
        self.assertEqual(snapshot["coverage"]["by_kind"]["item"]["static_without_name"], 1)
        item_name_gap = next(gap for gap in snapshot["gaps"] if gap["kind"] == "item" and gap["category"] == "name_static_mismatch")
        self.assertEqual(item_name_gap["count"], 1)
        self.assertEqual(snapshot["coverage"]["by_kind"]["recipe"]["static_records"], 0)
        self.assertEqual(snapshot["coverage"]["by_kind"]["set"]["parsed_images"], 1)
        self.assertTrue(any(gap["kind"] == "recipe" and gap["category"] == "source_present_empty" for gap in snapshot["gaps"]))
        self.assertTrue(any(gap["kind"] == "set" and gap["category"] == "source_present_empty" for gap in snapshot["gaps"]))

        relations = {(item["relation"], item["target_kind"], item["target_id"]): item for item in snapshot["relations"]}
        self.assertTrue(relations[("map_mob", "mob", "900")]["resolved"])
        self.assertTrue(relations[("map_npc", "npc", "901")]["resolved"])
        self.assertTrue(relations[("portal_target", "map", "100")]["resolved"])
        self.assertTrue(relations[("skill_req", "skill", "1000001")]["resolved"])
        self.assertTrue(relations[("quest_check_item", "item", "2000000")]["resolved"])
        self.assertTrue(relations[("quest_act_item", "item", "2000001")]["resolved"])
        self.assertTrue(relations[("quest_check_mob", "mob", "900")]["resolved"])
        self.assertTrue(relations[("quest_check_npc", "npc", "901")]["resolved"])
        self.assertTrue(relations[("quest_next", "quest", "1001")]["resolved"])
        self.assertEqual(snapshot["placement_count"], 1)
        self.assertEqual(snapshot["relation_count"], len(snapshot["relations"]))


if __name__ == "__main__":
    unittest.main()
