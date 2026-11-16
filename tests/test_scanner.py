#!/usr/bin/env python3
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from python.config import BackupConfig
from python.main import main as lbm_main
from python.reporter import BackupSummaryReport, format_size, render_backup_report
from python.scanner import DirectoryScanner


class TestPhase2ScannerAndConfig(unittest.TestCase):
    """Verify recursive scanning, inode metadata, symlinks, exclusions, permissions, and reporting."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="lbm_test_p2_")
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        for dirpath, dirnames, filenames in os.walk(self.root):
            for d in dirnames:
                try:
                    (Path(dirpath) / d).chmod(stat.S_IRWXU)
                except OSError:
                    pass
            for f in filenames:
                try:
                    (Path(dirpath) / f).chmod(stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass
        self.temp_dir.cleanup()

    def test_scan_empty_directory(self) -> None:
        scanner = DirectoryScanner()
        res = scanner.scan(self.root)
        self.assertEqual(res.files_discovered, 0)
        self.assertEqual(res.total_size_bytes, 0)
        self.assertEqual(res.directories_visited, 1)
        self.assertEqual(res.files, [])

    def test_scan_nested_directories_and_special_characters(self) -> None:
        nested = self.root / "dir with spaces" / "sub_@#$"
        nested.mkdir(parents=True)
        file_a = self.root / "alpha.txt"
        file_b = nested / "notes (final).md"

        file_a.write_bytes(b"12345")
        file_b.write_bytes(b"abcdefghij")

        scanner = DirectoryScanner()
        res = scanner.scan(self.root)

        self.assertEqual(res.files_discovered, 2)
        self.assertEqual(res.total_size_bytes, 15)
        rel_paths = [f.relative_path for f in res.files]
        self.assertEqual(
            rel_paths,
            ["alpha.txt", "dir with spaces/sub_@#$/notes (final).md"],
        )

    def test_permissions_and_timestamps_collected_accurately(self) -> None:
        script_file = self.root / "run.sh"
        script_file.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        script_file.chmod(0o750)
        os.utime(script_file, (1700000000.0, 1700000500.0))

        scanner = DirectoryScanner()
        res = scanner.scan(self.root)
        self.assertEqual(len(res.files), 1)
        meta = res.files[0]

        self.assertEqual(meta.permissions_octal, "0750")
        self.assertAlmostEqual(meta.mtime, 1700000500.0, places=2)
        self.assertEqual(meta.size_bytes, len("#!/bin/sh\necho hi\n"))
        self.assertEqual(meta.uid, os.getuid())
        self.assertGreater(meta.inode, 0)

    def test_symlink_and_dangling_symlink_handling_without_loop(self) -> None:
        real_file = self.root / "target.txt"
        real_file.write_text("data", encoding="utf-8")

        valid_link = self.root / "valid_link.txt"
        valid_link.symlink_to("target.txt")

        dangling_link = self.root / "broken_link.txt"
        dangling_link.symlink_to("non_existent_target.txt")

        loop_link = self.root / "infinite_loop"
        loop_link.symlink_to(self.root)

        scanner = DirectoryScanner(follow_symlinks=False)
        res = scanner.scan(self.root)

        self.assertEqual(res.files_discovered, 4)
        self.assertEqual(res.symlinks_discovered, 3)

        by_rel = {f.relative_path: f for f in res.files}
        self.assertTrue(by_rel["valid_link.txt"].is_symlink)
        self.assertEqual(by_rel["valid_link.txt"].symlink_target, "target.txt")
        self.assertTrue(by_rel["broken_link.txt"].is_symlink)
        self.assertEqual(
            by_rel["broken_link.txt"].symlink_target, "non_existent_target.txt"
        )

    def test_exclusion_patterns_skip_ignored_files_and_dirs(self) -> None:
        (self.root / "keep.py").write_text("print(1)\n", encoding="utf-8")
        (self.root / "ignore.pyc").write_bytes(b"\x00\x01")
        (self.root / "partial.tmp").write_text("temp", encoding="utf-8")

        git_dir = self.root / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

        scanner = DirectoryScanner()
        res = scanner.scan(self.root)

        self.assertEqual(res.files_discovered, 1)
        self.assertEqual(res.files[0].relative_path, "keep.py")
        self.assertEqual(res.files_skipped_excluded, 3)

    @unittest.skipIf(os.geteuid() == 0, "Root bypasses POSIX permission checks")
    def test_unreadable_file_and_subdir_recorded_in_permission_errors(self) -> None:
        readable = self.root / "ok.txt"
        readable.write_text("readable", encoding="utf-8")

        unreadable_file = self.root / "secret.txt"
        unreadable_file.write_text("secret", encoding="utf-8")
        unreadable_file.chmod(0o000)

        locked_dir = self.root / "locked_subdir"
        locked_dir.mkdir()
        (locked_dir / "inside.txt").write_text("hidden", encoding="utf-8")
        locked_dir.chmod(0o000)

        scanner = DirectoryScanner()
        res = scanner.scan(self.root)

        self.assertEqual(res.files_discovered, 1)
        self.assertEqual(res.files[0].relative_path, "ok.txt")
        self.assertEqual(len(res.permission_errors), 2)

    def test_backup_config_loads_defaults_and_validates(self) -> None:
        cfg = BackupConfig.from_json_file()
        self.assertTrue(cfg.compression_enabled)
        self.assertEqual(cfg.compression_level, 6)
        self.assertIn("*.pyc", cfg.exclude_patterns)

        with self.assertRaises(ValueError):
            BackupConfig(compression_level=99)

    def test_format_size_and_render_backup_report(self) -> None:
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(512), "512 B")
        self.assertEqual(format_size(2048), "2 KB")
        self.assertEqual(format_size(125 * 1024 * 1024), "125 MB")

        report = BackupSummaryReport(
            source_path="/home/user/Documents",
            destination_path="/home/user/backups",
            files_discovered=245,
            files_backed_up=245,
            files_skipped=0,
            backup_size_bytes=125 * 1024 * 1024,
            compression_enabled=True,
            status="SUCCESS",
        )
        text = render_backup_report(report)
        self.assertIn("LINUX BACKUP MANAGER", text)
        self.assertIn("Files discovered: 245", text)
        self.assertIn("Backup size: 125 MB", text)
        self.assertIn("Backup status: SUCCESS", text)

    def test_main_cli_scan_json_and_custom_exclude(self) -> None:
        (self.root / "data.csv").write_text("a,b,c\n", encoding="utf-8")
        (self.root / "debug.log").write_text("log line\n", encoding="utf-8")

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = lbm_main(
                ["scan", "--source", str(self.root), "--exclude", "*.log", "--json"]
            )
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["files_discovered"], 1)
        self.assertEqual(payload["files_skipped_excluded"], 1)
        self.assertEqual(payload["files"][0]["relative_path"], "data.csv")


if __name__ == "__main__":
    unittest.main()