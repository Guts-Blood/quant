import hashlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from experiments.market_snapshot import read_manifest, restore_file
from experiments.market_store import publish, restore


class MarketStoreTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "data"
        self.source.mkdir()
        (self.source / "sample.csv").write_bytes(b"abc")
        self.spec = {"name": "sample.csv", "bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest(),
                     "source": None, "drive_parts": ["part1", "part2"]}
        self.manifest = self.root / "manifest.json"
        self.write_manifest()

    def write_manifest(self):
        self.manifest.write_text(json.dumps({"snapshot": "v1", "files": [self.spec]}))

    def test_unsafe_part_rejected(self):
        self.spec["drive_parts"] = ["../sample.csv"]
        self.write_manifest()
        with self.assertRaises(ValueError):
            read_manifest(self.manifest)

    def test_bad_chunk_preserves_existing_file(self):
        (self.source / "sample.csv").write_bytes(b"old")
        (self.root / "part1").write_bytes(b"a")
        (self.root / "part2").write_bytes(b"bx")
        with self.assertRaisesRegex(ValueError, "mismatch"):
            restore_file(self.spec, self.source, self.root)
        self.assertEqual((self.source / "sample.csv").read_bytes(), b"old")

    @unittest.skipUnless(shutil.which("rclone"), "requires rclone")
    def test_publish_retry_restore_and_exclude_unlisted(self):
        store = str(self.root / "store")
        (self.source / "other.txt").write_text("unlisted")
        remote = publish(self.source, self.manifest, store)
        self.assertEqual(publish(self.source, self.manifest, store), remote)
        self.assertEqual({p.name for p in Path(remote).iterdir()}, {"sample.csv", "manifest.json"})
        output = self.root / "restore"
        restore(remote, output, None, False)
        self.assertEqual((output / "sample.csv").read_bytes(), b"abc")
        (Path(remote) / "manifest.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            restore(remote, output, None, False)
