#!/usr/bin/env bash
# ==============================================================================
# File: bash/common.sh
# Description: Shared constants, exit codes, logging helpers, and filesystem
#              permission validators for Linux Backup Manager.
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
    for cmd in tar gzip sha256sum find stat realpath python3; do
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

# Resolve a path to its canonical physical path (without resolving leaf symlinks if needed, or full canonical)
resolve_canonical_path() {
    local target_path="$1"
    realpath -s "${target_path}"
}