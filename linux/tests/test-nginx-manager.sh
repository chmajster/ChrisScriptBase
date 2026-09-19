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

out="$(bash -c 'source "$1"; generate_reverse_proxy_config api.example.com 127.0.0.1 9000 http false false 8088' _ "$SCRIPT")"
if [[ "$out" == *'listen 8088;'* && "$out" == *'listen [::]:8088;'* && "$out" == *'proxy_pass http://127.0.0.1:9000;'* ]]; then ok "proxy custom frontend port"; else not_ok "proxy custom frontend port"; fi

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

listen_cfg="$(mktemp)"
cat > "$listen_cfg" <<'EOF_SELECTIVE_LISTEN'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    listen 127.0.0.1:80;
    listen 443 ssl;
    listen 9000;
}
EOF_SELECTIVE_LISTEN
out="$(bash -c 'source "$1"; rewrite_specific_listen_port "$2" 80 8088' _ "$SCRIPT" "$listen_cfg")"
if [[ "$out" == *'listen 8088 default_server;'* && "$out" == *'listen [::]:8088 default_server;'* && "$out" == *'listen 127.0.0.1:8088;'* && "$out" == *'listen 443 ssl;'* && "$out" == *'listen 9000;'* ]]; then
  ok "selective global port rewrite"
else
  not_ok "selective global port rewrite"
fi
expect_success "listen port detector finds 80" bash -c 'source "$1"; config_listens_on_port "$2" 80' _ "$SCRIPT" "$listen_cfg"
expect_failure "listen port detector ignores 8088 before rewrite" bash -c 'source "$1"; config_listens_on_port "$2" 8088' _ "$SCRIPT" "$listen_cfg"
rm -f -- "$listen_cfg"

choices_dir="$(mktemp -d)"
cat > "$choices_dir/example.conf" <<'EOF_CHOICE_ONE'
server {
    listen 8080;
    listen [::]:8080;
    server_name example.com www.example.com;
}
EOF_CHOICE_ONE
cat > "$choices_dir/api.conf" <<'EOF_CHOICE_TWO'
server {
    listen 9000;
    server_name api.example.com;
}
EOF_CHOICE_TWO
out="$(bash -c '
  source "$1"
  LAYOUT=rhel
  SITES_AVAILABLE="$2"
  SITES_ENABLED="$2"
  site_choice_rows
' _ "$SCRIPT" "$choices_dir")"
if [[ "$out" == *$'example.com\texample.com www.example.com | ENABLED | port 8080 | example.conf'* && "$out" == *$'api.example.com\tapi.example.com | ENABLED | port 9000 | api.conf'* ]]; then
  ok "site selection rows"
else
  not_ok "site selection rows"
fi
rm -rf -- "$choices_dir"

discover_root="$(mktemp -d)"
mkdir -p "$discover_root/sites-available" "$discover_root/sites-enabled" "$discover_root/conf.d"
cat > "$discover_root/sites-available/app.conf" <<'EOF_DISCOVER_AVAILABLE'
server {
    listen 8080;
    server_name app.example;
}
EOF_DISCOVER_AVAILABLE
ln -s ../sites-available/app.conf "$discover_root/sites-enabled/app.conf"
cat > "$discover_root/sites-enabled/enabled-only.conf" <<'EOF_DISCOVER_ENABLED'
server {
    listen 8081;
    server_name enabled-only.example;
}
EOF_DISCOVER_ENABLED
cat > "$discover_root/conf.d/default" <<'EOF_DISCOVER_DEFAULT'
server {
    listen 8088 default_server;
    server_name _;
}
EOF_DISCOVER_DEFAULT

out="$(bash -c '
  source "$1"
  NGINX_ETC="$2"
  LAYOUT=debian
  SITES_AVAILABLE="$2/sites-available"
  SITES_ENABLED="$2/sites-enabled"
  CONF_D="$2/conf.d"
  site_file_choice_rows
' _ "$SCRIPT" "$discover_root")"

if [[ "$out" == *"$discover_root/sites-available/app.conf"$'\t'"app.example | ENABLED | port 8080 | sites-available/app.conf"* \
   && "$out" == *"$discover_root/sites-enabled/enabled-only.conf"$'\t'"enabled-only.example | ENABLED | port 8081 | sites-enabled/enabled-only.conf"* \
   && "$out" == *"$discover_root/conf.d/default"$'\t'"_ | CONFIG | port 8088 | conf.d/default"* ]]; then
  ok "discover editable configs across nginx directories"
