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
from python.reporter import BackupSummaryReport, format_size, render_backup_report
from python.restore import VALID_CONFLICT_POLICIES, RestoreManager
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
    """Orchestrate full or incremental backup creation and print the summary report."""
    manager = BackupManager(config=config)
    try:
        metadata = manager.create_backup(
            source_dir=args.source,
            dest_root=args.dest,
            staging_dir=args.staging if args.staging else None,
            extra_excludes=args.exclude or (),
            incremental=bool(args.incremental),
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


def handle_list(args: argparse.Namespace, config: BackupConfig) -> int:
    """List all recorded backup versions in the destination repository."""
    manager = BackupManager(config=config)
    dest_resolved = Path(args.dest).expanduser().resolve()
    backups = manager.list_backups(dest_root=dest_resolved)

    print("==================================")
    print("      LINUX BACKUP MANAGER")
    print("==================================\n")
    print(f"Destination: {dest_resolved}")
    print(f"Total backups: {len(backups)}\n")

    if not backups:
        print("No backups found.")
    else:
        for b in backups:
            parent_info = f" (parent: {b.parent_backup_id})" if b.parent_backup_id else ""
            print(
                f"- {b.backup_id} [{b.backup_type.upper()}]{parent_info} | "
                f"Files: {b.files_backed_up}/{b.files_discovered} | "
                f"Size: {format_size(b.archive_size_bytes)} | "
                f"Created: {b.created_at}"
            )

    print("\n==================================")
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
    status_str = (
        "SUCCESS (INTEGRITY VERIFIED)"
        if result.is_valid
        else "FAILED (CORRUPTION DETECTED)"
    )
    print(f"\nVerification status: {status_str}")
    print("\n==================================")

    if not result.is_valid:
        for err in result.errors:
            print(f"[ERROR] {err}", file=sys.stderr)
        return 6
    return 0


def handle_restore(args: argparse.Namespace, config: BackupConfig) -> int:
    """Restore a full/incremental backup or single file to target directory."""
    restorer = RestoreManager(config=config)
    dest_resolved = Path(args.dest).expanduser().resolve()
    target_resolved = Path(args.target).expanduser().resolve()

    try:
        res = restorer.restore(
            backup_id=args.backup_id,
            target_dir=target_resolved,
            dest_root=dest_resolved,
            single_file=args.file if args.file else None,
            on_conflict=args.conflict,
        )
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 5
    except FileExistsError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 6
    except Exception as exc:
        print(f"[ERROR] Restore failed: {exc}", file=sys.stderr)
        return 6

    print("==================================")
    print("      LINUX BACKUP MANAGER")
    print("==================================\n")
    print("Action: restore")
    print(f"Backup ID: {res.backup_id}")
    print(f"Destination: {dest_resolved}")
    print(f"Restore Target: {res.target_dir}")
    print(f"Files restored: {res.files_restored}")
    print(f"Files skipped (conflict): {res.files_skipped_conflict}")
    print(f"Restore status: {res.status}")
    print("\n==================================")
    return 0


def handle_delete(args: argparse.Namespace, config: BackupConfig) -> int:
    """Delete a backup snapshot and its SQLite catalog metadata."""
    manager = BackupManager(config=config)
    dest_resolved = Path(args.dest).expanduser().resolve()
    try:
        deleted = manager.delete_backup(
            backup_id=args.backup_id, dest_root=dest_resolved
        )
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    if not deleted:
        print(
            f"[ERROR] Backup '{args.backup_id}' not found in {dest_resolved}",
            file=sys.stderr,
        )
        return 5

    print("==================================")
    print("      LINUX BACKUP MANAGER")
    print("==================================\n")
    print("Action: delete")
    print(f"Backup ID: {args.backup_id}")
    print(f"Destination: {dest_resolved}")
    print("Delete status: SUCCESS")
    print("\n==================================")
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
        "-i",
        "--incremental",
        action="store_true",
        help="Perform an incremental backup relative to the latest snapshot",
    )
    create_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Additional exclusion glob pattern",
    )

    # 'list' subcommand
    list_parser = subparsers.add_parser("list", help="List recorded backups")
    list_parser.add_argument(
        "--dest", required=True, help="Destination backup root path"
    )

    # 'verify' subcommand
    verify_parser = subparsers.add_parser("verify", help="Verify backup integrity")
    verify_parser.add_argument(
        "--backup-id", required=True, help="Backup identifier to verify"
    )
    verify_parser.add_argument(
        "--dest", required=True, help="Destination backup root path"
    )

    # 'restore' subcommand
    restore_parser = subparsers.add_parser("restore", help="Restore a backup")
    restore_parser.add_argument(
        "--backup-id", required=True, help="Backup identifier to restore"
    )
    restore_parser.add_argument(
        "--dest", required=True, help="Destination backup root path"
    )
    restore_parser.add_argument(
        "--target", required=True, help="Target directory for restored files"
    )
    restore_parser.add_argument(
        "--file", default="", help="Optional single relative file path to restore"
    )
    restore_parser.add_argument(
        "--conflict",
        choices=VALID_CONFLICT_POLICIES,
        default="fail",
        help="Conflict handling policy when target file already exists",
    )

    # 'delete' subcommand
    delete_parser = subparsers.add_parser("delete", help="Delete a backup")
    delete_parser.add_argument(
        "--backup-id", required=True, help="Backup identifier to delete"
    )
    delete_parser.add_argument(
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
    if args.command == "list":
        return handle_list(args, config)
    if args.command == "verify":
        return handle_verify(args, config)
    if args.command == "restore":
        return handle_restore(args, config)
    if args.command == "delete":
        return handle_delete(args, config)

    return 1


if __name__ == "__main__":
    sys.exit(main())