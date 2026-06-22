#!/bin/bash
# =============================================================================
# Skrypt instalacji i konfiguracji: Apache2, PHP, MariaDB, phpMyAdmin, Samba
# Udostępnia /var/www jako \\serwer\www dla wszystkich użytkowników
# Testowane na Ubuntu 22.04 / 24.04
# Uruchom jako root: sudo bash setup_server.sh
# =============================================================================

set -e

# --- Kolory do komunikatów ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# --- Sprawdzenie uprawnień ---
if [[ $EUID -ne 0 ]]; then
    error "Uruchom skrypt jako root: sudo bash $0"
fi

# --- Zmienne konfiguracyjne ---
WWW_DIR="/var/www"
SAMBA_SHARE_NAME="www"
PHP_VERSION="8.3"  # zmieni się automatycznie na dostępną wersję

info "============================================="
info " Instalacja: Apache2 + PHP + MariaDB"
info "           + phpMyAdmin + Samba"
info "============================================="

# =============================================================================
# 1. Aktualizacja systemu
# =============================================================================
info "Aktualizacja listy pakietów..."
apt update -y
apt upgrade -y

# =============================================================================
# 2. Instalacja Apache2
# =============================================================================
info "Instalacja Apache2..."
apt install -y apache2
systemctl enable apache2
systemctl start apache2
info "Apache2 zainstalowany."

# =============================================================================
# 3. Instalacja PHP (z najnowszą dostępną wersją)
# =============================================================================
info "Instalacja PHP i modułów z domyślnych repozytoriów Ubuntu..."

apt install -y \
    php \
    php-cli \
    php-common \
    php-mysql \
    php-xml \
    php-curl \
    php-gd \
    php-mbstring \
    php-zip \
    php-intl \
    php-opcache \
    libapache2-mod-php

info "PHP zainstalowany: $(php -v 2>/dev/null | head -1)"

# =============================================================================
# 4. Instalacja MariaDB (MySQL)
# =============================================================================
info "Instalacja MariaDB..."
apt install -y mariadb-server mariadb-client
systemctl enable mariadb
systemctl start mariadb
info "MariaDB zainstalowany."

# Podstawowe zabezpieczenie MariaDB (automatyczne)
info "Konfiguracja zabezpieczeń MariaDB..."
mysql -e "DELETE FROM mysql.user WHERE User='';"
mysql -e "DELETE FROM mysql.user WHERE User='root' AND Host NOT IN ('localhost', '127.0.0.1', '::1');"
mysql -e "DROP DATABASE IF EXISTS test;"
mysql -e "DELETE FROM mysql.db WHERE Db='test' OR Db='test\\_%';"
mysql -e "FLUSH PRIVILEGES;"

# Ustaw hasło root dla MariaDB
MYSQL_ROOT_PASS="Admin123!"
mysql -e "ALTER USER 'root'@'localhost' IDENTIFIED BY '${MYSQL_ROOT_PASS}';"
mysql -e "FLUSH PRIVILEGES;"
warn "Hasło root MariaDB ustawione na: ${MYSQL_ROOT_PASS}"
warn "ZMIEŃ JE PO INSTALACJI komendą: sudo mysql -u root -p"

# =============================================================================
# 5. Instalacja phpMyAdmin
# =============================================================================
info "Instalacja phpMyAdmin..."

# Predefiniuj odpowiedzi dla instalatora phpMyAdmin
export DEBIAN_FRONTEND=noninteractive
echo "phpmyadmin phpmyadmin/dbconfig-install boolean true" | debconf-set-selections
echo "phpmyadmin phpmyadmin/app-password-confirm password ${MYSQL_ROOT_PASS}" | debconf-set-selections
echo "phpmyadmin phpmyadmin/mysql/admin-pass password ${MYSQL_ROOT_PASS}" | debconf-set-selections
echo "phpmyadmin phpmyadmin/mysql/app-pass password ${MYSQL_ROOT_PASS}" | debconf-set-selections
echo "phpmyadmin phpmyadmin/reconfigure-webserver multiselect apache2" | debconf-set-selections

apt install -y phpmyadmin

# Włącz konfigurację phpMyAdmin w Apache jeśli nie jest aktywna
if [ ! -f /etc/apache2/conf-enabled/phpmyadmin.conf ]; then
    ln -sf /etc/phpmyadmin/apache.conf /etc/apache2/conf-available/phpmyadmin.conf
    a2enconf phpmyadmin
fi

info "phpMyAdmin zainstalowany. Dostępny pod: http://localhost/phpmyadmin"

# =============================================================================
# 6. Instalacja i konfiguracja Samby
# =============================================================================
info "Instalacja Samba..."
apt install -y samba samba-common-bin

# Backup oryginalnej konfiguracji
cp /etc/samba/smb.conf /etc/samba/smb.conf.backup.$(date +%Y%m%d%H%M%S)

# Pobierz nazwę hosta
HOSTNAME=$(hostname)

# Nowa konfiguracja Samby
cat > /etc/samba/smb.conf << 'SAMBA_EOF'
# =============================================================================
# Konfiguracja Samba - wygenerowana automatycznie
# =============================================================================

