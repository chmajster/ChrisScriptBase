#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT="${1:-linux/install-github-selfhosted-runners.sh}"
MANAGER="linux/github-runner-docker/manager.sh"
ENTRYPOINT="linux/github-runner-docker/entrypoint.sh"
DOCKERFILE="linux/github-runner-docker/Dockerfile"

for file in "$SCRIPT" "$MANAGER" "$ENTRYPOINT" "$DOCKERFILE"; do
    [[ -f "$file" ]] || { echo "Missing file: $file" >&2; exit 1; }
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
LIB="$TMP/manager-lib.sh"
sed '/^main "\$@"$/d' "$MANAGER" > "$LIB"

pass=0
fail=0
ok() { echo "PASS: $*"; ((pass += 1)); }
not_ok() { echo "FAIL: $*" >&2; ((fail += 1)); }

expect_success() {
    local name="$1"; shift
    if "$@" >"$TMP/out" 2>"$TMP/err"; then
        ok "$name"
    else
        not_ok "$name"
        cat "$TMP/out" >&2 || true
        cat "$TMP/err" >&2 || true
    fi
}

expect_failure() {
    local name="$1"; shift
    if "$@" >"$TMP/out" 2>"$TMP/err"; then
        not_ok "$name (unexpected success)"
    else
        ok "$name"
    fi
}

mkdir -p "$TMP/home"
TOKEN_B64="$(printf test-token | base64 | tr -d '\n')"
cat > "$TMP/home/.gitconfig" <<EOF
[github]
    mode = user
    username = chmajster
    tokenBase64 = $TOKEN_B64
    labels = homelab,linux
[github "home"]
    mode = user
    username = chmajster
    tokenBase64 = $TOKEN_B64
    labels = homelab,linux,docker
[github "work"]
    mode = org
    organization = example-org
    tokenBase64 = $TOKEN_B64
EOF

expect_success "bash -n wrapper" bash -n "$SCRIPT"
expect_success "bash -n manager" bash -n "$MANAGER"
expect_success "bash -n entrypoint" bash -n "$ENTRYPOINT"

if bash "$SCRIPT" --help | grep -Fq 'GitHub Self-Hosted Runner Manager (Docker)'; then
    ok "--help describes Docker manager"
else
    not_ok "--help describes Docker manager"
fi

expect_failure "reject --all-repos + --select-repos" env LIB="$LIB" bash -c 'source "$LIB"; args --all-repos --select-repos'
expect_failure "reject --all-repos + --repo" env LIB="$LIB" bash -c 'source "$LIB"; args --all-repos --repo Repo'
expect_failure "reject --select-repos + --repo" env LIB="$LIB" bash -c 'source "$LIB"; args --select-repos --repo Repo'
expect_failure "reject --purge without --uninstall" env LIB="$LIB" bash -c 'source "$LIB"; args --purge'
expect_success "accept --uninstall --purge" env LIB="$LIB" bash -c 'source "$LIB"; args --uninstall --purge; [[ $ACTION == uninstall && $PURGE == true ]]'
expect_success "accept repeated --repo" env LIB="$LIB" bash -c 'source "$LIB"; args --repo A --repo B; [[ ${#REPOS[@]} -eq 2 ]]'
expect_success "accept --repos CSV" env LIB="$LIB" bash -c 'source "$LIB"; args --repos A,B; [[ ${#REPOS[@]} -eq 2 ]]'
expect_success "accept --profiles CSV" env LIB="$LIB" bash -c 'source "$LIB"; args --profiles home,work; [[ ${#PROFILES[@]} -eq 2 ]]'
expect_success "accept -p alias" env LIB="$LIB" bash -c 'source "$LIB"; args -p home; [[ ${PROFILES[0]} == home ]]'
expect_success "accept -r alias" env LIB="$LIB" bash -c 'source "$LIB"; args -r Repo; [[ ${REPOS[0]} == Repo ]]'
expect_success "accept --select-repos --gui" env LIB="$LIB" bash -c 'source "$LIB"; args --select-repos --gui; [[ $SELECT_MODE == interactive && $UI == gui ]]'
expect_success "accept --select-repos -GUI" env LIB="$LIB" bash -c 'source "$LIB"; args --select-repos -GUI; [[ $SELECT_MODE == interactive && $UI == gui ]]'
expect_success "accept --select-repos --tui" env LIB="$LIB" bash -c 'source "$LIB"; args --select-repos --tui; [[ $SELECT_MODE == interactive && $UI == tui ]]'
expect_success "accept --docker-socket" env LIB="$LIB" bash -c 'source "$LIB"; args --docker-socket; [[ $SOCKET == true ]]'
expect_success "accept --no-docker-socket" env LIB="$LIB" bash -c 'source "$LIB"; SOCKET=true; args --no-docker-socket; [[ $SOCKET == false ]]'
expect_success "accept --rebuild-image" env LIB="$LIB" bash -c 'source "$LIB"; args --rebuild-image; [[ $REBUILD == true ]]'

expect_success "load user profile from gitconfig" env LIB="$LIB" TEST_TMP="$TMP" bash -c '
    source "$LIB"
    GITCONFIG="$TEST_TMP/home/.gitconfig"
    load_profile home
    [[ $PROFILE == home ]]
    [[ $MODE == user ]]
    [[ $OWNER == chmajster ]]
    [[ $TOKEN == test-token ]]
    [[ $LABELS == homelab,linux,docker ]]
'

expect_success "load org profile from gitconfig" env LIB="$LIB" TEST_TMP="$TMP" bash -c '
    source "$LIB"
    GITCONFIG="$TEST_TMP/home/.gitconfig"
    load_profile work
    [[ $PROFILE == work ]]
    [[ $MODE == org ]]
    [[ $OWNER == example-org ]]
    [[ $TOKEN == test-token ]]
