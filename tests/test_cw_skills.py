from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cw_common import SourceError
import cw_extract_skills


class TestCwSkills(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temporary_directory.name)
        self.pack = self.data_root / "Packs" / "Skill_00000.ms"
        self.pack.parent.mkdir()
        self.pack.write_bytes(b"fixture")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _context(self, files: list[Path] | None = None, names: dict | None = None) -> dict:
        selected_files = [self.pack] if files is None else files
        hashes = {path.relative_to(self.data_root).as_posix(): "a" * 64 for path in selected_files}
        return {"data_root": self.data_root, "files": selected_files, "file_hashes": hashes, "names": names or {}}

    def _image(self, tree: dict, *, path: str = "Skill/100.img") -> dict:
        return {
            "resource": "Packs/Skill_00000.ms",
            "image": path,
            "tree": tree,
            "file_sha256": "a" * 64,
            "image_sha256": "b" * 64,
        }

    def _fake_iter_images(self, images: list[dict]):
        def read_selected(context: dict, paths, accept):
            self.assertEqual(list(paths), [self.pack] if self.pack in context["files"] else [])
            return (image for image in images if accept(image["resource"], image["image"]))

        return read_selected

    def test_extracts_lossless_skill_data_and_literal_requirement(self) -> None:
        skill = {
            "info": {"type": 50},
            "common": {"x": "((level * 3) + 7)"},
            "level": {
                "1": {"hs": "h1", "mhpR": "10"},
                "2": {"hs": "h2", "mhpR": 11},
            },
            "req": {"1000000": 3},
        }
        target = {"level": {"1": {"hs": "target", "mhpR": "4"}}}
        tree = {"info": {"type": 1}, "skill": {"1000001": skill, "1000000": target, "metadata": {"ignore": True}}}
        names = {
            ("skill", "1000001"): {
                "original_id": "1000001",
                "name": "Skill Name",
                "name_info": {"bookName": "Warrior Mastery"},
                "sources": [],
            }
        }
        names_before = copy.deepcopy(names)
        context = self._context(names=names)
        image = self._image(tree)
        unrelated_image = self._image({"skill": {"1000002": {}}}, path="Mob/100.img")

        with patch.object(cw_extract_skills, "iter_images", side_effect=self._fake_iter_images([image, unrelated_image])):
            result = cw_extract_skills.extract(context)

        by_id = {record["normalized_id"]: record for record in result["entities"]}
        record = by_id["1000001"]
        self.assertEqual(record["kind"], "skill")
        self.assertEqual(record["original_id"], "1000001")
        self.assertEqual(record["fields"]["skill_static"], skill)
        self.assertEqual(record["fields"]["raw"], skill)
        self.assertEqual(record["fields"]["skill_static"]["common"]["x"], "((level * 3) + 7)")
        self.assertEqual(record["fields"]["skill_static"]["level"]["1"]["mhpR"], "10")
        self.assertIsInstance(record["fields"]["skill_static"]["level"]["2"]["mhpR"], int)
        self.assertEqual(record["fields"]["skill_resource_group"], "100")
        self.assertEqual(record["fields"]["skill_book"], {"group": "100", "title": "Warrior Mastery"})

        self.assertEqual(len(result["relations"]), 1)
        relation = result["relations"][0]
        self.assertEqual(relation["source_kind"], "skill")
        self.assertEqual(relation["source_id"], "1000001")
        self.assertEqual(relation["relation"], "skill_req")
        self.assertEqual(relation["target_kind"], "skill")
        self.assertEqual(relation["target_id"], "1000000")
        self.assertEqual(relation["node_path"], "skill/1000001/req/1000000")
        self.assertEqual(relation["raw"], {"required_level": 3})
        self.assertEqual(relation["sources"][0]["node"], relation["node_path"])
        self.assertEqual(result["coverage"]["skill"], {
            "selected_file_count": 1,
            "parsed_image_count": 1,
            "static_record_count": 2,
        })
        self.assertEqual(names, names_before)

    def test_missing_source_missing_image_and_present_empty_are_distinct(self) -> None:
        absent_context = self._context(files=[])
        with patch.object(cw_extract_skills, "iter_images", side_effect=self._fake_iter_images([])):
            absent = cw_extract_skills.extract(absent_context)
        self.assertEqual(absent["coverage"]["skill"], {
            "selected_file_count": 0,
            "parsed_image_count": 0,
            "static_record_count": 0,
        })
        self.assertEqual(absent["gaps"][0]["category"], "source_absent")

        context = self._context()
        non_skill_image = self._image({"info": {}}, path="Skill/metadata.img")
        with patch.object(cw_extract_skills, "iter_images", side_effect=self._fake_iter_images([non_skill_image])):
            missing_image = cw_extract_skills.extract(context)
        self.assertEqual(missing_image["coverage"]["skill"]["parsed_image_count"], 0)
        self.assertEqual(missing_image["gaps"][0]["category"], "image_absent")

        empty_skill_image = self._image({"skill": {}})
        with patch.object(cw_extract_skills, "iter_images", side_effect=self._fake_iter_images([empty_skill_image])):
            empty = cw_extract_skills.extract(context)
        self.assertEqual(empty["coverage"]["skill"]["parsed_image_count"], 1)
        self.assertEqual(empty["gaps"][0]["category"], "source_present_empty")
        self.assertEqual(empty["gaps"][0]["examples"][0]["image"], "Skill/100.img")

    def test_selected_image_failure_propagates(self) -> None:
        context = self._context()
        with patch.object(cw_extract_skills, "iter_images", side_effect=SourceError("Incomplete MS image")):
            with self.assertRaisesRegex(SourceError, "Incomplete MS image"):
                cw_extract_skills.extract(context)


if __name__ == "__main__":
    unittest.main()
