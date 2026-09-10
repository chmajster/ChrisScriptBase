#!/usr/bin/env bats

setup() {
  SCRIPT="${BATS_TEST_DIRNAME}/../ldapsearch.sh"
  TMPDIR_TEST="$(mktemp -d)"
  mkdir -p "$TMPDIR_TEST/etc/sssd" "$TMPDIR_TEST/etc/openldap" "$TMPDIR_TEST/etc/ldap" "$TMPDIR_TEST/bin"
}

teardown() {
  rm -rf "$TMPDIR_TEST"
}

@test "1 parses sssd.conf and prefers SSSD" {
  cat >"$TMPDIR_TEST/etc/sssd/sssd.conf" <<'EOF'
[sssd]
domains = example.com
[domain/example.com]
id_provider = ldap
ldap_uri = ldaps://ldap01.example.com, ldaps://ldap02.example.com
ldap_search_base = dc=example,dc=com
ldap_user_search_base = ou=People,dc=example,dc=com
ldap_group_search_base = ou=Groups,dc=example,dc=com
ldap_netgroup_search_base = ou=Netgroups,dc=example,dc=com
ldap_default_bind_dn = cn=reader,dc=example,dc=com
ldap_default_authtok = top-secret
EOF
  run env LDAPSEARCH_ETC="$TMPDIR_TEST/etc" LDAPSEARCH_LIB=1 bash -c 'source "$1"; detect_config; printf "%s|%s|%s|%s" "$CONFIG_SOURCE" "$SSSD_DOMAIN" "$LDAP_USER_BASE" "${LDAP_URIS[*]}"' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == "SSSD|example.com|ou=People,dc=example,dc=com|ldaps://ldap01.example.com ldaps://ldap02.example.com" ]]
  [[ "$output" != *"top-secret"* ]]
}

@test "2 parses OpenLDAP ldap.conf" {
  cat >"$TMPDIR_TEST/etc/openldap/ldap.conf" <<'EOF'
URI ldap://ldap.example.com
BASE dc=example,dc=org
EOF
  run env LDAPSEARCH_ETC="$TMPDIR_TEST/etc" LDAPSEARCH_LIB=1 bash -c 'source "$1"; detect_config; printf "%s|%s|%s" "$CONFIG_SOURCE" "$LDAP_SEARCH_BASE" "${LDAP_URIS[*]}"' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == "OpenLDAP|dc=example,dc=org|ldap://ldap.example.com" ]]
}

@test "3 supports multiple LDAP URIs" {
  run env LDAPSEARCH_LIB=1 bash -c 'source "$1"; add_uris "ldap://one ldap://two,ldaps://three"; printf "%s\n" "${LDAP_URIS[@]}"' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "ldap://one" ]
  [ "${lines[1]}" = "ldap://two" ]
  [ "${lines[2]}" = "ldaps://three" ]
}

@test "4 escapes RFC4515 filter metacharacters" {
  run env LDAPSEARCH_LIB=1 bash -c 'source "$1"; ldap_escape_filter "a*(b)\\c"' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [ "$output" = 'a\2a\28b\29\5cc' ]
}

@test "5 LDAP injection payload is escaped" {
  run env LDAPSEARCH_LIB=1 bash -c 'source "$1"; ldap_escape_filter "*)(uid=*)"' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [ "$output" = '\2a\29\28uid=\2a\29' ]
}

@test "6 search_user_ldap tries exact match first" {
  run env LDAPSEARCH_LIB=1 bash -c '
    source "$1"
    LDAP_USER_BASE="dc=example,dc=com"
    ldap_query() {
      case "$2" in
        *"(uid=alice)"*) printf "dn: uid=alice,dc=example,dc=com\nuid: alice\ncn: Alice\n\n"; return 0 ;;
      esac
      return 1
    }
    search_user_ldap alice
  ' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"uid: alice"* ]]
}

@test "7 missing LDAP user returns failure" {
  run env LDAPSEARCH_LIB=1 bash -c 'source "$1"; LDAP_USER_BASE="dc=x"; ldap_query(){ return 1; }; search_user_ldap nobody' _ "$SCRIPT"
  [ "$status" -ne 0 ]
}

