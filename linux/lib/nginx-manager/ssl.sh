# shellcheck shell=bash

certificate_files() {
    find "$NGINX_ETC" /etc/letsencrypt/live -type f \( -name '*.crt' -o -name 'fullchain.pem' -o -name '*.pem' \) ! -name '*privkey*' ! -name '*.key' -print 2>/dev/null | sort -u
}

list_certificates() {
    local file subject issuer start end epoch now days
    now="$(date +%s)"
    printf '%-45s %-7s %s\n' CERTIFICATE DAYS VALID_UNTIL
    while IFS= read -r file; do
        [[ -n "$file" ]] || continue
        if ! openssl x509 -in "$file" -noout >/dev/null 2>&1; then continue; fi
        subject="$(openssl x509 -in "$file" -noout -subject 2>/dev/null | sed 's/^subject=//')"
        issuer="$(openssl x509 -in "$file" -noout -issuer 2>/dev/null | sed 's/^issuer=//')"
        start="$(openssl x509 -in "$file" -noout -startdate 2>/dev/null | cut -d= -f2-)"
        end="$(openssl x509 -in "$file" -noout -enddate 2>/dev/null | cut -d= -f2-)"
        epoch="$(date -d "$end" +%s 2>/dev/null || printf 0)"
        days=$(( (epoch - now) / 86400 ))
        printf '%-45s %-7d %s\n  Subject: %s\n  Issuer: %s\n  Valid from: %s\n' "$file" "$days" "$end" "$subject" "$issuer" "$start"
        ((days < 30)) && printf '  [WARNING] Certyfikat wygaśnie za mniej niż 30 dni.\n'
    done < <(certificate_files)
}

install_certbot() {
    command_exists certbot && return 0
    case "$OS_FAMILY" in
        debian) install_packages certbot python3-certbot-nginx ;;
        rhel) install_packages certbot python3-certbot-nginx ;;
        *) die "$EXIT_DEPENDENCY" "Brak obsługi instalacji Certbota dla tej dystrybucji." ;;
    esac
}

obtain_letsencrypt_certificate() {
    local domain="$1" email="$2"
    require_root
    validate_domain "$domain" || die "$EXIT_ARGS" "Niepoprawna domena."
    [[ "$email" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] || die "$EXIT_ARGS" "Niepoprawny adres e-mail."
    install_certbot || return $?
    test_nginx_config || return "$EXIT_CONFIG"
    confirm_action "Generate certificate" "$domain" "Certbot pobierze certyfikat Let's Encrypt i zmieni konfigurację Nginx." || return 0
    create_backup >/dev/null || return "$EXIT_BACKUP"
    if run_command certbot --nginx --non-interactive --agree-tos --redirect -m "$email" -d "$domain"; then
        test_nginx_config && reload_nginx
        log_event certbot success "$domain"
    else
        log_event certbot failure "$domain"
        return "$EXIT_GENERAL"
    fi
}

generate_self_signed_certificate() {
    local domain="$1" days="${2:-365}" directory="$NGINX_ETC/ssl/$domain"
    require_root
    validate_domain "$domain" || die "$EXIT_ARGS" "Niepoprawna domena."
    [[ "$days" =~ ^[0-9]+$ ]] && ((days >= 1 && days <= 3650)) || die "$EXIT_ARGS" "Niepoprawna ważność certyfikatu."
    confirm_action "Self-signed certificate" "$domain" "Utworzenie klucza prywatnego i certyfikatu w $directory." || return 0
    run_command mkdir -p -- "$directory" || return "$EXIT_GENERAL"
    run_command openssl req -x509 -nodes -newkey rsa:3072 -days "$days" -subj "/CN=$domain" \
        -addext "subjectAltName=DNS:$domain" -keyout "$directory/privkey.pem" -out "$directory/fullchain.pem" || return "$EXIT_GENERAL"
    [[ "$DRY_RUN" == true ]] || chmod 600 "$directory/privkey.pem"
    log_event self_signed success "$domain"
    printf 'Certificate: %s\nPrivate key: %s\n' "$directory/fullchain.pem" "$directory/privkey.pem"
}

renew_certificates() {
    require_root
    command_exists certbot || die "$EXIT_DEPENDENCY" "Certbot nie jest zainstalowany."
    test_nginx_config || return "$EXIT_CONFIG"
    run_command certbot renew || return "$EXIT_GENERAL"
    test_nginx_config && reload_nginx
    log_event renew_certificates success
}

add_existing_certificate() {
    local domain="$1" cert="$2" key="$3" file temp
    require_root
    validate_domain "$domain" || die "$EXIT_ARGS" "Niepoprawna domena."
    validate_safe_path "$cert" && validate_safe_path "$key" || die "$EXIT_ARGS" "Niepoprawna ścieżka certyfikatu."
    [[ -r "$cert" && -r "$key" ]] || die "$EXIT_ARGS" "Brak pliku certyfikatu lub klucza."
    openssl x509 -in "$cert" -noout >/dev/null 2>&1 || die "$EXIT_ARGS" "Plik nie jest poprawnym certyfikatem X.509."
    file="$(find_site_file "$domain")" || die "$EXIT_ARGS" "Nie znaleziono Virtual Hosta."
    temp="$(make_temp)" || return "$EXIT_GENERAL"
    awk -v cert="$cert" -v key="$key" '
      /server_name/ && !added {print; print "    listen 443 ssl;"; print "    listen [::]:443 ssl;"; print "    ssl_certificate " cert ";"; print "    ssl_certificate_key " key ";"; added=1; next}
      {print}
    ' "$file" > "$temp"
    preview_file "$temp"
    atomic_write_config "$temp" "$file"
}
