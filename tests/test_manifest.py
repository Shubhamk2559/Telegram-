import json
import tempfile
import unittest
from pathlib import Path

from src.manifest import Manifest, ManifestError


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.path = self.ws / "manifests" / "manifest.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_save_load_and_dedup(self):
        m = Manifest(self.path, self.ws)
        self.assertFalse(m.is_completed(-100, 5))
        m.upsert({"archive_id": "-100:5", "status": "completed"})
        m.upsert({"archive_id": "-100:5", "status": "completed", "x": 1})  # upsert, duplicate nahi
        m.save()
        m2 = Manifest(self.path, self.ws)
        self.assertTrue(m2.is_completed(-100, 5))
        self.assertEqual(len(m2.data["archives"]), 1)
        self.assertFalse(m2.is_completed(-100, 6))

    def test_failed_is_not_completed(self):
        m = Manifest(self.path, self.ws)
        m.upsert({"archive_id": "-1:1", "status": "failed"})
        self.assertFalse(m.is_completed(-1, 1))

    def test_corrupt_manifest_is_backed_up_not_overwritten(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{ not json")
        with self.assertRaises(ManifestError):
            Manifest(self.path, self.ws)
        self.assertTrue(any(p.name.startswith("manifest.json.corrupt-") for p in self.path.parent.iterdir()))

    def test_rel_path_and_valid_json(self):
        m = Manifest(self.path, self.ws)
        self.assertEqual(m.rel(self.ws / "extracted" / "a"), "extracted/a")
        m.save()
        self.assertEqual(json.loads(self.path.read_text())["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
