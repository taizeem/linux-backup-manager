#!/usr/bin/env bash
# ==============================================================================
# File: scripts/schedule_cron.sh
# Description: Configures, prints, installs, or removes scheduled cron backups.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=bash/common.sh
source "${PROJECT_ROOT}/bash/common.sh"

readonly CRON_TAG="# LINUX_BACKUP_MANAGER_AUTO_JOB"

usage() {
    cat <<EOF
Usage: $(basename "$0") <action> [options]

Actions:
  --print                  Print the generated crontab line without modifying crontab
  --install                Install or update the scheduled backup in user's crontab
  --status                 Show current Linux Backup Manager crontab schedule
  --remove                 Remove Linux Backup Manager schedule from user's crontab

Options:
  -s, --source <dir>       Source directory to back up (Required for --print / --install)
  -d, --dest <dir>         Destination backup root (Default: ~/backups)
  -c, --schedule <expr>    5-field cron schedule expression (Default: "0 2 * * *" daily 2 AM)
  -i, --incremental        Schedule incremental backups
  -k, --keep <count>       Retention count to keep (Default: 10)
  -h, --help               Show this help message and exit
EOF
}

validate_cron_expression() {
    local expr="$1"
    # Verify expression contains exactly 5 whitespace-separated fields
    local -a fields
    read -r -a fields <<< "${expr}"
    if [[ ${#fields[@]} -ne 5 ]]; then
        log_error "Invalid cron expression '${expr}': must contain exactly 5 fields (min hour dom mon dow)."
        return "${EXIT_INVALID_ARGS}"
    fi
    return "${EXIT_SUCCESS}"
}

build_cron_line() {
    local schedule_expr="$1"
    local source_dir="$2"
    local dest_dir="$3"
    local incremental="$4"
    local keep_count="$5"

    validate_cron_expression "${schedule_expr}" || return "$?"
    validate_source_directory "${source_dir}" || return "$?"
    ensure_destination_directory "${dest_dir}" || return "$?"

    local canonical_src
    canonical_src="$(resolve_canonical_path "${source_dir}")"
    local canonical_dest
    canonical_dest="$(resolve_canonical_path "${dest_dir}")"

    mkdir -p "${DEFAULT_LOG_DIR}"
    local cron_log="${DEFAULT_LOG_DIR}/cron.log"
    local runner="${PROJECT_ROOT}/scripts/cron_runner.sh"

    local inc_flag=""
    if [[ "${incremental}" == "true" ]]; then
        inc_flag=" --incremental"
    fi

    printf "%s \"%s\" \"%s\" --dest \"%s\"%s --keep %s >> \"%s\" 2>&1 %s\n" \
        "${schedule_expr}" "${runner}" "${canonical_src}" "${canonical_dest}" \
        "${inc_flag}" "${keep_count}" "${cron_log}" "${CRON_TAG}"
}

main() {
    local action=""
    local source_dir=""
    local dest_dir="${DEFAULT_BACKUP_ROOT}"
    local schedule_expr="0 2 * * *"
    local incremental="true"
    local keep_count="10"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)
                usage
                exit "${EXIT_SUCCESS}"
                ;;
            --print|--install|--status|--remove)
                action="$1"
                shift
                ;;
            -s|--source)
                source_dir="${2:-}"
                shift 2
                ;;
            -d|--dest)
                dest_dir="${2:-}"
                shift 2
                ;;
            -c|--schedule)
                schedule_expr="${2:-}"
                shift 2
                ;;
            -i|--incremental)
                incremental="true"
                shift
                ;;
            --full)
                incremental="false"
                shift
                ;;
            -k|--keep)
                keep_count="${2:-10}"
                shift 2
                ;;
            *)
                log_error "Unknown argument: $1"
                usage >&2
                exit "${EXIT_INVALID_ARGS}"
                ;;
        esac
    done

    if [[ -z "${action}" ]]; then
        log_error "Specify an action: --print, --install, --status, or --remove."
        usage >&2
        exit "${EXIT_INVALID_ARGS}"
    fi

    case "${action}" in
        --print)
            if [[ -z "${source_dir}" ]]; then
                log_error "--print requires --source <dir>."
                exit "${EXIT_INVALID_ARGS}"
            fi
            build_cron_line "${schedule_expr}" "${source_dir}" "${dest_dir}" "${incremental}" "${keep_count}"
            ;;
        --install)
            if [[ -z "${source_dir}" ]]; then
                log_error "--install requires --source <dir>."
                exit "${EXIT_INVALID_ARGS}"
            fi
            local cron_line
            cron_line="$(build_cron_line "${schedule_expr}" "${source_dir}" "${dest_dir}" "${incremental}" "${keep_count}")" || exit "$?"
            (
                crontab -l 2>/dev/null | grep -v "${CRON_TAG}" || true
                printf "%s\n" "${cron_line}"
            ) | crontab -
            log_info "Installed scheduled backup in crontab: ${cron_line}"
            ;;
        --status)
            if crontab -l 2>/dev/null | grep "${CRON_TAG}"; then
                return "${EXIT_SUCCESS}"
            else
                log_info "No scheduled Linux Backup Manager jobs found in crontab."
            fi
            ;;
        --remove)
            (crontab -l 2>/dev/null | grep -v "${CRON_TAG}" || true) | crontab -
            log_info "Removed Linux Backup Manager scheduled jobs from crontab."
            ;;
    esac
}

main "$@"