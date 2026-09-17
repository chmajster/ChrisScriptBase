# shellcheck shell=bash

nginx_installed() { command_exists nginx; }

test_nginx_config() {
    if ! nginx_installed; then
        error "Nginx nie jest zainstalowany."
        return "$EXIT_DEPENDENCY"
    fi
    local output status
    output="$(nginx -t 2>&1)"; status=$?
    printf '%s\n' "$output"
    if ((status == 0)); then
        log_event test_nginx_config success
        return 0
    fi
    log_event test_nginx_config failure "$output"
    return "$EXIT_CONFIG"
}

reload_nginx() {
    require_root
    if ! test_nginx_config; then
        error "Reload anulowany: konfiguracja Nginx jest niepoprawna."
        return "$EXIT_CONFIG"
    fi
    if run_command systemctl reload nginx; then
        log_event reload_nginx success
    else
        log_event reload_nginx failure
        return "$EXIT_SERVICE"
    fi
}

restart_nginx() {
    require_root
    if ! test_nginx_config; then
        error "Restart anulowany: konfiguracja Nginx jest niepoprawna."
        return "$EXIT_CONFIG"
    fi
    if run_command systemctl restart nginx; then
        log_event restart_nginx success
    else
        log_event restart_nginx failure
        return "$EXIT_SERVICE"
    fi
}

service_action() {
    local action="$1"
    command_exists systemctl || die "$EXIT_DEPENDENCY" "Brak systemctl."
    case "$action" in
        status) systemctl status --no-pager nginx ;;
        reload) reload_nginx ;;
        restart) restart_nginx ;;
        start|stop|enable|disable)
            require_root
            run_command systemctl "$action" nginx || return "$EXIT_SERVICE"
            log_event "service_$action" success
            ;;
        *) return "$EXIT_ARGS" ;;
    esac
}

show_server_status() {
    local installed=no version="-" service="unknown" pid="-" uptime="-" workers=0 ports="-" ips="-" config_test="NOT RUN"
    if nginx_installed; then
        installed=yes
        version="$(nginx -v 2>&1 | sed 's/^nginx version: //')"
        config_test="$(nginx -t 2>&1 | tail -n2 | tr '\n' ' ')"
    fi
    if command_exists systemctl; then
        service="$(systemctl is-active nginx 2>/dev/null || true)"
        pid="$(systemctl show nginx -p MainPID --value 2>/dev/null || true)"; [[ "$pid" == 0 || -z "$pid" ]] && pid="-"
    fi
    if [[ "$pid" != "-" && -r "/proc/$pid/stat" ]]; then
        uptime="$(ps -o etime= -p "$pid" 2>/dev/null | xargs || true)"
    fi
    workers="$(pgrep -fc 'nginx: worker process' 2>/dev/null || true)"
    if command_exists ss; then ports="$(ss -ltnp 2>/dev/null | awk '/nginx/ {print $4}' | sort -u | paste -sd, -)"; fi
    ips="$(hostname -I 2>/dev/null | xargs || true)"
    cat <<EOF
Nginx installed: $installed
Version:         $version
Systemd status:  ${service:-unknown}
PID:             $pid
Process uptime:  ${uptime:--}
Main config:     $MAIN_CONFIG
Listen sockets:  ${ports:--}
Local IP:        ${ips:--}
Worker count:    ${workers:-0}
Access log:      $NGINX_ETC/../log/nginx/access.log (typowo /var/log/nginx/access.log)
Error log:       $NGINX_ETC/../log/nginx/error.log (typowo /var/log/nginx/error.log)
Last config test: $config_test
EOF
}

install_packages() {
    require_root
    [[ -n "$PKG_MANAGER" ]] || die "$EXIT_DEPENDENCY" "Nie wykryto apt, dnf ani yum."
    case "$PKG_MANAGER" in
        apt-get) run_command apt-get update && run_command apt-get install -y "$@" ;;
        dnf) run_command dnf install -y "$@" ;;
        yum) run_command yum install -y "$@" ;;
    esac
}

install_nginx() {
    if nginx_installed; then info "Nginx jest już zainstalowany."; return 0; fi
    install_packages nginx || die "$EXIT_GENERAL" "Instalacja Nginx nie powiodła się."
    run_command systemctl enable --now nginx || return "$EXIT_SERVICE"
    log_event install_nginx success
}

update_nginx() {
    require_root
    nginx_installed || die "$EXIT_DEPENDENCY" "Nginx nie jest zainstalowany."
    create_backup >/dev/null || return "$EXIT_BACKUP"
    test_nginx_config || return "$EXIT_CONFIG"
    case "$PKG_MANAGER" in
        apt-get) run_command apt-get update && run_command apt-get install --only-upgrade -y nginx ;;
        dnf) run_command dnf upgrade -y nginx ;;
        yum) run_command yum update -y nginx ;;
        *) die "$EXIT_DEPENDENCY" "Nie wykryto managera pakietów." ;;
    esac
    test_nginx_config || return "$EXIT_CONFIG"
    systemctl status --no-pager nginx || true
    log_event update_nginx success
}

