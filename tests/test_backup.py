#!/usr/bin/env python3
import tempfile
import unittest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from python.backup import BackupManager
from python.config import BackupConfig


class TestPhase4FullBackup(unittest.TestCase):
    """Verify full backup creation, tar/gzip compression, manifest generation, and verification."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="lbm_test_p4_")
        self.base = Path(self.temp_dir.name)
        self.source_dir = self.base / "source project"
        self.dest_dir = self.base / "backups"
        self.source_dir.mkdir(parents=True)
        self.dest_dir.mkdir(parents=True)

        # Populate source tree with nested files, spaces, and a symlink
        (self.source_dir / "app.py").write_text("print('hello backup')\n", encoding="utf-8")
        sub = self.source_dir / "nested folder"
        sub.mkdir()
        (sub / "report 2026.txt").write_bytes(b"A" * 4096)
        (self.source_dir / "app_link.py").symlink_to("app.py")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_create_full_compressed_backup_and_verify_integrity(self) -> None:
        cfg = BackupConfig(backup_root=self.dest_dir, compression_enabled=True)
        manager = BackupManager(config=cfg)

        meta = manager.create_full_backup(self.source_dir, self.dest_dir)
        self.assertTrue(meta.backup_id.startswith("backup_"))
        self.assertEqual(meta.backup_type, "full")
        self.assertEqual(meta.files_discovered, 3)
        self.assertEqual(meta.files_backed_up, 3)
        self.assertEqual(meta.archive_filename, "archive.tar.gz")
        self.assertGreater(meta.archive_size_bytes, 0)

        snapshot_dir = self.dest_dir / meta.backup_id
        self.assertTrue((snapshot_dir / "archive.tar.gz").is_file())
        self.assertTrue((snapshot_dir / "manifest.json").is_file())
        self.assertTrue((snapshot_dir / "sha256sums.txt").is_file())
        self.assertTrue((snapshot_dir / "backup_meta.json").is_file())

        # Verify the created backup passes all 3 integrity checks (SHA-256, gzip -t/tar -tzf, manifest)
        ver = manager.verify_backup(meta.backup_id, self.dest_dir)
        self.assertTrue(ver.is_valid, msg=f"Errors: {ver.errors}")
        self.assertEqual(len(ver.verified_files), 3)

    def test_create_uncompressed_backup_and_verify(self) -> None:
        cfg = BackupConfig(backup_root=self.dest_dir, compression_enabled=False)
        manager = BackupManager(config=cfg)

        meta = manager.create_full_backup(self.source_dir, self.dest_dir)
        self.assertEqual(meta.archive_filename, "archive.tar")

        ver = manager.verify_backup(meta.backup_id, self.dest_dir)
        self.assertTrue(ver.is_valid)
        self.assertEqual(len(ver.verified_files), 3)

    def test_create_backup_of_empty_directory_succeeds(self) -> None:
        empty_src = self.base / "empty_src"
        empty_src.mkdir()
        cfg = BackupConfig(backup_root=self.dest_dir, compression_enabled=True)
        manager = BackupManager(config=cfg)

        meta = manager.create_full_backup(empty_src, self.dest_dir)
        self.assertEqual(meta.files_discovered, 0)
        self.assertEqual(meta.files_backed_up, 0)

        ver = manager.verify_backup(meta.backup_id, self.dest_dir)
        self.assertTrue(ver.is_valid)

    def test_verify_detects_corrupted_archive_bytes(self) -> None:
        cfg = BackupConfig(backup_root=self.dest_dir, compression_enabled=True)
        manager = BackupManager(config=cfg)
        meta = manager.create_full_backup(self.source_dir, self.dest_dir)

        archive_path = self.dest_dir / meta.backup_id / meta.archive_filename
        data = bytearray(archive_path.read_bytes())
        # Flip a byte in the middle of the archive
        data[len(data) // 2] ^= 0xFF
        archive_path.write_bytes(bytes(data))

        ver = manager.verify_backup(meta.backup_id, self.dest_dir)
        self.assertFalse(ver.is_valid)
        self.assertIn(meta.archive_filename, ver.modified_files)


if __name__ == "__main__":
    unittest.main()