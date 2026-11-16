#!/usr/bin/env python3
"""Backup report formatting and human-readable byte conversion utilities."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def format_size(size_bytes: int) -> str:
    """Format a raw byte integer into a human-readable string (B, KB, MB, GB, TB)."""
    if size_bytes < 0:
        raise ValueError(f"size_bytes cannot be negative: {size_bytes}")
    if size_bytes == 0:
        return "0 B"

    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size_bytes)
    unit_idx = 0

    while value >= 1024.0 and unit_idx < len(units) - 1:
        value /= 1024.0
        unit_idx += 1

    if unit_idx == 0:
        return f"{int(value)} B"
    if value.is_integer():
        return f"{int(value)} {units[unit_idx]}"
    return f"{value:.2f} {units[unit_idx]}"


@dataclass(frozen=True)
class BackupSummaryReport:
    """Structured summary data for rendering a backup execution report."""

    source_path: str
    destination_path: str
    files_discovered: int
    files_backed_up: int
    files_skipped: int
    backup_size_bytes: int
    compression_enabled: bool = True
    status: str = "SUCCESS"
    backup_id: str | None = None


def render_backup_report(report: BackupSummaryReport) -> str:
    """Render the standardized LINUX BACKUP MANAGER CLI summary block."""
    compression_label = "Enabled" if report.compression_enabled else "Disabled"
    formatted_size = format_size(report.backup_size_bytes)

    lines = [
        "==================================",
        "      LINUX BACKUP MANAGER",
        "==================================",
        "",
        f"Source: {report.source_path}",
        f"Destination: {report.destination_path}",
    ]
    if report.backup_id:
        lines.append(f"Backup ID: {report.backup_id}")

    lines.extend(
        [
            "",
            f"Files discovered: {report.files_discovered}",
            f"Files backed up: {report.files_backed_up}",
            f"Files skipped: {report.files_skipped}",
            "",
            f"Backup size: {formatted_size}",
            f"Compression: {compression_label}",
            "",
            f"Backup status: {report.status}",
            "",
            "==================================",
        ]
    )
    return "\n".join(lines)