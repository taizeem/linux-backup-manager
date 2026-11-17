# Linux Backup Manager

A production-grade Linux command-line backup utility built with **Bash** and **Python 3 (Standard Library)** to demonstrate systems programming, Virtual File System (VFS) inode and symbolic link handling, zero-allocation streaming SHA-256 hashing, atomic snapshot publishing, incremental delta lineage, and SQLite Write-Ahead Logging (WAL) catalog management.

## Key Capabilities

- **Full & Incremental Backups:** Creates timestamped full baseline archives (`backup_YYYY_MM_DD_HHMMSS`) and incremental delta archives (`--incremental`) that package only newly added or modified files while recording deleted files via tombstone manifests.
- **Constant-Memory Streaming SHA-256:** Computes cryptographic digests in $O(1)$ memory using a reusable `64 KiB` (`65536`-byte, 16-page) buffer via `readinto()` and `memoryview`, plus two-stage duplicate file detection (`st_size` pre-grouping followed by SHA-256).
- **Inode & Symbolic Link Safety:** Traverses directory trees using `os.scandir()` (`getdents64` syscall) and `lstat(2)` (`follow_symlinks=False`) with `os.readlink(2)` to preserve valid symlinks, broken/dangling symlinks, and circular directory symlinks without infinite recursion.
- **Atomic Snapshot Publishing:** Stages every backup inside an isolated `0700` directory (`mktemp -d`) on the target filesystem protected by POSIX `trap ... EXIT INT TERM` handlers, publishing completed snapshots atomically via `rename(2)` (`os.replace`).
- **Three-Layer Integrity Verification:** Verifies archive SHA-256 checksums, `gzip -t` and `tar -tzf` stream headers, and extracted per-file SHA-256 manifests—detecting silent bit rot even when file size and `mtime` are unchanged.
- **Staged Chain Replay & Safe Restore:** Reconstructs multi-generation incremental chains (`Full -> Inc1 -> Inc2`) or individual files (`--file`) inside an isolated staging directory, validates SHA-256 integrity prior to target copy, and enforces safe conflict resolution (`fail`, `skip`, `backup` `.bak`, or `overwrite`).
- **SQLite WAL Metadata Catalog:** Persists snapshot metadata, parent-child lineage pointers, file catalogs, and operational audit logs (`CREATE`, `VERIFY`, `RESTORE`, `DELETE`, `RECOVER`) in a `0600`-hardened SQLite database with `PRAGMA journal_mode = WAL` and `ON DELETE CASCADE`.
- **Automated Cron Scheduling & Lineage-Aware Retention:** Includes `flock(1)` concurrency-protected `scripts/cron_runner.sh`, `scripts/schedule_cron.sh`, pre-flight `os.statvfs` free-space checks (`f_bavail * f_frsize`), crash recovery, and retention pruning that protects active incremental ancestors.

## System Architecture

```text
+-------------------------------------------------------------------------+
|                         User / Cron Daemon (crond)                      |
+------------------------------------+------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
|  Bash OS Boundary Layer (backup.sh, bash/common.sh, bash/restore.sh)    |
|  - POSIX permission checks (-d, -r, -w, -x traverse bit)                |
|  - Canonical path resolution (realpath -s) & self-backup guard          |
|  - Atomic 0700 staging creation (mktemp -d) + trap EXIT INT TERM        |
|  - Advisory concurrency locking (flock -n FD 9 in cron_runner.sh)       |
+------------------------------------+------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
|  Python Orchestration Engine (python/main.py, backup.py, restore.py)    |
|  - DirectoryScanner: os.scandir() (getdents64) + lstat(follow_symlinks=0)|
|  - Streaming Hasher: 64 KiB reusable bytearray buffer + memoryview      |
|  - IncrementalDiff: Added / Modified / Unchanged / Deleted partitioning |
|  - Pre-flight disk capacity check via os.statvfs (f_bavail * f_frsize)  |
+-----------------+----------------------------------+--------------------+
                  |                                  |
                  v                                  v
+------------------------------------+  +---------------------------------+
|  Linux Archive Pipeline (Bash)     |  |  SQLite Metadata Catalog (WAL)  |
|  - tar --no-recursion --null -T    |  |  - backups (lineage pointers)   |
|  - gzip -<level> -n pipeline       |  |  - backup_files (ON DELETE      |
|  - gzip -t & tar -tzf validation   |  |    CASCADE + SHA-256 index)     |
|  - Same-FS atomic rename(2) commit |  |  - backup_history (audit log)   |
+------------------------------------+  +---------------------------------+
```

### Separation of Responsibilities

