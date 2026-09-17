#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/tailscale-manager.sh}"
PASS=0
FAIL=0

ok(){ printf 'ok - %s\n' "$1"; ((PASS+=1)); }
not_ok(){ printf 'not ok - %s\n' "$1" >&2; ((FAIL+=1)); }
expect_success(){ local name="$1"; shift; if "$@"; then ok "$name"; else not_ok "$name"; fi; }
expect_failure(){ local name="$1"; shift; if "$@"; then not_ok "$name"; else ok "$name"; fi; }

expect_success "bash syntax" bash -n "$SCRIPT"
expect_success "help" bash "$SCRIPT" --help
expect_success "version" bash "$SCRIPT" --version

expect_success "valid hostname" bash -c 'source "$1"; validate_hostname "server-01.lab"' _ "$SCRIPT"
expect_failure "invalid hostname" bash -c 'source "$1"; validate_hostname "-bad-host"' _ "$SCRIPT"
expect_success "valid IPv4" bash -c 'source "$1"; validate_ipv4 "192.168.10.25"' _ "$SCRIPT"
expect_failure "invalid IPv4" bash -c 'source "$1"; validate_ipv4 "192.168.10.999"' _ "$SCRIPT"
expect_success "valid IPv4 CIDR" bash -c 'source "$1"; validate_cidr "10.20.0.0/16"' _ "$SCRIPT"
expect_failure "invalid IPv4 CIDR" bash -c 'source "$1"; validate_cidr "10.20.0.0/99"' _ "$SCRIPT"
expect_success "valid tags" bash -c 'source "$1"; validate_tags "tag:server,tag:prod_1"' _ "$SCRIPT"
expect_failure "invalid tags" bash -c 'source "$1"; validate_tags "server,tag:prod"' _ "$SCRIPT"

cfg="$(mktemp)"
cat > "$cfg" <<'EOF_CFG'
HOSTNAME=config-host
SSH=false
ACCEPT_ROUTES=false
EOF_CFG
expect_success "CLI overrides config" bash -c '
  source "$1"
  CONFIG_FILE="$2"
  load_config
  parse_args --config "$2" --hostname cli-host --ssh --accept-routes --status --silent
  [[ "$HOSTNAME_OVERRIDE" == "cli-host" && "$SSH" == true && "$ACCEPT_ROUTES" == true && "$DO_STATUS" == true && "$MODE" == silent ]]
' _ "$SCRIPT" "$cfg"
rm -f "$cfg"

expect_failure "config rejects AUTH_KEY" bash -c '
  source "$1"
  f="$(mktemp)"; printf "%s\n" "AUTH_KEY=tskey-secret" > "$f"
  CONFIG_FILE="$f"
  load_config
' _ "$SCRIPT"

expect_failure "silent requires action" bash "$SCRIPT" --silent
expect_failure "invalid route exits" bash "$SCRIPT" --silent --advertise-routes 192.168.1.999/24 --dry-run

fakebin="$(mktemp -d)"
cat > "$fakebin/tailscale" <<'EOF_TS'
#!/usr/bin/env bash
case "${1:-}" in
  status)
    if [[ "${2:-}" == "--json" ]]; then printf '%s\n' '{"BackendState":"Running"}'; else printf '%s\n' '100.64.0.1 test user linux -'; fi
    ;;
  version) printf '%s\n' '1.90.0' ;;
  ip) [[ "${2:-}" == "-6" ]] && printf '%s\n' 'fd7a::1' || printf '%s\n' '100.64.0.1' ;;
  get) printf '%s\n' '--accept-dns=true' ;;
  dns) printf '%s\n' 'MagicDNS: true' ;;
  netcheck) printf '%s\n' 'UDP: true' ;;
  *) exit 0 ;;
esac
EOF_TS
cat > "$fakebin/systemctl" <<'EOF_SYSTEMCTL'
#!/usr/bin/env bash
[[ "${1:-}" == "is-active" ]] && exit 0
exit 0
EOF_SYSTEMCTL
chmod +x "$fakebin/tailscale" "$fakebin/systemctl"

out="$(PATH="$fakebin:$PATH" bash "$SCRIPT" --silent --connect --auth-key tskey-super-secret --dry-run 2>&1)"
if [[ "$out" == *"--auth-key=********"* && "$out" != *"tskey-super-secret"* ]]; then ok "auth key masked in dry-run"; else not_ok "auth key masked in dry-run"; fi

out="$(PATH="$fakebin:$PATH" bash "$SCRIPT" --silent --advertise-routes 192.168.50.0/24 --dry-run 2>&1)"
if [[ "$out" == *"net.ipv4.ip_forward=1"* && "$out" == *"tailscale set"* ]]; then ok "subnet router dry-run"; else not_ok "subnet router dry-run"; fi

rm -rf "$fakebin"

printf '\nTests: %d passed, %d failed\n' "$PASS" "$FAIL"
((FAIL == 0))
