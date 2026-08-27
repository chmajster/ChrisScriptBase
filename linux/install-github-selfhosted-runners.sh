#!/usr/bin/env bash

set -Eeuo pipefail

# ============================================================
# GitHub Self-Hosted Runner Installer
# ============================================================
# MODE=user
#   - pobiera wszystkie niearchiwalne repozytoria użytkownika
#   - tworzy osobnego runnera dla każdego repozytorium
#
# MODE=org
#   - tworzy jeden organization-level runner
#
# Dane GitHub mogą być przekazane przez zmienne środowiskowe
# albo odczytane z ~/.gitconfig użytkownika uruchamiającego skrypt.
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

log() {
    echo
    echo "============================================================"
    echo "$*"
    echo "============================================================"
}

warn() {
    echo "WARNING: $*" >&2
}

error() {
    echo "ERROR: $*" >&2
    exit 1
}

show_help() {
    cat <<'HELP'
GitHub Self-Hosted Runner Installer

Użycie:
  ./install-github-selfhosted-runners.sh --help
  sudo ./install-github-selfhosted-runners.sh
  sudo -E ./install-github-selfhosted-runners.sh

Opcje:
  -h, --help     Wyświetla pomoc.

Źródła konfiguracji:
  1. Zmienne środowiskowe mają najwyższy priorytet.
  2. Jeśli GITHUB_OWNER/GITHUB_TOKEN nie są ustawione, skrypt czyta
     ~/.gitconfig użytkownika, który uruchomił sudo.

Obsługiwane wpisy ~/.gitconfig:
  MODE=user:
    github.username
    github.tokenBase64

  MODE=org:
    github.organization
    github.tokenBase64

Przykład:
  [github]
      username = chmajster
      tokenBase64 = <TOKEN_W_BASE64>

Zmienne środowiskowe:
  GITHUB_OWNER   Nazwa użytkownika GitHub lub organizacji.
  GITHUB_TOKEN   GitHub Personal Access Token.
  MODE           user albo org. Domyślnie: user.
  RUNNER_USER    Użytkownik usługi. Domyślnie: github-runner.
  RUNNER_BASE    Katalog bazowy. Domyślnie: /opt/github-runners.
  CUSTOM_LABELS  Etykiety runnera. Domyślnie: homelab.

Wymagane uprawnienia tokenu dla MODE=user:
  Fine-grained PAT:
    - dostęp do wybranych repozytoriów
    - Repository permissions -> Administration: Read and write
  Classic PAT:
    - scope: repo
  Użytkownik tokenu musi mieć admin access do repozytorium.

Wymagane uprawnienia tokenu dla MODE=org:
  Fine-grained PAT:
    - Organization permissions -> Self-hosted runners: Read and write
  Classic PAT:
    - scope: admin:org
    - dla prywatnych repozytoriów może być wymagany również scope repo
  Użytkownik tokenu musi mieć admin access do organizacji.

Przykłady:
  sudo ./install-github-selfhosted-runners.sh

  export GITHUB_OWNER="chmajster"
  export GITHUB_TOKEN="github_pat_xxxxxxxxxxxxxxxxx"
  export MODE="user"
  sudo -E ./install-github-selfhosted-runners.sh

  export GITHUB_OWNER="moja-organizacja"
  export MODE="org"
  sudo -E ./install-github-selfhosted-runners.sh

Sprawdzenie usług:
  systemctl --type=service | grep actions.runner

Logi:
  journalctl -u 'actions.runner.*' -f
HELP
}

require_root() {
    [[ "$EUID" -eq 0 ]] || error "Uruchom skrypt jako root lub przez sudo."
}

install_dependencies() {
    log "Instalacja zależności"
    apt-get update
    apt-get install -y curl jq tar gzip ca-certificates git
}

get_invoking_user() {
    if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
        printf '%s\n' "$SUDO_USER"
    else
        id -un
    fi
}

get_user_home() {
    local username="$1"
    local home

    home="$(getent passwd "$username" | cut -d: -f6)"
    [[ -n "$home" ]] || error "Nie można ustalić HOME użytkownika: $username"
    printf '%s\n' "$home"
}

read_gitconfig_value() {
    local key="$1"
    [[ -f "$GITCONFIG_PATH" ]] || return 1
    git config --file "$GITCONFIG_PATH" --get "$key" 2>/dev/null || true
}

