#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT="${1:-linux/install-github-selfhosted-runners.sh}"
[[ -f "$SCRIPT" ]] || { echo "Missing script: $SCRIPT" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
LIB="$TMP/runner-manager-lib.sh"
sed '/^main "\$@"$/d' "$SCRIPT" > "$LIB"

pass=0
fail=0
ok() { echo "PASS: $*"; ((pass += 1)); }
not_ok() { echo "FAIL: $*" >&2; ((fail += 1)); }

run_case() {
    local name="$1" code="$2"
    if TEST_TMP="$TMP" LIB="$LIB" bash -c "$code" >"$TMP/out" 2>"$TMP/err"; then
        ok "$name"
    else
        not_ok "$name"
        cat "$TMP/out" >&2 || true
        cat "$TMP/err" >&2 || true
    fi
}

run_case "parse legacy service without runner-name suffix" '
    source "$LIB"
    GITHUB_OWNER=chmajster
    ACTIVE_PROFILE=home
    hostname() { echo kynlab01; }
    repo="$(repo_from_service_unit actions.runner.chmajster-Knightly.kynlab01.service)"
    [[ "$repo" == knightly ]]
'

run_case "legacy service is discoverable for explicit repo" '
    source "$LIB"
    GITHUB_OWNER=chmajster
    ACTIVE_PROFILE=home
    hostname() { echo kynlab01; }
    list_action_runner_service_units() {
        echo actions.runner.chmajster-Knightly.kynlab01.service
    }
    unit="$(runner_service_units_for_repo Knightly)"
    [[ "$unit" == actions.runner.chmajster-Knightly.kynlab01.service ]]
'

run_case "uninstall removes orphan legacy service without runner directory" '
    source "$LIB"
    GITHUB_OWNER=chmajster
    ACTIVE_PROFILE=home
    RUNNER_BASE="$TEST_TMP/runners"
    hostname() { echo kynlab01; }
    list_action_runner_service_units() {
        [[ ! -f "$TEST_TMP/legacy-unit-removed" ]] &&
            echo actions.runner.chmajster-Knightly.kynlab01.service
    }
    remove_runner_service_unit() {
        [[ "$1" == actions.runner.chmajster-Knightly.kynlab01.service ]]
        touch "$TEST_TMP/legacy-unit-removed"
        return 0
    }
    uninstall_repo_runner Knightly
    [[ -f "$TEST_TMP/legacy-unit-removed" ]]
'

printf '\nRESULT: pass=%d fail=%d\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
