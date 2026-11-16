#!/usr/bin/env bash
# ==============================================================================
# File: backup.sh
# Description: Top-level convenience entrypoint that delegates directly to
#              bash/backup.sh via exec.
# ==============================================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${ROOT_DIR}/bash/backup.sh" "$@"