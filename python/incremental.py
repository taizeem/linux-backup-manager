#!/usr/bin/env python3
"""Incremental backup diffing, change/deletion detection, and version lineage."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from python.scanner import FileMetadata


@dataclass(frozen=True)
class IncrementalDiff:
    """Four-way partition of filesystem changes relative to a parent backup."""

    parent_backup_id: str
    added_files: list[FileMetadata] = field(default_factory=list)
    modified_files: list[FileMetadata] = field(default_factory=list)
    unchanged_files: list[FileMetadata] = field(default_factory=list)
    deleted_paths: list[str] = field(default_factory=list)

    @property
    def files_to_archive(self) -> list[FileMetadata]:
        """Return only new and modified entries that need to be written to the delta archive."""
        combined = list(self.added_files) + list(self.modified_files)
        combined.sort(key=lambda m: m.relative_path)
        return combined

    def to_dict(self) -> dict[str, object]:
        return {
            "parent_backup_id": self.parent_backup_id,
            "added_paths": [m.relative_path for m in self.added_files],
            "modified_paths": [m.relative_path for m in self.modified_files],
            "unchanged_paths": [m.relative_path for m in self.unchanged_files],
            "deleted_paths": list(self.deleted_paths),
            "archived_paths": [m.relative_path for m in self.files_to_archive],
        }


def has_entry_changed(prev: FileMetadata, curr: FileMetadata) -> bool:
    """Determine whether a filesystem entry's type, target, size, or SHA-256 changed."""
    if prev.entry_type != curr.entry_type or prev.is_symlink != curr.is_symlink:
        return True

    if curr.is_symlink:
        return prev.symlink_target != curr.symlink_target

    if prev.size_bytes != curr.size_bytes:
        return True

    if prev.sha256 is not None and curr.sha256 is not None:
        return prev.sha256 != curr.sha256

    # Fallback if hashes were not computed: compare nanosecond mtime
    return prev.mtime_ns != curr.mtime_ns


def compute_incremental_diff(
    parent_backup_id: str,
    previous_entries: Sequence[FileMetadata],
    current_entries: Sequence[FileMetadata],
) -> IncrementalDiff:
    """Compare current_entries against previous_entries and classify every path."""
    prev_map: dict[str, FileMetadata] = {
        entry.relative_path: entry for entry in previous_entries
    }
    curr_map: dict[str, FileMetadata] = {
        entry.relative_path: entry for entry in current_entries
    }

    added: list[FileMetadata] = []
    modified: list[FileMetadata] = []
    unchanged: list[FileMetadata] = []
    deleted: list[str] = []

    for rel_path in sorted(curr_map.keys()):
        curr_meta = curr_map[rel_path]
        prev_meta = prev_map.get(rel_path)
        if prev_meta is None:
            added.append(curr_meta)
        elif has_entry_changed(prev_meta, curr_meta):
            modified.append(curr_meta)
        else:
            unchanged.append(curr_meta)

    for rel_path in sorted(prev_map.keys()):
        if rel_path not in curr_map:
            deleted.append(rel_path)

    return IncrementalDiff(
        parent_backup_id=parent_backup_id,
        added_files=added,
        modified_files=modified,
        unchanged_files=unchanged,
        deleted_paths=deleted,
    )


def discover_backup_snapshots(dest_root: Path | str) -> list[dict[str, object]]:
    """Return all valid backup metadata records in dest_root sorted oldest to newest."""
    root = Path(dest_root).expanduser().resolve()
    if not root.is_dir():
        return []

    snapshots: list[dict[str, object]] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        meta_file = entry / "backup_meta.json"
        manifest_file = entry / "manifest.json"
        if not meta_file.is_file() or not manifest_file.is_file():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if meta.get("status") == "SUCCESS":
                snapshots.append(meta)
        except (OSError, json.JSONDecodeError):
            continue

    snapshots.sort(key=lambda m: (str(m.get("created_at", "")), str(m.get("backup_id", ""))))
    return snapshots


def find_latest_backup_for_source(
    dest_root: Path | str, source_root: Path | str
) -> dict[str, object] | None:
    """Find the most recent successful backup snapshot for the given canonical source_root."""
    canonical_src = str(Path(source_root).expanduser().resolve())
    matching = [
        snap
        for snap in discover_backup_snapshots(dest_root)
        if str(snap.get("source_root")) == canonical_src
    ]
    return matching[-1] if matching else None