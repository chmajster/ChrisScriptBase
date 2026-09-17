# shellcheck shell=bash

usage() {
    cat <<'EOF_HELP'
ChrisScriptBase - Tailscale Manager

Użycie:
  sudo bash linux/tailscale-manager.sh [--gui|--cli|--silent] [akcje] [opcje]

TRYBY
  --gui                         Interfejs dialog/whiptail z fallbackiem do CLI.
  --cli                         Interaktywny terminal (domyślny).
  --silent                      Bez pytań; wszystkie dane muszą pochodzić z argumentów/configu.

AKCJE
  --install                     Zainstaluj Tailscale i uruchom tailscaled.
  --update                      Zaktualizuj Tailscale.
  --uninstall                   Usuń pakiet Tailscale (w silent wymaga --force).
  --connect                     Połącz/autoryzuj urządzenie.
  --disconnect                  tailscale down.
  --logout                      tailscale logout.
  --status                      Pokaż status.
  --ip                          Pokaż adresy Tailscale.
  --diagnose                    Uruchom diagnostykę (status, IP, DNS, netcheck, routes).
  --ping HOST                   Uruchom tailscale ping HOST.
  --service ACTION              start|stop|restart|enable|disable|status.

AUTORYZACJA
  --auth-key KEY                Auth key. Preferuj --auth-key-file; klucz jest maskowany w logach.
  --auth-key-file PATH          Plik z kluczem; Tailscale otrzymuje file:PATH.
  --force-reauth                Wymuś ponowną autoryzację przy tailscale up.

KONFIGURACJA URZĄDZENIA
  --hostname NAME
  --ssh | --no-ssh
  --accept-routes | --no-accept-routes
  --accept-dns | --no-accept-dns
  --advertise-routes CIDR[,CIDR...]
  --clear-advertise-routes
  --advertise-exit-node | --no-advertise-exit-node
  --exit-node HOST_OR_IP
  --clear-exit-node
  --exit-node-allow-lan-access | --no-exit-node-allow-lan-access
  --advertise-tags tag:server[,tag:other]
  --operator USER
  --shields-up | --no-shields-up
  --snat-subnet-routes | --no-snat-subnet-routes
  --stateful-filtering | --no-stateful-filtering
  --netfilter-mode on|nodivert|off
  --auto-update | --no-auto-update
  --webclient | --no-webclient

PLIKI / OUTPUT
  --config PATH                 Załaduj konfigurację KEY=VALUE. Argumenty CLI mają wyższy priorytet.
  --save-config [PATH]          Zapisz konfigurację bez sekretu (domyślnie /etc/chrisscriptbase/tailscale.conf).
  --log-file PATH               Ścieżka logu.
  --json                        JSON tam, gdzie wspiera go Tailscale.
  --dry-run                     Nie wykonuj zmian.
  --verbose                     Więcej diagnostyki.
  --quiet                       Tylko błędy / wynik właściwej komendy.
  --force                       Potwierdzenie operacji destrukcyjnych w silent.
  -h, --help
  --version

PRIORYTET KONFIGURACJI
  wartości domyślne < plik --config < argumenty CLI

PRZYKŁADY
  sudo bash linux/tailscale-manager.sh --gui
  sudo bash linux/tailscale-manager.sh --cli

  sudo bash linux/tailscale-manager.sh --silent --install \
    --auth-key-file /root/tailscale.key --hostname server01 --ssh --accept-routes

  sudo bash linux/tailscale-manager.sh --silent --install \
    --auth-key-file /root/tailscale.key --hostname router01 \
    --advertise-routes 192.168.1.0/24

  sudo bash linux/tailscale-manager.sh --silent --install \
    --auth-key-file /root/tailscale.key --hostname exit01 --advertise-exit-node

  sudo bash linux/tailscale-manager.sh --silent --status
  sudo bash linux/tailscale-manager.sh --silent --diagnose
  sudo bash linux/tailscale-manager.sh --silent --ping server02

EXIT CODES
  0  success
  1  general error
  2  invalid arguments
  3  unsupported operating system
  4  dependency error
  5  installation/update/uninstall error
  6  tailscaled/service error
  7  authentication error
  8  configuration error
  9  connectivity error
  10 validation error
EOF_HELP
}

print_version() {
    printf '%s %s\n' "$SCRIPT_NAME" "$SCRIPT_VERSION"
}


