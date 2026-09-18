#!/usr/bin/env bash
set -o pipefail

# ChrisScriptBase - Nginx Manager

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=linux/lib/nginx-manager/common.sh
source "$SCRIPT_DIR/lib/nginx-manager/common.sh"
# shellcheck source=linux/lib/nginx-manager/args.sh
source "$SCRIPT_DIR/lib/nginx-manager/args.sh"
# shellcheck source=linux/lib/nginx-manager/nginx.sh
source "$SCRIPT_DIR/lib/nginx-manager/nginx.sh"
# shellcheck source=linux/lib/nginx-manager/sites.sh
source "$SCRIPT_DIR/lib/nginx-manager/sites.sh"
# shellcheck source=linux/lib/nginx-manager/ssl.sh
source "$SCRIPT_DIR/lib/nginx-manager/ssl.sh"
# shellcheck source=linux/lib/nginx-manager/ui.sh
source "$SCRIPT_DIR/lib/nginx-manager/ui.sh"

execute_action() {
    case "$ACTION" in
        status) show_server_status ;;
        test) test_nginx_config ;;
        reload) reload_nginx ;;
        restart) restart_nginx ;;
        backup) create_backup ;;
        list-sites) list_sites ;;
        diagnostic) generate_diagnostic_report "$REPORT_FILE" ;;
        install) install_nginx ;;
        update) update_nginx ;;
        remove) remove_nginx false ;;
        purge) remove_nginx true ;;
        service) service_action "$SERVICE_ACTION" ;;
        add-site) create_site "$DOMAIN" "$DOCUMENT_ROOT" "$LISTEN_PORT" "$PHP_SOCKET" "$ENABLE_SSL" ;;
        add-proxy) create_reverse_proxy "$DOMAIN" "$BACKEND_HOST" "$BACKEND_PORT" "$BACKEND_SCHEME" "$WEBSOCKET" "$ENABLE_SSL" "$LISTEN_PORT" ;;
        change-site-port) change_site_port "$DOMAIN" "$LISTEN_PORT" ;;
        set-default-port) change_default_port "$LISTEN_PORT" ;;
        move-listen-port) move_all_listen_ports "$SOURCE_PORT" "$LISTEN_PORT" ;;
        "") return 0 ;;
        *) die "$EXIT_ARGS" "Nieznana akcja: $ACTION" ;;
    esac
}

main() {
    parse_args "$@"
    init_logging
    detect_os
    detect_nginx_layout

    if [[ "$MODE" == "gui" ]]; then
        gui_main
    elif [[ "$MODE" == "cli" && -z "$ACTION" ]]; then
        gui_main
    else
        [[ -n "$ACTION" ]] || die "$EXIT_ARGS" "Tryb nieinteraktywny wymaga akcji."
        validate_action_arguments
        execute_action
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
