#!/usr/bin/env bash
# ==============================================================================
# File: bash/restore.sh
# Description: Pre-flight validation and tar extraction helper for restoring backups.
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
  -f, --file <rel_path>       Restore only a single relative file path
  --conflict <policy>         Conflict policy: fail, skip, backup, overwrite (Default: fail)
  -h, --help                  Show this help message and exit
EOF
}

# Extract a tar or tar.gz archive into target_dir preserving permissions (-p)
execute_tar_extract() {
    local archive_path="$1"
    local extract_dir="$2"
    local compress_enabled="${3:-true}"
    local specific_member="${4:-}"

    if [[ ! -f "${archive_path}" ]]; then
        log_error "Archive file not found for extraction: ${archive_path}"
        return "${EXIT_BACKUP_NOT_FOUND}"
    fi

    mkdir -p "${extract_dir}"

    local -a tar_args=(-p -C "${extract_dir}")
    if [[ "${compress_enabled}" == "true" ]]; then
        tar_args=(-xzf "${archive_path}" "${tar_args[@]}")
    else
        tar_args=(-xf "${archive_path}" "${tar_args[@]}")
    fi

    if [[ -n "${specific_member}" ]]; then
        tar_args+=("${specific_member}")
    fi

    if ! tar "${tar_args[@]}" 2>/dev/null; then
        log_error "Failed to extract archive: ${archive_path}"
        return "${EXIT_RUNTIME_ERROR}"
    fi

    return "${EXIT_SUCCESS}"
}

run_restore_preflight() {
    local backup_id="${1:-}"
    local backup_root="${2:-${DEFAULT_BACKUP_ROOT}}"
    local restore_target="${3:-}"
    local single_file="${4:-}"
    local conflict_policy="${5:-fail}"

    validate_backup_id "${backup_id}" || return "$?"
    ensure_destination_directory "${backup_root}" || return "$?"

    if [[ -n "${restore_target}" ]]; then
        ensure_destination_directory "${restore_target}" || return "$?"
    fi

    local canonical_root
    canonical_root="$(resolve_canonical_path "${backup_root}")"

    # If the backup snapshot or catalog exists, run full Python restore engine
    if [[ -d "${canonical_root}/${backup_id}" || -f "${canonical_root}/backup_catalog.db" ]]; then
        local effective_target="${restore_target:-$(pwd)}"
        ensure_destination_directory "${effective_target}" || return "$?"
        local canonical_target
        canonical_target="$(resolve_canonical_path "${effective_target}")"

        local -a py_args=(
            restore
            --backup-id "${backup_id}"
            --dest "${canonical_root}"
            --target "${canonical_target}"
            --conflict "${conflict_policy}"
        )
        if [[ -n "${single_file}" ]]; then
            py_args+=(--file "${single_file}")
        fi

        python3 "${LBM_PROJECT_ROOT}/python/main.py" "${py_args[@]}" || return "$?"
        return "${EXIT_SUCCESS}"
    fi

    # Uninitialized repository pre-flight output
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
    local single_file=""
    local conflict_policy="fail"

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
            -f|--file)
                if [[ $# -lt 2 || -z "${2:-}" ]]; then
                    log_error "Option '$1' requires a relative file path."
                    exit "${EXIT_INVALID_ARGS}"
                fi
                single_file="$2"
                shift 2
                ;;
            --conflict)
                if [[ $# -lt 2 || -z "${2:-}" ]]; then
                    log_error "Option '$1' requires a policy (fail|skip|backup|overwrite)."
                    exit "${EXIT_INVALID_ARGS}"
                fi
                conflict_policy="$2"
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

    run_restore_preflight "${backup_id}" "${backup_root}" "${restore_target}" "${single_file}" "${conflict_policy}" || exit "$?"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi