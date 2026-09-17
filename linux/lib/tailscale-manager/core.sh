# shellcheck shell=bash

ensure_tailscale_installed() {
    if command_exists tailscale; then return 0; fi
    if [[ "$DRY_RUN" == true && "$DO_INSTALL" == true ]]; then return 0; fi
    die "$EXIT_DEPENDENCY" "Tailscale nie jest zainstalowany. Użyj --install."
}

ensure_tailscaled() {
    if [[ "$DRY_RUN" == true && "$DO_INSTALL" == true ]]; then
        printf 'DRY RUN: systemctl enable --now tailscaled\n'
        return 0
    fi
    if command_exists systemctl; then
        if ! systemctl is-active --quiet tailscaled; then
            if [[ "$DRY_RUN" == true ]]; then
                run_cmd systemctl start tailscaled
            else
                systemctl start tailscaled >/dev/null 2>&1 || die "$EXIT_DAEMON" "Nie udało się uruchomić tailscaled."
            fi
        fi
    fi
}

install_tailscale() {
    require_root
    detect_os
    if command_exists tailscale; then
        log_info "Tailscale jest już zainstalowany: $(tailscale version 2>/dev/null | head -n1 || true)"
        ensure_tailscaled
        return 0
    fi
    [[ "$OS_FAMILY" != "unknown" ]] || die "$EXIT_UNSUPPORTED_OS" "Nieobsługiwana dystrybucja: $OS_ID"

    if [[ "$OS_FAMILY" == "arch" ]]; then
        [[ "$PKG_MANAGER" == "pacman" ]] || die "$EXIT_DEPENDENCY" "Arch Linux wymaga pacman."
        run_cmd pacman -Syu --noconfirm tailscale
        if [[ "$DRY_RUN" != true && -n "$(command -v systemctl 2>/dev/null || true)" ]]; then
            systemctl enable --now tailscaled >/dev/null 2>&1 || die "$EXIT_DAEMON" "Nie udało się włączyć tailscaled."
        fi
        log_success "Tailscale zainstalowany."
        return 0
    fi

    command_exists curl || die "$EXIT_DEPENDENCY" "Brak curl. Zainstaluj curl i ponów."

    if [[ "$DRY_RUN" == true ]]; then
        printf 'DRY RUN: pobranie https://tailscale.com/install.sh, weryfikacja odpowiedzi i uruchomienie instalatora\n'
        printf 'DRY RUN: systemctl enable --now tailscaled\n'
        return 0
    fi

    local installer
    installer="$(mktemp)"
    chmod 600 "$installer"
    log_info "Pobieram oficjalny instalator Tailscale dla $OS_ID $OS_VERSION_ID."
    if ! curl --proto '=https' --tlsv1.2 -fsSL https://tailscale.com/install.sh -o "$installer"; then
        rm -f "$installer"
        die "$EXIT_INSTALL" "Pobranie instalatora Tailscale nie powiodło się."
    fi
    grep -q 'tailscale' "$installer" || { rm -f "$installer"; die "$EXIT_INSTALL" "Pobrany plik nie wygląda jak instalator Tailscale."; }
    sh "$installer" || { rm -f "$installer"; die "$EXIT_INSTALL" "Instalacja Tailscale nie powiodła się."; }
    rm -f "$installer"
    command_exists tailscale || die "$EXIT_INSTALL" "Instalator zakończył pracę, ale polecenie tailscale nie istnieje."
    if command_exists systemctl; then
        systemctl enable --now tailscaled >/dev/null 2>&1 || die "$EXIT_DAEMON" "Nie udało się włączyć tailscaled."
    fi
    log_success "Tailscale zainstalowany."
}

package_update_tailscale() {
    detect_os
    case "$PKG_MANAGER" in
        apt-get) run_cmd apt-get update; run_cmd apt-get install -y --only-upgrade tailscale ;;
        dnf) run_cmd dnf upgrade -y tailscale ;;
        yum) run_cmd yum update -y tailscale ;;
        zypper) run_cmd zypper --non-interactive update tailscale ;;
        pacman) run_cmd pacman -Syu --noconfirm tailscale ;;
        *) die "$EXIT_INSTALL" "Brak obsługi aktualizacji dla package managera." ;;
    esac
}

