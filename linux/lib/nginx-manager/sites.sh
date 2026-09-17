# shellcheck shell=bash

site_config_path() {
    local domain="$1"
    if [[ "$LAYOUT" == "debian" ]]; then printf '%s/%s.conf' "$SITES_AVAILABLE" "$domain"; else printf '%s/%s.conf' "$CONF_D" "$domain"; fi
}

site_enabled() {
    local path="$1" name
    name="$(basename "$path")"
    if [[ "$LAYOUT" == "debian" ]]; then [[ -e "$SITES_ENABLED/$name" || -L "$SITES_ENABLED/$name" ]]; else [[ "$path" == *.conf ]]; fi
}

site_files() {
    local dir="$SITES_AVAILABLE"
    [[ -d "$dir" ]] || return 0
    find "$dir" -maxdepth 1 \( -type f -o -type l \) \( -name '*.conf' -o -name '*.conf.disabled' -o ! -name '*.*' \) -print 2>/dev/null | sort
}

list_sites() {
    local file domains state listen root proxy ssl
    printf '%-32s %-9s %-10s %s\n' DOMAIN STATUS PORT TYPE
    while IFS= read -r file; do
        [[ -n "$file" ]] || continue
        domains="$(sed -n -E 's/^[[:space:]]*server_name[[:space:]]+([^;]+);.*/\1/p' "$file" | head -n1)"
        [[ -n "$domains" ]] || domains="$(basename "$file" .conf)"
        listen="$(sed -n -E 's/^[[:space:]]*listen[[:space:]]+([^; ]+).*/\1/p' "$file" | head -n1)"; listen="${listen##*:}"
        root="$(sed -n -E 's/^[[:space:]]*root[[:space:]]+([^;]+);.*/\1/p' "$file" | head -n1)"
        proxy="$(sed -n -E 's/^[[:space:]]*proxy_pass[[:space:]]+([^;]+);.*/\1/p' "$file" | head -n1)"
        ssl="$(sed -n -E 's/^[[:space:]]*ssl_certificate[[:space:]]+([^;]+);.*/\1/p' "$file" | head -n1)"
        if site_enabled "$file"; then state="ENABLED"; else state="DISABLED"; fi
        if [[ -n "$proxy" ]]; then
            printf '%-32s %-9s %-10s proxy:%s%s\n' "$domains" "$state" "${listen:--}" "$proxy" "${ssl:+ SSL}"
        else
            printf '%-32s %-9s %-10s static:%s%s\n' "$domains" "$state" "${listen:--}" "${root:--}" "${ssl:+ SSL}"
        fi
    done < <(site_files)
}

find_site_file() {
    local domain="$1" file
    validate_domain "$domain" || return 1
    while IFS= read -r file; do
        if sed -n -E 's/^[[:space:]]*server_name[[:space:]]+([^;]+);.*/\1/p' "$file" | tr ' ' '\n' | grep -Fqx -- "$domain"; then
            printf '%s' "$file"; return 0
        fi
    done < <(site_files)
    file="$(site_config_path "$domain")"
    [[ -e "$file" ]] && printf '%s' "$file"
}

