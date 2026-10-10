from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cw_extract_items


def image(resource: str, image_name: str, tree: dict, *, file_sha256: str = "a" * 64, image_sha256: str = "b" * 64) -> dict:
    return {
        "resource": resource,
        "image": image_name,
        "tree": tree,
        "file_sha256": file_sha256,
        "image_sha256": image_sha256,
    }


class TestCwItems(unittest.TestCase):
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
        file_hashes = {
            path.relative_to(self.root).as_posix(): "a" * 64
            for path in files
        }
        context = {"data_root": self.root, "files": files, "file_hashes": file_hashes, "names": {}}

        def fixture_images(_context, paths, accept):
            selected = {Path(path).relative_to(self.root).as_posix() for path in paths}
            return iter(
                fixture
                for fixture in images
                if fixture["resource"] in selected
                and accept(fixture["resource"], fixture["image"])
            )

        with patch.object(cw_extract_items, "iter_images", side_effect=fixture_images):
            return cw_extract_items.extract(context)

    def test_character_weapon_images_keep_literal_info_paths_and_distinct_incwat(self) -> None:
        weapon_info = {
            "islot": "Wp",
            "vslot": "Wp",
            "walk": 1,
            "stand": 1,
            "attack": 1,
            "afterImage": "swordOL",
            "sfx": "swordL",
            "reqJob": 0,
            "reqLevel": 0,
            "reqSTR": 0,
            "reqDEX": 0,
            "reqINT": 0,
            "reqLUK": 0,
            "incWAT": 17,
            "tuc": 7,
            "price": 0,
            "attackSpeed": 4,
            "cash": 0,
        }
        result = self.run_extract(
            ["Character/Weapon/Weapon_000.wz"],
            [
                image("Character/Weapon/Weapon_000.wz", "01302000.img", {"info": weapon_info}),
                image("Character/Weapon/Weapon_000.wz", "01302001.img", {"info": {"incWAT": 27, "incPAD": 3}}),
            ],
        )

        weapons = {record["original_id"]: record for record in result["entities"]}
        first = weapons["01302000"]
        second = weapons["01302001"]
        self.assertEqual((first["kind"], first["normalized_id"], first["fields"]["section"]), ("equipment", "1302000", "equipment"))
        self.assertEqual(first["fields"]["static_info"]["info/incWAT"], 17)
        self.assertEqual(first["fields"]["static_info"]["info/reqJob"], 0)
        self.assertEqual(first["fields"]["raw"], {"info": weapon_info})
        self.assertEqual(first["sources"][0]["node"], ".")
        self.assertEqual(second["fields"]["static_info"]["info/incWAT"], 27)
        self.assertEqual(second["fields"]["static_info"]["info/incPAD"], 3)
        self.assertNotIn("info/incPAD", first["fields"]["static_info"])
        self.assertNotIn("item_effects", second["fields"])

    def test_grouped_consume_records_keep_each_spec_effect(self) -> None:
        result = self.run_extract(
            ["Item/Consume/Consume_000.wz"],
            [
                image(
                    "Item/Consume/Consume_000.wz",
                    "0200.img",
                    {
                        "02000000": {"info": {"price": 10}, "spec": {"hp": 100}},
                        "02000001": {"info": {"price": 20}, "spec": {"hp": 250}},
                        "02000002": {"info": {"price": 30}, "spec": {"hp": 500}},
                    },
                )
            ],
        )

        items = {record["original_id"]: record for record in result["entities"]}
        self.assertEqual(set(items), {"02000000", "02000001", "02000002"})
        self.assertEqual([items[key]["fields"]["item_effects"]["spec/hp"] for key in sorted(items)], [100, 250, 500])
        self.assertEqual(items["02000000"]["sources"][0]["node"], "02000000")
        self.assertEqual(items["02000000"]["fields"]["raw"]["spec"], {"hp": 100})

    def test_req_job_missing_and_explicit_null_remain_distinct(self) -> None:
        result = self.run_extract(
            ["Character/Coat/Coat_000.wz"],
            [
                image("Character/Coat/Coat_000.wz", "01040000.img", {"info": {"reqLevel": 10}}),
                image("Character/Coat/Coat_000.wz", "01040001.img", {"info": {"reqJob": None}}),
            ],
        )

        records = {record["original_id"]: record for record in result["entities"]}
        self.assertNotIn("info/reqJob", records["01040000"]["fields"]["static_info"])
        self.assertIn("info/reqJob", records["01040001"]["fields"]["static_info"])
        self.assertIsNone(records["01040001"]["fields"]["static_info"]["info/reqJob"])
        self.assertNotIn("reqJob", records["01040000"]["fields"]["raw"]["info"])
        self.assertIsNone(records["01040001"]["fields"]["raw"]["info"]["reqJob"])
        self.assertNotIn("req_job", records["01040000"]["fields"])

    def test_empty_equipment_and_group_records_do_not_create_entities(self) -> None:
        result = self.run_extract(
            ["Character/Weapon/Weapon_000.wz", "Item/Consume/Consume_000.wz"],
            [
                image("Character/Weapon/Weapon_000.wz", "01302000.img", {}),
                image("Item/Consume/Consume_000.wz", "0200.img", {"02000000": {}, "bad": {"info": {"price": 1}}}),
            ],
        )

        self.assertEqual(result["entities"], [])
        gaps = {(gap["kind"], gap["category"]): gap for gap in result["gaps"]}
        self.assertEqual(gaps[("equipment", "source_present_empty")]["count"], 1)
        self.assertNotIn(("item", "source_present_empty"), gaps)

    def test_empty_set_and_recipe_images_have_hash_bearing_gaps(self) -> None:
        expected_file_hash = "c" * 64
        expected_image_hash = "d" * 64
        result = self.run_extract(
            ["Etc/Etc_000.wz"],
            [
                image("Etc/Etc_000.wz", "SetItemInfo.img", {}, file_sha256=expected_file_hash, image_sha256=expected_image_hash),
                image("Etc/Etc_000.wz", "ItemMake.img", {}, file_sha256=expected_file_hash, image_sha256=expected_image_hash),
                image("Etc/Etc_000.wz", "Unrelated.img", {"123": {"ignored": True}}),
            ],
        )

        self.assertEqual(result["entities"], [])
        empty_gaps = [gap for gap in result["gaps"] if gap["category"] == "source_present_empty"]
        self.assertEqual({gap["kind"] for gap in empty_gaps}, {"set", "recipe"})
        for gap in empty_gaps:
            example = gap["examples"][0]
            self.assertEqual(example["file_sha256"], expected_file_hash)
            self.assertEqual(example["image_sha256"], expected_image_hash)

    def test_same_recipe_entry_in_two_groups_has_distinct_composite_identity(self) -> None:
        result = self.run_extract(
            ["Etc/Etc_000.wz"],
            [
                image(
                    "Etc/Etc_000.wz",
                    "ItemMake.img",
                    {"0001": {"0005": {"reqLevel": 1}}, "0002": {"0005": {"reqLevel": 2}}},
                )
            ],
        )

        recipes = {record["original_id"]: record for record in result["entities"]}
        self.assertEqual(set(recipes), {"0001/0005", "0002/0005"})
        self.assertEqual(recipes["0001/0005"]["normalized_id"], "1/5")
        self.assertEqual(recipes["0002/0005"]["normalized_id"], "2/5")
        self.assertEqual(recipes["0001/0005"]["fields"]["recipe_group"], "0001")
        self.assertEqual(recipes["0001/0005"]["fields"]["recipe_entry"], "0005")
        self.assertEqual(recipes["0001/0005"]["fields"]["raw"], {"reqLevel": 1})
        self.assertEqual(result["relations"], [])
        reference_gaps = [gap for gap in result["gaps"] if gap["kind"] == "recipe" and gap["category"] == "reference_shape_unmapped"]
        self.assertEqual(reference_gaps[0]["count"], 2)

    def test_set_uses_numeric_top_level_entries(self) -> None:
        result = self.run_extract(
            ["Etc/Etc_000.wz"],
            [
                image(
                    "Etc/Etc_000.wz",
                    "SetItemInfo.img",
                    {"0001": {"name": "Starter set"}, "bad": {"name": "ignored"}, "0002": {}},
                )
            ],
        )

        sets = {record["original_id"]: record for record in result["entities"]}
        self.assertEqual(set(sets), {"0001"})
        self.assertEqual(sets["0001"]["normalized_id"], "1")
        self.assertEqual(sets["0001"]["fields"]["set_data"], {"name": "Starter set"})
        self.assertEqual(sets["0001"]["fields"]["raw"], {"name": "Starter set"})
        self.assertEqual(result["relations"], [])
        gaps = [gap for gap in result["gaps"] if gap["kind"] == "set" and gap["category"] == "reference_shape_unmapped"]
        self.assertEqual(gaps[0]["count"], 1)

    def test_unknown_item_family_is_retained_with_gap(self) -> None:
        result = self.run_extract(
            ["Item/Mystery/Mystery_000.wz"],
            [image("Item/Mystery/Mystery_000.wz", "9999.img", {"99990000": {"info": {"label": "unknown"}}})],
        )

        record = result["entities"][0]
        self.assertEqual((record["kind"], record["fields"]["family"], record["fields"]["section"]), ("item", "Mystery", "items"))
        self.assertEqual(record["fields"]["raw"], {"info": {"label": "unknown"}})
        self.assertEqual(record["sources"][0]["node"], "99990000")
        gaps = [gap for gap in result["gaps"] if gap["category"] == "unclassified_family"]
        self.assertEqual(gaps[0]["kind"], "item")
        self.assertEqual(gaps[0]["examples"][0]["family"], "Mystery")

    def test_pet_uses_numeric_image_stems_and_pets_section(self) -> None:
        result = self.run_extract(
            ["Item/Pet/Pet_000.wz"],
            [image("Item/Pet/Pet_000.wz", "5000000.img", {"info": {"cash": 1}})],
        )

        pet = result["entities"][0]
        self.assertEqual((pet["kind"], pet["original_id"], pet["fields"]["section"]), ("item", "5000000", "pets"))
        self.assertEqual(pet["sources"][0]["node"], ".")


if __name__ == "__main__":
    unittest.main()