else
  not_ok "discover editable configs across nginx directories"
fi

app_count="$(printf '%s\n' "$out" | grep -Fc "$discover_root/sites-available/app.conf" || true)"
if [[ "$app_count" == 1 ]]; then
  ok "sites-enabled symlink does not duplicate site"
else
  not_ok "sites-enabled symlink does not duplicate site"
fi

found_default="$(bash -c '
  source "$1"
  NGINX_ETC="$2"
  LAYOUT=debian
  SITES_AVAILABLE="$2/sites-available"
  SITES_ENABLED="$2/sites-enabled"
  CONF_D="$2/conf.d"
  find_site_file default
' _ "$SCRIPT" "$discover_root")"
if [[ "$found_default" == "$discover_root/conf.d/default" ]]; then
  ok "conf.d/default can be selected as site"
else
  not_ok "conf.d/default can be selected as site"
fi

rm -rf -- "$discover_root"

fakebin="$(mktemp -d)"
fakeetc="$(mktemp -d)"
fakebackup="$(mktemp -d)"
mkdir -p "$fakeetc/conf.d"
printf 'events {}\nhttp { include %s/conf.d/*.conf; }\n' "$fakeetc" > "$fakeetc/nginx.conf"

cat > "$fakebin/nginx" <<'EOF_NGINX'
#!/usr/bin/env bash
case "${1:-}" in
  -v) printf '%s\n' 'nginx version: nginx/1.26.0' >&2 ;;
  -t)
    if [[ "${FAKE_NGINX_INVALID:-0}" == 1 ]] || { [[ -n "${FAKE_NGINX_REJECT_PORT:-}" ]] && grep -RqsE "^[[:space:]]*listen[[:space:]].*(:|[[:space:]])${FAKE_NGINX_REJECT_PORT}([[:space:];]|$)" "${NGINX_MANAGER_ETC}/conf.d"; }; then
      printf '%s\n' 'configuration file test failed' >&2
      exit 1
    fi
    printf '%s\n' 'configuration file test is successful' >&2
    ;;
  -T)
    printf '# configuration file %s/nginx.conf:\n' "${NGINX_MANAGER_ETC}"
    cat "${NGINX_MANAGER_ETC}/nginx.conf"
    for file in "${NGINX_MANAGER_ETC}"/conf.d/*.conf; do
      [[ -f "$file" ]] || continue
      printf '# configuration file %s:\n' "$file"
      cat "$file"
    done
    ;;
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
expect_failure "global port move requires explicit target port" bash "$SCRIPT" --non-interactive --move-listen-port --yes

cat > "$fakeetc/conf.d/port-migration.conf" <<'EOF_PORT_MIGRATION'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    listen 127.0.0.1:80;
    listen 443 ssl;
    listen 9000;
    server_name migration.example;
}
EOF_PORT_MIGRATION
expect_success "global port migration" bash -c '
  source "$1"
  require_root(){ :; }
  ASSUME_YES=true
  detect_nginx_layout
  move_all_listen_ports 80 8088
' _ "$SCRIPT"
if grep -Eq '^[[:space:]]*listen[[:space:]]+(8088|\[::\]:8088|127\.0\.0\.1:8088)' "$fakeetc/conf.d/port-migration.conf"   && ! grep -Eq '^[[:space:]]*listen[[:space:]]+(80|[^[:space:];]*:80)([[:space:];]|$)' "$fakeetc/conf.d/port-migration.conf"   && grep -q 'listen 443 ssl;' "$fakeetc/conf.d/port-migration.conf"   && grep -q 'listen 9000;' "$fakeetc/conf.d/port-migration.conf"; then
  ok "global migration removes port 80 and preserves other ports"
else
  not_ok "global migration removes port 80 and preserves other ports"
fi

export FAKE_NGINX_REJECT_PORT=8099
expect_failure "global port migration rollback on invalid config" bash -c '
  source "$1"
  require_root(){ :; }
  ASSUME_YES=true
  detect_nginx_layout
  move_all_listen_ports 8088 8099
' _ "$SCRIPT"
unset FAKE_NGINX_REJECT_PORT
if grep -q 'listen 8088 default_server;' "$fakeetc/conf.d/port-migration.conf" && ! grep -q '8099' "$fakeetc/conf.d/port-migration.conf"; then
  ok "global migration rollback restores all listeners"
else
  not_ok "global migration rollback restores all listeners"
fi

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
