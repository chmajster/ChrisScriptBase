#!/bin/bash
# Repository: [chmajster/ChrisScriptBase](https://github.com/chmajster/ChrisScriptBase)

echo "Detecting OS"

if [[ -r /etc/SuSE-release ]]
# /etc/os-release does not exist on SLES11 and RHEL6
# so OS specific release files need to be used
then
    OSNAME=sles
    OSVERS=$(awk '/^VERSION/ { printf("%s", $3) }' /etc/SuSE-release)
    OS_SP=$(awk '/^PATCHLEVEL/ { printf("%d\n", $3) }' /etc/SuSE-release)

elif [[ -r /etc/redhat-release ]]
then
    OSNAME=rhel
    OSVERS=$(awk -F 'release' '{print $2}' /etc/redhat-release | awk '{print $1}' | awk -F '.' '{print $1}')
    OS_SP=$(awk -F 'release' '{print $2}' /etc/redhat-release | awk '{print $1}' | awk -F '.' '{print $2}')

elif [[ -r /etc/os-release ]]
then
    OSNAME=$(awk -F '=' '/^ID=/ { print tolower($2) }' /etc/os-release | sed 's/"//g')
    OSVERS="$(grep '^\(VERSION_ID\)' /etc/os-release | sed -n 's/.*"\([[:digit:]]\+\).*/\1/p')"
    OS_SP="$(grep '^\(VERSION_ID\)' /etc/os-release | sed -n 's/.*\.\([[:digit:]]\+\).*/\1/p')"


else
    OSNAME="unsupported_os"
    OSVERS="unknown_ver"
    OS_SP="unknown_sp"
fi

echo "Detected: $OSNAME $OSVERS.$OS_SP"

case "$OSNAME" in
    sles)
        case $OSVERS in
            11)
                echo "$OSNAME $OSVERS no longer supported, aborting."
                exit 1
                ;;
            12|15)
                echo "Supported SLES ver: $OSNAME $OSVERS.$OS_SP, continue"
                exit 0
                ;;
            *)
                echo "Unsupported SLES version: $OSNAME $OSVERS, aborting."
                exit 1
                ;;
        esac
        ;;
    rhel)
        case $OSVERS in
            8|9)
                echo "Supported RHEL ver: $OSNAME $OSVERS.$OS_SP, continue"
                exit 0
                ;;
            *)
                echo "Unsupported RHEL version: $OSNAME $OSVERS, aborting." 
                exit 1
                ;;
        esac
        ;;
    *)
        echo "Unsupported OS: $OSNAME, aborting."
        exit 1
        ;;
esac
    
