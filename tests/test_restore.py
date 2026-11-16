#!/usr/bin/env python3
import os
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
from python.restore import RestoreManager


class TestPhase7Restore(unittest.TestCase):
    """Verify full/incremental chain restoration, single-file restore, permissions, and conflicts."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="lbm_test_p7_")
        self.base = Path(self.temp_dir.name)
        self.source_dir = self.base / "src"
        self.dest_dir = self.base / "backups"
        self.restore_dir = self.base / "restored"
        self.source_dir.mkdir()
        self.dest_dir.mkdir()
        self.restore_dir.mkdir()

        self.cfg = BackupConfig(backup_root=self.dest_dir, compression_enabled=True)
        self.backup_mgr = BackupManager(config=self.cfg)
        self.restore_mgr = RestoreManager(config=self.cfg)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_restore_full_backup_preserves_content_permissions_and_symlinks(self) -> None:
        script = self.source_dir / "deploy.sh"
        script.write_text("#!/bin/sh\necho deploy\n", encoding="utf-8")
        script.chmod(0o750)

        nested = self.source_dir / "sub dir"
        nested.mkdir()
        (nested / "data.txt").write_text("important payload\n", encoding="utf-8")
        (self.source_dir / "deploy_link.sh").symlink_to("deploy.sh")

        meta = self.backup_mgr.create_full_backup(self.source_dir, self.dest_dir)

        res = self.restore_mgr.restore(
            backup_id=meta.backup_id,
            target_dir=self.restore_dir,
            dest_root=self.dest_dir,
        )
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.files_restored, 3)

        restored_script = self.restore_dir / "deploy.sh"
        self.assertEqual(
            restored_script.read_text(encoding="utf-8"), "#!/bin/sh\necho deploy\n"
        )
        self.assertEqual(stat.S_IMODE(restored_script.stat().st_mode), 0o750)
        self.assertTrue((self.restore_dir / "deploy_link.sh").is_symlink())
        self.assertEqual(os.readlink(self.restore_dir / "deploy_link.sh"), "deploy.sh")

    def test_restore_multi_step_incremental_chain_and_previous_version(self) -> None:
        # Generation 1 (Full): a.txt (v1), b.txt (v1)
        (self.source_dir / "a.txt").write_text("a_v1\n", encoding="utf-8")
        (self.source_dir / "b.txt").write_text("b_v1\n", encoding="utf-8")
        b1 = self.backup_mgr.create_full_backup(
            self.source_dir, self.dest_dir, backup_id="backup_2026_11_16_010000"
        )

        # Generation 2 (Incremental #1): modify a.txt (v2), add c.txt
        (self.source_dir / "a.txt").write_text("a_v2\n", encoding="utf-8")
        (self.source_dir / "c.txt").write_text("c_v1\n", encoding="utf-8")
        b2 = self.backup_mgr.create_incremental_backup(
            self.source_dir, self.dest_dir, backup_id="backup_2026_11_16_020000"
        )

        # Generation 3 (Incremental #2): delete b.txt, modify c.txt (v2)
        (self.source_dir / "b.txt").unlink()
        (self.source_dir / "c.txt").write_text("c_v2\n", encoding="utf-8")
        b3 = self.backup_mgr.create_incremental_backup(
            self.source_dir, self.dest_dir, backup_id="backup_2026_11_16_030000"
        )

        # Restore Generation 3 (must replay b1 -> b2 -> b3 and delete b.txt!)
        target_b3 = self.base / "restore_b3"
        res_b3 = self.restore_mgr.restore(
            b3.backup_id, target_dir=target_b3, dest_root=self.dest_dir
        )
        self.assertEqual(res_b3.chain_applied, [b1.backup_id, b2.backup_id, b3.backup_id])
        self.assertEqual((target_b3 / "a.txt").read_text(encoding="utf-8"), "a_v2\n")
        self.assertEqual((target_b3 / "c.txt").read_text(encoding="utf-8"), "c_v2\n")
        self.assertFalse(
            (target_b3 / "b.txt").exists(),
            "Deleted file b.txt must not exist when restoring Generation 3!",
        )

        # Restore previous version Generation 1 (b.txt must still exist and a.txt must be v1!)
        target_b1 = self.base / "restore_b1"
        self.restore_mgr.restore(
            b1.backup_id, target_dir=target_b1, dest_root=self.dest_dir
        )
        self.assertEqual((target_b1 / "a.txt").read_text(encoding="utf-8"), "a_v1\n")
        self.assertEqual((target_b1 / "b.txt").read_text(encoding="utf-8"), "b_v1\n")

    def test_restore_single_individual_file(self) -> None:
        (self.source_dir / "keep.txt").write_text("keep\n", encoding="utf-8")
        sub = self.source_dir / "docs"
        sub.mkdir()
        (sub / "target_only.md").write_text("# Restored Single\n", encoding="utf-8")

        meta = self.backup_mgr.create_full_backup(self.source_dir, self.dest_dir)

        res = self.restore_mgr.restore(
            meta.backup_id,
            target_dir=self.restore_dir,
            dest_root=self.dest_dir,
            single_file="docs/target_only.md",
        )
        self.assertEqual(res.files_restored, 1)
        self.assertTrue((self.restore_dir / "docs" / "target_only.md").is_file())
        self.assertFalse((self.restore_dir / "keep.txt").exists())

    def test_conflict_handling_policies_fail_skip_backup_overwrite(self) -> None:
        (self.source_dir / "notes.txt").write_text("backed_up_version\n", encoding="utf-8")
        meta = self.backup_mgr.create_full_backup(self.source_dir, self.dest_dir)

        # Pre-populate restore_dir with a conflicting local modification
        conflict_file = self.restore_dir / "notes.txt"
        conflict_file.write_text("local_unsaved_work\n", encoding="utf-8")

        # 1. Default policy 'fail' must raise FileExistsError and NOT overwrite local work
        with self.assertRaises(FileExistsError):
            self.restore_mgr.restore(
                meta.backup_id,
                target_dir=self.restore_dir,
                dest_root=self.dest_dir,
                on_conflict="fail",
            )
        self.assertEqual(
            conflict_file.read_text(encoding="utf-8"), "local_unsaved_work\n"
        )

        # 2. Policy 'skip' must leave local work untouched and record skipped count
        res_skip = self.restore_mgr.restore(
            meta.backup_id,
            target_dir=self.restore_dir,
            dest_root=self.dest_dir,
            on_conflict="skip",
        )
        self.assertEqual(res_skip.files_skipped_conflict, 1)
        self.assertEqual(
            conflict_file.read_text(encoding="utf-8"), "local_unsaved_work\n"
        )

        # 3. Policy 'backup' must rename local file to notes.txt.bak and write restored version
        res_bak = self.restore_mgr.restore(
            meta.backup_id,
            target_dir=self.restore_dir,
            dest_root=self.dest_dir,
            on_conflict="backup",
        )
        self.assertEqual(res_bak.files_backed_up_conflict, 1)
        self.assertEqual(
            conflict_file.read_text(encoding="utf-8"), "backed_up_version\n"
        )
        self.assertEqual(
            (self.restore_dir / "notes.txt.bak").read_text(encoding="utf-8"),
            "local_unsaved_work\n",
        )

        # 4. Policy 'overwrite' must replace modified file in place
        conflict_file.write_text("another_edit\n", encoding="utf-8")
        res_ow = self.restore_mgr.restore(
            meta.backup_id,
            target_dir=self.restore_dir,
            dest_root=self.dest_dir,
            on_conflict="overwrite",
        )
        self.assertEqual(res_ow.files_restored, 1)
        self.assertEqual(
            conflict_file.read_text(encoding="utf-8"), "backed_up_version\n"
        )


if __name__ == "__main__":
    unittest.main()