#!/usr/bin/env python3
"""Backup integrity verification and manifest management."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from python.config import DEFAULT_EXCLUDE_PATTERNS
from python.hasher import calculate_file_sha256, calculate_symlink_sha256
from python.scanner import DirectoryScanner, FileMetadata, ScanResult


@dataclass(frozen=True)
class VerificationResult:
    """Detailed outcome of verifying a directory or checksum manifest."""

    target_root: str
    is_valid: bool
    verified_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    unexpected_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_checked(self) -> int:
        return (
            len(self.verified_files)
            + len(self.modified_files)
            + len(self.missing_files)
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def write_sha256sums_file(
    files: Sequence[FileMetadata], output_path: Path | str
) -> Path:
    """Write a standard GNU sha256sum-compatible checksum file (<sha256>  <relative_path>).

    Only regular files with a computed sha256 digest are included in sha256sums.txt
    because GNU sha256sum -c opens and hashes target files rather than symlink strings.
    """
    dest = Path(output_path)
    lines: list[str] = []
    for meta in sorted(files, key=lambda m: m.relative_path):
        if not meta.is_symlink and meta.sha256:
            # GNU coreutils format uses two spaces between digest and filename
            lines.append(f"{meta.sha256}  {meta.relative_path}")

    content = "\n".join(lines) + ("\n" if lines else "")
    dest.write_text(content, encoding="utf-8")
    return dest


def write_json_manifest(scan_result: ScanResult, output_path: Path | str) -> Path:
    """Write a complete JSON manifest including symlinks, permissions, timestamps, and hashes."""
    dest = Path(output_path)
    dest.write_text(
        json.dumps(scan_result.to_dict(), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return dest


def load_json_manifest(manifest_path: Path | str) -> list[FileMetadata]:
    """Load a list of FileMetadata entries from a saved manifest.json file."""
    path = Path(manifest_path)
    if not path.is_file():
        raise FileNotFoundError(f"Manifest file not found: {path}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw_files = raw.get("files", [])
    entries: list[FileMetadata] = []
    for item in raw_files:
        entries.append(
            FileMetadata(
                relative_path=str(item["relative_path"]),
                absolute_path=str(item.get("absolute_path", "")),
                entry_type=str(item["entry_type"]),
                size_bytes=int(item["size_bytes"]),
                mtime=float(item["mtime"]),
                mtime_ns=int(item["mtime_ns"]),
                ctime=float(item["ctime"]),
                permissions_octal=str(item["permissions_octal"]),
                uid=int(item["uid"]),
                gid=int(item["gid"]),
                inode=int(item["inode"]),
                is_symlink=bool(item.get("is_symlink", False)),
                symlink_target=(
                    str(item["symlink_target"])
                    if item.get("symlink_target") is not None
                    else None
                ),
                sha256=(
                    str(item["sha256"]) if item.get("sha256") is not None else None
                ),
            )
        )
    return entries


def verify_directory_against_manifest(
    target_dir: Path | str,
    manifest_entries: Sequence[FileMetadata],
    exclude_patterns: Sequence[str] = DEFAULT_EXCLUDE_PATTERNS,
    check_unexpected: bool = True,
) -> VerificationResult:
    """Verify every entry in manifest_entries against the actual files in target_dir."""
    root = Path(target_dir).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        return VerificationResult(
            target_root=str(root),
            is_valid=False,
            errors=[f"Target directory does not exist: {root}"],
        )

    verified: list[str] = []
    modified: list[str] = []
    missing: list[str] = []
    unexpected: list[str] = []
    errors: list[str] = []

    expected_by_rel: dict[str, FileMetadata] = {
        entry.relative_path: entry for entry in manifest_entries
    }

    for rel_path in sorted(expected_by_rel.keys()):
        expected = expected_by_rel[rel_path]
        candidate = root / rel_path

        try:
            st = candidate.lstat()
        except FileNotFoundError:
            missing.append(rel_path)
            continue
        except OSError as exc:
            errors.append(f"{rel_path}: {exc.strerror}")
            continue

        is_currently_symlink = candidate.is_symlink()
        if is_currently_symlink != expected.is_symlink:
            modified.append(rel_path)
            continue

        if expected.is_symlink:
            try:
                current_target = os.readlink(candidate)
                current_link_hash = calculate_symlink_sha256(candidate)
            except OSError as exc:
                errors.append(f"{rel_path}: {exc.strerror}")
                continue

            if (
                expected.symlink_target is not None
                and current_target != expected.symlink_target
            ) or (
                expected.sha256 is not None and current_link_hash != expected.sha256
            ):
                modified.append(rel_path)
            else:
                verified.append(rel_path)
            continue

        # Regular file verification
        if not candidate.is_file():
            modified.append(rel_path)
            continue

        if int(st.st_size) != expected.size_bytes:
            modified.append(rel_path)
            continue

        try:
            actual_hash = calculate_file_sha256(candidate)
        except OSError as exc:
            errors.append(f"{rel_path}: {exc.strerror}")
            continue

        if expected.sha256 is not None and actual_hash != expected.sha256:
            modified.append(rel_path)
        else:
            verified.append(rel_path)

    if check_unexpected:
        scanner = DirectoryScanner(
            exclude_patterns=exclude_patterns,
            follow_symlinks=False,
            compute_hashes=False,
        )
        live_scan = scanner.scan(root)
        for live_meta in live_scan.files:
            if live_meta.relative_path not in expected_by_rel:
                unexpected.append(live_meta.relative_path)

    is_valid = (
        len(modified) == 0
        and len(missing) == 0
        and len(unexpected) == 0
        and len(errors) == 0
    )

    return VerificationResult(
        target_root=str(root),
        is_valid=is_valid,
        verified_files=verified,
        modified_files=modified,
        missing_files=missing,
        unexpected_files=sorted(unexpected),
        errors=errors,
    )


def verify_sha256sums_file(
    target_dir: Path | str, checksum_file: Path | str
) -> VerificationResult:
    """Verify files in target_dir against a GNU sha256sum-formatted file."""
    root = Path(target_dir).expanduser().resolve()
    chk_path = Path(checksum_path := checksum_file)
    if not chk_path.is_file():
        return VerificationResult(
            target_root=str(root),
            is_valid=False,
            errors=[f"Checksum file not found: {checksum_path}"],
        )

    verified: list[str] = []
    modified: list[str] = []
    missing: list[str] = []
    errors: list[str] = []

    for line_num, raw_line in enumerate(
        chk_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "  " in raw_line:
            expected_hash, rel_path = raw_line.rstrip("\r\n").split("  ", 1)
        else:
            parts = line.split(None, 1)
            if len(parts) != 2:
                errors.append(f"Malformed checksum line {line_num}: {raw_line!r}")
                continue
            expected_hash, rel_path = parts

        expected_hash = expected_hash.strip().lower()
        candidate = root / rel_path
        if not candidate.exists() and not candidate.is_symlink():
            missing.append(rel_path)
            continue

        try:
            actual_hash = calculate_file_sha256(candidate)
        except OSError as exc:
            errors.append(f"{rel_path}: {exc.strerror}")
            continue

        if actual_hash == expected_hash:
            verified.append(rel_path)
        else:
            modified.append(rel_path)

    is_valid = len(modified) == 0 and len(missing) == 0 and len(errors) == 0
    return VerificationResult(
        target_root=str(root),
        is_valid=is_valid,
        verified_files=sorted(verified),
        modified_files=sorted(modified),
        missing_files=sorted(missing),
        unexpected_files=[],
        errors=errors,
    )