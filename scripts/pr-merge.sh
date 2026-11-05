#!/usr/bin/env bash
set -euo pipefail
# Invoking this command requires review and explicit authorization for this exact head.
exec python3 -B "$(dirname "$0")/pr_workflow.py" merge "$@"
