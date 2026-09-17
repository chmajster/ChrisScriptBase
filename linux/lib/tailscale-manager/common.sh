# shellcheck shell=bash

# ChrisScriptBase - Tailscale Manager
# GUI (dialog/whiptail), interactive CLI and fully non-interactive silent mode.

SCRIPT_NAME="tailscale-manager.sh"
SCRIPT_VERSION="1.0.0"
DEFAULT_CONFIG_FILE="/etc/chrisscriptbase/tailscale.conf"
DEFAULT_LOG_FILE="/var/log/chrisscriptbase/tailscale.log"
SYSCTL_FILE="/etc/sysctl.d/99-tailscale.conf"

EXIT_GENERAL=1
EXIT_ARGS=2
EXIT_UNSUPPORTED_OS=3
EXIT_DEPENDENCY=4
EXIT_INSTALL=5
EXIT_DAEMON=6
EXIT_AUTH=7
EXIT_CONFIG=8
EXIT_CONNECTIVITY=9
EXIT_VALIDATION=10

MODE="cli"
CONFIG_FILE=""
SAVE_CONFIG_FILE=""
LOG_FILE="${TAILSCALE_MANAGER_LOG_FILE:-$DEFAULT_LOG_FILE}"
DRY_RUN=false
VERBOSE=false
QUIET=false
FORCE=false
JSON_OUTPUT=false

DO_INSTALL=false
DO_UPDATE=false
DO_UNINSTALL=false
DO_CONNECT=false
DO_DISCONNECT=false
DO_LOGOUT=false
DO_STATUS=false
DO_IP=false
DO_DIAGNOSE=false
PING_TARGET=""
SERVICE_ACTION=""

AUTH_KEY=""
AUTH_KEY_FILE=""
HOSTNAME_OVERRIDE=""
SSH=""
ACCEPT_ROUTES=""
ACCEPT_DNS=""
ADVERTISE_ROUTES=""
CLEAR_ADVERTISE_ROUTES=false
ADVERTISE_EXIT_NODE=""
EXIT_NODE=""
CLEAR_EXIT_NODE=false
EXIT_NODE_ALLOW_LAN_ACCESS=""
ADVERTISE_TAGS=""
OPERATOR=""
SHIELDS_UP=""
FORCE_REAUTH=false
SNAT_SUBNET_ROUTES=""
STATEFUL_FILTERING=""
NETFILTER_MODE=""
AUTO_UPDATE=""
WEBCLIENT=""

OS_ID=""
OS_VERSION_ID=""
OS_FAMILY=""
PKG_MANAGER=""
UI_BIN=""
LOG_READY=false
TEMP_AUTH_FILE=""
AUTH_ARG=""

log_line() {
    local level="$1"; shift
    local message="$*"
    local line
    line="$(date '+%Y-%m-%d %H:%M:%S') [$level] $message"
    if [[ "$QUIET" != true || "$level" == "ERROR" ]]; then
        printf '%s\n' "$line" >&2
    fi
    if [[ "$LOG_READY" == true ]]; then
        printf '%s\n' "$line" >> "$LOG_FILE" 2>/dev/null || true
    fi
}

log_info(){ log_line INFO "$@"; }
log_success(){ log_line SUCCESS "$@"; }
log_warn(){ log_line WARNING "$@"; }
log_error(){ log_line ERROR "$@"; }
log_debug(){ if [[ "$VERBOSE" == true ]]; then log_line DEBUG "$@"; fi; }

die() {
    local code="$1"; shift
    log_error "$*"
    exit "$code"
}

cleanup() {
    if [[ -n "$TEMP_AUTH_FILE" && -f "$TEMP_AUTH_FILE" ]]; then
        rm -f -- "$TEMP_AUTH_FILE" || true
    fi
}
trap cleanup EXIT INT TERM


