#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# ChrisScriptBase - Ubuntu VM Slim
# Odchudzanie Ubuntu działającego jako VM/server
#
# Bezpieczne użycie:
#   ./ubuntu-vm-slim.sh --status
#   sudo ./ubuntu-vm-slim.sh --apply
#
# Mocniejsze czyszczenie:
#   sudo ./ubuntu-vm-slim.sh --apply --aggressive
#
# Automatycznie:
#   sudo ./ubuntu-vm-slim.sh --apply --aggressive --yes
# ============================================================

SCRIPT_NAME="ubuntu-vm-slim"
VERSION="1.1.0"

APPLY=false
AGGRESSIVE=false
YES=false
SHOW_ALL=false
REMOVE_SNAPD=false

TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
BACKUP_DIR="/var/backups/chriscriptbase/vm-slim-${TIMESTAMP}"

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    GREEN="\033[0;32m"
    YELLOW="\033[0;33m"
    RED="\033[0;31m"
    BLUE="\033[0;34m"
    RESET="\033[0m"
else
    GREEN=""
    YELLOW=""
    RED=""
    BLUE=""
    RESET=""
fi

ok()   { echo -e "${GREEN}[ OK ]${RESET} $*"; }
info() { echo -e "${BLUE}[INFO]${RESET} $*"; }
warn() { echo -e "${YELLOW}[WARN]${RESET} $*"; }
fail() { echo -e "${RED}[FAIL]${RESET} $*" >&2; }

stage() {
    echo
    echo "============================================================"
    echo "[$1/6] $2"
    echo "============================================================"
}

usage() {
    cat <<EOF
ChrisScriptBase - Ubuntu VM Slim v${VERSION}

Użycie:

  $0 --status
      Tylko analiza. Niczego nie usuwa.

  sudo $0 --apply
      Bezpieczne czyszczenie:
      - apt autoremove --purge
      - apt clean
      - stare osierocone zależności
      - stare kernele oznaczone przez APT jako zbędne

  sudo $0 --apply --aggressive
      Dodatkowo usuwa typowe pakiety desktopowe i peryferyjne,
      m.in. GNOME, X11/Xorg, drukowanie, Bluetooth, audio,
      skanery, modem/mobile broadband, firmware sprzętowy VM
      oraz opcjonalne narzędzia developerskie/debug.

Opcje:

  --status          analiza bez zmian
  --apply           wykonaj czyszczenie
  --aggressive      mocniejsze czyszczenie VM
  --remove-snapd    usuń snapd, ale tylko jeśli nie ma zainstalowanych snapów
  --all             pokaż wszystkie zainstalowane pakiety
  --yes             bez pytania o potwierdzenie
  --help            pomoc
  --uninstall       usuń ten skrypt z /usr/local/sbin
EOF
}

if [[ $# -eq 0 ]]; then
    usage
    exit 0
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --status)
            APPLY=false
            ;;
        --apply)
            APPLY=true
            ;;
        --aggressive)
            AGGRESSIVE=true
            ;;
        --remove-snapd)
            REMOVE_SNAPD=true
            ;;
        --all)
            SHOW_ALL=true
            ;;
        --yes|-y)
            YES=true
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --uninstall)
            if [[ $EUID -ne 0 ]]; then
                fail "Uruchom --uninstall przez sudo."
                exit 1
            fi

            rm -f "/usr/local/sbin/${SCRIPT_NAME}"
            ok "Usunięto /usr/local/sbin/${SCRIPT_NAME}"
            exit 0
            ;;
        *)
            fail "Nieznana opcja: $1"
            usage
            exit 1
            ;;
    esac

    shift
done

is_installed() {
    dpkg-query -W -f='${Status}' "$1" 2>/dev/null \
        | grep -q "install ok installed"
}

confirm() {
    if [[ "$YES" == true ]]; then
        return 0
    fi

    echo
    read -r -p "Kontynuować? [y/N]: " answer

    [[ "$answer" =~ ^[Yy]$ ]]
}

human_kb() {
    local kb="$1"

    awk -v kb="$kb" '
    BEGIN {
        if (kb >= 1048576)
            printf "%.2f GiB", kb / 1048576;
        else if (kb >= 1024)
            printf "%.2f MiB", kb / 1024;
        else
            printf "%d KiB", kb;
    }'
}

stage 1 "Walidacja systemu"