update_tailscale() {
    require_root
    ensure_tailscale_installed
    local before after
    before="$(tailscale version 2>/dev/null | head -n1 || true)"
    log_info "Aktualna wersja: ${before:-unknown}"
    if tailscale update --help >/dev/null 2>&1; then
        if [[ "$DRY_RUN" == true ]]; then
            run_cmd tailscale update --dry-run
        else
            tailscale update --yes || package_update_tailscale
        fi
    else
        package_update_tailscale
    fi
    after="$(tailscale version 2>/dev/null | head -n1 || true)"
    log_success "Wersja po aktualizacji: ${after:-unknown}"
}

uninstall_tailscale() {
    require_root
    [[ "$MODE" != "silent" || "$FORCE" == true ]] || die "$EXIT_ARGS" "--uninstall w trybie silent wymaga --force."
    detect_os
    if [[ "$DRY_RUN" == true ]]; then
        printf 'DRY RUN: zatrzymanie tailscaled i usunięcie pakietu tailscale\n'
        return 0
    fi
    command_exists tailscale || { log_info "Tailscale nie jest zainstalowany."; return 0; }
    tailscale down >/dev/null 2>&1 || true
    if command_exists systemctl; then systemctl disable --now tailscaled >/dev/null 2>&1 || true; fi
    case "$PKG_MANAGER" in
        apt-get) apt-get remove -y tailscale ;;
        dnf) dnf remove -y tailscale ;;
        yum) yum remove -y tailscale ;;
        zypper) zypper --non-interactive remove tailscale ;;
        pacman) pacman -Rns --noconfirm tailscale ;;
        *) die "$EXIT_INSTALL" "Brak obsługi uninstall dla package managera." ;;
    esac
    log_success "Tailscale usunięty."
}

enable_ip_forwarding() {
    require_root
    if [[ "$DRY_RUN" == true ]]; then
        printf 'DRY RUN: zapis net.ipv4.ip_forward=1 i net.ipv6.conf.all.forwarding=1 do %s\n' "$SYSCTL_FILE"
        return 0
    fi
    local target="$SYSCTL_FILE"
    [[ -d /etc/sysctl.d ]] || target="/etc/sysctl.conf"
    if [[ "$target" == "$SYSCTL_FILE" ]]; then
        cat > "$target" <<'EOF_SYSCTL'
# Managed by ChrisScriptBase tailscale-manager.sh
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
EOF_SYSCTL
    else
        if grep -q '^net.ipv4.ip_forward' "$target" 2>/dev/null; then
            sed -i 's/^net\.ipv4\.ip_forward.*/net.ipv4.ip_forward = 1/' "$target"
        else
            printf '\nnet.ipv4.ip_forward = 1\n' >> "$target"
        fi
        if grep -q '^net.ipv6.conf.all.forwarding' "$target" 2>/dev/null; then
            sed -i 's/^net\.ipv6\.conf\.all\.forwarding.*/net.ipv6.conf.all.forwarding = 1/' "$target"
        else
            printf 'net.ipv6.conf.all.forwarding = 1\n' >> "$target"
        fi
    fi
    sysctl -p "$target" >/dev/null || die "$EXIT_CONFIG" "Nie udało się zastosować IP forwarding."
    [[ "$(sysctl -n net.ipv4.ip_forward 2>/dev/null || echo 0)" == "1" ]] || die "$EXIT_CONFIG" "IPv4 forwarding nadal jest wyłączony."
    log_success "IP forwarding włączony."
}

backend_state() {
    ensure_tailscale_installed
    tailscale status --json 2>/dev/null | sed -n 's/.*"BackendState"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1
}

prepare_auth_arg() {
    AUTH_ARG=""
    if [[ -n "$AUTH_KEY_FILE" ]]; then
        AUTH_ARG="file:$AUTH_KEY_FILE"
        return 0
    fi
    if [[ -n "$AUTH_KEY" ]]; then
        local secret_dir="${XDG_RUNTIME_DIR:-/run}"
        if [[ ! -d "$secret_dir" || ! -w "$secret_dir" ]]; then
            secret_dir="${HOME:-/root}/.cache/chrisscriptbase"
            mkdir -p "$secret_dir"
            chmod 700 "$secret_dir"
        fi
        TEMP_AUTH_FILE="$(mktemp "$secret_dir/tailscale-auth.XXXXXX")"
        chmod 600 "$TEMP_AUTH_FILE"
        printf '%s' "$AUTH_KEY" > "$TEMP_AUTH_FILE"
        AUTH_ARG="file:$TEMP_AUTH_FILE"
    fi
}