load_gitconfig_credentials() {
    INVOKING_USER="$(get_invoking_user)"
    INVOKING_HOME="$(get_user_home "$INVOKING_USER")"
    GITCONFIG_PATH="${INVOKING_HOME}/.gitconfig"

    [[ -n "$GITHUB_OWNER" ]] && GITHUB_OWNER_SOURCE="environment"
    [[ -n "$GITHUB_TOKEN" ]] && GITHUB_TOKEN_SOURCE="environment"

    if [[ ! -f "$GITCONFIG_PATH" ]]; then
        warn "Brak $GITCONFIG_PATH; używam wyłącznie zmiennych środowiskowych."
        return
    fi

    if [[ -z "$GITHUB_OWNER" ]]; then
        case "$MODE" in
            user) GITHUB_OWNER="$(read_gitconfig_value 'github.username')" ;;
            org)  GITHUB_OWNER="$(read_gitconfig_value 'github.organization')" ;;
        esac
        [[ -n "$GITHUB_OWNER" ]] && GITHUB_OWNER_SOURCE="$GITCONFIG_PATH"
    fi

    if [[ -z "$GITHUB_TOKEN" ]]; then
        local token_base64 decoded_token
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

check_config() {
    case "$MODE" in
        user|org) ;;
        *) error "MODE musi mieć wartość user albo org." ;;
    esac

    if [[ -z "$GITHUB_OWNER" ]]; then
        if [[ "$MODE" == "org" ]]; then
            error "Brak GITHUB_OWNER. Ustaw GITHUB_OWNER albo github.organization w $GITCONFIG_PATH."
        fi
        error "Brak GITHUB_OWNER. Ustaw GITHUB_OWNER albo github.username w $GITCONFIG_PATH."
    fi

    [[ -n "$GITHUB_TOKEN" ]] || \
        error "Brak GITHUB_TOKEN. Ustaw GITHUB_TOKEN albo github.tokenBase64 w $GITCONFIG_PATH."
}

print_api_error() {
    local status="$1"
    local method="$2"
    local endpoint="$3"
    local body_file="$4"
    local headers_file="$5"
    local message docs request_id remaining

    message="$(jq -r '.message // empty' "$body_file" 2>/dev/null || true)"
    docs="$(jq -r '.documentation_url // empty' "$body_file" 2>/dev/null || true)"
    request_id="$(awk -F': ' 'tolower($1)=="x-github-request-id" {gsub("\r", "", $2); print $2}' "$headers_file" | tail -n1)"
    remaining="$(awk -F': ' 'tolower($1)=="x-ratelimit-remaining" {gsub("\r", "", $2); print $2}' "$headers_file" | tail -n1)"

    echo >&2
    echo "GitHub API ERROR" >&2
    echo "  HTTP:       $status" >&2
    echo "  Request:    $method $endpoint" >&2
    [[ -n "$message" ]] && echo "  Message:    $message" >&2
    [[ -n "$request_id" ]] && echo "  Request ID: $request_id" >&2
    [[ -n "$remaining" ]] && echo "  Rate limit: $remaining remaining" >&2
    [[ -n "$docs" ]] && echo "  Docs:       $docs" >&2

    case "$status" in
        401)
            echo >&2
            echo "Przyczyna: token jest nieprawidłowy, wygasł albo został unieważniony." >&2
            ;;
        403)
            echo >&2
            echo "Przyczyna: GitHub rozpoznał żądanie, ale token/konto nie ma wymaganych uprawnień." >&2

            if [[ "$endpoint" == /repos/*/actions/runners/registration-token ]]; then
                echo "Dla rejestracji runnera repozytorium wymagane jest:" >&2
                echo "  - admin access do repozytorium" >&2
                echo "  - Fine-grained PAT: Administration = Read and write" >&2
                echo "    oraz repozytorium musi być objęte dostępem tokenu" >&2
                echo "  - Classic PAT: scope repo" >&2
            elif [[ "$endpoint" == /orgs/*/actions/runners/registration-token ]]; then
                echo "Dla rejestracji runnera organizacji wymagane jest:" >&2
                echo "  - admin access do organizacji" >&2
                echo "  - Fine-grained PAT: Self-hosted runners = Read and write" >&2
                echo "  - Classic PAT: scope admin:org" >&2
            fi

            if [[ "$remaining" == "0" ]]; then
                echo "  - limit GitHub API jest wyczerpany" >&2
            fi

            echo "Sprawdź także SAML/SSO, jeśli organizacja wymaga autoryzacji tokenu." >&2
            ;;
        404)
            echo >&2
            echo "Przyczyna: zasób nie istnieje albo token nie ma do niego dostępu." >&2
            ;;
        422)
            echo >&2
            echo "Przyczyna: GitHub odrzucił parametry żądania lub wystąpiła walidacja API." >&2
            ;;
        429)
            echo >&2
            echo "Przyczyna: przekroczono limit żądań GitHub API." >&2
            ;;
    esac
}

