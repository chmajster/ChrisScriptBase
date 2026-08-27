#!/usr/bin/env bash

set -Eeuo pipefail

# ============================================================
# GitHub Self-Hosted Runner Installer
#
# MODE=user
#   - pobiera wszystkie repozytoria należące do użytkownika
#   - tworzy osobnego runnera dla każdego repozytorium
#
# MODE=org
#   - tworzy jeden organization-level runner
#   - dostępny dla repozytoriów organizacji
#
# Dane GitHub mogą zostać przekazane przez zmienne środowiskowe
# albo odczytane z ~/.gitconfig użytkownika uruchamiającego skrypt.
#
# Wymagania:
#   Ubuntu / Debian
#   root
#   GitHub PAT
# ============================================================

MODE="${MODE:-user}"
GITHUB_OWNER="${GITHUB_OWNER:-}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"

RUNNER_USER="${RUNNER_USER:-github-runner}"
RUNNER_BASE="${RUNNER_BASE:-/opt/github-runners}"

CUSTOM_LABELS="${CUSTOM_LABELS:-homelab}"
API_VERSION="2026-03-10"

INVOKING_USER=""
INVOKING_HOME=""
GITCONFIG_PATH=""
GITHUB_OWNER_SOURCE=""
GITHUB_TOKEN_SOURCE=""

# ============================================================
# Funkcje
# ============================================================

log() {
    echo
    echo "============================================================"
    echo "$*"
    echo "============================================================"
}

error() {
    echo "ERROR: $*" >&2
    exit 1
}

show_help() {
    cat <<'EOF'
GitHub Self-Hosted Runner Installer

Użycie:
  ./install-github-selfhosted-runners.sh --help
  sudo ./install-github-selfhosted-runners.sh
  sudo -E ./install-github-selfhosted-runners.sh

Opcje:
  -h, --help     Wyświetla tę pomoc i kończy działanie.

Źródła konfiguracji:
  1. Zmienne środowiskowe mają najwyższy priorytet.
  2. Jeżeli GITHUB_OWNER lub GITHUB_TOKEN nie są ustawione, skrypt próbuje
     odczytać dane z ~/.gitconfig użytkownika, który uruchomił sudo.

Obsługiwane wpisy ~/.gitconfig:

  MODE=user:
    github.username
    github.tokenBase64

  MODE=org:
    github.organization
    github.tokenBase64

Przykładowa konfiguracja zgodna z linux/git-config.sh:

  [github]
      username = chmajster
      tokenBase64 = <TOKEN_W_BASE64>

Dla organizacji można dodatkowo ustawić:

  [github]
      organization = moja-organizacja

Zmienne środowiskowe:
  GITHUB_OWNER   Opcjonalne, jeśli odpowiedni owner jest w ~/.gitconfig.
                 Nazwa użytkownika GitHub albo organizacji.
  GITHUB_TOKEN   Opcjonalne, jeśli github.tokenBase64 jest w ~/.gitconfig.
                 GitHub Personal Access Token.
  MODE           Tryb instalacji: user albo org. Domyślnie: user.
  RUNNER_USER    Lokalny użytkownik systemowy runnera.
                 Domyślnie: github-runner.
  RUNNER_BASE    Katalog bazowy instalacji runnerów.
                 Domyślnie: /opt/github-runners.
  CUSTOM_LABELS  Dodatkowe etykiety runnera. Domyślnie: homelab.

Priorytet danych:
  GITHUB_OWNER > ~/.gitconfig
  GITHUB_TOKEN > ~/.gitconfig github.tokenBase64

Tryby:
  MODE=user
    Pobiera wszystkie niearchiwalne repozytoria należące do GITHUB_OWNER
    i tworzy osobną instancję GitHub Actions Runner dla każdego repozytorium.

  MODE=org
    Tworzy jedną instancję GitHub Actions Runner na poziomie organizacji.

Przykłady:

  Użycie tylko ~/.gitconfig użytkownika:
    sudo ./install-github-selfhosted-runners.sh

  Konto użytkownika przez zmienne środowiskowe:
    export GITHUB_OWNER="chmajster"
    export GITHUB_TOKEN="github_pat_xxxxxxxxxxxxxxxxx"
    export MODE="user"
    sudo -E ./install-github-selfhosted-runners.sh

  Organizacja:
    export GITHUB_OWNER="moja-organizacja"
    export MODE="org"
    sudo -E ./install-github-selfhosted-runners.sh

  Własny katalog i etykiety:
    export RUNNER_BASE="/srv/github-runners"
    export CUSTOM_LABELS="homelab,linux,proxmox"
    sudo -E ./install-github-selfhosted-runners.sh

Sprawdzenie usług:
  systemctl --type=service | grep actions.runner

Logi runnerów:
  journalctl -u 'actions.runner.*' -f
EOF
}