bool_normalize() {
    case "${1,,}" in
        1|true|yes|y|on|enabled) printf 'true' ;;
        0|false|no|n|off|disabled) printf 'false' ;;
        "") printf '' ;;
        *) return 1 ;;
    esac
}

trim() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

unquote_value() {
    local value
    value="$(trim "$1")"
    if [[ ${#value} -ge 2 ]]; then
        if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
            value="${value:1:${#value}-2}"
        elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
            value="${value:1:${#value}-2}"
        fi
    fi
    printf '%s' "$value"
}


init_logging() {
    [[ -n "$LOG_FILE" ]] || return 0
    local dir
    dir="$(dirname "$LOG_FILE")"
    if [[ $EUID -eq 0 ]]; then
        mkdir -p "$dir" 2>/dev/null || true
        touch "$LOG_FILE" 2>/dev/null || true
        chmod 600 "$LOG_FILE" 2>/dev/null || true
    fi
    if [[ -w "$LOG_FILE" ]]; then LOG_READY=true; else LOG_READY=false; fi
}

detect_os() {
    [[ -r /etc/os-release ]] || die "$EXIT_UNSUPPORTED_OS" "Brak /etc/os-release."
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_VERSION_ID="${VERSION_ID:-unknown}"
    case "$OS_ID" in
        ubuntu|debian|linuxmint|raspbian|pop) OS_FAMILY="debian" ;;
        rhel|centos|rocky|almalinux|fedora|ol|amzn) OS_FAMILY="rhel" ;;
        sles|sled|opensuse*|opensuse-leap|opensuse-tumbleweed) OS_FAMILY="suse" ;;
        arch|manjaro) OS_FAMILY="arch" ;;
        *)
            case " ${ID_LIKE:-} " in
                *" debian "*) OS_FAMILY="debian" ;;
                *" rhel "*|*" fedora "*) OS_FAMILY="rhel" ;;
                *" suse "*) OS_FAMILY="suse" ;;
                *" arch "*) OS_FAMILY="arch" ;;
                *) OS_FAMILY="unknown" ;;
            esac
            ;;
    esac
    local candidate
    for candidate in apt-get dnf yum zypper pacman; do
        if command -v "$candidate" >/dev/null 2>&1; then PKG_MANAGER="$candidate"; break; fi
    done
    log_debug "OS=$OS_ID VERSION=$OS_VERSION_ID FAMILY=$OS_FAMILY PKG_MANAGER=${PKG_MANAGER:-none}"
}

require_root() {
    [[ "$DRY_RUN" == true ]] && return 0
    [[ $EUID -eq 0 ]] || die "$EXIT_GENERAL" "Ta operacja wymaga root. Uruchom przez sudo."
}

command_exists(){ command -v "$1" >/dev/null 2>&1; }

run_cmd() {
    local -a cmd=("$@")
    if [[ "$DRY_RUN" == true ]]; then
        printf 'DRY RUN:'
        printf ' %q' "${cmd[@]}"
        printf '\n'
        return 0
    fi
    log_debug "Uruchamiam: ${cmd[*]}"
    "${cmd[@]}"
}

