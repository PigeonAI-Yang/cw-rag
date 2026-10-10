from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cw_common
import cw_extract_actors


class TestCwActors(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.data_root = self.root / "Data"
        self.data_root.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _context(self, relative_paths: list[str]) -> dict:
        paths = []
        file_hashes = {}
        for index, relative in enumerate(relative_paths):
            path = self.data_root / Path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
            paths.append(path)
            file_hashes[relative.replace("\\", "/")] = f"{index + 1:064x}"
        return {
            "data_root": self.data_root,
            "files": paths,
            "file_hashes": file_hashes,
            "names": {},
        }

    def _iter_fixture_images(self, fixtures: list[dict]):
        def iter_images(context: dict, paths: list[Path], accept):
            selected = {path.resolve(strict=False).relative_to(context["data_root"].resolve(strict=False)).as_posix() for path in paths}
            for image in fixtures:
                if image["resource"] in selected and accept(image["resource"], image["image"]):
                    yield image

        return iter_images

    @staticmethod
    def _image(resource: str, image: str, tree: dict, file_hash: str, image_hash: str) -> dict:
        return {
            "resource": resource,
            "image": image,
            "tree": tree,
            "file_sha256": file_hash,
            "image_sha256": image_hash,
        }

    def test_map_relations_preserve_mob_placements_npc_type_and_portal_sentinels(self) -> None:
        context = self._context(
            ["Packs/Mob_00000.ms", "Map/Map/Map0_000.wz", "Npc/Npc_000.wz"]
        )
        fixtures = [
            self._image(
                "Packs/Mob_00000.ms",
                "Mob/0000900.img",
                {"info": {"maxHP": 200, "unlisted": "kept"}},
                "1" * 64,
                "2" * 64,
            ),
            self._image(
                "Map/Map/Map0_000.wz",
                "Map0/010002031.img",
                {
                    "info": {"fieldLimit": 4, "unlistedMapInfo": "kept"},
                    "life": {
                        "0": {"type": "m", "id": "0000900", "x": "-6", "y": 24, "f": 0},
                        "1": {"type": "n", "id": 12, "x": "7", "y": -8},
                        "2": {"type": "r", "id": "not-an-actor"},
                    },
                    "portal": {
                        "0": {"pn": "sp", "tm": 999999999, "tn": "start"},
                        "1": {"pn": "next", "tm": "010002032", "tn": "end"},
                        "2": {"pn": "invalid", "tm": True},
                    },
                },
                "3" * 64,
                "4" * 64,
            ),
            self._image(
                "Npc/Npc_000.wz",
                "0000012.img",
                {
                    "info": {
                        "speak": {"0": "Welcome to the town."},
                        "script": {"$type": "UOL", "value": "../dialog/12"},
                    }
                },
                "5" * 64,
                "6" * 64,
            ),
            self._image(
                "Npc/Npc_000.wz",
                "not-an-id.img",
                {"info": {"speak": {"0": "unrelated"}}},
                "5" * 64,
                "7" * 64,
            ),
        ]
        with patch.object(cw_common, "iter_images", side_effect=self._iter_fixture_images(fixtures)):
            result = cw_extract_actors.extract(context)

        mob = next(item for item in result["entities"] if item["kind"] == "mob")
        map_entity = next(item for item in result["entities"] if item["kind"] == "map")
        npc = next(item for item in result["entities"] if item["kind"] == "npc")
        self.assertEqual(mob["normalized_id"], "900")
        self.assertEqual(mob["fields"]["static_info"], {"info/maxHP": 200, "info/unlisted": "kept"})
        self.assertEqual(map_entity["fields"]["static_info"]["info/unlistedMapInfo"], "kept")
        self.assertEqual(npc["normalized_id"], "12")
        self.assertIsNone(npc["name"])
        self.assertEqual(npc["fields"]["static_info"]["info/speak/0"], "Welcome to the town.")
        self.assertEqual(npc["fields"]["raw"]["info"]["script"], {"$type": "UOL", "value": "../dialog/12"})

        expected_raw_mob = {"f": 0, "id": "0000900", "type": "m", "x": "-6", "y": 24}
        self.assertEqual(
            result["placements"],
            [
                {
                    "map_id": "10002031",
                    "node_id": "0",
                    "mob_id": "900",
                    "x": -6,
                    "y": 24,
                    "raw_json": json.dumps(expected_raw_mob, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                }
            ],
        )
        relations = {(item["relation"], item["target_id"]): item for item in result["relations"]}
        self.assertEqual(set(relations), {
            ("map_mob", "900"),
            ("map_npc", "12"),
            ("portal_target", "999999999"),
            ("portal_target", "10002032"),
        })
        self.assertEqual(relations[("map_mob", "900")]["node_path"], "life/0")
        self.assertEqual(relations[("map_mob", "900")]["raw"], expected_raw_mob)
        self.assertEqual(relations[("map_npc", "12")]["node_path"], "life/1")
        sentinel = relations[("portal_target", "999999999")]
        self.assertEqual(sentinel["raw"]["tm"], 999999999)
        self.assertEqual(sentinel["node_path"], "portal/0")
        self.assertEqual(sentinel["sources"][0]["resource"], "Map/Map/Map0_000.wz")
        self.assertEqual(sentinel["sources"][0]["image"], "Map0/010002031.img")
        self.assertEqual(sentinel["sources"][0]["node"], "portal/0")
        self.assertEqual(result["coverage"]["npc"]["parsed_images"], 1)

    def test_absent_images_and_valid_empty_definitions_have_distinct_coverage(self) -> None:
        context = self._context(["Packs/Mob_00000.ms", "Npc/Npc_000.wz"])
        fixtures = [
            self._image("Packs/Mob_00000.ms", "Mob/0000001.img", {}, "1" * 64, "2" * 64),
            self._image("Packs/Mob_00000.ms", "Mob/unrelated.img", {"info": {"x": 1}}, "1" * 64, "3" * 64),
        ]
        with patch.object(cw_common, "iter_images", side_effect=self._iter_fixture_images(fixtures)):
            result = cw_extract_actors.extract(context)

        mob_coverage = result["coverage"]["mob"]
        self.assertEqual(mob_coverage, {"selected_files": 1, "parsed_images": 1, "static_records": 1, "empty_images": 1})
        mob = next(item for item in result["entities"] if item["kind"] == "mob")
        self.assertEqual(mob["fields"]["raw"], {})
        self.assertIn(("mob", "source_present_empty"), {(gap["kind"], gap["category"]) for gap in result["gaps"]})
        self.assertIn(("map", "source_absent"), {(gap["kind"], gap["category"]) for gap in result["gaps"]})
        self.assertIn(("npc", "image_absent"), {(gap["kind"], gap["category"]) for gap in result["gaps"]})
        self.assertEqual(result["coverage"]["npc"]["selected_files"], 1)
        self.assertEqual(result["coverage"]["npc"]["parsed_images"], 0)


if __name__ == "__main__":
    unittest.main()
