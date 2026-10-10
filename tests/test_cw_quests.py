from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cw_extract_quests


def image(resource: str, image_name: str, tree: dict) -> dict:
    return {
        "resource": resource,
        "image": image_name,
        "tree": tree,
        "file_sha256": "a" * 64,
        "image_sha256": "b" * 64,
    }


QUEST_1001 = {
    "QuestInfo": {
        "name": "Bringing a Mirror to Heena",
        "parent": "Sera's Mirror",
        "order": 2,
        "area": 1,
        "1": "I got the mirror from #b#p2##k, who was doing her laundry. I have to take it to #b#p1##k now.",
    },
    "Check": {
        "0": {
            "npc": 2,
            "quest": {"0": {"id": 1000, "state": 2, "order": 1}},
            "job": {"0": 0, "1": 100},
            "job_GL": None,
        },
        "1": {"npc": 1, "item": {"0": {"id": 4031000, "count": 1, "order": 1}}, "order": 1},
    },
    "Act": {
        "0": {"item": {"0": {"id": 4031000, "count": 1}}},
        "1": {"exp": 2, "item": {"0": {"id": 4031000, "count": -1, "potentialGrade": "Normal"}}},
    },
    "Say": {
        "0": {"0": "The mirror looks like this: #i4031000#.", "stop": None, "yes": {"0": "Thank you."}},
        "1": {"stop": {"npc": {"0": "Please hurry!"}}},
    },
}


QUEST_1009 = {
    "QuestInfo": {"name": "Mai's Training", "parent": "Mai's Training", "area": 1},
    "Check": {
        "0": {"npc": 11, "quest": {"0": {"id": 1006, "state": 2}}, "job": {"0": 0}},
        "1": {
            "npc": 11,
            "mob": {
                "0": {"id": 3, "count": 10},
                "1": {"id": 4, "count": 10},
                "2": {"id": 5, "count": 5},
            },
        },
    },
    "Act": {"0": None, "1": {"exp": 80, "nextQuest": 1010, "item": {"0": {"id": 2010002, "count": 10, "potentialGrade": "normal"}}}},
    "Say": {"0": {"0": "Listen, hunt weaker monsters until you can handle bigger ones."}},
}

QUEST_1011 = {
    "QuestInfo": {"name": "Biggs's Collection of Items", "area": 1},
    "Act": {
        "0": None,
        "1": {
            "exp": 120,
            "item": {
                "0": {"id": 1332001, "count": 1, "prop": 1},
                "1": {"id": 1332000, "count": 1, "prop": 1},
                "2": {"id": 4000002, "count": -10},
                "3": {"id": 4000009, "count": -1},
            },
        },
    },
}


