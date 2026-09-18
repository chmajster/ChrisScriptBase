# shellcheck shell=bash

ensure_dialog() {
    if command_exists dialog; then UI_BIN="dialog"; return 0; fi
    if [[ $EUID -eq 0 && -n "$PKG_MANAGER" ]]; then
        printf 'Pakiet dialog nie jest zainstalowany. Zainstalować? [y/N]: '
        local answer; read -r answer
        if [[ "$answer" =~ ^[YyTt]$ ]]; then install_packages dialog || true; fi
    fi
    command_exists dialog && UI_BIN="dialog" && return 0
    die "$EXIT_DEPENDENCY" "Brak programu dialog. Użyj trybu CLI, np. --status lub zainstaluj dialog."
}

ui_menu() {
    local title="$1" prompt="$2"; shift 2
    "$UI_BIN" --clear --cancel-label "Powrót" --title "$title" --menu "$prompt" 28 100 18 "$@" 3>&1 1>&2 2>&3
}

ui_input() {
    local title="$1" prompt="$2" default="${3:-}"
    "$UI_BIN" --cancel-label "Anuluj" --title "$title" --inputbox "$prompt" 11 90 "$default" 3>&1 1>&2 2>&3
}

ui_yesno() { "$UI_BIN" --yes-label "Wykonaj" --no-label "Anuluj" --title "$1" --yesno "$2" 13 90; }
ui_msg() { "$UI_BIN" --title "$1" --msgbox "$2" 24 100; }

ui_text() {
    local title="$1" content="$2" file
    file="$(make_temp)" || return 1
    printf '%s\n' "$content" > "$file"
    "$UI_BIN" --title "$title" --textbox "$file" 30 110
}

