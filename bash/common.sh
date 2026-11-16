#!/usr/bin/env bash
# ==============================================================================
# File: bash/common.sh
# Description: Shared constants, exit codes, logging helpers, temporary staging,
#              archive execution, and filesystem permission validators.
# ==============================================================================

set -euo pipefail

# Include guard to prevent re-declaring readonly variables when sourced multiple times
if [[ -n "${_LBM_COMMON_SH_LOADED:-}" ]]; then
    return 0
fi
readonly _LBM_COMMON_SH_LOADED=1

# ------------------------------------------------------------------------------
# Standardized Process Exit Codes
# ------------------------------------------------------------------------------
readonly EXIT_SUCCESS=0
readonly EXIT_INVALID_ARGS=1
readonly EXIT_DIR_NOT_FOUND=2
readonly EXIT_PERMISSION_DENIED=3
readonly EXIT_DEPENDENCY_MISSING=4
readonly EXIT_BACKUP_NOT_FOUND=5
readonly EXIT_RUNTIME_ERROR=6

# ------------------------------------------------------------------------------
# Project Paths & Default Configuration
# ------------------------------------------------------------------------------
LBM_BASH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LBM_BASH_DIR
LBM_PROJECT_ROOT="$(cd "${LBM_BASH_DIR}/.." && pwd)"
readonly LBM_PROJECT_ROOT

# Default backup destination can be overridden via LBM_BACKUP_ROOT environment variable
readonly DEFAULT_BACKUP_ROOT="${LBM_BACKUP_ROOT:-${HOME}/backups}"
readonly DEFAULT_LOG_DIR="${LBM_LOG_DIR:-${LBM_PROJECT_ROOT}/logs}"

# Global array tracking temporary staging directories created by the current process
declare -a LBM_ACTIVE_TEMP_DIRS=()

# ------------------------------------------------------------------------------
# Logging Helpers (STDOUT for info, STDERR for warnings and errors)
# ------------------------------------------------------------------------------
log_info() {
    local message="$1"
    printf "[INFO] %s\n" "${message}"
}

log_warn() {
    local message="$1"
    printf "[WARN] %s\n" "${message}" >&2
}

log_error() {
    local message="$1"
    printf "[ERROR] %s\n" "${message}" >&2
}

print_banner() {
    printf "==================================\n"
    printf "      LINUX BACKUP MANAGER\n"
    printf "==================================\n\n"
}

print_footer() {
    printf "\n==================================\n"
}

# ------------------------------------------------------------------------------
# System & Filesystem Validation Functions
# ------------------------------------------------------------------------------

# Verify that required Linux utilities exist in PATH
check_required_commands() {
    local cmd
    for cmd in tar gzip sha256sum find stat realpath mktemp python3; do
        if ! command -v "${cmd}" >/dev/null 2>&1; then
            log_error "Required system utility '${cmd}' is not installed or not in PATH."
            return "${EXIT_DEPENDENCY_MISSING}"
        fi
    done
    return "${EXIT_SUCCESS}"
}

# Validate that a source directory exists, is a directory, and has read + traverse (r+x) permissions
validate_source_directory() {
    local src_path="$1"

    if [[ -z "${src_path}" ]]; then
        log_error "Source directory path cannot be empty."
        return "${EXIT_INVALID_ARGS}"
    fi

    if [[ ! -e "${src_path}" ]]; then
        log_error "Source directory does not exist: ${src_path}"
        return "${EXIT_DIR_NOT_FOUND}"
    fi

    if [[ ! -d "${src_path}" ]]; then
        log_error "Source path is not a directory: ${src_path}"
        return "${EXIT_DIR_NOT_FOUND}"
    fi

    # Both read (-r) and execute/traverse (-x) permissions are required to scan a directory
    if [[ ! -r "${src_path}" || ! -x "${src_path}" ]]; then
        log_error "Permission denied: cannot read or traverse source directory: ${src_path}"
        return "${EXIT_PERMISSION_DENIED}"
    fi

    return "${EXIT_SUCCESS}"
}

# Ensure destination backup directory exists (or can be created) and has write + traverse (w+x) permissions
ensure_destination_directory() {
    local dest_path="$1"

    if [[ -z "${dest_path}" ]]; then
        log_error "Destination directory path cannot be empty."
        return "${EXIT_INVALID_ARGS}"
    fi

    if [[ ! -e "${dest_path}" ]]; then
        if ! mkdir -p "${dest_path}" 2>/dev/null; then
            log_error "Permission denied: unable to create destination directory: ${dest_path}"
            return "${EXIT_PERMISSION_DENIED}"
        fi
    fi

    if [[ ! -d "${dest_path}" ]]; then
        log_error "Destination path exists but is not a directory: ${dest_path}"
        return "${EXIT_DIR_NOT_FOUND}"
    fi

    if [[ ! -w "${dest_path}" || ! -x "${dest_path}" ]]; then
        log_error "Permission denied: destination directory is not writable: ${dest_path}"
        return "${EXIT_PERMISSION_DENIED}"
    fi

    return "${EXIT_SUCCESS}"
}

