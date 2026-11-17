#!/usr/bin/env python3
"""Full and incremental backup orchestration, failure recovery, retention, and verification."""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from python.config import BackupConfig
from python.database import BackupDatabase
from python.hasher import calculate_file_sha256
from python.incremental import (
    IncrementalDiff,
    compute_incremental_diff,
    discover_backup_snapshots,
    find_latest_backup_for_source,
)
from python.reporter import get_logger
from python.scanner import DirectoryScanner, FileMetadata, ScanResult
from python.verifier import (
    VerificationResult,
    load_json_manifest,
    verify_directory_against_manifest,
    write_json_manifest,
    write_sha256sums_file,
)

COMMON_SH_PATH = PROJECT_ROOT / "bash" / "common.sh"


@dataclass(frozen=True)
class BackupMetadata:
    """Metadata describing a completed full or incremental backup snapshot."""

    backup_id: str
    backup_type: str  # "full" or "incremental"
    created_at: str
    source_root: str
    backup_dir: str
    archive_filename: str
    archive_sha256: str
    archive_size_bytes: int
    source_size_bytes: int
    files_discovered: int
    files_backed_up: int
    files_skipped: int
    compression_enabled: bool
    parent_backup_id: str | None = None
    files_deleted: int = 0
    status: str = "SUCCESS"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RecoveryReport:
    """Summary of incomplete snapshots and stale temporary artifacts cleaned up."""

    incomplete_backups_removed: list[str] = field(default_factory=list)
    staging_artifacts_removed: list[str] = field(default_factory=list)

    @property
    def total_cleaned(self) -> int:
        return len(self.incomplete_backups_removed) + len(
            self.staging_artifacts_removed
        )