ui_select_site() {
    local title="$1" domain description
    local -a options=()
    while IFS=$'\t' read -r domain description; do
        [[ -n "$domain" ]] || continue
        options+=("$domain" "$description")
    done < <(site_choice_rows)
    if ((${#options[@]} == 0)); then
        ui_msg "$title" "Nie znaleziono skonfigurowanych Virtual Hostów."
        return 1
    fi
    ui_menu "$title" "Wybierz stronę z listy:" "${options[@]}"
}

gui_status() {
    local action output
    while true; do
        action="$(ui_menu "Status serwera" "$(show_server_status 2>&1)" start Start stop Stop restart Restart reload Reload enable "Enable autostart" disable "Disable autostart" back Powrót || true)"
        case "$action" in
            start|stop|restart|reload|enable|disable) output="$(service_action "$action" 2>&1 || true)"; ui_text "Wynik" "$output" ;;
            *) break ;;
        esac
    done
}

gui_site_wizard() {
    local domain root port php_choice php_socket="" ssl=false temp
    domain="$(ui_input "Nowa strona" "Domena:" "example.com" || true)"; [[ -n "$domain" ]] || return 0
    validate_domain "$domain" || { ui_msg "Błąd" "Niepoprawna domena."; return 0; }
    root="$(ui_input "Nowa strona" "Document root:" "/var/www/$domain" || true)"; [[ -n "$root" ]] || return 0
    port="$(ui_input "Nowa strona" "Port HTTP:" "80" || true)"; [[ -n "$port" ]] || return 0
    if ui_yesno "PHP-FPM" "Włączyć PHP-FPM?"; then
        local -a options=(); local socket index=1
        while IFS= read -r socket; do options+=("$index" "$socket"); ((index+=1)); done < <(find_php_sockets)
        if ((${#options[@]})); then
            php_choice="$(ui_menu "PHP-FPM" "Wybierz socket" "${options[@]}" || true)"
            [[ -n "$php_choice" ]] && php_socket="${options[$((php_choice*2-1))]}"
        else
            ui_msg "PHP-FPM" "Nie znaleziono socketów PHP-FPM."
        fi
    fi
    ui_yesno "HTTPS" "Dodać dyrektywy HTTPS dla plików Let's Encrypt?" && ssl=true
    temp="$(make_temp)" || return 1
    generate_static_site_config "$domain" "$root" "$port" "$php_socket" "$ssl" > "$temp"
    preview_file "$temp" || return 0
    ui_yesno "Zapis konfiguracji" "Zapisać i włączyć Virtual Host $domain?" || return 0
    ASSUME_YES=true create_site "$domain" "$root" "$port" "$php_socket" "$ssl"
    ui_msg "Virtual Host" "Strona $domain została skonfigurowana."
}

gui_sites() {
    local action domain target file
    while true; do
        action="$(ui_menu "Strony / Virtual Hosts" "Wybierz operację" list "Lista stron" add "Dodaj stronę" port "Zmień port strony" edit "Edytuj stronę" delete "Usuń stronę" enable "Włącz stronę" disable "Wyłącz stronę" show "Pokaż konfigurację" test "Testuj konfigurację" clone "Klonuj konfigurację" search "Wyszukaj w konfiguracji" back Powrót || true)"
        case "$action" in
            list) ui_text "Virtual Hosts" "$(list_sites 2>&1)" ;;
            add) gui_site_wizard ;;
            port)
                domain="$(ui_select_site "Port strony" || true)"; [[ -n "$domain" ]] || continue
                target="$(ui_input "Port strony" "Nowy port HTTP:" "8080" || true)"; [[ -n "$target" ]] || continue
                ui_yesno "Zmiana portu" "Zmienić port HTTP strony $domain na $target? Port HTTPS pozostanie bez zmian." && ASSUME_YES=true change_site_port "$domain" "$target"
                ;;
            edit) domain="$(ui_select_site "Edycja" || true)"; [[ -n "$domain" ]] && { file="$(find_site_file "$domain" 2>/dev/null || true)"; [[ -n "$file" ]] && edit_config_file "$file" || ui_msg "Błąd" "Nie znaleziono strony."; } ;;
            delete|enable|disable|show)
                domain="$(ui_select_site "Virtual Host" || true)"; [[ -n "$domain" ]] || continue
                case "$action" in
                    delete) ui_yesno "Delete site" "Usunąć konfigurację $domain? Document root pozostanie." && ASSUME_YES=true delete_site "$domain" ;;
                    enable) enable_site "$domain" ;;
                    disable) ui_yesno "Disable site" "Wyłączyć $domain?" && ASSUME_YES=true disable_site "$domain" ;;
                    show) file="$(find_site_file "$domain" 2>/dev/null || true)"; [[ -n "$file" ]] && ui_text "$domain" "$(cat "$file")" || ui_msg "Błąd" "Nie znaleziono strony." ;;
                esac
                ;;
            test) ui_text "nginx -t" "$(test_nginx_config 2>&1 || true)" ;;
            clone)
                domain="$(ui_select_site "Klonowanie — strona źródłowa" || true)"; [[ -n "$domain" ]] || continue
                target="$(ui_input "Klonowanie" "Domena docelowa:" "" || true)"; [[ -n "$target" ]] && clone_site "$domain" "$target"
                ;;
            search) target="$(ui_input "Wyszukiwanie" "Domena, port, proxy_pass, document root lub IP:" "" || true)"; [[ -n "$target" ]] && ui_text "Wyniki" "$(search_config "$target")" ;;
            *) break ;;
        esac
    done
}

gui_proxy_wizard() {
    local domain host port listen_port scheme websocket=false ssl=false
    domain="$(ui_input "Reverse Proxy" "Domena:" "proxy.example.com" || true)"; [[ -n "$domain" ]] || return 0
    listen_port="$(ui_input "Reverse Proxy" "Port wejściowy Nginx (listen):" "80" || true)"; [[ -n "$listen_port" ]] || return 0
    host="$(ui_input "Reverse Proxy" "Backend host:" "127.0.0.1" || true)"; [[ -n "$host" ]] || return 0
    port="$(ui_input "Reverse Proxy" "Backend port:" "8080" || true)"; [[ -n "$port" ]] || return 0
    scheme="$(ui_menu "Reverse Proxy" "Protokół backendu" http HTTP https HTTPS || true)"; [[ -n "$scheme" ]] || return 0
    ui_yesno "WebSocket" "Dodać obsługę WebSocket?" && websocket=true
    ui_yesno "SSL" "Dodać dyrektywy SSL dla Let's Encrypt?" && ssl=true
    ASSUME_YES=true create_reverse_proxy "$domain" "$host" "$port" "$scheme" "$websocket" "$ssl" "$listen_port"
    ui_msg "Reverse Proxy" "Konfiguracja $domain została zapisana na porcie wejściowym $listen_port."
}

