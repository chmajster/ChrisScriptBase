#!/usr/bin/env bash
# LDAP/Active Directory user lookup utility.
# Discovers LDAP settings from SSSD/OpenLDAP/nslcd configuration and falls back to NSS/SSSD.
set -uo pipefail

VERSION="1.0.1"
LDAPSEARCH_ETC="${LDAPSEARCH_ETC:-/etc}"
LDAP_TIMEOUT="${LDAP_TIMEOUT:-8}"
DEBUG=0
MODE="full"
USER_QUERY=""
CONFIG_SOURCE=""
SSSD_ENABLED="no"
SSSD_DOMAIN=""
ID_PROVIDER=""
LDAP_SCHEMA=""
LDAP_ID_MAPPING=""
AD_DOMAIN=""
AD_SERVER=""
LDAP_SEARCH_BASE=""
LDAP_USER_BASE=""
LDAP_GROUP_BASE=""
LDAP_NETGROUP_BASE=""
LDAP_BIND_DN=""
LDAP_BIND_PASSWORD=""
AUTH_METHOD="anonymous"
ACTIVE_URI=""
PASSWORD_FILE=""
declare -a LDAP_URIS=()
declare -a TMP_FILES=()

cleanup() {
    local file
    for file in "${TMP_FILES[@]:-}"; do
        if [[ -n "$file" && -e "$file" ]]; then
            rm -f -- "$file"
        fi
    done
}
trap cleanup EXIT
trap 'cleanup; exit 129' HUP
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

init_colors() {
    if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
        C_RED=$'\033[31m'
        C_GREEN=$'\033[32m'
        C_YELLOW=$'\033[33m'
        C_CYAN=$'\033[1;36m'
        C_BOLD=$'\033[1m'
        C_RESET=$'\033[0m'
    else
        C_RED=""
        C_GREEN=""
        C_YELLOW=""
        C_CYAN=""
        C_BOLD=""
        C_RESET=""
    fi
}
init_colors

debug() {
    (( DEBUG )) || return 0
    printf '%sDEBUG:%s %s\n' "$C_CYAN" "$C_RESET" "$*" >&2
}

warn() {
    printf '%sWARNING:%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2
}

error() {
    printf '%sERROR:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2
}

die() {
    error "$*"
    exit 1
}