github_api() {
    local method="$1"
    local endpoint="$2"
    local body_file headers_file status curl_rc

    body_file="$(mktemp)"
    headers_file="$(mktemp)"

    status="$(
        curl \
            --silent \
            --show-error \
            --location \
            --request "$method" \
            --header "Accept: application/vnd.github+json" \
            --header "Authorization: Bearer ${GITHUB_TOKEN}" \
            --header "X-GitHub-Api-Version: ${API_VERSION}" \
            --dump-header "$headers_file" \
            --output "$body_file" \
            --write-out '%{http_code}' \
            "https://api.github.com${endpoint}"
    )" || {
        curl_rc=$?
        echo "Błąd połączenia z GitHub API (curl exit code: $curl_rc): $method $endpoint" >&2
        rm -f "$body_file" "$headers_file"
        return "$curl_rc"
    }

    if [[ ! "$status" =~ ^2[0-9][0-9]$ ]]; then
        print_api_error "$status" "$method" "$endpoint" "$body_file" "$headers_file"
        rm -f "$body_file" "$headers_file"
        return 1
    fi

    cat "$body_file"
    rm -f "$body_file" "$headers_file"
}

validate_github_token() {
    local response login

    log "Weryfikacja tokenu GitHub"

    if ! response="$(github_api GET '/user')"; then
        error "Nie udało się uwierzytelnić tokenu GitHub."
    fi

    login="$(printf '%s' "$response" | jq -r '.login // empty')"
    [[ -n "$login" ]] || error "GitHub API nie zwrócił loginu użytkownika tokenu."

    echo "Token owner:     $login"
    echo "Token source:    ${GITHUB_TOKEN_SOURCE:-unknown}"

    if [[ "$MODE" == "user" && "${login,,}" != "${GITHUB_OWNER,,}" ]]; then
        warn "Token należy do '$login', a GITHUB_OWNER='$GITHUB_OWNER'. Repozytoria będą filtrowane według GITHUB_OWNER."
    fi
}

create_runner_user() {
    if ! id "$RUNNER_USER" &>/dev/null; then
        log "Tworzenie użytkownika $RUNNER_USER"
        useradd --system --create-home --shell /bin/bash "$RUNNER_USER"
    fi

    mkdir -p "$RUNNER_BASE"
    chown -R "$RUNNER_USER:$RUNNER_USER" "$RUNNER_BASE"
}

get_runner_version() {
    curl --silent --show-error --fail \
        "https://api.github.com/repos/actions/runner/releases/latest" |
        jq -r '.tag_name' |
        sed 's/^v//'
}

detect_arch() {
    case "$(uname -m)" in
        x86_64|amd64) echo "x64" ;;
        aarch64|arm64) echo "arm64" ;;
        armv7l) echo "arm" ;;
        *) error "Nieobsługiwana architektura: $(uname -m)" ;;
    esac
}

download_runner() {
    local destination="$1"
    local version="$2"
    local arch="$3"
    local archive="/tmp/actions-runner-${version}-${arch}.tar.gz"
    local url="https://github.com/actions/runner/releases/download/v${version}/actions-runner-linux-${arch}-${version}.tar.gz"

    log "Pobieranie GitHub Actions Runner ${version}"
    curl -L --fail --show-error "$url" -o "$archive"
    mkdir -p "$destination"
    tar xzf "$archive" -C "$destination"
    rm -f "$archive"
    chown -R "$RUNNER_USER:$RUNNER_USER" "$destination"
}

sanitize_name() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9._-'
}

get_repo_token() {
    local repo="$1"
    local response

    response="$(github_api POST "/repos/${GITHUB_OWNER}/${repo}/actions/runners/registration-token")" || return 1
    printf '%s' "$response" | jq -r '.token // empty'
}

install_repo_runner() {
    local repo="$1"
    local version="$2"
    local arch="$3"
    local safe_repo runner_dir hostname runner_name registration_token

    safe_repo="$(sanitize_name "$repo")"
    runner_dir="${RUNNER_BASE}/${safe_repo}"
    hostname="$(hostname -s)"
    runner_name="${hostname}-${safe_repo}"

    log "Repozytorium: ${GITHUB_OWNER}/${repo}"

    if [[ -f "${runner_dir}/.runner" ]]; then
        echo "Runner już istnieje: $runner_dir"
        return 0
    fi

    if ! registration_token="$(get_repo_token "$repo")"; then
        echo >&2
        echo "Nie można pobrać tokenu rejestracyjnego dla:" >&2
        echo "  ${GITHUB_OWNER}/${repo}" >&2
        echo "Pomijam repozytorium." >&2
        rm -rf "$runner_dir"
        return 1
    fi

    if [[ -z "$registration_token" || "$registration_token" == "null" ]]; then
        warn "GitHub zwrócił pusty registration token dla ${GITHUB_OWNER}/${repo}. Pomijam repozytorium."
        rm -rf "$runner_dir"
        return 1
    fi

    mkdir -p "$runner_dir"
    download_runner "$runner_dir" "$version" "$arch"

    log "Konfiguracja runnera: $runner_name"

    sudo -u "$RUNNER_USER" bash -c "
        cd '$runner_dir'
        ./config.sh \\
            --unattended \\
            --url 'https://github.com/${GITHUB_OWNER}/${repo}' \\
            --token '$registration_token' \\
            --name '$runner_name' \\
            --labels '$CUSTOM_LABELS' \\
            --work '_work' \\
            --replace
    "

    unset registration_token

    log "Instalacja systemd"
    cd "$runner_dir"
    ./svc.sh install "$RUNNER_USER"
    ./svc.sh start

    echo "Runner gotowy:"
    echo "  Repository: ${GITHUB_OWNER}/${repo}"
    echo "  Runner:     $runner_name"
    echo "  Directory:  $runner_dir"
}