gui_ssl() {
    local action domain email days cert key
    while true; do
        action="$(ui_menu "SSL / HTTPS" "Wybierz operację" list "Lista certyfikatów" certbot "Certbot / Let's Encrypt" existing "Dodaj istniejący certyfikat" self "Certyfikat self-signed" renew "Odnowienie certyfikatów" check "Sprawdź ważność certyfikatów" back Powrót || true)"
        case "$action" in
            list|check) ui_text "Certyfikaty" "$(list_certificates 2>&1)" ;;
            certbot) domain="$(ui_select_site "Certbot — wybierz stronę" || true)"; email="$(ui_input "Certbot" "E-mail Let's Encrypt:" "" || true)"; [[ -n "$domain" && -n "$email" ]] && ASSUME_YES=true obtain_letsencrypt_certificate "$domain" "$email" ;;
            existing) domain="$(ui_select_site "SSL — wybierz stronę" || true)"; cert="$(ui_input "SSL" "Plik certyfikatu:" "" || true)"; key="$(ui_input "SSL" "Plik klucza prywatnego:" "" || true)"; [[ -n "$domain" && -n "$cert" && -n "$key" ]] && add_existing_certificate "$domain" "$cert" "$key" ;;
            self) domain="$(ui_input "Self-signed" "Domena:" "" || true)"; days="$(ui_input "Self-signed" "Ważność w dniach:" "365" || true)"; [[ -n "$domain" ]] && ASSUME_YES=true generate_self_signed_certificate "$domain" "$days" ;;
            renew) ui_yesno "Certbot" "Odnowić certyfikaty i przeładować Nginx?" && renew_certificates ;;
            *) break ;;
        esac
    done
}

gui_config() {
    local action file
    action="$(ui_menu "Konfiguracja Nginx" "Wybierz operację" main nginx.conf vhosts "Virtual Hosts" confd conf.d snippets Snippets mime "MIME configuration" full "Pełna konfiguracja nginx -T" back Powrót || true)"
    case "$action" in
        main) edit_config_file "$MAIN_CONFIG" ;;
        vhosts) gui_sites ;;
        confd) file="$(ui_input "conf.d" "Pełna ścieżka pliku do edycji:" "$CONF_D/" || true)"; [[ -f "$file" ]] && edit_config_file "$file" ;;
        snippets) file="$(ui_input "Snippets" "Pełna ścieżka pliku do edycji:" "$NGINX_ETC/snippets/" || true)"; [[ -f "$file" ]] && edit_config_file "$file" ;;
        mime) edit_config_file "$NGINX_ETC/mime.types" ;;
        full) ui_text "nginx -T" "$(nginx -T 2>&1 || true)" ;;
    esac
}

gui_logs() {
    local source count filter file
    source="$(ui_menu "Logi" "Źródło" access access.log error error.log journal journalctl back Powrót || true)"; [[ "$source" != back && -n "$source" ]] || return 0
    count="$(ui_menu "Logi" "Zakres" 50 "Ostatnie 50 wpisów" 100 "Ostatnie 100 wpisów" live "Live view" search Szukaj error "Filtr ERROR" warn "Filtr WARN" ip "Filtr po IP" status "Filtr po HTTP status" || true)"; [[ -n "$count" ]] || return 0
    case "$source" in access) file=/var/log/nginx/access.log;; error) file=/var/log/nginx/error.log;; journal) file="";; esac
    if [[ "$count" == live ]]; then
        if [[ "$source" == journal ]]; then journalctl -fu nginx; else tail -f -- "$file"; fi
        return 0
    fi
    local content
    if [[ "$source" == journal ]]; then content="$(journalctl -u nginx --no-pager -n 200 2>&1)"; else content="$(tail -n 200 -- "$file" 2>&1)"; fi
    case "$count" in
        50|100) content="$(printf '%s\n' "$content" | tail -n "$count")" ;;
        search) filter="$(ui_input "Logi" "Szukana fraza:" "" || true)"; content="$(printf '%s\n' "$content" | grep -F -- "$filter" || true)" ;;
        error) content="$(printf '%s\n' "$content" | grep -i ERROR || true)" ;;
        warn) content="$(printf '%s\n' "$content" | grep -i WARN || true)" ;;
        ip) filter="$(ui_input "Logi" "Adres IP:" "" || true)"; content="$(printf '%s\n' "$content" | grep -F -- "$filter" || true)" ;;
        status) filter="$(ui_input "Logi" "Kod HTTP:" "500" || true)"; content="$(printf '%s\n' "$content" | awk -v s="$filter" '$9 == s')" ;;
    esac
    ui_text "Logi" "$content"
}

