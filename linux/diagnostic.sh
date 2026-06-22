#!/bin/bash


DATE=$(which date)
FREE=$(which free)
UNAME=$(which uname)
LSB_RELEASE=$(which lsb_release)
GREP=$(which grep)


function print_memory_usage() {
    info "Printing memory usage"
    free -h
}

function print_timezone_and_date() {
    info "Printing timezone and date"
    date
    timedatectl
}

function print_dns() {
    info "Printing DNS configuration"
    if [[ -r /etc/resolv.conf ]]; then
        grep -vE '^[[:space:]]*#|^[[:space:]]*$' /etc/resolv.conf
    else
        echo "Cannot read /etc/resolv.conf"
    fi
}

function print_system_info() {
    info "Printing system information"
    uname -a
    lsb_release -a 2>/dev/null || cat /etc/*release 2>/dev/null
    echo "Kernel version: $(uname -r)"
    echo "Architecture: $(uname -m)"
    echo "CPU info:"
    lscpu
    echo "Memory info:"
    free -h
}

function print_hostname() {
    cat /etc/hostname
}

function print_repository_list() {
    info "Printing repository list"

    if [[ "$PACKAGE_MANAGER" == "zypper" ]]; then
        zypper lr -u
    elif [[ "$PACKAGE_MANAGER" == "yum" ]]; then
        yum repolist all
    elif [[ "$PACKAGE_MANAGER" == "dnf" ]]; then
        dnf repolist all
    elif [[ "$PACKAGE_MANAGER" == "apt" ]]; then
        apt-cache policy
    else
        error "Unsupported package manager: ${PACKAGE_MANAGER}"
        exit 1
    fi

}


function main() {
    detect_os_and_package_manager
    print_memory_usage
    print_timezone_and_date
    print_dns
    print_system_info
    print_hostname
    print_repository_list
    
}


