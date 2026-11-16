#!/usr/bin/env python3
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKUP_SH = PROJECT_ROOT / "bash" / "backup.sh"


class TestPhase1BashCLI(unittest.TestCase):
    """Verify Bash CLI argument handling, directory checks, permissions, and exit codes."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="lbm_test_p1_")
        self.base_path = Path(self.temp_dir.name)
        self.source_dir = self.base_path / "source docs"  # Intentionally includes a space!
        self.dest_dir = self.base_path / "backups"
        self.source_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        # Restore permissions on any locked test directories before cleanup
        for root, dirs, _ in os.walk(self.base_path):
            for d in dirs:
                dir_path = Path(root) / d
                try:
                    dir_path.chmod(stat.S_IRWXU)
                except OSError:
                    pass
        try:
            self.source_dir.chmod(stat.S_IRWXU)
        except OSError:
            pass
        self.temp_dir.cleanup()

    def run_cli(self, *args: str, env_override: dict | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["LBM_BACKUP_ROOT"] = str(self.dest_dir)
        if env_override:
            env.update(env_override)
        return subprocess.run(
            ["bash", str(BACKUP_SH), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_help_flag_returns_zero(self) -> None:
        res = self.run_cli("--help")
        self.assertEqual(res.returncode, 0)
        self.assertIn("Usage:", res.stdout)

    def test_no_arguments_returns_exit_code_1(self) -> None:
        res = self.run_cli()
        self.assertEqual(res.returncode, 1)
        self.assertIn("[ERROR]", res.stderr)

    def test_unknown_command_returns_exit_code_1(self) -> None:
        res = self.run_cli("invalid_cmd")
        self.assertEqual(res.returncode, 1)
        self.assertIn("[ERROR]", res.stderr)

    def test_create_valid_directory_with_spaces_succeeds(self) -> None:
        res = self.run_cli("create", str(self.source_dir))
        self.assertEqual(res.returncode, 0, msg=f"STDERR: {res.stderr}")
        self.assertIn("LINUX BACKUP MANAGER", res.stdout)
        self.assertIn(f"Source: {self.source_dir.resolve()}", res.stdout)
        self.assertTrue(self.dest_dir.is_dir())

    def test_create_missing_source_directory_returns_exit_code_2(self) -> None:
        missing_dir = self.base_path / "does_not_exist"
        res = self.run_cli("create", str(missing_dir))
        self.assertEqual(res.returncode, 2)
        self.assertIn("does not exist", res.stderr)

    def test_create_file_instead_of_directory_returns_exit_code_2(self) -> None:
        regular_file = self.base_path / "file.txt"
        regular_file.write_text("hello\n", encoding="utf-8")
        res = self.run_cli("create", str(regular_file))
        self.assertEqual(res.returncode, 2)
        self.assertIn("not a directory", res.stderr)

    @unittest.skipIf(os.geteuid() == 0, "Root bypasses POSIX permission checks")
    def test_create_unreadable_source_directory_returns_exit_code_3(self) -> None:
        # Remove read and execute bits (0000)
        self.source_dir.chmod(0o000)
        res = self.run_cli("create", str(self.source_dir))
        self.assertEqual(res.returncode, 3)
        self.assertIn("Permission denied", res.stderr)

    @unittest.skipIf(os.geteuid() == 0, "Root bypasses POSIX permission checks")
    def test_create_directory_without_traverse_bit_returns_exit_code_3(self) -> None:
        # Read-only without execute/traverse bit (0400)
        self.source_dir.chmod(stat.S_IRUSR)
        res = self.run_cli("create", str(self.source_dir))
        self.assertEqual(res.returncode, 3)
        self.assertIn("Permission denied", res.stderr)

    def test_create_rejects_destination_inside_source(self) -> None:
        nested_dest = self.source_dir / "nested_backups"
        res = self.run_cli("create", str(self.source_dir), "--dest", str(nested_dest))
        self.assertEqual(res.returncode, 1)
        self.assertIn("cannot be inside source directory", res.stderr)

    def test_subcommands_list_verify_restore_delete(self) -> None:
        for cmd in ("verify", "restore", "delete"):
            with self.subTest(cmd=cmd):
                res = self.run_cli(cmd, "backup_2026_11_16")
                self.assertEqual(res.returncode, 0)
                self.assertIn(f"Action: {cmd}", res.stdout)

        # Missing backup_id should fail with exit code 1
        res_missing = self.run_cli("verify")
        self.assertEqual(res_missing.returncode, 1)

        # Path traversal in backup_id should fail with exit code 1
        res_traversal = self.run_cli("delete", "../etc/passwd")
        self.assertEqual(res_traversal.returncode, 1)


if __name__ == "__main__":
    unittest.main()