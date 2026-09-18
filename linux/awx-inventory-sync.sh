#!/usr/bin/env bash
set -Eeuo pipefail

# AWX Inventory Sync
#
# Uruchomienie interaktywne na hoście Kubernetes, na którym działa AWX:
#   sudo bash awx-inventory-sync.sh
#
# Skrypt:
#   - wykrywa dostęp do Kubernetes (kubectl, k3s albo microk8s),
#   - wykrywa działający kontener AWX z awx-manage,
#   - pyta tylko o dane SSH serwera źródłowego,
#   - generuje dedykowany klucz ED25519 i instaluje go przez jednorazowe hasło,
#   - automatycznie pobiera/eksportuje inventory z serwera źródłowego,
#   - tworzy Inventory w AWX i importuje do niego dane,
#   - instaluje siebie w /usr/local/sbin i dodaje cron co 5 minut.
#
# Hasło SSH jest używane wyłącznie w pamięci podczas bootstrapu i nie jest
# zapisywane na dysku. Późniejsze synchronizacje używają wyłącznie klucza SSH.

APP="awx-inventory-sync"
INSTALL_PATH="/usr/local/sbin/${APP}"
STATE_DIR="/etc/${APP}"
STATE_FILE="${STATE_DIR}/state.env"
KEY_FILE="${STATE_DIR}/id_ed25519"
KNOWN_HOSTS="${STATE_DIR}/known_hosts"
KUBECONFIG_FILE="${STATE_DIR}/kubeconfig"
CRON_FILE="/etc/cron.d/${APP}"
LOG_FILE="/var/log/${APP}.log"
LOCK_FILE="/run/${APP}.lock"
CRON_SCHEDULE="*/5 * * * *"

KUBE_MODE=""
KUBE_SOURCE_CONFIG=""
KUBE_CMD=()

SOURCE_HOST=""
SOURCE_PORT="22"
SOURCE_USER=""
INVENTORY_NAME=""
LAST_HASH=""

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
    log "BŁĄD: $*" >&2
    exit 1
}

require_root() {
    [[ ${EUID:-$(id -u)} -eq 0 ]] || die "Uruchom skrypt jako root, np. sudo bash $0"
}

have() {
    command -v "$1" >/dev/null 2>&1
}

install_dependencies() {
    local need_ssh=0 need_sshpass=0 need_flock=0
    have ssh || need_ssh=1
    have ssh-keygen || need_ssh=1
    have ssh-keyscan || need_ssh=1
    have sshpass || need_sshpass=1
    have flock || need_flock=1

    (( need_ssh == 0 && need_sshpass == 0 && need_flock == 0 )) && return 0

    log "Instaluję brakujące zależności..."

    if have apt-get; then
        local pkgs=()
        (( need_ssh )) && pkgs+=(openssh-client)
        (( need_sshpass )) && pkgs+=(sshpass)
        (( need_flock )) && pkgs+=(util-linux)
        DEBIAN_FRONTEND=noninteractive apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${pkgs[@]}"
    elif have dnf; then
        local pkgs=()
        (( need_ssh )) && pkgs+=(openssh-clients)
        (( need_sshpass )) && pkgs+=(sshpass)
        (( need_flock )) && pkgs+=(util-linux)
        dnf install -y "${pkgs[@]}"
    elif have yum; then
        local pkgs=()
        (( need_ssh )) && pkgs+=(openssh-clients)
        (( need_sshpass )) && pkgs+=(sshpass)
        (( need_flock )) && pkgs+=(util-linux)
        yum install -y "${pkgs[@]}"
    elif have zypper; then
        local pkgs=()
        (( need_ssh )) && pkgs+=(openssh)
        (( need_sshpass )) && pkgs+=(sshpass)
        (( need_flock )) && pkgs+=(util-linux)
        zypper --non-interactive install "${pkgs[@]}"
    else
        die "Nie znam menedżera pakietów. Zainstaluj: OpenSSH client, sshpass i util-linux."
    fi

    have ssh && have ssh-keygen && have ssh-keyscan || die "Brak narzędzi OpenSSH po instalacji."
    have sshpass || die "Brak sshpass po instalacji."
    have flock || die "Brak flock po instalacji."
}

