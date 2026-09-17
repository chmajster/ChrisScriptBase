# shellcheck shell=bash

ensure_ui() {
    if command_exists dialog; then UI_BIN="dialog"; return 0; fi
    if command_exists whiptail; then UI_BIN="whiptail"; return 0; fi
    if [[ $EUID -eq 0 && -n "$PKG_MANAGER" ]]; then
        log_info "Brak dialog/whiptail; próbuję zainstalować dialog."
        case "$PKG_MANAGER" in
            apt-get) if apt-get update >/dev/null 2>&1; then apt-get install -y dialog >/dev/null 2>&1 || true; fi ;;
            dnf) dnf install -y dialog >/dev/null 2>&1 || true ;;
            yum) yum install -y dialog >/dev/null 2>&1 || true ;;
            zypper) zypper --non-interactive install dialog >/dev/null 2>&1 || true ;;
            pacman) pacman -S --noconfirm dialog >/dev/null 2>&1 || true ;;
        esac
    fi
    if command_exists dialog; then UI_BIN="dialog"; return 0; fi
    if command_exists whiptail; then UI_BIN="whiptail"; return 0; fi
    log_warn "Brak dialog/whiptail. Przechodzę do CLI."
    MODE="cli"
    return 1
}

ui_menu() {
    local title="$1" text="$2"; shift 2
    "$UI_BIN" --clear --title "$title" --menu "$text" 22 78 14 "$@" 3>&1 1>&2 2>&3
}

ui_input() {
    local title="$1" text="$2" default="${3:-}"
    "$UI_BIN" --title "$title" --inputbox "$text" 10 76 "$default" 3>&1 1>&2 2>&3
}

ui_password() {
    local title="$1" text="$2"
    "$UI_BIN" --title "$title" --passwordbox "$text" 10 76 3>&1 1>&2 2>&3
}

ui_yesno() {
    "$UI_BIN" --title "$1" --yesno "$2" 10 76
}

ui_msg() {
    "$UI_BIN" --title "$1" --msgbox "$2" 18 78
}

reset_runtime_actions() {
    DO_INSTALL=false; DO_UPDATE=false; DO_UNINSTALL=false; DO_CONNECT=false; DO_DISCONNECT=false; DO_LOGOUT=false; DO_STATUS=false; DO_IP=false; DO_DIAGNOSE=false; PING_TARGET=""; SERVICE_ACTION=""
}

gui_connect_wizard() {
    reset_runtime_actions
    DO_CONNECT=true
    HOSTNAME_OVERRIDE="$(ui_input "Tailscale" "Hostname (puste = bez zmiany):" "$HOSTNAME_OVERRIDE" || true)"
    local auth_method
    auth_method="$(ui_menu "Tailscale" "Metoda logowania" 1 "Auth key z pliku" 2 "Auth key wpisany teraz" 3 "Logowanie przeglądarką / URL" || true)"
    case "$auth_method" in
        1) AUTH_KEY_FILE="$(ui_input "Auth key" "Ścieżka do pliku z auth key:" "$AUTH_KEY_FILE" || true)"; AUTH_KEY="" ;;
        2) AUTH_KEY="$(ui_password "Auth key" "Wpisz auth key (nie zostanie zapisany w logu):" || true)"; AUTH_KEY_FILE="" ;;
        3) AUTH_KEY=""; AUTH_KEY_FILE="" ;;
        *) return 0 ;;
    esac
    if ui_yesno "Tailscale SSH" "Włączyć Tailscale SSH?"; then SSH=true; else SSH=false; fi
    if ui_yesno "Routes" "Akceptować trasy reklamowane przez inne nody?"; then ACCEPT_ROUTES=true; else ACCEPT_ROUTES=false; fi
    if ui_yesno "DNS" "Akceptować konfigurację DNS z tailnetu?"; then ACCEPT_DNS=true; else ACCEPT_DNS=false; fi
    execute_actions
    ui_msg "Tailscale" "Konfiguracja zakończona."
}