require_root() {
    if [[ "$EUID" -ne 0 ]]; then
        error "Uruchom skrypt jako root lub przez sudo."
    fi
}

install_dependencies() {
    log "Instalacja zależności"

    apt-get update

    apt-get install -y \
        curl \
        jq \
        tar \
        gzip \
        ca-certificates \
        git
}

get_invoking_user() {
    if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
        printf '%s\n' "$SUDO_USER"
        return
    fi

    id -un
}

get_user_home() {
    local username="$1"
    local home

    home="$(getent passwd "$username" | cut -d: -f6)"

    [[ -n "$home" ]] || error "Nie można ustalić katalogu HOME użytkownika: $username"

    printf '%s\n' "$home"
}

read_gitconfig_value() {
    local key="$1"

    [[ -f "$GITCONFIG_PATH" ]] || return 1

    git config \
        --file "$GITCONFIG_PATH" \
        --get "$key" \
        2>/dev/null || true
}

load_gitconfig_credentials() {
    INVOKING_USER="$(get_invoking_user)"
    INVOKING_HOME="$(get_user_home "$INVOKING_USER")"
    GITCONFIG_PATH="${INVOKING_HOME}/.gitconfig"

    if [[ -n "$GITHUB_OWNER" ]]; then
        GITHUB_OWNER_SOURCE="environment"
    fi

    if [[ -n "$GITHUB_TOKEN" ]]; then
        GITHUB_TOKEN_SOURCE="environment"
    fi

    if [[ ! -f "$GITCONFIG_PATH" ]]; then
        echo "Brak $GITCONFIG_PATH - używam wyłącznie zmiennych środowiskowych."
        return
    fi

    if [[ -z "$GITHUB_OWNER" ]]; then
        case "$MODE" in
            user)
                GITHUB_OWNER="$(read_gitconfig_value 'github.username')"
                ;;
            org)
                GITHUB_OWNER="$(read_gitconfig_value 'github.organization')"
                ;;
        esac

        if [[ -n "$GITHUB_OWNER" ]]; then
            GITHUB_OWNER_SOURCE="$GITCONFIG_PATH"
        fi
    fi

    if [[ -z "$GITHUB_TOKEN" ]]; then
        local token_base64
        local decoded_token

        token_base64="$(read_gitconfig_value 'github.tokenBase64')"

        if [[ -n "$token_base64" ]]; then
            if decoded_token="$(printf '%s' "$token_base64" | base64 --decode 2>/dev/null)"; then
                GITHUB_TOKEN="$decoded_token"
                GITHUB_TOKEN_SOURCE="$GITCONFIG_PATH"
            else
                error "Nie można zdekodować github.tokenBase64 z $GITCONFIG_PATH."
            fi

            unset token_base64 decoded_token
        fi
    fi
}

create_runner_user() {
    if ! id "$RUNNER_USER" &>/dev/null; then
        log "Tworzenie użytkownika $RUNNER_USER"

        useradd \
            --system \
            --create-home \
            --shell /bin/bash \
            "$RUNNER_USER"
    fi

    mkdir -p "$RUNNER_BASE"

    chown -R "$RUNNER_USER:$RUNNER_USER" "$RUNNER_BASE"
}

check_config() {
    case "$MODE" in
        user|org)
            ;;
        *)
            error "MODE musi mieć wartość user albo org."
            ;;
    esac

    if [[ -z "$GITHUB_OWNER" ]]; then
        if [[ "$MODE" == "org" ]]; then
            error "Brak GITHUB_OWNER. Ustaw zmienną GITHUB_OWNER albo github.organization w $GITCONFIG_PATH."
        fi

        error "Brak GITHUB_OWNER. Ustaw zmienną GITHUB_OWNER albo github.username w $GITCONFIG_PATH."
    fi

    [[ -n "$GITHUB_TOKEN" ]] || \
        error "Brak GITHUB_TOKEN. Ustaw zmienną GITHUB_TOKEN albo github.tokenBase64 w $GITCONFIG_PATH."
}