save_flattened_kubeconfig() {
    local -a base=("$@")
    mkdir -p "$STATE_DIR"
    chmod 700 "$STATE_DIR"
    "${base[@]}" config view --raw --flatten --minify > "${KUBECONFIG_FILE}.tmp"
    [[ -s "${KUBECONFIG_FILE}.tmp" ]] || return 1
    chmod 600 "${KUBECONFIG_FILE}.tmp"
    mv -f "${KUBECONFIG_FILE}.tmp" "$KUBECONFIG_FILE"
}

setup_kubernetes() {
    mkdir -p "$STATE_DIR"
    chmod 700 "$STATE_DIR"

    if have kubectl && [[ -s "$KUBECONFIG_FILE" ]] && kubectl --kubeconfig "$KUBECONFIG_FILE" get namespaces >/dev/null 2>&1; then
        KUBE_MODE="kubectl"
        KUBE_CMD=(kubectl --kubeconfig "$KUBECONFIG_FILE")
        return 0
    fi

    if have k3s && k3s kubectl get namespaces >/dev/null 2>&1; then
        KUBE_MODE="k3s"
        KUBE_CMD=(k3s kubectl)
        return 0
    fi

    if have microk8s && microk8s kubectl get namespaces >/dev/null 2>&1; then
        KUBE_MODE="microk8s"
        KUBE_CMD=(microk8s kubectl)
        return 0
    fi

    if have kubectl && kubectl get namespaces >/dev/null 2>&1; then
        save_flattened_kubeconfig kubectl || die "Nie udało się zapisać kubeconfig."
        KUBE_MODE="kubectl"
        KUBE_CMD=(kubectl --kubeconfig "$KUBECONFIG_FILE")
        return 0
    fi

    if have kubectl; then
        local candidate sudo_home=""
        if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
            sudo_home="$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6 || true)"
        fi

        for candidate in \
            "${KUBECONFIG:-}" \
            "/root/.kube/config" \
            "${sudo_home:+${sudo_home}/.kube/config}" \
            "/etc/rancher/k3s/k3s.yaml"; do
            [[ -n "$candidate" && -r "$candidate" ]] || continue
            if kubectl --kubeconfig "$candidate" get namespaces >/dev/null 2>&1; then
                save_flattened_kubeconfig kubectl --kubeconfig "$candidate" || die "Nie udało się zapisać kubeconfig."
                KUBE_MODE="kubectl"
                KUBE_CMD=(kubectl --kubeconfig "$KUBECONFIG_FILE")
                return 0
            fi
        done
    fi

    die "Nie wykryto działającego dostępu do Kubernetes. Wymagany jest kubectl, k3s lub microk8s z dostępem do klastra AWX."
}

k() {
    "${KUBE_CMD[@]}" "$@"
}

AWX_NAMESPACE=""
AWX_POD=""
AWX_CONTAINER=""

detect_awx() {
    local lines ns pod phase containers container
    lines="$(k get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"|"}{.metadata.name}{"|"}{.status.phase}{"|"}{range .spec.containers[*]}{.name}{","}{end}{"\n"}{end}')" \
        || die "Nie udało się pobrać listy podów Kubernetes."

    while IFS='|' read -r ns pod phase containers; do
        [[ "$phase" == "Running" ]] || continue
        [[ "$pod" =~ [Aa][Ww][Xx].*[Tt][Aa][Ss][Kk] ]] || continue

        IFS=',' read -r -a container_list <<< "$containers"
        for container in "${container_list[@]}"; do
            [[ -n "$container" ]] || continue
            if k exec -n "$ns" "$pod" -c "$container" -- awx-manage --help >/dev/null 2>&1; then
                AWX_NAMESPACE="$ns"
                AWX_POD="$pod"
                AWX_CONTAINER="$container"
                log "Wykryto AWX: namespace=$AWX_NAMESPACE pod=$AWX_POD container=$AWX_CONTAINER"
                return 0
            fi
        done
    done <<< "$lines"

    # Fallback: sprawdź wszystkie działające pody zawierające "awx" w nazwie.
    while IFS='|' read -r ns pod phase containers; do
        [[ "$phase" == "Running" ]] || continue
        [[ "$pod" =~ [Aa][Ww][Xx] ]] || continue

        IFS=',' read -r -a container_list <<< "$containers"
        for container in "${container_list[@]}"; do
            [[ -n "$container" ]] || continue
            if k exec -n "$ns" "$pod" -c "$container" -- awx-manage --help >/dev/null 2>&1; then
                AWX_NAMESPACE="$ns"
                AWX_POD="$pod"
                AWX_CONTAINER="$container"
                log "Wykryto AWX: namespace=$AWX_NAMESPACE pod=$AWX_POD container=$AWX_CONTAINER"
                return 0
            fi
        done
    done <<< "$lines"

    die "Nie znaleziono działającego poda/kontenera AWX zawierającego polecenie awx-manage."
}