gui_routes() {
    local routes
    routes="$(ui_input "Subnet Router" "Trasy CIDR rozdzielone przecinkami. Puste = wyczyść reklamowane trasy:" "$ADVERTISE_ROUTES" || true)"
    if [[ -z "$routes" ]]; then CLEAR_ADVERTISE_ROUTES=true; ADVERTISE_ROUTES=""; else CLEAR_ADVERTISE_ROUTES=false; ADVERTISE_ROUTES="$routes"; fi
    validate_config
    configure_tailscale
    ui_msg "Subnet Router" "Ustawienia tras zastosowane. Reklamowane trasy mogą wymagać zatwierdzenia w panelu Tailscale."
}

gui_exit_node() {
    local choice
    choice="$(ui_menu "Exit Node" "Wybierz operację" 1 "Udostępnij ten serwer jako Exit Node" 2 "Wyłącz udostępnianie jako Exit Node" 3 "Używaj innego Exit Node" 4 "Przestań używać Exit Node" || true)"
    case "$choice" in
        1) ADVERTISE_EXIT_NODE=true; configure_tailscale ;;
        2) ADVERTISE_EXIT_NODE=false; configure_tailscale ;;
        3) EXIT_NODE="$(ui_input "Exit Node" "Hostname lub Tailscale IP:" "$EXIT_NODE" || true)"; [[ -n "$EXIT_NODE" ]] && configure_tailscale ;;
        4) CLEAR_EXIT_NODE=true; configure_tailscale ;;
    esac
}

gui_service() {
    local action
    action="$(ui_menu "tailscaled" "Zarządzanie usługą" start "Start" stop "Stop" restart "Restart" enable "Enable + start" disable "Disable + stop" status "Status" || true)"
    [[ -n "$action" ]] || return 0
    SERVICE_ACTION="$action"
    if [[ "$action" == status ]]; then
        local out
        out="$(systemctl status --no-pager tailscaled 2>&1 || true)"
        ui_msg "tailscaled" "$out"
    else
        service_action
        ui_msg "tailscaled" "Operacja '$action' zakończona."
    fi
}

gui_main() {
    detect_os
    ensure_ui || { cli_main; return; }
    while true; do
        local choice
        choice="$(ui_menu "ChrisScriptBase — Tailscale Manager" "Wybierz operację" \
            1 "Instalacja Tailscale" \
            2 "Połącz / autoryzuj serwer" \
            3 "Konfiguracja SSH/DNS/routes" \
            4 "Subnet Router" \
            5 "Exit Node" \
            6 "Status" \
            7 "Diagnostyka" \
            8 "Tailscale Ping" \
            9 "Usługa tailscaled" \
            10 "Aktualizacja" \
            11 "Rozłącz" \
            12 "Wyloguj urządzenie" \
            13 "Odinstaluj" \
            14 "Wyjście" || true)"
        case "$choice" in
            1) DO_INSTALL=true; install_tailscale; DO_INSTALL=false; ui_msg "Tailscale" "Instalacja zakończona." ;;
            2) gui_connect_wizard ;;
            3)
                if ui_yesno "Tailscale SSH" "Włączyć Tailscale SSH?"; then SSH=true; else SSH=false; fi
                if ui_yesno "DNS" "Akceptować DNS z tailnetu?"; then ACCEPT_DNS=true; else ACCEPT_DNS=false; fi
                if ui_yesno "Routes" "Akceptować subnet routes?"; then ACCEPT_ROUTES=true; else ACCEPT_ROUTES=false; fi
                configure_tailscale; ui_msg "Tailscale" "Konfiguracja zastosowana." ;;
            4) gui_routes ;;
            5) gui_exit_node ;;
            6) ui_msg "Status" "$(show_status 2>&1 || true)" ;;
            7) ui_msg "Diagnostyka" "$(run_diagnostics 2>&1 || true)" ;;
            8) PING_TARGET="$(ui_input "Tailscale Ping" "Host lub Tailscale IP:" "" || true)"; if [[ -n "$PING_TARGET" ]]; then ui_msg "Ping" "$(tailscale ping --c 3 "$PING_TARGET" 2>&1 || true)"; fi ;;
            9) gui_service ;;
            10) update_tailscale; ui_msg "Tailscale" "Aktualizacja zakończona." ;;
            11) disconnect_tailscale; ui_msg "Tailscale" "Urządzenie rozłączone." ;;
            12) if ui_yesno "Wylogowanie" "Wylogować to urządzenie z Tailscale?"; then logout_tailscale; fi ;;
            13) if ui_yesno "Odinstalowanie" "Usunąć Tailscale z systemu?"; then FORCE=true; uninstall_tailscale; fi ;;
            14|"") break ;;
        esac
    done
}