@test "8 groups include member lookup" {
  run env LDAPSEARCH_LIB=1 bash -c '
    source "$1"; LDAP_GROUP_BASE="ou=Groups,dc=x"
    ldap_query(){ [[ "$2" == *"member="* ]] || return 1; printf "dn: cn=admins,ou=Groups,dc=x\ncn: admins\n\n"; }
    get_user_groups_ldap $'"'"'dn: uid=alice,dc=x\nuid: alice'"'"'
  ' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"admins"* ]]
}

@test "9 groups include memberUid lookup" {
  run env LDAPSEARCH_LIB=1 bash -c '
    source "$1"; LDAP_GROUP_BASE="ou=Groups,dc=x"
    ldap_query(){ [[ "$2" == *"memberUid=alice"* ]] || return 1; printf "dn: cn=unix,ou=Groups,dc=x\ncn: unix\n\n"; }
    get_user_groups_ldap $'"'"'dn: uid=alice,dc=x\nuid: alice'"'"'
  ' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"unix"* ]]
}

@test "10 groups include uniqueMember lookup" {
  run env LDAPSEARCH_LIB=1 bash -c '
    source "$1"; LDAP_GROUP_BASE="ou=Groups,dc=x"
    ldap_query(){ [[ "$2" == *"uniqueMember="* ]] || return 1; printf "dn: cn=unique,ou=Groups,dc=x\ncn: unique\n\n"; }
    get_user_groups_ldap $'"'"'dn: uid=alice,dc=x\nuid: alice'"'"'
  ' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"unique"* ]]
}

@test "11 groups include memberOf attribute" {
  run env LDAPSEARCH_LIB=1 bash -c '
    source "$1"; LDAP_GROUP_BASE=""
    get_user_groups_ldap $'"'"'dn: uid=alice,dc=x\nuid: alice\nmemberOf: cn=devs,ou=Groups,dc=x'"'"'
  ' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [ "$output" = "devs" ]
}

@test "12 direct netgroup membership works" {
  run env LDAPSEARCH_LIB=1 bash -c '
    source "$1"; LDAP_NETGROUP_BASE="ou=Netgroups,dc=x"
    ldap_query(){ cat <<EOF
dn: cn=linux,ou=Netgroups,dc=x
cn: linux
objectClass: nisNetgroup
nisNetgroupTriple: (,alice,)

EOF
    }
    get_user_netgroups_ldap alice
  ' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [ "$output" = "linux" ]
}

@test "13 nested netgroup membership works" {
  run env LDAPSEARCH_LIB=1 bash -c '
    source "$1"; LDAP_NETGROUP_BASE="ou=Netgroups,dc=x"
    ldap_query(){ cat <<EOF
dn: cn=child,ou=Netgroups,dc=x
cn: child
objectClass: nisNetgroup
nisNetgroupTriple: (,alice,)

dn: cn=parent,ou=Netgroups,dc=x
cn: parent
objectClass: nisNetgroup
memberNisNetgroup: child

EOF
    }
    get_user_netgroups_ldap alice
  ' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"child"* ]]
  [[ "$output" == *"parent"* ]]
}

@test "14 cyclic nested netgroups terminate safely" {
  run env LDAPSEARCH_LIB=1 timeout 3 bash -c '
    source "$1"; LDAP_NETGROUP_BASE="ou=Netgroups,dc=x"
    ldap_query(){ cat <<EOF
dn: cn=A,ou=Netgroups,dc=x
cn: A
objectClass: nisNetgroup
memberNisNetgroup: B

dn: cn=B,ou=Netgroups,dc=x
cn: B
objectClass: nisNetgroup
memberNisNetgroup: A
nisNetgroupTriple: (,alice,)

EOF
    }
    get_user_netgroups_ldap alice
  ' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"A"* ]]
  [[ "$output" == *"B"* ]]
}

@test "15 LDAP failover advances to second URI" {
  cat >"$TMPDIR_TEST/bin/ldapsearch" <<'EOF'
#!/usr/bin/env bash
case " $* " in
  *" -H ldap://one "*) exit 81 ;;
  *" -H ldap://two "*) printf 'dn: uid=alice,dc=x\nuid: alice\n\n'; exit 0 ;;