load_state() {
    [[ -r "$STATE_FILE" ]] || die "Brak konfiguracji $STATE_FILE. Najpierw uruchom skrypt bez --sync."
    # shellcheck disable=SC1090
    source "$STATE_FILE"
    [[ -n "${SOURCE_HOST:-}" ]] || die "Brak SOURCE_HOST w $STATE_FILE."
    [[ -n "${SOURCE_USER:-}" ]] || die "Brak SOURCE_USER w $STATE_FILE."
    SOURCE_PORT="${SOURCE_PORT:-22}"
    INVENTORY_NAME="${INVENTORY_NAME:-SSH Inventory - ${SOURCE_HOST}}"
    LAST_HASH="${LAST_HASH:-}"
}

shell_assign() {
    local key="$1" value="$2"
    printf '%s=%q\n' "$key" "$value"
}

save_state() {
    local tmp="${STATE_FILE}.tmp"
    {
        shell_assign SOURCE_HOST "$SOURCE_HOST"
        shell_assign SOURCE_PORT "$SOURCE_PORT"
        shell_assign SOURCE_USER "$SOURCE_USER"
        shell_assign INVENTORY_NAME "$INVENTORY_NAME"
        shell_assign LAST_HASH "$LAST_HASH"
    } > "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$STATE_FILE"
}

setup_key() {
    mkdir -p "$STATE_DIR"
    chmod 700 "$STATE_DIR"
    touch "$KNOWN_HOSTS"
    chmod 600 "$KNOWN_HOSTS"

    if [[ ! -s "$KEY_FILE" ]]; then
        log "Generuję dedykowany klucz ED25519..."
        ssh-keygen -q -t ed25519 -N '' -C "${APP}@$(hostname -s)" -f "$KEY_FILE"
    fi
    chmod 600 "$KEY_FILE"
    chmod 644 "${KEY_FILE}.pub"
}

refresh_host_key() {
    local scanned fingerprint
    scanned="$(mktemp)"
    ssh-keyscan -T 10 -p "$SOURCE_PORT" -H "$SOURCE_HOST" > "$scanned" 2>/dev/null \
        || die "Nie udało się pobrać klucza hosta SSH z ${SOURCE_HOST}:${SOURCE_PORT}."
    [[ -s "$scanned" ]] || die "Serwer ${SOURCE_HOST}:${SOURCE_PORT} nie zwrócił klucza SSH."

    fingerprint="$(ssh-keygen -lf "$scanned" -E sha256 2>/dev/null | head -n1 | awk '{print $2}' || true)"
    log "Klucz hosta SSH: ${fingerprint:-nie udało się odczytać fingerprintu}"

    # Pierwsze uruchomienie działa jako TOFU. Później zachowujemy zapisany known_hosts.
    if [[ ! -s "$KNOWN_HOSTS" ]]; then
        cat "$scanned" > "$KNOWN_HOSTS"
        chmod 600 "$KNOWN_HOSTS"
    fi
    rm -f "$scanned"
}

ssh_base() {
    SSH_BASE=(
        ssh
        -i "$KEY_FILE"
        -p "$SOURCE_PORT"
        -o BatchMode=yes
        -o ConnectTimeout=15
        -o ServerAliveInterval=15
        -o ServerAliveCountMax=2
        -o UserKnownHostsFile="$KNOWN_HOSTS"
        -o StrictHostKeyChecking=yes
        "${SOURCE_USER}@${SOURCE_HOST}"
    )
}

remote_exec() {
    ssh_base
    "${SSH_BASE[@]}" "$@"
}

