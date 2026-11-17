#!/usr/bin/env python3
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from python.backup import BackupManager
from python.config import BackupConfig

CRON_RUNNER_SH = PROJECT_ROOT / "scripts" / "cron_runner.sh"
SCHEDULE_CRON_SH = PROJECT_ROOT / "scripts" / "schedule_cron.sh"


class TestPhase4And5And9Backup(unittest.TestCase):
    """Verify full/incremental backups, delta archiving, retention policies, and cron scripts."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="lbm_test_p459_")
        self.base = Path(self.temp_dir.name)
        self.source_dir = self.base / "source project"
        self.dest_dir = self.base / "backups"
        self.source_dir.mkdir(parents=True)
        self.dest_dir.mkdir(parents=True)

        (self.source_dir / "app.py").write_text(
            "print('hello backup')\n", encoding="utf-8"
        )
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
        data[len(data) // 2] ^= 0xFF
        archive_path.write_bytes(bytes(data))

        ver = manager.verify_backup(meta.backup_id, self.dest_dir)
        self.assertFalse(ver.is_valid)
        self.assertIn(meta.archive_filename, ver.modified_files)

    def test_incremental_backup_falls_back_to_full_when_no_prior_backup_exists(self) -> None:
        cfg = BackupConfig(backup_root=self.dest_dir, compression_enabled=True)
        manager = BackupManager(config=cfg)

        meta = manager.create_incremental_backup(self.source_dir, self.dest_dir)
        self.assertEqual(meta.backup_type, "full")
        self.assertIsNone(meta.parent_backup_id)
        self.assertEqual(meta.files_backed_up, 3)

    def test_incremental_backup_detects_added_modified_deleted_and_skips_unchanged(self) -> None:
        cfg = BackupConfig(backup_root=self.dest_dir, compression_enabled=True)
        manager = BackupManager(config=cfg)

        full_meta = manager.create_full_backup(self.source_dir, self.dest_dir)

        (self.source_dir / "app.py").write_text(
            "print('updated v2')\n", encoding="utf-8"
        )
        (self.source_dir / "app_link.py").unlink()
        (self.source_dir / "new_module.py").write_text("x = 42\n", encoding="utf-8")

        inc_meta = manager.create_incremental_backup(self.source_dir, self.dest_dir)
        self.assertEqual(inc_meta.backup_type, "incremental")
        self.assertEqual(inc_meta.parent_backup_id, full_meta.backup_id)
        self.assertEqual(inc_meta.files_discovered, 3)
        self.assertEqual(inc_meta.files_backed_up, 2)
        self.assertEqual(inc_meta.files_skipped, 1)
        self.assertEqual(inc_meta.files_deleted, 1)

        diff_file = self.dest_dir / inc_meta.backup_id / "incremental_diff.json"
        diff_data = json.loads(diff_file.read_text(encoding="utf-8"))
        self.assertEqual(diff_data["added_paths"], ["new_module.py"])
        self.assertEqual(diff_data["modified_paths"], ["app.py"])
        self.assertEqual(diff_data["unchanged_paths"], ["nested folder/report 2026.txt"])
        self.assertEqual(diff_data["deleted_paths"], ["app_link.py"])

        ver = manager.verify_backup(inc_meta.backup_id, self.dest_dir)
        self.assertTrue(ver.is_valid, msg=f"Errors: {ver.errors}")
        self.assertEqual(ver.verified_files, ["app.py", "new_module.py"])

        versions = manager.list_backups(self.dest_dir)
        self.assertEqual(
            [v.backup_id for v in versions], [full_meta.backup_id, inc_meta.backup_id]
        )

    def test_retention_policy_prunes_old_chains_while_protecting_active_ancestors(self) -> None:
        cfg = BackupConfig(backup_root=self.dest_dir, retention_count=2)
        manager = BackupManager(config=cfg)

        # Chain 1: b1 (full) -> b2 (inc)
        b1 = manager.create_full_backup(
            self.source_dir, self.dest_dir, backup_id="backup_2026_11_16_010000"
        )
        (self.source_dir / "app.py").write_text("v2\n", encoding="utf-8")
        b2 = manager.create_incremental_backup(
            self.source_dir, self.dest_dir, backup_id="backup_2026_11_16_020000"
        )

        # Chain 2: b3 (full) -> b4 (inc) -> b5 (inc)
        b3 = manager.create_full_backup(
            self.source_dir, self.dest_dir, backup_id="backup_2026_11_16_030000"
        )
        (self.source_dir / "app.py").write_text("v4\n", encoding="utf-8")
        b4 = manager.create_incremental_backup(
            self.source_dir, self.dest_dir, backup_id="backup_2026_11_16_040000"
        )
        (self.source_dir / "app.py").write_text("v5\n", encoding="utf-8")
        b5 = manager.create_incremental_backup(
            self.source_dir, self.dest_dir, backup_id="backup_2026_11_16_050000"
        )

        # Retain newest 2 (b4, b5). Because b4/b5 depend on b3 (full), b3 MUST be protected,
        # while b1 and b2 (from superseded Chain 1) MUST be pruned!
        pruned = manager.apply_retention_policy(self.dest_dir, keep_count=2)
        self.assertEqual(pruned, [b1.backup_id, b2.backup_id])

        remaining = [b.backup_id for b in manager.list_backups(self.dest_dir)]
        self.assertEqual(remaining, [b3.backup_id, b4.backup_id, b5.backup_id])

    def test_cron_runner_and_schedule_cron_scripts(self) -> None:
        # 1. Test schedule_cron.sh --print (dry-run without modifying system crontab)
        proc_print = subprocess.run(
            [
                "bash",
                str(SCHEDULE_CRON_SH),
                "--print",
                "--source",
                str(self.source_dir),
                "--dest",
                str(self.dest_dir),
                "--schedule",
                "30 3 * * *",
                "--keep",
                "5",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc_print.returncode, 0, msg=proc_print.stderr)
        self.assertIn("30 3 * * *", proc_print.stdout)
        self.assertIn("cron_runner.sh", proc_print.stdout)
        self.assertIn("# LINUX_BACKUP_MANAGER_AUTO_JOB", proc_print.stdout)

        # 2. Test invalid cron schedule (only 3 fields) fails with exit code 1
        proc_bad = subprocess.run(
            [
                "bash",
                str(SCHEDULE_CRON_SH),
                "--print",
                "--source",
                str(self.source_dir),
                "--schedule",
                "0 2 *",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc_bad.returncode, 1)

        # 3. Test cron_runner.sh executes end-to-end (recover + backup + prune) under flock
        proc_run = subprocess.run(
            [
                "bash",
                str(CRON_RUNNER_SH),
                str(self.source_dir),
                "--dest",
                str(self.dest_dir),
                "--incremental",
                "--keep",
                "3",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc_run.returncode, 0, msg=proc_run.stderr)
        self.assertIn("Backup status: SUCCESS", proc_run.stdout)


if __name__ == "__main__":
    unittest.main()