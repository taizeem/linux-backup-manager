#!/usr/bin/env python3
"""Primary Python orchestrator for the Linux Backup Manager."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from python.backup import BackupManager
from python.config import BackupConfig
from python.reporter import BackupSummaryReport, render_backup_report
from python.scanner import DirectoryScanner


def handle_scan(args: argparse.Namespace, config: BackupConfig) -> int:
    """Execute recursive file discovery and output metadata or JSON."""
    exclude_patterns = list(config.exclude_patterns)
    if args.exclude:
        exclude_patterns.extend(args.exclude)

    scanner = DirectoryScanner(
        exclude_patterns=exclude_patterns,
        follow_symlinks=config.follow_symlinks,
    )
    try:
        scan_result = scanner.scan(args.source)
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    except PermissionError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(scan_result.to_dict(), indent=2))
    else:
        report = BackupSummaryReport(
            source_path=scan_result.source_root,
            destination_path=str(Path(args.dest).expanduser().resolve()),
            files_discovered=scan_result.files_discovered,
            files_backed_up=0,
            files_skipped=scan_result.files_skipped_excluded,
            backup_size_bytes=scan_result.total_size_bytes,
            compression_enabled=config.compression_enabled,
            status="SCAN COMPLETE",
        )
        print(render_backup_report(report))

    return 0


def handle_create(args: argparse.Namespace, config: BackupConfig) -> int:
    """Orchestrate full backup creation and print the summary report."""
    manager = BackupManager(config=config)
    try:
        metadata = manager.create_full_backup(
            source_dir=args.source,
            dest_root=args.dest,
            staging_dir=args.staging if args.staging else None,
            extra_excludes=args.exclude or (),
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    except PermissionError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[ERROR] Backup creation failed: {exc}", file=sys.stderr)
        return 6

    report = BackupSummaryReport(
        source_path=metadata.source_root,
        destination_path=str(Path(args.dest).expanduser().resolve()),
        files_discovered=metadata.files_discovered,
        files_backed_up=metadata.files_backed_up,
        files_skipped=metadata.files_skipped,
        backup_size_bytes=metadata.archive_size_bytes,
        compression_enabled=metadata.compression_enabled,
        status=metadata.status,
        backup_id=metadata.backup_id,
    )
    print(render_backup_report(report))
    return 0


def handle_verify(args: argparse.Namespace, config: BackupConfig) -> int:
    """Verify an existing backup snapshot's archive and per-file SHA-256 manifest."""
    manager = BackupManager(config=config)
    dest_resolved = Path(args.dest).expanduser().resolve()
    result = manager.verify_backup(backup_id=args.backup_id, dest_root=dest_resolved)

    print("==================================")
    print("      LINUX BACKUP MANAGER")
    print("==================================\n")
    print("Action: verify")
    print(f"Backup ID: {args.backup_id}")
    print(f"Destination: {dest_resolved}")
    print(f"Files verified: {len(result.verified_files)}")
    print(f"Files modified/corrupted: {len(result.modified_files)}")
    print(f"Files missing: {len(result.missing_files)}")
    status_str = "SUCCESS (INTEGRITY VERIFIED)" if result.is_valid else "FAILED (CORRUPTION DETECTED)"
    print(f"\nVerification status: {status_str}")
    print("\n==================================")

    if not result.is_valid:
        for err in result.errors:
            print(f"[ERROR] {err}", file=sys.stderr)
        return 6
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lbm-engine",
        description="Linux Backup Manager Python orchestration engine.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 'scan' subcommand
    scan_parser = subparsers.add_parser("scan", help="Scan source directory metadata")
    scan_parser.add_argument("--source", required=True, help="Source directory path")
    scan_parser.add_argument(
        "--dest", default="~/backups", help="Destination backup root path"
    )
    scan_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Additional exclusion glob pattern (can be specified multiple times)",
    )
    scan_parser.add_argument(
        "--json", action="store_true", help="Emit scan result as JSON"
    )

    # 'create' subcommand
    create_parser = subparsers.add_parser("create", help="Create a backup")
    create_parser.add_argument("--source", required=True, help="Source directory path")
    create_parser.add_argument(
        "--dest", required=True, help="Destination backup root path"
    )
    create_parser.add_argument(
        "--staging", default="", help="Temporary staging directory created by Bash"
    )
    create_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Additional exclusion glob pattern",
    )

    # 'verify' subcommand
    verify_parser = subparsers.add_parser("verify", help="Verify backup integrity")
    verify_parser.add_argument(
        "--backup-id", required=True, help="Backup identifier to verify"
    )
    verify_parser.add_argument(
        "--dest", required=True, help="Destination backup root path"
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = BackupConfig.from_json_file()

    if args.command == "scan":
        return handle_scan(args, config)
    if args.command == "create":
        return handle_create(args, config)
    if args.command == "verify":
        return handle_verify(args, config)

    return 1


if __name__ == "__main__":
    sys.exit(main())