github_api() {
    local method="$1"
    local endpoint="$2"

    curl \
        --silent \
        --show-error \
        --fail-with-body \
        --request "$method" \
        --header "Accept: application/vnd.github+json" \
        --header "Authorization: Bearer ${GITHUB_TOKEN}" \
        --header "X-GitHub-Api-Version: ${API_VERSION}" \
        "https://api.github.com${endpoint}"
}

get_runner_version() {
    curl \
        --silent \
        --show-error \
        --fail \
        "https://api.github.com/repos/actions/runner/releases/latest" |
        jq -r '.tag_name' |
        sed 's/^v//'
}

detect_arch() {
    case "$(uname -m)" in
        x86_64|amd64)
            echo "x64"
            ;;
        aarch64|arm64)
            echo "arm64"
            ;;
        armv7l)
            echo "arm"
            ;;
        *)
            error "Nieobsługiwana architektura: $(uname -m)"
            ;;
    esac
}

download_runner() {
    local destination="$1"
    local version="$2"
    local arch="$3"

    local archive="/tmp/actions-runner-${version}-${arch}.tar.gz"
    local url="https://github.com/actions/runner/releases/download/v${version}/actions-runner-linux-${arch}-${version}.tar.gz"

    log "Pobieranie GitHub Actions Runner ${version}"

    curl \
        -L \
        --fail \
        --show-error \
        "$url" \
        -o "$archive"

    mkdir -p "$destination"

    tar \
        xzf "$archive" \
        -C "$destination"

    rm -f "$archive"

    chown -R "$RUNNER_USER:$RUNNER_USER" "$destination"
}

sanitize_name() {
    echo "$1" |
        tr '[:upper:]' '[:lower:]' |
        tr -cd 'a-z0-9._-'
}

# ============================================================
# REPOSITORY RUNNER
# ============================================================

get_repo_token() {
    local repo="$1"

    github_api \
        POST \
        "/repos/${GITHUB_OWNER}/${repo}/actions/runners/registration-token" |
        jq -r '.token'
}

install_repo_runner() {
    local repo="$1"
    local version="$2"
    local arch="$3"

    local safe_repo
    safe_repo="$(sanitize_name "$repo")"

    local runner_dir="${RUNNER_BASE}/${safe_repo}"

    local hostname
    hostname="$(hostname -s)"

    local runner_name="${hostname}-${safe_repo}"

    log "Repozytorium: ${GITHUB_OWNER}/${repo}"

    if [[ -f "${runner_dir}/.runner" ]]; then
        echo "Runner już istnieje:"
        echo "  $runner_dir"
        return
    fi

    mkdir -p "$runner_dir"

    download_runner \
        "$runner_dir" \
        "$version" \
        "$arch"

    local registration_token

    if ! registration_token="$(get_repo_token "$repo")"; then
        echo "Nie można pobrać tokenu dla:"
        echo "  ${GITHUB_OWNER}/${repo}"
        echo
        echo "Pomijam repozytorium."
        rm -rf "$runner_dir"
        return
    fi

    if [[ -z "$registration_token" || "$registration_token" == "null" ]]; then
        echo "Niepoprawny registration token."
        rm -rf "$runner_dir"
        return
    fi

    log "Konfiguracja runnera: $runner_name"

    sudo -u "$RUNNER_USER" \
        bash -c "
            cd '$runner_dir'

            ./config.sh \
                --unattended \
                --url 'https://github.com/${GITHUB_OWNER}/${repo}' \
                --token '$registration_token' \
                --name '$runner_name' \
                --labels '$CUSTOM_LABELS' \
                --work '_work' \
                --replace
        "

    log "Instalacja systemd"

    cd "$runner_dir"

    ./svc.sh install "$RUNNER_USER"
    ./svc.sh start

    echo
    echo "Runner gotowy:"
    echo "  Repository: ${GITHUB_OWNER}/${repo}"
    echo "  Runner:     $runner_name"
    echo "  Directory:  $runner_dir"
}