connect_tailscale() {
    ensure_tailscale_installed
    ensure_tailscaled
    local state auth_arg
    state="$(backend_state || true)"
    prepare_auth_arg
    auth_arg="$AUTH_ARG"
    local -a args=(up)
    [[ -z "$auth_arg" ]] || args+=("--auth-key=$auth_arg")
    [[ "$FORCE_REAUTH" != true ]] || args+=(--force-reauth)
    [[ -z "$HOSTNAME_OVERRIDE" ]] || args+=("--hostname=$HOSTNAME_OVERRIDE")
    [[ -z "$ADVERTISE_TAGS" ]] || args+=("--advertise-tags=$ADVERTISE_TAGS")

    if [[ "$MODE" == "silent" && -z "$auth_arg" && "$state" =~ ^(NeedsLogin|NoState)$ ]]; then
        die "$EXIT_AUTH" "Urządzenie wymaga logowania; w silent użyj --auth-key-file lub --auth-key."
    fi

    if [[ "$DRY_RUN" == true ]]; then
        printf 'DRY RUN: tailscale up'
        local item
        for item in "${args[@]:1}"; do
            if [[ "$item" == --auth-key=* ]]; then printf ' --auth-key=********'; else printf ' %q' "$item"; fi
        done
        printf '\n'
    else
        if ! tailscale "${args[@]}"; then
            die "$EXIT_AUTH" "tailscale up nie powiodło się."
        fi
    fi
    log_success "Połączenie/autoryzacja Tailscale zakończona."
}

configure_tailscale() {
    ensure_tailscale_installed
    ensure_tailscaled
    if [[ -n "$ADVERTISE_ROUTES" || "$ADVERTISE_EXIT_NODE" == true ]]; then
        enable_ip_forwarding
    fi
    local -a args=(set)
    [[ -z "$HOSTNAME_OVERRIDE" ]] || args+=("--hostname=$HOSTNAME_OVERRIDE")
    [[ -z "$SSH" ]] || args+=("--ssh=$SSH")
    [[ -z "$ACCEPT_ROUTES" ]] || args+=("--accept-routes=$ACCEPT_ROUTES")
    [[ -z "$ACCEPT_DNS" ]] || args+=("--accept-dns=$ACCEPT_DNS")
    if [[ "$CLEAR_ADVERTISE_ROUTES" == true ]]; then args+=("--advertise-routes=")
    elif [[ -n "$ADVERTISE_ROUTES" ]]; then args+=("--advertise-routes=$ADVERTISE_ROUTES"); fi
    [[ -z "$ADVERTISE_EXIT_NODE" ]] || args+=("--advertise-exit-node=$ADVERTISE_EXIT_NODE")
    if [[ "$CLEAR_EXIT_NODE" == true ]]; then args+=("--exit-node=")
    elif [[ -n "$EXIT_NODE" ]]; then args+=("--exit-node=$EXIT_NODE"); fi
    [[ -z "$EXIT_NODE_ALLOW_LAN_ACCESS" ]] || args+=("--exit-node-allow-lan-access=$EXIT_NODE_ALLOW_LAN_ACCESS")
    [[ -z "$OPERATOR" ]] || args+=("--operator=$OPERATOR")
    [[ -z "$SHIELDS_UP" ]] || args+=("--shields-up=$SHIELDS_UP")
    [[ -z "$SNAT_SUBNET_ROUTES" ]] || args+=("--snat-subnet-routes=$SNAT_SUBNET_ROUTES")
    [[ -z "$STATEFUL_FILTERING" ]] || args+=("--stateful-filtering=$STATEFUL_FILTERING")
    [[ -z "$NETFILTER_MODE" ]] || args+=("--netfilter-mode=$NETFILTER_MODE")
    [[ -z "$AUTO_UPDATE" ]] || args+=("--auto-update=$AUTO_UPDATE")
    [[ -z "$WEBCLIENT" ]] || args+=("--webclient=$WEBCLIENT")

    if ((${#args[@]} == 1)); then
        log_debug "Brak ustawień do tailscale set."
        return 0
    fi
    run_cmd tailscale "${args[@]}" || die "$EXIT_CONFIG" "tailscale set nie powiodło się."
    log_success "Konfiguracja Tailscale zastosowana."
}

disconnect_tailscale(){ ensure_tailscale_installed; run_cmd tailscale down || die "$EXIT_CONFIG" "tailscale down nie powiodło się."; }
logout_tailscale(){ ensure_tailscale_installed; run_cmd tailscale logout || die "$EXIT_AUTH" "tailscale logout nie powiodło się."; }

service_action() {
    [[ -n "$SERVICE_ACTION" ]] || return 0
    command_exists systemctl || die "$EXIT_DEPENDENCY" "Brak systemctl."
    case "$SERVICE_ACTION" in
        start|stop|restart) require_root; run_cmd systemctl "$SERVICE_ACTION" tailscaled ;;
        enable) require_root; run_cmd systemctl enable --now tailscaled ;;
        disable) require_root; run_cmd systemctl disable --now tailscaled ;;
        status) systemctl status --no-pager tailscaled ;;
    esac
}