remove_nginx() {
    local purge="$1"
    require_root
    nginx_installed || { info "Nginx nie jest zainstalowany."; return 0; }
    create_backup >/dev/null || return "$EXIT_BACKUP"
    local label="Remove Nginx" detail="Usunięcie pakietu Nginx; konfiguracja pozostanie."
    [[ "$purge" == true ]] && label="Purge Nginx" && detail="Usunięcie pakietu i konfiguracji po wykonaniu backupu."
    confirm_action "$label" nginx "$detail" || return 0
    case "$PKG_MANAGER" in
        apt-get)
            if [[ "$purge" == true ]]; then run_command apt-get purge -y nginx nginx-common; else run_command apt-get remove -y nginx; fi
            ;;
        dnf|yum) run_command "$PKG_MANAGER" remove -y nginx ;;
        *) die "$EXIT_DEPENDENCY" "Nie wykryto managera pakietów." ;;
    esac
    if [[ "$purge" == true && -d "$NGINX_ETC" ]]; then
        run_command rm -rf --one-file-system -- "$NGINX_ETC"
    fi
    log_event remove_nginx success "purge=$purge"
}

create_backup() {
    require_root
    [[ -d "$NGINX_ETC" ]] || die "$EXIT_BACKUP" "Brak katalogu $NGINX_ETC."
    local stamp target
    stamp="$(date '+%Y-%m-%d_%H%M%S')"
    target="$BACKUP_DIR/nginx-$stamp.tar.gz"
    run_command mkdir -p -- "$BACKUP_DIR" || return "$EXIT_BACKUP"
    if [[ "$DRY_RUN" == true ]]; then
        run_command tar -C "$(dirname "$NGINX_ETC")" -czf "$target" "$(basename "$NGINX_ETC")"
    elif tar -C "$(dirname "$NGINX_ETC")" -czf "$target" "$(basename "$NGINX_ETC")"; then
        chmod 600 "$target" 2>/dev/null || true
    else
        log_event backup failure
        return "$EXIT_BACKUP"
    fi
    log_event backup success "$target"
    printf '%s\n' "$target"
}

list_backups() {
    [[ -d "$BACKUP_DIR" ]] || return 0
    find "$BACKUP_DIR" -maxdepth 1 -type f -name 'nginx-*.tar.gz' -printf '%f\n' 2>/dev/null | sort -r
}