get_org_token() {
    local response
    response="$(github_api POST "/orgs/${GITHUB_OWNER}/actions/runners/registration-token")" || return 1
    printf '%s' "$response" | jq -r '.token // empty'
}

install_org_runner() {
    local version="$1"
    local arch="$2"
    local runner_dir="${RUNNER_BASE}/organization"
    local hostname runner_name registration_token

    hostname="$(hostname -s)"
    runner_name="${hostname}-${GITHUB_OWNER}"

    if [[ -f "${runner_dir}/.runner" ]]; then
        echo "Organization runner już istnieje: $runner_dir"
        return 0
    fi

    if ! registration_token="$(get_org_token)"; then
        error "Nie udało się pobrać organization registration token dla $GITHUB_OWNER."
    fi

    [[ -n "$registration_token" && "$registration_token" != "null" ]] || \
        error "GitHub zwrócił pusty organization registration token."

    mkdir -p "$runner_dir"
    download_runner "$runner_dir" "$version" "$arch"

    log "Konfiguracja Organization Runner"
    sudo -u "$RUNNER_USER" bash -c "
        cd '$runner_dir'
        ./config.sh \\
            --unattended \\
            --url 'https://github.com/${GITHUB_OWNER}' \\
            --token '$registration_token' \\
            --name '$runner_name' \\
            --labels '$CUSTOM_LABELS' \\
            --work '_work' \\
            --replace
    "

    unset registration_token

    cd "$runner_dir"
    ./svc.sh install "$RUNNER_USER"
    ./svc.sh start

    echo "Organization runner został utworzony."
    echo "Organization: $GITHUB_OWNER"
    echo "Runner:       $runner_name"
}

get_user_repositories() {
    local page=1 response count

    while true; do
        response="$(github_api GET "/user/repos?affiliation=owner&per_page=100&page=${page}&sort=full_name")" || return 1
        count="$(printf '%s' "$response" | jq 'length')"
        [[ "$count" -gt 0 ]] || break

        printf '%s' "$response" |
            jq -r --arg owner "$GITHUB_OWNER" '
                .[]
                | select((.owner.login | ascii_downcase) == ($owner | ascii_downcase))
                | select(.archived == false)
                | .name
            '

        ((page++))
    done
}

main() {
    case "${1:-}" in
        -h|--help)
            show_help
            exit 0
            ;;
        "") ;;
        *) error "Nieznana opcja: $1. Użyj --help." ;;
    esac

    require_root
    install_dependencies
    load_gitconfig_credentials
    check_config
    validate_github_token
    create_runner_user

    local version arch
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
        install_org_runner "$version" "$arch"
        exit 0
    fi

    log "Pobieranie repozytoriów użytkownika $GITHUB_OWNER"

    local -a repositories=()
    mapfile -t repositories < <(get_user_repositories)

    [[ ${#repositories[@]} -gt 0 ]] || error "Nie znaleziono repozytoriów lub GitHub API odmówił dostępu."

    echo "Znalezione repozytoria: ${#repositories[@]}"
    printf ' - %s\n' "${repositories[@]}"

    local success=0 failed=0 skipped=0 repo

    for repo in "${repositories[@]}"; do
        if [[ -f "${RUNNER_BASE}/$(sanitize_name "$repo")/.runner" ]]; then
            install_repo_runner "$repo" "$version" "$arch"
            ((skipped+=1))
            continue
        fi

        if install_repo_runner "$repo" "$version" "$arch"; then
            ((success+=1))
        else
            ((failed+=1))
        fi
    done

    log "Podsumowanie"
    echo "Repozytoria:      ${#repositories[@]}"
    echo "Zainstalowane:    $success"
    echo "Już istniejące:   $skipped"
    echo "Błędy/pominięte:  $failed"

    echo
    echo "Usługi runnerów:"
    systemctl --no-pager --type=service | grep 'actions.runner' || true

    if [[ "$failed" -gt 0 ]]; then
        echo >&2
        echo "Co najmniej jedno repozytorium nie otrzymało runnera. Szczegóły błędów są powyżej." >&2
        exit 2
    fi
}

main "$@"