if [[ ! -f /etc/os-release ]]; then
    fail "Nie znaleziono /etc/os-release."
    exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release

if [[ "${ID:-}" != "ubuntu" ]]; then
    fail "Ten moduł jest przeznaczony dla Ubuntu."
    exit 1
fi

ok "System: ${PRETTY_NAME:-Ubuntu}"
info "Kernel: $(uname -r)"
info "Architektura: $(dpkg --print-architecture)"

if systemd-detect-virt --quiet 2>/dev/null; then
    ok "Wirtualizacja: $(systemd-detect-virt)"
else
    warn "Nie wykryto hypervisora. Możliwe, że system nie jest VM."
fi

if [[ "$APPLY" == true && $EUID -ne 0 ]]; then
    fail "Tryb --apply wymaga sudo/root."
    exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
    fail "Nie znaleziono apt-get."
    exit 1
fi

if [[ "$APPLY" == true ]]; then
    if dpkg --audit | grep -q .; then
        fail "dpkg zgłasza niedokończone lub uszkodzone operacje."
        dpkg --audit
        exit 1
    fi

    if ! apt-get check >/dev/null 2>&1; then
        fail "APT wykrył problem z zależnościami."
        exit 1
    fi

    ok "APT/dpkg: stan poprawny"
fi

stage 2 "Analiza zainstalowanych pakietów"

PACKAGE_COUNT="$(
    dpkg-query -W -f='${binary:Package}\n' 2>/dev/null | wc -l
)"

info "Zainstalowanych pakietów: ${PACKAGE_COUNT}"

TOTAL_KB="$(
    dpkg-query -W -f='${Installed-Size}\n' 2>/dev/null \
        | awk '{total += $1} END {print total+0}'
)"

info "Łączny Installed-Size: $(human_kb "$TOTAL_KB")"

echo
echo "Największe zainstalowane pakiety:"
echo

printf "%-12s %-45s %s\n" "ROZMIAR" "PAKIET" "WERSJA"
printf "%-12s %-45s %s\n" "-------" "------" "------"

while IFS=$'\t' read -r size package version; do
    printf "%-12s %-45s %s\n" \
        "$(human_kb "$size")" \
        "$package" \
        "$version"
done < <(
    dpkg-query -W \
        -f='${Installed-Size}\t${binary:Package}\t${Version}\n' \
        2>/dev/null \
        | sort -nr \
        | head -50
)

if [[ "$SHOW_ALL" == true ]]; then
    echo
    echo "Wszystkie pakiety:"
    echo

    dpkg-query -W \
        -f='${binary:Package}\t${Version}\t${Installed-Size} KiB\n' \
        | sort
fi

stage 3 "Ochrona krytycznych komponentów VM"

RUNNING_KERNEL="$(uname -r)"

REQUIRED_PACKAGES=(
    ssh
    curl
    nano
)

PROTECTED_PACKAGES=(
    apt
    bash
    coreutils
    dpkg
    systemd
    systemd-sysv
    sudo
    passwd
    login
    util-linux
    openssh-server
    openssh-client
    ssh
    curl
    nano
    iproute2
    iputils-ping
    netplan.io
    network-manager
    ca-certificates
    cloud-init
    qemu-guest-agent
    open-vm-tools
    ubuntu-minimal
    ubuntu-server
    ubuntu-server-minimal
    "linux-image-${RUNNING_KERNEL}"
    "linux-modules-${RUNNING_KERNEL}"
    "linux-modules-extra-${RUNNING_KERNEL}"
    linux-image-generic
    linux-generic
    grub-pc
    grub-pc-bin
    grub-efi-amd64
    grub-efi-amd64-bin
    grub-efi-amd64-signed
    shim-signed
)

INSTALLED_PROTECTED=()

for package in "${PROTECTED_PACKAGES[@]}"; do
    if is_installed "$package"; then
        INSTALLED_PROTECTED+=("$package")
    fi
done

printf '%s\n' "${INSTALLED_PROTECTED[@]}"

info "Chronionych pakietów: ${#INSTALLED_PROTECTED[@]}"

if is_installed openssh-server; then
    ok "OpenSSH Server pozostanie zainstalowany"
fi

if is_installed qemu-guest-agent; then
    ok "QEMU Guest Agent pozostanie zainstalowany"
fi

if is_installed cloud-init; then
    ok "cloud-init pozostanie zainstalowany"
fi