install_public_key() {
    local password="$1" pub
    pub="$(cat "${KEY_FILE}.pub")"

    if remote_exec 'true' >/dev/null 2>&1; then
        log "Logowanie wygenerowanym kluczem SSH już działa."
        return 0
    fi

    log "Instaluję klucz publiczny na serwerze źródłowym..."
    SSHPASS="$password" sshpass -e ssh \
        -p "$SOURCE_PORT" \
        -o PreferredAuthentications=password,keyboard-interactive \
        -o PubkeyAuthentication=no \
        -o ConnectTimeout=15 \
        -o UserKnownHostsFile="$KNOWN_HOSTS" \
        -o StrictHostKeyChecking=yes \
        "${SOURCE_USER}@${SOURCE_HOST}" \
        "umask 077; mkdir -p \"\$HOME/.ssh\"; touch \"\$HOME/.ssh/authorized_keys\"; grep -qxF '$pub' \"\$HOME/.ssh/authorized_keys\" || printf '%s\\n' '$pub' >> \"\$HOME/.ssh/authorized_keys\"" \
        >/dev/null || die "Nie udało się zalogować hasłem lub zainstalować klucza SSH."

    remote_exec 'true' >/dev/null 2>&1 \
        || die "Klucz został wysłany, ale logowanie nim nadal nie działa. Sprawdź konfigurację sshd/authorized_keys."

    log "Logowanie kluczem SSH działa. Hasło nie będzie więcej potrzebne."
}

sanitize_alias() {
    printf '%s' "$1" | tr -cs 'A-Za-z0-9_.-' '-' | sed 's/^-*//;s/-*$//'
}

fetch_inventory() {
    local out_file="$1" mode_file="$2" remote_path="" ext="" host_alias=""

    # Najpierw używamy Ansible na serwerze źródłowym. To poprawnie rozwija
    # inventory plugins, group_vars i host_vars dostępne w jego konfiguracji.
    if remote_exec 'command -v ansible-inventory >/dev/null 2>&1' >/dev/null 2>&1; then
        if remote_exec 'ansible-inventory --list --export' > "$out_file" 2>/dev/null && [[ -s "$out_file" ]]; then
            printf 'json\n' > "$mode_file"
            log "Pobrano inventory przez ansible-inventory --list --export."
            return 0
        fi
    fi

    # Fallback dla hostów, które mają statyczny plik inventory, ale nie mają
    # polecenia ansible-inventory w PATH.
    remote_path="$(remote_exec 'for f in /etc/ansible/hosts /etc/ansible/inventory /etc/ansible/inventory.ini /etc/ansible/inventory.yml /etc/ansible/inventory.yaml /opt/ansible/inventory /opt/ansible/inventory.ini /opt/ansible/inventory.yml /opt/ansible/inventory.yaml "$HOME/ansible/inventory" "$HOME/ansible/inventory.ini" "$HOME/ansible/inventory.yml" "$HOME/ansible/inventory.yaml"; do [ -f "$f" ] && { printf "%s\\n" "$f"; exit 0; }; done; find /etc/ansible /opt/ansible "$HOME" -maxdepth 3 -type f \( -name "inventory" -o -name "inventory.ini" -o -name "inventory.yml" -o -name "inventory.yaml" -o -name "inventory.json" \) 2>/dev/null | head -n1' 2>/dev/null || true)"

    if [[ -n "$remote_path" ]]; then
        remote_exec "cat -- $(printf '%q' "$remote_path")" > "$out_file"
        [[ -s "$out_file" ]] || die "Znaleziono $remote_path, ale nie udało się pobrać jego zawartości."

        ext="${remote_path##*.}"
        case "$ext" in
            yml|yaml|json|ini) printf '%s\n' "$ext" > "$mode_file" ;;
            *) printf 'ini\n' > "$mode_file" ;;
        esac
        log "Pobrano statyczne inventory: $remote_path"
        return 0
    fi

    # Jeżeli serwer nie posiada własnego inventory Ansible, importujemy sam
    # serwer źródłowy, dzięki czemu skrypt nadal jest autonomiczny.
    host_alias="$(remote_exec 'hostname -f 2>/dev/null || hostname' 2>/dev/null | head -n1 || true)"
    host_alias="$(sanitize_alias "${host_alias:-$SOURCE_HOST}")"
    [[ -n "$host_alias" ]] || host_alias="ssh-source"

    cat > "$out_file" <<EOF_INVENTORY
