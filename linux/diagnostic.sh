#!/bin/bash


DATE=$(which date)
FREE=$(which free)
UNAME=$(which uname)
LSB_RELEASE=$(which lsb_release)
GREP=$(which grep)

if [[ -r /etc/SuSE-release ]]
# /etc/os-release does not exist on SLES11 and RHEL6
# so OS specific release files need to be used
then
    OSNAME=sles
    OSVERS=$(awk '/^VERSION/ { printf("%s", $3) }' /etc/SuSE-release)
    OS_SP=$(awk '/^PATCHLEVEL/ { printf("%d\n", $3) }' /etc/SuSE-release)
    ZYPPER=$(which zypper)
elif [[ -r /etc/redhat-release ]]
then
    OSNAME=rhel
    OSVERS=$(awk -F 'release' '{print $2}' /etc/redhat-release | awk '{print $1}' | awk -F '.' '{print $1}')
    OS_SP=$(awk -F 'release' '{print $2}' /etc/redhat-release | awk '{print $1}' | awk -F '.' '{print $2}')
    YUM=$(which yum)
    DNF=$(which dnf)
    
elif [[ -r /etc/os-release ]]
then
    OSNAME=$(awk -F '=' '/^ID=/ { print tolower($2) }' /etc/os-release | sed 's/"//g')
    OSVERS="$(grep '^\(VERSION_ID\)' /etc/os-release | sed -n 's/.*"\([[:digit:]]\+\).*/\1/p')"
    OS_SP="$(grep '^\(VERSION_ID\)' /etc/os-release | sed -n 's/.*\.\([[:digit:]]\+\).*/\1/p')"
    if [[ "$OSNAME" == "ubuntu" || "$OSNAME" == "debian" ]]; then
        APT=$(which apt)
    fi
else
    OSNAME="unsupported_os"
    OSVERS="unknown_ver"
    OS_SP="unknown_sp"
fi

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

function print_locked_packages() {
    info "Printing locked packages"

    if [[ "$PACKAGE_MANAGER" == "zypper" ]]; then
        zypper locks
    elif [[ "$PACKAGE_MANAGER" == "yum" ]]; then
        yum versionlock list
    elif [[ "$PACKAGE_MANAGER" == "dnf" ]]; then
        dnf versionlock list
    elif [[ "$PACKAGE_MANAGER" == "apt" ]]; then
        apt-mark showhold
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
    print_locked_packages
}

main

if you type 1 run 