# ============================================================
# ORGANIZATION RUNNER
# ============================================================

get_org_token() {
    github_api \
        POST \
        "/orgs/${GITHUB_OWNER}/actions/runners/registration-token" |
        jq -r '.token'
}

install_org_runner() {
    local version="$1"
    local arch="$2"

    local runner_dir="${RUNNER_BASE}/organization"

    local hostname
    hostname="$(hostname -s)"

    local runner_name="${hostname}-${GITHUB_OWNER}"

    if [[ -f "${runner_dir}/.runner" ]]; then
        echo "Organization runner już istnieje:"
        echo "  $runner_dir"
        return
    fi

    mkdir -p "$runner_dir"

    download_runner \
        "$runner_dir" \
        "$version" \
        "$arch"

    local registration_token

    registration_token="$(get_org_token)"

    [[ -n "$registration_token" ]] || \
        error "Nie udało się otrzymać organization registration token."

    log "Konfiguracja Organization Runner"

    sudo -u "$RUNNER_USER" \
        bash -c "
            cd '$runner_dir'

            ./config.sh \
                --unattended \
                --url 'https://github.com/${GITHUB_OWNER}' \
                --token '$registration_token' \
                --name '$runner_name' \
                --labels '$CUSTOM_LABELS' \
                --work '_work' \
                --replace
        "

    cd "$runner_dir"

    ./svc.sh install "$RUNNER_USER"
    ./svc.sh start

    echo
    echo "Organization runner został utworzony."
    echo
    echo "Organization:"
    echo "  $GITHUB_OWNER"
    echo
    echo "Runner:"
    echo "  $runner_name"
}

# ============================================================
# Pobieranie repozytoriów użytkownika
# ============================================================

get_user_repositories() {
    local page=1

    while true; do
        local response

        response="$(
            github_api \
                GET \
                "/user/repos?affiliation=owner&per_page=100&page=${page}&sort=full_name"
        )"

        local count

        count="$(echo "$response" | jq 'length')"

        [[ "$count" -gt 0 ]] || break

        echo "$response" |
            jq -r \
                --arg owner "$GITHUB_OWNER" \
                '
                .[]
                | select(.owner.login == $owner)
                | select(.archived == false)
                | .name
                '

        ((page++))
    done
}

# ============================================================
# MAIN
# ============================================================

main() {
    case "${1:-}" in
        -h|--help)
            show_help
            exit 0
            ;;
        "")
            ;;
        *)
            error "Nieznana opcja: $1. Użyj --help, aby wyświetlić pomoc."
            ;;
    esac

    require_root
    install_dependencies
    load_gitconfig_credentials
    check_config
    create_runner_user

    local version
    local arch

    version="$(get_runner_version)"
    arch="$(detect_arch)"

    echo
    echo "GitHub owner:    $GITHUB_OWNER"
    echo "Owner source:    ${GITHUB_OWNER_SOURCE:-unknown}"
    echo "Token source:    ${GITHUB_TOKEN_SOURCE:-unknown}"
    echo "Invoking user:   $INVOKING_USER"
    echo "Git config:      $GITCONFIG_PATH"
    echo "Mode:            $MODE"
    echo "Runner version:  $version"
    echo "Architecture:    $arch"
    echo "Runner user:     $RUNNER_USER"
    echo "Runner base:     $RUNNER_BASE"

    if [[ "$MODE" == "org" ]]; then
        install_org_runner \
            "$version" \
            "$arch"

        exit 0
    fi

    log "Pobieranie repozytoriów użytkownika $GITHUB_OWNER"

    mapfile -t repositories < <(get_user_repositories)

    if [[ ${#repositories[@]} -eq 0 ]]; then
        error "Nie znaleziono repozytoriów."
    fi

    echo
    echo "Znalezione repozytoria: ${#repositories[@]}"
    echo

    printf ' - %s\n' "${repositories[@]}"

    for repo in "${repositories[@]}"; do
        install_repo_runner \
            "$repo" \
            "$version" \
            "$arch"
    done

    log "Instalacja zakończona"

    echo
    echo "Liczba repozytoriów: ${#repositories[@]}"
    echo
    echo "Usługi runnerów:"
    echo

    systemctl \
        --no-pager \
        --type=service |
        grep 'actions.runner' || true
}

main "$@"