validate_hostname() {
    local value="$1"
    [[ ${#value} -le 253 && "$value" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ && "$value" != *".."* ]]
}

validate_ipv4() {
    local ip="$1" a b c d extra
    IFS=. read -r a b c d extra <<< "$ip"
    [[ -z "${extra:-}" && -n "${a:-}" && -n "${b:-}" && -n "${c:-}" && -n "${d:-}" ]] || return 1
    local oct
    for oct in "$a" "$b" "$c" "$d"; do
        [[ "$oct" =~ ^[0-9]{1,3}$ ]] || return 1
        ((10#$oct >= 0 && 10#$oct <= 255)) || return 1
    done
}

validate_cidr() {
    local cidr="$1" ip prefix
    [[ "$cidr" == */* ]] || return 1
    ip="${cidr%/*}"; prefix="${cidr##*/}"
    [[ "$prefix" =~ ^[0-9]+$ ]] || return 1
    if [[ "$ip" == *:* ]]; then
        ((prefix >= 0 && prefix <= 128)) || return 1
        if command_exists python3; then
            python3 - "$cidr" <<'PY' >/dev/null 2>&1
import ipaddress, sys
ipaddress.ip_network(sys.argv[1], strict=False)
PY
        else
            [[ "$ip" =~ ^[0-9A-Fa-f:]+$ && "$ip" == *:* ]]
        fi
    else
        ((prefix >= 0 && prefix <= 32)) || return 1
        validate_ipv4 "$ip"
    fi
}

validate_routes() {
    local routes="$1" route
    local -a _routes=()
    [[ -z "$routes" ]] && return 0
    IFS=',' read -r -a _routes <<< "$routes"
    for route in "${_routes[@]}"; do
        route="$(trim "$route")"
        validate_cidr "$route" || return 1
    done
}

validate_tags() {
    local tags="$1" tag
    local -a _tags=()
    [[ -z "$tags" ]] && return 0
    IFS=',' read -r -a _tags <<< "$tags"
    for tag in "${_tags[@]}"; do
        [[ "$tag" =~ ^tag:[A-Za-z0-9][A-Za-z0-9_-]*$ ]] || return 1
    done
}

validate_config() {
    [[ -z "$HOSTNAME_OVERRIDE" ]] || validate_hostname "$HOSTNAME_OVERRIDE" || die "$EXIT_VALIDATION" "Niepoprawny hostname: $HOSTNAME_OVERRIDE"
    [[ -z "$ADVERTISE_ROUTES" ]] || validate_routes "$ADVERTISE_ROUTES" || die "$EXIT_VALIDATION" "Niepoprawna lista tras: $ADVERTISE_ROUTES"
    [[ -z "$ADVERTISE_TAGS" ]] || validate_tags "$ADVERTISE_TAGS" || die "$EXIT_VALIDATION" "Niepoprawne tagi: $ADVERTISE_TAGS"
    [[ -z "$NETFILTER_MODE" || "$NETFILTER_MODE" =~ ^(on|nodivert|off)$ ]] || die "$EXIT_VALIDATION" "--netfilter-mode: dozwolone on|nodivert|off."
    [[ -z "$OPERATOR" ]] || id "$OPERATOR" >/dev/null 2>&1 || die "$EXIT_VALIDATION" "Użytkownik operatora '$OPERATOR' nie istnieje."
    if [[ -n "$AUTH_KEY_FILE" ]]; then
        [[ -r "$AUTH_KEY_FILE" ]] || die "$EXIT_AUTH" "Nie można odczytać auth-key-file: $AUTH_KEY_FILE"
        local perm
        perm="$(stat -c '%a' "$AUTH_KEY_FILE" 2>/dev/null || printf '')"
        if [[ -n "$perm" && "$perm" != "600" && "$perm" != "400" ]]; then
            log_warn "Auth key file ma uprawnienia $perm; zalecane 600 lub 400."
        fi
    fi
    [[ -z "$AUTH_KEY" || -z "$AUTH_KEY_FILE" ]] || die "$EXIT_ARGS" "Użyj tylko jednego z --auth-key lub --auth-key-file."
    if [[ -n "$AUTH_KEY" ]]; then
        log_warn "--auth-key może być widoczny w argv procesu uruchamiającego; preferuj --auth-key-file."
    fi
    if [[ "$DO_INSTALL" == true && "$DO_UNINSTALL" == true ]]; then
        die "$EXIT_ARGS" "Nie można łączyć --install i --uninstall."
    fi
    [[ -z "$SERVICE_ACTION" || "$SERVICE_ACTION" =~ ^(start|stop|restart|enable|disable|status)$ ]] || die "$EXIT_ARGS" "Niepoprawne --service: $SERVICE_ACTION"
}
