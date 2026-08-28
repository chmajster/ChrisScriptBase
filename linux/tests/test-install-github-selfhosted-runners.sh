#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT="${1:-linux/install-github-selfhosted-runners.sh}"
[[ -f "$SCRIPT" ]] || { echo "Missing script: $SCRIPT" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0
fail=0

ok(){ echo "PASS: $*"; ((pass += 1)); }
not_ok(){ echo "FAIL: $*" >&2; ((fail += 1)); }

expect_success(){
    local name="$1"; shift
    if "$@" >"$TMP/out" 2>"$TMP/err"; then
        ok "$name"
    else
        not_ok "$name"
        cat "$TMP/out" >&2 || true
        cat "$TMP/err" >&2 || true
    fi
}

expect_failure(){
    local name="$1"; shift
    if "$@" >"$TMP/out" 2>"$TMP/err"; then
        not_ok "$name (unexpected success)"
    else
        ok "$name"
    fi
}

expect_success "bash -n single script" bash -n "$SCRIPT"

if bash "$SCRIPT" --help | grep -Fq 'single-file, Docker'; then
    ok "--help identifies single-file implementation"
else
    not_ok "--help identifies single-file implementation"
fi

if grep -Fq 'github-runner-docker/manager.sh' "$SCRIPT"; then
    not_ok "script has no external manager dependency"
else
    ok "script has no external manager dependency"
fi

expect_failure "reject --all-repos + --select-repos" bash -c 'source "$1"; args --all-repos --select-repos' _ "$SCRIPT"
expect_failure "reject --purge without --uninstall" bash -c 'source "$1"; args --purge' _ "$SCRIPT"
expect_success "accept --include-public" bash -c 'source "$1"; args --include-public; [[ $INCLUDE_PUBLIC == true ]]' _ "$SCRIPT"
expect_success "private-only default" bash -c 'source "$1"; [[ $INCLUDE_PUBLIC == false ]]' _ "$SCRIPT"
expect_success "accept --force-recreate" bash -c 'source "$1"; args --force-recreate; [[ $FORCE_RECREATE == true ]]' _ "$SCRIPT"
expect_success "accept resource limits" bash -c 'source "$1"; args --cpus 2 --memory 4g --pids-limit 256; [[ $RUNNER_CPUS == 2 && $RUNNER_MEMORY == 4g && $RUNNER_PIDS_LIMIT == 256 ]]' _ "$SCRIPT"

expect_success "render embedded Docker context" env SCRIPT="$SCRIPT" TEST_TMP="$TMP" bash -c '
    source "$SCRIPT"
    mkdir -p "$TEST_TMP/context"
    render_docker_context "$TEST_TMP/context"
    [[ -f "$TEST_TMP/context/Dockerfile" ]]
    [[ -f "$TEST_TMP/context/runner-entrypoint.sh" ]]
    bash -n "$TEST_TMP/context/runner-entrypoint.sh"
    grep -Fq "FROM ubuntu:24.04" "$TEST_TMP/context/Dockerfile"
    grep -Fq "actions-runner-linux-" "$TEST_TMP/context/Dockerfile"
'

expect_success "PAT is not passed to container entrypoint" env SCRIPT="$SCRIPT" TEST_TMP="$TMP" bash -c '
    source "$SCRIPT"
    mkdir -p "$TEST_TMP/context2"
    render_docker_context "$TEST_TMP/context2"
    ! grep -Fq "GITHUB_PAT" "$TEST_TMP/context2/runner-entrypoint.sh"
    grep -Fq "runner_registration_token" "$TEST_TMP/context2/runner-entrypoint.sh"
'

expect_success "docker label follows docker socket capability" env SCRIPT="$SCRIPT" bash -c '
    source "$SCRIPT"
    LABELS="homelab,linux,docker"
    SOCKET=false
    [[ $(effective_labels) == homelab,linux ]]
    SOCKET=true
    [[ $(effective_labels) == homelab,linux,docker ]]
'

expect_success "write_state stores short-lived registration token" env SCRIPT="$SCRIPT" TEST_TMP="$TMP" bash -c '
    source "$SCRIPT"
    PROFILE=home
    MODE=user
    OWNER=chmajster
    IMAGE=test/image
    SOCKET=false
    ALLOW_SUDO=false
    chown(){ return 0; }
    write_state "$TEST_TMP/state" repo runner container hash registration-token
    [[ $(stat -c %a "$TEST_TMP/state/registration_token") == 600 ]]
    [[ $(cat "$TEST_TMP/state/registration_token") == registration-token ]]
    [[ ! -e "$TEST_TMP/state/github_token" ]]
'

expect_success "named profile refuses ambiguous root legacy directory" env SCRIPT="$SCRIPT" TEST_TMP="$TMP" bash -c '
    source "$SCRIPT"
    PROFILE=home
    RUNNER_BASE="$TEST_TMP/legacy"
    mkdir -p "$RUNNER_BASE/repo"
    touch "$RUNNER_BASE/repo/.runner"
    if legacy_dirs repo 2>/dev/null | grep -Fq "$RUNNER_BASE/repo"; then
        exit 1
    fi
'

expect_success "named profile accepts matching legacy metadata" env SCRIPT="$SCRIPT" TEST_TMP="$TMP" bash -c '
    source "$SCRIPT"
    PROFILE=home
    RUNNER_BASE="$TEST_TMP/legacy-match"
    mkdir -p "$RUNNER_BASE/repo"
    cat >"$RUNNER_BASE/repo/.chrisscriptbase-runner" <<EOF
profile=home
repo=repo
EOF
    legacy_dirs repo | grep -Fqx "$RUNNER_BASE/repo"
'

if grep -Fq -- '--restart unless-stopped' "$SCRIPT" &&
   grep -Fq -- '--log-opt "max-size=$LOG_MAX_SIZE"' "$SCRIPT" &&
   grep -Fq -- '--pids-limit' "$SCRIPT"; then
    ok "container restart, logging and PID limits are configured"
else
    not_ok "container restart, logging and PID limits are configured"
fi

if grep -Fq 'registration-token' "$SCRIPT" &&
   ! grep -Fq 'dst=/run/secrets/github_token' "$SCRIPT"; then
    ok "long-lived PAT is not mounted into jobs"
else
    not_ok "long-lived PAT is not mounted into jobs"
fi

printf '\nRESULT: pass=%d fail=%d\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