| Layer | Modules | Core Responsibilities |
| :--- | :--- | :--- |
| **Bash Layer** | `backup.sh`, `bash/common.sh`, `bash/backup.sh`, `bash/restore.sh`, `bash/cleanup.sh`, `scripts/*.sh` | Pre-flight directory & permission checks (`-d`, `-r`, `-w`, `-x`), canonical path resolution (`realpath -s`), `0700` staging creation (`mktemp -d`), signal trapping (`EXIT`, `INT`, `TERM`), executing `tar`/`gzip`/`sha256sum` pipelines, `flock` concurrency locking, and `crontab` management. |
| **Python Layer** | `python/main.py`, `python/config.py`, `python/scanner.py`, `python/hasher.py`, `python/incremental.py`, `python/backup.py`, `python/restore.py`, `python/database.py`, `python/verifier.py`, `python/reporter.py` | Recursive directory traversal (`os.scandir` + `lstat`), octal mode extraction (`stat.S_IMODE`), streaming SHA-256 hashing, 4-way incremental diffing, `os.statvfs` capacity checks, SQLite WAL transactions, staged chain restoration, and rotating file logging (`RotatingFileHandler`). |

## Repository Structure

```text
linux-backup-manager/
├── backup.sh                        # Top-level CLI wrapper (execs bash/backup.sh)
├── bash/
│   ├── common.sh                    # Exit codes, r/w/x validators, mktemp, tar/gzip helpers
│   ├── backup.sh                    # Primary Bash CLI parser and pre-flight validator
│   ├── restore.sh                   # Tar extraction and restore pre-flight helper
│   └── cleanup.sh                   # Stale staging cleanup and retention trigger
├── python/
│   ├── __init__.py                  # Package initialization
│   ├── main.py                      # CLI subcommand dispatcher
│   ├── config.py                    # BackupConfig dataclass and JSON loader
│   ├── scanner.py                   # os.scandir + lstat recursive metadata collector
│   ├── hasher.py                    # Streaming 64 KiB SHA-256 & two-pass duplicate finder
│   ├── incremental.py               # 4-way incremental diff and lineage discovery
│   ├── backup.py                    # Full/incremental backup, recovery, and retention engine
│   ├── restore.py                   # Chain replay, single-file restore, and conflict guard
│   ├── database.py                  # SQLite WAL catalog and lineage chain resolver
│   ├── verifier.py                  # Manifest writer and SHA-256 integrity verifier
│   └── reporter.py                  # CLI report formatter and RotatingFileHandler logger
├── scripts/
│   ├── cron_runner.sh               # flock-protected non-interactive cron job wrapper
│   └── schedule_cron.sh             # User crontab schedule generator and installer
├── tests/
│   ├── test_cli_phase1.py           # Bash CLI, exit codes, permissions, and trap tests
│   ├── test_scanner.py              # Scanner, symlinks, exclusions, and reporter tests
│   ├── test_hasher.py               # Streaming SHA-256, sha256sum parity, and duplicate tests
│   ├── test_verifier.py             # Bit-rot detection, manifest, ENOSPC, and recovery tests
│   ├── test_backup.py               # Full/incremental backups, retention, and cron tests
│   ├── test_database.py             # SQLite WAL, foreign keys, and lineage chain tests
│   ├── test_restore.py              # Chain replay, single-file restore, and conflict tests
│   └── test_production_hardening.py # End-to-end lifecycle and security hardening tests
├── config/
│   └── default_config.json          # Default exclusions, compression, and retention config
├── docs/
│   └── ARCHITECTURE.md              # Detailed systems architecture documentation
├── logs/                            # Runtime rotating logs (backup.log, cron.log)
├── .gitignore
├── LICENSE
└── README.md
```

## Requirements

- **OS:** Linux (Ubuntu, Debian, Fedora, Arch, RHEL)
- **Bash:** GNU Bash 4.4+
- **Python:** Python 3.10+ (uses Python Standard Library only; zero third-party packages required)
- **System Utilities:** `tar`, `gzip`, `sha256sum`, `find`, `stat`, `realpath`, `mktemp`, `flock`, `crontab`

## Installation & Setup

```bash
git clone [https://github.com/taizeem/linux-backup-manager.git](https://github.com/taizeem/linux-backup-manager.git)
cd linux-backup-manager

chmod +x backup.sh bash/*.sh scripts/*.sh
```

## CLI Usage

### 1. Create a Full Backup

```bash
./backup.sh create ~/Documents
```

Override the destination backup directory using `--dest` (or export `LBM_BACKUP_ROOT`):

```bash
./backup.sh create ~/Documents --dest ~/backups
```

Example output:

```text
==================================
      LINUX BACKUP MANAGER
==================================

Source: /home/user/Documents
Destination: /home/user/backups
Backup ID: backup_2026_11_17_080000

Files discovered: 245
Files backed up: 245
Files skipped: 0

Backup size: 125 MB
Compression: Enabled

Backup status: SUCCESS

==================================
```

