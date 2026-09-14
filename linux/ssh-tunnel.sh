#!/usr/bin/env bash
# SSH tunnel manager using tmux.
#
# Configure the variables below, then use:
#   ./ssh-tunnel.sh start
#   ./ssh-tunnel.sh stop
#   ./ssh-tunnel.sh restart
#   ./ssh-tunnel.sh status
#   ./ssh-tunnel.sh attach
#
# The tunnel listens only on 127.0.0.1 by default.

set -euo pipefail

TMUX_SESSION="ssh-tunnel"

SSH_USER="user"
SSH_HOST="server.example.com"
SSH_PORT="22"
SSH_KEY="$HOME/.ssh/id_ed25519"

LOCAL_BIND_ADDRESS="127.0.0.1"
LOCAL_PORT="3000"
REMOTE_HOST="127.0.0.1"
REMOTE_PORT="3000"

require_command() {
    local cmd="$1"

    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "Błąd: wymagane polecenie '$cmd' nie jest zainstalowane." >&2
        return 1
    fi
}

check_dependencies() {
    require_command ssh
    require_command tmux
}

session_exists() {
    tmux has-session -t "$TMUX_SESSION" 2>/dev/null
}

ssh_running() {
    if ! session_exists; then
        return 1
    fi

    local pane_pid
    pane_pid="$(tmux list-panes -t "$TMUX_SESSION" -F '#{pane_pid}' 2>/dev/null | head -n1 || true)"

    [[ -n "$pane_pid" ]] || return 1

    if command -v pgrep >/dev/null 2>&1; then
        pgrep -P "$pane_pid" -f '(^|/)ssh( |$)' >/dev/null 2>&1 && return 0
    fi

    tmux list-panes -t "$TMUX_SESSION" -F '#{pane_current_command}' 2>/dev/null \
        | grep -qx 'ssh'
}

build_ssh_command() {
    local -a ssh_cmd

    ssh_cmd=(
        ssh
        -N
        -L "${LOCAL_BIND_ADDRESS}:${LOCAL_PORT}:${REMOTE_HOST}:${REMOTE_PORT}"
        -p "$SSH_PORT"
        -o ServerAliveInterval=30
        -o ServerAliveCountMax=3
        -o ExitOnForwardFailure=yes
    )

    if [[ -n "$SSH_KEY" ]]; then
        if [[ ! -f "$SSH_KEY" ]]; then
            echo "Błąd: wskazany klucz SSH nie istnieje: $SSH_KEY" >&2
            return 1
        fi
        ssh_cmd+=( -i "$SSH_KEY" )
    fi

    ssh_cmd+=( "${SSH_USER}@${SSH_HOST}" )

    printf '%q ' "${ssh_cmd[@]}"
}

start_tunnel() {
    check_dependencies

    if session_exists; then
        if ssh_running; then
            echo "Tunel SSH już działa w sesji tmux '$TMUX_SESSION'."
            return 0
        fi

        echo "Sesja tmux '$TMUX_SESSION' istnieje, ale tunel SSH nie działa. Odtwarzam sesję."
        tmux kill-session -t "$TMUX_SESSION"
    fi

    local ssh_command
    ssh_command="$(build_ssh_command)"

    tmux new-session -d -s "$TMUX_SESSION"
    tmux send-keys -t "$TMUX_SESSION" "$ssh_command" C-m

    sleep 1

    if ! session_exists; then
        echo "Błąd: nie udało się utworzyć sesji tmux '$TMUX_SESSION'." >&2
        return 1
    fi

    if ! ssh_running; then
        echo "Błąd: sesja tmux została utworzona, ale proces SSH nie działa." >&2
        echo "Sprawdź szczegóły poleceniem:" >&2
        echo "  tmux attach -t $TMUX_SESSION" >&2
        return 1
    fi

    cat <<EOF
Tunel SSH uruchomiony.

Sesja tmux: $TMUX_SESSION
Lokalny adres: ${LOCAL_BIND_ADDRESS}:${LOCAL_PORT}
Cel: ${REMOTE_HOST}:${REMOTE_PORT}
Serwer SSH: ${SSH_USER}@${SSH_HOST}:${SSH_PORT}

Podgląd:
  tmux attach -t $TMUX_SESSION

Odłączenie od tmux:
  Ctrl+B, D

Zatrzymanie:
  $0 stop
EOF
}

stop_tunnel() {
    check_dependencies

    if ! session_exists; then
        echo "Tunel SSH nie działa. Sesja tmux '$TMUX_SESSION' nie istnieje."
        return 0
    fi

    tmux kill-session -t "$TMUX_SESSION"
    echo "Tunel SSH zatrzymany. Sesja tmux '$TMUX_SESSION' została zamknięta."
}

status_tunnel() {
    check_dependencies

    if ! session_exists; then
        echo "Status: STOPPED"
        echo "Sesja tmux '$TMUX_SESSION' nie istnieje."
        return 1
    fi

    if ssh_running; then
        echo "Status: RUNNING"
        echo "Sesja tmux: $TMUX_SESSION"
        echo "Lokalny adres: ${LOCAL_BIND_ADDRESS}:${LOCAL_PORT}"
        echo "Cel: ${REMOTE_HOST}:${REMOTE_PORT}"
        echo "Serwer SSH: ${SSH_USER}@${SSH_HOST}:${SSH_PORT}"
        return 0
    fi

    echo "Status: ERROR"
    echo "Sesja tmux '$TMUX_SESSION' istnieje, ale proces SSH nie działa."
    return 2
}

attach_tunnel() {
    check_dependencies

    if ! session_exists; then
        echo "Błąd: sesja tmux '$TMUX_SESSION' nie istnieje." >&2
        return 1
    fi

    exec tmux attach -t "$TMUX_SESSION"
}

usage() {
    cat <<EOF
Użycie: $0 {start|stop|restart|status|attach}

  start    Uruchom tunel SSH w sesji tmux
  stop     Zatrzymaj tunel i usuń sesję tmux
  restart  Uruchom tunel ponownie
  status   Sprawdź status sesji tmux i procesu SSH
  attach   Dołącz do sesji tmux
EOF
}

main() {
    local action="${1:-}"

    case "$action" in
        start)
            start_tunnel
            ;;
        stop)
            stop_tunnel
            ;;
        restart)
            stop_tunnel
            start_tunnel
            ;;
        status)
            status_tunnel
            ;;
        attach)
            attach_tunnel
            ;;
        -h|--help|help)
            usage
            ;;
        *)
            usage >&2
            return 64
            ;;
    esac
}

main "$@"