gui_backup() {
    local action archive name
    action="$(ui_menu "Backup konfiguracji" "Wybierz operację" list "Lista backupów" create "Utwórz backup" restore "Przywróć backup" delete "Usuń backup" back Powrót || true)"
    case "$action" in
        list) ui_text "Backupy" "$(list_backups)" ;;
        create) ui_msg "Backup" "Utworzono: $(create_backup)" ;;
        restore) name="$(ui_input "Restore" "Nazwa pliku backupu:" "" || true)"; [[ -n "$name" ]] && ASSUME_YES=true restore_backup "$BACKUP_DIR/$name" ;;
        delete) name="$(ui_input "Usuń backup" "Nazwa pliku backupu:" "" || true)"; archive="$BACKUP_DIR/$name"; [[ -f "$archive" ]] && ui_yesno "Usuń backup" "Usunąć $name?" && rm -f -- "$archive" ;;
    esac
}

gui_ports() {
    local action domain port from_port
    while true; do
        action="$(ui_menu "Porty i połączenia" "Wybierz operację" list "Pokaż porty i procesy" global "Przenieś wszystkie aktywne listen z portu 80" site "Zmień port strony" default "Zmień domyślny port Nginx" back Powrót || true)"
        case "$action" in
            list) ui_text "Porty i połączenia" "$(show_ports 2>&1 || true)" ;;
            global)
                from_port="$(ui_input "Globalna zmiana portu" "Port źródłowy:" "80" || true)"; [[ -n "$from_port" ]] || continue
                port="$(ui_input "Globalna zmiana portu" "Nowy port docelowy:" "8080" || true)"; [[ -n "$port" ]] || continue
                ui_yesno "Globalna zmiana portu" "Przenieść WSZYSTKIE aktywne dyrektywy listen z portu $from_port na $port? Zostanie wykonany backup, nginx -t i rollback przy błędzie." && ASSUME_YES=true move_all_listen_ports "$from_port" "$port"
                ;;
            site)
                domain="$(ui_select_site "Port strony" || true)"; [[ -n "$domain" ]] || continue
                port="$(ui_input "Port strony" "Nowy port HTTP:" "8080" || true)"; [[ -n "$port" ]] || continue
                ui_yesno "Zmiana portu" "Zmienić port HTTP strony $domain na $port?" && ASSUME_YES=true change_site_port "$domain" "$port"
                ;;
            default)
                port="$(ui_input "Domyślny port Nginx" "Nowy port HTTP:" "80" || true)"; [[ -n "$port" ]] || continue
                ui_yesno "Zmiana domyślnego portu" "Ustawić domyślny port Nginx na $port?" && ASSUME_YES=true change_default_port "$port"
                ;;
            *) break ;;
        esac
    done
}

gui_install_remove() {
    local action
    action="$(ui_menu "Instalacja / usunięcie" "Wybierz operację" install "Install Nginx" reinstall "Reinstall Nginx" remove "Remove Nginx" purge "Purge Nginx" back Powrót || true)"
    case "$action" in
        install) install_nginx ;;
        reinstall) create_backup >/dev/null; case "$PKG_MANAGER" in apt-get) apt-get install --reinstall -y nginx;; dnf|yum) "$PKG_MANAGER" reinstall -y nginx;; esac ;;
        remove) ui_yesno "Remove Nginx" "Usunąć pakiet Nginx? Backup zostanie utworzony." && ASSUME_YES=true remove_nginx false ;;
        purge) ui_yesno "Purge Nginx" "Usunąć pakiet i /etc/nginx? Backup zostanie utworzony." && ASSUME_YES=true remove_nginx true ;;
    esac
}

