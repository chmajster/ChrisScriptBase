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

run_case "repo cleanup removes only matching service" '
    source "$LIB"
    RUNNER_SYSTEMD_DIR="$TEST_TMP/systemd-repo"
    mkdir -p "$RUNNER_SYSTEMD_DIR/multi-user.target.wants"
    GITHUB_OWNER=chmajster
    wanted="actions.runner.chmajster-HomeLAB-DNS.kynlab01-homelab-dns.service"
    other="actions.runner.chmajster-HomeLAB-OpenLDAP.kynlab01-homelab-openldap.service"
    touch "$RUNNER_SYSTEMD_DIR/$wanted" "$RUNNER_SYSTEMD_DIR/$other"
    ln -s "../$wanted" "$RUNNER_SYSTEMD_DIR/multi-user.target.wants/$wanted"
    systemctl() {
        case "$1" in
            list-unit-files)
                [[ -e "$RUNNER_SYSTEMD_DIR/$wanted" ]] && printf "%s enabled\n" "$wanted"
                [[ -e "$RUNNER_SYSTEMD_DIR/$other" ]] && printf "%s enabled\n" "$other"
                ;;
            stop|disable|reset-failed|daemon-reload) return 0 ;;
            *) return 0 ;;
        esac
    }
    cleanup_repo_runner_services HomeLAB-DNS
    [[ ! -e "$RUNNER_SYSTEMD_DIR/$wanted" ]]
    [[ ! -e "$RUNNER_SYSTEMD_DIR/multi-user.target.wants/$wanted" ]]
    [[ -e "$RUNNER_SYSTEMD_DIR/$other" ]]
'

run_case "owner cleanup removes legacy repo services" '
    source "$LIB"
    RUNNER_SYSTEMD_DIR="$TEST_TMP/systemd-owner"
    mkdir -p "$RUNNER_SYSTEMD_DIR"
    GITHUB_OWNER=chmajster
    one="actions.runner.chmajster-HomeLAB-DNS.kynlab01-homelab-dns.service"
    two="actions.runner.chmajster-Knightly.kynlab01-knightly.service"
    unrelated="actions.runner.other-Repo.host.service"
    org="actions.runner.chmajster.host-org.service"
    touch "$RUNNER_SYSTEMD_DIR/$one" "$RUNNER_SYSTEMD_DIR/$two" "$RUNNER_SYSTEMD_DIR/$unrelated" "$RUNNER_SYSTEMD_DIR/$org"
    systemctl() {
        case "$1" in
            list-unit-files)
                for unit in "$one" "$two" "$unrelated" "$org"; do
                    [[ -e "$RUNNER_SYSTEMD_DIR/$unit" ]] && printf "%s enabled\n" "$unit"
                done
                ;;
            stop|disable|reset-failed|daemon-reload) return 0 ;;
            *) return 0 ;;
        esac
    }
    cleanup_owner_repo_runner_services
    [[ ! -e "$RUNNER_SYSTEMD_DIR/$one" ]]
    [[ ! -e "$RUNNER_SYSTEMD_DIR/$two" ]]
    [[ -e "$RUNNER_SYSTEMD_DIR/$unrelated" ]]
    [[ -e "$RUNNER_SYSTEMD_DIR/$org" ]]
'

run_case "missing runner directory still removes stale service" '
    source "$LIB"
    RUNNER_BASE="$TEST_TMP/runners-missing"
    RUNNER_SYSTEMD_DIR="$TEST_TMP/systemd-missing"
    mkdir -p "$RUNNER_SYSTEMD_DIR"
    ACTIVE_PROFILE=home
    GITHUB_OWNER=chmajster
    unit="actions.runner.chmajster-HomeLAB-DNS.kynlab01-homelab-dns.service"
    touch "$RUNNER_SYSTEMD_DIR/$unit"
    systemctl() {
        case "$1" in
            list-unit-files) [[ -e "$RUNNER_SYSTEMD_DIR/$unit" ]] && printf "%s enabled\n" "$unit" ;;
            stop|disable|reset-failed|daemon-reload) return 0 ;;
            *) return 0 ;;
        esac
    }
    uninstall_repo_runner HomeLAB-DNS
    [[ ! -e "$RUNNER_SYSTEMD_DIR/$unit" ]]
'

run_case "uninstall all cleans services when profile directory is empty" '
    source "$LIB"
    RUNNER_BASE="$TEST_TMP/runners-all"
    RUNNER_SYSTEMD_DIR="$TEST_TMP/systemd-all"
    mkdir -p "$RUNNER_SYSTEMD_DIR"
    ACTIVE_PROFILE=home
    GITHUB_OWNER=chmajster
    ACTION=uninstall
    REPO_SELECTION_MODE=all
    unit="actions.runner.chmajster-HomeLAB-DNS.kynlab01-homelab-dns.service"
    touch "$RUNNER_SYSTEMD_DIR/$unit"
    resolve_repositories() { return 0; }
    systemctl() {
        case "$1" in
            list-unit-files) [[ -e "$RUNNER_SYSTEMD_DIR/$unit" ]] && printf "%s enabled\n" "$unit" ;;
            stop|disable|reset-failed|daemon-reload) return 0 ;;
            *) return 0 ;;
        esac
    }
    process_user_profile
    [[ ! -e "$RUNNER_SYSTEMD_DIR/$unit" ]]
'

run_case "org cleanup does not remove repository services" '
    source "$LIB"
    RUNNER_SYSTEMD_DIR="$TEST_TMP/systemd-org"
    mkdir -p "$RUNNER_SYSTEMD_DIR"
    GITHUB_OWNER=example-org
    org="actions.runner.example-org.kynlab01-work.service"
    repo="actions.runner.example-org-Repo.kynlab01-repo.service"
    touch "$RUNNER_SYSTEMD_DIR/$org" "$RUNNER_SYSTEMD_DIR/$repo"
    systemctl() {
        case "$1" in
            list-unit-files)
                [[ -e "$RUNNER_SYSTEMD_DIR/$org" ]] && printf "%s enabled\n" "$org"
                [[ -e "$RUNNER_SYSTEMD_DIR/$repo" ]] && printf "%s enabled\n" "$repo"
                ;;
            stop|disable|reset-failed|daemon-reload) return 0 ;;
            *) return 0 ;;
        esac
    }
    cleanup_org_runner_services
    [[ ! -e "$RUNNER_SYSTEMD_DIR/$org" ]]
    [[ -e "$RUNNER_SYSTEMD_DIR/$repo" ]]
'

printf '\nRESULT: pass=%d fail=%d\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