restore_backup() {
    local archive="$1"
    require_root
    validate_safe_path "$archive" || die "$EXIT_BACKUP" "Niepoprawna ścieżka backupu."
    [[ -f "$archive" && "$archive" == "$BACKUP_DIR"/* ]] || die "$EXIT_BACKUP" "Backup musi pochodzić z $BACKUP_DIR."
    if tar -tzf "$archive" | awk '/(^|\/)\.\.($|\/)|^\// {bad=1} END{exit bad?0:1}'; then
        die "$EXIT_BACKUP" "Backup zawiera niebezpieczne ścieżki."
    fi
    confirm_action "Restore backup" "$archive" "Aktualna konfiguracja zostanie zarchiwizowana, następnie zastąpiona." || return 0
    local safety staging base
    safety="$(create_backup)" || return "$EXIT_BACKUP"
    staging="$(mktemp -d "${TMPDIR:-/tmp}/nginx-restore.XXXXXX")" || return "$EXIT_BACKUP"
    base="$(basename "$NGINX_ETC")"
    if ! tar -xzf "$archive" -C "$staging" --no-same-owner --no-same-permissions || [[ ! -d "$staging/$base" ]]; then
        rm -rf -- "$staging"; return "$EXIT_BACKUP"
    fi
    local old="${NGINX_ETC}.nginx-manager-old.$$"
    mv -- "$NGINX_ETC" "$old" && mv -- "$staging/$base" "$NGINX_ETC"
    rm -rf -- "$staging"
    if test_nginx_config; then
        rm -rf -- "$old"
        reload_nginx
        log_event restore success "$archive"
        return 0
    fi
    rm -rf -- "$NGINX_ETC"
    mv -- "$old" "$NGINX_ETC"
    error "Przywrócona konfiguracja była błędna. Wykonano rollback. Backup bezpieczeństwa: $safety"
    log_event restore rollback "$archive"
    return "$EXIT_CONFIG"
}

atomic_write_config() {
    local source="$1" destination="$2"
    require_root
    validate_safe_path "$destination" || die "$EXIT_ARGS" "Niebezpieczna ścieżka docelowa."
    [[ "$destination" == "$NGINX_ETC"/* ]] || die "$EXIT_ARGS" "Plik musi znajdować się w $NGINX_ETC."
    local parent backup="" staged
    parent="$(dirname "$destination")"
    if [[ "$DRY_RUN" == true ]]; then
        run_command install -D -m 0644 -- "$source" "$destination"
        printf 'DRY RUN: nginx -t && systemctl reload nginx\n'
        return 0
    fi
    run_command mkdir -p -- "$parent" || return "$EXIT_GENERAL"
    staged="$(mktemp "$parent/.nginx-manager.XXXXXX")" || return "$EXIT_GENERAL"
    if ! install -m 0644 -- "$source" "$staged"; then return "$EXIT_GENERAL"; fi
    if [[ -e "$destination" || -L "$destination" ]]; then
        backup="${destination}.bak.$(date +%Y%m%d%H%M%S).$$"
        cp -a -- "$destination" "$backup" || return "$EXIT_BACKUP"
    fi
    mv -f -- "$staged" "$destination" || return "$EXIT_GENERAL"
    local test_log
    test_log="$(make_temp)" || return "$EXIT_GENERAL"
    if test_nginx_config >"$test_log" 2>&1; then
        reload_nginx
        [[ -z "$backup" ]] || rm -f -- "$backup"
        log_event atomic_write success "$destination"
        return 0
    fi
    local test_output
    test_output="$(cat "$test_log" 2>/dev/null || true)"
    if [[ -n "$backup" ]]; then mv -f -- "$backup" "$destination"; else rm -f -- "$destination"; fi
    error "Test konfiguracji nie przeszedł. Przywrócono poprzedni plik."
    printf '%s\n' "$test_output" >&2
    log_event atomic_write rollback "$destination"
    return "$EXIT_CONFIG"
}

show_ports() {
    if command_exists ss; then ss -tulpn; elif command_exists lsof; then lsof -nP -iTCP -sTCP:LISTEN; else die "$EXIT_DEPENDENCY" "Brak ss i lsof."; fi
}

generate_diagnostic_report() {
    local output="${1:-}"
    [[ -n "$output" ]] || output="/tmp/nginx-diagnostic-$(date +%Y%m%d-%H%M%S).txt"
    validate_safe_path "$output" || die "$EXIT_ARGS" "Niepoprawna ścieżka raportu."
    {
        printf 'ChrisScriptBase Nginx Diagnostic\nGenerated: %s\n\n' "$(date -Is)"
        printf '== OS ==\n'; cat /etc/os-release 2>/dev/null || true
        printf '\n== Kernel ==\n'; uname -a
        printf '\n== Status ==\n'; show_server_status
        printf '\n== Configuration test ==\n'; nginx -t 2>&1 || true
        printf '\n== Listening ports ==\n'; show_ports 2>&1 || true
        printf '\n== Virtual hosts ==\n'; list_sites 2>&1 || true
        printf '\n== Active listen/server_name/root/proxy/SSL directives ==\n'
        nginx -T 2>&1 | sed -n -E '/^[[:space:]]*(listen|server_name|root|proxy_pass|ssl_certificate)[[:space:]]/p' || true
        printf '\n== SSL certificates ==\n'; list_certificates 2>&1 || true
        printf '\n== Disk ==\n'; df -h "$NGINX_ETC" 2>&1 || true
        printf '\n== Memory ==\n'; free -h 2>&1 || true
        printf '\n== Recent errors ==\n'; tail -n 100 /var/log/nginx/error.log 2>&1 || true
        printf '\n== Conflict checks ==\n'; diagnose_config_conflicts
    } > "$output" || return "$EXIT_GENERAL"
    chmod 600 "$output" 2>/dev/null || true
    log_event diagnostic success "$output"
    printf '%s\n' "$output"
}

diagnose_config_conflicts() {
    local dump
    dump="$(nginx -T 2>&1 || true)"
    printf '%s\n' "$dump" | sed -n -E 's/^[[:space:]]*server_name[[:space:]]+([^;]+);/\1/p' | tr ' ' '\n' | sort | uniq -d | sed 's/^/[WARNING] duplicate server_name: /'
    printf '%s\n' "$dump" | sed -n -E 's/^[[:space:]]*root[[:space:]]+([^;]+);/\1/p' | while IFS= read -r root; do
        [[ "$root" == *'$'* ]] && continue
        [[ -d "$root" ]] || printf '[WARNING] missing document root: %s\n' "$root"
    done
    printf '%s\n' "$dump" | sed -n -E 's/^[[:space:]]*ssl_certificate(_key)?[[:space:]]+([^;]+);/\2/p' | while IFS= read -r cert; do
        [[ "$cert" == *'$'* ]] && continue
        [[ -f "$cert" ]] || printf '[ERROR] missing certificate file: %s\n' "$cert"
    done
}