class TestCwQuests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "Data"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def source_file(self, relative: str) -> Path:
        path = self.root / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
        return path

    def run_extract(self, relative_files: list[str], images: list[dict]) -> dict:
        files = [self.source_file(relative) for relative in relative_files]
        context = {"data_root": self.root, "files": files, "file_hashes": {}, "names": {}}

        def fixture_images(_context, paths, accept):
            selected = {Path(path).relative_to(self.root).as_posix() for path in paths}
            self.selected_paths = selected
            return iter(
                fixture
                for fixture in images
                if fixture["resource"] in selected and accept(fixture["resource"], fixture["image"])
            )

        with patch.object(cw_extract_quests, "iter_images", side_effect=fixture_images):
            return cw_extract_quests.extract(context)

    def test_real_quest_shape_keeps_subtrees_name_and_unknown_scalar_data(self) -> None:
        result = self.run_extract(
            ["Quest/QuestData/QuestData_000.wz"],
            [image("Quest/QuestData/QuestData_000.wz", "1001.img", QUEST_1001)],
        )

        quest = result["entities"][0]
        self.assertEqual((quest["kind"], quest["normalized_id"], quest["original_id"]), ("quest", "1001", "1001"))
        self.assertEqual(quest["name"], "Bringing a Mirror to Heena")
        self.assertEqual(quest["fields"]["quest_info"], QUEST_1001["QuestInfo"])
        self.assertEqual(quest["fields"]["quest_check"], QUEST_1001["Check"])
        self.assertEqual(quest["fields"]["quest_act"], QUEST_1001["Act"])
        self.assertEqual(quest["fields"]["quest_say"], QUEST_1001["Say"])
        self.assertEqual(quest["fields"]["raw"], QUEST_1001)
        self.assertIsNone(quest["fields"]["raw"]["Check"]["0"]["job_GL"])
        self.assertEqual(quest["fields"]["raw"]["Check"]["1"]["order"], 1)
        self.assertEqual(quest["fields"]["raw"]["Act"]["1"]["item"]["0"]["count"], -1)
        self.assertEqual(quest["fields"]["raw"]["Act"]["1"]["item"]["0"]["potentialGrade"], "Normal")
        self.assertEqual({source["node"] for source in quest["sources"]}, {"QuestInfo", "Check", "Act", "Say"})

    def test_real_stage_references_have_typed_targets_and_exact_source_branches(self) -> None:
        result = self.run_extract(
            ["Quest/QuestData/QuestData_000.wz"],
            [image("Quest/QuestData/QuestData_000.wz", "1009.img", QUEST_1009)],
        )

        relations_by_path = {relation["node_path"]: relation for relation in result["relations"]}
        self.assertEqual(
            set(relations_by_path),
            {
                "Check/0/npc",
                "Check/0/quest/0/id",
                "Check/1/npc",
                "Check/1/mob/0/id",
                "Check/1/mob/1/id",
                "Check/1/mob/2/id",
                "Act/1/item/0/id",
                "Act/1/nextQuest",
            },
        )
        self.assertEqual(relations_by_path["Check/0/npc"]["relation"], "quest_check_npc")
        self.assertEqual(relations_by_path["Check/0/npc"]["target_kind"], "npc")
        self.assertEqual(relations_by_path["Check/0/quest/0/id"]["relation"], "quest_check_quest")
        self.assertEqual(relations_by_path["Check/0/quest/0/id"]["raw"], {"id": 1006, "state": 2})
        self.assertEqual(relations_by_path["Check/1/mob/0/id"]["target_id"], "3")
        self.assertEqual(relations_by_path["Check/1/mob/0/id"]["raw"], {"id": 3, "count": 10})
        item_edge = relations_by_path["Act/1/item/0/id"]
        self.assertEqual((item_edge["relation"], item_edge["target_kind"], item_edge["target_id"]), ("quest_act_item", "item_ref", "2010002"))
        self.assertEqual(item_edge["raw"], {"id": 2010002, "count": 10, "potentialGrade": "normal"})
        next_edge = relations_by_path["Act/1/nextQuest"]
        self.assertEqual((next_edge["relation"], next_edge["target_kind"], next_edge["target_id"]), ("quest_next", "quest", "1010"))
        self.assertEqual(next_edge["raw"], QUEST_1009["Act"]["1"])
        self.assertEqual(next_edge["sources"][0]["node"], "Act/1/nextQuest")
        self.assertEqual(next_edge["sources"][0]["source_id"], "1009")
        self.assertEqual(relations_by_path["Check/0/npc"]["raw"], QUEST_1009["Check"]["0"])
        self.assertEqual([entity["kind"] for entity in result["entities"]], ["quest"])

    def test_act_npc_reference_uses_its_own_predicate(self) -> None:
        tree = {"QuestInfo": {"name": "NPC stages"}, "Check": {"0": {"npc": 1}}, "Act": {"1": {"npc": 2}}}
        result = self.run_extract(
            ["Quest/QuestData/QuestData_000.wz"],
            [image("Quest/QuestData/QuestData_000.wz", "1100.img", tree)],
        )

        relations = {relation["node_path"]: relation for relation in result["relations"]}
        self.assertEqual(set(relations), {"Check/0/npc", "Act/1/npc"})
        self.assertEqual(relations["Act/1/npc"]["relation"], "quest_act_npc")
        self.assertEqual(relations["Act/1/npc"]["raw"], tree["Act"]["1"])

    def test_real_action_item_branches_keep_prop_and_signed_counts(self) -> None:
        result = self.run_extract(
            ["Quest/QuestData/QuestData_000.wz"],
            [image("Quest/QuestData/QuestData_000.wz", "1011.img", QUEST_1011)],
        )

        relations = {relation["node_path"]: relation for relation in result["relations"]}
        self.assertEqual(relations["Act/1/item/0/id"]["raw"], {"id": 1332001, "count": 1, "prop": 1})
        self.assertEqual(relations["Act/1/item/2/id"]["raw"], {"id": 4000002, "count": -10})
        self.assertEqual(relations["Act/1/item/3/id"]["raw"], {"id": 4000009, "count": -1})

    def test_malformed_references_are_ignored_without_changing_raw_tree(self) -> None:
        tree = {
            "QuestInfo": {"name": "Guarded references"},
            "Check": {
                "0": {
                    "npc": True,
                    "item": {
                        "0": {"id": "1x", "count": 1},
                        "1": {"id": -1, "count": -1},
                        "2": {"id": True, "count": 1},
                        "3": {"id": "123456789012345678901", "count": 1},
                        "bad": {"id": 3},
                    },
                    "mob": {"0": {"id": 1.5, "count": 1}, "bad": {"id": 2}},
                    "quest": {"0": {"id": False, "state": 2}},
                },
                "bad-stage": {"npc": 5, "mob": {"0": {"id": 7}}},
            },
            "Act": {"1": {"nextQuest": "1.0", "item": {"0": {"id": 2.5}}}},
            "Say": {"0": {"npc": 99, "item": {"0": {"id": 98}}}},
        }
        result = self.run_extract(
            ["Quest/QuestData/QuestData_000.wz"],
            [image("Quest/QuestData/QuestData_000.wz", "1200.img", tree)],
        )

        self.assertEqual(result["relations"], [])
        self.assertEqual(result["entities"][0]["fields"]["raw"], tree)

    def test_coverage_counts_files_images_empty_images_and_missing_images(self) -> None:
        resource = "Quest/QuestData/QuestData_000.wz"
        result = self.run_extract(
            [
                resource,
                "Quest/QuestData/QuestData_001.wz",
                "Quest/QuestDataExtra/QuestData_002.wz",
                "String/String_000.wz",
            ],
            [
                image(resource, "1001.img", QUEST_1001),
                image(resource, "1002.img", {}),
                image(resource, "summary.img", {"QuestInfo": {"name": "Not a numeric quest"}}),
                image(resource, "nested/1003.img", {"QuestInfo": {"name": "Not a root quest image"}}),
                image("Quest/QuestDataExtra/QuestData_002.wz", "1004.img", QUEST_1001),
            ],
        )

        self.assertEqual(self.selected_paths, {resource, "Quest/QuestData/QuestData_001.wz"})
        self.assertEqual(
            result["coverage"]["quest"],
            {
                "selected_files": 2,
                "missing_files": 0,
                "parsed_images": 2,
                "missing_images": 1,
                "empty_images": 1,
                "static_records": 1,
            },
        )
        self.assertEqual(
            {(gap["category"], gap["count"]) for gap in result["gaps"]},
            {("image_absent", 1), ("source_present_empty", 1)},
        )

    def test_missing_source_file_is_reported_as_coverage_gap(self) -> None:
        result = self.run_extract([], [])

        self.assertEqual(
            result["coverage"]["quest"],
            {"selected_files": 0, "missing_files": 1, "parsed_images": 0, "missing_images": 0, "empty_images": 0, "static_records": 0},
        )
        self.assertEqual(result["gaps"], [{"kind": "quest", "category": "source_absent", "count": 1, "examples": [{"resource": "Quest/QuestData/QuestData_*.wz"}]}])


if __name__ == "__main__":
    unittest.main()