gui_security() {
    local action temp
    action="$(ui_menu "Security" "Zmiany nie są stosowane automatycznie bez podglądu." version "Ukryj wersję Nginx" headers "Security headers" body "Limit request body" rate "Rate limiting" methods "HTTP methods" tls "TLS configuration" hsts HSTS back Powrót || true)"
    case "$action" in
        version) temp="$(make_temp)"; printf 'server_tokens off;\n' > "$temp"; preview_file "$temp"; ui_yesno "Security" "Zapisać konfigurację?" && atomic_write_config "$temp" "$NGINX_ETC/conf.d/99-server-tokens.conf" ;;
        headers) apply_security_headers ;;
        body) temp="$(make_temp)"; printf 'client_max_body_size 10m;\n' > "$temp"; preview_file "$temp"; ui_msg "Security" "Dodaj dyrektywę do wybranego bloku http/server po dopasowaniu limitu." ;;
        rate) temp="$(make_temp)"; printf 'limit_req_zone $binary_remote_addr zone=perip:10m rate=10r/s;\n' > "$temp"; preview_file "$temp"; ui_msg "Security" "Wymaga dodatkowej dyrektywy limit_req w wybranej lokalizacji." ;;
        methods) temp="$(make_temp)"; printf 'limit_except GET HEAD POST { deny all; }\n' > "$temp"; preview_file "$temp" ;;
        tls) temp="$(make_temp)"; printf 'ssl_protocols TLSv1.2 TLSv1.3;\nssl_session_cache shared:SSL:10m;\nssl_session_timeout 1d;\n' > "$temp"; preview_file "$temp" ;;
        hsts) ui_msg "HSTS" "HSTS dodawaj wyłącznie w Virtual Hoście z poprawnym HTTPS:\nadd_header Strict-Transport-Security \"max-age=31536000\" always;" ;;
    esac
}

gui_settings() {
    local action
    action="$(ui_menu "Ustawienia" "Wybierz sekcję" security Security paths "Wykryte ścieżki" dependencies Zależności back Powrót || true)"
    case "$action" in
        security) gui_security ;;
        paths) ui_msg "Ścieżki" "Layout: $LAYOUT\nnginx.conf: $MAIN_CONFIG\nsites-available: $SITES_AVAILABLE\nsites-enabled: $SITES_ENABLED\nconf.d: $CONF_D\nbackup: $BACKUP_DIR\nlog: $LOG_FILE" ;;
        dependencies) ui_text "Zależności" "$(for c in nginx dialog curl openssl systemctl certbot ss lsof; do command_exists "$c" && printf '[OK] %s\n' "$c" || printf '[MISSING] %s\n' "$c"; done)" ;;
    esac
}

gui_main() {
    ensure_dialog
    while true; do
        local state version choice name
        state="$(systemctl is-active nginx 2>/dev/null || printf unknown)"
        version="$(nginx -v 2>&1 | sed 's/^nginx version: //' || printf not-installed)"
        choice="$(ui_menu "ChrisScriptBase — Nginx Manager" "Status: $state | Version: $version" \
            1 "Status serwera" 2 "Strony / Virtual Hosts" 3 "Reverse Proxy" 4 "SSL / HTTPS" \
            5 "Konfiguracja Nginx" 6 "Test konfiguracji" 7 Logi 8 "Usługa Nginx" \
            9 "Porty i połączenia" 10 "Backup konfiguracji" 11 "Przywróć konfigurację" \
            12 "Informacje diagnostyczne" 13 "Aktualizacja Nginx" 14 "Instalacja / usunięcie Nginx" \
            15 Ustawienia 0 Wyjście || true)"
        case "$choice" in
            1) gui_status ;;
            2) gui_sites ;;
            3) gui_proxy_wizard ;;
            4) gui_ssl ;;
            5) gui_config ;;
            6) ui_text "nginx -t" "$(test_nginx_config 2>&1 || true)" ;;
            7) gui_logs ;;
            8) gui_status ;;
            9) gui_ports ;;
            10) gui_backup ;;
            11) name="$(ui_input "Restore" "Nazwa pliku z $BACKUP_DIR:" "" || true)"; [[ -n "$name" ]] && ASSUME_YES=true restore_backup "$BACKUP_DIR/$name" ;;
            12) ui_msg "Diagnostyka" "Raport: $(generate_diagnostic_report)" ;;
            13) update_nginx ;;
            14) gui_install_remove ;;
            15) gui_settings ;;
            0|"") break ;;
        esac
    done
    clear
}
