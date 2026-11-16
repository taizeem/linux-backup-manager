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

#Print CLI usage instructions
usage() {
    cat <<EOF
Usage: $(basename "$0") <command> [arguments] [options]

Commands:
  create <source_dir>      Create a new backup of <source_dir>
  list                     List all recorded backups
  restore <backup_id>      Restore a specific backup by ID
  verify <backup_id>       Verify integrity of a specific backup by ID
  delete <backup_id>       Delete a specific backup by ID

Options:
  -d, --dest <dest_dir>    Override destination backup root directory
                           (Default: \$LBM_BACKUP_ROOT or ~/backups)
  -h, --help               Show this help message and exit

Examples:
  ./bash/backup.sh create ~/Documents
  ./bash/backup.sh list
  ./bash/backup.sh verify backup_2026_11_16
  ./bash/backup.sh restore backup_2026_11_16
  ./bash/backup.sh delete backup_2026_11_16
EOF
}

# Handle 'create <source_dir>' command
cmd_create() {
    local source_arg="${1:-}"
    local dest_root="${2:-${DEFAULT_BACKUP_ROOT}}"

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

    print_banner
    printf "Source: %s\n" "${canonical_src}"
    printf "Destination: %s\n\n" "${canonical_dest}"
    printf "Files discovered: 0\n"
    printf "Files backed up: 0\n"
    printf "Files skipped: 0\n\n"
    printf "Backup size: 0 MB\n"
    printf "Compression: Enabled\n\n"
    printf "Backup status: READY \n"
    print_footer
    exit "${EXIT_SUCCESS}"
}

# Handle 'list' command
cmd_list() {
    local dest_root="${1:-${DEFAULT_BACKUP_ROOT}}"
    ensure_destination_directory "${dest_root}" || exit "$?"

    local canonical_dest
    canonical_dest="$(resolve_canonical_path "${dest_root}")"

    print_banner
    printf "Destination: %s\n" "${canonical_dest}"
    printf "Recorded backups: (Catalog integration pending Phase 6)\n"
    print_footer
    exit "${EXIT_SUCCESS}"
}

# Handle 'restore', 'verify', and 'delete' commands that require a <backup_id>
cmd_backup_id_action() {
    local action="$1"
    local backup_id="${2:-}"
    local dest_root="${3:-${DEFAULT_BACKUP_ROOT}}"

    if [[ -z "${backup_id}" ]]; then
        log_error "'${action}' requires a backup ID argument (e.g., backup_2026_11_16)."
        usage >&2
        exit "${EXIT_INVALID_ARGS}"
    fi

    # Reject path traversal characters in backup_id for safety
    if [[ "${backup_id}" == *"/"* || "${backup_id}" == *".."* ]]; then
        log_error "Invalid backup ID '${backup_id}': must not contain path separators or '..'."
        exit "${EXIT_INVALID_ARGS}"
    fi

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
            create|list|restore|verify|delete)
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
            cmd_create "${target_arg}" "${dest_override}"
            ;;
        list)
            if [[ -n "${target_arg}" ]]; then
                log_error "'list' does not accept positional arguments."
                exit "${EXIT_INVALID_ARGS}"
            fi
            cmd_list "${dest_override}"
            ;;
        restore|verify|delete)
            cmd_backup_id_action "${subcommand}" "${target_arg}" "${dest_override}"
            ;;
    esac
}

main "$@"