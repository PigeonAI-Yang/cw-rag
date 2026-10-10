from __future__ import annotations

from collections import Counter
from contextlib import closing, redirect_stdout
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cw_rag


class TestCwSchema2(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.data_dir = self.root / "schema2"
        self.data_dir.mkdir()
        self.database = self.data_dir / "catalog.sqlite"
        snapshot = self.schema2_snapshot()
        temporary = self.data_dir / ".fixture-catalog.tmp"
        cw_rag._write_database(self.database, temporary, snapshot)
        temporary.replace(self.database)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def evidence(number: int, *, resource: str | None = None) -> dict[str, str]:
        digest = f"{number:064x}"
        return {
            "role": "static",
            "resource": resource or f"Item/Weapon/{number}.img",
            "image": f"{number}.img",
            "node": "info",
            "source_id": str(number),
            "file_sha256": digest,
            "image_sha256": digest,
        }

    def schema2_snapshot(self, *, source_fingerprint: str = "c" * 64, parser_revision: str = "d" * 64) -> dict:
        long_text = "Readable description " + ("z" * 8200)
        equipment_sources = [self.evidence(i, resource="R" * 5000 if i == 1 else None) for i in range(1, 6)]
        entities = [
            {
                "kind": "equipment", "normalized_id": "1302000", "original_id": "01302000", "name": "Example Sword",
                "fields": {
                    "family": "Weapon",
                    "section": "equipment",
                    "name_info": {"long": long_text, "description": "Sharp bright blade"},
                    "static_info": {"info/reqLevel": 35, "info/reqJob": -1, "info/incWAT": 17},
                    "item_effects": {"spec/hp": 100},
                    "raw": {"search_excluded": "OnlyRawHedge"},
                },
                "sources": equipment_sources,
            },
            {
                "kind": "equipment", "normalized_id": "1302001", "original_id": "01302001", "name": "Unknown Weapon",
                "fields": {"family": "Weapon", "static_info": {"info/reqLevel": "formula", "info/reqJob": True}, "raw": {"reqLevel": "formula"}},
                "sources": [self.evidence(7)],
            },
            {
                "kind": "item", "normalized_id": "2000000", "original_id": "02000000", "name": "Red Potion",
                "fields": {"family": "Consume", "section": "pets", "item_effects": {"spec/hp": 100}, "static_info": {}},
                "sources": [self.evidence(8)],
            },
            {
                "kind": "mob", "normalized_id": "900", "original_id": "0000900", "name": "Slime",
                "fields": {"static_info": {"info/level": 5, "info/maxHP": 100, "info/exp": 3, "info/PADamage": 2}},
                "sources": [self.evidence(9)],
            },
            {
                "kind": "map", "normalized_id": "1000", "original_id": "000001000", "name": "Training Field",
                "fields": {"placements": {"life/0/id": "900", "life/0/type": "m"}, "portals": {"portal/0/tm": "999"}},
                "sources": [self.evidence(10)],
            },
            {
                "kind": "skill", "normalized_id": "1000000", "original_id": "01000000", "name": "Quick Study",
                "fields": {
                    "name_info": {"description-1": "First level source text", "h2": "Fallback level two"},
                    "skill_static": {
                        "common": {"hs": "shared", "common_value": 4},
                        "level": {
                            "1": {"hs": "description-1", "damage": 12, "caption": "explicit level"},
                            "2": {"hs": "unmapped", "damage": "formula"},
                        },
                        "req": {"2000000": 3},
                    },
                    "skill_resource_group": "100",
                },
                "sources": [self.evidence(11), self.evidence(12), self.evidence(13), self.evidence(14)],
            },
            {
                "kind": "skill", "normalized_id": "2000000", "original_id": "02000000", "name": "Required Skill",
                "fields": {"skill_static": {"level": {"1": {"damage": 1}}}},
                "sources": [self.evidence(15)],
            },
            {
                "kind": "npc", "normalized_id": "300", "original_id": "0000300", "name": "Guide",
                "fields": {"static_info": {"info/speak": "Welcome"}}, "sources": [self.evidence(16)],
            },
            {
                "kind": "quest", "normalized_id": "400", "original_id": "0000400", "name": "First Errand",
                "fields": {"quest_info": {"job_GL": -1}, "quest_check": {"0": {"item": {"2000000": -2}}}},
                "sources": [self.evidence(17)],
            },
            {"kind": "set", "normalized_id": "1", "original_id": "1", "name": None, "fields": {"set_data": {}}, "sources": [self.evidence(18)]},
            {"kind": "recipe", "normalized_id": "100/2", "original_id": "0100/0002", "name": None, "fields": {"recipe_group": "0100", "recipe_entry": "0002", "recipe_data": {}}, "sources": [self.evidence(19)]},
        ]
        entity_counts = {kind: 0 for kind in cw_rag.KINDS}
        entity_counts.update(Counter(entity["kind"] for entity in entities))
        relation_source = self.evidence(30)
        long_raw = {f"part/{index}": index for index in range(20)}
        relations = [
            {"source_kind": "map", "source_id": "1000", "relation": "map_mob", "target_kind": "mob", "target_id": "900", "node_path": "life/0", "raw": {"id": "900", "type": "m"}, "sources": [relation_source]},
            {"source_kind": "map", "source_id": "1000", "relation": "map_npc", "target_kind": "npc", "target_id": "999", "node_path": "life/1", "raw": long_raw, "sources": [self.evidence(31), self.evidence(32)]},
            {"source_kind": "skill", "source_id": "1000000", "relation": "skill_req", "target_kind": "skill", "target_id": "2000000", "node_path": "skill/1000000/req/2000000", "raw": {"required_level": 3}, "sources": [self.evidence(33)]},
            {"source_kind": "quest", "source_id": "400", "relation": "quest_check_item", "target_kind": "item_ref", "target_id": "9", "node_path": "Check/0/item/9", "raw": {"count": -1}, "sources": [self.evidence(34)]},
        ]
        return {
            "schema_version": "2", "edition": "classic-world", "region": "GLOBAL", "locale": "en",
            "source_fingerprint": source_fingerprint, "parser_revision": parser_revision, "built_at": "2026-10-10T00:00:00Z",
            "source_files": [], "entity_counts": entity_counts, "placement_count": 1,
            "coverage": {"entities": entity_counts, "gaps": [{"kind": "fixture", "category": str(index), "count": 5, "examples": ["a", "b", "c", "d", "e"]} for index in range(55)]},
            "entities": entities, "placements": [{"map_id": "1000", "node_id": "0", "mob_id": "900", "x": None, "y": None, "raw_json": "{\"id\":\"900\",\"type\":\"m\"}"}],
            "relations": relations,
        }

    @staticmethod
    def legacy_snapshot() -> dict:
        return {
            "schema_version": "1", "edition": "classic-world", "region": "GLOBAL", "locale": "en",
            "source_fingerprint": "a" * 64, "parser_revision": "b" * 64, "built_at": "2026-01-01T00:00:00Z",
            "source_files": [], "entity_counts": {"mob": 1, "map": 1}, "placement_count": 1,
            "coverage": {"entities": {"mob": 1, "map": 1}, "gaps": []},
            "entities": [
                {"kind": "mob", "normalized_id": "900", "original_id": "0000900", "name": "Slime", "fields": {"name_info": {"note": "Safe search"}}, "sources": []},
                {"kind": "map", "normalized_id": "10002031", "original_id": "010002031", "name": "Southern Forest", "fields": {"placements": {"life/0/id": "0000900", "life/0/type": "m"}, "portals": {"portal/0/pn": "sp"}}, "sources": []},
            ],
            "placements": [{"map_id": "10002031", "node_id": "0", "mob_id": "900", "x": None, "y": None, "raw_json": "{\"id\":\"0000900\",\"type\":\"m\"}"}],
        }

    def write_legacy_catalog(self, data_dir: Path) -> Path:
        data_dir.mkdir(parents=True, exist_ok=True)
        database = data_dir / "catalog.sqlite"
        temporary = data_dir / ".legacy-fixture.tmp"
        cw_rag._write_database(database, temporary, self.legacy_snapshot())
        temporary.replace(database)
        return database

    def query(self, **overrides):
        options = {
            "kind": None, "entity_id": None, "text": None, "min_level": None, "max_level": None,
            "max_hp": None, "limit": 10, "related": False,
        }
        options.update(overrides)
        return cw_rag.query_index(self.data_dir, **options)

    def test_equipment_projections_keep_raw_fields_and_compact_sources(self) -> None:
        result = self.query(kind="equipment", family="Weapon", min_req_level=35, max_req_level=35, req_job_raw=-1)
        self.assertEqual(result["total"], 1)
        match = result["matches"][0]
        self.assertEqual(match["family"], "Weapon")
        self.assertEqual(match["stats"], {"req_level": 35, "req_job": -1})
        self.assertEqual(match["source_count"], 5)
        self.assertEqual(len(match["sources"]), 3)
        self.assertTrue(match["sources_truncated"])
        self.assertTrue(all(len(source["resource"]) <= 4096 for source in match["sources"]))
        self.assertTrue(match["detail_available"])
        self.assertNotIn("fields", match)
        self.assertNotIn("RawSecretToken", json.dumps(match))
        nulls = self.query(kind="equipment", family="Weapon", min_req_level=0)
        self.assertEqual(nulls["total"], 1)
        self.assertEqual(nulls["matches"][0]["id"], "1302000")
        raw_boolean = self.query(kind="equipment", req_job_raw=1)
        self.assertEqual(raw_boolean["total"], 0)

    def test_cli_accepts_raw_negative_job_and_punctuation_is_not_fts_syntax(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cw_rag.main([
                "query", "--data-dir", str(self.data_dir), "--kind", "equipment", "--id", "01302000",
                "--family", "Weapon", "--min-req-level", "35", "--req-job-raw", "-1",
            ])
        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["matches"][0]["stats"]["req_job"], -1)
        self.assertEqual(self.query(kind="equipment", text='Sword" OR *')["total"], 0)
        self.assertEqual(self.query(text="OnlyRawHedge")["total"], 0)
        with self.assertRaisesRegex(cw_rag.CwRagError, "32 quoted tokens"):
            self.query(text=" ".join(["token"] * 33))

    def test_detail_paths_page_leaves_and_resume_long_strings(self) -> None:
        first = self.query(kind="equipment", entity_id="1302000", detail=True, field="fields/name_info/long", limit=1)
        field = first["matches"][0]["detail"]["fields"][0]
        self.assertEqual(field["path"], "fields/name_info/long")
        self.assertEqual(len(field["value"]), 4096)
        self.assertEqual(field["original_length"], 8221)
        self.assertEqual(field["value_offset"], 0)
        self.assertEqual(field["value_next_offset"], 4096)
        second = self.query(kind="equipment", entity_id="1302000", detail=True, field="fields/name_info/long", value_offset=4096, limit=1)
        resumed = second["matches"][0]["detail"]["fields"][0]
        self.assertEqual(resumed["value_offset"], 4096)
        self.assertEqual(resumed["value"], "z" * 4096)
        page = self.query(kind="equipment", entity_id="1302000", detail=True, field_prefix="fields/static_info/info/req", limit=1)
        detail = page["matches"][0]["detail"]
        self.assertEqual(detail["field_total"], 2)
        self.assertEqual(detail["field_returned"], 1)
        self.assertTrue(detail["field_truncated"])
        self.assertEqual(detail["field_next_offset"], 1)
        with self.assertRaisesRegex(cw_rag.CwRagError, "stored value is a string"):
            self.query(kind="equipment", entity_id="1302000", detail=True, field="fields/static_info/info/reqLevel", value_offset=0)

    def test_skill_level_preserves_explicit_mixed_values_and_raw_description_key(self) -> None:
        result = self.query(kind="skill", entity_id="01000000", skill_level=1, limit=20)
        skill_level = result["matches"][0]["skill_level"]
        self.assertTrue(skill_level["explicit_level_present"])
        self.assertEqual(skill_level["description_key"], "description-1")
        self.assertEqual(skill_level["common_available_prefix"], "fields/skill_static/common")
        fields = {item["path"]: item["value"] for item in skill_level["fields"]}
        self.assertEqual(fields["fields/skill_static/level/1/damage"], 12)
        self.assertEqual(fields["fields/skill_static/level/1/caption"], "explicit level")
        self.assertEqual(fields["fields/name_info/description-1"], "First level source text")
        fallback = self.query(kind="skill", entity_id="1000000", skill_level=2, limit=20)["matches"][0]["skill_level"]
        self.assertEqual(fallback["description_key"], "h2")
        absent = self.query(kind="skill", entity_id="1000000", skill_level=3)["matches"][0]["skill_level"]
        self.assertFalse(absent["explicit_level_present"])
        self.assertEqual(absent["fields"], [])
        common = self.query(kind="skill", entity_id="1000000", detail=True, field_prefix="fields/skill_static/common", limit=10)
        self.assertIn("fields/skill_static/common/common_value", {item["path"] for item in common["matches"][0]["detail"]["fields"]})

    def test_relations_show_unresolved_targets_and_bound_raw_summary(self) -> None:
        page = self.query(kind="map", entity_id="1000", related=True, limit=1)
        outgoing = page["matches"][0]["related"]["outgoing"]
        self.assertEqual(outgoing["total"], 2)
        self.assertTrue(outgoing["truncated"])
        self.assertEqual(outgoing["next_offset"], 1)
        self.assertEqual(outgoing["matches"][0]["relation"], "map_mob")
        next_page = self.query(kind="map", entity_id="1000", related=True, related_offset=1, limit=1)["matches"][0]["related"]["outgoing"]
        unresolved = next_page["matches"][0]
        self.assertEqual(unresolved["relation"], "map_npc")
        self.assertFalse(unresolved["resolved"])
        self.assertEqual(unresolved["target"]["id"], "999")
        self.assertEqual(unresolved["target"]["name"], None)
        self.assertEqual(unresolved["raw"]["field_total"], 20)
        self.assertEqual(len(unresolved["raw"]["fields"]), 16)
        self.assertTrue(unresolved["raw"]["field_truncated"])
        self.assertEqual(page["matches"][0]["related"]["mobs"]["matches"][0]["id"], "900")
        incoming = self.query(kind="mob", entity_id="900", related=True)["matches"][0]["related"]["incoming"]
        self.assertTrue(incoming["matches"][0]["resolved"])
        with self.assertRaisesRegex(cw_rag.CwRagError, "requires --related"):
            self.query(kind="map", entity_id="1000", related_offset=0)

    def test_status_gap_examples_and_groups_are_bounded(self) -> None:
        status = cw_rag.status_index(self.data_dir)
        coverage = status["snapshot"]["coverage"]
        self.assertEqual(len(coverage["gaps"]), 50)
        self.assertEqual(coverage["gaps_omitted"], 5)
        self.assertEqual(len(coverage["gaps"][0]["examples"]), 3)
        self.assertEqual(coverage["gaps"][0]["examples_omitted"], 2)
        self.assertEqual(status["schema_version"], 2)
        self.assertFalse(status["rebuild_required"])

    def test_schema1_remains_readable_and_new_options_require_rebuild(self) -> None:
        legacy_dir = self.root / "legacy"
        self.write_legacy_catalog(legacy_dir)
        status = cw_rag.status_index(legacy_dir)
        self.assertEqual(status["schema_version"], 1)
        self.assertTrue(status["legacy_snapshot"])
        self.assertTrue(status["rebuild_required"])
        old_map = cw_rag.query_index(legacy_dir, kind="map", entity_id="010002031", text=None, min_level=None, max_level=None, max_hp=None, limit=10, related=False)
        self.assertIn("placements", old_map["matches"][0]["fields"])
        with self.assertRaisesRegex(cw_rag.CwRagError, "schema 2 rebuild"):
            cw_rag.query_index(legacy_dir, kind="equipment", entity_id=None, text=None, min_level=None, max_level=None, max_hp=None, limit=10, related=False)
        with self.assertRaisesRegex(cw_rag.CwRagError, "schema 2 rebuild"):
            cw_rag.query_index(legacy_dir, kind="equipment", entity_id=None, text=None, min_level=None, max_level=None, max_hp=None, limit=10, related=False, family="Weapon")

    def client_fixture(self) -> Path:
        client = self.root / "client"
        for relative in ("String", "Packs", "Map/Map"):
            (client / "Data" / relative).mkdir(parents=True, exist_ok=True)
        (client / "Data" / "String" / "String_000.wz").write_bytes(b"fixture")
        (client / "Data" / "Packs" / "Mob_00000.ms").write_bytes(b"fixture")
        (client / "Data" / "Map" / "Map" / "Map0_000.wz").write_bytes(b"fixture")
        return client

    def test_schema1_build_transition_creates_verified_backup(self) -> None:
        client = self.client_fixture()
        legacy_database = self.write_legacy_catalog(self.data_dir)
        old_hash = hashlib.sha256(legacy_database.read_bytes()).hexdigest()
        prepared = {"source_fingerprint": "e" * 64, "parser_revision": "f" * 64}
        new_snapshot = self.schema2_snapshot(source_fingerprint=prepared["source_fingerprint"], parser_revision=prepared["parser_revision"])
        with patch.object(cw_rag, "prepare_source", return_value=prepared), patch.object(cw_rag, "extract_snapshot", return_value=new_snapshot), patch.object(cw_rag, "verify_source_unchanged"):
            result = cw_rag.build_index(client, self.data_dir)
        backup = Path(result["legacy_backup"])
        self.assertEqual(result["status"], "built")
        self.assertEqual(backup.name, f"catalog.schema1.{old_hash}.sqlite")
        self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), old_hash)
        self.assertEqual(cw_rag.status_index(self.data_dir)["schema_version"], 2)

    def test_failed_rebuild_preserves_legacy_hash_and_cleans_stage(self) -> None:
        client = self.client_fixture()
        legacy_database = self.write_legacy_catalog(self.data_dir)
        old_hash = hashlib.sha256(legacy_database.read_bytes()).hexdigest()
        prepared = {"source_fingerprint": "e" * 64, "parser_revision": "f" * 64}
        with patch.object(cw_rag, "prepare_source", return_value=prepared), patch.object(cw_rag, "extract_snapshot", side_effect=RuntimeError("fixture extraction failure")), patch.object(cw_rag, "verify_source_unchanged"):
            with self.assertRaisesRegex(RuntimeError, "fixture extraction failure"):
                cw_rag.build_index(client, self.data_dir)
        self.assertEqual(hashlib.sha256(legacy_database.read_bytes()).hexdigest(), old_hash)
        self.assertFalse(any(path.name.startswith(".catalog-") for path in self.data_dir.iterdir()))
        self.assertFalse(any(path.name.startswith("catalog.schema1.") for path in self.data_dir.iterdir()))

    def test_failed_atomic_replacement_preserves_legacy_and_verified_backup(self) -> None:
        client = self.client_fixture()
        legacy_database = self.write_legacy_catalog(self.data_dir)
        old_hash = hashlib.sha256(legacy_database.read_bytes()).hexdigest()
        prepared = {"source_fingerprint": "e" * 64, "parser_revision": "f" * 64}
        new_snapshot = self.schema2_snapshot(source_fingerprint=prepared["source_fingerprint"], parser_revision=prepared["parser_revision"])
        real_replace = cw_rag.os.replace

        def fail_catalog_replace(source, destination):
            if Path(destination) == legacy_database:
                raise OSError("fixture replacement failure")
            return real_replace(source, destination)

        with patch.object(cw_rag, "prepare_source", return_value=prepared), patch.object(cw_rag, "extract_snapshot", return_value=new_snapshot), patch.object(cw_rag, "verify_source_unchanged"), patch.object(cw_rag.os, "replace", side_effect=fail_catalog_replace):
            with self.assertRaisesRegex(OSError, "fixture replacement failure"):
                cw_rag.build_index(client, self.data_dir)
        self.assertEqual(hashlib.sha256(legacy_database.read_bytes()).hexdigest(), old_hash)
        backup = self.data_dir / f"catalog.schema1.{old_hash}.sqlite"
        self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), old_hash)
        self.assertFalse(any(path.name.startswith(".catalog-") for path in self.data_dir.iterdir()))

    def test_all_kinds_compound_recipe_and_snapshot_queries_are_read_only(self) -> None:
        old_hash = hashlib.sha256(self.database.read_bytes()).hexdigest()
        for kind in cw_rag.KINDS:
            with self.subTest(kind=kind):
                result = self.query(kind=kind, limit=1)
                self.assertGreater(result["total"], 0)
                self.assertEqual(result["returned"], 1)
                self.assertEqual(result["matches"][0]["type"], kind)
        recipe = self.query(kind="recipe", entity_id="0100/0002", detail=True)
        self.assertEqual(recipe["matches"][0]["id"], "100/2")
        self.assertEqual(recipe["matches"][0]["original_id"], "0100/0002")
        self.assertEqual(self.query(text="bright")["matches"][0]["id"], "1302000")
        cw_rag.status_index(self.data_dir)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), old_hash)
        self.assertFalse(self.database.with_name("catalog.sqlite-wal").exists())

    def test_detail_pages_reassemble_sources_and_literal_paths(self) -> None:
        complete = self.query(kind="equipment", entity_id="1302000", detail=True, limit=50)["matches"][0]["detail"]
        expected = complete["fields"]
        received = []
        offset = 0
        while True:
            page = self.query(kind="equipment", entity_id="1302000", detail=True, limit=3, offset=offset)["matches"][0]["detail"]
            received.extend(page["fields"])
            if page["field_next_offset"] is None:
                break
            self.assertGreater(page["field_next_offset"], offset)
            offset = page["field_next_offset"]
        self.assertEqual(received, expected)
        paths = [leaf["path"] for leaf in received]
        self.assertEqual(paths, sorted(set(paths)))
        self.assertIn("sources/4/source_id", paths)
        literal = self.query(kind="equipment", entity_id="1302000", detail=True, field_prefix="fields/name_info/%")
        self.assertEqual(literal["matches"][0]["detail"]["field_total"], 0)
        with self.assertRaisesRegex(cw_rag.CwRagError, "stored value is a string"):
            self.query(kind="equipment", entity_id="1302000", detail=True, field="fields/missing", value_offset=0)

    def test_cli_help_and_argument_errors_are_bounded_json(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as exited:
            cw_rag.main(["query", "--help"])
        self.assertEqual(exited.exception.code, 0)
        for flag in ("--family", "--min-req-level", "--max-req-level", "--req-job-raw", "--skill-level", "--detail", "--field-prefix", "--field", "--value-offset", "--offset", "--related-offset"):
            self.assertIn(flag, output.getvalue())
        invalid_options = [
            ["--kind", "equipment", "--min-req-level", "36", "--max-req-level", "35"],
            ["--kind", "item", "--min-level", "1"],
            ["--kind", "npc", "--family", "Weapon"],
            ["--kind", "recipe", "--id", "2"],
            ["--skill-level", "1"], ["--detail"],
            ["--related-offset", "0"], ["--value-offset", "0"],
            ["--kind", "equipment", "--id", "1302000", "--detail", "--field", "a", "--field-prefix", "b"],
            ["--limit", "51"], ["--kind", "unsupported"],
            ["--req-job-raw", str(2 ** 63)], ["--text", "a" * 513],
        ]
        for options in invalid_options:
            with self.subTest(options=options):
                output = io.StringIO()
                with redirect_stdout(output):
                    try:
                        exit_code = cw_rag.main(["query", "--data-dir", str(self.data_dir), *options])
                    except SystemExit as error:
                        exit_code = error.code
                self.assertEqual(exit_code, 2)
                payload = json.loads(output.getvalue())
                self.assertEqual(payload["status"], "error")
                self.assertLessEqual(len(payload["reason"]), 4096)

    def test_failed_final_source_verification_preserves_old_snapshot(self) -> None:
        client = self.client_fixture()
        old_hash = hashlib.sha256(self.database.read_bytes()).hexdigest()
        prepared = {"source_fingerprint": "e" * 64, "parser_revision": "f" * 64}
        snapshot = self.schema2_snapshot(source_fingerprint=prepared["source_fingerprint"], parser_revision=prepared["parser_revision"])
        with patch.object(cw_rag, "prepare_source", return_value=prepared), patch.object(cw_rag, "extract_snapshot", return_value=snapshot), patch.object(cw_rag, "verify_source_unchanged", side_effect=[None, cw_rag.SourceError("selected source changed")]):
            with self.assertRaisesRegex(cw_rag.SourceError, "selected source changed"):
                cw_rag.build_index(client, self.data_dir)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), old_hash)
        self.assertFalse(any(path.name.startswith(".catalog-") for path in self.data_dir.iterdir()))

    def test_conflicting_legacy_backup_prevents_replacement(self) -> None:
        client = self.client_fixture()
        legacy = self.write_legacy_catalog(self.data_dir)
        old_hash = hashlib.sha256(legacy.read_bytes()).hexdigest()
        backup = self.data_dir / f"catalog.schema1.{old_hash}.sqlite"
        backup.write_bytes(b"existing different contents")
        prepared = {"source_fingerprint": "e" * 64, "parser_revision": "f" * 64}
        snapshot = self.schema2_snapshot(source_fingerprint=prepared["source_fingerprint"], parser_revision=prepared["parser_revision"])
        with patch.object(cw_rag, "prepare_source", return_value=prepared), patch.object(cw_rag, "extract_snapshot", return_value=snapshot), patch.object(cw_rag, "verify_source_unchanged"):
            with self.assertRaisesRegex(cw_rag.CwRagError, "different contents"):
                cw_rag.build_index(client, self.data_dir)
        self.assertEqual(hashlib.sha256(legacy.read_bytes()).hexdigest(), old_hash)
        self.assertEqual(backup.read_bytes(), b"existing different contents")
        self.assertFalse(any(path.name.startswith(".catalog-") for path in self.data_dir.iterdir()))

    def test_wrong_identity_unknown_schema_and_projection_mismatch_are_rejected(self) -> None:
        for key, value in (("edition", "cn"), ("schema_version", "99"), ("placement_count", "99")):
            with self.subTest(key=key):
                with closing(sqlite3.connect(self.database)) as connection, connection:
                    original = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()[0]
                    connection.execute("UPDATE metadata SET value=? WHERE key=?", (value, key))
                with self.assertRaises(cw_rag.CwRagError):
                    cw_rag.status_index(self.data_dir)
                with closing(sqlite3.connect(self.database)) as connection, connection:
                    connection.execute("UPDATE metadata SET value=? WHERE key=?", (original, key))
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE placements SET mob_id='901'")
        with self.assertRaisesRegex(cw_rag.CwRagError, "placements projection"):
            cw_rag.status_index(self.data_dir)

    def test_no_change_validates_sources_without_extracting_or_writing(self) -> None:
        client = self.client_fixture()
        old_hash = hashlib.sha256(self.database.read_bytes()).hexdigest()
        prepared = {"source_fingerprint": "c" * 64, "parser_revision": "d" * 64}
        with patch.object(cw_rag, "prepare_source", return_value=prepared), patch.object(cw_rag, "extract_snapshot") as extract, patch.object(cw_rag, "verify_source_unchanged") as verify:
            result = cw_rag.build_index(client, self.data_dir)
        self.assertEqual(result["status"], "no_change")
        extract.assert_not_called()
        verify.assert_called_once_with(prepared)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), old_hash)

    def test_invalid_staged_relation_preserves_old_database(self) -> None:
        client = self.client_fixture()
        old_hash = hashlib.sha256(self.database.read_bytes()).hexdigest()
        prepared = {"source_fingerprint": "e" * 64, "parser_revision": "f" * 64}
        snapshot = self.schema2_snapshot(source_fingerprint=prepared["source_fingerprint"], parser_revision=prepared["parser_revision"])
        snapshot["relations"][0]["source_id"] = "missing"
        with patch.object(cw_rag, "prepare_source", return_value=prepared), patch.object(cw_rag, "extract_snapshot", return_value=snapshot), patch.object(cw_rag, "verify_source_unchanged"):
            with self.assertRaisesRegex(cw_rag.CwRagError, "FOREIGN KEY"):
                cw_rag.build_index(client, self.data_dir)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), old_hash)
        self.assertFalse(any(path.name.startswith(".catalog-") for path in self.data_dir.iterdir()))

    def test_failed_postpublication_readback_reports_uncertainty_and_backup(self) -> None:
        client = self.client_fixture()
        legacy = self.write_legacy_catalog(self.data_dir)
        old_hash = hashlib.sha256(legacy.read_bytes()).hexdigest()
        prepared = {"source_fingerprint": "e" * 64, "parser_revision": "f" * 64}
        snapshot = self.schema2_snapshot(source_fingerprint=prepared["source_fingerprint"], parser_revision=prepared["parser_revision"])
        real_open = cw_rag._connect_read_only
        opens = 0

        def fail_readback(database):
            nonlocal opens
            opens += 1
            if opens == 2:
                raise cw_rag.CwRagError("fixture readback failure")
            return real_open(database)

        with patch.object(cw_rag, "prepare_source", return_value=prepared), patch.object(cw_rag, "extract_snapshot", return_value=snapshot), patch.object(cw_rag, "verify_source_unchanged"), patch.object(cw_rag, "_connect_read_only", side_effect=fail_readback):
            with self.assertRaisesRegex(cw_rag.CwRagError, "publication/readback is uncertain.*schema 1 backup"):
                cw_rag.build_index(client, self.data_dir)
        backup = self.data_dir / f"catalog.schema1.{old_hash}.sqlite"
        self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), old_hash)
        self.assertEqual(cw_rag.status_index(self.data_dir)["schema_version"], 2)

    def test_null_array_raw_and_overflow_values_remain_lossless_detail(self) -> None:
        snapshot = self.schema2_snapshot()
        equipment = snapshot["entities"][0]
        equipment["fields"]["raw"]["mixed"] = [None, True, "formula", 2 ** 70]
        equipment["fields"]["static_info"]["info/reqLevel"] = str(2 ** 70)
        temporary = self.data_dir / ".extended-fixture.tmp"
        cw_rag._write_database(self.database, temporary, snapshot)
        temporary.replace(self.database)
        self.assertEqual(self.query(kind="equipment", min_req_level=0)["total"], 0)
        detail = self.query(kind="equipment", entity_id="1302000", detail=True, field_prefix="fields/raw/mixed")["matches"][0]["detail"]
        self.assertEqual([leaf["value"] for leaf in detail["fields"]], [None, True, "formula", 2 ** 70])
        self.assertEqual(detail["field_total"], 4)


if __name__ == "__main__":
    unittest.main()