show_ip() {
    ensure_tailscale_installed
    tailscale ip || die "$EXIT_CONNECTIVITY" "Nie udało się pobrać adresu Tailscale."
}

show_status() {
    ensure_tailscale_installed
    if [[ "$JSON_OUTPUT" == true ]]; then
        tailscale status --json
        return
    fi
    printf 'Tailscale Status\n\n'
    if command_exists systemctl; then
        printf 'Service:        %s\n' "$(systemctl is-active tailscaled 2>/dev/null || echo unknown)"
    fi
    printf 'Version:        %s\n' "$(tailscale version 2>/dev/null | head -n1 || echo unknown)"
    printf 'Backend state:  %s\n' "$(backend_state 2>/dev/null || echo unknown)"
    printf 'IPv4:           %s\n' "$(tailscale ip -4 2>/dev/null || echo '-')"
    printf 'IPv6:           %s\n' "$(tailscale ip -6 2>/dev/null || echo '-')"
    printf '\nPeers:\n'
    tailscale status 2>/dev/null || true
    if tailscale get --help >/dev/null 2>&1; then
        printf '\nPreferences:\n'
        tailscale get --set-flags 2>/dev/null || true
    fi
}

run_diagnostics() {
    ensure_tailscale_installed
    printf 'Tailscale Diagnostics\n\n'
    local failures=0
    if command_exists systemctl && systemctl is-active --quiet tailscaled; then printf '[OK] tailscaled running\n'; else printf '[FAIL] tailscaled not running\n'; ((failures+=1)); fi
    local state ipv4 ipv6
    state="$(backend_state 2>/dev/null || true)"
    if [[ "$state" == "Running" ]]; then
        printf '[OK] Backend state: Running\n'
    else
        printf '[WARN] Backend state: %s\n' "${state:-unknown}"
        ((failures+=1))
    fi
    ipv4="$(tailscale ip -4 2>/dev/null || true)"; ipv6="$(tailscale ip -6 2>/dev/null || true)"
    if [[ -n "$ipv4" ]]; then printf '[OK] IPv4: %s\n' "$ipv4"; else printf '[WARN] Brak IPv4 Tailscale\n'; fi
    if [[ -n "$ipv6" ]]; then printf '[OK] IPv6: %s\n' "$ipv6"; else printf '[WARN] Brak IPv6 Tailscale\n'; fi
    printf '\nDNS:\n'; tailscale dns status 2>/dev/null || printf 'tailscale dns status niedostępne\n'
    printf '\nNetwork check:\n'; tailscale netcheck 2>/dev/null || { printf 'netcheck nie powiódł się\n'; ((failures+=1)); }
    printf '\nRoutes/preferences:\n'; tailscale get --set-flags 2>/dev/null || true
    printf '\nPeers:\n'; tailscale status 2>/dev/null || true
    if ((failures > 0)); then
        printf '\nOverall status: DEGRADED (%d problemów)\n' "$failures"
        return "$EXIT_CONNECTIVITY"
    fi
    printf '\nOverall status: HEALTHY\n'
}

ping_peer() {
    ensure_tailscale_installed
    [[ -n "$PING_TARGET" ]] || die "$EXIT_ARGS" "Brak celu ping."
    tailscale ping --c 3 "$PING_TARGET" || die "$EXIT_CONNECTIVITY" "Tailscale ping do $PING_TARGET nie powiódł się."
}
