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

site_primary_domain() {
    local file="$1" domain
    while IFS= read -r domain; do
        validate_domain "$domain" && { printf '%s' "$domain"; return 0; }
    done < <(sed -n -E 's/^[[:space:]]*server_name[[:space:]]+([^;]+);.*/\1/p' "$file" | tr ' ' '\n')
    domain="$(basename "$file")"
    domain="${domain%.disabled}"
    domain="${domain%.conf}"
    validate_domain "$domain" && printf '%s' "$domain"
}

site_http_port() {
    local file="$1"
    awk '
      /^[[:space:]]*listen[[:space:]]/ && $0 !~ /(^|[[:space:]])ssl([[:space:];]|$)/ {
        line = $0
        sub(/^[[:space:]]*listen[[:space:]]+/, "", line)
        match(line, /^[^[:space:];]+/)
        endpoint = substr(line, RSTART, RLENGTH)
        if (endpoint ~ /:[0-9]+$/) sub(/^.*:/, "", endpoint)
        if (endpoint ~ /^[0-9]+$/) { print endpoint; exit }
      }
    ' "$file"
}

site_choice_rows() {
    local file primary domains state port
    while IFS= read -r file; do
        [[ -n "$file" ]] || continue
        primary="$(site_primary_domain "$file" || true)"
        [[ -n "$primary" ]] || continue
        domains="$(sed -n -E 's/^[[:space:]]*server_name[[:space:]]+([^;]+);.*/\1/p' "$file" | head -n1)"
        [[ -n "$domains" ]] || domains="$primary"
        if site_enabled "$file"; then state="ENABLED"; else state="DISABLED"; fi
        port="$(site_http_port "$file")"
        printf '%s\t%s | %s | port %s | %s\n' "$primary" "$domains" "$state" "${port:--}" "$(basename "$file")"
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

find_default_site_file() {
    local candidate file resolved
    for candidate in "$SITES_ENABLED/default" "$SITES_ENABLED/default.conf"; do
        if [[ -f "$candidate" ]]; then
            resolved="$(readlink -f -- "$candidate" 2>/dev/null || printf '%s' "$candidate")"
            [[ "$resolved" == "$NGINX_ETC"/* ]] && { printf '%s' "$resolved"; return 0; }
        fi
    done
    if [[ -d "$SITES_ENABLED" ]]; then
        while IFS= read -r file; do
            if grep -Eq '^[[:space:]]*listen[[:space:]].*default_server([[:space:];]|$)' "$file"; then
                resolved="$(readlink -f -- "$file" 2>/dev/null || printf '%s' "$file")"
                [[ "$resolved" == "$NGINX_ETC"/* ]] && { printf '%s' "$resolved"; return 0; }
            fi
        done < <(find "$SITES_ENABLED" -maxdepth 1 \( -type f -o -type l \) -print 2>/dev/null | sort)
    fi
    for candidate in "$SITES_AVAILABLE/default" "$SITES_AVAILABLE/default.conf" "$CONF_D/default.conf"; do
        [[ -f "$candidate" ]] && { printf '%s' "$candidate"; return 0; }
    done
    while IFS= read -r file; do
        if grep -Eq '^[[:space:]]*listen[[:space:]].*default_server([[:space:];]|$)' "$file"; then
            printf '%s' "$file"
            return 0
        fi
    done < <(site_files)
    return 1
}

rewrite_http_listen_ports() {
    local source="$1" port="$2"
    awk -v port="$port" '
      /^[[:space:]]*listen[[:space:]]/ && $0 !~ /(^|[[:space:]])ssl([[:space:];]|$)/ {
        match($0, /^[[:space:]]*/)
        indent = substr($0, RSTART, RLENGTH)
        body = $0
        sub(/^[[:space:]]*listen[[:space:]]+/, "", body)
        match(body, /^[^[:space:];]+/)
        endpoint = substr(body, RSTART, RLENGTH)
        tail = substr(body, RLENGTH + 1)
        if (endpoint ~ /^[0-9]+$/) {
          endpoint = port
        } else if (endpoint ~ /:[0-9]+$/) {
          sub(/:[0-9]+$/, ":" port, endpoint)
        }
        print indent "listen " endpoint tail
        changed = 1
        next
      }
      { print }
      END { if (!changed) exit 42 }
    ' "$source"
}

rewrite_specific_listen_port() {
    local source="$1" from_port="$2" to_port="$3"
    awk -v from="$from_port" -v to="$to_port" '
      /^[[:space:]]*listen[[:space:]]/ {
        match($0, /^[[:space:]]*/)
        indent = substr($0, RSTART, RLENGTH)
        body = $0
        sub(/^[[:space:]]*listen[[:space:]]+/, "", body)
        match(body, /^[^[:space:];]+/)
        endpoint = substr(body, RSTART, RLENGTH)
        tail = substr(body, RLENGTH + 1)

        matched = 0
        if (endpoint == from) {
          endpoint = to
          matched = 1
        } else if (endpoint ~ (":" from "$")) {
          sub(":" from "$", ":" to, endpoint)
          matched = 1
        }

        if (matched) {
          print indent "listen " endpoint tail
          changed = 1
          next
        }
      }
      { print }
      END { if (!changed) exit 42 }
    ' "$source"
}

config_listens_on_port() {
    local file="$1" port="$2"
    awk -v port="$port" '
      /^[[:space:]]*listen[[:space:]]/ {
        body = $0
        sub(/^[[:space:]]*listen[[:space:]]+/, "", body)
        match(body, /^[^[:space:];]+/)
        endpoint = substr(body, RSTART, RLENGTH)
        if (endpoint == port || endpoint ~ (":" port "$")) exit 0
      }
      END { exit 1 }
    ' "$file"
}

nginx_dump_listens_on_port() {
    local port="$1" dump
    dump="$(nginx -T 2>&1)" || return "$EXIT_CONFIG"
    printf '%s\n' "$dump" | awk -v port="$port" '
      /^[[:space:]]*listen[[:space:]]/ {
        body = $0
        sub(/^[[:space:]]*listen[[:space:]]+/, "", body)
        match(body, /^[^[:space:];]+/)
        endpoint = substr(body, RSTART, RLENGTH)
        if (endpoint == port || endpoint ~ (":" port "$")) exit 0
      }
      END { exit 1 }
    '
}

active_nginx_config_files() {
    local dump file resolved found=false
    nginx_installed || die "$EXIT_DEPENDENCY" "Nginx nie jest zainstalowany."
    if ! dump="$(nginx -T 2>&1)"; then
        printf '%s\n' "$dump" >&2
        return "$EXIT_CONFIG"
    fi

    while IFS= read -r file; do
        [[ -n "$file" && -f "$file" ]] || continue
        resolved="$(readlink -f -- "$file" 2>/dev/null || printf '%s' "$file")"
        printf '%s\n' "$resolved"
        found=true
    done < <(printf '%s\n' "$dump" | sed -n -E 's/^# configuration file (.*):$/\1/p')

    if [[ "$found" == false ]]; then
        [[ -f "$MAIN_CONFIG" ]] && printf '%s\n' "$(readlink -f -- "$MAIN_CONFIG" 2>/dev/null || printf '%s' "$MAIN_CONFIG")"
        if [[ -d "$SITES_ENABLED" ]]; then
            while IFS= read -r file; do
                resolved="$(readlink -f -- "$file" 2>/dev/null || true)"
                [[ -n "$resolved" && -f "$resolved" ]] && printf '%s\n' "$resolved"
            done < <(find "$SITES_ENABLED" -maxdepth 1 \( -type f -o -type l \) -print 2>/dev/null)
        fi
        if [[ -d "$CONF_D" && "$CONF_D" != "$SITES_ENABLED" ]]; then
            find "$CONF_D" -maxdepth 1 -type f -name '*.conf' -print 2>/dev/null
        fi
    fi
}

move_all_listen_ports() {
    local from_port="$1" to_port="$2" files_output file temp status backup_archive i
    local -a files=() affected=() external=() originals=() staged=()

    require_root
    validate_port "$from_port" || die "$EXIT_ARGS" "Niepoprawny port źródłowy: $from_port"
    validate_port "$to_port" || die "$EXIT_ARGS" "Niepoprawny port docelowy: $to_port"
    [[ "$from_port" != "$to_port" ]] || die "$EXIT_ARGS" "Port źródłowy i docelowy muszą być różne."
    nginx_installed || die "$EXIT_DEPENDENCY" "Nginx nie jest zainstalowany."
    test_nginx_config >/dev/null || return "$EXIT_CONFIG"

    files_output="$(active_nginx_config_files)" || return $?
    if [[ -n "$files_output" ]]; then
        mapfile -t files < <(printf '%s\n' "$files_output" | sort -u)
    fi

    for file in "${files[@]}"; do
        [[ -f "$file" ]] || continue
        if config_listens_on_port "$file" "$from_port"; then
            if [[ "$file" == "$NGINX_ETC"/* ]]; then
                affected+=("$file")
            else
                external+=("$file")
            fi
        fi
    done

    if (("${#external[@]}" > 0)); then
        error "Aktywna konfiguracja spoza $NGINX_ETC nadal nasłuchuje na porcie $from_port:"
        printf '  %s\n' "${external[@]}" >&2
        die "$EXIT_CONFIG" "Przerwano, aby nie pozostawić częściowej migracji."
    fi

    if (("${#affected[@]}" == 0)); then
        info "Brak aktywnych dyrektyw listen na porcie $from_port."
        return 0
    fi

    info "Aktywne pliki wymagające zmiany portu $from_port -> $to_port:"
    printf '  %s\n' "${affected[@]}"
    confirm_action "Move Nginx listen port" "$from_port -> $to_port" "Wszystkie aktywne dyrektywy listen na porcie $from_port zostaną przeniesione na $to_port. Inne porty pozostaną bez zmian." || return 0

    if [[ "$DRY_RUN" == true ]]; then
        for file in "${affected[@]}"; do
            temp="$(make_temp)" || return "$EXIT_GENERAL"
            rewrite_specific_listen_port "$file" "$from_port" "$to_port" > "$temp"; status=$?
            ((status == 0)) || return "$EXIT_GENERAL"
            printf '\n--- DRY RUN: %s ---\n' "$file"
            diff -u -- "$file" "$temp" || true
        done
        printf '\nDRY RUN: nginx -t && systemctl reload nginx\n'
        return 0
    fi

    backup_archive="$(create_backup)" || return "$EXIT_BACKUP"

    for file in "${affected[@]}"; do
        temp="$(make_temp)" || return "$EXIT_GENERAL"
        rm -f -- "$temp"
        cp -a -- "$file" "$temp" || return "$EXIT_BACKUP"
        originals+=("$temp")

        temp="$(make_temp)" || return "$EXIT_GENERAL"
        rewrite_specific_listen_port "$file" "$from_port" "$to_port" > "$temp"; status=$?
        if ((status != 0)); then
            error "Nie udało się przygotować zmiany dla $file."
            return "$EXIT_GENERAL"
        fi
        staged+=("$temp")
    done

    for ((i=0; i<${#affected[@]}; i++)); do
        cat -- "${staged[$i]}" > "${affected[$i]}" || {
            for ((i=0; i<${#originals[@]}; i++)); do cat -- "${originals[$i]}" > "${affected[$i]}" 2>/dev/null || true; done
            return "$EXIT_GENERAL"
        }
    done

    if ! test_nginx_config >/dev/null 2>&1 || nginx_dump_listens_on_port "$from_port"; then
        for ((i=0; i<${#originals[@]}; i++)); do cat -- "${originals[$i]}" > "${affected[$i]}" 2>/dev/null || true; done
        error "Migracja portu nie przeszła walidacji. Przywrócono wszystkie zmienione pliki. Backup: $backup_archive"
        test_nginx_config >/dev/null 2>&1 || true
        log_event move_listen_port rollback "$from_port->$to_port"
        return "$EXIT_CONFIG"
    fi

    if ! reload_nginx; then
        for ((i=0; i<${#originals[@]}; i++)); do cat -- "${originals[$i]}" > "${affected[$i]}" 2>/dev/null || true; done
        test_nginx_config >/dev/null 2>&1 && reload_nginx >/dev/null 2>&1 || true
        error "Reload po migracji nie powiódł się. Przywrócono poprzednią konfigurację. Backup: $backup_archive"
        log_event move_listen_port rollback "$from_port->$to_port reload"
        return "$EXIT_SERVICE"
    fi

    info "Nginx nie ma już aktywnych dyrektyw listen na porcie $from_port. Nowy port: $to_port."
    info "Backup przed zmianą: $backup_archive"
    log_event move_listen_port success "$from_port->$to_port files=${#affected[@]}"
}

change_site_port() {
    local domain="$1" port="$2" file temp status
    require_root
    validate_domain "$domain" || die "$EXIT_ARGS" "Niepoprawna domena."
    validate_port "$port" || die "$EXIT_ARGS" "Niepoprawny port."
    file="$(find_site_file "$domain")" || die "$EXIT_ARGS" "Nie znaleziono strony $domain."
    temp="$(make_temp)" || return "$EXIT_GENERAL"
    rewrite_http_listen_ports "$file" "$port" > "$temp"; status=$?
    if ((status == 42)); then
        die "$EXIT_ARGS" "Virtual Host nie zawiera dyrektywy listen HTTP możliwej do zmiany."
    elif ((status != 0)); then
        return "$EXIT_GENERAL"
    fi
    if cmp -s -- "$file" "$temp"; then
        info "Port strony $domain jest już ustawiony na $port."
        return 0
    fi
    preview_file "$temp"
    confirm_action "Change site port" "$domain" "Port HTTP zostanie zmieniony na $port; porty SSL pozostaną bez zmian." || return 0
    atomic_write_config "$temp" "$file"
    log_event change_site_port success "$domain:$port"
}

change_default_port() {
    local port="$1" file temp status created=false
    require_root
    validate_port "$port" || die "$EXIT_ARGS" "Niepoprawny port."
    if file="$(find_default_site_file)"; then
        temp="$(make_temp)" || return "$EXIT_GENERAL"
        rewrite_http_listen_ports "$file" "$port" > "$temp"; status=$?
        if ((status == 42)); then
            die "$EXIT_ARGS" "Domyślny Virtual Host nie zawiera dyrektywy listen HTTP."
        elif ((status != 0)); then
            return "$EXIT_GENERAL"
        fi
    else
        created=true
        if [[ "$LAYOUT" == "debian" ]]; then file="$SITES_AVAILABLE/default"; else file="$CONF_D/default.conf"; fi
        temp="$(make_temp)" || return "$EXIT_GENERAL"
        cat > "$temp" <<EOF
server {
    listen $port default_server;
    listen [::]:$port default_server;
    server_name _;
    return 444;
}
EOF
    fi
    if [[ "$created" == false ]] && cmp -s -- "$file" "$temp"; then
        info "Domyślny port Nginx jest już ustawiony na $port."
        return 0
    fi
    preview_file "$temp"
    confirm_action "Change default Nginx port" "$file" "Domyślny port HTTP zostanie ustawiony na $port." || return 0
    atomic_write_config "$temp" "$file" || return $?
    if [[ "$LAYOUT" == "debian" ]]; then
        local name
        name="$(basename "$file")"
        mkdir -p -- "$SITES_ENABLED"
        [[ -e "$SITES_ENABLED/$name" || -L "$SITES_ENABLED/$name" ]] || ln -s -- "$file" "$SITES_ENABLED/$name"
        if ! test_nginx_config; then
            rm -f -- "$SITES_ENABLED/$name"
            return "$EXIT_CONFIG"
        fi
        reload_nginx
    fi
    log_event change_default_port success "$port"
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
    local domain="$1" host="$2" port="$3" scheme="$4" websocket="$5" ssl="$6" listen_port="${7:-80}"
    cat <<EOF
server {
    listen $listen_port;
    listen [::]:$listen_port;
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
    local domain="$1" host="$2" port="$3" scheme="$4" websocket="$5" ssl="$6" listen_port="${7:-80}"
    require_root
    validate_domain "$domain" && validate_host "$host" && validate_port "$port" && validate_port "$listen_port" || die "$EXIT_ARGS" "Niepoprawne dane reverse proxy."
    [[ "$scheme" =~ ^https?$ ]] || die "$EXIT_ARGS" "Niepoprawny protokół backendu."
    local temp destination
    temp="$(make_temp)" || return "$EXIT_GENERAL"
    generate_reverse_proxy_config "$domain" "$host" "$port" "$scheme" "$websocket" "$ssl" "$listen_port" > "$temp"
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