def check_available_disk_space(dest_dir: Path | str, required_bytes: int) -> int:
    """Verify via os.statvfs that dest_dir has at least required_bytes available for non-root users."""
    target = Path(dest_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    st = os.statvfs(target)
    available_bytes = int(st.f_bavail) * int(st.f_frsize)
    if required_bytes > available_bytes:
        raise OSError(
            errno.ENOSPC,
            f"Insufficient disk space in {target}: required {required_bytes} bytes, available {available_bytes} bytes",
        )
    return available_bytes


def generate_backup_id(dest_root: Path, now: datetime | None = None) -> str:
    """Generate a unique timestamped backup ID (e.g. backup_2026_11_16_184000)."""
    ts = (now or datetime.now()).strftime("%Y_%m_%d_%H%M%S")
    base_id = f"backup_{ts}"
    candidate = base_id
    seq = 1
    while (dest_root / candidate).exists():
        candidate = f"{base_id}_{seq:02d}"
        seq += 1
    return candidate


def invoke_bash_tar_archive(
    source_root: Path,
    relative_paths: Sequence[str],
    output_archive: Path,
    compression_enabled: bool = True,
    compression_level: int = 6,
) -> None:
    """Delegate tar + gzip archive creation and validation to bash/common.sh."""
    null_list_path = output_archive.parent / ".files.null.list"
    try:
        payload = b"\x00".join(os.fsencode(p) for p in relative_paths)
        if payload:
            payload += b"\x00"
        null_list_path.write_bytes(payload)

        compress_flag = "true" if compression_enabled else "false"
        cmd = [
            "bash",
            "-c",
            'source "$1" && execute_tar_archive "$2" "$3" "$4" "$5" "$6"',
            "lbm-tar",
            str(COMMON_SH_PATH),
            str(source_root),
            str(null_list_path),
            str(output_archive),
            compress_flag,
            str(compression_level),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            err_msg = proc.stderr.strip() or "Bash archive creation failed"
            raise RuntimeError(err_msg)
    finally:
        if null_list_path.exists():
            null_list_path.unlink()


def invoke_bash_verify_archive(
    archive_path: Path, compression_enabled: bool = True
) -> bool:
    """Run Bash verify_tar_archive (gzip -t and tar -tf) on an existing archive."""
    compress_flag = "true" if compression_enabled else "false"
    cmd = [
        "bash",
        "-c",
        'source "$1" && verify_tar_archive "$2" "$3"',
        "lbm-verify-tar",
        str(COMMON_SH_PATH),
        str(archive_path),
        compress_flag,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode == 0


class BackupManager:
    """Orchestrates full and incremental backups, recovery, retention, and verification."""

    def __init__(
        self, config: BackupConfig | None = None, log_dir: Path | str | None = None
    ) -> None:
        self.config = config or BackupConfig.from_json_file()
        self.logger = get_logger(log_dir)

    def _get_db(self, dest_root: Path) -> BackupDatabase:
        return BackupDatabase.for_backup_root(dest_root)

    def detect_incomplete_backups(
        self, dest_root: Path | str | None = None
    ) -> list[str]:
        """Identify backup_* directories that are missing required metadata or archive files."""
        dst_root = (
            Path(dest_root).expanduser().resolve()
            if dest_root is not None
            else self.config.backup_root.expanduser().resolve()
        )
        if not dst_root.is_dir():
            return []

        incomplete: list[str] = []
        for entry in sorted(dst_root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir() or not entry.name.startswith("backup_"):
                continue
            meta_file = entry / "backup_meta.json"
            manifest_file = entry / "manifest.json"
            if not meta_file.is_file() or not manifest_file.is_file():
                incomplete.append(entry.name)
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                archive_name = str(meta.get("archive_filename", ""))
                if (
                    meta.get("status") != "SUCCESS"
                    or not archive_name
                    or not (entry / archive_name).is_file()
                ):
                    incomplete.append(entry.name)
            except (OSError, json.JSONDecodeError):
                incomplete.append(entry.name)

        return incomplete

    def recover_interrupted_backups(
        self, dest_root: Path | str | None = None
    ) -> RecoveryReport:
        """Clean up incomplete backup snapshots and leftover .lbm_staging.* / *.tmp artifacts."""
        dst_root = (
            Path(dest_root).expanduser().resolve()
            if dest_root is not None
            else self.config.backup_root.expanduser().resolve()
        )
        if not dst_root.is_dir():
            return RecoveryReport()

        db = self._get_db(dst_root)
        incomplete_ids = self.detect_incomplete_backups(dst_root)
        removed_backups: list[str] = []
        removed_staging: list[str] = []

        for b_id in incomplete_ids:
            target = dst_root / b_id
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            db.delete_backup_record(b_id)
            db.log_event(
                "RECOVER",
                b_id,
                "CLEANED",
                "Removed incomplete/interrupted backup snapshot",
            )
            self.logger.warning("Recovered incomplete backup snapshot: %s", b_id)
            removed_backups.append(b_id)

        for entry in sorted(dst_root.iterdir(), key=lambda p: p.name):
            if entry.name.startswith(".lbm_staging.") or entry.name.endswith(".tmp"):
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                self.logger.warning("Cleaned stale temporary artifact: %s", entry.name)
                removed_staging.append(entry.name)

        return RecoveryReport(
            incomplete_backups_removed=removed_backups,
            staging_artifacts_removed=removed_staging,
        )

    def apply_retention_policy(
        self,
        dest_root: Path | str | None = None,
        keep_count: int | None = None,
        protect_ancestors: bool = True,
    ) -> list[str]:
        """Prune old backups beyond keep_count while protecting ancestors needed by retained incrementals."""
        dst_root = (
            Path(dest_root).expanduser().resolve()
            if dest_root is not None
            else self.config.backup_root.expanduser().resolve()
        )
        limit = keep_count if keep_count is not None else self.config.retention_count
        if limit < 1:
            raise ValueError(f"keep_count must be >= 1, got {limit}")

        all_backups = self.list_backups(dst_root)
        if len(all_backups) <= limit:
            return []

        by_id: dict[str, BackupMetadata] = {b.backup_id: b for b in all_backups}
        retained_Primary = all_backups[-limit:]
        protected_ids: set[str] = {b.backup_id for b in retained_Primary}

        if protect_ancestors:
            for snap in retained_Primary:
                curr_parent = snap.parent_backup_id
                while curr_parent and curr_parent in by_id:
                    protected_ids.add(curr_parent)
                    curr_parent = by_id[curr_parent].parent_backup_id

        deleted_ids: list[str] = []
        for snap in all_backups[:-limit]:
            if snap.backup_id not in protected_ids:
                if self.delete_backup(snap.backup_id, dst_root):
                    deleted_ids.append(snap.backup_id)

        if deleted_ids:
            self.logger.info(
                "Retention policy pruned %d old backup(s): %s",
                len(deleted_ids),
                ", ".join(deleted_ids),
            )
        return deleted_ids

    def create_backup(
        self,
        source_dir: Path | str,
        dest_root: Path | str | None = None,
        staging_dir: Path | str | None = None,
        extra_excludes: Sequence[str] = (),
        backup_id: str | None = None,
        incremental: bool = False,
    ) -> BackupMetadata:
        """Create a full or incremental backup snapshot, commit it atomically, and record in SQLite."""
        src_path = Path(source_dir).expanduser().resolve()
        dst_root = (
            Path(dest_root).expanduser().resolve()
            if dest_root is not None
            else self.config.backup_root.expanduser().resolve()
        )
        dst_root.mkdir(parents=True, exist_ok=True)

        if dst_root == src_path or src_path in dst_root.parents:
            raise ValueError(
                f"Destination directory ({dst_root}) cannot be inside source directory ({src_path})."
            )

        exclude_patterns = list(self.config.exclude_patterns) + list(extra_excludes)
        scanner = DirectoryScanner(
            exclude_patterns=exclude_patterns,
            follow_symlinks=self.config.follow_symlinks,
            compute_hashes=True,
        )
        scan_result: ScanResult = scanner.scan(src_path)

        parent_meta: dict[str, object] | None = None
        if incremental:
            parent_meta = find_latest_backup_for_source(dst_root, src_path)

        backup_type = (
            "incremental" if (incremental and parent_meta is not None) else "full"
        )
        parent_backup_id: str | None = None
        diff: IncrementalDiff | None = None

        if backup_type == "incremental" and parent_meta is not None:
            parent_backup_id = str(parent_meta["backup_id"])
            parent_manifest_path = dst_root / parent_backup_id / "manifest.json"
            previous_entries = load_json_manifest(parent_manifest_path)
            diff = compute_incremental_diff(
                parent_backup_id=parent_backup_id,
                previous_entries=previous_entries,
                current_entries=scan_result.files,
            )
            entries_to_archive: list[FileMetadata] = diff.files_to_archive
            files_skipped_count = (
                scan_result.files_skipped_excluded + len(diff.unchanged_files)
            )
            files_deleted_count = len(diff.deleted_paths)
        else:
            entries_to_archive = list(scan_result.files)
            files_skipped_count = scan_result.files_skipped_excluded
            files_deleted_count = 0

        bytes_to_archive = sum(
            m.size_bytes for m in entries_to_archive if not m.is_symlink
        )
        check_available_disk_space(dst_root, bytes_to_archive + 65536)

        resolved_backup_id = backup_id or generate_backup_id(dst_root)
        final_backup_dir = dst_root / resolved_backup_id
        if final_backup_dir.exists():
            raise FileExistsError(
                f"Backup directory already exists: {final_backup_dir}"
            )

        owns_staging = False
        if staging_dir:
            stage_root = Path(staging_dir).expanduser().resolve()
            stage_root.mkdir(parents=True, exist_ok=True)
        else:
            stage_root = Path(
                tempfile.mkdtemp(prefix=".lbm_staging.", dir=str(dst_root))
            )
            os.chmod(stage_root, 0o700)
            owns_staging = True

        staged_snapshot_dir = stage_root / resolved_backup_id
        staged_snapshot_dir.mkdir(parents=True, exist_ok=True)

        try:
            archive_name = (
                "archive.tar.gz" if self.config.compression_enabled else "archive.tar"
            )
            staged_archive_path = staged_snapshot_dir / archive_name

            relative_paths = [entry.relative_path for entry in entries_to_archive]
            invoke_bash_tar_archive(
                source_root=src_path,
                relative_paths=relative_paths,
                output_archive=staged_archive_path,
                compression_enabled=self.config.compression_enabled,
                compression_level=self.config.compression_level,
            )

            archive_sha256 = calculate_file_sha256(staged_archive_path)
            archive_size = staged_archive_path.stat().st_size

            write_json_manifest(scan_result, staged_snapshot_dir / "manifest.json")
            write_sha256sums_file(
                entries_to_archive, staged_snapshot_dir / "sha256sums.txt"
            )
            (staged_snapshot_dir / f"{archive_name}.sha256").write_text(
                f"{archive_sha256}  {archive_name}\n", encoding="utf-8"
            )

            if diff is not None:
                (staged_snapshot_dir / "incremental_diff.json").write_text(
                    json.dumps(diff.to_dict(), indent=2) + "\n", encoding="utf-8"
                )

            created_at = datetime.now().isoformat(timespec="seconds")
            metadata = BackupMetadata(
                backup_id=resolved_backup_id,
                backup_type=backup_type,
                created_at=created_at,
                source_root=str(src_path),
                backup_dir=str(final_backup_dir),
                archive_filename=archive_name,
                archive_sha256=archive_sha256,
                archive_size_bytes=int(archive_size),
                source_size_bytes=scan_result.total_size_bytes,
                files_discovered=scan_result.files_discovered,
                files_backed_up=len(entries_to_archive),
                files_skipped=files_skipped_count,
                compression_enabled=self.config.compression_enabled,
                parent_backup_id=parent_backup_id,
                files_deleted=files_deleted_count,
                status="SUCCESS",
            )
            (staged_snapshot_dir / "backup_meta.json").write_text(
                json.dumps(metadata.to_dict(), indent=2) + "\n", encoding="utf-8"
            )

            os.replace(staged_snapshot_dir, final_backup_dir)

            db = self._get_db(dst_root)
            db.record_backup(metadata.to_dict(), scan_result.files, diff=diff)
            self.logger.info(
                "Created %s backup %s for %s (%d files archived, %d bytes)",
                backup_type,
                resolved_backup_id,
                src_path,
                len(entries_to_archive),
                archive_size,
            )
            return metadata
        except Exception as exc:
            self.logger.error("Backup failed for %s: %s", src_path, exc)
            raise
        finally:
            if staged_snapshot_dir.exists():
                shutil.rmtree(staged_snapshot_dir, ignore_errors=True)
            if owns_staging and stage_root.exists():
                shutil.rmtree(stage_root, ignore_errors=True)

    def create_full_backup(
        self,
        source_dir: Path | str,
        dest_root: Path | str | None = None,
        staging_dir: Path | str | None = None,
        extra_excludes: Sequence[str] = (),
        backup_id: str | None = None,
    ) -> BackupMetadata:
        """Create a full backup snapshot."""
        return self.create_backup(
            source_dir=source_dir,
            dest_root=dest_root,
            staging_dir=staging_dir,
            extra_excludes=extra_excludes,
            backup_id=backup_id,
            incremental=False,
        )

    def create_incremental_backup(
        self,
        source_dir: Path | str,
        dest_root: Path | str | None = None,
        staging_dir: Path | str | None = None,
        extra_excludes: Sequence[str] = (),
        backup_id: str | None = None,
    ) -> BackupMetadata:
        """Create an incremental backup snapshot (falls back to full if no parent exists)."""
        return self.create_backup(
            source_dir=source_dir,
            dest_root=dest_root,
            staging_dir=staging_dir,
            extra_excludes=extra_excludes,
            backup_id=backup_id,
            incremental=True,
        )

    def list_backups(
        self, dest_root: Path | str | None = None
    ) -> list[BackupMetadata]:
        """List all valid backup versions in dest_root ordered oldest to newest."""
        dst_root = (
            Path(dest_root).expanduser().resolve()
            if dest_root is not None
            else self.config.backup_root.expanduser().resolve()
        )
        db = self._get_db(dst_root)
        raw_list = db.list_backups()
        if not raw_list:
            raw_list = discover_backup_snapshots(dst_root)

        results: list[BackupMetadata] = []
        for item in raw_list:
            results.append(
                BackupMetadata(
                    backup_id=str(item["backup_id"]),
                    backup_type=str(item.get("backup_type", "full")),
                    created_at=str(item["created_at"]),
                    source_root=str(item["source_root"]),
                    backup_dir=str(item["backup_dir"]),
                    archive_filename=str(item["archive_filename"]),
                    archive_sha256=str(item["archive_sha256"]),
                    archive_size_bytes=int(item["archive_size_bytes"]),
                    source_size_bytes=int(item.get("source_size_bytes", 0)),
                    files_discovered=int(item["files_discovered"]),
                    files_backed_up=int(item["files_backed_up"]),
                    files_skipped=int(item["files_skipped"]),
                    compression_enabled=bool(item.get("compression_enabled", True)),
                    parent_backup_id=(
                        str(item["parent_backup_id"])
                        if item.get("parent_backup_id") is not None
                        else None
                    ),
                    files_deleted=int(item.get("files_deleted", 0)),
                    status=str(item.get("status", "SUCCESS")),
                )
            )
        return results

    def delete_backup(
        self, backup_id: str, dest_root: Path | str | None = None
    ) -> bool:
        """Safely delete a backup snapshot directory and its SQLite catalog records."""
        if "/" in backup_id or ".." in backup_id or not backup_id.strip():
            raise ValueError(f"Invalid backup_id: {backup_id!r}")

        dst_root = (
            Path(dest_root).expanduser().resolve()
            if dest_root is not None
            else self.config.backup_root.expanduser().resolve()
        )
        snapshot_dir = (dst_root / backup_id).resolve()
        if dst_root not in snapshot_dir.parents:
            raise ValueError(
                f"Refusing to delete path outside backup root: {snapshot_dir}"
            )

        existed_on_disk = False
        if snapshot_dir.is_dir():
            shutil.rmtree(snapshot_dir)
            existed_on_disk = True

        db = self._get_db(dst_root)
        deleted_in_db = db.delete_backup_record(backup_id)
        if existed_on_disk or deleted_in_db:
            self.logger.info("Deleted backup snapshot: %s", backup_id)
        return existed_on_disk or deleted_in_db

    def verify_backup(
        self, backup_id: str, dest_root: Path | str | None = None
    ) -> VerificationResult:
        """Verify the integrity of a full or incremental backup snapshot and log in SQLite."""
        dst_root = (
            Path(dest_root).expanduser().resolve()
            if dest_root is not None
            else self.config.backup_root.expanduser().resolve()
        )
        db = self._get_db(dst_root)
        snapshot_dir = dst_root / backup_id
        if not snapshot_dir.is_dir():
            db.log_event("VERIFY", backup_id, "FAILED", "Snapshot directory missing")
            self.logger.error("Verify failed for %s: directory missing", backup_id)
            return VerificationResult(
                target_root=str(snapshot_dir),
                is_valid=False,
                errors=[f"Backup snapshot not found: {snapshot_dir}"],
            )

        meta_path = snapshot_dir / "backup_meta.json"
        manifest_path = snapshot_dir / "manifest.json"
        if not meta_path.is_file() or not manifest_path.is_file():
            db.log_event("VERIFY", backup_id, "FAILED", "Incomplete metadata files")
            self.logger.error("Verify failed for %s: incomplete metadata", backup_id)
            return VerificationResult(
                target_root=str(snapshot_dir),
                is_valid=False,
                errors=[f"Incomplete backup metadata in: {snapshot_dir}"],
            )

        raw_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        archive_name = str(raw_meta["archive_filename"])
        expected_archive_hash = str(raw_meta["archive_sha256"])
        compression_enabled = bool(raw_meta.get("compression_enabled", True))
        backup_type = str(raw_meta.get("backup_type", "full"))

        archive_path = snapshot_dir / archive_name
        if not archive_path.is_file():
            db.log_event("VERIFY", backup_id, "FAILED", "Archive file missing")
            self.logger.error("Verify failed for %s: archive missing", backup_id)
            return VerificationResult(
                target_root=str(snapshot_dir),
                is_valid=False,
                missing_files=[archive_name],
                errors=[f"Archive file missing: {archive_path}"],
            )

        actual_archive_hash = calculate_file_sha256(archive_path)
        if actual_archive_hash != expected_archive_hash:
            db.log_event("VERIFY", backup_id, "FAILED", "Archive SHA-256 mismatch")
            self.logger.error("Verify failed for %s: SHA-256 mismatch", backup_id)
            return VerificationResult(
                target_root=str(snapshot_dir),
                is_valid=False,
                modified_files=[archive_name],
                errors=[
                    f"Archive SHA-256 mismatch for {archive_name}: expected {expected_archive_hash}, got {actual_archive_hash}"
                ],
            )

        if not invoke_bash_verify_archive(archive_path, compression_enabled):
            db.log_event(
                "VERIFY", backup_id, "FAILED", "Archive tar/gzip structure corrupted"
            )
            self.logger.error(
                "Verify failed for %s: tar/gzip structure corrupted", backup_id
            )
            return VerificationResult(
                target_root=str(snapshot_dir),
                is_valid=False,
                modified_files=[archive_name],
                errors=[f"Archive failed tar/gzip structural check: {archive_path}"],
            )

        all_manifest_entries = load_json_manifest(manifest_path)

        if backup_type == "incremental":
            diff_path = snapshot_dir / "incremental_diff.json"
            if not diff_path.is_file():
                db.log_event(
                    "VERIFY", backup_id, "FAILED", "Missing incremental_diff.json"
                )
                return VerificationResult(
                    target_root=str(snapshot_dir),
                    is_valid=False,
                    errors=[f"Missing incremental_diff.json in {snapshot_dir}"],
                )
            diff_data = json.loads(diff_path.read_text(encoding="utf-8"))
            archived_set = set(diff_data.get("archived_paths", []))
            expected_entries = [
                e for e in all_manifest_entries if e.relative_path in archived_set
            ]
        else:
            expected_entries = all_manifest_entries

        with tempfile.TemporaryDirectory(prefix="lbm_verify_extract_") as extract_tmp:
            tar_flag = "-xzf" if compression_enabled else "-xf"
            proc = subprocess.run(
                ["tar", tar_flag, str(archive_path), "-C", extract_tmp],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                db.log_event("VERIFY", backup_id, "FAILED", "Archive extraction failed")
                return VerificationResult(
                    target_root=str(snapshot_dir),
                    is_valid=False,
                    modified_files=[archive_name],
                    errors=[
                        f"Failed to extract archive during verification: {proc.stderr.strip()}"
                    ],
                )

            ver_res = verify_directory_against_manifest(
                extract_tmp, expected_entries, check_unexpected=True
            )
            db.log_event(
                "VERIFY",
                backup_id,
                "SUCCESS" if ver_res.is_valid else "FAILED",
                f"Verified {len(ver_res.verified_files)} file(s)",
            )
            self.logger.info(
                "Verified backup %s: valid=%s (%d files verified)",
                backup_id,
                ver_res.is_valid,
                len(ver_res.verified_files),
            )
            return VerificationResult(
                target_root=str(snapshot_dir),
                is_valid=ver_res.is_valid,
                verified_files=ver_res.verified_files,
                modified_files=ver_res.modified_files,
                missing_files=ver_res.missing_files,
                unexpected_files=ver_res.unexpected_files,
                errors=ver_res.errors,
            )