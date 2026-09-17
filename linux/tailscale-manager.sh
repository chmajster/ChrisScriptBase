#!/usr/bin/env bash
set -Eeuo pipefail

# ChrisScriptBase - Tailscale Manager
# GUI (dialog/whiptail), interactive CLI and fully non-interactive silent mode.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=linux/lib/tailscale-manager/common.sh
source "$SCRIPT_DIR/lib/tailscale-manager/common.sh"
# shellcheck source=linux/lib/tailscale-manager/config.sh
source "$SCRIPT_DIR/lib/tailscale-manager/config.sh"
# shellcheck source=linux/lib/tailscale-manager/core.sh
source "$SCRIPT_DIR/lib/tailscale-manager/core.sh"
# shellcheck source=linux/lib/tailscale-manager/ui.sh
source "$SCRIPT_DIR/lib/tailscale-manager/ui.sh"

execute_actions() {
    validate_config
    if [[ "$DO_INSTALL" == true ]]; then install_tailscale; fi
    if [[ "$DO_UPDATE" == true ]]; then update_tailscale; fi
    if [[ "$DO_UNINSTALL" == true ]]; then uninstall_tailscale; fi
    if [[ "$DO_CONNECT" == true ]]; then connect_tailscale; fi
    if has_settings && [[ "$DO_UNINSTALL" != true ]]; then configure_tailscale; fi
    if [[ "$DO_DISCONNECT" == true ]]; then disconnect_tailscale; fi
    if [[ "$DO_LOGOUT" == true ]]; then logout_tailscale; fi
    if [[ -n "$SERVICE_ACTION" ]]; then service_action; fi
    if [[ "$DO_STATUS" == true ]]; then show_status; fi
    if [[ "$DO_IP" == true ]]; then show_ip; fi
    if [[ "$DO_DIAGNOSE" == true ]]; then run_diagnostics; fi
    if [[ -n "$PING_TARGET" ]]; then ping_peer; fi
    if [[ -n "$SAVE_CONFIG_FILE" ]]; then save_config; fi
}

main() {
    preparse_config "$@"
    load_config
    parse_args "$@"
    init_logging
    normalize_actions
    log_debug "Mode=$MODE dry_run=$DRY_RUN"

    case "$MODE" in
        gui) gui_main ;;
        cli) cli_main ;;
        silent) execute_actions ;;
        *) die "$EXIT_ARGS" "Niepoprawny tryb: $MODE" ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
