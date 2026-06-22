#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

LOG_DIR="/var/log/patching"
LOG_FILE="${LOG_DIR}/patching.log"

# List applications/services to stop before patching.
# Use systemd service names without ".service", for example: nginx, apache2, tomcat.
STOP_APPLICATIONS=(
    # nginx
    # apache2
    # tomcat
)

# Optional runtime override:
# APPS_TO_STOP="nginx apache2 my-app" ./os_patching.sh
APPS_TO_STOP="${APPS_TO_STOP:-${STOP_APPLICATIONS[*]}}"

OS_NAME="unsupported_os"
OS_VERSION="unknown"
OS_MAJOR="unknown"
OS_MINOR="0"
OS_FAMILY="unsupported"
PKG_MANAGER=""

info() {
    echo "[INFO] $*"
}

error() {
    echo "[ERROR] $*" >&2
}

run_logged() {
    local description="$1"
    shift

    info "$description"
    {
        echo
        echo "===== $(date '+%Y-%m-%d %H:%M:%S') | ${description} ====="
        "$@"
    } 2>&1 | tee -a "$LOG_FILE"
}

run_logged_allow_failure() {
    local description="$1"
    shift

    info "$description"
    {
        echo
        echo "===== $(date '+%Y-%m-%d %H:%M:%S') | ${description} ====="
        "$@" || true
    } 2>&1 | tee -a "$LOG_FILE"
}

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        error "Run this script as root: sudo bash $0"
        exit 1
    fi
}

prepare_log_file() {
    if [[ ! -d "$LOG_DIR" ]]; then
        mkdir -p "$LOG_DIR"
    fi

    if [[ ! -f "$LOG_FILE" ]]; then
        touch "$LOG_FILE"
    fi
}

read_os_release_value() {
    local key="$1"

    awk -F '=' -v key="$key" '
        $1 == key {
            value = $2
            for (i = 3; i <= NF; i++) {
                value = value "=" $i
            }
            gsub(/^"/, "", value)
            gsub(/"$/, "", value)
            print value
            exit
        }
    ' /etc/os-release
}

split_version() {
    local version="$1"

    OS_MAJOR="${version%%.*}"
    if [[ "$version" == *.* ]]; then
        OS_MINOR="${version#*.}"
        OS_MINOR="${OS_MINOR%%.*}"
    else
        OS_MINOR="0"
    fi
}

detect_os() {
    info "Detecting OS"

    if [[ -r /etc/SuSE-release ]]; then
        OS_NAME="sles"
        OS_FAMILY="sles"
        OS_MAJOR="$(awk '/^VERSION/ { print $3; exit }' /etc/SuSE-release)"
        OS_MINOR="$(awk '/^PATCHLEVEL/ { print $3; exit }' /etc/SuSE-release)"
        OS_VERSION="${OS_MAJOR}.${OS_MINOR}"
    elif [[ -r /etc/redhat-release && ! -r /etc/os-release ]]; then
        OS_NAME="rhel"
        OS_FAMILY="rhel"
        OS_VERSION="$(awk -F 'release' '{ print $2 }' /etc/redhat-release | awk '{ print $1; exit }')"
        split_version "$OS_VERSION"
    elif [[ -r /etc/os-release ]]; then
        OS_NAME="$(read_os_release_value ID | tr '[:upper:]' '[:lower:]')"
        OS_VERSION="$(read_os_release_value VERSION_ID)"
        split_version "$OS_VERSION"

        case "$OS_NAME" in
            sles|sled|opensuse-leap)
                OS_FAMILY="sles"
                ;;
            rhel|centos|rocky|almalinux|ol)
                OS_FAMILY="rhel"
                ;;
            fedora)
                OS_FAMILY="fedora"
                ;;
            ubuntu|debian)
                OS_FAMILY="debian"
                ;;
            *)
                OS_FAMILY="unsupported"
                ;;
        esac
    fi

    info "Detected: ${OS_NAME} ${OS_MAJOR}.${OS_MINOR} (${OS_FAMILY})"
}

validate_supported_os() {
    case "$OS_FAMILY" in
        sles)
            case "$OS_MAJOR" in
                12|15) PKG_MANAGER="zypper" ;;
                *)
                    error "Unsupported SLES version: ${OS_MAJOR}.${OS_MINOR}"
                    exit 1
                    ;;
            esac
            ;;
        rhel)
            case "$OS_MAJOR" in
                7) PKG_MANAGER="yum" ;;
                8|9|10) PKG_MANAGER="dnf" ;;
                *)
                    error "Unsupported RHEL-compatible version: ${OS_MAJOR}.${OS_MINOR}"
                    exit 1
                    ;;
            esac
            ;;
        fedora)
            PKG_MANAGER="dnf"
            ;;
        debian)
            case "${OS_NAME}:${OS_MAJOR}" in
                ubuntu:20|ubuntu:22|ubuntu:24|debian:11|debian:12)
                    PKG_MANAGER="apt"
                    ;;
                *)
                    error "Unsupported Debian-family version: ${OS_NAME} ${OS_MAJOR}.${OS_MINOR}"
                    exit 1
                    ;;
            esac
            ;;
        *)
            error "Unsupported OS: ${OS_NAME} ${OS_MAJOR}.${OS_MINOR}"
            exit 1
            ;;
    esac

    if ! command -v "$PKG_MANAGER" >/dev/null 2>&1; then
        error "Package manager not found: ${PKG_MANAGER}"
        exit 1
    fi

    info "Supported OS. Package manager: ${PKG_MANAGER}"
}

print_repository_list() {
    case "$PKG_MANAGER" in
        zypper)
            run_logged "Repository list" zypper repos --uri
            ;;
        yum|dnf)
            run_logged "Repository list" "$PKG_MANAGER" repolist all
            ;;
        apt)
            run_logged "Repository list" apt-cache policy
            ;;
    esac
}

stop_applications() {
    if [[ -z "$APPS_TO_STOP" ]]; then
        info "No applications configured to stop. Add service names to STOP_APPLICATIONS in this script."
        return
    fi

    info "Applications configured to stop: ${APPS_TO_STOP}"

    local app
    for app in $APPS_TO_STOP; do
        if systemctl list-unit-files "${app}.service" >/dev/null 2>&1; then
            run_logged "Stopping application: ${app}" systemctl stop "$app"
        else
            info "Service not found, skipping stop: ${app}"
        fi
    done
}

print_available_updates() {
    case "$PKG_MANAGER" in
        zypper)
            run_logged_allow_failure "Available updates" zypper list-updates
            ;;
        yum|dnf)
            run_logged_allow_failure "Available updates" "$PKG_MANAGER" check-update
            ;;
        apt)
            run_logged "Refreshing apt metadata" apt-get update
            run_logged_allow_failure "Available updates" apt list --upgradable
            ;;
    esac
}

update_system() {
    case "$PKG_MANAGER" in
        zypper)
            run_logged "Updating system" zypper --non-interactive update
            ;;
        yum)
            run_logged "Updating system" yum -y update
            ;;
        dnf)
            run_logged "Updating system" dnf -y upgrade
            ;;
        apt)
            run_logged "Updating system" apt-get -y upgrade
            ;;
    esac
}

main() {
    require_root
    prepare_log_file

    info "Logging to ${LOG_FILE}"
    detect_os
    validate_supported_os
    print_repository_list
    stop_applications
    print_available_updates
    update_system

    info "Patching completed. Log file: ${LOG_FILE}"
}

main "$@"
