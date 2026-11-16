#!/usr/bin/env python3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from python.hasher import (
    calculate_file_sha256,
    calculate_symlink_sha256,
    find_duplicate_files,
    verify_file_sha256,
)
from python.scanner import DirectoryScanner


class TestPhase3Hasher(unittest.TestCase):
    """Verify streaming SHA-256 calculations, chunked large-file handling, and duplicates."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="lbm_test_p3_")
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_empty_file_sha256_matches_known_vector(self) -> None:
        empty_file = self.root / "empty.bin"
        empty_file.write_bytes(b"")
        expected = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        self.assertEqual(calculate_file_sha256(empty_file), expected)
        self.assertTrue(verify_file_sha256(empty_file, expected))

    def test_large_file_chunked_streaming_matches_linux_sha256sum(self) -> None:
        # Generate a 2.5 MiB binary file with deterministic repeating pattern
        large_file = self.root / "large_payload.bin"
        chunk = bytes(range(256)) * 1024  # 256 KiB
        with large_file.open("wb") as f:
            for _ in range(10):
                f.write(chunk)

        # Compute digest with tiny 4096-byte chunk size to exercise multi-chunk loop
        py_digest = calculate_file_sha256(large_file, chunk_size=4096)

        # Cross-verify against the native Linux sha256sum C utility
        proc = subprocess.run(
            ["sha256sum", str(large_file)],
            capture_output=True,
            text=True,
            check=True,
        )
        linux_digest = proc.stdout.split()[0]
        self.assertEqual(py_digest, linux_digest)

    def test_single_byte_modification_changes_digest_and_fails_verify(self) -> None:
        doc = self.root / "ledger.txt"
        doc.write_bytes(b"balance=1000\n")
        original_hash = calculate_file_sha256(doc)

        # Modify a single byte ('0' -> '1') keeping file size identical!
        doc.write_bytes(b"balance=1001\n")
        new_hash = calculate_file_sha256(doc)

        self.assertNotEqual(original_hash, new_hash)
        self.assertFalse(verify_file_sha256(doc, original_hash))
        self.assertTrue(verify_file_sha256(doc, new_hash))

    def test_symlink_hashing_handles_dangling_target_safely(self) -> None:
        dangling = self.root / "dangling_link"
        dangling.symlink_to("/nonexistent/target/file.txt")
        digest = calculate_symlink_sha256(dangling)
        self.assertEqual(len(digest), 64)
        self.assertEqual(calculate_file_sha256(dangling), digest)

    def test_find_duplicate_files_groups_identical_content_only(self) -> None:
        dup_a = self.root / "copy1.txt"
        dup_b = self.root / "nested" / "copy2.txt"
        dup_b.parent.mkdir(parents=True)
        unique_same_size = self.root / "same_size_different_bytes.txt"
        unique_diff_size = self.root / "short.txt"

        dup_a.write_bytes(b"identical-payload-12345")
        dup_b.write_bytes(b"identical-payload-12345")
        unique_same_size.write_bytes(b"DIFFERENT-payload-12345")
        unique_diff_size.write_bytes(b"hi")

        duplicates = find_duplicate_files(
            [dup_a, dup_b, unique_same_size, unique_diff_size]
        )
        self.assertEqual(len(duplicates), 1)
        expected_hash = calculate_file_sha256(dup_a)
        self.assertIn(expected_hash, duplicates)
        self.assertEqual(
            duplicates[expected_hash],
            sorted([str(dup_a), str(dup_b)]),
        )

    def test_scanner_with_compute_hashes_populates_sha256(self) -> None:
        sample = self.root / "sample.txt"
        sample.write_text("scanner hash integration\n", encoding="utf-8")

        scanner = DirectoryScanner(compute_hashes=True)
        res = scanner.scan(self.root)
        self.assertEqual(len(res.files), 1)
        self.assertEqual(res.files[0].sha256, calculate_file_sha256(sample))


if __name__ == "__main__":
    unittest.main()