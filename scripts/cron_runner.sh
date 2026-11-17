#!/usr/bin/env bash
# ==============================================================================
# File: scripts/cron_runner.sh
# Description: Non-interactive cron execution wrapper protected by flock(1).
#              Runs crash recovery, executes backup, and prunes old snapshots.
# ==============================================================================

set -euo pipefail

export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=bash/common.sh
source "${PROJECT_ROOT}/bash/common.sh"

usage() {
    cat <<EOF
Usage: $(basename "$0") <source_dir> [options]

Options:
  -d, --dest <dest_dir>    Backup destination root (Default: \$LBM_BACKUP_ROOT or ~/backups)
  -i, --incremental        Perform an incremental backup
  -k, --keep <count>       Retention count to keep after backup (Default: 10)
  -h, --help               Show this help message and exit
EOF
}

main() {
    local source_dir=""
    local dest_dir="${DEFAULT_BACKUP_ROOT}"
    local incremental="false"
    local keep_count="10"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)
                usage
                exit "${EXIT_SUCCESS}"
                ;;
            -d|--dest)
                dest_dir="${2:-}"
                shift 2
                ;;
            -i|--incremental)
                incremental="true"
                shift
                ;;
            -k|--keep)
                keep_count="${2:-10}"
                shift 2
                ;;
            -*)
                log_error "Unknown option: $1"
                exit "${EXIT_INVALID_ARGS}"
                ;;
            *)
                if [[ -z "${source_dir}" ]]; then
                    source_dir="$1"
                    shift
                else
                    log_error "Unexpected argument: $1"
                    exit "${EXIT_INVALID_ARGS}"
                fi
                ;;
        esac
    done

    if [[ -z "${source_dir}" ]]; then
        log_error "cron_runner.sh requires a <source_dir> argument."
        exit "${EXIT_INVALID_ARGS}"
    fi

    validate_source_directory "${source_dir}" || exit "$?"
    ensure_destination_directory "${dest_dir}" || exit "$?"

    local canonical_src
    canonical_src="$(resolve_canonical_path "${source_dir}")"
    local canonical_dest
    canonical_dest="$(resolve_canonical_path "${dest_dir}")"
    local lock_file="${canonical_dest}/.lbm_cron.lock"

    # Acquire exclusive non-blocking advisory lock on FD 9
    exec 9>"${lock_file}"
    if ! flock -n 9; then
        log_warn "Another backup job is already running on ${canonical_dest}; skipping overlapping run."
        exit "${EXIT_SUCCESS}"
    fi

    # 1. Recover any previously interrupted backups or staging directories
    python3 "${PROJECT_ROOT}/python/main.py" recover --dest "${canonical_dest}"

    # 2. Execute backup
    local -a backup_args=(create "${canonical_src}" --dest "${canonical_dest}")
    if [[ "${incremental}" == "true" ]]; then
        backup_args+=(--incremental)
    fi
    "${PROJECT_ROOT}/backup.sh" "${backup_args[@]}"

    # 3. Apply retention policy
    python3 "${PROJECT_ROOT}/python/main.py" prune \
        --dest "${canonical_dest}" \
        --keep "${keep_count}"
}

main "$@"