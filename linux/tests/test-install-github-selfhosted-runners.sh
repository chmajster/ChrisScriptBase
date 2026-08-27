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
TOKEN="$(printf test-token | base64 | tr -d '\n')"
cat > "$TMP/home/.gitconfig" <<EOF
[github]
    mode = user
    username = chmajster
    tokenBase64 = $TOKEN
    labels = homelab,linux
[github "home"]
    mode = user
    username = chmajster
    tokenBase64 = $TOKEN
    labels = homelab,linux
[github "work"]
    mode = org
    organization = example-org
    tokenBase64 = $TOKEN
EOF

expect_success "bash -n" bash -n "$SCRIPT"
expect_success "--help" bash "$SCRIPT" --help
expect_success "-h" bash "$SCRIPT" -h

expect_failure "reject --all-repos + --select-repos" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --all-repos --select-repos'
expect_failure "reject --all-repos + --repo" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --all-repos --repo Repo'
expect_failure "reject --select-repos + --repo" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --select-repos --repo Repo'
expect_failure "reject --gui without --select-repos" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --gui'
expect_failure "reject --tui without --select-repos" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --tui'
expect_failure "reject --zenity without --select-repos" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --zenity'
expect_failure "reject --purge without --uninstall" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --purge'
expect_success "accept --uninstall --purge" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --uninstall --purge'
expect_success "accept repeated --repo" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --repo A --repo B; [[ ${#SELECTED_REPOS[@]} -eq 2 ]]'
expect_success "accept --repos CSV" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --repos A,B; [[ ${#SELECTED_REPOS[@]} -eq 2 ]]'
expect_success "accept --profiles CSV" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --profiles home,work; [[ ${#SELECTED_PROFILES[@]} -eq 2 ]]'
expect_success "accept -p alias" env LIB="$LIB" bash -c 'source "$LIB"; parse_args -p home; [[ ${SELECTED_PROFILES[0]} == home ]]'
expect_success "accept -r alias" env LIB="$LIB" bash -c 'source "$LIB"; parse_args -r Repo; [[ ${SELECTED_REPOS[0]} == Repo ]]'
expect_success "accept --select-repos --gui" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --select-repos --gui; [[ $SELECTION_UI == gui ]]'
expect_success "accept --select-repos -GUI" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --select-repos -GUI; [[ $SELECTION_UI == gui ]]'
expect_success "accept --select-repos --tui" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --select-repos --tui; [[ $SELECTION_UI == tui ]]'
expect_success "accept --select-repos --zenity" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --select-repos --zenity; [[ $SELECTION_UI == zenity ]]'
expect_success "purge_if_empty disabled returns 0" env LIB="$LIB" bash -c 'source "$LIB"; PURGE=false; purge_if_empty'

run_main() {
    : > "$TMP/actions"
    : > "$TMP/sideeffects"
    env LIB="$LIB" TEST_TMP="$TMP" bash -c '
        source "$LIB"
        init_invoking_user() { INVOKING_USER=tester; INVOKING_HOME="$TEST_TMP/home"; GITCONFIG_PATH="$TEST_TMP/home/.gitconfig"; }
        require_root() { return 0; }
        ensure_dependencies() { return 0; }
        ensure_selection_ui_dependencies() { return 0; }
        validate_github_token() { return 0; }
        create_runner_user() { echo create_runner_user >> "$TEST_TMP/sideeffects"; return 0; }
        get_runner_version() { echo get_runner_version >> "$TEST_TMP/sideeffects"; echo 9.9.9; }
        detect_arch() { echo detect_arch >> "$TEST_TMP/sideeffects"; echo x64; }
        get_user_repositories() { printf "%s\n" HomeLAB-DNS Algen-server-web-explorer-panel; }
        get_local_repositories() { printf "%s\n" HomeLAB-DNS Algen-server-web-explorer-panel; }
        interactive_select_repositories() { echo HomeLAB-DNS; }
        install_repo_runner() { echo "install:$ACTIVE_PROFILE:$1" >> "$TEST_TMP/actions"; return 0; }
        uninstall_repo_runner() { echo "uninstall:$ACTIVE_PROFILE:$1" >> "$TEST_TMP/actions"; return 0; }
        install_org_runner() { echo "install-org:$ACTIVE_PROFILE" >> "$TEST_TMP/actions"; return 0; }
        uninstall_org_runner() { echo "uninstall-org:$ACTIVE_PROFILE" >> "$TEST_TMP/actions"; return 0; }
        purge_if_empty() { echo purge >> "$TEST_TMP/actions"; return 0; }
        systemctl() { return 0; }
        main "$@"
    ' _ "$@"
}

if run_main --profile home --list-repos >"$TMP/list.out" 2>"$TMP/list.err" &&
   grep -Fx HomeLAB-DNS "$TMP/list.out" >/dev/null &&
   grep -Fx Algen-server-web-explorer-panel "$TMP/list.out" >/dev/null; then
    ok "--list-repos prints repositories"
else
    not_ok "--list-repos prints repositories"
    cat "$TMP/list.out" >&2 || true
    cat "$TMP/list.err" >&2 || true
fi
if [[ ! -s "$TMP/sideeffects" ]]; then ok "--list-repos has no install side effects"; else not_ok "--list-repos has no install side effects"; cat "$TMP/sideeffects" >&2; fi

if run_main --profile work --list-repos >/dev/null 2>&1 && [[ ! -s "$TMP/actions" && ! -s "$TMP/sideeffects" ]]; then
    ok "org --list-repos has no runner side effects"
else
    not_ok "org --list-repos has no runner side effects"
    cat "$TMP/actions" >&2 || true
    cat "$TMP/sideeffects" >&2 || true
fi

if run_main --profile home --select-repos --gui >/dev/null 2>&1 && grep -Fqx 'install:home:HomeLAB-DNS' "$TMP/actions"; then ok "HELP: SSH GUI install"; else not_ok "HELP: SSH GUI install"; fi
if run_main --uninstall --profile home --select-repos --gui >/dev/null 2>&1 && grep -Fqx 'uninstall:home:HomeLAB-DNS' "$TMP/actions"; then ok "HELP: SSH GUI uninstall"; else not_ok "HELP: SSH GUI uninstall"; fi
if run_main --uninstall --profile home --all-repos >/dev/null 2>&1 && [[ $(grep -c '^uninstall:home:' "$TMP/actions" || true) -eq 2 ]]; then ok "HELP: uninstall all"; else not_ok "HELP: uninstall all"; fi
if run_main --uninstall --profile home --all-repos --purge >/dev/null 2>&1 && grep -Fqx purge "$TMP/actions"; then ok "HELP: uninstall all + purge"; else not_ok "HELP: uninstall all + purge"; fi
if run_main --profile home --select-repos --zenity >/dev/null 2>&1 && grep -Fqx 'install:home:HomeLAB-DNS' "$TMP/actions"; then ok "HELP: forced Zenity selection"; else not_ok "HELP: forced Zenity selection"; fi

if run_main --install --profile home --repo HomeLAB-DNS >/dev/null 2>&1 && grep -Fqx 'install:home:HomeLAB-DNS' "$TMP/actions"; then ok "single repo install"; else not_ok "single repo install"; fi
if run_main --install --profile home --repos HomeLAB-DNS,Algen-server-web-explorer-panel >/dev/null 2>&1 && [[ $(grep -c '^install:home:' "$TMP/actions" || true) -eq 2 ]]; then ok "multiple repo install"; else not_ok "multiple repo install"; fi

expect_success "list profiles" env LIB="$LIB" TEST_TMP="$TMP" bash -c '
    source "$LIB"
    init_invoking_user() { INVOKING_USER=tester; INVOKING_HOME="$TEST_TMP/home"; GITCONFIG_PATH="$TEST_TMP/home/.gitconfig"; }
    main --list-profiles | grep -Fx home >/dev/null
'


expect_success "legacy service is discovered for named profile" env LIB="$LIB" TEST_TMP="$TMP" bash -c '
    source "$LIB"
    ACTIVE_PROFILE=home
    GITHUB_OWNER=chmajster
    hostname() { echo kynlab01; }
    systemctl() {
        if [[ "$1" == list-units ]]; then
  printf "%s\n" "actions.runner.chmajster-HomeLAB-DNS.kynlab01-homelab-dns.service loaded active running"
        fi
        return 0
    }
    [[ $(get_service_repositories) == homelab-dns ]]
'

expect_success "orphan legacy service is removed without runner directory" env LIB="$LIB" TEST_TMP="$TMP" bash -c '
    source "$LIB"
    ACTIVE_PROFILE=home
    GITHUB_OWNER=chmajster
    RUNNER_BASE="$TEST_TMP/runners"
    hostname() { echo kynlab01; }
    list_action_runner_service_units() {
        [[ -f "$TEST_TMP/service-removed" ]] || echo actions.runner.chmajster-HomeLAB-DNS.kynlab01-homelab-dns.service
    }
    remove_runner_service_unit() {
        echo "$1" > "$TEST_TMP/service-removed"
        return 0
    }
    uninstall_repo_runner HomeLAB-DNS
    grep -Fqx actions.runner.chmajster-HomeLAB-DNS.kynlab01-homelab-dns.service "$TEST_TMP/service-removed"
'

expect_success "svc uninstall failure falls back to forced service cleanup" env LIB="$LIB" TEST_TMP="$TMP" bash -c '
    source "$LIB"
    ACTIVE_PROFILE=home
    GITHUB_OWNER=chmajster
    RUNNER_BASE="$TEST_TMP/runners-fallback"
    dir="$RUNNER_BASE/profiles/home/homelab-dns"
    mkdir -p "$dir"
    printf "%s\n" actions.runner.chmajster-HomeLAB-DNS.kynlab01-home-homelab-dns.service > "$dir/.service"
    printf "#!/usr/bin/env bash\nexit 1\n" > "$dir/svc.sh"
    chmod +x "$dir/svc.sh"
    list_action_runner_service_units() { return 0; }
    remove_runner_service_unit() { echo "$1" > "$TEST_TMP/forced-service"; return 0; }
    uninstall_repo_runner HomeLAB-DNS
    [[ ! -d "$dir" ]]
    grep -Fqx actions.runner.chmajster-HomeLAB-DNS.kynlab01-home-homelab-dns.service "$TEST_TMP/forced-service"
'

expect_success "purge keeps runner user when services still exist" env LIB="$LIB" TEST_TMP="$TMP" bash -c '
    source "$LIB"
    PURGE=true
    RUNNER_BASE="$TEST_TMP/purge-root"
    RUNNER_USER=github-runner
    mkdir -p "$RUNNER_BASE"
    list_action_runner_service_units() { echo actions.runner.chmajster-Repo.host-repo.service; }
    id() { echo "id should not be called" > "$TEST_TMP/id-called"; return 0; }
    purge_if_empty
    [[ -d "$RUNNER_BASE" && ! -e "$TEST_TMP/id-called" ]]
'


expect_success "uninstall order is stop then service removal then files" env LIB="$LIB" TEST_TMP="$TMP" bash -c '
    source "$LIB"
    ACTIVE_PROFILE=home
    GITHUB_OWNER=chmajster
    RUNNER_BASE="$TEST_TMP/order-runners"
    dir="$RUNNER_BASE/profiles/home/homelab-dns"
    mkdir -p "$dir"
    printf "%s\n" actions.runner.chmajster-HomeLAB-DNS.kynlab01-home-homelab-dns.service > "$dir/.service"
    set -- "\$1"
    cat > "$dir/svc.sh" <<EOF
#!/usr/bin/env bash
echo "svc-$1" >> "$TEST_TMP/order.log"
exit 0
EOF
    chmod +x "$dir/svc.sh"
    list_action_runner_service_units() {
        [[ -f "$TEST_TMP/unit-removed" ]] || echo actions.runner.chmajster-HomeLAB-DNS.kynlab01-home-homelab-dns.service
    }
    hostname() { echo kynlab01; }
    stop_runner_service_unit() { echo systemd-stop >> "$TEST_TMP/order.log"; return 0; }
    remove_runner_service_unit() { echo systemd-remove >> "$TEST_TMP/order.log"; touch "$TEST_TMP/unit-removed"; return 0; }
    rm() {
        if [[ "$*" == *"$dir"* ]]; then echo files-remove >> "$TEST_TMP/order.log"; fi
        command rm "$@"
    }
    : > "$TEST_TMP/order.log"
    uninstall_repo_runner HomeLAB-DNS
    mapfile -t order < "$TEST_TMP/order.log"
    [[ "${order[0]}" == svc-stop ]]
    [[ "${order[1]}" == systemd-stop ]]
    [[ "${order[2]}" == svc-uninstall ]]
    [[ "${order[3]}" == systemd-remove ]]
    [[ "${order[4]}" == files-remove ]]
'

expect_success "service cleanup failure keeps runner files" env LIB="$LIB" TEST_TMP="$TMP" bash -c '
    source "$LIB"
    ACTIVE_PROFILE=home
    GITHUB_OWNER=chmajster
    RUNNER_BASE="$TEST_TMP/keep-runners"
    dir="$RUNNER_BASE/profiles/home/homelab-dns"
    mkdir -p "$dir"
    printf "%s\n" actions.runner.chmajster-HomeLAB-DNS.kynlab01-home-homelab-dns.service > "$dir/.service"
    list_action_runner_service_units() { echo actions.runner.chmajster-HomeLAB-DNS.kynlab01-home-homelab-dns.service; }
    hostname() { echo kynlab01; }
    stop_runner_service_unit() { return 0; }
    remove_runner_service_unit() { return 0; }
    if uninstall_repo_runner HomeLAB-DNS; then exit 1; fi
    [[ -d "$dir" ]]
'

printf '\nRESULT: pass=%d fail=%d\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