[global]
   workgroup = WORKGROUP
   server string = Serwer Ubuntu
   netbios name = serwer
   security = user
   map to guest = Bad User
   dns proxy = no

   # Obsługa protokołów SMB
   server min protocol = SMB2
   server max protocol = SMB3

   # Logowanie
   log file = /var/log/samba/log.%m
   max log size = 1000
   logging = file

   # Optymalizacja wydajności
   socket options = TCP_NODELAY IPTOS_LOWDELAY

# =============================================================================
# Udział WWW - dostępny dla każdego bez hasła
# =============================================================================
[www]
   comment = Katalog WWW Apache
   path = /var/www
   browseable = yes
   read only = no
   writable = yes
   guest ok = yes
   guest only = yes
   public = yes
   force user = www-data
   force group = www-data
   create mask = 0775
   directory mask = 0775

SAMBA_EOF

info "Samba skonfigurowana."

# =============================================================================
# 7. Konfiguracja uprawnień do katalogu /var/www
# =============================================================================
info "Konfiguracja uprawnień katalogu ${WWW_DIR}..."

# Ustaw właściciela
chown -R www-data:www-data ${WWW_DIR}

# Ustaw uprawnienia: właściciel i grupa mogą czytać/pisać/wykonywać
chmod -R 0775 ${WWW_DIR}

# Ustaw sticky bit na katalogach, żeby nowe pliki dziedziczyły grupę
find ${WWW_DIR} -type d -exec chmod g+s {} \;

info "Uprawnienia ustawione."

# =============================================================================
# 8. Włączenie modułów Apache2
# =============================================================================
info "Włączanie modułów Apache2..."
a2enmod rewrite
a2enmod headers
a2enmod ssl

# =============================================================================
# 9. Konfiguracja firewalla (UFW)
# =============================================================================
info "Konfiguracja firewalla..."
if command -v ufw &> /dev/null; then
    ufw allow 'Apache Full'    # porty 80, 443
    ufw allow 'Samba'           # porty 139, 445
    # Jeśli firewall jest aktywny, przeładuj
    if ufw status | grep -q "Status: active"; then
        ufw reload
        info "Reguły firewalla dodane i przeładowane."
    else
        warn "UFW nie jest aktywny. Reguły dodane, ale firewall wyłączony."
        warn "Włącz komendą: sudo ufw enable"
    fi
else
    warn "UFW nie jest zainstalowany. Pominięto konfigurację firewalla."
fi

# =============================================================================
# 10. Utworzenie strony testowej
# =============================================================================
info "Tworzenie strony testowej..."
cat > ${WWW_DIR}/html/info.php << 'PHP_EOF'
<?php
// Strona testowa - USUŃ PO WERYFIKACJI!
echo "<h1>Serwer działa poprawnie!</h1>";
echo "<h2>Informacje o PHP:</h2>";
phpinfo();
?>
PHP_EOF

chown www-data:www-data ${WWW_DIR}/html/info.php

# =============================================================================
# 11. Restart wszystkich usług
# =============================================================================
info "Restartowanie usług..."
systemctl restart apache2
systemctl restart smbd
systemctl restart nmbd
systemctl restart mariadb

systemctl enable smbd
systemctl enable nmbd

# =============================================================================
# 12. Podsumowanie
# =============================================================================
IP_ADDR=$(hostname -I | awk '{print $1}')

echo ""
echo -e "${GREEN}=============================================${NC}"
echo -e "${GREEN} INSTALACJA ZAKOŃCZONA POMYŚLNIE!${NC}"
echo -e "${GREEN}=============================================${NC}"
echo ""
echo -e " ${YELLOW}Adres IP serwera:${NC}     ${IP_ADDR}"
echo -e " ${YELLOW}Nazwa NetBIOS:${NC}        serwer"
echo ""
echo -e " ${GREEN}--- Usługi webowe ---${NC}"
echo -e " Strona główna:        http://${IP_ADDR}/"
echo -e " Strona testowa PHP:   http://${IP_ADDR}/info.php"
echo -e " phpMyAdmin:           http://${IP_ADDR}/phpmyadmin"
echo ""
echo -e " ${GREEN}--- Baza danych ---${NC}"
echo -e " Użytkownik:           root"
echo -e " Hasło:                ${MYSQL_ROOT_PASS}"
echo -e " ${RED}(ZMIEŃ HASŁO PO INSTALACJI!)${NC}"
echo ""
echo -e " ${GREEN}--- Udział Samba ---${NC}"
echo -e " Ścieżka sieciowa:     \\\\\\\\serwer\\\\www"
echo -e " Lub po IP:            \\\\\\\\${IP_ADDR}\\\\www"
echo -e " Katalog lokalny:      ${WWW_DIR}"
echo -e " Dostęp:               Publiczny (bez hasła)"
echo ""
echo -e " ${GREEN}--- Wersje ---${NC}"
echo -e " Apache:  $(apache2 -v 2>/dev/null | head -1)"
echo -e " PHP:     $(php -v 2>/dev/null | head -1)"
echo -e " MariaDB: $(mysql --version 2>/dev/null)"
echo -e " Samba:   $(smbd --version 2>/dev/null)"
echo ""
echo -e " ${RED}WAŻNE KROKI PO INSTALACJI:${NC}"
echo -e " 1. Zmień hasło MariaDB: sudo mysql -u root -p"
echo -e " 2. Usuń plik testowy:   sudo rm ${WWW_DIR}/html/info.php"
echo -e " 3. Sprawdź firewall:    sudo ufw status"
echo ""
