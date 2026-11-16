#!/usr/bin/env python3
"""Configuration management for Linux Backup Manager."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default_config.json"

DEFAULT_EXCLUDE_PATTERNS: tuple[str, ...] = (
    ".git",
    ".git/*",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".venv",
    "venv",
    ".DS_Store",
    "*.swp",
    "*.tmp",
    ".lbm_staging.*",
)


@dataclass(frozen=True)
class BackupConfig:
    """Immutable runtime configuration for backup and scan operations."""

    backup_root: Path = field(
        default_factory=lambda: Path(
            os.environ.get("LBM_BACKUP_ROOT", str(Path.home() / "backups"))
        ).expanduser()
    )
    compression_enabled: bool = True
    compression_level: int = 6
    follow_symlinks: bool = False
    exclude_patterns: tuple[str, ...] = DEFAULT_EXCLUDE_PATTERNS
    retention_count: int = 10

    def __post_init__(self) -> None:
        if not (1 <= self.compression_level <= 9):
            raise ValueError(
                f"compression_level must be between 1 and 9, got {self.compression_level}"
            )
        if self.retention_count < 1:
            raise ValueError(
                f"retention_count must be >= 1, got {self.retention_count}"
            )

    @classmethod
    def from_json_file(cls, config_path: Path | str = DEFAULT_CONFIG_PATH) -> BackupConfig:
        """Load configuration from a JSON file, applying environment overrides if set."""
        path = Path(config_path)
        if not path.is_file():
            return cls()

        raw_data = json.loads(path.read_text(encoding="utf-8"))
        env_backup_root = os.environ.get("LBM_BACKUP_ROOT")
        raw_root = env_backup_root or raw_data.get("backup_root", "~/backups")
        backup_root = Path(raw_root).expanduser().resolve()

        patterns: Sequence[str] = raw_data.get(
            "exclude_patterns", list(DEFAULT_EXCLUDE_PATTERNS)
        )

        return cls(
            backup_root=backup_root,
            compression_enabled=bool(raw_data.get("compression_enabled", True)),
            compression_level=int(raw_data.get("compression_level", 6)),
            follow_symlinks=bool(raw_data.get("follow_symlinks", False)),
            exclude_patterns=tuple(patterns),
            retention_count=int(raw_data.get("retention_count", 10)),
        )