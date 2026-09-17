#!/usr/bin/env bash
set -uo pipefail

SCRIPT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/nginx-manager.sh}"
PASS=0
FAIL=0

ok(){ printf 'ok - %s\n' "$1"; ((PASS+=1)); }
not_ok(){ printf 'not ok - %s\n' "$1" >&2; ((FAIL+=1)); }
expect_success(){ local name="$1"; shift; if "$@"; then ok "$name"; else not_ok "$name"; fi; }
expect_failure(){ local name="$1"; shift; if "$@"; then not_ok "$name"; else ok "$name"; fi; }

expect_success "bash syntax" bash -n "$SCRIPT"
expect_success "help" bash "$SCRIPT" --help
expect_success "version" bash "$SCRIPT" --version

expect_success "valid domain" bash -c 'source "$1"; validate_domain "api.example.com"' _ "$SCRIPT"
expect_failure "invalid domain" bash -c 'source "$1"; validate_domain "bad..example.com"' _ "$SCRIPT"
expect_success "valid port" bash -c 'source "$1"; validate_port 443' _ "$SCRIPT"
expect_failure "invalid port" bash -c 'source "$1"; validate_port 70000' _ "$SCRIPT"
expect_success "valid absolute path" bash -c 'source "$1"; validate_safe_path "/var/www/example"' _ "$SCRIPT"
expect_failure "path traversal rejected" bash -c 'source "$1"; validate_safe_path "/var/www/../etc"' _ "$SCRIPT"

out="$(bash -c 'source "$1"; generate_static_site_config example.com /var/www/example 80 "" false' _ "$SCRIPT")"
if [[ "$out" == *'server_name example.com;'* && "$out" == *'try_files $uri $uri/ =404;'* ]]; then ok "static config generator"; else not_ok "static config generator"; fi

out="$(bash -c 'source "$1"; generate_reverse_proxy_config api.example.com 127.0.0.1 8080 http true false' _ "$SCRIPT")"
if [[ "$out" == *'proxy_pass http://127.0.0.1:8080;'* && "$out" == *'proxy_set_header Upgrade $http_upgrade;'* ]]; then ok "proxy websocket generator"; else not_ok "proxy websocket generator"; fi

listen_cfg="$(mktemp)"
cat > "$listen_cfg" <<'EOF_LISTEN'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    listen 443 ssl;
    listen [::]:443 ssl;
}
EOF_LISTEN
out="$(bash -c 'source "$1"; rewrite_http_listen_ports "$2" 8080' _ "$SCRIPT" "$listen_cfg")"
if [[ "$out" == *'listen 8080 default_server;'* && "$out" == *'listen [::]:8080 default_server;'* && "$out" == *'listen 443 ssl;'* && "$out" == *'listen [::]:443 ssl;'* ]]; then
  ok "HTTP port rewrite preserves SSL"
else
  not_ok "HTTP port rewrite preserves SSL"
fi
rm -f -- "$listen_cfg"

fakebin="$(mktemp -d)"
fakeetc="$(mktemp -d)"
fakebackup="$(mktemp -d)"
mkdir -p "$fakeetc/conf.d"
printf 'events {}\nhttp { include %s/conf.d/*.conf; }\n' "$fakeetc" > "$fakeetc/nginx.conf"

cat > "$fakebin/nginx" <<'EOF_NGINX'
#!/usr/bin/env bash
case "${1:-}" in
  -v) printf '%s\n' 'nginx version: nginx/1.26.0' >&2 ;;
  -t) [[ "${FAKE_NGINX_INVALID:-0}" == 1 ]] && { printf '%s\n' 'configuration file test failed' >&2; exit 1; }; printf '%s\n' 'configuration file test is successful' >&2 ;;
  -T) printf '%s\n' "include ${NGINX_MANAGER_ETC}/conf.d/*.conf;" ;;