'

expect_success "deterministic Docker names" env LIB="$LIB" bash -c '
    source "$LIB"
    PROFILE=home
    OWNER=chmajster
    hostname() { echo KynLab01; }
    [[ $(repo_runner HomeLAB-DNS) == kynlab01-home-homelab-dns ]]
    [[ $(repo_container HomeLAB-DNS) == github-runner-home-homelab-dns ]]
    [[ $(org_container) == github-runner-home-org ]]
'

expect_success "state token is mode 600" env LIB="$LIB" TEST_TMP="$TMP" bash -c '
    source "$LIB"
    PROFILE=home
    MODE=user
    OWNER=chmajster
    LABELS=homelab,docker
    IMAGE=test/image:local
    SOCKET=false
    TOKEN=test-token
    chown() { return 0; }
    write_state "$TEST_TMP/state" HomeLAB-DNS runner-name container-name
    [[ $(stat -c %a "$TEST_TMP/state/github_token") == 600 ]]
    [[ $(cat "$TEST_TMP/state/github_token") == test-token ]]
    grep -Fqx "container_name=container-name" "$TEST_TMP/state/metadata"
'

expect_success "docker run uses restart policy and persistent workdir" env LIB="$LIB" TEST_TMP="$TMP" bash -c '
    source "$LIB"
    PROFILE=home
    MODE=user
    OWNER=chmajster
    LABELS=homelab,docker
    IMAGE=test/image:local
    SOCKET=false
    TOKEN=test-token
    chown() { return 0; }
    sleep() { return 0; }
    docker() {
        if [[ $1 == inspect && ${2:-} == -f ]]; then
            echo true
            return 0
        fi
        if [[ $1 == inspect ]]; then
            return 1
        fi
        if [[ $1 == run ]]; then
            printf "%s\n" "$*" > "$TEST_TMP/docker-run"
            return 0
        fi
        return 0
    }
    run_container "$TEST_TMP/docker-state" repo HomeLAB-DNS runner-name container-name
    grep -Fq -- "--restart unless-stopped" "$TEST_TMP/docker-run"
    grep -Fq -- "com.chrisscriptbase.github-runner=true" "$TEST_TMP/docker-run"
    grep -Fq -- "RUNNER_SCOPE=repo" "$TEST_TMP/docker-run"
    grep -Fq -- "RUNNER_NAME=runner-name" "$TEST_TMP/docker-run"
    grep -Fq -- "$TEST_TMP/docker-state/work" "$TEST_TMP/docker-run"
    grep -Fq -- "/run/secrets/github_token" "$TEST_TMP/docker-run"
'

expect_success "legacy runner is removed before Docker migration" env LIB="$LIB" TEST_TMP="$TMP" bash -c '
    source "$LIB"
    PROFILE=home
    OWNER=chmajster
    RUNNER_BASE="$TEST_TMP/legacy-root"
    dir="$RUNNER_BASE/profiles/home/homelab-dns"
    mkdir -p "$dir"
    printf "%s\n" "{\"agentName\":\"old-runner\"}" > "$dir/.runner"
    remote_delete() { printf "%s %s\n" "$1" "$2" > "$TEST_TMP/remote-delete"; }
    legacy_cleanup HomeLAB-DNS
    [[ ! -d "$dir" ]]
    grep -Fqx "/repos/chmajster/HomeLAB-DNS/actions/runners old-runner" "$TEST_TMP/remote-delete"
'

expect_success "local repo discovery includes Docker state" env LIB="$LIB" TEST_TMP="$TMP" bash -c '
    source "$LIB"
    PROFILE=home
    STATE_BASE="$TEST_TMP/docker-root"
    RUNNER_BASE="$TEST_TMP/legacy-none"
    d="$(repo_state HomeLAB-DNS)"
    mkdir -p "$d"
    cat > "$d/metadata" <<EOF
profile=home
mode=user
owner=chmajster
repo=HomeLAB-DNS
runner_name=test
container_name=test
EOF
    local_repos | grep -Fx HomeLAB-DNS >/dev/null
'

expect_success "user process installs selected Docker runners" env LIB="$LIB" TEST_TMP="$TMP" bash -c '
    source "$LIB"
    MODE=user
    PROFILE=home
    ACTION=install
    LIST_REPOS=false
    resolve() { printf "%s\n" HomeLAB-DNS Algen-server-web-explorer-panel; }
    install_repo() { echo "install:$1" >> "$TEST_TMP/actions"; return 0; }
    : > "$TEST_TMP/actions"
    process
    [[ $(grep -c "^install:" "$TEST_TMP/actions") -eq 2 ]]
'

expect_success "org process installs organization Docker runner" env LIB="$LIB" TEST_TMP="$TMP" bash -c '
    source "$LIB"
    MODE=org
    PROFILE=work
    ACTION=install
    LIST_REPOS=false
    install_org() { echo install-org > "$TEST_TMP/org-action"; return 0; }
    process
    grep -Fqx install-org "$TEST_TMP/org-action"
'

if grep -Fq 'restart unless-stopped' "$MANAGER" && grep -Fq 'docker.sock' "$MANAGER"; then
    ok "manager contains Docker restart and socket support"
else
    not_ok "manager contains Docker restart and socket support"
fi

if grep -Fq 'USER runner' "$DOCKERFILE"; then
    not_ok "Dockerfile must not start entrypoint as runner"
else
    ok "Dockerfile starts privileged bootstrap for root-only secret"
fi

if grep -Fq 'sudo -u runner -H ./run.sh' "$ENTRYPOINT"; then
    ok "runner process drops privileges"
else
    not_ok "runner process drops privileges"
fi

printf '\nRESULT: pass=%d fail=%d\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