### 2. Create an Incremental Backup

Archives only files added or modified since the most recent snapshot for the same source directory (automatically falls back to a full backup if no prior snapshot exists):

```bash
./backup.sh create ~/Documents --incremental
```

### 3. List Recorded Backups

Displays all snapshots in chronological order with backup type (`[FULL]` or `[INCREMENTAL]`), parent lineage, archived vs. discovered file counts, compressed archive size, and creation timestamp:

```bash
./backup.sh list
```

### 4. Verify Backup Integrity

Performs a 3-stage verification on the specified snapshot (archive SHA-256 check, `gzip -t` + `tar -tzf` structural validation, and isolated extraction verified against `manifest.json`):

```bash
./backup.sh verify backup_2026_11_17_080000
```

### 5. Restore a Complete Backup or Single File

Restore an entire backup chain (replaying `Full -> Incremental_1 -> ... -> Target` and applying deletion tombstones) into a target directory:

```bash
./bash/restore.sh backup_2026_11_17_080000 --target ~/Restored_Documents
```

Restore a single file from a snapshot:

```bash
./bash/restore.sh backup_2026_11_17_080000 --target ~/Restored_Documents --file notes/project.md
```

Specify a conflict policy (`fail` [default], `skip`, `backup` [renames existing local file to `.bak`], or `overwrite`):

```bash
./bash/restore.sh backup_2026_11_17_080000 --target ~/Restored_Documents --conflict backup
```

### 6. Delete a Backup or Enforce Retention Policy

Delete a single snapshot directory and cascade-delete its SQLite catalog records:

```bash
./backup.sh delete backup_2026_11_17_080000
```

Clean stale temporary staging artifacts and prune old snapshots while automatically protecting ancestor snapshots needed by retained incrementals:

```bash
./bash/cleanup.sh --keep 7
```

### 7. Automated Cron Scheduling

Preview the generated `crontab(5)` entry without modifying your system crontab:

```bash
./scripts/schedule_cron.sh --print --source ~/Documents --dest ~/backups --schedule "0 2 * * *" --keep 10
```

Install, inspect, or remove the automated schedule in your user `crontab`:

```bash
./scripts/schedule_cron.sh --install --source ~/Documents --dest ~/backups --schedule "0 2 * * *" --keep 10
./scripts/schedule_cron.sh --status
./scripts/schedule_cron.sh --remove
```

## Configuration (`config/default_config.json`)

```json
{
  "backup_root": "~/backups",
  "compression_enabled": true,
  "compression_level": 6,
  "follow_symlinks": false,
  "exclude_patterns": [
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
    ".lbm_staging.*"
  ],
  "retention_count": 10
}
```

## Running the Automated Test Suite

The test suite contains **54 automated unit and integration tests** across 7 test modules. All tests execute inside isolated `tempfile.TemporaryDirectory` fixtures and never touch personal files:

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

What is tested:
- Empty directories, deeply nested directories, and multi-megabyte binary files
- Filenames containing spaces, quotes, `$HOME`, backticks, and shell metacharacters
- Valid symbolic links, broken/dangling symbolic links, and circular symlink loops
- Unreadable files (`0000`) and directories missing the traverse (`x`) bit (`0400`)
- Silent single-bit corruption detection even when file size and `mtime` are unchanged
- Full and multi-step incremental backup chains, deletion tombstones, and ancestor-safe retention pruning
- Crash recovery of interrupted backups (`SIGKILL` staging directories and incomplete snapshots) and `ENOSPC` disk-space checks
- SQLite WAL mode, `0600` permission hardening, and `ON DELETE CASCADE` foreign keys

## Standardized Exit Codes

| Exit Code | Constant | Description |
| :--- | :--- | :--- |
| `0` | `EXIT_SUCCESS` | Operation completed successfully |
| `1` | `EXIT_INVALID_ARGS` | Invalid command-line arguments, unknown flag, or path traversal rejected |
| `2` | `EXIT_DIR_NOT_FOUND` | Source or destination directory does not exist or is not a directory |
| `3` | `EXIT_PERMISSION_DENIED` | Insufficient read, write, or traverse (`r`/`w`/`x`) permissions |
| `4` | `EXIT_DEPENDENCY_MISSING` | Required system utility (`tar`, `gzip`, `sha256sum`, `python3`) missing from `PATH` |
| `5` | `EXIT_BACKUP_NOT_FOUND` | Specified `backup_id` or target file does not exist in the backup repository |
| `6` | `EXIT_RUNTIME_ERROR` | Archive corruption, restore conflict abort, insufficient disk space, or runtime failure |

## License

MIT License — Copyright (c) 2026 Taizeem 