set_config_key() {
    local key="$1" value="$2" normalized
    case "$key" in
        INSTALL) normalized="$(bool_normalize "$value")" || return 1; DO_INSTALL="$normalized" ;;
        UPDATE) normalized="$(bool_normalize "$value")" || return 1; DO_UPDATE="$normalized" ;;
        UNINSTALL) normalized="$(bool_normalize "$value")" || return 1; DO_UNINSTALL="$normalized" ;;
        CONNECT) normalized="$(bool_normalize "$value")" || return 1; DO_CONNECT="$normalized" ;;
        DISCONNECT) normalized="$(bool_normalize "$value")" || return 1; DO_DISCONNECT="$normalized" ;;
        LOGOUT) normalized="$(bool_normalize "$value")" || return 1; DO_LOGOUT="$normalized" ;;
        STATUS) normalized="$(bool_normalize "$value")" || return 1; DO_STATUS="$normalized" ;;
        DIAGNOSE) normalized="$(bool_normalize "$value")" || return 1; DO_DIAGNOSE="$normalized" ;;
        HOSTNAME) HOSTNAME_OVERRIDE="$value" ;;
        AUTH_KEY_FILE) AUTH_KEY_FILE="$value" ;;
        SSH) SSH="$(bool_normalize "$value")" || return 1 ;;
        ACCEPT_ROUTES) ACCEPT_ROUTES="$(bool_normalize "$value")" || return 1 ;;
        ACCEPT_DNS) ACCEPT_DNS="$(bool_normalize "$value")" || return 1 ;;
        ADVERTISE_ROUTES) ADVERTISE_ROUTES="$value" ;;
        ADVERTISE_EXIT_NODE) ADVERTISE_EXIT_NODE="$(bool_normalize "$value")" || return 1 ;;
        EXIT_NODE) EXIT_NODE="$value" ;;
        EXIT_NODE_ALLOW_LAN_ACCESS) EXIT_NODE_ALLOW_LAN_ACCESS="$(bool_normalize "$value")" || return 1 ;;
        ADVERTISE_TAGS) ADVERTISE_TAGS="$value" ;;
        OPERATOR) OPERATOR="$value" ;;
        SHIELDS_UP) SHIELDS_UP="$(bool_normalize "$value")" || return 1 ;;
        SNAT_SUBNET_ROUTES) SNAT_SUBNET_ROUTES="$(bool_normalize "$value")" || return 1 ;;
        STATEFUL_FILTERING) STATEFUL_FILTERING="$(bool_normalize "$value")" || return 1 ;;
        NETFILTER_MODE) NETFILTER_MODE="$value" ;;
        AUTO_UPDATE) AUTO_UPDATE="$(bool_normalize "$value")" || return 1 ;;
        WEBCLIENT) WEBCLIENT="$(bool_normalize "$value")" || return 1 ;;
        LOG_FILE) LOG_FILE="$value" ;;
        *) log_warn "Pomijam nieznany klucz konfiguracji: $key" ;;
    esac
}

preparse_config() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --config)
                [[ $# -ge 2 ]] || die "$EXIT_ARGS" "--config wymaga ścieżki."
                CONFIG_FILE="$2"; shift 2 ;;
            --config=*) CONFIG_FILE="${1#*=}"; shift ;;
            *) shift ;;
        esac
    done
}

load_config() {
    [[ -n "$CONFIG_FILE" ]] || return 0
    [[ -r "$CONFIG_FILE" ]] || die "$EXIT_CONFIG" "Nie można odczytać configu: $CONFIG_FILE"
    local raw line key value lineno=0
    while IFS= read -r raw || [[ -n "$raw" ]]; do
        ((lineno+=1))
        line="$(trim "$raw")"
        [[ -z "$line" || "${line:0:1}" == "#" ]] && continue
        [[ "$line" == *=* ]] || die "$EXIT_CONFIG" "Niepoprawna linia $lineno w $CONFIG_FILE (oczekiwano KEY=VALUE)."
        key="$(trim "${line%%=*}")"
        value="$(unquote_value "${line#*=}")"
        [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || die "$EXIT_CONFIG" "Niepoprawny klucz '$key' w $CONFIG_FILE:$lineno"
        [[ "$key" != "AUTH_KEY" ]] || die "$EXIT_CONFIG" "AUTH_KEY nie może być zapisany w configu; użyj AUTH_KEY_FILE."
        set_config_key "$key" "$value" || die "$EXIT_CONFIG" "Niepoprawna wartość '$value' dla $key w $CONFIG_FILE:$lineno"
    done < "$CONFIG_FILE"
}

require_arg() {
    local opt="$1" count="$2"
    (( count >= 2 )) || die "$EXIT_ARGS" "$opt wymaga wartości."
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help) usage; exit 0 ;;
            --version) print_version; exit 0 ;;
            --gui) MODE="gui"; shift ;;
            --cli) MODE="cli"; shift ;;
            --silent) MODE="silent"; shift ;;
            --install) DO_INSTALL=true; shift ;;
            --update) DO_UPDATE=true; shift ;;
            --uninstall) DO_UNINSTALL=true; shift ;;
            --connect) DO_CONNECT=true; shift ;;
            --disconnect) DO_DISCONNECT=true; shift ;;
            --logout) DO_LOGOUT=true; shift ;;
            --status) DO_STATUS=true; shift ;;
            --ip) DO_IP=true; shift ;;
            --diagnose) DO_DIAGNOSE=true; shift ;;
            --ping) require_arg "$1" "$#"; PING_TARGET="$2"; shift 2 ;;
            --service) require_arg "$1" "$#"; SERVICE_ACTION="$2"; shift 2 ;;
            --auth-key) require_arg "$1" "$#"; AUTH_KEY="$2"; shift 2 ;;
            --auth-key=*) AUTH_KEY="${1#*=}"; shift ;;
            --auth-key-file) require_arg "$1" "$#"; AUTH_KEY_FILE="$2"; shift 2 ;;
            --auth-key-file=*) AUTH_KEY_FILE="${1#*=}"; shift ;;
            --hostname) require_arg "$1" "$#"; HOSTNAME_OVERRIDE="$2"; shift 2 ;;
            --hostname=*) HOSTNAME_OVERRIDE="${1#*=}"; shift ;;
            --ssh) SSH=true; shift ;;
            --no-ssh) SSH=false; shift ;;
            --accept-routes) ACCEPT_ROUTES=true; shift ;;
            --no-accept-routes) ACCEPT_ROUTES=false; shift ;;
            --accept-dns) ACCEPT_DNS=true; shift ;;
            --no-accept-dns) ACCEPT_DNS=false; shift ;;
            --advertise-routes) require_arg "$1" "$#"; ADVERTISE_ROUTES="$2"; shift 2 ;;
            --advertise-routes=*) ADVERTISE_ROUTES="${1#*=}"; shift ;;
            --clear-advertise-routes) CLEAR_ADVERTISE_ROUTES=true; ADVERTISE_ROUTES=""; shift ;;
            --advertise-exit-node) ADVERTISE_EXIT_NODE=true; shift ;;
            --no-advertise-exit-node) ADVERTISE_EXIT_NODE=false; shift ;;
            --exit-node) require_arg "$1" "$#"; EXIT_NODE="$2"; shift 2 ;;
            --exit-node=*) EXIT_NODE="${1#*=}"; shift ;;
            --clear-exit-node) CLEAR_EXIT_NODE=true; EXIT_NODE=""; shift ;;
            --exit-node-allow-lan-access) EXIT_NODE_ALLOW_LAN_ACCESS=true; shift ;;
            --no-exit-node-allow-lan-access) EXIT_NODE_ALLOW_LAN_ACCESS=false; shift ;;
            --advertise-tags) require_arg "$1" "$#"; ADVERTISE_TAGS="$2"; shift 2 ;;
            --advertise-tags=*) ADVERTISE_TAGS="${1#*=}"; shift ;;
            --operator) require_arg "$1" "$#"; OPERATOR="$2"; shift 2 ;;
            --operator=*) OPERATOR="${1#*=}"; shift ;;
            --shields-up) SHIELDS_UP=true; shift ;;
            --no-shields-up) SHIELDS_UP=false; shift ;;
            --force-reauth) FORCE_REAUTH=true; shift ;;
            --snat-subnet-routes) SNAT_SUBNET_ROUTES=true; shift ;;
            --no-snat-subnet-routes) SNAT_SUBNET_ROUTES=false; shift ;;
            --stateful-filtering) STATEFUL_FILTERING=true; shift ;;
            --no-stateful-filtering) STATEFUL_FILTERING=false; shift ;;
            --netfilter-mode) require_arg "$1" "$#"; NETFILTER_MODE="$2"; shift 2 ;;
            --netfilter-mode=*) NETFILTER_MODE="${1#*=}"; shift ;;
            --auto-update) AUTO_UPDATE=true; shift ;;
            --no-auto-update) AUTO_UPDATE=false; shift ;;
            --webclient) WEBCLIENT=true; shift ;;
            --no-webclient) WEBCLIENT=false; shift ;;
            --config) require_arg "$1" "$#"; CONFIG_FILE="$2"; shift 2 ;;
            --config=*) CONFIG_FILE="${1#*=}"; shift ;;
            --save-config)
                if [[ $# -ge 2 && "$2" != --* ]]; then SAVE_CONFIG_FILE="$2"; shift 2; else SAVE_CONFIG_FILE="$DEFAULT_CONFIG_FILE"; shift; fi ;;
            --save-config=*) SAVE_CONFIG_FILE="${1#*=}"; shift ;;
            --log-file) require_arg "$1" "$#"; LOG_FILE="$2"; shift 2 ;;
            --log-file=*) LOG_FILE="${1#*=}"; shift ;;
            --json) JSON_OUTPUT=true; shift ;;
            --dry-run) DRY_RUN=true; shift ;;
            --verbose) VERBOSE=true; shift ;;
            --quiet) QUIET=true; shift ;;
            --force) FORCE=true; shift ;;
            *) die "$EXIT_ARGS" "Nieznana opcja: $1" ;;
        esac
    done
}


has_settings() {
    [[ -n "$HOSTNAME_OVERRIDE$SSH$ACCEPT_ROUTES$ACCEPT_DNS$ADVERTISE_ROUTES$ADVERTISE_EXIT_NODE$EXIT_NODE$EXIT_NODE_ALLOW_LAN_ACCESS$ADVERTISE_TAGS$OPERATOR$SHIELDS_UP$SNAT_SUBNET_ROUTES$STATEFUL_FILTERING$NETFILTER_MODE$AUTO_UPDATE$WEBCLIENT" || "$CLEAR_ADVERTISE_ROUTES" == true || "$CLEAR_EXIT_NODE" == true ]]
}

has_action() {
    [[ "$DO_INSTALL" == true || "$DO_UPDATE" == true || "$DO_UNINSTALL" == true || "$DO_CONNECT" == true || "$DO_DISCONNECT" == true || "$DO_LOGOUT" == true || "$DO_STATUS" == true || "$DO_IP" == true || "$DO_DIAGNOSE" == true || -n "$PING_TARGET" || -n "$SERVICE_ACTION" || -n "$SAVE_CONFIG_FILE" ]] || has_settings
}

normalize_actions() {
    if [[ -n "$AUTH_KEY" || -n "$AUTH_KEY_FILE" || -n "$ADVERTISE_TAGS" || "$FORCE_REAUTH" == true ]]; then DO_CONNECT=true; fi
    if [[ "$DO_INSTALL" == true ]] && has_settings; then DO_CONNECT=true; fi
    if [[ "$MODE" == "silent" ]] && ! has_action; then
        die "$EXIT_ARGS" "Tryb --silent wymaga akcji, np. --install, --status lub --connect."
    fi
}


save_config() {
    [[ -n "$SAVE_CONFIG_FILE" ]] || return 0
    if [[ "$DRY_RUN" == true ]]; then
        printf 'DRY RUN: zapis konfiguracji do %s (bez auth key)\n' "$SAVE_CONFIG_FILE"
        return 0
    fi
    local dir
    dir="$(dirname "$SAVE_CONFIG_FILE")"
    [[ -d "$dir" ]] || { require_root; mkdir -p "$dir"; }
    umask 077
    cat > "$SAVE_CONFIG_FILE" <<EOF_CFG
# ChrisScriptBase Tailscale Manager configuration
# Sekrety nie są zapisywane. Użyj AUTH_KEY_FILE.
INSTALL=$DO_INSTALL
CONNECT=$DO_CONNECT
HOSTNAME=$HOSTNAME_OVERRIDE
AUTH_KEY_FILE=$AUTH_KEY_FILE
SSH=$SSH
ACCEPT_ROUTES=$ACCEPT_ROUTES
ACCEPT_DNS=$ACCEPT_DNS
ADVERTISE_ROUTES=$ADVERTISE_ROUTES
ADVERTISE_EXIT_NODE=$ADVERTISE_EXIT_NODE
EXIT_NODE=$EXIT_NODE
EXIT_NODE_ALLOW_LAN_ACCESS=$EXIT_NODE_ALLOW_LAN_ACCESS
ADVERTISE_TAGS=$ADVERTISE_TAGS
OPERATOR=$OPERATOR
SHIELDS_UP=$SHIELDS_UP
SNAT_SUBNET_ROUTES=$SNAT_SUBNET_ROUTES
STATEFUL_FILTERING=$STATEFUL_FILTERING
NETFILTER_MODE=$NETFILTER_MODE
AUTO_UPDATE=$AUTO_UPDATE
WEBCLIENT=$WEBCLIENT
EOF_CFG
    chmod 600 "$SAVE_CONFIG_FILE"
    log_success "Konfiguracja zapisana: $SAVE_CONFIG_FILE"
}