generate_static_site_config() {
    local domain="$1" root="$2" port="$3" php_socket="${4:-}" ssl="${5:-false}"
    cat <<EOF
server {
    listen $port;
    listen [::]:$port;
    server_name $domain;
    root $root;
    index index.html index.htm${php_socket:+ index.php};

    access_log /var/log/nginx/${domain}.access.log;
    error_log /var/log/nginx/${domain}.error.log;

    location / {
        try_files \$uri \$uri/ =404;
    }
EOF
    if [[ -n "$php_socket" ]]; then
        cat <<EOF

    location ~ \.php\$ {
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
        fastcgi_pass unix:$php_socket;
    }
EOF
    fi
    if [[ "$ssl" == true ]]; then
        cat <<EOF

    listen 443 ssl;
    listen [::]:443 ssl;
    ssl_certificate /etc/letsencrypt/live/$domain/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$domain/privkey.pem;
EOF
    fi
    printf '}\n'
}

generate_reverse_proxy_config() {
    local domain="$1" host="$2" port="$3" scheme="$4" websocket="$5" ssl="$6"
    cat <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $domain;

    access_log /var/log/nginx/${domain}.access.log;
    error_log /var/log/nginx/${domain}.error.log;

    location / {
        proxy_pass ${scheme}://${host}:${port};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 30s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
EOF
    if [[ "$websocket" == true ]]; then
        cat <<'EOF'
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
EOF
    fi
    cat <<'EOF'
    }
EOF
    if [[ "$ssl" == true ]]; then
        cat <<EOF

    listen 443 ssl;
    listen [::]:443 ssl;
    ssl_certificate /etc/letsencrypt/live/$domain/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$domain/privkey.pem;
EOF
    fi
    printf '}\n'
}

preview_file() {
    local file="$1"
    if [[ -n "$UI_BIN" ]]; then "$UI_BIN" --title "Preview configuration" --textbox "$file" 28 100; else sed -n '1,240p' "$file"; fi
}

create_site() {
    local domain="$1" root="$2" port="$3" php_socket="$4" ssl="$5"
    require_root
    validate_domain "$domain" && validate_safe_path "$root" && validate_port "$port" || die "$EXIT_ARGS" "Niepoprawne dane strony."
    [[ -z "$php_socket" ]] || validate_safe_path "$php_socket" || die "$EXIT_ARGS" "Niepoprawny PHP socket."
    local temp destination
    temp="$(make_temp)" || return "$EXIT_GENERAL"
    generate_static_site_config "$domain" "$root" "$port" "$php_socket" "$ssl" > "$temp"
    preview_file "$temp"
    if [[ -e "$(site_config_path "$domain")" ]]; then
        confirm_action "Replace configuration" "$domain" "Istniejący Virtual Host zostanie zastąpiony atomowo." || return 0
    fi
    run_command mkdir -p -- "$root" || return "$EXIT_GENERAL"
    if [[ ! -e "$root/index.html" ]]; then
        local index
        index="$(make_temp)" || return "$EXIT_GENERAL"
        printf '<!doctype html><html><head><meta charset="utf-8"><title>%s</title></head><body><h1>%s</h1></body></html>\n' "$domain" "$domain" > "$index"
        run_command install -m 0644 -- "$index" "$root/index.html" || return "$EXIT_GENERAL"
    fi
    destination="$(site_config_path "$domain")"
    atomic_write_config "$temp" "$destination" || return $?
    enable_site "$domain"
}

create_reverse_proxy() {
    local domain="$1" host="$2" port="$3" scheme="$4" websocket="$5" ssl="$6"
    require_root
    validate_domain "$domain" && validate_host "$host" && validate_port "$port" || die "$EXIT_ARGS" "Niepoprawne dane reverse proxy."
    [[ "$scheme" =~ ^https?$ ]] || die "$EXIT_ARGS" "Niepoprawny protokół backendu."
    local temp destination
    temp="$(make_temp)" || return "$EXIT_GENERAL"
    generate_reverse_proxy_config "$domain" "$host" "$port" "$scheme" "$websocket" "$ssl" > "$temp"
    preview_file "$temp"
    destination="$(site_config_path "$domain")"
    if [[ -e "$destination" ]]; then confirm_action "Replace configuration" "$domain" "Konfiguracja reverse proxy zostanie zastąpiona atomowo." || return 0; fi
    atomic_write_config "$temp" "$destination" || return $?
    enable_site "$domain"
}

enable_site() {
    local domain="$1" file name
    require_root
    file="$(find_site_file "$domain")" || die "$EXIT_ARGS" "Nie znaleziono strony $domain."
    name="$(basename "$file")"
    if [[ "$LAYOUT" == "debian" ]]; then
        mkdir -p -- "$SITES_ENABLED"
        [[ -L "$SITES_ENABLED/$name" || -e "$SITES_ENABLED/$name" ]] || ln -s -- "$file" "$SITES_ENABLED/$name"
        if ! test_nginx_config; then rm -f -- "$SITES_ENABLED/$name"; return "$EXIT_CONFIG"; fi
        reload_nginx
    elif [[ "$file" == *.disabled ]]; then
        mv -- "$file" "${file%.disabled}"
        test_nginx_config && reload_nginx || { mv -- "${file%.disabled}" "$file"; return "$EXIT_CONFIG"; }
    fi
    log_event enable_site success "$domain"
}

disable_site() {
    local domain="$1" file name
    require_root
    file="$(find_site_file "$domain")" || die "$EXIT_ARGS" "Nie znaleziono strony $domain."
    confirm_action "Disable virtual host" "$domain" "Wyłączenie strony bez usuwania jej konfiguracji." || return 0
    name="$(basename "$file")"
    if [[ "$LAYOUT" == "debian" ]]; then rm -f -- "$SITES_ENABLED/$name"; else mv -- "$file" "$file.disabled"; fi
    test_nginx_config && reload_nginx || return "$EXIT_CONFIG"
    log_event disable_site success "$domain"
}

delete_site() {
    local domain="$1" file name backup
    require_root
    file="$(find_site_file "$domain")" || die "$EXIT_ARGS" "Nie znaleziono strony $domain."
    confirm_action "Delete site" "$domain" "Konfiguracja zostanie usunięta; document root pozostanie." || return 0
    backup="$(create_backup)" || return "$EXIT_BACKUP"
    name="$(basename "$file")"
    [[ "$LAYOUT" == "debian" ]] && rm -f -- "$SITES_ENABLED/$name"
    rm -f -- "$file"
    if test_nginx_config; then reload_nginx; else error "Błąd po usunięciu. Przywróć backup: $backup"; return "$EXIT_CONFIG"; fi
    log_event delete_site success "$domain"
}

clone_site() {
    local source_domain="$1" target_domain="$2" source_file target_file temp
    validate_domain "$target_domain" || die "$EXIT_ARGS" "Niepoprawna domena docelowa."
    source_file="$(find_site_file "$source_domain")" || die "$EXIT_ARGS" "Nie znaleziono strony źródłowej."
    target_file="$(site_config_path "$target_domain")"
    [[ ! -e "$target_file" ]] || die "$EXIT_ARGS" "Strona docelowa już istnieje."
    temp="$(make_temp)" || return "$EXIT_GENERAL"
    sed "s/\b${source_domain//./\\.}\b/$target_domain/g" "$source_file" > "$temp"
    preview_file "$temp"
    atomic_write_config "$temp" "$target_file"
}

search_config() {
    local term="$1"
    [[ -n "$term" && "$term" != -* && "$term" != *$'\n'* ]] || die "$EXIT_ARGS" "Niepoprawna fraza wyszukiwania."
    grep -RInF --exclude='*.key' -- "$term" "$NGINX_ETC" 2>/dev/null || true
}

find_php_sockets() {
    find /run/php /var/run/php -maxdepth 1 -type s -name 'php*-fpm.sock' -print 2>/dev/null | sort -Vr
}

edit_config_file() {
    local file="$1" editor
    require_root
    validate_safe_path "$file" || die "$EXIT_ARGS" "Niepoprawna ścieżka."
    [[ "$file" == "$NGINX_ETC"/* || "$file" == "$MAIN_CONFIG" ]] || die "$EXIT_ARGS" "Edycja tylko wewnątrz $NGINX_ETC."
    [[ -f "$file" ]] || die "$EXIT_ARGS" "Plik nie istnieje."
    editor="${EDITOR:-}"
    if [[ -z "$editor" ]]; then
        for editor in nano vim vi; do command_exists "$editor" && break; done
    fi
    command_exists "$editor" || die "$EXIT_DEPENDENCY" "Brak edytora nano/vim/vi."
    local backup="${file}.bak.$(date +%Y%m%d%H%M%S).$$"
    cp -a -- "$file" "$backup" || return "$EXIT_BACKUP"
    "$editor" "$file"
    if test_nginx_config; then reload_nginx; rm -f -- "$backup"; else mv -f -- "$backup" "$file"; error "Błędna konfiguracja. Przywrócono poprzedni plik."; return "$EXIT_CONFIG"; fi
}

generate_security_headers() {
    cat <<'EOF'
add_header X-Content-Type-Options "nosniff" always;
add_header X-Frame-Options "SAMEORIGIN" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;
EOF
}

apply_security_headers() {
    local temp
    temp="$(make_temp)" || return "$EXIT_GENERAL"
    generate_security_headers > "$temp"
    preview_file "$temp"
    confirm_action "Security headers" "$NGINX_ETC/conf.d/99-security-headers.conf" "Dodanie podstawowych nagłówków bezpieczeństwa." || return 0
    atomic_write_config "$temp" "$NGINX_ETC/conf.d/99-security-headers.conf"
}