if is_installed nano; then
    ok "nano pozostanie zainstalowane"
fi

MISSING_REQUIRED=()

for package in "${REQUIRED_PACKAGES[@]}"; do
    if ! is_installed "$package"; then
        MISSING_REQUIRED+=("$package")
    fi
done

if [[ ${#MISSING_REQUIRED[@]} -eq 0 ]]; then
    ok "Wymagane pakiety są zainstalowane: ${REQUIRED_PACKAGES[*]}"
else
    warn "Brak wymaganych pakietów: ${MISSING_REQUIRED[*]}"

    if [[ "$APPLY" != true ]]; then
        info "Tryb --status: pakiety zostaną zainstalowane po uruchomieniu z --apply."
    fi
fi

stage 4 "Wyszukiwanie zbędnych pakietów"

mapfile -t AUTOREMOVE_PACKAGES < <(
    apt-get -s autoremove --purge 2>/dev/null \
        | awk '/^Remv / {print $2}' \
        | sort -u
)

echo
echo "APT autoremove:"
echo

if [[ ${#AUTOREMOVE_PACKAGES[@]} -eq 0 ]]; then
    ok "Brak osieroconych zależności."
else
    printf '  - %s\n' "${AUTOREMOVE_PACKAGES[@]}"
    info "Do autoremove: ${#AUTOREMOVE_PACKAGES[@]} pakietów"
fi

AGGRESSIVE_PATTERNS=(
    # Ubuntu Desktop / GNOME / aplikacje GUI
    'ubuntu-desktop'
    'ubuntu-desktop-minimal'
    'ubuntu-session'
    'ubuntu-settings'
    'ubuntu-wallpapers.*'
    'gdm3'
    'gjs'
    'gnome-.*'
    'baobab'
    'eog'
    'evince'
    'firefox'
    'nautilus.*'
    'seahorse'
    'yelp.*'
    'zenity.*'
    'tracker.*'
    'libreoffice.*'
    'thunderbird'

    # X11 / Xorg / Wayland desktop
    'xorg'
    'xinit'
    'xinput'
    'x11-apps'
    'x11-common'
    'x11-session-utils'
    'x11-utils'
    'x11-xkb-utils'
    'x11-xserver-utils'
    'xauth'
    'xbitmaps'
    'xbrlapi'
    'xcursor-themes'
    'xcvt'
    'xfonts-.*'
    'xserver-.*'
    'xwayland'

    # Drukowanie
    'cups.*'
    'foomatic-db-compressed-ppds'
    'hplip.*'
    'ipp-usb'
    'openprinting-ppds'
    'printer-driver-.*'
    'system-config-printer-.*'

    # Bluetooth
    'bluez.*'
    'gnome-bluetooth-sendto'

    # Audio desktop
    'alsa-base'
    'alsa-utils'
    'pipewire'
    'pipewire-.*'
    'wireplumber'
    'rtkit'
    'sound-icons'
    'sound-theme-freedesktop'
    'gstreamer1\.0-alsa'
    'gstreamer1\.0-pipewire'

    # Accessibility / screen reader / braille
    'brltty'
    'speech-dispatcher.*'
    'espeak-ng-data'
    'liblouis.*'
    'liblouisutdml.*'
    'orca'

    # Modem / mobile broadband
    'modemmanager'
    'mobile-broadband-provider-info'
    'usb-modeswitch.*'
    'libmbim-.*'
    'libqmi-.*'
    'libqrtr-glib0'

    # Skanery
    'sane-.*'
    'libsane.*'

    # GUI aktualizacji
    'update-manager'
    'update-notifier'
    'software-properties-gtk'
    'ubuntu-release-upgrader-gtk'
    'aptdaemon.*'

    # Funkcje laptop/desktop niepotrzebne na typowej VM
    'fprintd'
    'bolt'
    'iio-sensor-proxy'
    'power-profiles-daemon'
    'switcheroo-control'
    'thermald'
    'gamemode.*'

    # Dokumentacja developerska / desktopowa
    'ubuntu-docs'
    'gnome-user-docs.*'
    'xorg-docs-core'
    'manpages-dev'

    # Opcjonalne narzędzia debug/development
    'gdb'
    'strace'
    'bpfcc-tools'
    'bpftrace'
    'trace-cmd'
    'linux-tools-.*'
    'libc6-dbg'
    'libc-devtools'
    'libc6-dev'
    'cpp'
    'cpp-[0-9].*'
    'cpp-x86-64-linux-gnu'

    # Firmware fizycznego sprzętu zwykle zbędny w VM
    'linux-firmware-(amd-graphics|amd-misc|broadcom-wireless|intel-graphics|intel-misc|intel-wireless|marvell-prestera|marvell-wireless|mediatek|mellanox-spectrum|netronome|nvidia-graphics|qlogic|qualcomm-graphics|qualcomm-misc|qualcomm-wireless|realtek)'

    # Pozostałe typowe usługi desktopowe
    'avahi-daemon'
    'whoopsie'
    'popularity-contest'
    'fwupd'
)

AGGRESSIVE_REGEX="^($(IFS='|'; echo "${AGGRESSIVE_PATTERNS[*]}"))$"

mapfile -t AGGRESSIVE_PACKAGES < <(
    dpkg-query -W -f='${binary:Package}\n' 2>/dev/null \
        | sed 's/:.*$//' \
        | grep -E "$AGGRESSIVE_REGEX" \
        | sort -u || true
)

# Symulacja APT: profil aggressive nie może usunąć pakietów chronionych
# jako efekt uboczny zależności.
PROTECTED_REMOVALS=()

if [[ "$AGGRESSIVE" == true && ${#AGGRESSIVE_PACKAGES[@]} -gt 0 ]]; then
    mapfile -t AGGRESSIVE_REMOVAL_PLAN < <(
        apt-get -s purge "${AGGRESSIVE_PACKAGES[@]}" 2>/dev/null \
            | awk '/^Remv / {print $2}' \
            | sed 's/:.*$//' \
            | sort -u
    )

    for package in "${INSTALLED_PROTECTED[@]}"; do
        package="${package%%:*}"

        if printf '%s\n' "${AGGRESSIVE_REMOVAL_PLAN[@]}" \
            | grep -Fxq "$package"; then
            PROTECTED_REMOVALS+=("$package")
        fi
    done

    if [[ ${#PROTECTED_REMOVALS[@]} -gt 0 ]]; then
        fail "APT chciałby usunąć chronione pakiety: ${PROTECTED_REMOVALS[*]}"
        fail "Przerywam profil aggressive zamiast ryzykować uszkodzenie VM."
        exit 1
    fi
fi

if [[ "$AGGRESSIVE" == true ]]; then
    echo
    echo "Pakiety profilu AGGRESSIVE:"
    echo

    if [[ ${#AGGRESSIVE_PACKAGES[@]} -eq 0 ]]; then
        ok "Nie znaleziono typowych pakietów desktop/peripherals."
    else
        printf '  - %s\n' "${AGGRESSIVE_PACKAGES[@]}"
    fi
fi

stage 5 "Czyszczenie systemu"

if [[ "$APPLY" != true ]]; then
    warn "Tryb STATUS — nie wykonuję żadnych zmian."

    echo
    echo "Aby wykonać bezpieczne czyszczenie:"
    echo "  sudo $0 --apply"
    echo
    echo "Aby wykonać mocniejsze odchudzanie VM:"
    echo "  sudo $0 --apply --aggressive"
else
    mkdir -p "$BACKUP_DIR"

    dpkg-query -W \
        -f='${binary:Package}\t${Version}\t${Installed-Size}\n' \
        > "${BACKUP_DIR}/installed-packages.tsv"

    apt-mark showmanual > "${BACKUP_DIR}/manual-packages.txt"
    apt-mark showauto > "${BACKUP_DIR}/auto-packages.txt"
    apt-mark showhold > "${BACKUP_DIR}/held-packages.txt"

    cp -a /etc/apt/sources.list "${BACKUP_DIR}/" 2>/dev/null || true
    cp -a /etc/apt/sources.list.d "${BACKUP_DIR}/" 2>/dev/null || true

    ok "Backup list pakietów: ${BACKUP_DIR}"

    if [[ ${#MISSING_REQUIRED[@]} -gt 0 ]]; then
        info "Aktualizuję indeks APT przed instalacją wymaganych pakietów..."
        DEBIAN_FRONTEND=noninteractive apt-get update

        info "Instaluję wymagane pakiety: ${MISSING_REQUIRED[*]}"
        DEBIAN_FRONTEND=noninteractive apt-get install -y "${MISSING_REQUIRED[@]}"

        for package in "${MISSING_REQUIRED[@]}"; do
            if is_installed "$package"; then
                ok "Zainstalowano wymagany pakiet: $package"
            else
                fail "Nie udało się zainstalować wymaganego pakietu: $package"
                exit 1
            fi
        done
    fi

    echo
    warn "Pakiety aplikacyjne, bazy danych, nginx, Docker itd. nie są automatycznie usuwane."
    warn "Profil aggressive usuwa GUI i typowe komponenty desktop/peripherals."

    if ! confirm; then
        warn "Operacja anulowana."
        exit 0
    fi

    info "Oznaczam krytyczne komponenty jako instalowane ręcznie."

    for package in "${INSTALLED_PROTECTED[@]}"; do
        apt-mark manual "$package" >/dev/null 2>&1 || true
    done

    if [[ "$AGGRESSIVE" == true && ${#AGGRESSIVE_PACKAGES[@]} -gt 0 ]]; then
        info "Usuwam pakiety profilu aggressive..."

        DEBIAN_FRONTEND=noninteractive \
            apt-get purge -y \
            "${AGGRESSIVE_PACKAGES[@]}"
    fi

    if [[ "$REMOVE_SNAPD" == true ]]; then
        if command -v snap >/dev/null 2>&1; then
            SNAP_COUNT="$(
                snap list 2>/dev/null \
                    | awk 'NR > 1 {count++} END {print count+0}'
            )"

            if [[ "$SNAP_COUNT" -gt 0 ]]; then
                warn "snapd ma ${SNAP_COUNT} zainstalowanych snapów."
                warn "Nie usuwam snapd automatycznie."
            elif is_installed snapd; then
                info "Usuwam nieużywany snapd..."

                DEBIAN_FRONTEND=noninteractive \
                    apt-get purge -y snapd
            fi
        elif is_installed snapd; then
            DEBIAN_FRONTEND=noninteractive \
                apt-get purge -y snapd
        fi
    fi

    info "Uruchamiam apt autoremove --purge..."

    DEBIAN_FRONTEND=noninteractive \
        apt-get autoremove --purge -y

    info "Czyszczę cache APT..."
    apt-get clean

    if command -v journalctl >/dev/null 2>&1; then
        info "Ograniczam stare logi journal do 100 MB..."
        journalctl --vacuum-size=100M >/dev/null 2>&1 || true
    fi

    rm -rf /var/lib/apt/lists/partial/* 2>/dev/null || true

    ok "Czyszczenie zakończone."
fi

stage 6 "Kontrola końcowa"

NEW_PACKAGE_COUNT="$(
    dpkg-query -W -f='${binary:Package}\n' 2>/dev/null | wc -l
)"

NEW_TOTAL_KB="$(
    dpkg-query -W -f='${Installed-Size}\n' 2>/dev/null \
        | awk '{total += $1} END {print total+0}'
)"

info "Pakiety przed: ${PACKAGE_COUNT}"
info "Pakiety teraz: ${NEW_PACKAGE_COUNT}"

info "Installed-Size przed: $(human_kb "$TOTAL_KB")"
info "Installed-Size teraz: $(human_kb "$NEW_TOTAL_KB")"

if [[ "$APPLY" == true ]]; then
    SAVED_KB=$(( TOTAL_KB - NEW_TOTAL_KB ))

    if (( SAVED_KB > 0 )); then
        ok "Usunięto około: $(human_kb "$SAVED_KB")"
    fi
fi

echo
df -h /

echo

if is_installed openssh-server; then
    if systemctl is-active --quiet ssh 2>/dev/null; then
        ok "SSH działa"
    else
        warn "openssh-server jest zainstalowany, ale usługa SSH nie działa."
    fi
fi

if is_installed qemu-guest-agent; then
    if systemctl is-active --quiet qemu-guest-agent 2>/dev/null; then
        ok "QEMU Guest Agent działa"
    else
        warn "QEMU Guest Agent jest zainstalowany, ale obecnie nie działa."
    fi
fi

if is_installed nano; then
    ok "nano jest zainstalowane"
else
    fail "nano nie jest zainstalowane po zakończeniu operacji."
    exit 1
fi

if ip route show default 2>/dev/null | grep -q '^default'; then
    ok "Default route istnieje"
else
    warn "Nie znaleziono default route."
fi

if [[ "$APPLY" == true ]]; then
    info "Backup: ${BACKUP_DIR}"
fi

ok "Gotowe."
