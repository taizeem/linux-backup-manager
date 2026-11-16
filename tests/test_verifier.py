#!/usr/bin/env python3
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from python.scanner import DirectoryScanner
from python.verifier import (
    load_json_manifest,
    verify_directory_against_manifest,
    verify_sha256sums_file,
    write_json_manifest,
    write_sha256sums_file,
)


class TestPhase3Verifier(unittest.TestCase):
    """Verify manifest persistence, GNU sha256sum compatibility, and corruption detection."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="lbm_test_p3_ver_")
        self.base = Path(self.temp_dir.name)
        self.source_dir = self.base / "source"
        self.meta_dir = self.base / "meta"
        self.source_dir.mkdir()
        self.meta_dir.mkdir()

        # Populate sample tree
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

        # Corrupt 1 byte while keeping exact same length ("port=8080\n" -> "port=8081\n")
        target_file.write_text("port=8081\n", encoding="utf-8")
        # Spoof mtime and atime back to original timestamp!
        os.utime(target_file, (orig_stat.st_atime, orig_stat.st_mtime))

        ver = verify_directory_against_manifest(self.source_dir, scan_res.files)
        self.assertFalse(ver.is_valid)
        self.assertEqual(ver.modified_files, ["config.ini"])
        self.assertIn("docs/readme.txt", ver.verified_files)

    def test_detects_deleted_and_unexpected_files(self) -> None:
        scanner = DirectoryScanner(compute_hashes=True)
        scan_res = scanner.scan(self.source_dir)

        # Delete docs/readme.txt and add an unexpected file
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

        # 1. Verify using our Python verifier
        py_ver = verify_sha256sums_file(self.source_dir, sums_path)
        self.assertTrue(py_ver.is_valid)
        self.assertEqual(py_ver.verified_files, ["config.ini", "docs/readme.txt"])

        # 2. Verify using the native Linux `sha256sum -c` binary
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


if __name__ == "__main__":
    unittest.main()