[all]
${host_alias} ansible_host=${SOURCE_HOST} ansible_user=${SOURCE_USER} ansible_port=${SOURCE_PORT}
EOF_INVENTORY
    printf 'ini\n' > "$mode_file"
    log "Nie znaleziono inventory na serwerze. Utworzono inventory zawierające sam serwer źródłowy."
}

ensure_awx_inventory() {
    local py output inventory_id
    py='import os; from awx.main.models import Inventory, Organization; org=Organization.objects.order_by("id").first(); assert org is not None, "No AWX organization found"; name=os.environ["SYNC_INVENTORY_NAME"]; inv,_=Inventory.objects.get_or_create(name=name, organization=org, defaults={"description":"Managed by awx-inventory-sync"}); print("SYNC_INVENTORY_ID=%s" % inv.id)'

    output="$(k exec -n "$AWX_NAMESPACE" "$AWX_POD" -c "$AWX_CONTAINER" -- \
        env "SYNC_INVENTORY_NAME=$INVENTORY_NAME" awx-manage shell -c "$py")" \
        || die "Nie udało się utworzyć/odnaleźć Inventory w AWX."

    inventory_id="$(printf '%s\n' "$output" | sed -n 's/.*SYNC_INVENTORY_ID=\([0-9][0-9]*\).*/\1/p' | tail -n1)"
    [[ "$inventory_id" =~ ^[0-9]+$ ]] || die "AWX nie zwrócił poprawnego Inventory ID."
    printf '%s\n' "$inventory_id"
}

import_into_awx() {
    local local_file="$1" source_type="$2" inventory_id="$3" pod_file
    case "$source_type" in
        yml|yaml|json|ini) ;;
        *) source_type="ini" ;;
    esac

    pod_file="/tmp/${APP}-$$.${source_type}"

    k exec -i -n "$AWX_NAMESPACE" "$AWX_POD" -c "$AWX_CONTAINER" -- \
        sh -c "cat > '$pod_file'" < "$local_file" \
        || die "Nie udało się przesłać inventory do poda AWX."

    if ! k exec -n "$AWX_NAMESPACE" "$AWX_POD" -c "$AWX_CONTAINER" -- \
        awx-manage inventory_import \
            --inventory-id="$inventory_id" \
            --source="$pod_file" \
            --overwrite \
            --overwrite-vars; then
        k exec -n "$AWX_NAMESPACE" "$AWX_POD" -c "$AWX_CONTAINER" -- rm -f "$pod_file" >/dev/null 2>&1 || true
        die "Import inventory do AWX zakończył się błędem."
    fi

    k exec -n "$AWX_NAMESPACE" "$AWX_POD" -c "$AWX_CONTAINER" -- rm -f "$pod_file" >/dev/null 2>&1 || true
    log "Inventory zostało zaimportowane do AWX (ID=$inventory_id)."
}

sync_inventory() {
    local tmp type_file source_type inventory_id current_hash
    exec 9>"$LOCK_FILE"
    flock -n 9 || { log "Synchronizacja już trwa; pomijam to uruchomienie."; return 0; }

    load_state
    setup_key
    setup_kubernetes
    detect_awx

    remote_exec 'true' >/dev/null 2>&1 \
        || die "Nie można połączyć się z ${SOURCE_USER}@${SOURCE_HOST}:${SOURCE_PORT} przy użyciu zapisanego klucza. Uruchom skrypt ponownie bez --sync, aby wykonać bootstrap."

    tmp="$(mktemp)"
    type_file="$(mktemp)"
    fetch_inventory "$tmp" "$type_file"
    source_type="$(cat "$type_file")"

    inventory_id="$(ensure_awx_inventory)"
    current_hash="$(sha256sum "$tmp" | awk '{print $1}')"

    if [[ -n "${LAST_HASH:-}" && "$current_hash" == "$LAST_HASH" ]]; then
        log "Inventory bez zmian; import do AWX nie jest potrzebny."
        rm -f "$tmp" "$type_file"
        return 0
    fi

    import_into_awx "$tmp" "$source_type" "$inventory_id"
    LAST_HASH="$current_hash"
    save_state
    rm -f "$tmp" "$type_file"
}

