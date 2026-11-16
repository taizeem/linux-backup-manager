#!/usr/bin/env python3
"""Restore orchestration for full backups, incremental chains, and individual files."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from python.config import BackupConfig
from python.database import BackupDatabase
from python.hasher import calculate_file_sha256, calculate_symlink_sha256
from python.scanner import FileMetadata
from python.verifier import load_json_manifest, verify_directory_against_manifest

RESTORE_SH_PATH = PROJECT_ROOT / "bash" / "restore.sh"
VALID_CONFLICT_POLICIES = ("fail", "skip", "backup", "overwrite")


@dataclass(frozen=True)
class RestoreResult:
    """Outcome summary of a restore operation."""

    backup_id: str
    target_dir: str
    files_restored: int
    files_skipped_conflict: int = 0
    files_backed_up_conflict: int = 0
    chain_applied: list[str] = field(default_factory=list)
    status: str = "SUCCESS"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def invoke_bash_tar_extract(
    archive_path: Path,
    extract_dir: Path,
    compression_enabled: bool = True,
    specific_member: str = "",
) -> None:
    """Delegate tar extraction to bash/restore.sh execute_tar_extract."""
    compress_flag = "true" if compression_enabled else "false"
    cmd = [
        "bash",
        "-c",
        'source "$1" && execute_tar_extract "$2" "$3" "$4" "$5"',
        "lbm-extract",
        str(RESTORE_SH_PATH),
        str(archive_path),
        str(extract_dir),
        compress_flag,
        specific_member,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        err_msg = proc.stderr.strip() or f"Failed to extract {archive_path}"
        raise RuntimeError(err_msg)


class RestoreManager:
    """Reconstructs backup chains in an isolated staging area, verifies hashes, and applies files safely."""

    def __init__(self, config: BackupConfig | None = None) -> None:
        self.config = config or BackupConfig.from_json_file()

    def _resolve_lineage_chain(
        self, dst_root: Path, backup_id: str
    ) -> list[dict[str, object]]:
        """Resolve [root_full, ..., target_backup] via SQLite or backup_meta.json fallback."""
        db = BackupDatabase.for_backup_root(dst_root)
        try:
            return db.get_backup_chain(backup_id)
        except KeyError:
            pass

        # Fallback: walk backup_meta.json files on disk if SQLite catalog was not used
        chain: list[dict[str, object]] = []
        visited: set[str] = set()
        curr_id: str | None = backup_id

        while curr_id is not None:
            if curr_id in visited:
                raise RuntimeError(f"Circular backup lineage detected at {curr_id}")
            visited.add(curr_id)

            meta_path = dst_root / curr_id / "backup_meta.json"
            if not meta_path.is_file():
                raise FileNotFoundError(f"Backup '{curr_id}' not found in {dst_root}")

            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            chain.append(meta)
            if meta.get("backup_type") == "full" or not meta.get("parent_backup_id"):
                break
            curr_id = str(meta["parent_backup_id"])

        chain.reverse()
        return chain

    def restore(
        self,
        backup_id: str,
        target_dir: Path | str,
        dest_root: Path | str | None = None,
        single_file: str | None = None,
        on_conflict: str = "fail",
    ) -> RestoreResult:
        """Restore an entire snapshot (or a single relative file) into target_dir."""
        if on_conflict not in VALID_CONFLICT_POLICIES:
            raise ValueError(
                f"Invalid on_conflict policy {on_conflict!r}; must be one of {VALID_CONFLICT_POLICIES}"
            )

        dst_root = (
            Path(dest_root).expanduser().resolve()
            if dest_root is not None
            else self.config.backup_root.expanduser().resolve()
        )
        out_dir = Path(target_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        db = BackupDatabase.for_backup_root(dst_root)

        try:
            chain = self._resolve_lineage_chain(dst_root, backup_id)
        except (KeyError, FileNotFoundError) as exc:
            db.log_event("RESTORE", backup_id, "FAILED", str(exc))
            raise FileNotFoundError(f"Backup '{backup_id}' not found in {dst_root}") from exc

        target_snapshot_dir = dst_root / backup_id
        target_manifest_path = target_snapshot_dir / "manifest.json"
        if not target_manifest_path.is_file():
            db.log_event("RESTORE", backup_id, "FAILED", "Missing manifest.json")
            raise FileNotFoundError(f"Manifest missing for backup '{backup_id}'")

        target_manifest = load_json_manifest(target_manifest_path)
        manifest_by_rel: dict[str, FileMetadata] = {
            m.relative_path: m for m in target_manifest
        }

        if single_file is not None:
            normalized_single = single_file.strip().lstrip("/")
            if normalized_single not in manifest_by_rel:
                db.log_event(
                    "RESTORE",
                    backup_id,
                    "FAILED",
                    f"Requested file '{normalized_single}' not in backup",
                )
                raise FileNotFoundError(
                    f"File '{normalized_single}' does not exist in backup '{backup_id}'"
                )
            entries_to_restore = [manifest_by_rel[normalized_single]]
        else:
            entries_to_restore = target_manifest

        # Stage and verify extraction inside an isolated temporary directory first
        with tempfile.TemporaryDirectory(prefix="lbm_restore_stage_") as stage_tmp:
            stage_path = Path(stage_tmp)
            chain_ids: list[str] = []

            for step_meta in chain:
                step_id = str(step_meta["backup_id"])
                chain_ids.append(step_id)
                step_dir = dst_root / step_id
                archive_name = str(step_meta["archive_filename"])
                archive_path = step_dir / archive_name
                compression_enabled = bool(step_meta.get("compression_enabled", True))

                # Verify archive SHA-256 before extracting
                expected_arc_hash = str(step_meta["archive_sha256"])
                actual_arc_hash = calculate_file_sha256(archive_path)
                if actual_arc_hash != expected_arc_hash:
                    db.log_event(
                        "RESTORE",
                        backup_id,
                        "FAILED",
                        f"Corrupted archive in chain step {step_id}",
                    )
                    raise RuntimeError(
                        f"Integrity failure: archive {archive_path} SHA-256 mismatch"
                    )

                # Extract archive into staging directory
                invoke_bash_tar_extract(
                    archive_path=archive_path,
                    extract_dir=stage_path,
                    compression_enabled=compression_enabled,
                )

                # If this step is incremental, remove any files deleted in this step
                diff_file = step_dir / "incremental_diff.json"
                if diff_file.is_file():
                    diff_data = json.loads(diff_file.read_text(encoding="utf-8"))
                    for del_rel in diff_data.get("deleted_paths", []):
                        del_target = stage_path / del_rel
                        if del_target.is_symlink() or del_target.is_file():
                            del_target.unlink()
                        elif del_target.is_dir():
                            shutil.rmtree(del_target)

            # Verify staged files against target manifest before touching user's target_dir!
            ver_res = verify_directory_against_manifest(
                stage_path,
                entries_to_restore,
                check_unexpected=(single_file is None),
            )
            if not ver_res.is_valid:
                db.log_event(
                    "RESTORE",
                    backup_id,
                    "FAILED",
                    "Staged files failed SHA-256 manifest verification",
                )
                raise RuntimeError(
                    f"Post-extraction verification failed: modified={ver_res.modified_files}, missing={ver_res.missing_files}"
                )

            # Pre-check conflicts across ALL target files if policy == "fail"
            if on_conflict == "fail":
                conflicts: list[str] = []
                for meta in entries_to_restore:
                    dest_file = out_dir / meta.relative_path
                    if dest_file.exists() or dest_file.is_symlink():
                        if not self._is_destination_identical(dest_file, meta):
                            conflicts.append(meta.relative_path)
                if conflicts:
                    db.log_event(
                        "RESTORE",
                        backup_id,
                        "FAILED",
                        f"Aborted due to {len(conflicts)} conflicting file(s)",
                    )
                    raise FileExistsError(
                        f"Restore aborted to protect existing files (conflicts: {', '.join(conflicts)}). "
                        f"Use --conflict skip|backup|overwrite to proceed."
                    )

            # Apply verified staged files to target_dir according to conflict policy
            restored_count = 0
            skipped_conflict = 0
            backed_up_conflict = 0

            for meta in entries_to_restore:
                staged_item = stage_path / meta.relative_path
                dest_item = out_dir / meta.relative_path
                dest_item.parent.mkdir(parents=True, exist_ok=True)

                if dest_item.exists() or dest_item.is_symlink():
                    if self._is_destination_identical(dest_item, meta):
                        # Already identical on disk; ensure metadata matches
                        if not meta.is_symlink:
                            os.chmod(dest_item, int(meta.permissions_octal, 8))
                            os.utime(dest_item, (meta.mtime, meta.mtime))
                        restored_count += 1
                        continue

                    if on_conflict == "skip":
                        skipped_conflict += 1
                        continue
                    elif on_conflict == "backup":
                        bak_path = dest_item.with_name(dest_item.name + ".bak")
                        if bak_path.exists() or bak_path.is_symlink():
                            bak_path.unlink()
                        dest_item.replace(bak_path)
                        backed_up_conflict += 1
                    elif on_conflict == "overwrite":
                        if dest_item.is_symlink() or dest_item.is_file():
                            dest_item.unlink()
                        elif dest_item.is_dir():
                            shutil.rmtree(dest_item)

                # Copy symlink or regular file from verified staging directory
                if meta.is_symlink:
                    target_str = os.readlink(staged_item)
                    dest_item.symlink_to(target_str)
                else:
                    shutil.copy2(staged_item, dest_item)
                    os.chmod(dest_item, int(meta.permissions_octal, 8))
                    os.utime(dest_item, (meta.mtime, meta.mtime))

                restored_count += 1

        db.log_event(
            "RESTORE",
            backup_id,
            "SUCCESS",
            f"Restored {restored_count} file(s) to {out_dir}",
        )
        return RestoreResult(
            backup_id=backup_id,
            target_dir=str(out_dir),
            files_restored=restored_count,
            files_skipped_conflict=skipped_conflict,
            files_backed_up_conflict=backed_up_conflict,
            chain_applied=chain_ids,
            status="SUCCESS",
        )

    @staticmethod
    def _is_destination_identical(dest_path: Path, expected: FileMetadata) -> bool:
        """Return True if dest_path already has the exact same content/target as expected."""
        try:
            if dest_path.is_symlink() != expected.is_symlink:
                return False
            if expected.is_symlink:
                return os.readlink(dest_path) == expected.symlink_target
            if not dest_path.is_file():
                return False
            if dest_path.stat().st_size != expected.size_bytes:
                return False
            return calculate_file_sha256(dest_path) == expected.sha256
        except OSError:
            return False