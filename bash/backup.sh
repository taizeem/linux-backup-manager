#!/usr/bin/env bash
# ==============================================================================
# File: bash/backup.sh
# Description: Primary CLI entry point for the Linux Backup Manager.
#              Validates arguments, permissions, and dispatches subcommands.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bash/common.sh
source "${SCRIPT_DIR}/common.sh"
# shellcheck source=bash/restore.sh
source "${SCRIPT_DIR}/restore.sh"
# shellcheck source=bash/cleanup.sh
source "${SCRIPT_DIR}/cleanup.sh"

register_cleanup_trap

# Print CLI usage instructions
usage() {
    cat <<EOF
Usage: $(basename "$0") <command> [arguments] [options]

Commands:
  create <source_dir>      Create a new backup of <source_dir>
  list                     List all recorded backups
  restore <backup_id>      Restore a specific backup by ID
  verify <backup_id>       Verify integrity of a specific backup by ID
  delete <backup_id>       Delete a specific backup by ID
  cleanup                  Remove stale temporary staging artifacts

Options:
  -d, --dest <dest_dir>    Override destination backup root directory
                           (Default: \$LBM_BACKUP_ROOT or ~/backups)
  -i, --incremental        Create an incremental backup relative to latest snapshot
  -h, --help               Show this help message and exit

Examples:
  ./backup.sh create ~/Documents
  ./backup.sh create ~/Documents --incremental
  ./backup.sh list
  ./backup.sh verify backup_2026_11_16
  ./backup.sh restore backup_2026_11_16
  ./backup.sh delete backup_2026_11_16
EOF
}

# Handle 'create <source_dir>' command
cmd_create() {
    local source_arg="${1:-}"
    local dest_root="${2:-${DEFAULT_BACKUP_ROOT}}"
    local incremental_flag="${3:-false}"

    if [[ -z "${source_arg}" ]]; then
        log_error "'create' requires a source directory argument."
        usage >&2
        exit "${EXIT_INVALID_ARGS}"
    fi

    # Validate source directory existence and r+x permissions
    validate_source_directory "${source_arg}" || exit "$?"

    # Ensure destination root exists and has w+x permissions
    ensure_destination_directory "${dest_root}" || exit "$?"

    local canonical_src
    canonical_src="$(resolve_canonical_path "${source_arg}")"
    local canonical_dest
    canonical_dest="$(resolve_canonical_path "${dest_root}")"

    # Prevent backing up a directory into itself
    if [[ "${canonical_dest}" == "${canonical_src}" || "${canonical_dest}" == "${canonical_src}/"* ]]; then
        log_error "Destination directory (${canonical_dest}) cannot be inside source directory (${canonical_src})."
        exit "${EXIT_INVALID_ARGS}"
    fi

    # Create and verify atomic staging directory (automatically removed on EXIT by trap)
    local staging_dir
    staging_dir="$(create_staging_dir "${canonical_dest}")" || exit "$?"
    LBM_ACTIVE_TEMP_DIRS+=("${staging_dir}")

    local -a py_args=(
        create
        --source "${canonical_src}"
        --dest "${canonical_dest}"
        --staging "${staging_dir}"
    )
    if [[ "${incremental_flag}" == "true" ]]; then
        py_args+=(--incremental)
    fi

    # Delegate file discovery and backup orchestration to the Python engine
    python3 "${LBM_PROJECT_ROOT}/python/main.py" "${py_args[@]}" || exit "$?"

    exit "${EXIT_SUCCESS}"
}

# Handle 'list' command
cmd_list() {
    local dest_root="${1:-${DEFAULT_BACKUP_ROOT}}"
    ensure_destination_directory "${dest_root}" || exit "$?"

    local canonical_dest
    canonical_dest="$(resolve_canonical_path "${dest_root}")"

    python3 "${LBM_PROJECT_ROOT}/python/main.py" list --dest "${canonical_dest}" || exit "$?"
    exit "${EXIT_SUCCESS}"
}

# Handle 'verify' and 'delete' commands that require a <backup_id>
cmd_backup_id_action() {
    local action="$1"
    local backup_id="${2:-}"
    local dest_root="${3:-${DEFAULT_BACKUP_ROOT}}"

    if [[ -z "${backup_id}" ]]; then
        log_error "'${action}' requires a backup ID argument (e.g., backup_2026_11_16)."
        usage >&2
        exit "${EXIT_INVALID_ARGS}"
    fi

    validate_backup_id "${backup_id}" || exit "$?"
    ensure_destination_directory "${dest_root}" || exit "$?"

    local canonical_dest
    canonical_dest="$(resolve_canonical_path "${dest_root}")"

    print_banner
    printf "Action: %s\n" "${action}"
    printf "Backup ID: %s\n" "${backup_id}"
    printf "Destination: %s\n" "${canonical_dest}"
    print_footer
    exit "${EXIT_SUCCESS}"
}

main() {
    if [[ $# -eq 0 ]]; then
        log_error "No command specified."
        usage >&2
        exit "${EXIT_INVALID_ARGS}"
    fi

    check_required_commands || exit "$?"

    local subcommand=""
    local target_arg=""
    local dest_override="${DEFAULT_BACKUP_ROOT}"
    local incremental_mode="false"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)
                usage
                exit "${EXIT_SUCCESS}"
                ;;
            -d|--dest)
                if [[ $# -lt 2 || -z "${2:-}" ]]; then
                    log_error "Option '$1' requires a destination directory path."
                    exit "${EXIT_INVALID_ARGS}"
                fi
                dest_override="$2"
                shift 2
                ;;
            -i|--incremental)
                incremental_mode="true"
                shift
                ;;
            create|list|restore|verify|delete|cleanup)
                if [[ -z "${subcommand}" ]]; then
                    subcommand="$1"
                    shift
                else
                    log_error "Multiple commands specified ('${subcommand}' and '$1')."
                    exit "${EXIT_INVALID_ARGS}"
                fi
                ;;
            -*)
                log_error "Unknown option: $1"
                usage >&2
                exit "${EXIT_INVALID_ARGS}"
                ;;
            *)
                if [[ -z "${target_arg}" ]]; then
                    target_arg="$1"
                    shift
                else
                    log_error "Unexpected extra positional argument: $1"
                    usage >&2
                    exit "${EXIT_INVALID_ARGS}"
                fi
                ;;
        esac
    done

    if [[ -z "${subcommand}" ]]; then
        log_error "No valid command specified."
        usage >&2
        exit "${EXIT_INVALID_ARGS}"
    fi

    case "${subcommand}" in
        create)
            cmd_create "${target_arg}" "${dest_override}" "${incremental_mode}"
            ;;
        list)
            if [[ -n "${target_arg}" ]]; then
                log_error "'list' does not accept positional arguments."
                exit "${EXIT_INVALID_ARGS}"
            fi
            cmd_list "${dest_override}"
            ;;
        restore)
            if [[ -z "${target_arg}" ]]; then
                log_error "'restore' requires a backup ID argument (e.g., backup_2026_11_16)."
                usage >&2
                exit "${EXIT_INVALID_ARGS}"
            fi
            run_restore_preflight "${target_arg}" "${dest_override}" "" || exit "$?"
            ;;
        verify|delete)
            cmd_backup_id_action "${subcommand}" "${target_arg}" "${dest_override}"
            ;;
        cleanup)
            ensure_destination_directory "${dest_override}" || exit "$?"
            purge_stale_temp_artifacts "${dest_override}" || exit "$?"
            ;;
    esac
}

main "$@"