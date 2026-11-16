#!/usr/bin/env python3
import stat
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from python.backup import BackupManager
from python.config import BackupConfig
from python.database import CATALOG_FILENAME, BackupDatabase


class TestPhase6Database(unittest.TestCase):
    """Verify SQLite schema, 0600 permissions, lineage resolution, cascade deletes, and audit log."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="lbm_test_p6_")
        self.base = Path(self.temp_dir.name)
        self.source_dir = self.base / "src"
        self.dest_dir = self.base / "backups"
        self.source_dir.mkdir()
        self.dest_dir.mkdir()

        (self.source_dir / "file1.txt").write_text("initial v1\n", encoding="utf-8")
        (self.source_dir / "file2.txt").write_text("stays unchanged\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_database_initializes_with_0600_permissions_and_wal_mode(self) -> None:
        db = BackupDatabase.for_backup_root(self.dest_dir)
        db_file = self.dest_dir / CATALOG_FILENAME
        self.assertTrue(db_file.is_file())

        perms = stat.S_IMODE(db_file.stat().st_mode)
        self.assertEqual(perms, 0o600)

        with db._connect() as conn:
            journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            fk_enabled = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
            self.assertEqual(journal_mode.lower(), "wal")
            self.assertEqual(fk_enabled, 1)

    def test_full_and_incremental_lineage_chain_and_file_catalog(self) -> None:
        cfg = BackupConfig(backup_root=self.dest_dir, compression_enabled=True)
        manager = BackupManager(config=cfg)

        # 1. Full backup
        b1 = manager.create_full_backup(
            self.source_dir, self.dest_dir, backup_id="backup_2026_11_16_100000"
        )

        # 2. Incremental #1 (modify file1.txt, add file3.txt)
        (self.source_dir / "file1.txt").write_text("updated v2\n", encoding="utf-8")
        (self.source_dir / "file3.txt").write_text("added in inc1\n", encoding="utf-8")
        b2 = manager.create_incremental_backup(
            self.source_dir, self.dest_dir, backup_id="backup_2026_11_16_110000"
        )

        # 3. Incremental #2 (delete file2.txt)
        (self.source_dir / "file2.txt").unlink()
        b3 = manager.create_incremental_backup(
            self.source_dir, self.dest_dir, backup_id="backup_2026_11_16_120000"
        )

        db = BackupDatabase.for_backup_root(self.dest_dir)

        # Verify lineage chain resolution [b1, b2, b3]
        chain = db.get_backup_chain(b3.backup_id)
        self.assertEqual(
            [item["backup_id"] for item in chain],
            [b1.backup_id, b2.backup_id, b3.backup_id],
        )

        # Verify file states recorded in Incremental #2
        b3_files_all = db.get_backup_files(b3.backup_id, include_deleted=True)
        by_path = {row["relative_path"]: row for row in b3_files_all}
        self.assertEqual(by_path["file2.txt"]["change_state"], "deleted")
        self.assertEqual(by_path["file1.txt"]["change_state"], "unchanged")

        # Verify active (non-deleted) files in Incremental #2
        b3_active = db.get_backup_files(b3.backup_id, include_deleted=False)
        self.assertEqual(
            [r["relative_path"] for r in b3_active], ["file1.txt", "file3.txt"]
        )

    def test_delete_backup_cascades_to_backup_files_and_logs_history(self) -> None:
        cfg = BackupConfig(backup_root=self.dest_dir, compression_enabled=True)
        manager = BackupManager(config=cfg)

        b1 = manager.create_full_backup(
            self.source_dir, self.dest_dir, backup_id="backup_2026_11_16_100000"
        )
        manager.verify_backup(b1.backup_id, self.dest_dir)

        db = BackupDatabase.for_backup_root(self.dest_dir)
        self.assertEqual(len(db.get_backup_files(b1.backup_id)), 2)

        # Delete backup via manager
        deleted = manager.delete_backup(b1.backup_id, self.dest_dir)
        self.assertTrue(deleted)
        self.assertFalse((self.dest_dir / b1.backup_id).exists())
        self.assertIsNone(db.get_backup(b1.backup_id))
        # ON DELETE CASCADE must have removed all rows in backup_files for b1!
        self.assertEqual(db.get_backup_files(b1.backup_id), [])

        # History table should contain CREATE, VERIFY, and DELETE events
        history = db.get_backup_history()
        actions = [h["action"] for h in history]
        self.assertEqual(actions, ["DELETE", "VERIFY", "CREATE"])


if __name__ == "__main__":
    unittest.main()