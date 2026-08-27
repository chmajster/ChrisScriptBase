from pathlib import Path
import re

path = Path("linux/install-github-selfhosted-runners.sh")
text = path.read_text()


def replace_once(old: str, new: str, label: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly 1 match, got {count}")
    text = text.replace(old, new, 1)


def replace_function(name: str, body: str) -> None:
    global text
    pattern = rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}\n"
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise SystemExit(f"{name}: expected exactly 1 function, got {len(matches)}")
    text = text[:matches[0].start()] + body.rstrip() + "\n" + text[matches[0].end():]


replace_once(
    'RUNNER_BASE="${RUNNER_BASE:-/opt/github-runners}"\nDEFAULT_LABELS="${CUSTOM_LABELS:-homelab}"',
    'RUNNER_BASE="${RUNNER_BASE:-/opt/github-runners}"\nRUNNER_SYSTEMD_DIR="${RUNNER_SYSTEMD_DIR:-/etc/systemd/system}"\nDEFAULT_LABELS="${CUSTOM_LABELS:-homelab}"',
    "RUNNER_SYSTEMD_DIR",
)

replace_once(
    '  --uninstall               Wyrejestrowuje i usuwa runnery.\n',
    '  --uninstall               Wyrejestrowuje runnery i usuwa ich usługi systemd.\n',
    "help --uninstall",
)

marker = "\ninstall_repo_runner() {\n"
helpers = r'''
list_runner_service_units() {
    local unit_path unit
    [[ -d "$RUNNER_SYSTEMD_DIR" ]] || return 0

    for unit_path in "$RUNNER_SYSTEMD_DIR"/actions.runner.*.service; do
        [[ -e "$unit_path" || -L "$unit_path" ]] || continue
        unit="${unit_path##*/}"
        [[ "$unit" == actions.runner.*.service ]] || continue
        printf '%s\n' "$unit"
    done
}

remove_runner_service_unit() {
    local unit="$1"
    log "Usuwanie usługi systemd: $unit"
    systemctl stop "$unit" >/dev/null 2>&1 || true
    systemctl disable "$unit" >/dev/null 2>&1 || true
    rm -f "${RUNNER_SYSTEMD_DIR}/${unit}"
    if [[ -d "$RUNNER_SYSTEMD_DIR" ]]; then
        find "$RUNNER_SYSTEMD_DIR" -type l -name "$unit" -delete 2>/dev/null || true
    fi
    systemctl reset-failed "$unit" >/dev/null 2>&1 || true
}

cleanup_repo_runner_services() {
    local repo="$1" unit lower prefix removed=0
    prefix="actions.runner.${GITHUB_OWNER}-${repo}."
    while IFS= read -r unit; do
        [[ -n "$unit" ]] || continue
        lower="${unit,,}"
        if [[ "$lower" == "${prefix,,}"*.service ]]; then
            remove_runner_service_unit "$unit"
            ((removed += 1))
        fi
    done < <(list_runner_service_units)

    if (( removed > 0 )); then
        systemctl daemon-reload >/dev/null 2>&1 || true
        echo "Usunięto usługi systemd dla ${GITHUB_OWNER}/${repo}: $removed"
        return 0
    fi
    return 1
}

cleanup_owner_repo_runner_services() {
    local unit lower prefix removed=0
    prefix="actions.runner.${GITHUB_OWNER}-"
    while IFS= read -r unit; do
        [[ -n "$unit" ]] || continue
        lower="${unit,,}"
        if [[ "$lower" == "${prefix,,}"*.service ]]; then
            remove_runner_service_unit "$unit"
            ((removed += 1))
        fi
    done < <(list_runner_service_units)

    if (( removed > 0 )); then
        systemctl daemon-reload >/dev/null 2>&1 || true
        echo "Usunięto wszystkie pozostałe repo-level usługi systemd ownera '$GITHUB_OWNER': $removed"
        return 0
    fi
    return 1
}

cleanup_org_runner_services() {
    local unit lower prefix removed=0
    prefix="actions.runner.${GITHUB_OWNER}."
    while IFS= read -r unit; do
        [[ -n "$unit" ]] || continue
        lower="${unit,,}"
        if [[ "$lower" == "${prefix,,}"*.service ]]; then
            remove_runner_service_unit "$unit"
            ((removed += 1))
        fi
    done < <(list_runner_service_units)

    if (( removed > 0 )); then
        systemctl daemon-reload >/dev/null 2>&1 || true
        echo "Usunięto pozostałe organization-level usługi systemd '$GITHUB_OWNER': $removed"
        return 0
    fi
    return 1
}

'''
if text.count(marker) != 1:
    raise SystemExit(f"helper insertion marker count={text.count(marker)}")
text = text.replace(marker, "\n" + helpers + "install_repo_runner() {\n", 1)

