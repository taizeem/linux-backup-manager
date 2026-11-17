#!/usr/bin/env bash
# ==============================================================================
# File: bash/cleanup.sh
# Description: Removes stale temporary staging directories (.lbm_staging.*),
#              incomplete .tmp archives, and optionally prunes old backups.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bash/common.sh
source "${SCRIPT_DIR}/common.sh"

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Options:
  -d, --dest <dest_dir>    Target backup root directory to clean
                           (Default: \$LBM_BACKUP_ROOT or ~/backups)
  -k, --keep <count>       Also prune old backups, retaining newest <count>
  -h, --help               Show this help message and exit
EOF
}

# Clean stale temporary staging directories and .tmp files inside target_dir
purge_stale_temp_artifacts() {
    local target_dir="$1"
    validate_source_directory "${target_dir}" || return "$?"

    if [[ ! -w "${target_dir}" ]]; then
        log_error "Permission denied: cannot clean non-writable directory: ${target_dir}"
        return "${EXIT_PERMISSION_DENIED}"
    fi

    local removed_count=0
    local entry

    shopt -s nullglob
    for entry in "${target_dir}"/.lbm_staging.* "${target_dir}"/*.tmp; do
        if [[ -e "${entry}" || -L "${entry}" ]]; then
            rm -rf "${entry}"
            removed_count=$((removed_count + 1))
        fi
    done
    shopt -u nullglob

    log_info "Cleaned ${removed_count} temporary artifact(s) from $(resolve_canonical_path "${target_dir}")."
    return "${EXIT_SUCCESS}"
}

main() {
    local dest_dir="${DEFAULT_BACKUP_ROOT}"
    local keep_count=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)
                usage
                exit "${EXIT_SUCCESS}"
                ;;
            -d|--dest)
                if [[ $# -lt 2 || -z "${2:-}" ]]; then
                    log_error "Option '$1' requires a directory path."
                    exit "${EXIT_INVALID_ARGS}"
                fi
                dest_dir="$2"
                shift 2
                ;;
            -k|--keep)
                if [[ $# -lt 2 || -z "${2:-}" ]]; then
                    log_error "Option '$1' requires a positive integer keep count."
                    exit "${EXIT_INVALID_ARGS}"
                fi
                keep_count="$2"
                shift 2
                ;;
            *)
                log_error "Unknown argument: $1"
                usage >&2
                exit "${EXIT_INVALID_ARGS}"
                ;;
        esac
    done

    ensure_destination_directory "${dest_dir}" || exit "$?"
    purge_stale_temp_artifacts "${dest_dir}" || exit "$?"

    if [[ -n "${keep_count}" ]]; then
        local canonical_dest
        canonical_dest="$(resolve_canonical_path "${dest_dir}")"
        python3 "${LBM_PROJECT_ROOT}/python/main.py" prune \
            --dest "${canonical_dest}" \
            --keep "${keep_count}" || exit "$?"
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi