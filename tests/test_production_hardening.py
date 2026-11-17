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

from python.backup import BackupManager
from python.config import BackupConfig
from python.restore import RestoreManager

ROOT_BACKUP_SH = PROJECT_ROOT / "backup.sh"
RESTORE_SH = PROJECT_ROOT / "bash" / "restore.sh"


class TestPhase10ProductionHardening(unittest.TestCase):
    """End-to-end CLI integration, shell metacharacter safety, and path traversal defenses."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="lbm_test_p10_")
        self.base = Path(self.temp_dir.name)
        self.source_dir = self.base / "workspace"
        self.dest_dir = self.base / "backups"
        self.restore_dir = self.base / "restored"
        self.source_dir.mkdir()
        self.dest_dir.mkdir()
        self.restore_dir.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cmd(self, script: Path, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["LBM_BACKUP_ROOT"] = str(self.dest_dir)
        env["LBM_LOG_DIR"] = str(self.base / "logs")
        return subprocess.run(
            ["bash", str(script), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_end_to_end_cli_lifecycle_create_incremental_list_verify_restore_delete(
        self,
    ) -> None:
        (self.source_dir / "main.c").write_text("int main() { return 0; }\n", encoding="utf-8")
        (self.source_dir / "Makefile").write_text("all:\n\tgcc main.c\n", encoding="utf-8")

        # 1. Create full backup via ./backup.sh
        res_full = self.run_cmd(
            ROOT_BACKUP_SH, "create", str(self.source_dir), "--dest", str(self.dest_dir)
        )
        self.assertEqual(res_full.returncode, 0, msg=res_full.stderr)
        self.assertIn("Backup status: SUCCESS", res_full.stdout)

        # 2. Modify file and create incremental backup via ./backup.sh --incremental
        (self.source_dir / "main.c").write_text("int main() { return 42; }\n", encoding="utf-8")
        (self.source_dir / "README.txt").write_text("added in inc\n", encoding="utf-8")
        res_inc = self.run_cmd(
            ROOT_BACKUP_SH,
            "create",
            str(self.source_dir),
            "--dest",
            str(self.dest_dir),
            "--incremental",
        )
        self.assertEqual(res_inc.returncode, 0, msg=res_inc.stderr)
        self.assertIn("Files backed up: 2", res_inc.stdout)
        self.assertIn("Files skipped: 1", res_inc.stdout)

        # 3. List backups via ./backup.sh list
        res_list = self.run_cmd(ROOT_BACKUP_SH, "list", "--dest", str(self.dest_dir))
        self.assertEqual(res_list.returncode, 0)
        self.assertIn("Total backups: 2", res_list.stdout)
        self.assertIn("[FULL]", res_list.stdout)
        self.assertIn("[INCREMENTAL]", res_list.stdout)

        mgr = BackupManager(BackupConfig(backup_root=self.dest_dir))
        backups = mgr.list_backups(self.dest_dir)
        self.assertEqual(len(backups), 2)
        latest_id = backups[-1].backup_id

        # 4. Verify latest incremental backup via ./backup.sh verify
        res_ver = self.run_cmd(
            ROOT_BACKUP_SH, "verify", latest_id, "--dest", str(self.dest_dir)
        )
        self.assertEqual(res_ver.returncode, 0, msg=res_ver.stderr)
        self.assertIn("SUCCESS (INTEGRITY VERIFIED)", res_ver.stdout)

        # 5. Restore incremental chain via bash/restore.sh
        res_rest = self.run_cmd(
            RESTORE_SH,
            latest_id,
            "--dest",
            str(self.dest_dir),
            "--target",
            str(self.restore_dir),
        )
        self.assertEqual(res_rest.returncode, 0, msg=res_rest.stderr)
        self.assertIn("Restore status: SUCCESS", res_rest.stdout)
        self.assertEqual(
            (self.restore_dir / "main.c").read_text(encoding="utf-8"),
            "int main() { return 42; }\n",
        )
        self.assertEqual(
            (self.restore_dir / "Makefile").read_text(encoding="utf-8"),
            "all:\n\tgcc main.c\n",
        )
        self.assertEqual(
            (self.restore_dir / "README.txt").read_text(encoding="utf-8"),
            "added in inc\n",
        )

        # 6. Delete backup via ./backup.sh delete
        res_del = self.run_cmd(
            ROOT_BACKUP_SH, "delete", latest_id, "--dest", str(self.dest_dir)
        )
        self.assertEqual(res_del.returncode, 0, msg=res_del.stderr)
        self.assertIn("Delete status: SUCCESS", res_del.stdout)
        self.assertFalse((self.dest_dir / latest_id).exists())

    def test_filenames_with_shell_metacharacters_backed_up_and_restored_safely(
        self,
    ) -> None:
        # Create files with dangerous shell metacharacters in their names
        weird_names = [
            "file with spaces & quotes 'single'.txt",
            "dollar_$HOME_and_`backticks`.txt",
            "; echo injected ;.txt",
        ]
        for name in weird_names:
            (self.source_dir / name).write_text(f"payload:{name}\n", encoding="utf-8")

        cfg = BackupConfig(backup_root=self.dest_dir)
        mgr = BackupManager(config=cfg, log_dir=self.base / "logs")
        restorer = RestoreManager(config=cfg)

        meta = mgr.create_full_backup(self.source_dir, self.dest_dir)
        self.assertEqual(meta.files_backed_up, 3)

        ver = mgr.verify_backup(meta.backup_id, self.dest_dir)
        self.assertTrue(ver.is_valid)

        restorer.restore(meta.backup_id, self.restore_dir, self.dest_dir)
        for name in weird_names:
            restored_file = self.restore_dir / name
            self.assertTrue(restored_file.is_file())
            self.assertEqual(
                restored_file.read_text(encoding="utf-8"), f"payload:{name}\n"
            )

    def test_security_rejects_path_traversal_in_delete_and_restore(self) -> None:
        cfg = BackupConfig(backup_root=self.dest_dir)
        mgr = BackupManager(config=cfg, log_dir=self.base / "logs")
        restorer = RestoreManager(config=cfg)

        with self.assertRaises(ValueError):
            mgr.delete_backup("../outside_dir", self.dest_dir)

        (self.source_dir / "safe.txt").write_text("ok\n", encoding="utf-8")
        meta = mgr.create_full_backup(self.source_dir, self.dest_dir)

        with self.assertRaises(FileNotFoundError):
            restorer.restore(
                meta.backup_id,
                self.restore_dir,
                self.dest_dir,
                single_file="../../etc/passwd",
            )


if __name__ == "__main__":
    unittest.main()