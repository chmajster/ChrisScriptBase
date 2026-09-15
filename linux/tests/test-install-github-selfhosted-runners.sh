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
expect_success "sudo enabled by default for Actions compatibility" bash -c 'source "$1"; [[ $ALLOW_SUDO == true ]]' _ "$SCRIPT"
expect_success "accept --no-sudo override" bash -c 'source "$1"; args --no-sudo; [[ $ALLOW_SUDO == false ]]' _ "$SCRIPT"
expect_success "accept --force-recreate" bash -c 'source "$1"; args --force-recreate; [[ $FORCE_RECREATE == true && $FORCE_REMOTE_DELETE == true ]]' _ "$SCRIPT"
expect_success "force recreate tolerates conflict delete failure" bash -c '
    source "$1"; FORCE_REMOTE_DELETE=true; GITHUB_API_RETRIES=1; sleep(){ :; }; remote_delete(){ return 43; }; remote_delete_recreate /repos/test/actions/runners test-runner
' _ "$SCRIPT"
expect_failure "force recreate does not hide authorization failure" bash -c '
    source "$1"; FORCE_REMOTE_DELETE=true; remote_delete(){ return 40; }; remote_delete_recreate /repos/test/actions/runners test-runner
' _ "$SCRIPT"
expect_failure "normal reinstall rejects conflict delete failure" bash -c '
    source "$1"; FORCE_REMOTE_DELETE=false; GITHUB_API_RETRIES=1; sleep(){ :; }; remote_delete(){ return 43; }; remote_delete_recreate /repos/test/actions/runners test-runner
' _ "$SCRIPT"
expect_success "accept --prepare-host" bash -c 'source "$1"; args --prepare-host; [[ $PREPARE_HOST == true ]]' _ "$SCRIPT"
expect_success "accept --status" bash -c 'source "$1"; args --status; [[ $STATUS_ONLY == true ]]' _ "$SCRIPT"
expect_success "accept --repair" bash -c 'source "$1"; args --repair; [[ $REPAIR_MODE == true && $REINSTALL_ONLY == true ]]' _ "$SCRIPT"
expect_success "accept --check-updates" bash -c 'source "$1"; args --check-updates; [[ $CHECK_UPDATES == true ]]' _ "$SCRIPT"
expect_success "accept --update-runner" bash -c 'source "$1"; args --update-runner; [[ $UPDATE_RUNNER == true && $REBUILD == true && $REINSTALL_ONLY == true ]]' _ "$SCRIPT"
expect_success "accept resource limits" bash -c 'source "$1"; args --cpus 2 --memory 4g --pids-limit 256; [[ $RUNNER_CPUS == 2 && $RUNNER_MEMORY == 4g && $RUNNER_PIDS_LIMIT == 256 ]]' _ "$SCRIPT"
expect_success "normalize pinned runner version" bash -c 'source "$1"; RUNNER_VERSION=v2.999.1; [[ $(resolve_runner_version) == 2.999.1 ]]' _ "$SCRIPT"

expect_success "apt metadata is refreshed at most once" bash -c '
    source "$1"
    APT_UPDATED=false
    apt_calls=0
    apt-get(){ ((apt_calls += 1)); return 0; }
    apt_update_once
    apt_update_once
    [[ $apt_calls -eq 1 && $APT_UPDATED == true ]]
' _ "$SCRIPT"

expect_success "prepare-host required package list" bash -c '
    source "$1"
    packages=" ${HOST_REQUIRED_PACKAGES[*]} "
    for package in ca-certificates curl jq git coreutils gawk sudo libc-bin findutils grep sed hostname docker.io dialog; do
        [[ "$packages" == *" $package "* ]] || exit 1
    done
' _ "$SCRIPT"

expect_success "package report lists status and versions" bash -c '
    source "$1"
    HOST_REQUIRED_PACKAGES=(curl jq docker.io)
    dpkg-query(){
        local package="${@: -1}"
        if [[ "$*" == *"Status"* ]]; then
            printf "install ok installed"
        else
            printf "test-version-%s" "$package"
        fi
    }
    docker(){
        case "${1:-}" in
            info) return 0 ;;
            version) printf "99.0.0" ;;
        esac
    }
    output="$(host_package_report)"
    grep -Fq "curl" <<< "$output"
    grep -Fq "jq" <<< "$output"
    grep -Fq "docker.io" <<< "$output"
    grep -Fq "test-version-curl" <<< "$output"
    grep -Fq "Docker Engine: OK (wersja 99.0.0)" <<< "$output"
    grep -Fq "Wszystkie wymagane pakiety są zainstalowane (3/3)." <<< "$output"
' _ "$SCRIPT"

expect_success "package manager mappings" bash -c '
    source "$1"; [[ $(package_name_for libc-bin apt) == libc-bin ]]; [[ $(package_name_for libc-bin dnf) == glibc-common ]]; [[ $(package_name_for libc-bin zypper) == glibc ]]; [[ $(package_name_for docker.io zypper) == docker ]]
' _ "$SCRIPT"
expect_success "transactional reinstall restarts old container on API failure" bash -c '
    source "$1"; logf="$(mktemp)"; docker(){ case "$1" in inspect) if [[ "$*" == *State.Running* ]]; then echo true; fi; return 0 ;; stop|start|rm) echo "$1" >>"$logf"; return 0 ;; esac; }; remote_delete_recreate(){ return 41; }; if retire_existing_container /repos/test/actions/runners runner container; then exit 1; fi; grep -Fxq stop "$logf"; grep -Fxq start "$logf"; ! grep -Fxq rm "$logf"; rm -f "$logf"
' _ "$SCRIPT"
expect_success "runner image stores version label" bash -c '
    source "$1"; tmp="$(mktemp -d)"; render_docker_context "$tmp"; grep -Fq "com.chrisscriptbase.runner-version" "$tmp/Dockerfile"; rm -rf "$tmp"
' _ "$SCRIPT"

expect_success "render embedded Docker context" env SCRIPT="$SCRIPT" TEST_TMP="$TMP" bash -c '
    source "$SCRIPT"
    mkdir -p "$TEST_TMP/context"
    render_docker_context "$TEST_TMP/context"
    [[ -f "$TEST_TMP/context/Dockerfile" ]]
    [[ -f "$TEST_TMP/context/runner-entrypoint.sh" ]]
    bash -n "$TEST_TMP/context/runner-entrypoint.sh"
    grep -Fq "FROM ubuntu:24.04" "$TEST_TMP/context/Dockerfile"
    grep -Fq "actions-runner-linux-" "$TEST_TMP/context/Dockerfile"
    grep -Fq "zip unzip" "$TEST_TMP/context/Dockerfile"
    grep -Fq "git-lfs" "$TEST_TMP/context/Dockerfile"
    grep -Fq "openssh-client rsync" "$TEST_TMP/context/Dockerfile"
    grep -Fq "python3-pip python3-venv shellcheck" "$TEST_TMP/context/Dockerfile"
    grep -Fq "ALLOW_SUDO=\"\${RUNNER_ALLOW_SUDO:-true}\"" "$TEST_TMP/context/runner-entrypoint.sh"
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
