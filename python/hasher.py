#!/usr/bin/env python3
"""Streaming SHA-256 hashing and duplicate content detection utilities."""

from __future__ import annotations

import hashlib
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 64 KiB (16 x 4 KiB pages) balances syscall overhead and CPU cache locality
DEFAULT_CHUNK_SIZE: int = 65536


def calculate_file_sha256(
    file_path: Path | str, chunk_size: int = DEFAULT_CHUNK_SIZE
) -> str:
    """Compute the SHA-256 hex digest of a regular file using fixed-size chunks."""
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be > 0, got {chunk_size}")

    path = Path(file_path)
    if path.is_symlink():
        return calculate_symlink_sha256(path)
    if not path.exists():
        raise FileNotFoundError(f"File does not exist: {path}")
    if path.is_dir():
        raise IsADirectoryError(f"Cannot compute file SHA-256 on a directory: {path}")

    hasher = hashlib.sha256()
    buffer = bytearray(chunk_size)
    memory_view = memoryview(buffer)

    with path.open("rb") as f:
        while True:
            bytes_read = f.readinto(buffer)
            if not bytes_read:
                break
            hasher.update(memory_view[:bytes_read])

    return hasher.hexdigest()


def calculate_symlink_sha256(symlink_path: Path | str) -> str:
    """Compute the SHA-256 hex digest of a symbolic link's target path string."""
    path = Path(symlink_path)
    if not path.is_symlink():
        raise ValueError(f"Path is not a symbolic link: {path}")
    target = os.readlink(path)
    return hashlib.sha256(os.fsencode(target)).hexdigest()


def verify_file_sha256(
    file_path: Path | str, expected_sha256: str, chunk_size: int = DEFAULT_CHUNK_SIZE
) -> bool:
    """Return True if the file's current SHA-256 matches expected_sha256."""
    normalized_expected = expected_sha256.strip().lower()
    if len(normalized_expected) != 64:
        raise ValueError(
            f"Invalid expected SHA-256 digest length ({len(normalized_expected)}): {expected_sha256!r}"
        )
    actual_sha256 = calculate_file_sha256(file_path, chunk_size=chunk_size)
    return actual_sha256 == normalized_expected


def find_duplicate_files(
    paths: Iterable[Path | str], chunk_size: int = DEFAULT_CHUNK_SIZE
) -> dict[str, list[str]]:
    """Group regular files with identical content by SHA-256 digest.

    Uses a two-pass optimization:
    1. Group files by exact byte size (st_size) via lstat() so unique-sized files
       never need to be read from disk.
    2. Compute SHA-256 only for size buckets containing 2 or more files.

    Returns a dictionary mapping sha256_hex -> sorted list of file paths (only for
    hashes shared by 2 or more files).
    """
    size_buckets: dict[int, list[Path]] = defaultdict(list)

    for raw_path in paths:
        path = Path(raw_path)
        try:
            st = path.lstat()
        except OSError:
            continue
        # Only compare regular files (skip directories and symlinks)
        if not path.is_symlink() and path.is_file():
            size_buckets[int(st.st_size)].append(path)

    hash_buckets: dict[str, list[str]] = defaultdict(list)
    for size_bytes, candidate_paths in size_buckets.items():
        if len(candidate_paths) < 2:
            continue
        for candidate in candidate_paths:
            try:
                digest = calculate_file_sha256(candidate, chunk_size=chunk_size)
            except OSError:
                continue
            hash_buckets[digest].append(str(candidate))

    return {
        digest: sorted(file_list)
        for digest, file_list in hash_buckets.items()
        if len(file_list) >= 2
    }