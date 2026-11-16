#!/usr/bin/env bash
# ==============================================================================
# File: bash/restore.sh
# Description: Pre-flight validation and execution helper for restoring backups.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bash/common.sh
source "${SCRIPT_DIR}/common.sh"

usage() {
    cat <<EOF
Usage: $(basename "$0") <backup_id> [options]

Options:
  -d, --dest <dest_dir>       Backup repository root directory
                              (Default: \$LBM_BACKUP_ROOT or ~/backups)
  -t, --target <restore_dir>  Directory where files will be restored
                              (Default: current working directory)
  -h, --help                  Show this help message and exit
EOF
}

run_restore_preflight() {
    local backup_id="${1:-}"
    local backup_root="${2:-${DEFAULT_BACKUP_ROOT}}"
    local restore_target="${3:-}"

    validate_backup_id "${backup_id}" || return "$?"
    ensure_destination_directory "${backup_root}" || return "$?"

    if [[ -n "${restore_target}" ]]; then
        ensure_destination_directory "${restore_target}" || return "$?"
    fi

    local canonical_root
    canonical_root="$(resolve_canonical_path "${backup_root}")"

    print_banner
    printf "Action: restore\n"
    printf "Backup ID: %s\n" "${backup_id}"
    printf "Destination: %s\n" "${canonical_root}"
    if [[ -n "${restore_target}" ]]; then
        printf "Restore Target: %s\n" "$(resolve_canonical_path "${restore_target}")"
    fi
    print_footer
    return "${EXIT_SUCCESS}"
}

main() {
    if [[ $# -eq 0 ]]; then
        log_error "Restore requires a <backup_id> argument."
        usage >&2
        exit "${EXIT_INVALID_ARGS}"
    fi

    local backup_id=""
    local backup_root="${DEFAULT_BACKUP_ROOT}"
    local restore_target=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)
                usage
                exit "${EXIT_SUCCESS}"
                ;;
            -d|--dest)
                if [[ $# -lt 2 || -z "${2:-}" ]]; then
                    log_error "Option '$1' requires a backup root path."
                    exit "${EXIT_INVALID_ARGS}"
                fi
                backup_root="$2"
                shift 2
                ;;
            -t|--target)
                if [[ $# -lt 2 || -z "${2:-}" ]]; then
                    log_error "Option '$1' requires a target restore directory."
                    exit "${EXIT_INVALID_ARGS}"
                fi
                restore_target="$2"
                shift 2
                ;;
            -*)
                log_error "Unknown option: $1"
                usage >&2
                exit "${EXIT_INVALID_ARGS}"
                ;;
            *)
                if [[ -z "${backup_id}" ]]; then
                    backup_id="$1"
                    shift
                else
                    log_error "Unexpected extra positional argument: $1"
                    exit "${EXIT_INVALID_ARGS}"
                fi
                ;;
        esac
    done

    if [[ -z "${backup_id}" ]]; then
        log_error "'restore' requires a backup ID argument (e.g., backup_2026_11_16)."
        usage >&2
        exit "${EXIT_INVALID_ARGS}"
    fi

    run_restore_preflight "${backup_id}" "${backup_root}" "${restore_target}" || exit "$?"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi