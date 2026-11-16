#!/usr/bin/env python3
"""Recursive directory scanner and inode metadata collector."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import stat
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from python.config import DEFAULT_EXCLUDE_PATTERNS


@dataclass(frozen=True)
class FileMetadata:
    """Inode and path metadata for a single discovered filesystem entry."""

    relative_path: str
    absolute_path: str
    entry_type: str  # "file" or "symlink"
    size_bytes: int
    mtime: float
    mtime_ns: int
    ctime: float
    permissions_octal: str  # e.g., "0644" or "0755"
    uid: int
    gid: int
    inode: int
    is_symlink: bool = False
    symlink_target: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class ScanResult:
    """Aggregated result of scanning a source directory tree."""

    source_root: str
    files: list[FileMetadata] = field(default_factory=list)
    directories_visited: int = 0
    files_discovered: int = 0
    symlinks_discovered: int = 0
    files_skipped_excluded: int = 0
    permission_errors: list[str] = field(default_factory=list)
    total_size_bytes: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "source_root": self.source_root,
            "directories_visited": self.directories_visited,
            "files_discovered": self.files_discovered,
            "symlinks_discovered": self.symlinks_discovered,
            "files_skipped_excluded": self.files_skipped_excluded,
            "permission_errors": list(self.permission_errors),
            "total_size_bytes": self.total_size_bytes,
            "files": [f.to_dict() for f in self.files],
        }


class DirectoryScanner:
    """Scans a directory tree using os.scandir and lstat without following symlink loops."""

    def __init__(
        self,
        exclude_patterns: Sequence[str] = DEFAULT_EXCLUDE_PATTERNS,
        follow_symlinks: bool = False,
    ) -> None:
        self.exclude_patterns: tuple[str, ...] = tuple(exclude_patterns)
        self.follow_symlinks: bool = follow_symlinks

    def is_excluded(self, relative_path: str, entry_name: str) -> bool:
        """Check whether an entry matches any configured exclusion glob pattern."""
        normalized_rel = relative_path.replace(os.sep, "/")
        for pattern in self.exclude_patterns:
            if fnmatch.fnmatch(entry_name, pattern) or fnmatch.fnmatch(
                normalized_rel, pattern
            ):
                return True
        return False

    def scan(self, source_dir: Path | str) -> ScanResult:
        """Recursively scan source_dir and collect metadata for all non-excluded entries."""
        root_path = Path(source_dir).expanduser().resolve()
        if not root_path.exists():
            raise FileNotFoundError(f"Source directory does not exist: {root_path}")
        if not root_path.is_dir():
            raise NotADirectoryError(f"Source path is not a directory: {root_path}")
        if not os.access(root_path, os.R_OK | os.X_OK):
            raise PermissionError(
                f"Permission denied: cannot read or traverse source directory: {root_path}"
            )

        result = ScanResult(source_root=str(root_path))
        self._scan_recursive(root_path, root_path, result)
        # Sort deterministically by relative_path so backup manifests are reproducible
        result.files.sort(key=lambda item: item.relative_path)
        return result

    def _scan_recursive(
        self, root_path: Path, current_dir: Path, result: ScanResult
    ) -> None:
        try:
            scandir_iter = os.scandir(current_dir)
        except PermissionError:
            result.permission_errors.append(str(current_dir))
            return
        except OSError as exc:
            result.permission_errors.append(f"{current_dir}: {exc.strerror}")
            return

        result.directories_visited += 1

        with scandir_iter as entries:
            # Sort entries by name for deterministic traversal order
            sorted_entries = sorted(entries, key=lambda e: e.name)
            for entry in sorted_entries:
                entry_path = Path(entry.path)
                rel_path = entry_path.relative_to(root_path).as_posix()

                if self.is_excluded(rel_path, entry.name):
                    result.files_skipped_excluded += 1
                    continue

                try:
                    is_symlink = entry.is_symlink()
                    st = entry.stat(follow_symlinks=self.follow_symlinks)
                except PermissionError:
                    result.permission_errors.append(str(entry_path))
                    continue
                except OSError as exc:
                    result.permission_errors.append(f"{entry_path}: {exc.strerror}")
                    continue

                mode_Format = stat.S_IFMT(st.st_mode)
                perms_octal = f"{stat.S_IMODE(st.st_mode):04o}"

                # Handle symbolic links when follow_symlinks is False (default)
                if is_symlink and not self.follow_symlinks:
                    try:
                        link_target = os.readlink(entry_path)
                    except OSError as exc:
                        result.permission_errors.append(f"{entry_path}: {exc.strerror}")
                        continue

                    meta = FileMetadata(
                        relative_path=rel_path,
                        absolute_path=str(entry_path),
                        entry_type="symlink",
                        size_bytes=int(st.st_size),
                        mtime=float(st.st_mtime),
                        mtime_ns=int(st.st_mtime_ns),
                        ctime=float(st.st_ctime),
                        permissions_octal=perms_octal,
                        uid=int(st.st_uid),
                        gid=int(st.st_gid),
                        inode=int(st.st_ino),
                        is_symlink=True,
                        symlink_target=link_target,
                    )
                    result.files.append(meta)
                    result.files_discovered += 1
                    result.symlinks_discovered += 1
                    continue

                # Handle subdirectories
                if mode_Format == stat.S_IFDIR:
                    self._scan_recursive(root_path, entry_path, result)
                    continue

                # Handle regular files
                if mode_Format == stat.S_IFREG:
                    if not os.access(entry_path, os.R_OK):
                        result.permission_errors.append(str(entry_path))
                        continue

                    meta = FileMetadata(
                        relative_path=rel_path,
                        absolute_path=str(entry_path),
                        entry_type="file",
                        size_bytes=int(st.st_size),
                        mtime=float(st.st_mtime),
                        mtime_ns=int(st.st_mtime_ns),
                        ctime=float(st.st_ctime),
                        permissions_octal=perms_octal,
                        uid=int(st.st_uid),
                        gid=int(st.st_gid),
                        inode=int(st.st_ino),
                        is_symlink=False,
                        symlink_target=None,
                    )
                    result.files.append(meta)
                    result.files_discovered += 1
                    result.total_size_bytes += int(st.st_size)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan a directory and output discovered file metadata."
    )
    parser.add_argument("source_dir", help="Path to source directory to scan")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit scan summary and file metadata as formatted JSON",
    )
    args = parser.parse_args(argv)

    scanner = DirectoryScanner()
    try:
        scan_res = scanner.scan(args.source_dir)
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    except PermissionError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(scan_res.to_dict(), indent=2))
    else:
        print(f"Source: {scan_res.source_root}")
        print(f"Directories visited: {scan_res.directories_visited}")
        print(f"Files discovered: {scan_res.files_discovered}")
        print(f"Symlinks discovered: {scan_res.symlinks_discovered}")
        print(f"Files skipped (excluded): {scan_res.files_skipped_excluded}")
        print(f"Total size (bytes): {scan_res.total_size_bytes}")

    return 0


if __name__ == "__main__":
    sys.exit(main())