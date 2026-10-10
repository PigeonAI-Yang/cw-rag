from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cw_rag


class TestCwRag(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.data_dir = self.root / "index"
        self.data_dir.mkdir()
        self.database = self.data_dir / "catalog.sqlite"
        snapshot = {
            "schema_version": "1",
            "edition": "classic-world",
            "region": "GLOBAL",
            "locale": "en",
            "source_fingerprint": "a" * 64,
            "parser_revision": "b" * 64,
            "built_at": "2026-01-01T00:00:00Z",
            "source_files": [],
            "entity_counts": {"mob": 1, "map": 1},
            "placement_count": 1,
            "coverage": {"entities": {"mob": 1, "map": 1}, "gaps": []},
            "entities": [
                {"kind": "mob", "normalized_id": "900", "original_id": "0000900", "name": "Slime", "fields": {"name_info": {"note": "Safe search"}}, "sources": []},
                {"kind": "map", "normalized_id": "10002031", "original_id": "010002031", "name": "Dungeon, Southern Forest I", "fields": {"name_info": {"streetName": "Victoria Road"}, "placements": {"life/0/id": "0000900", "life/0/type": "m"}, "portals": {"portal/0/pn": "sp"}}, "sources": []},
            ],
            "placements": [{"map_id": "10002031", "node_id": "0", "mob_id": "900", "x": None, "y": None, "raw_json": "{\"id\":\"0000900\",\"type\":\"m\"}"}],
        }
        temporary = self.data_dir / ".test-catalog.tmp"
        cw_rag._write_database(self.database, temporary, snapshot)
        temporary.replace(self.database)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_safe_punctuation_in_text_query(self) -> None:
        result = cw_rag.query_index(self.data_dir, kind="mob", entity_id=None, text='Slime" OR *', min_level=None, max_level=None, max_hp=None, limit=10, related=False)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["total"], 0)

    def test_output_under_client_is_rejected_without_writes(self) -> None:
        client = self.root / "client"
        data = client / "Data"
        data.mkdir(parents=True)
        (data / "sample.wz").write_bytes(b"inventory marker")
        output = data / "local-index"
        with self.assertRaises(cw_rag.CwRagError):
            cw_rag.build_index(client, output)
        self.assertFalse(output.exists())
        self.assertEqual(sorted(path.name for path in data.iterdir()), ["sample.wz"])

    def test_malformed_required_source_preserves_old_database(self) -> None:
        client = self.root / "broken-client"
        data = client / "Data"
        for relative in ("String", "Packs", "Map/Map"):
            (data / relative).mkdir(parents=True, exist_ok=True)
        (data / "String" / "String_000.wz").write_bytes(b"not a WZ archive")
        (data / "Packs" / "Mob_00000.ms").write_bytes(b"not an MS archive")
        (data / "Map" / "Map" / "Map0_000.wz").write_bytes(b"not a WZ archive")
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with self.assertRaises(Exception):
            cw_rag.build_index(client, self.data_dir)
        after = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        result = cw_rag.query_index(self.data_dir, kind="mob", entity_id="900", text=None, min_level=None, max_level=None, max_hp=None, limit=10, related=False)
        self.assertEqual(result["matches"][0]["name"], "Slime")
        self.assertFalse(any(path.name.startswith(".catalog-") for path in self.data_dir.iterdir()))

    def test_missing_numeric_fields_are_not_reported_as_zero(self) -> None:
        detail = cw_rag.query_index(self.data_dir, kind="mob", entity_id="900", text=None, min_level=None, max_level=None, max_hp=None, limit=10, related=False)
        self.assertNotIn("stats", detail["matches"][0])
        filtered = cw_rag.query_index(self.data_dir, kind="mob", entity_id=None, text=None, min_level=0, max_level=None, max_hp=None, limit=10, related=False)
        self.assertEqual(filtered["total"], 0)

    def test_selected_source_mutation_is_detected(self) -> None:
        source = self.root / "mutation-client" / "Data"
        for relative in ("String", "Packs", "Map/Map"):
            (source / relative).mkdir(parents=True, exist_ok=True)
        (source / "String" / "String_000.wz").write_bytes(b"source-before")
        (source / "Packs" / "Mob_00000.ms").write_bytes(b"source-before")
        (source / "Map" / "Map" / "Map0_000.wz").write_bytes(b"source-before")
        prepared = cw_rag.prepare_source(source)
        (source / "String" / "String_000.wz").write_bytes(b"source-after")
        with self.assertRaises(cw_rag.SourceError):
            cw_rag.verify_source_unchanged(prepared)


if __name__ == "__main__":
    unittest.main()