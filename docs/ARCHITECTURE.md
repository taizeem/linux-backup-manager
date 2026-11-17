# Linux Backup Manager — Systems Architecture

## 1. Design Philosophy & Layer Separation

`linux-backup-manager` enforces a strict boundary between **Bash** (operating system process execution, stream redirection, signal trapping, and native Linux binary invocation) and **Python 3 Standard Library** (stateful filesystem traversal, streaming SHA-256 hashing, 4-way incremental delta computation, and ACID SQLite catalog persistence).

```text
+-------------------------------------------------------------------------+
|                         User / Cron Daemon (crond)                      |
+------------------------------------+------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
|  Bash OS Boundary Layer (backup.sh, bash/common.sh, bash/restore.sh)    |
|  - POSIX permission checks (-d, -r, -w, -x traverse bit)                |
|  - Canonical path resolution (realpath -s) & recursion guards           |
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