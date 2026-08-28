#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANAGER="${SCRIPT_DIR}/github-runner-docker/manager.sh"

if [[ ! -f "$MANAGER" ]]; then
    echo "ERROR: Brak Docker runner managera: $MANAGER" >&2
    exit 1
fi

BASH_FLAGS=()
[[ $- == *x* ]] && BASH_FLAGS+=(-x)
exec bash "${BASH_FLAGS[@]}" "$MANAGER" "$@"
