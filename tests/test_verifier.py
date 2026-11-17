#!/usr/bin/env python3
import errno
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from python.backup import BackupManager, check_available_disk_space
from python.config import BackupConfig
from python.scanner import DirectoryScanner
from python.verifier import (
    load_json_manifest,
    verify_directory_against_manifest,
    verify_sha256sums_file,
    write_json_manifest,
    write_sha256sums_file,
)


class TestPhase3And8VerifierAndRecovery(unittest.TestCase):
    """Verify manifest persistence, sha256sum compatibility, corruption detection, and crash recovery."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="lbm_test_p38_ver_")
        self.base = Path(self.temp_dir.name)
        self.source_dir = self.base / "source"
        self.meta_dir = self.base / "meta"
        self.dest_dir = self.base / "backups"
        self.log_dir = self.base / "logs"
        self.source_dir.mkdir()
        self.meta_dir.mkdir()
        self.dest_dir.mkdir()
        self.log_dir.mkdir()

        (self.source_dir / "config.ini").write_text("port=8080\n", encoding="utf-8")
        sub = self.source_dir / "docs"
        sub.mkdir()
        (sub / "readme.txt").write_text("backup documentation\n", encoding="utf-8")
        (self.source_dir / "latest.ini").symlink_to("config.ini")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_clean_directory_verifies_successfully(self) -> None:
        scanner = DirectoryScanner(compute_hashes=True)
        scan_res = scanner.scan(self.source_dir)

        manifest_path = write_json_manifest(scan_res, self.meta_dir / "manifest.json")
        loaded_entries = load_json_manifest(manifest_path)

        ver = verify_directory_against_manifest(self.source_dir, loaded_entries)
        self.assertTrue(ver.is_valid)
        self.assertEqual(len(ver.verified_files), 3)
        self.assertEqual(ver.modified_files, [])
        self.assertEqual(ver.missing_files, [])
        self.assertEqual(ver.unexpected_files, [])

    def test_detects_silent_bit_rot_even_with_identical_size_and_mtime(self) -> None:
        scanner = DirectoryScanner(compute_hashes=True)
        scan_res = scanner.scan(self.source_dir)

        target_file = self.source_dir / "config.ini"
        orig_stat = target_file.stat()

        target_file.write_text("port=8081\n", encoding="utf-8")
        os.utime(target_file, (orig_stat.st_atime, orig_stat.st_mtime))

        ver = verify_directory_against_manifest(self.source_dir, scan_res.files)
        self.assertFalse(ver.is_valid)
        self.assertEqual(ver.modified_files, ["config.ini"])
        self.assertIn("docs/readme.txt", ver.verified_files)

    def test_detects_deleted_and_unexpected_files(self) -> None:
        scanner = DirectoryScanner(compute_hashes=True)
        scan_res = scanner.scan(self.source_dir)

        (self.source_dir / "docs" / "readme.txt").unlink()
        (self.source_dir / "intruder.txt").write_text("extra\n", encoding="utf-8")

        ver = verify_directory_against_manifest(self.source_dir, scan_res.files)
        self.assertFalse(ver.is_valid)
        self.assertEqual(ver.missing_files, ["docs/readme.txt"])
        self.assertEqual(ver.unexpected_files, ["intruder.txt"])

    def test_detects_modified_symlink_target(self) -> None:
        scanner = DirectoryScanner(compute_hashes=True)
        scan_res = scanner.scan(self.source_dir)

        link_path = self.source_dir / "latest.ini"
        link_path.unlink()
        link_path.symlink_to("other_target.ini")

        ver = verify_directory_against_manifest(self.source_dir, scan_res.files)
        self.assertFalse(ver.is_valid)
        self.assertEqual(ver.modified_files, ["latest.ini"])

    def test_sha256sums_file_compatible_with_linux_sha256sum_check(self) -> None:
        scanner = DirectoryScanner(compute_hashes=True)
        scan_res = scanner.scan(self.source_dir)

        sums_path = write_sha256sums_file(
            scan_res.files, self.meta_dir / "sha256sums.txt"
        )

        py_ver = verify_sha256sums_file(self.source_dir, sums_path)
        self.assertTrue(py_ver.is_valid)
        self.assertEqual(py_ver.verified_files, ["config.ini", "docs/readme.txt"])

        proc = subprocess.run(
            ["sha256sum", "-c", str(sums_path)],
            cwd=str(self.source_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=f"STDERR: {proc.stderr}")
        self.assertIn("config.ini: OK", proc.stdout)
        self.assertIn("docs/readme.txt: OK", proc.stdout)

    def test_detect_and_recover_interrupted_backups_and_rotating_logs(self) -> None:
        cfg = BackupConfig(backup_root=self.dest_dir, compression_enabled=True)
        mgr = BackupManager(config=cfg, log_dir=self.log_dir)

        # Create 1 valid backup
        valid_meta = mgr.create_full_backup(
            self.source_dir, self.dest_dir, backup_id="backup_2026_11_16_100000"
        )

        # Simulate an interrupted backup directory (missing archive.tar.gz and manifest.json)
        incomplete_dir = self.dest_dir / "backup_2026_11_16_100500"
        incomplete_dir.mkdir()
        (incomplete_dir / "backup_meta.json").write_text('{"status": "IN_PROGRESS"}', encoding="utf-8")

        # Simulate a leftover SIGKILL staging directory and .tmp file
        stale_stage = self.dest_dir / ".lbm_staging.killed99"
        stale_stage.mkdir()
        stale_tmp = self.dest_dir / "orphan.tar.gz.tmp"
        stale_tmp.write_text("partial", encoding="utf-8")

        detected = mgr.detect_incomplete_backups(self.dest_dir)
        self.assertEqual(detected, ["backup_2026_11_16_100500"])

        recovery = mgr.recover_interrupted_backups(self.dest_dir)
        self.assertEqual(
            recovery.incomplete_backups_removed, ["backup_2026_11_16_100500"]
        )
        self.assertEqual(
            sorted(recovery.staging_artifacts_removed),
            [".lbm_staging.killed99", "orphan.tar.gz.tmp"],
        )
        self.assertFalse(incomplete_dir.exists())
        self.assertTrue((self.dest_dir / valid_meta.backup_id).is_dir())

        # Verify rotating log file was created and contains operational entries
        log_file = self.log_dir / "backup.log"
        self.assertTrue(log_file.is_file())
        log_text = log_file.read_text(encoding="utf-8")
        self.assertIn("Created full backup", log_text)
        self.assertIn("Recovered incomplete backup snapshot", log_text)

    def test_check_available_disk_space_raises_enospc_when_insufficient(self) -> None:
        # Request 1 Exabyte (10^18 bytes) to trigger ENOSPC deterministically
        with self.assertRaises(OSError) as ctx:
            check_available_disk_space(self.dest_dir, 10**18)
        self.assertEqual(ctx.exception.errno, errno.ENOSPC)


if __name__ == "__main__":
    unittest.main()