replace_function("uninstall_repo_runner", r'''uninstall_repo_runner() {
    local repo="$1" dir token="" remote_ok=true
    dir="$(repo_runner_dir "$repo")"
    log "[$ACTIVE_PROFILE] Usuwanie: ${GITHUB_OWNER}/${repo}"

    if [[ ! -d "$dir" ]]; then
        if cleanup_repo_runner_services "$repo"; then
            warn "Brak katalogu $dir; usunięto osierocone usługi systemd. Runner może nadal wymagać usunięcia z GitHub."
            return 0
        fi
        echo "Runner lokalny nie istnieje: $dir"
        return 3
    fi

    if [[ -x "$dir/svc.sh" ]]; then
        (
            cd "$dir"
            ./svc.sh stop || true
            ./svc.sh uninstall || true
        )
    fi

    if [[ -f "$dir/.runner" && -x "$dir/config.sh" ]]; then
        if token="$(get_repo_remove_token "$repo")" && [[ -n "$token" && "$token" != null ]]; then
            sudo -u "$RUNNER_USER" bash -c "cd '$dir' && ./config.sh remove --unattended --token '$token'" || remote_ok=false
        else
            warn "Brak remove token; usuwam tylko lokalnie."
            remote_ok=false
        fi
    fi

    cleanup_repo_runner_services "$repo" || true
    unset token
    rm -rf "$dir"
    [[ "$remote_ok" == true ]]
}''')

replace_function("uninstall_org_runner", r'''uninstall_org_runner() {
    local dir token="" remote_ok=true
    dir="$(org_runner_dir)"

    if [[ ! -d "$dir" ]]; then
        if cleanup_org_runner_services; then
            warn "Brak katalogu $dir; usunięto osierocone organization-level usługi systemd."
            return 0
        fi
        echo "Runner lokalny nie istnieje: $dir"
        return 3
    fi

    if [[ -x "$dir/svc.sh" ]]; then
        (
            cd "$dir"
            ./svc.sh stop || true
            ./svc.sh uninstall || true
        )
    fi

    if [[ -f "$dir/.runner" && -x "$dir/config.sh" ]]; then
        if token="$(get_org_remove_token)" && [[ -n "$token" && "$token" != null ]]; then
            sudo -u "$RUNNER_USER" bash -c "cd '$dir' && ./config.sh remove --unattended --token '$token'" || remote_ok=false
        else
            remote_ok=false
        fi
    fi

    cleanup_org_runner_services || true
    unset token
    rm -rf "$dir"
    [[ "$remote_ok" == true ]]
}''')

replace_function("process_user_profile", r'''process_user_profile() {
    local version="${1:-}" arch="${2:-}" repo rc
    local -a repositories=()
    local success=0 failed=0 skipped=0

    if [[ "$LIST_REPOS" == true ]]; then
        resolve_repositories
        return 0
    fi

    mapfile -t repositories < <(resolve_repositories)
    if [[ ${#repositories[@]} -eq 0 ]]; then
        if [[ "$ACTION" == uninstall && "$REPO_SELECTION_MODE" == all ]] &&
           cleanup_owner_repo_runner_services; then
            warn "Profil '$ACTIVE_PROFILE': nie znaleziono katalogów runnerów, ale usunięto osierocone usługi systemd."
            return 0
        fi
        warn "Profil '$ACTIVE_PROFILE': nic nie wybrano."
        return 0
    fi

    echo "Wybrane repozytoria [$ACTIVE_PROFILE]: ${#repositories[@]}"
    printf ' - %s\n' "${repositories[@]}"

    for repo in "${repositories[@]}"; do
        if [[ "$ACTION" == install ]]; then
            if install_repo_runner "$repo" "$version" "$arch"; then
                ((success += 1))
            else
                rc=$?
                if [[ "$rc" -eq 3 ]]; then
                    ((skipped += 1))
                else
                    ((failed += 1))
                fi
            fi
        else
            if uninstall_repo_runner "$repo"; then
                ((success += 1))
            else
                rc=$?
                if [[ "$rc" -eq 3 ]]; then
                    ((skipped += 1))
                else
                    ((failed += 1))
                fi
            fi
        fi
    done

    if [[ "$ACTION" == uninstall && "$REPO_SELECTION_MODE" == all ]]; then
        cleanup_owner_repo_runner_services || true
    fi

    log "Podsumowanie profilu: $ACTIVE_PROFILE"
    echo "Akcja: $ACTION | Sukces: $success | Pominięte: $skipped | Błędy: $failed"
    [[ "$failed" -eq 0 ]]
}''')

required = [
    'RUNNER_SYSTEMD_DIR="${RUNNER_SYSTEMD_DIR:-/etc/systemd/system}"',
    'if cleanup_repo_runner_services "$repo"; then',
    'cleanup_owner_repo_runner_services',
    'if cleanup_org_runner_services; then',
]
for needle in required:
    if needle not in text:
        raise SystemExit(f"missing expected patched text: {needle}")

path.write_text(text)
