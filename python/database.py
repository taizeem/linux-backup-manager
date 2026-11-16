#!/usr/bin/env python3
"""SQLite metadata catalog, file inventory, version lineage, and audit history."""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from python.incremental import IncrementalDiff
from python.scanner import FileMetadata

CATALOG_FILENAME = "backup_catalog.db"


class BackupDatabase:
    """Manages the SQLite WAL catalog for backup versions, file metadata, and audit history."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    @classmethod
    def for_backup_root(cls, dest_root: Path | str) -> BackupDatabase:
        root = Path(dest_root).expanduser().resolve()
        return cls(root / CATALOG_FILENAME)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            yield conn
        finally:
            conn.close()

    def _initialize_schema(self) -> None:
        with self._connect() as conn:
            with conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS backups (
                        backup_id TEXT PRIMARY KEY,
                        backup_type TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        source_root TEXT NOT NULL,
                        backup_dir TEXT NOT NULL,
                        archive_filename TEXT NOT NULL,
                        archive_sha256 TEXT NOT NULL,
                        archive_size_bytes INTEGER NOT NULL,
                        source_size_bytes INTEGER NOT NULL,
                        files_discovered INTEGER NOT NULL,
                        files_backed_up INTEGER NOT NULL,
                        files_skipped INTEGER NOT NULL,
                        files_deleted INTEGER NOT NULL DEFAULT 0,
                        compression_enabled INTEGER NOT NULL,
                        parent_backup_id TEXT,
                        status TEXT NOT NULL,
                        FOREIGN KEY (parent_backup_id) REFERENCES backups(backup_id) ON DELETE SET NULL
                    );

                    CREATE TABLE IF NOT EXISTS backup_files (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        backup_id TEXT NOT NULL,
                        relative_path TEXT NOT NULL,
                        entry_type TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        mtime REAL NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        permissions_octal TEXT NOT NULL,
                        uid INTEGER NOT NULL,
                        gid INTEGER NOT NULL,
                        inode INTEGER NOT NULL,
                        is_symlink INTEGER NOT NULL,
                        symlink_target TEXT,
                        sha256 TEXT,
                        change_state TEXT NOT NULL,
                         is_archived INTEGER NOT NULL,
                        UNIQUE(backup_id, relative_path),
                        FOREIGN KEY (backup_id) REFERENCES backups(backup_id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_backup_files_backup_id
                        ON backup_files(backup_id);

                    CREATE INDEX IF NOT EXISTS idx_backup_files_sha256
                        ON backup_files(sha256);

                    CREATE TABLE IF NOT EXISTS backup_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        action TEXT NOT NULL,
                        backup_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        details TEXT
                    );
                    """
                )
        # Harden database file permissions to owner read/write only (0600)
        if self.db_path.exists():
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass

    def record_backup(
        self,
        metadata_dict: dict[str, object],
        files: Sequence[FileMetadata],
        diff: IncrementalDiff | None = None,
    ) -> None:
        """Insert a backup snapshot, its file catalog, and a CREATE history event atomically."""
        backup_id = str(metadata_dict["backup_id"])
        added_set = {m.relative_path for m in diff.added_files} if diff else set()
        modified_set = {m.relative_path for m in diff.modified_files} if diff else set()

        file_rows: list[tuple[object, ...]] = []
        for meta in files:
            if diff is None:
                state = "full"
                is_archived = 1
            elif meta.relative_path in added_set:
                state = "added"
                is_archived = 1
            elif meta.relative_path in modified_set:
                state = "modified"
                is_archived = 1
            else:
                state = "unchanged"
                is_archived = 0

            file_rows.append(
                (
                    backup_id,
                    meta.relative_path,
                    meta.entry_type,
                    meta.size_bytes,
                    meta.mtime,
                    meta.mtime_ns,
                    meta.permissions_octal,
                    meta.uid,
                    meta.gid,
                    meta.inode,
                    1 if meta.is_symlink else 0,
                    meta.symlink_target,
                    meta.sha256,
                    state,
                    is_archived,
                )
            )

        # Also record tombstone rows for deleted files in incremental backups
        if diff is not None:
            for deleted_rel in diff.deleted_paths:
                file_rows.append(
                    (
                        backup_id,
                        deleted_rel,
                        "deleted",
                        0,
                        0.0,
                        0,
                        "0000",
                        0,
                        0,
                        0,
                        0,
                        None,
                        None,
                        "deleted",
                        0,
                    )
                )

        now_iso = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO backups (
                        backup_id, backup_type, created_at, source_root, backup_dir,
                        archive_filename, archive_sha256, archive_size_bytes,
                        source_size_bytes, files_discovered, files_backed_up,
                        files_skipped, files_deleted, compression_enabled,
                        parent_backup_id, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        backup_id,
                        str(metadata_dict["backup_type"]),
                        str(metadata_dict["created_at"]),
                        str(metadata_dict["source_root"]),
                        str(metadata_dict["backup_dir"]),
                        str(metadata_dict["archive_filename"]),
                        str(metadata_dict["archive_sha256"]),
                        int(metadata_dict["archive_size_bytes"]),
                        int(metadata_dict["source_size_bytes"]),
                        int(metadata_dict["files_discovered"]),
                        int(metadata_dict["files_backed_up"]),
                        int(metadata_dict["files_skipped"]),
                        int(metadata_dict.get("files_deleted", 0)),
                        1 if metadata_dict.get("compression_enabled", True) else 0,
                        metadata_dict.get("parent_backup_id"),
                        str(metadata_dict.get("status", "SUCCESS")),
                    ),
                )
                conn.execute(
                    "DELETE FROM backup_files WHERE backup_id = ?", (backup_id,)
                )
                conn.executemany(
                    """
                    INSERT INTO backup_files (
                        backup_id, relative_path, entry_type, size_bytes,
                        mtime, mtime_ns, permissions_octal, uid, gid, inode,
                        is_symlink, symlink_target, sha256, change_state, is_archived
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    file_rows,
                )
                conn.execute(
                    """
                    INSERT INTO backup_history (timestamp, action, backup_id, status, details)
                    VALUES (?, 'CREATE', ?, ?, ?)
                    """,
                    (
                        now_iso,
                        backup_id,
                        str(metadata_dict.get("status", "SUCCESS")),
                        f"Created {metadata_dict['backup_type']} backup ({metadata_dict['files_backed_up']} files archived)",
                    ),
                )

    def log_event(
        self, action: str, backup_id: str, status: str, details: str = ""
    ) -> None:
        """Record an operational event (VERIFY, RESTORE, DELETE) in backup_history."""
        now_iso = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO backup_history (timestamp, action, backup_id, status, details)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (now_iso, action.upper(), backup_id, status, details),
                )

    def get_backup(self, backup_id: str) -> dict[str, object] | None:
        """Retrieve a single backup metadata record by backup_id."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM backups WHERE backup_id = ?", (backup_id,)
            ).fetchone()
            if row is None:
                return None
            data = dict(row)
            data["compression_enabled"] = bool(data["compression_enabled"])
            return data

    def list_backups(
        self, source_root: str | None = None
    ) -> list[dict[str, object]]:
        """List recorded backups ordered chronologically from oldest to newest."""
        with self._connect() as conn:
            if source_root:
                rows = conn.execute(
                    """
                    SELECT * FROM backups
                    WHERE source_root = ?
                    ORDER BY created_at ASC, backup_id ASC
                    """,
                    (source_root,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM backups ORDER BY created_at ASC, backup_id ASC"
                ).fetchall()

            results: list[dict[str, object]] = []
            for row in rows:
                item = dict(row)
                item["compression_enabled"] = bool(item["compression_enabled"])
                results.append(item)
            return results

    def get_backup_files(
        self, backup_id: str, include_deleted: bool = False
    ) -> list[dict[str, object]]:
        """Return all file catalog rows for a specific backup_id."""
        with self._connect() as conn:
            if include_deleted:
                rows = conn.execute(
                    """
                    SELECT * FROM backup_files
                    WHERE backup_id = ?
                    ORDER BY relative_path ASC
                    """,
                    (backup_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM backup_files
                    WHERE backup_id = ? AND change_state != 'deleted'
                    ORDER BY relative_path ASC
                    """,
                    (backup_id,),
                ).fetchall()

            results: list[dict[str, object]] = []
            for row in rows:
                item = dict(row)
                item["is_symlink"] = bool(item["is_symlink"])
                item["is_archived"] = bool(item["is_archived"])
                results.append(item)
            return results

    def get_backup_chain(self, backup_id: str) -> list[dict[str, object]]:
        """Resolve the ordered lineage chain [root_full, ..., target_backup] for restoration."""
        chain: list[dict[str, object]] = []
        visited: set[str] = set()
        current_id: str | None = backup_id

        while current_id is not None:
            if current_id in visited:
                raise RuntimeError(
                    f"Circular parent_backup_id reference detected at {current_id}"
                )
            visited.add(current_id)

            record = self.get_backup(current_id)
            if record is None:
                raise KeyError(f"Backup '{current_id}' not found in catalog")

            chain.append(record)
            if record["backup_type"] == "full" or not record.get("parent_backup_id"):
                break
            current_id = str(record["parent_backup_id"])

        chain.reverse()
        return chain

    def delete_backup_record(self, backup_id: str) -> bool:
        """Delete a backup from backups (cascading to backup_files) and log a DELETE event."""
        now_iso = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            with conn:
                cur = conn.execute(
                    "DELETE FROM backups WHERE backup_id = ?", (backup_id,)
                )
                deleted = cur.rowcount > 0
                if deleted:
                    conn.execute(
                        """
                        INSERT INTO backup_history (timestamp, action, backup_id, status, details)
                        VALUES (?, 'DELETE', ?, 'SUCCESS', 'Deleted backup snapshot and catalog records')
                        """,
                        (now_iso, backup_id),
                    )
                return deleted

    def get_backup_history(self, limit: int = 100) -> list[dict[str, object]]:
        """Return recent operational events from backup_history."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM backup_history
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]