esac
exit 1
EOF
  chmod +x "$TMPDIR_TEST/bin/ldapsearch"
  run env PATH="$TMPDIR_TEST/bin:/usr/bin:/bin" LDAPSEARCH_LIB=1 bash -c '
    source "$1"; LDAP_URIS=(ldap://one ldap://two); AUTH_METHOD=anonymous
    ldap_query "dc=x" "(uid=alice)" sub uid
    printf "ACTIVE=%s\n" "$ACTIVE_URI"
  ' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"ACTIVE=ldap://two"* ]]
}

@test "16 LDAP timeout prevents hanging forever" {
  cat >"$TMPDIR_TEST/bin/ldapsearch" <<'EOF'
#!/usr/bin/env bash
sleep 5
EOF
  chmod +x "$TMPDIR_TEST/bin/ldapsearch"
  run env PATH="$TMPDIR_TEST/bin:/usr/bin:/bin" LDAP_TIMEOUT=1 LDAPSEARCH_LIB=1 timeout 3 bash -c '
    source "$1"; LDAP_URIS=(ldap://slow); AUTH_METHOD=anonymous
    ldap_query "dc=x" "(uid=alice)" sub uid
  ' _ "$SCRIPT"
  [ "$status" -ne 0 ]
}

@test "17 redirected config output contains no ANSI" {
  cat >"$TMPDIR_TEST/etc/openldap/ldap.conf" <<'EOF'
URI ldap://ldap.example.com
BASE dc=example,dc=com
EOF
  run env LDAPSEARCH_ETC="$TMPDIR_TEST/etc" bash "$SCRIPT" --config
  [ "$status" -eq 0 ]
  [[ "$output" != *$'\033['* ]]
}

@test "18 NO_COLOR disables ANSI" {
  run env NO_COLOR=1 LDAPSEARCH_LIB=1 bash -c 'source "$1"; printf "%q" "$C_RED"' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [ "$output" = "''" ]
}

@test "19 JSON output is valid JSON" {
  run env LDAPSEARCH_LIB=1 bash -c '
    source "$1"
    LDAP_GROUP_BASE=""; LDAP_NETGROUP_BASE=""
    get_user_groups_ldap(){ printf "devs\nadmins\n"; }
    get_user_groups_nss(){ :; }
    get_user_netgroups_ldap(){ printf "linux\n"; }
    get_user_netgroups_nss(){ :; }
    print_json $'"'"'dn: uid=alice,dc=x\nuid: alice\nuidNumber: 1001\ngidNumber: 100\ncn: Alice Example\nmail: alice@example.com'"'"' alice
  ' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  run python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); assert d["uid"]=="alice" and d["uidNumber"]==1001 and "admins" in d["groups"]' <<<"$output"
  [ "$status" -eq 0 ]
}

@test "20 missing ldapsearch can use NSS fallback" {
  cat >"$TMPDIR_TEST/bin/getent" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "$TMPDIR_TEST/bin/getent"
  run env PATH="$TMPDIR_TEST/bin" LDAPSEARCH_LIB=1 /bin/bash -c 'source "$1"; require_ldapsearch_or_nss' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"direct LDAP queries disabled"* ]]
}

@test "21 NSS passwd fallback produces LDIF-like user data" {
  cat >"$TMPDIR_TEST/bin/getent" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == passwd && "$2" == alice ]]; then
  echo 'alice:x:1001:100:Alice Example:/home/alice:/bin/bash'
  exit 0
fi
exit 2
EOF
  chmod +x "$TMPDIR_TEST/bin/getent"
  run env PATH="$TMPDIR_TEST/bin:/usr/bin:/bin" LDAPSEARCH_LIB=1 bash -c 'source "$1"; nss_user_ldif alice' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"uidNumber: 1001"* ]]
  [[ "$output" == *"source: NSS/SSSD"* ]]
}

@test "22 raw LDIF filtering removes credential attributes and continuations" {
  run env LDAPSEARCH_LIB=1 bash -c '
    source "$1"
    printf "%s\n" $'"'"'dn: uid=alice,dc=x\nuid: alice\nuserPassword:: c2VjcmV0\n continued\nunicodePwd:: xxx\napiToken: nope\nmail: alice@example.com'"'"' | filter_ldif_secrets
  ' _ "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"uid: alice"* ]]
  [[ "$output" == *"mail: alice@example.com"* ]]
  [[ "$output" != *"Password"* ]]
  [[ "$output" != *"unicodePwd"* ]]
  [[ "$output" != *"Token"* ]]
  [[ "$output" != *"continued"* ]]
}