read_yesno() {
    local prompt="$1" default="$2" answer
    if [[ "$default" == true ]]; then
        read -r -p "$prompt [Y/n]: " answer
        [[ -z "$answer" || "$answer" =~ ^[YyTt]$ ]]
    else
        read -r -p "$prompt [y/N]: " answer
        [[ "$answer" =~ ^[YyTt]$ ]]
    fi
}

cli_connect_wizard() {
    reset_runtime_actions
    DO_CONNECT=true
    local value method
    read -r -p "Hostname [${HOSTNAME_OVERRIDE:-bez zmiany}]: " value
    [[ -z "$value" ]] || HOSTNAME_OVERRIDE="$value"
    printf 'Autoryzacja:\n1) Auth key z pliku\n2) Auth key wpisany teraz\n3) Logowanie URL/przeglądarka\n'
    read -r -p "Wybierz [3]: " method; method="${method:-3}"
    case "$method" in
        1) read -r -p "Ścieżka do auth key: " AUTH_KEY_FILE; AUTH_KEY="" ;;
        2) read -r -s -p "Auth key: " AUTH_KEY; printf '\n'; AUTH_KEY_FILE="" ;;
        3) AUTH_KEY=""; AUTH_KEY_FILE="" ;;
        *) die "$EXIT_ARGS" "Niepoprawny wybór." ;;
    esac
    if read_yesno "Włączyć Tailscale SSH?" true; then SSH=true; else SSH=false; fi
    if read_yesno "Akceptować routes?" false; then ACCEPT_ROUTES=true; else ACCEPT_ROUTES=false; fi
    if read_yesno "Akceptować DNS?" true; then ACCEPT_DNS=true; else ACCEPT_DNS=false; fi
    execute_actions
}

cli_main() {
    if has_action; then execute_actions; return; fi
    while true; do
        cat <<'EOF_MENU'

ChrisScriptBase — Tailscale Manager
1) Install
2) Connect / configure
3) Status
4) Diagnostics
5) Configure subnet routes
6) Configure exit node
7) Service
8) Update
9) Disconnect
10) Logout
11) Uninstall
12) Exit
EOF_MENU
        local choice
        read -r -p "Select: " choice
        case "$choice" in
            1) install_tailscale ;;
            2) cli_connect_wizard ;;
            3) show_status ;;
            4) run_diagnostics || true ;;
            5) read -r -p "Advertise routes (CIDR[,CIDR...], puste=clear): " ADVERTISE_ROUTES; if [[ -z "$ADVERTISE_ROUTES" ]]; then CLEAR_ADVERTISE_ROUTES=true; fi; validate_config; configure_tailscale ;;
            6) read -r -p "1=advertise this node, 2=disable advertise, 3=use exit node, 4=clear: " choice; case "$choice" in 1) ADVERTISE_EXIT_NODE=true;; 2) ADVERTISE_EXIT_NODE=false;; 3) read -r -p "Exit node host/IP: " EXIT_NODE;; 4) CLEAR_EXIT_NODE=true;; esac; configure_tailscale ;;
            7) read -r -p "Action start|stop|restart|enable|disable|status: " SERVICE_ACTION; validate_config; service_action ;;
            8) update_tailscale ;;
            9) disconnect_tailscale ;;
            10) logout_tailscale ;;
            11) if read_yesno "Odinstalować Tailscale?" false; then FORCE=true; uninstall_tailscale; fi ;;
            12) break ;;
            *) printf 'Niepoprawny wybór.\n' >&2 ;;
        esac
    done
}
