# shellcheck shell=bash

SCRIPT_NAME="nginx-manager.sh"
SCRIPT_VERSION="1.1.0"

EXIT_GENERAL=1
EXIT_ARGS=2
EXIT_PERMISSION=3
EXIT_DEPENDENCY=4
EXIT_CONFIG=5
EXIT_SERVICE=6
EXIT_BACKUP=7

MODE="cli"
ACTION=""
NON_INTERACTIVE=false
ASSUME_YES=false
DRY_RUN=false
QUIET=false

NGINX_ETC="${NGINX_MANAGER_ETC:-/etc/nginx}"
BACKUP_DIR="${NGINX_MANAGER_BACKUP_DIR:-/var/backups/chrisscriptbase/nginx}"
LOG_FILE="${NGINX_MANAGER_LOG_FILE:-/var/log/chrisscriptbase/nginx-manager.log}"
REPORT_FILE=""
MAIN_CONFIG="$NGINX_ETC/nginx.conf"
SITES_AVAILABLE=""
SITES_ENABLED=""
CONF_D=""
LAYOUT="unknown"
PKG_MANAGER=""
OS_ID="unknown"
OS_FAMILY="unknown"
UI_BIN=""
LOG_READY=false
TEMP_ROOT="${TMPDIR:-/tmp}/nginx-manager.${UID:-0}.$$"

DOMAIN=""
DOCUMENT_ROOT=""
LISTEN_PORT="80"
PORT_SET=false
PHP_SOCKET=""
ENABLE_SSL=false
BACKEND_HOST="127.0.0.1"
BACKEND_PORT="8080"
BACKEND_SCHEME="http"
WEBSOCKET=false
SERVICE_ACTION=""

cleanup() {
    [[ "$TEMP_ROOT" == "${TMPDIR:-/tmp}"/nginx-manager.* ]] && rm -rf -- "$TEMP_ROOT" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

command_exists() { command -v "$1" >/dev/null 2>&1; }

log_event() {
    local action="$1" result="$2" detail="${3:-}"
    local line
    line="$(date '+%Y-%m-%d %H:%M:%S') | user=$(id -un 2>/dev/null || printf unknown) | action=$action | result=$result"
    [[ -z "$detail" ]] || line+=" | detail=${detail//$'\n'/ }"
    if [[ "$LOG_READY" == true ]]; then
        printf '%s\n' "$line" >> "$LOG_FILE" 2>/dev/null || true
    fi
}

info() { [[ "$QUIET" == true ]] || printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
error() { printf 'ERROR: %s\n' "$*" >&2; }
die() { local code="$1"; shift; error "$*"; exit "$code"; }

init_logging() {
    local dir
    dir="$(dirname "$LOG_FILE")"
    if [[ $EUID -eq 0 ]]; then
        mkdir -p -- "$dir" 2>/dev/null || true
        touch "$LOG_FILE" 2>/dev/null || true
        chmod 600 "$LOG_FILE" 2>/dev/null || true
    fi
    [[ -w "$LOG_FILE" ]] && LOG_READY=true || LOG_READY=false
}

detect_os() {
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        OS_ID="${ID:-unknown}"
        case "$OS_ID" in
            debian|ubuntu|linuxmint) OS_FAMILY="debian" ;;
            rhel|rocky|almalinux|centos|fedora) OS_FAMILY="rhel" ;;
            *)
                case " ${ID_LIKE:-} " in
                    *" debian "*) OS_FAMILY="debian" ;;
                    *" rhel "*|*" fedora "*) OS_FAMILY="rhel" ;;
                esac
                ;;
        esac
    fi
    local manager
    for manager in apt-get dnf yum; do
        if command_exists "$manager"; then PKG_MANAGER="$manager"; break; fi
    done
}

detect_nginx_layout() {
    MAIN_CONFIG="$NGINX_ETC/nginx.conf"
    if [[ -d "$NGINX_ETC/sites-available" || "$OS_FAMILY" == "debian" ]]; then
        LAYOUT="debian"
        SITES_AVAILABLE="$NGINX_ETC/sites-available"
        SITES_ENABLED="$NGINX_ETC/sites-enabled"
        CONF_D="$NGINX_ETC/conf.d"
    else
        LAYOUT="rhel"
        CONF_D="$NGINX_ETC/conf.d"
        SITES_AVAILABLE="$CONF_D"
        SITES_ENABLED="$CONF_D"
    fi

    if command_exists nginx; then
        local dump include
        dump="$(nginx -T 2>&1 || true)"
        include="$(printf '%s\n' "$dump" | sed -n 's/^[[:space:]]*include[[:space:]]\+\([^;]*\);.*/\1/p' | head -n1)"
        if [[ "$include" == */sites-enabled/* ]]; then
            SITES_ENABLED="${include%/*}"
            [[ -d "${SITES_ENABLED%/sites-enabled}/sites-available" ]] && SITES_AVAILABLE="${SITES_ENABLED%/sites-enabled}/sites-available"
            LAYOUT="debian"
        elif [[ "$include" == */conf.d/* ]]; then
            CONF_D="${include%/*}"
            [[ "$LAYOUT" != "debian" ]] && SITES_AVAILABLE="$CONF_D" && SITES_ENABLED="$CONF_D"
        fi
    fi
}

require_root() {
    [[ "$DRY_RUN" == true ]] && return 0
    [[ $EUID -eq 0 ]] || die "$EXIT_PERMISSION" "Ta operacja wymaga root. Uruchom skrypt przez sudo."
}

run_command() {
    if [[ "$DRY_RUN" == true ]]; then
        printf 'DRY RUN:'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    "$@"
}

make_temp() {
    local file
    mkdir -p -m 700 -- "$TEMP_ROOT" || return 1
    file="$(mktemp "$TEMP_ROOT/file.XXXXXX")" || return 1
    printf '%s' "$file"
}

confirm_action() {
    local operation="$1" object="$2" changes="$3"
    [[ "$ASSUME_YES" == true ]] && return 0
    [[ "$NON_INTERACTIVE" == false ]] || die "$EXIT_ARGS" "$operation wymaga --yes w trybie nieinteraktywnym."
    local answer
    printf 'Operacja: %s\nObiekt: %s\nZmiany: %s\n' "$operation" "$object" "$changes"
    read -r -p "Wykonać? [y/N]: " answer
    [[ "$answer" =~ ^[YyTt]$ ]]
}

validate_domain() {
    local value="${1,,}"
    [[ ${#value} -le 253 && "$value" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ && "$value" != *".."* ]]
}

validate_port() { [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535)); }

validate_host() {
    [[ "$1" =~ ^[A-Za-z0-9:._-]+$ && "$1" != *".."* ]]
}

validate_safe_path() {
    local value="$1"
    [[ "$value" == /* && "$value" != *"/../"* && "$value" != */.. && "$value" != *$'\n'* && "$value" != *$'\r'* ]]
}

escape_nginx_value() {
    local value="$1"
    [[ "$value" != *'$'* && "$value" != *';'* && "$value" != *'{'* && "$value" != *'}'* && "$value" != *$'\n'* ]]
}