install_self() {
    local src
    src="$(readlink -f "$0")"
    [[ -r "$src" ]] || die "Nie można odczytać bieżącego skryptu: $src"

    if [[ "$src" != "$INSTALL_PATH" ]]; then
        install -m 0755 "$src" "$INSTALL_PATH"
    else
        chmod 0755 "$INSTALL_PATH"
    fi
}

install_cron() {
    cat > "$CRON_FILE" <<EOF_CRON
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
${CRON_SCHEDULE} root ${INSTALL_PATH} --sync >> ${LOG_FILE} 2>&1
EOF_CRON
    chmod 644 "$CRON_FILE"
    log "Zainstalowano cron: ${CRON_SCHEDULE}"
}

bootstrap() {
    local password
    require_root
    install_dependencies
    setup_kubernetes
    detect_awx

    printf '\nAWX Inventory Sync - konfiguracja\n\n'
    read -r -p 'Adres IP/DNS serwera źródłowego: ' SOURCE_HOST
    [[ -n "$SOURCE_HOST" ]] || die "Adres serwera nie może być pusty."

    read -r -p 'Port SSH [22]: ' SOURCE_PORT
    SOURCE_PORT="${SOURCE_PORT:-22}"
    [[ "$SOURCE_PORT" =~ ^[0-9]+$ ]] && (( SOURCE_PORT >= 1 && SOURCE_PORT <= 65535 )) \
        || die "Niepoprawny port SSH."

    read -r -p 'Użytkownik SSH: ' SOURCE_USER
    [[ -n "$SOURCE_USER" ]] || die "Użytkownik SSH nie może być pusty."

    read -r -s -p 'Hasło SSH (tylko do instalacji klucza): ' password
    printf '\n'
    [[ -n "$password" ]] || die "Hasło SSH nie może być puste."

    INVENTORY_NAME="SSH Inventory - ${SOURCE_HOST}"
    LAST_HASH=""

    setup_key
    : > "$KNOWN_HOSTS"
    chmod 600 "$KNOWN_HOSTS"
    refresh_host_key
    install_public_key "$password"
    password=''
    unset password

    save_state
    install_self
    install_cron

    log "Wykonuję pierwszą synchronizację..."
    "$INSTALL_PATH" --sync

    printf '\nGotowe.\n'
    printf 'Inventory AWX: %s\n' "$INVENTORY_NAME"
    printf 'Synchronizacja: co 5 minut\n'
    printf 'Skrypt: %s\n' "$INSTALL_PATH"
    printf 'Stan: %s\n' "$STATE_FILE"
    printf 'Klucz SSH: %s\n' "$KEY_FILE"
    printf 'Log: %s\n' "$LOG_FILE"
}

status() {
    require_root
    load_state
    printf 'Źródło SSH: %s@%s:%s\n' "$SOURCE_USER" "$SOURCE_HOST" "$SOURCE_PORT"
    printf 'Inventory AWX: %s\n' "$INVENTORY_NAME"
    printf 'Cron: %s\n' "$CRON_FILE"
    printf 'Klucz: %s\n' "$KEY_FILE"
    printf 'Ostatni hash: %s\n' "${LAST_HASH:-brak}"
    setup_kubernetes
    detect_awx
}

uninstall_sync() {
    require_root
    rm -f "$CRON_FILE" "$INSTALL_PATH"
    log "Usunięto cron i zainstalowaną kopię skryptu. Dane w $STATE_DIR pozostawiono celowo."
}

usage() {
    cat <<EOF_USAGE
Użycie:
  sudo bash $0            konfiguracja interaktywna / ponowny bootstrap
  sudo $INSTALL_PATH --sync       ręczna synchronizacja
  sudo $INSTALL_PATH --status     status
  sudo $INSTALL_PATH --uninstall  usuń cron i zainstalowaną kopię skryptu
EOF_USAGE
}

main() {
    case "${1:-}" in
        '') bootstrap ;;
        --sync) require_root; sync_inventory ;;
        --status) status ;;
        --uninstall) uninstall_sync ;;
        -h|--help) usage ;;
        *) usage; exit 2 ;;
    esac
}

main "$@"
