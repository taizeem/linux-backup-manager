#!/usr/bin/env bash
# ==============================================================================
# File: bash/cleanup.sh
# Description: Removes stale temporary staging directories (.lbm_staging.*) and
#              incomplete .tmp archives from the backup destination directory.
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

    # Use nullglob so non-matching globs expand to nothing instead of literal strings
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
            *)
                log_error "Unknown argument: $1"
                usage >&2
                exit "${EXIT_INVALID_ARGS}"
                ;;
        esac
    done

    ensure_destination_directory "${dest_dir}" || exit "$?"
    purge_stale_temp_artifacts "${dest_dir}" || exit "$?"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi