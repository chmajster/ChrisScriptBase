# shellcheck shell=bash

usage() {
    cat <<'EOF'
ChrisScriptBase Nginx Manager

Usage:
  nginx-manager.sh
  nginx-manager.sh --help
  nginx-manager.sh --version
  nginx-manager.sh --status|--test|--reload|--restart|--backup|--list-sites|--diagnostic
  nginx-manager.sh --add-site --domain DOMAIN --root PATH [--port PORT] [--php-socket PATH]
  nginx-manager.sh --add-proxy --domain DOMAIN --backend-host HOST --backend-port PORT [--websocket]
  nginx-manager.sh --change-site-port --domain DOMAIN --port PORT
  nginx-manager.sh --set-default-port --port PORT
  nginx-manager.sh --move-listen-port [--from-port PORT] --port PORT
  nginx-manager.sh --disable-port-80 --port PORT

Options:
  --gui                    Wymuś interfejs dialog.
  --non-interactive       Nie uruchamiaj dialog ani pytań.
  --yes                    Potwierdź operacje destrukcyjne.
  --dry-run                Pokaż polecenia bez zmian.
  --status                 Status Nginx.
  --test                   Test nginx -t.
  --reload|--restart       Bezpieczny reload/restart po nginx -t.
  --backup                 Utwórz backup konfiguracji.
  --list-sites             Wyświetl Virtual Hosts.
  --diagnostic [PATH]      Wygeneruj raport diagnostyczny.
  --install|--update       Instalacja lub aktualizacja Nginx.
  --remove|--purge         Usuń pakiet; purge wymaga --yes i tworzy backup.
  --service ACTION         start|stop|restart|reload|enable|disable|status.
  --change-site-port       Zmień port HTTP wskazanego Virtual Hosta.
  --set-default-port       Zmień port domyślnego serwera Nginx.
  --move-listen-port       Przenieś wszystkie aktywne dyrektywy listen z --from-port na --port.
  --disable-port-80        Alias migracji wszystkich aktywnych listen z 80 na --port.
  --from-port PORT         Port źródłowy dla --move-listen-port; domyślnie 80.
  --ssl                    Włącz SSL przy tworzeniu strony/proxy.
  --quiet                  Ogranicz komunikaty.
EOF
}

parse_args() {
    while (($#)); do
        case "$1" in
            -h|--help) usage; exit 0 ;;
            --version) printf '%s %s\n' "$SCRIPT_NAME" "$SCRIPT_VERSION"; exit 0 ;;
            --gui) MODE="gui" ;;
            --non-interactive|--silent) MODE="silent"; NON_INTERACTIVE=true ;;
            --yes|-y) ASSUME_YES=true ;;
            --dry-run) DRY_RUN=true ;;
            --quiet) QUIET=true ;;
            --status) ACTION="status" ;;
            --test) ACTION="test" ;;
            --reload) ACTION="reload" ;;
            --restart) ACTION="restart" ;;
            --backup) ACTION="backup" ;;
            --list-sites) ACTION="list-sites" ;;
            --diagnostic)
                ACTION="diagnostic"
                if [[ -n "${2:-}" && "${2:-}" != --* ]]; then REPORT_FILE="$2"; shift; fi
                ;;
            --install) ACTION="install" ;;
            --update) ACTION="update" ;;
            --remove) ACTION="remove" ;;
            --purge) ACTION="purge" ;;
            --service) [[ -n "${2:-}" ]] || die "$EXIT_ARGS" "Brak wartości --service"; ACTION="service"; SERVICE_ACTION="$2"; shift ;;
            --add-site) ACTION="add-site" ;;
            --add-proxy) ACTION="add-proxy" ;;
            --change-site-port) ACTION="change-site-port" ;;
            --set-default-port) ACTION="set-default-port" ;;
            --move-listen-port|--move-http-port) ACTION="move-listen-port" ;;
            --disable-port-80) ACTION="move-listen-port"; SOURCE_PORT="80"; SOURCE_PORT_SET=true ;;
            --from-port) [[ -n "${2:-}" ]] || die "$EXIT_ARGS" "Brak wartości --from-port"; SOURCE_PORT="$2"; SOURCE_PORT_SET=true; shift ;;
            --domain) [[ -n "${2:-}" ]] || die "$EXIT_ARGS" "Brak wartości --domain"; DOMAIN="$2"; shift ;;
            --root) [[ -n "${2:-}" ]] || die "$EXIT_ARGS" "Brak wartości --root"; DOCUMENT_ROOT="$2"; shift ;;
            --port) [[ -n "${2:-}" ]] || die "$EXIT_ARGS" "Brak wartości --port"; LISTEN_PORT="$2"; PORT_SET=true; shift ;;
            --php-socket) [[ -n "${2:-}" ]] || die "$EXIT_ARGS" "Brak wartości --php-socket"; PHP_SOCKET="$2"; shift ;;
            --backend-host) [[ -n "${2:-}" ]] || die "$EXIT_ARGS" "Brak wartości --backend-host"; BACKEND_HOST="$2"; shift ;;
            --backend-port) [[ -n "${2:-}" ]] || die "$EXIT_ARGS" "Brak wartości --backend-port"; BACKEND_PORT="$2"; shift ;;
            --backend-scheme) [[ -n "${2:-}" ]] || die "$EXIT_ARGS" "Brak wartości --backend-scheme"; BACKEND_SCHEME="$2"; shift ;;
            --websocket) WEBSOCKET=true ;;
            --ssl) ENABLE_SSL=true ;;
            *) die "$EXIT_ARGS" "Nieznany argument: $1" ;;
        esac
        shift
    done
}

validate_action_arguments() {
    case "$ACTION" in
        service) [[ "$SERVICE_ACTION" =~ ^(start|stop|restart|reload|enable|disable|status)$ ]] || die "$EXIT_ARGS" "Niepoprawna akcja usługi." ;;
        add-site)
            validate_domain "$DOMAIN" || die "$EXIT_ARGS" "Niepoprawna domena."
            validate_safe_path "$DOCUMENT_ROOT" || die "$EXIT_ARGS" "Niepoprawny document root."
            validate_port "$LISTEN_PORT" || die "$EXIT_ARGS" "Niepoprawny port."
            [[ -z "$PHP_SOCKET" ]] || validate_safe_path "$PHP_SOCKET" || die "$EXIT_ARGS" "Niepoprawna ścieżka PHP socket."
            ;;
        add-proxy)
            validate_domain "$DOMAIN" || die "$EXIT_ARGS" "Niepoprawna domena."
            validate_host "$BACKEND_HOST" || die "$EXIT_ARGS" "Niepoprawny backend host."
            validate_port "$BACKEND_PORT" || die "$EXIT_ARGS" "Niepoprawny backend port."
            validate_port "$LISTEN_PORT" || die "$EXIT_ARGS" "Niepoprawny port wejściowy Nginx."
            [[ "$BACKEND_SCHEME" =~ ^https?$ ]] || die "$EXIT_ARGS" "Backend scheme: tylko http lub https."
            ;;
        change-site-port)
            validate_domain "$DOMAIN" || die "$EXIT_ARGS" "Niepoprawna domena."
            [[ "$PORT_SET" == true ]] || die "$EXIT_ARGS" "Akcja --change-site-port wymaga --port."
            validate_port "$LISTEN_PORT" || die "$EXIT_ARGS" "Niepoprawny port."
            ;;
        set-default-port)
            [[ "$PORT_SET" == true ]] || die "$EXIT_ARGS" "Akcja --set-default-port wymaga --port."
            validate_port "$LISTEN_PORT" || die "$EXIT_ARGS" "Niepoprawny port."
            ;;
        move-listen-port)
            [[ "$PORT_SET" == true ]] || die "$EXIT_ARGS" "Akcja --move-listen-port wymaga docelowego --port."
            validate_port "$SOURCE_PORT" || die "$EXIT_ARGS" "Niepoprawny port źródłowy."
            validate_port "$LISTEN_PORT" || die "$EXIT_ARGS" "Niepoprawny port docelowy."
            [[ "$SOURCE_PORT" != "$LISTEN_PORT" ]] || die "$EXIT_ARGS" "Port źródłowy i docelowy muszą być różne."
            ;;
    esac
}