# Resolve a path to its canonical physical path (without resolving leaf symlinks)
resolve_canonical_path() {
    local target_path="$1"
    realpath -s "${target_path}"
}

# Validate a backup ID string to prevent path traversal or empty values
validate_backup_id() {
    local backup_id="${1:-}"

    if [[ -z "${backup_id}" ]]; then
        log_error "Backup ID cannot be empty."
        return "${EXIT_INVALID_ARGS}"
    fi

    if [[ "${backup_id}" == *"/"* || "${backup_id}" == *".."* || "${backup_id}" == *" "* ]]; then
        log_error "Invalid backup ID '${backup_id}': must not contain spaces, '/', or '..'."
        return "${EXIT_INVALID_ARGS}"
    fi

    return "${EXIT_SUCCESS}"
}

# ------------------------------------------------------------------------------
# Temporary Staging & Signal Trap Helpers
# ------------------------------------------------------------------------------

# Create an isolated 0700 temporary staging directory inside base_dir (defaults to /tmp)
create_staging_dir() {
    local base_dir="${1:-/tmp}"
    ensure_destination_directory "${base_dir}" || return "$?"

    local staging_dir
    if ! staging_dir="$(mktemp -d "${base_dir%/}/.lbm_staging.XXXXXX" 2>/dev/null)"; then
        log_error "Failed to create temporary staging directory in ${base_dir}"
        return "${EXIT_RUNTIME_ERROR}"
    fi

    chmod 700 "${staging_dir}"
    LBM_ACTIVE_TEMP_DIRS+=("${staging_dir}")
    printf "%s\n" "${staging_dir}"
    return "${EXIT_SUCCESS}"
}

# Cleanup handler invoked automatically on EXIT, SIGINT, or SIGTERM
cleanup_active_temp_dirs() {
    local exit_status="$?"
    local dir
    if [[ ${#LBM_ACTIVE_TEMP_DIRS[@]} -gt 0 ]]; then
        for dir in "${LBM_ACTIVE_TEMP_DIRS[@]}"; do
            if [[ -n "${dir}" && -d "${dir}" ]]; then
                rm -rf "${dir}" 2>/dev/null || true
            fi
        done
    fi
    return "${exit_status}"
}

# Register the cleanup handler for EXIT, INT, and TERM signals
register_cleanup_trap() {
    trap cleanup_active_temp_dirs EXIT INT TERM
}

# ------------------------------------------------------------------------------
# Archive & Compression Execution Helpers (tar + gzip)
# ------------------------------------------------------------------------------

# Build a tar (or tar.gz) archive from a NUL-delimited file list
execute_tar_archive() {
    local source_root="$1"
    local null_file_list="$2"
    local output_archive="$3"
    local compress_enabled="${4:-true}"
    local compress_level="${5:-6}"

    if [[ ! -f "${null_file_list}" ]]; then
        log_error "File list for archiving does not exist: ${null_file_list}"
        return "${EXIT_RUNTIME_ERROR}"
    fi

    if [[ "${compress_enabled}" == "true" ]]; then
        if ! tar -C "${source_root}" --no-recursion --null -T "${null_file_list}" -cf - \
            | gzip "-${compress_level}" -n > "${output_archive}"; then
            log_error "Failed to create compressed archive: ${output_archive}"
            return "${EXIT_RUNTIME_ERROR}"
        fi
    else
        if ! tar -C "${source_root}" --no-recursion --null -T "${null_file_list}" -cf "${output_archive}"; then
            log_error "Failed to create uncompressed archive: ${output_archive}"
            return "${EXIT_RUNTIME_ERROR}"
        fi
    fi

    verify_tar_archive "${output_archive}" "${compress_enabled}" || return "$?"
    return "${EXIT_SUCCESS}"
}

# Validate archive stream structure using gzip -t and tar -tf
verify_tar_archive() {
    local archive_path="$1"
    local compress_enabled="${2:-true}"

    if [[ ! -f "${archive_path}" ]]; then
        log_error "Archive file missing during validation: ${archive_path}"
        return "${EXIT_BACKUP_NOT_FOUND}"
    fi

    if [[ "${compress_enabled}" == "true" ]]; then
        if ! gzip -t "${archive_path}" >/dev/null 2>&1; then
            log_error "Corrupted gzip stream detected in archive: ${archive_path}"
            return "${EXIT_RUNTIME_ERROR}"
        fi
        if ! tar -tzf "${archive_path}" >/dev/null 2>&1; then
            log_error "Corrupted tar structure detected in archive: ${archive_path}"
            return "${EXIT_RUNTIME_ERROR}"
        fi
    else
        if ! tar -tf "${archive_path}" >/dev/null 2>&1; then
            log_error "Corrupted tar structure detected in archive: ${archive_path}"
            return "${EXIT_RUNTIME_ERROR}"
        fi
    fi

    return "${EXIT_SUCCESS}"
}