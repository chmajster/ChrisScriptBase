#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="${SCRIPT_DIR}/install-github-selfhosted-runners.sh"

if [[ ! -x "$INSTALLER" ]]; then
    echo "ERROR: Brak wykonywalnego instalatora: $INSTALLER" >&2
    echo "Uruchom: chmod +x '$INSTALLER'" >&2
    exit 1
fi

exec "$INSTALLER" --uninstall "$@"