trim() {
    local value="${1-}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

config_file() {
    printf '%s/%s' "${LDAPSEARCH_ETC%/}" "${1#/}"
}

read_ini_value() {
    local file="$1"
    local section="$2"
    local key="$3"
    [[ -r "$file" ]] || return 1

    awk -v want_section="$section" -v want_key="$key" '
        function trim_value(s) {
            sub(/^[[:space:]]+/, "", s)
            sub(/[[:space:]]+$/, "", s)
            return s
        }
        /^[[:space:]]*[#;]/ { next }
        /^[[:space:]]*\[/ {
            sec=$0
            sub(/^[[:space:]]*\[/, "", sec)
            sub(/\][[:space:]]*$/, "", sec)
            sec=trim_value(sec)
            next
        }
        {
            pos=index($0, "=")
            if (!pos || sec != want_section) next
            current_key=trim_value(substr($0, 1, pos - 1))
            if (current_key != want_key) next
            value=trim_value(substr($0, pos + 1))
            sub(/[[:space:]]+[;#].*$/, "", value)
            print value
            exit
        }
    ' "$file"
}

first_sssd_domain() {
    local file="$1"
    local configured
    configured="$(read_ini_value "$file" sssd domains 2>/dev/null || true)"
    if [[ -n "$configured" ]]; then
        configured="${configured%%,*}"
        trim "$configured"
        return 0
    fi

    awk '
        /^[[:space:]]*\[domain\/[^]]+\][[:space:]]*$/ {
            section=$0
            sub(/^[[:space:]]*\[domain\//, "", section)
            sub(/\][[:space:]]*$/, "", section)
            print section
            exit
        }
    ' "$file"
}

add_uris() {
    local raw="${1-}"
    local token
    local existing
    local duplicate
    local -a tokens=()
    [[ -n "$raw" ]] || return 0

    raw="${raw//,/ }"
    read -r -a tokens <<<"$raw"
    for token in "${tokens[@]}"; do
        [[ "$token" =~ ^ldaps?:// ]] || continue
        duplicate=0
        for existing in "${LDAP_URIS[@]:-}"; do
            if [[ "$existing" == "$token" ]]; then
                duplicate=1
                break
            fi
        done
        if (( ! duplicate )); then
            LDAP_URIS+=("$token")
        fi
    done
}

read_simple_conf_value() {
    local file="$1"
    local key="$2"
    [[ -r "$file" ]] || return 1

    awk -v want="$key" '
        function trim_value(s) {
            sub(/^[[:space:]]+/, "", s)
            sub(/[[:space:]]+$/, "", s)
            return s
        }
        /^[[:space:]]*#/ { next }
        {
            line=$0
            sub(/[[:space:]]+#.*$/, "", line)
            line=trim_value(line)
            split(line, fields, /[[:space:]]+/)
            if (tolower(fields[1]) == tolower(want)) {
                sub(/^[^[:space:]]+[[:space:]]+/, "", line)
                print trim_value(line)
                exit
            }
        }
    ' "$file"
}

detect_sssd() {
    local file
    local section
    local ad_hosts
    local host
    local part
    local derived=""
    local -a ad_host_list=()
    local -a ad_parts=()

    file="$(config_file sssd/sssd.conf)"
    [[ -r "$file" ]] || return 1

    SSSD_DOMAIN="$(first_sssd_domain "$file" || true)"
    [[ -n "$SSSD_DOMAIN" ]] || return 1
    section="domain/$SSSD_DOMAIN"

    ID_PROVIDER="$(read_ini_value "$file" "$section" id_provider 2>/dev/null || true)"
    AD_DOMAIN="$(read_ini_value "$file" "$section" ad_domain 2>/dev/null || true)"
    AD_SERVER="$(read_ini_value "$file" "$section" ad_server 2>/dev/null || true)"
    LDAP_SCHEMA="$(read_ini_value "$file" "$section" ldap_schema 2>/dev/null || true)"
    LDAP_ID_MAPPING="$(read_ini_value "$file" "$section" ldap_id_mapping 2>/dev/null || true)"
    LDAP_SEARCH_BASE="$(read_ini_value "$file" "$section" ldap_search_base 2>/dev/null || true)"
    LDAP_USER_BASE="$(read_ini_value "$file" "$section" ldap_user_search_base 2>/dev/null || true)"
    LDAP_GROUP_BASE="$(read_ini_value "$file" "$section" ldap_group_search_base 2>/dev/null || true)"
    LDAP_NETGROUP_BASE="$(read_ini_value "$file" "$section" ldap_netgroup_search_base 2>/dev/null || true)"
    LDAP_BIND_DN="$(read_ini_value "$file" "$section" ldap_default_bind_dn 2>/dev/null || true)"
    LDAP_BIND_PASSWORD="$(read_ini_value "$file" "$section" ldap_default_authtok 2>/dev/null || true)"
    add_uris "$(read_ini_value "$file" "$section" ldap_uri 2>/dev/null || true)"

    if [[ ${#LDAP_URIS[@]} -eq 0 && -n "$AD_SERVER" ]]; then
        ad_hosts="${AD_SERVER//,/ }"
        read -r -a ad_host_list <<<"$ad_hosts"
        for host in "${ad_host_list[@]}"; do
            host="$(trim "$host")"
            if [[ -n "$host" && "$host" != _srv_ ]]; then
                add_uris "ldaps://$host"
            fi
        done
    fi

    if [[ -z "$LDAP_SEARCH_BASE" && -n "$AD_DOMAIN" && "$AD_DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
        IFS='.' read -r -a ad_parts <<<"$AD_DOMAIN"
        for part in "${ad_parts[@]}"; do
            [[ -n "$part" ]] || continue
            [[ -n "$derived" ]] && derived+=","
            derived+="dc=$part"
        done
        LDAP_SEARCH_BASE="$derived"
    fi

    [[ -n "$LDAP_USER_BASE" ]] || LDAP_USER_BASE="$LDAP_SEARCH_BASE"
    [[ -n "$LDAP_GROUP_BASE" ]] || LDAP_GROUP_BASE="$LDAP_SEARCH_BASE"
    [[ -n "$LDAP_NETGROUP_BASE" ]] || LDAP_NETGROUP_BASE="$LDAP_SEARCH_BASE"
    CONFIG_SOURCE="SSSD"
    SSSD_ENABLED="yes"
    return 0
}

detect_openldap() {
    local file
    local value
    for file in "$(config_file openldap/ldap.conf)" "$(config_file ldap/ldap.conf)"; do
        [[ -r "$file" ]] || continue
        value="$(read_simple_conf_value "$file" URI 2>/dev/null || true)"
        add_uris "$value"
        [[ -n "$LDAP_SEARCH_BASE" ]] || LDAP_SEARCH_BASE="$(read_simple_conf_value "$file" BASE 2>/dev/null || true)"
        [[ -n "$CONFIG_SOURCE" ]] || CONFIG_SOURCE="OpenLDAP"
    done

    [[ -n "$LDAP_USER_BASE" ]] || LDAP_USER_BASE="$LDAP_SEARCH_BASE"
    [[ -n "$LDAP_GROUP_BASE" ]] || LDAP_GROUP_BASE="$LDAP_SEARCH_BASE"
    [[ -n "$LDAP_NETGROUP_BASE" ]] || LDAP_NETGROUP_BASE="$LDAP_SEARCH_BASE"
    [[ ${#LDAP_URIS[@]} -gt 0 || -n "$LDAP_SEARCH_BASE" ]]
}

detect_nslcd() {
    local file
    for file in "$(config_file nslcd.conf)" "$(config_file ldap.conf)"; do
        [[ -r "$file" ]] || continue
        add_uris "$(read_simple_conf_value "$file" uri 2>/dev/null || true)"
        [[ -n "$LDAP_SEARCH_BASE" ]] || LDAP_SEARCH_BASE="$(read_simple_conf_value "$file" base 2>/dev/null || true)"
        [[ -n "$LDAP_BIND_DN" ]] || LDAP_BIND_DN="$(read_simple_conf_value "$file" binddn 2>/dev/null || true)"
        [[ -n "$LDAP_BIND_PASSWORD" ]] || LDAP_BIND_PASSWORD="$(read_simple_conf_value "$file" bindpw 2>/dev/null || true)"
        [[ -n "$CONFIG_SOURCE" ]] || CONFIG_SOURCE="nslcd/ldap.conf"
    done

    [[ -n "$LDAP_USER_BASE" ]] || LDAP_USER_BASE="$LDAP_SEARCH_BASE"
    [[ -n "$LDAP_GROUP_BASE" ]] || LDAP_GROUP_BASE="$LDAP_SEARCH_BASE"
    [[ -n "$LDAP_NETGROUP_BASE" ]] || LDAP_NETGROUP_BASE="$LDAP_SEARCH_BASE"
    [[ ${#LDAP_URIS[@]} -gt 0 || -n "$LDAP_SEARCH_BASE" ]]
}

detect_config() {
    local source_before_fallback
    LDAP_URIS=()
    CONFIG_SOURCE=""
    SSSD_ENABLED="no"

    if detect_sssd; then
        if [[ ${#LDAP_URIS[@]} -eq 0 || -z "$LDAP_SEARCH_BASE" ]]; then
            source_before_fallback="$CONFIG_SOURCE"
            detect_openldap || true
            CONFIG_SOURCE="$source_before_fallback"
        fi
    else
        detect_openldap || true
        detect_nslcd || true
    fi

    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet sssd 2>/dev/null; then
        SSSD_ENABLED="yes"
    fi

    debug "config source=${CONFIG_SOURCE:-none} domain=${SSSD_DOMAIN:-none} uris=${LDAP_URIS[*]:-none}"
}

detect_auth_method() {
    if command -v klist >/dev/null 2>&1 && klist -s >/dev/null 2>&1; then
        AUTH_METHOD="GSSAPI"
    else
        AUTH_METHOD="anonymous"
    fi
}

ldap_escape_filter() {
    local value="${1-}"
    value="${value//\\/\\5c}"
    value="${value//\*/\\2a}"
    value="${value//\(/\\28}"
    value="${value//\)/\\29}"
    printf '%s' "$value"
}

ldif_attr() {
    local ldif="$1"
    local attr="$2"
    awk -v wanted="$attr" '
        {
            pos=index($0, ":")
            if (!pos) next
            key=substr($0, 1, pos - 1)
            if (tolower(key) == tolower(wanted)) {
                value=substr($0, pos + 1)
                sub(/^:? /, "", value)
                print value
                exit
            }
        }
    ' <<<"$ldif"
}

ldif_attrs() {
    local ldif="$1"
    local attr="$2"
    awk -v wanted="$attr" '
        {
            pos=index($0, ":")
            if (!pos) next
            key=substr($0, 1, pos - 1)
            if (tolower(key) == tolower(wanted)) {
                value=substr($0, pos + 1)
                sub(/^:? /, "", value)
                print value
            }
        }
    ' <<<"$ldif"
}

filter_ldif_secrets() {
    awk '
        BEGIN { skip=0 }
        /^[[:space:]]/ {
            if (skip) next
            print
            next
        }
        {
            skip=0
            pos=index($0, ":")
            if (!pos) { print; next }
            key=tolower(substr($0, 1, pos - 1))
            if (key ~ /(password|passwd|unicodepwd|krbprincipalkey|authtok|secret|token|privatekey|credential)/) {
                skip=1
                next
            }
            print
        }
    '
}

command_timeout() {
    if command -v timeout >/dev/null 2>&1; then
        timeout --preserve-status "${LDAP_TIMEOUT}s" "$@"
    else
        "$@"
    fi
}

make_password_file() {
    PASSWORD_FILE="$(mktemp)"
    chmod 600 "$PASSWORD_FILE"
    printf '%s' "$LDAP_BIND_PASSWORD" >"$PASSWORD_FILE"
    TMP_FILES+=("$PASSWORD_FILE")
}

ldapsearch_once() {
    local uri="$1"
    local base="$2"
    local scope="$3"
    local filter="$4"
    local auth="$5"
    local pwfile=""
    local rc=0
    shift 5
    local -a args=(-LLL -o ldif-wrap=no -o "nettimeout=${LDAP_TIMEOUT}" -H "$uri" -b "$base" -s "$scope")

    case "$auth" in
        GSSAPI)
            args+=(-Y GSSAPI)
            ;;
        anonymous)
            args+=(-x)
            ;;
        simple-bind)
            [[ -n "$LDAP_BIND_DN" && -n "$LDAP_BIND_PASSWORD" ]] || return 1
            make_password_file
            pwfile="$PASSWORD_FILE"
            args+=(-x -D "$LDAP_BIND_DN" -y "$pwfile")
            ;;
        *)
            return 1
            ;;
    esac

    args+=("$filter")
    if (($#)); then
        args+=("$@")
    fi

    command_timeout ldapsearch "${args[@]}" || rc=$?
    if [[ -n "$pwfile" ]]; then
        rm -f -- "$pwfile"
    fi
    return "$rc"
}

ldap_query() {
    local base="$1"
    local filter="$2"
    local scope="${3:-sub}"
    local uri
    local output
    local auth
    local rc
    shift 3
    local -a methods=()

    [[ ${#LDAP_URIS[@]} -gt 0 ]] || return 2
    [[ -n "$base" || "$scope" == base ]] || return 2

    if [[ "$AUTH_METHOD" == GSSAPI ]]; then
        methods=(GSSAPI anonymous)
    else
        methods=(anonymous)
    fi
    if [[ -n "$LDAP_BIND_DN" && -n "$LDAP_BIND_PASSWORD" ]]; then
        methods+=(simple-bind)
    fi

    for uri in "${LDAP_URIS[@]}"; do
        for auth in "${methods[@]}"; do
            debug "LDAP query uri=$uri base=$base scope=$scope auth=$auth filter=$filter"
            output="$(ldapsearch_once "$uri" "$base" "$scope" "$filter" "$auth" "$@" 2>/dev/null)"
            rc=$?
            if (( rc == 0 )); then
                ACTIVE_URI="$uri"
                AUTH_METHOD="$auth"
                printf '%s\n' "$output"
                return 0
            fi
            debug "LDAP query failed rc=$rc uri=$uri auth=$auth"
        done
    done
    return 1
}

require_ldapsearch_or_nss() {
    if command -v ldapsearch >/dev/null 2>&1; then
        return 0
    fi
    if command -v getent >/dev/null 2>&1; then
        warn "ldapsearch command not found; direct LDAP queries disabled, using NSS/SSSD fallback."
        return 0
    fi

    cat >&2 <<'EOF_DEPS'
ERROR: ldapsearch command not found.

Debian/Ubuntu:
  apt install ldap-utils

RHEL/Rocky/Alma:
  dnf install openldap-clients

SUSE:
  zypper install openldap2-client
EOF_DEPS
    return 1
}

search_user_ldap() {
    local query="$1"
    local escaped
    local exact_filter
    local broad_filter
    local output
    local -a attrs=(
        uid uidNumber gidNumber cn sn givenName displayName mail
        employeeNumber employeeID department departmentNumber title company
        manager homeDirectory loginShell gecos memberOf objectClass
        userPrincipalName sAMAccountName accountStatus shadowExpire
        pwdAccountLockedTime userAccountControl
    )

    escaped="$(ldap_escape_filter "$query")"
    exact_filter="(|(uid=${escaped})(sAMAccountName=${escaped})(userPrincipalName=${escaped}))"
    broad_filter="(|(uid=${escaped})(sAMAccountName=${escaped})(userPrincipalName=${escaped}*)(cn=*${escaped}*)(mail=${escaped})(displayName=*${escaped}*))"

    output="$(ldap_query "$LDAP_USER_BASE" "$exact_filter" sub "${attrs[@]}" || true)"
    if grep -qi '^dn:' <<<"$output"; then
        printf '%s\n' "$output"
        return 0
    fi

    output="$(ldap_query "$LDAP_USER_BASE" "$broad_filter" sub "${attrs[@]}" || true)"
    grep -qi '^dn:' <<<"$output" || return 1
    printf '%s\n' "$output"
}

nss_user_ldif() {
    local user="$1"
    local line
    local name
    local uid
    local gid
    local home
    local shell
    local gecos

    command -v getent >/dev/null 2>&1 || return 1
    line="$(getent passwd "$user" 2>/dev/null || true)"
    [[ -n "$line" ]] || return 1
    IFS=: read -r name _ uid gid gecos home shell <<<"$line"

    printf 'dn: nss:%s\nuid: %s\nuidNumber: %s\ngidNumber: %s\ncn: %s\nhomeDirectory: %s\nloginShell: %s\nsource: NSS/SSSD\n' \
        "$name" "$name" "$uid" "$gid" "${gecos%%,*}" "$home" "$shell"
}

split_ldif_entries() {
    awk 'BEGIN { RS=""; ORS="\0" } /(^|\n)dn:[[:space:]]/ { print }'
}

group_name_from_dn() {
    local dn="$1"
    local first
    first="${dn%%,*}"
    if [[ "$first" =~ ^[Cc][Nn]= ]]; then
        printf '%s' "${first#*=}"
    else
        printf '%s' "$dn"
    fi
}

get_user_groups_ldap() {
    local user_ldif="$1"
    local dn
    local uid
    local escaped_dn
    local escaped_uid
    local result
    local line

    dn="$(ldif_attr "$user_ldif" dn)"
    uid="$(ldif_attr "$user_ldif" uid)"
    [[ -n "$uid" ]] || uid="$(ldif_attr "$user_ldif" sAMAccountName)"

    {
        while IFS= read -r line; do
            if [[ -n "$line" ]]; then
                group_name_from_dn "$line"
                printf '\n'
            fi
        done < <(ldif_attrs "$user_ldif" memberOf)

        if [[ -n "$LDAP_GROUP_BASE" && -n "$dn" && "$dn" != nss:* ]]; then
            escaped_dn="$(ldap_escape_filter "$dn")"
            escaped_uid="$(ldap_escape_filter "$uid")"
            result="$(ldap_query "$LDAP_GROUP_BASE" "(|(member=${escaped_dn})(uniqueMember=${escaped_dn})(memberUid=${escaped_uid}))" sub cn || true)"
            ldif_attrs "$result" cn
        fi
    } | awk 'NF && !seen[$0]++' | sort
}

get_user_groups_nss() {
    command -v id >/dev/null 2>&1 || return 0
    id -nG "$1" 2>/dev/null | tr ' ' '\n' | awk 'NF && !seen[$0]++' | sort || true
}

netgroup_user_match() {
    local triple="$1"
    local user="$2"
    local inner
    local ng_user

    triple="$(trim "$triple")"
    [[ "$triple" == \(*\) ]] || return 1
    inner="${triple#(}"
    inner="${inner%)}"
    IFS=, read -r _ ng_user _ <<<"$inner"
    [[ "$(trim "$ng_user")" == "$user" ]]
}

get_user_netgroups_ldap() {
    local user="$1"
    local result
    local entry
    local name
    local triple
    local child
    local changed
    local parent

    [[ -n "$LDAP_NETGROUP_BASE" ]] || return 0
    result="$(ldap_query "$LDAP_NETGROUP_BASE" '(objectClass=nisNetgroup)' sub cn nisNetgroupTriple memberNisNetgroup nisNetgroupMember || true)"
    [[ -n "$result" ]] || return 0

    declare -A contains_user=()
    declare -A children=()

    while IFS= read -r -d '' entry; do
        name="$(ldif_attr "$entry" cn)"
        [[ -n "$name" ]] || continue

        while IFS= read -r triple; do
            if netgroup_user_match "$triple" "$user"; then
                contains_user["$name"]=1
            fi
        done < <(ldif_attrs "$entry" nisNetgroupTriple)

        while IFS= read -r child; do
            if [[ -n "$child" ]]; then
                children["$name"]+="${child}"$'\n'
            fi
        done < <(ldif_attrs "$entry" memberNisNetgroup)

        while IFS= read -r child; do
            if [[ "$(trim "$child")" == "$user" ]]; then
                contains_user["$name"]=1
            fi
        done < <(ldif_attrs "$entry" nisNetgroupMember)
    done < <(printf '%s\n' "$result" | split_ldif_entries)

    # Fixed-point expansion handles nested groups and naturally terminates on cycles.
    changed=1
    while (( changed )); do
        changed=0
        for parent in "${!children[@]}"; do
            [[ -n "${contains_user[$parent]:-}" ]] && continue
            while IFS= read -r child; do
                [[ -n "$child" ]] || continue
                if [[ -n "${contains_user[$child]:-}" ]]; then
                    contains_user["$parent"]=1
                    changed=1
                    break
                fi
            done <<<"${children[$parent]}"
        done
    done

    printf '%s\n' "${!contains_user[@]}" | awk 'NF' | sort
}

get_user_netgroups_nss() {
    local user="$1"
    local line
    local name
    local rest
    local triple

    command -v getent >/dev/null 2>&1 || return 0
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        name="${line%%[[:space:]]*}"
        rest="${line#"$name"}"
        while IFS= read -r triple; do
            if netgroup_user_match "$triple" "$user"; then
                printf '%s\n' "$name"
                break
            fi
        done < <(grep -o '([^)]*)' <<<"$rest" || true)
    done < <(getent netgroup 2>/dev/null || true)
}

merge_unique_lines() {
    awk 'NF && !seen[$0]++' | sort
}

print_kv() {
    [[ -n "${2-}" ]] || return 0
    printf '%-20s %s\n' "${1}:" "$2"
}

display_name() {
    local ldif="$1"
    local value
    local given
    local surname

    value="$(ldif_attr "$ldif" displayName)"
    if [[ -n "$value" ]]; then
        printf '%s' "$value"
        return 0
    fi
    value="$(ldif_attr "$ldif" cn)"
    if [[ -n "$value" ]]; then
        printf '%s' "$value"
        return 0
    fi
    given="$(ldif_attr "$ldif" givenName)"
    surname="$(ldif_attr "$ldif" sn)"
    trim "$given $surname"
}

account_status() {
    local ldif="$1"
    local status
    local lock
    local uac
    local shadow
    local today_days

    status="$(ldif_attr "$ldif" accountStatus)"
    if [[ -n "$status" ]]; then
        printf '%s' "$status"
        return 0
    fi

    lock="$(ldif_attr "$ldif" pwdAccountLockedTime)"
    if [[ -n "$lock" ]]; then
        printf 'Locked'
        return 0
    fi

    uac="$(ldif_attr "$ldif" userAccountControl)"
    if [[ "$uac" =~ ^[0-9]+$ ]] && (( (uac & 2) != 0 )); then
        printf 'Disabled'
        return 0
    fi

    shadow="$(ldif_attr "$ldif" shadowExpire)"
    if [[ "$shadow" =~ ^[0-9]+$ && "$shadow" -gt 0 ]]; then
        today_days=$(( $(date +%s) / 86400 ))
        if (( shadow < today_days )); then
            printf 'Expired'
            return 0
        fi
    fi

    printf 'Active/unknown'
}

print_user() {
    local ldif="$1"
    local query="$2"
    local uid
    local name
    local groups_ldap
    local groups_nss
    local netgroups_ldap
    local netgroups_nss
    local source

    uid="$(ldif_attr "$ldif" uid)"
    [[ -n "$uid" ]] || uid="$(ldif_attr "$ldif" sAMAccountName)"
    [[ -n "$uid" ]] || uid="$query"
    name="$(display_name "$ldif")"
    source="$(ldif_attr "$ldif" source)"
    [[ -n "$source" ]] || source="LDAP"

    groups_ldap="$(get_user_groups_ldap "$ldif" || true)"
    groups_nss="$(get_user_groups_nss "$uid" || true)"
    netgroups_ldap="$(get_user_netgroups_ldap "$uid" || true)"
    netgroups_nss="$(get_user_netgroups_nss "$uid" || true)"

    printf '%sLDAP USER INFORMATION%s\n%s\n' "$C_GREEN" "$C_RESET" '============================================================'
    print_kv Source "$source"
    print_kv 'LDAP server' "${ACTIVE_URI:-${LDAP_URIS[0]:-}}"
    print_kv 'Search base' "$LDAP_SEARCH_BASE"
    print_kv Authentication "$AUTH_METHOD"

    printf '\n%sIdentity%s\n%s\n' "$C_CYAN" "$C_RESET" '------------------------------------------------------------'
    print_kv Login "$uid"
    print_kv Name "$name"
    print_kv UID "$(ldif_attr "$ldif" uid)"
    print_kv 'UID Number' "$(ldif_attr "$ldif" uidNumber)"
    print_kv 'GID Number' "$(ldif_attr "$ldif" gidNumber)"
    print_kv 'Employee ID' "$(ldif_attr "$ldif" employeeID)"
    print_kv 'Employee Number' "$(ldif_attr "$ldif" employeeNumber)"
    print_kv DN "$(ldif_attr "$ldif" dn)"

    printf '\n%sAccount%s\n%s\n' "$C_CYAN" "$C_RESET" '------------------------------------------------------------'
    print_kv Home "$(ldif_attr "$ldif" homeDirectory)"
    print_kv Shell "$(ldif_attr "$ldif" loginShell)"
    print_kv Email "$(ldif_attr "$ldif" mail)"
    print_kv UPN "$(ldif_attr "$ldif" userPrincipalName)"
    print_kv 'Account status' "$(account_status "$ldif")"

    printf '\n%sOrganization%s\n%s\n' "$C_CYAN" "$C_RESET" '------------------------------------------------------------'
    print_kv Department "$(ldif_attr "$ldif" department)"
    print_kv 'Department No.' "$(ldif_attr "$ldif" departmentNumber)"
    print_kv Title "$(ldif_attr "$ldif" title)"
    print_kv Company "$(ldif_attr "$ldif" company)"
    print_kv Manager "$(ldif_attr "$ldif" manager)"

    if [[ -n "$groups_ldap" ]]; then
        printf '\n%sGroups (LDAP)%s\n%s\n%s\n' "$C_BOLD" "$C_RESET" '------------------------------------------------------------' "$groups_ldap"
    fi
    if [[ -n "$groups_nss" ]]; then
        printf '\n%sGroups (NSS/SSSD)%s\n%s\n%s\n' "$C_BOLD" "$C_RESET" '------------------------------------------------------------' "$groups_nss"
    fi
    if [[ -n "$netgroups_ldap" ]]; then
        printf '\n%sNetgroups (LDAP)%s\n%s\n%s\n' "$C_BOLD" "$C_RESET" '------------------------------------------------------------' "$netgroups_ldap"
    fi
    if [[ -n "$netgroups_nss" ]]; then
        printf '\n%sNetgroups (NSS/SSSD)%s\n%s\n%s\n' "$C_BOLD" "$C_RESET" '------------------------------------------------------------' "$netgroups_nss"
    fi
}

json_escape() {
    local value="${1-}"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//$'\n'/\\n}"
    value="${value//$'\r'/\\r}"
    value="${value//$'\t'/\\t}"
    printf '%s' "$value"
}

json_string_or_null() {
    if [[ -n "${1-}" ]]; then
        printf '"%s"' "$(json_escape "$1")"
    else
        printf 'null'
    fi
}

json_number_or_null() {
    if [[ "${1-}" =~ ^[0-9]+$ ]]; then
        printf '%s' "$1"
    else
        printf 'null'
    fi
}

json_array_from_lines() {
    local lines="${1-}"
    local first=1
    local line
    printf '['
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        if (( first )); then
            first=0
        else
            printf ','
        fi
        printf '"%s"' "$(json_escape "$line")"
    done <<<"$lines"
    printf ']'
}

print_json() {
    local ldif="$1"
    local query="$2"
    local uid
    local name
    local groups
    local netgroups

    uid="$(ldif_attr "$ldif" uid)"
    [[ -n "$uid" ]] || uid="$(ldif_attr "$ldif" sAMAccountName)"
    [[ -n "$uid" ]] || uid="$query"
    name="$(display_name "$ldif")"
    groups="$({ get_user_groups_ldap "$ldif"; get_user_groups_nss "$uid"; } 2>/dev/null | merge_unique_lines)"
    netgroups="$({ get_user_netgroups_ldap "$uid"; get_user_netgroups_nss "$uid"; } 2>/dev/null | merge_unique_lines)"

    printf '{\n  "uid": '
    json_string_or_null "$uid"
    printf ',\n  "uidNumber": '
    json_number_or_null "$(ldif_attr "$ldif" uidNumber)"
    printf ',\n  "gidNumber": '
    json_number_or_null "$(ldif_attr "$ldif" gidNumber)"
    printf ',\n  "name": '
    json_string_or_null "$name"
    printf ',\n  "mail": '
    json_string_or_null "$(ldif_attr "$ldif" mail)"
    printf ',\n  "dn": '
    json_string_or_null "$(ldif_attr "$ldif" dn)"
    printf ',\n  "groups": '
    json_array_from_lines "$groups"
    printf ',\n  "netgroups": '
    json_array_from_lines "$netgroups"
    printf '\n}\n'
}

print_config() {
    printf '%sConfiguration%s\n%s\n' "$C_CYAN" "$C_RESET" '------------------------------------------------------------'
    print_kv Source "$CONFIG_SOURCE"
    print_kv SSSD "$SSSD_ENABLED"
    print_kv 'SSSD domain' "$SSSD_DOMAIN"
    print_kv 'ID provider' "$ID_PROVIDER"
    print_kv 'AD domain' "$AD_DOMAIN"
    print_kv 'AD server' "$AD_SERVER"
    print_kv 'LDAP schema' "$LDAP_SCHEMA"
    print_kv 'LDAP ID mapping' "$LDAP_ID_MAPPING"
    print_kv 'LDAP URI' "${LDAP_URIS[*]:-}"
    print_kv 'Search base' "$LDAP_SEARCH_BASE"
    print_kv 'User search base' "$LDAP_USER_BASE"
    print_kv 'Group search base' "$LDAP_GROUP_BASE"
    print_kv 'Netgroup base' "$LDAP_NETGROUP_BASE"
    print_kv 'Bind DN' "$LDAP_BIND_DN"
    print_kv Authentication "$AUTH_METHOD"
}

print_groups_only() {
    {
        get_user_groups_ldap "$1"
        get_user_groups_nss "$2"
    } 2>/dev/null | merge_unique_lines
}

print_netgroups_only() {
    {
        get_user_netgroups_ldap "$1"
        get_user_netgroups_nss "$1"
    } 2>/dev/null | merge_unique_lines
}

usage() {
    cat <<'EOF_USAGE'
Usage:
  ldapsearch.sh USER
  ldapsearch.sh --user USER
  ldapsearch.sh -u USER
  ldapsearch.sh --raw USER
  ldapsearch.sh --groups USER
  ldapsearch.sh --netgroups USER
  ldapsearch.sh --json USER
  ldapsearch.sh --debug USER
  ldapsearch.sh --config
  ldapsearch.sh --help

Options:
  -u, --user USER   User/login/search term.
  --raw             Print safe LDAP LDIF with credential attributes removed.
  --groups          Print group memberships only.
  --netgroups       Print netgroup memberships only.
  --json            Print JSON.
  --debug           Print diagnostics without credentials.
  --config          Print detected LDAP/SSSD configuration without secrets.
  -h, --help        Show help.
  --version         Show version.
EOF_USAGE
}

parse_args() {
    while (($#)); do
        case "$1" in
            -u|--user)
                (($# >= 2)) || die "$1 requires an argument"
                USER_QUERY="$2"
                shift 2
                ;;
            --raw|--groups|--netgroups|--json)
                MODE="${1#--}"
                shift
                ;;
            --debug)
                DEBUG=1
                shift
                ;;
            --config)
                MODE="config"
                shift
                ;;
            --version)
                printf '%s\n' "$VERSION"
                exit 0
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            --)
                shift
                if (($#)); then
                    USER_QUERY="$1"
                fi
                break
                ;;
            -*)
                die "Unknown option: $1"
                ;;
            *)
                [[ -z "$USER_QUERY" ]] || die 'Only one USER argument is accepted'
                USER_QUERY="$1"
                shift
                ;;
        esac
    done
}

main() {
    local found=""
    local user_ldif=""
    local count=0
    local uid
    local entry
    local -a entries=()

    parse_args "$@"
    detect_config
    detect_auth_method

    if [[ "$MODE" == config ]]; then
        print_config
        exit 0
    fi

    if [[ -z "$USER_QUERY" ]]; then
        usage >&2
        exit 2
    fi
    require_ldapsearch_or_nss || exit 127

    if command -v ldapsearch >/dev/null 2>&1 && [[ ${#LDAP_URIS[@]} -gt 0 && -n "$LDAP_USER_BASE" ]]; then
        found="$(search_user_ldap "$USER_QUERY" || true)"
    fi

    if [[ -n "$found" ]]; then
        if [[ "$MODE" == raw ]]; then
            printf '%s\n' "$found" | filter_ldif_secrets
            exit 0
        fi
        mapfile -d '' -t entries < <(printf '%s\n' "$found" | split_ldif_entries)
        count="${#entries[@]}"
        if (( count > 1 )) && [[ "$MODE" == json ]]; then
            warn 'Multiple LDAP entries matched; JSON mode uses the first exact/highest-priority result.'
        fi
        user_ldif="${entries[0]:-}"
    else
        user_ldif="$(nss_user_ldif "$USER_QUERY" || true)"
        [[ -n "$user_ldif" ]] || die "User not found: $USER_QUERY"
        warn 'Direct LDAP query unavailable or returned no entry; using NSS/SSSD fallback.'
        if [[ "$MODE" == raw ]]; then
            printf '%s\n' "$user_ldif" | filter_ldif_secrets
            exit 0
        fi
    fi

    uid="$(ldif_attr "$user_ldif" uid)"
    [[ -n "$uid" ]] || uid="$(ldif_attr "$user_ldif" sAMAccountName)"
    [[ -n "$uid" ]] || uid="$USER_QUERY"

    case "$MODE" in
        groups)
            print_groups_only "$user_ldif" "$uid"
            ;;
        netgroups)
            print_netgroups_only "$uid"
            ;;
        json)
            print_json "$user_ldif" "$USER_QUERY"
            ;;
        full)
            if (( count > 1 )); then
                printf 'Found %d users. Showing all matches in LDAP priority order.\n\n' "$count"
                for entry in "${entries[@]}"; do
                    print_user "$entry" "$USER_QUERY"
                    printf '\n'
                done
            else
                print_user "$user_ldif" "$USER_QUERY"
            fi
            ;;
        *)
            die "Internal error: unsupported mode $MODE"
            ;;
    esac

    if [[ "$SSSD_ENABLED" == yes && "$DEBUG" -eq 1 ]] && command -v sssctl >/dev/null 2>&1; then
        sssctl user-checks "$uid" 2>/dev/null || true
        sssctl user-show "$uid" 2>/dev/null || true
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" && "${LDAPSEARCH_LIB:-0}" != 1 ]]; then
    main "$@"
fi