esac
EOF_NGINX
cat > "$fakebin/systemctl" <<'EOF_SYSTEMCTL'
#!/usr/bin/env bash
case "${1:-}" in
  is-active) printf '%s\n' active ;;
  show) printf '%s\n' 123 ;;
  *) printf '%s\n' "systemctl $*" >> "${FAKE_SYSTEMCTL_LOG}" ;;
esac
EOF_SYSTEMCTL
chmod +x "$fakebin/nginx" "$fakebin/systemctl"

export PATH="$fakebin:$PATH"
export NGINX_MANAGER_ETC="$fakeetc"
export NGINX_MANAGER_BACKUP_DIR="$fakebackup"
export NGINX_MANAGER_LOG_FILE="$fakebackup/events.log"
export FAKE_SYSTEMCTL_LOG="$fakebackup/systemctl.log"

expect_success "status with fake nginx" bash "$SCRIPT" --non-interactive --status
expect_success "valid config test" bash "$SCRIPT" --non-interactive --test
reload_out="$(bash "$SCRIPT" --non-interactive --reload --dry-run 2>&1)"
if [[ "$reload_out" == *'DRY RUN: systemctl reload nginx'* ]]; then ok "reload after valid test"; else not_ok "reload after valid test"; fi

: > "$FAKE_SYSTEMCTL_LOG"
export FAKE_NGINX_INVALID=1
expect_failure "invalid config test" bash "$SCRIPT" --non-interactive --test
expect_failure "invalid config blocks reload" bash "$SCRIPT" --non-interactive --reload --dry-run
if [[ ! -s "$FAKE_SYSTEMCTL_LOG" ]]; then ok "no reload on invalid config"; else not_ok "no reload on invalid config"; fi
unset FAKE_NGINX_INVALID

expect_failure "silent add-site requires domain" bash "$SCRIPT" --non-interactive --add-site --root /var/www/example
expect_failure "site port change requires explicit port" bash "$SCRIPT" --non-interactive --change-site-port --domain example.com --yes
expect_failure "default port change requires explicit port" bash "$SCRIPT" --non-interactive --set-default-port --yes

source_cfg="$(mktemp)"
printf 'server { listen 80; server_name atomic.example; }\n' > "$source_cfg"
expect_success "atomic config write" bash -c '
  source "$1"
  require_root(){ :; }
  reload_nginx(){ :; }
  atomic_write_config "$2" "$3/conf.d/atomic.conf"
  cmp -s "$2" "$3/conf.d/atomic.conf"
' _ "$SCRIPT" "$source_cfg" "$fakeetc"

printf 'old configuration\n' > "$fakeetc/conf.d/rollback.conf"
printf 'new invalid configuration\n' > "$source_cfg"
export FAKE_NGINX_INVALID=1
expect_failure "atomic invalid write fails" bash -c '
  source "$1"
  require_root(){ :; }
  reload_nginx(){ :; }
  atomic_write_config "$2" "$3/conf.d/rollback.conf"
' _ "$SCRIPT" "$source_cfg" "$fakeetc"
if grep -q '^old configuration$' "$fakeetc/conf.d/rollback.conf"; then ok "atomic rollback restores file"; else not_ok "atomic rollback restores file"; fi
unset FAKE_NGINX_INVALID

base="$(basename "$fakeetc")"
good_backup="$fakebackup/nginx-good.tar.gz"
tar -C "$(dirname "$fakeetc")" -czf "$good_backup" "$base"
printf 'changed\n' > "$fakeetc/marker"
expect_success "restore valid backup" bash -c '
  source "$1"
  require_root(){ :; }
  reload_nginx(){ :; }
  ASSUME_YES=true
  restore_backup "$2"
' _ "$SCRIPT" "$good_backup"
if [[ ! -e "$fakeetc/marker" ]]; then ok "restore replaced configuration"; else not_ok "restore replaced configuration"; fi

rm -f -- "$source_cfg"

rm -rf -- "$fakebin" "$fakeetc" "$fakebackup"

printf '\nTests: %d passed, %d failed\n' "$PASS" "$FAIL"
((FAIL == 0))
