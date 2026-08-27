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

ok() { echo "PASS: $*"; ((pass+=1)); }
not_ok() { echo "FAIL: $*" >&2; ((fail+=1)); }

expect_success() {
  local name="$1"; shift
  if "$@" >"$TMP/out" 2>"$TMP/err"; then
    ok "$name"
  else
    not_ok "$name"
    echo "--- stdout ---" >&2; cat "$TMP/out" >&2 || true
    echo "--- stderr ---" >&2; cat "$TMP/err" >&2 || true
  fi
}

expect_failure() {
  local name="$1"; shift
  if "$@" >"$TMP/out" 2>"$TMP/err"; then
    not_ok "$name (unexpected success)"
    cat "$TMP/out" >&2 || true
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

# Static syntax and help.
expect_success "bash -n" bash -n "$SCRIPT"
expect_success "--help" bash "$SCRIPT" --help
expect_success "-h" bash "$SCRIPT" -h

# Parser conflicts and validation documented in HELP.
expect_failure "reject --all-repos + --select-repos" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --all-repos --select-repos'
expect_failure "reject --all-repos + --repo" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --all-repos --repo Repo'
expect_failure "reject --select-repos + --repo" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --select-repos --repo Repo'
expect_failure "reject --gui without --select-repos" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --gui'
expect_failure "reject -GUI without --select-repos" env LIB="$LIB" bash -c 'source "$LIB"; parse_args -GUI'
expect_failure "reject --tui without --select-repos" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --tui'
expect_failure "reject --purge without --uninstall" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --purge'
expect_success "accept --uninstall --purge" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --uninstall --purge'
expect_success "accept repeated --repo" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --repo A --repo B; [[ ${#SELECTED_REPOS[@]} -eq 2 ]]'
expect_success "accept --repos CSV" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --repos A,B; [[ ${#SELECTED_REPOS[@]} -eq 2 ]]'
expect_success "accept --profiles CSV" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --profiles home,work; [[ ${#SELECTED_PROFILES[@]} -eq 2 ]]'
expect_success "accept -p alias" env LIB="$LIB" bash -c 'source "$LIB"; parse_args -p home; [[ ${SELECTED_PROFILES[0]} == home ]]'
expect_success "accept -r alias" env LIB="$LIB" bash -c 'source "$LIB"; parse_args -r Repo; [[ ${SELECTED_REPOS[0]} == Repo ]]'
expect_success "accept --select-repos --gui" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --select-repos --gui; [[ $SELECTION_UI == gui && $REPO_SELECTION_MODE == interactive ]]'
expect_success "accept --select-repos -GUI" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --select-repos -GUI; [[ $SELECTION_UI == gui && $REPO_SELECTION_MODE == interactive ]]'
expect_success "accept --select-repos --tui" env LIB="$LIB" bash -c 'source "$LIB"; parse_args --select-repos --tui; [[ $SELECTION_UI == tui && $REPO_SELECTION_MODE == interactive ]]'

# Regression for no-GUI + set -e: auto mode must not terminate.
expect_success "auto GUI dependency check without DISPLAY returns 0" env LIB="$LIB" bash -c '
  source "$LIB"
  REPO_SELECTION_MODE=interactive; SELECTION_UI=auto
  INVOKING_USER="$(id -un)"; INVOKING_HOME="$HOME"
  GUI_DISPLAY=""; GUI_WAYLAND_DISPLAY=""
  detect_graphical_session() { GUI_DISPLAY=""; GUI_WAYLAND_DISPLAY=""; return 0; }
  ensure_gui_dependencies
'
expect_failure "forced GUI without DISPLAY fails clearly" env LIB="$LIB" bash -c '
  source "$LIB"
  REPO_SELECTION_MODE=interactive; SELECTION_UI=gui
  INVOKING_USER="$(id -un)"; INVOKING_HOME="$HOME"
  GUI_DISPLAY=""; GUI_WAYLAND_DISPLAY=""
  detect_graphical_session() { GUI_DISPLAY=""; GUI_WAYLAND_DISPLAY=""; return 0; }
  ensure_gui_dependencies
'
expect_success "purge_if_empty disabled returns 0" env LIB="$LIB" bash -c 'source "$LIB"; PURGE=false; purge_if_empty'

# GUI selector logic with Zenity mocked.
mkdir -p "$TMP/bin"
printf '#!/usr/bin/env bash\nexit 0\n' > "$TMP/bin/zenity"
chmod +x "$TMP/bin/zenity"
expect_success "GUI selector returns selected repository" env LIB="$LIB" PATH="$TMP/bin:$PATH" bash -c '
  source "$LIB"
  ACTION=install; GUI_DISPLAY=:99; GUI_WAYLAND_DISPLAY=""
  run_zenity() {
    if [[ " $* " == *" --list "* ]]; then echo HomeLAB-DNS; return 0; fi
    if [[ " $* " == *" --question "* ]]; then return 0; fi
    return 1
  }
  [[ $(gui_select_repositories home HomeLAB-DNS OtherRepo) == HomeLAB-DNS ]]
'

run_main_case() {
  local name="$1" expected="$2"; shift 2
  : > "$TMP/actions"; : > "$TMP/sideeffects"
  local args_q="" arg
  for arg in "$@"; do printf -v args_q '%s %q' "$args_q" "$arg"; done
  if CASE_CODE="main$args_q" LIB="$LIB" TEST_TMP="$TMP" bash -c '
    source "$LIB"
    init_invoking_user() { INVOKING_USER=tester; INVOKING_HOME="$TEST_TMP/home"; GITCONFIG_PATH="$TEST_TMP/home/.gitconfig"; }
    require_root() { return 0; }
    ensure_dependencies() { return 0; }
    ensure_gui_dependencies() { return 0; }
    validate_github_token() { return 0; }
    create_runner_user() { echo create_runner_user >> "$TEST_TMP/sideeffects"; return 0; }
    get_runner_version() { echo get_runner_version >> "$TEST_TMP/sideeffects"; echo 9.9.9; }
    detect_arch() { echo x64; }
    get_user_repositories() { printf "%s\n" HomeLAB-DNS Algen-server-web-explorer-panel; }
    interactive_select_repositories() { echo HomeLAB-DNS; }
    install_repo_runner() { echo "install:$ACTIVE_PROFILE:$GITHUB_OWNER:$1" >> "$TEST_TMP/actions"; return 0; }
    uninstall_repo_runner() { echo "uninstall:$ACTIVE_PROFILE:$GITHUB_OWNER:$1" >> "$TEST_TMP/actions"; return 0; }
    purge_if_empty() { echo purge >> "$TEST_TMP/actions"; return 0; }
    systemctl() { return 0; }
    eval "$CASE_CODE"
  ' > "$TMP/main.out" 2> "$TMP/main.err"; then
    if grep -Fqx "$expected" "$TMP/actions" 2>/dev/null || [[ "$expected" == "NO_ACTION" && ! -s "$TMP/actions" ]]; then
      ok "$name"
    else
      not_ok "$name (expected action: $expected)"
      cat "$TMP/actions" >&2 || true
    fi
  else
    not_ok "$name"
    cat "$TMP/main.out" >&2 || true; cat "$TMP/main.err" >&2 || true
  fi
}

# HELP examples executed through main with destructive/network functions mocked.
run_main_case "HELP: GUI install" "install:home:chmajster:HomeLAB-DNS" --install --profile home --select-repos --gui
run_main_case "HELP: GUI uninstall" "uninstall:home:chmajster:HomeLAB-DNS" --uninstall --profile home --select-repos --gui
run_main_case "HELP: auto GUI/TUI" "install:home:chmajster:HomeLAB-DNS" --profile home --select-repos
run_main_case "HELP: TUI selection" "install:home:chmajster:HomeLAB-DNS" --profile home --select-repos --tui
run_main_case "HELP: single repo install" "install:home:chmajster:HomeLAB-DNS" --install --profile home --repo HomeLAB-DNS

: > "$TMP/actions"; : > "$TMP/sideeffects"
if CASE_CODE='main --install --profile home --repos HomeLAB-DNS,Algen-server-web-explorer-panel' LIB="$LIB" TEST_TMP="$TMP" bash -c '
  source "$LIB"
  init_invoking_user(){ INVOKING_USER=tester; INVOKING_HOME="$TEST_TMP/home"; GITCONFIG_PATH="$TEST_TMP/home/.gitconfig"; }
  require_root(){ return 0; }; ensure_dependencies(){ return 0; }; ensure_gui_dependencies(){ return 0; }; validate_github_token(){ return 0; }
  create_runner_user(){ return 0; }; get_runner_version(){ echo 9.9.9; }; detect_arch(){ echo x64; }
  get_user_repositories(){ printf "%s\n" HomeLAB-DNS Algen-server-web-explorer-panel; }
  install_repo_runner(){ echo "install:$1" >> "$TEST_TMP/actions"; return 0; }
  systemctl(){ return 0; }
  eval "$CASE_CODE"
' >/dev/null 2>&1; then :; fi
if grep -Fqx 'install:HomeLAB-DNS' "$TMP/actions" && grep -Fqx 'install:Algen-server-web-explorer-panel' "$TMP/actions"; then ok "HELP: multiple repo install"; else not_ok "HELP: multiple repo install"; cat "$TMP/actions" >&2 || true; fi

run_main_case "HELP: uninstall one repo" "uninstall:home:chmajster:HomeLAB-DNS" --uninstall --profile home --repo HomeLAB-DNS

: > "$TMP/actions"; : > "$TMP/sideeffects"
if CASE_CODE='main --uninstall --profile home --all-repos' LIB="$LIB" TEST_TMP="$TMP" bash -c '
  source "$LIB"
  init_invoking_user(){ INVOKING_USER=tester; INVOKING_HOME="$TEST_TMP/home"; GITCONFIG_PATH="$TEST_TMP/home/.gitconfig"; }
  require_root(){ return 0; }; ensure_dependencies(){ return 0; }; ensure_gui_dependencies(){ return 0; }; validate_github_token(){ return 0; }
  get_user_repositories(){ printf "%s\n" HomeLAB-DNS Algen-server-web-explorer-panel; }
  uninstall_repo_runner(){ echo "uninstall:$1" >> "$TEST_TMP/actions"; return 0; }
  purge_if_empty(){ return 0; }; systemctl(){ return 0; }
  eval "$CASE_CODE"
' >/dev/null 2>&1; then :; fi
if [[ $(grep -c '^uninstall:' "$TMP/actions" || true) -eq 2 ]]; then ok "HELP: uninstall all repos"; else not_ok "HELP: uninstall all repos"; cat "$TMP/actions" >&2 || true; fi

: > "$TMP/actions"; : > "$TMP/sideeffects"
if CASE_CODE='main --uninstall --profile home --all-repos --purge' LIB="$LIB" TEST_TMP="$TMP" bash -c '
  source "$LIB"
  init_invoking_user(){ INVOKING_USER=tester; INVOKING_HOME="$TEST_TMP/home"; GITCONFIG_PATH="$TEST_TMP/home/.gitconfig"; }
  require_root(){ return 0; }; ensure_dependencies(){ return 0; }; ensure_gui_dependencies(){ return 0; }; validate_github_token(){ return 0; }
  get_user_repositories(){ printf "%s\n" HomeLAB-DNS Algen-server-web-explorer-panel; }
  uninstall_repo_runner(){ echo "uninstall:$1" >> "$TEST_TMP/actions"; return 0; }
  purge_if_empty(){ echo purge >> "$TEST_TMP/actions"; return 0; }; systemctl(){ return 0; }
  eval "$CASE_CODE"
' >/dev/null 2>&1; then :; fi
if grep -Fqx purge "$TMP/actions"; then ok "HELP: full purge"; else not_ok "HELP: full purge"; cat "$TMP/actions" >&2 || true; fi

run_main_case "HELP: default profile single repo" "install:default:chmajster:HomeLAB-DNS" --install --repo HomeLAB-DNS

# list-profiles and list-repos.
expect_success "HELP: list profiles" env LIB="$LIB" TEST_TMP="$TMP" bash -c '
  source "$LIB"
  init_invoking_user(){ INVOKING_USER=tester; INVOKING_HOME="$TEST_TMP/home"; GITCONFIG_PATH="$TEST_TMP/home/.gitconfig"; }
  main --list-profiles | grep -Fx home >/dev/null
'

: > "$TMP/sideeffects"
if CASE_CODE='main --profile home --list-repos' LIB="$LIB" TEST_TMP="$TMP" bash -c '
  source "$LIB"
  init_invoking_user(){ INVOKING_USER=tester; INVOKING_HOME="$TEST_TMP/home"; GITCONFIG_PATH="$TEST_TMP/home/.gitconfig"; }
  require_root(){ return 0; }; ensure_dependencies(){ return 0; }; ensure_gui_dependencies(){ return 0; }; validate_github_token(){ return 0; }
  create_runner_user(){ echo create_runner_user >> "$TEST_TMP/sideeffects"; return 0; }
  get_runner_version(){ echo get_runner_version >> "$TEST_TMP/sideeffects"; echo 9.9.9; }
  detect_arch(){ echo x64; }
  get_user_repositories(){ printf "%s\n" HomeLAB-DNS Algen-server-web-explorer-panel; }
  systemctl(){ return 0; }
  eval "$CASE_CODE"
' > "$TMP/listrepos.out" 2> "$TMP/listrepos.err"; then
  if grep -Fx HomeLAB-DNS "$TMP/listrepos.out" >/dev/null; then ok "HELP: list repos returns repositories"; else not_ok "HELP: list repos returns repositories"; fi
else
  not_ok "HELP: list repos exits successfully"
  cat "$TMP/listrepos.err" >&2 || true
fi

if [[ ! -s "$TMP/sideeffects" ]]; then
  ok "list-repos has no install side effects"
else
  not_ok "list-repos has no install side effects"
  cat "$TMP/sideeffects" >&2
fi

# Profile-scoped explicit selector: work is org mode and must not receive the user repo action.
: > "$TMP/actions"
if CASE_CODE='main --profiles home,work --repo home:HomeLAB-DNS' LIB="$LIB" TEST_TMP="$TMP" bash -c '
  source "$LIB"
  init_invoking_user(){ INVOKING_USER=tester; INVOKING_HOME="$TEST_TMP/home"; GITCONFIG_PATH="$TEST_TMP/home/.gitconfig"; }
  require_root(){ return 0; }; ensure_dependencies(){ return 0; }; ensure_gui_dependencies(){ return 0; }; validate_github_token(){ return 0; }
  create_runner_user(){ return 0; }; get_runner_version(){ echo 9.9.9; }; detect_arch(){ echo x64; }
  get_user_repositories(){ printf "%s\n" HomeLAB-DNS; }
  install_repo_runner(){ echo "$ACTIVE_PROFILE:$1" >> "$TEST_TMP/actions"; return 0; }
  install_org_runner(){ echo "org:$ACTIVE_PROFILE" >> "$TEST_TMP/actions"; return 0; }
  systemctl(){ return 0; }
  eval "$CASE_CODE"
' >/dev/null 2>&1; then :; fi
if grep -Fqx 'home:HomeLAB-DNS' "$TMP/actions" && grep -Fqx 'org:work' "$TMP/actions"; then
  ok "profile-scoped repo leaves org profile at organization runner semantics"
else
  not_ok "profile-scoped repo leaves org profile at organization runner semantics"
  cat "$TMP/actions" >&2 || true
fi

echo
echo "RESULT: pass=$pass fail=$fail"
[[ "$fail" -eq 0 ]]
