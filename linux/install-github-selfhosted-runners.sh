#!/usr/bin/env bash
set -Eeuo pipefail

ACTION="install"
DEFAULT_MODE="${MODE:-user}"
ENV_GITHUB_OWNER="${GITHUB_OWNER:-}"
ENV_GITHUB_TOKEN="${GITHUB_TOKEN:-}"
RUNNER_USER="${RUNNER_USER:-github-runner}"
RUNNER_BASE="${RUNNER_BASE:-/opt/github-runners}"
DEFAULT_LABELS="${CUSTOM_LABELS:-homelab}"
API_VERSION="2026-03-10"

INTERACTIVE_REPOS=false
SELECTION_UI="auto"
LIST_PROFILES=false
LIST_REPOS=false
PURGE=false
SELECTED_PROFILES=()
SELECTED_REPOS=()

INVOKING_USER=""
INVOKING_HOME=""
GITCONFIG_PATH=""
ACTIVE_PROFILE="default"
MODE="$DEFAULT_MODE"
GITHUB_OWNER=""
GITHUB_TOKEN=""
CUSTOM_LABELS="$DEFAULT_LABELS"
GITHUB_OWNER_SOURCE=""
GITHUB_TOKEN_SOURCE=""

log() {
    echo
    echo "============================================================"
    echo "$*"
    echo "============================================================"
}

warn() { echo "WARNING: $*" >&2; }
error() { echo "ERROR: $*" >&2; exit 1; }

show_help() {
cat <<'HELP'
GitHub Self-Hosted Runner Manager

Jeden skrypt do instalacji i odinstalowania GitHub Actions self-hosted runnerów.
Obsługuje wiele profili GitHub, wybór konkretnych repozytoriów oraz GUI.

UŻYCIE
  sudo ./install-github-selfhosted-runners.sh [opcje]
  sudo ./install-github-selfhosted-runners.sh --install [opcje]
  sudo ./install-github-selfhosted-runners.sh --uninstall [opcje]

AKCJE
  --install
      Instaluje runnery. Akcja domyślna.

  --uninstall
      Zatrzymuje usługę systemd, wyrejestrowuje runnera z GitHub
      i usuwa jego lokalny katalog.

  --purge
      Z --uninstall usuwa także pusty RUNNER_BASE i RUNNER_USER.

PROFILE
  -p, --profile NAME
      Wybiera profil. Opcję można podać wiele razy.

  --profiles A,B,C
      Wybiera kilka profili.

  --list-profiles
      Pokazuje profile wykryte w ~/.gitconfig.

REPOZYTORIA
  -r, --repo REPO
      Wybiera jedno repozytorium. Opcję można podać wiele razy.

  --repos REPO1,REPO2
      Wybiera kilka repozytoriów.

  --repo PROFILE:REPO
      Przypisuje repozytorium tylko do wskazanego profilu.

  --select-repos
      Uruchamia interaktywny wybór repozytoriów.
      Domyślnie używa graficznego okna Zenity, jeśli dostępna jest
      sesja graficzna. Bez GUI przechodzi do wyboru w terminalu.

  --select-repos -GUI
  --select-repos --gui
      Wymusza graficzne okno wyboru repozytoriów z checkboxami.
      Skrypt używa Zenity i wyświetla okno potwierdzenia przed kontynuacją.

  --select-repos --tui
      Wymusza wybór tekstowy w terminalu.

  --all-repos
      Wszystkie repozytoria MODE=user. To również zachowanie domyślne,
      jeśli nie podano --repo, --repos ani --select-repos.

  --list-repos
      Pokazuje repozytoria dostępne dla wybranych profili.

GUI
  -GUI, --gui
      Wymusza GUI dla --select-repos.

  --tui
      Wymusza interfejs terminalowy dla --select-repos.

  GUI wymaga sesji X11/Wayland. Jeśli Zenity nie jest zainstalowane,
  skrypt spróbuje je doinstalować. Przy sudo okno jest uruchamiane
  jako użytkownik, który wywołał sudo, a nie jako root.

KONFIGURACJA ~/.gitconfig

Profil domyślny:
  [github]
      username = chmajster
      tokenBase64 = <TOKEN_BASE64>

Nazwany profil:
  [github "home"]
      mode = user
      username = chmajster
      tokenBase64 = <TOKEN_BASE64>
      labels = homelab,linux

Profil organizacji:
  [github "work"]
      mode = org
      organization = moja-organizacja
      tokenBase64 = <TOKEN_BASE64>
      labels = work,linux

Pola profilu:
  mode          user albo org
  username      owner dla mode=user
  organization  owner dla mode=org
  owner         uniwersalny owner
  tokenBase64   PAT zakodowany Base64
  labels        etykiety runnera

PRZYKŁADY

  GUI — wybór repozytoriów:
    sudo ./install-github-selfhosted-runners.sh --select-repos -GUI

  GUI — wybór repozytoriów do usunięcia:
    sudo ./install-github-selfhosted-runners.sh --uninstall --select-repos -GUI

  Automatyczny GUI/TUI:
    sudo ./install-github-selfhosted-runners.sh --select-repos

  Jedno repo:
    sudo ./install-github-selfhosted-runners.sh --repo HomeLAB-DNS

  Kilka repo:
    sudo ./install-github-selfhosted-runners.sh \
      --repos HomeLAB-DNS,Algen-server-web-explorer-panel

  Profil home + GUI:
    sudo ./install-github-selfhosted-runners.sh \
      --profile home --select-repos -GUI

  Odinstalowanie repo:
    sudo ./install-github-selfhosted-runners.sh \
      --uninstall --profile home --repo HomeLAB-DNS

  Odinstalowanie wszystkich:
    sudo ./install-github-selfhosted-runners.sh \
      --uninstall --profile home --all-repos

  Pełne czyszczenie:
    sudo ./install-github-selfhosted-runners.sh \
      --uninstall --profile home --all-repos --purge

UPRAWNIENIA TOKENU
  MODE=user, Fine-grained PAT:
      Repository permissions -> Administration: Read and write
  MODE=user, Classic PAT:
      repo

  MODE=org, Fine-grained PAT:
      Organization permissions -> Self-hosted runners: Read and write
  MODE=org, Classic PAT:
      admin:org

DIAGNOSTYKA
  systemctl --type=service | grep actions.runner
  journalctl -u 'actions.runner.*' -f
HELP
}

append_csv() {
    local array_name="$1" csv="$2" item
    local -n target="$array_name"
    local -a parts=()
    IFS=',' read -r -a parts <<< "$csv"
    for item in "${parts[@]}"; do
        item="${item#"${item%%[![:space:]]*}"}"
        item="${item%"${item##*[![:space:]]}"}"
        [[ -n "$item" ]] && target+=("$item")
    done
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help) show_help; exit 0 ;;
            --install) ACTION="install"; shift ;;
            --uninstall) ACTION="uninstall"; shift ;;
            --purge) PURGE=true; shift ;;
            -p|--profile)
                [[ $# -ge 2 ]] || error "$1 wymaga nazwy profilu."
                SELECTED_PROFILES+=("$2"); shift 2 ;;
            --profiles)
                [[ $# -ge 2 ]] || error "$1 wymaga listy profili."
                append_csv SELECTED_PROFILES "$2"; shift 2 ;;
            -r|--repo)
                [[ $# -ge 2 ]] || error "$1 wymaga repozytorium."
                SELECTED_REPOS+=("$2"); shift 2 ;;
            --repos)
                [[ $# -ge 2 ]] || error "$1 wymaga listy repozytoriów."
                append_csv SELECTED_REPOS "$2"; shift 2 ;;
            --select-repos) INTERACTIVE_REPOS=true; shift ;;
            -GUI|--gui) SELECTION_UI="gui"; shift ;;
            --tui) SELECTION_UI="tui"; shift ;;
            --all-repos) SELECTED_REPOS=(); INTERACTIVE_REPOS=false; shift ;;
            --list-profiles) LIST_PROFILES=true; shift ;;
            --list-repos) LIST_REPOS=true; shift ;;
            *) error "Nieznana opcja: $1. Użyj --help." ;;
        esac
    done
}

require_root() { [[ "$EUID" -eq 0 ]] || error "Uruchom skrypt jako root lub przez sudo."; }

ensure_dependencies() {
    local missing=() cmd
    for cmd in curl jq tar gzip git base64 getent awk; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        log "Instalacja brakujących zależności"
        apt-get update
        apt-get install -y curl jq tar gzip ca-certificates git coreutils gawk
    fi
}

get_invoking_user() {
    if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
        printf '%s\n' "$SUDO_USER"
    else
        id -un
    fi
}

get_user_home() {
    local username="$1" home
    home="$(getent passwd "$username" | cut -d: -f6)"
    [[ -n "$home" ]] || error "Nie można ustalić HOME użytkownika: $username"
    printf '%s\n' "$home"
}

init_gitconfig() {
    INVOKING_USER="$(get_invoking_user)"
    INVOKING_HOME="$(get_user_home "$INVOKING_USER")"
    GITCONFIG_PATH="${INVOKING_HOME}/.gitconfig"
}

read_gitconfig_value() {
    local key="$1"
    [[ -f "$GITCONFIG_PATH" ]] || return 1
    git config --file "$GITCONFIG_PATH" --get "$key" 2>/dev/null || true
}

decode_token() { printf '%s' "$1" | base64 --decode 2>/dev/null; }

list_profiles() {
    local -a profiles=()
    local profile

    if [[ -n "$ENV_GITHUB_OWNER" || -n "$ENV_GITHUB_TOKEN" ]] ||
       [[ -n "$(read_gitconfig_value 'github.username')" ]] ||
       [[ -n "$(read_gitconfig_value 'github.organization')" ]] ||
       [[ -n "$(read_gitconfig_value 'github.tokenBase64')" ]]; then
        profiles+=("default")
    fi

    if [[ -f "$GITCONFIG_PATH" ]]; then
        while IFS= read -r profile; do
            [[ -n "$profile" ]] && profiles+=("$profile")
        done < <(
            git config --file "$GITCONFIG_PATH" --name-only \
                --get-regexp '^github\..+\.(username|organization|owner|tokenBase64|mode|labels)$' \
                2>/dev/null |
            awk -F. 'NF >= 3 {print $2}' |
            sort -u
        )
    fi

    [[ ${#profiles[@]} -gt 0 ]] || { echo "Brak skonfigurowanych profili."; return; }
    printf '%s\n' "${profiles[@]}" | awk '!seen[$0]++'
}

load_profile() {
    local profile="$1" token_base64="" profile_mode="" profile_owner="" labels=""

    ACTIVE_PROFILE="$profile"
    GITHUB_OWNER=""
    GITHUB_TOKEN=""
    GITHUB_OWNER_SOURCE=""
    GITHUB_TOKEN_SOURCE=""
    CUSTOM_LABELS="$DEFAULT_LABELS"
    MODE="$DEFAULT_MODE"

    if [[ "$profile" == "default" ]]; then
        GITHUB_OWNER="$ENV_GITHUB_OWNER"
        GITHUB_TOKEN="$ENV_GITHUB_TOKEN"
        [[ -n "$GITHUB_OWNER" ]] && GITHUB_OWNER_SOURCE="environment"
        [[ -n "$GITHUB_TOKEN" ]] && GITHUB_TOKEN_SOURCE="environment"

        if [[ -z "$GITHUB_OWNER" ]]; then
            case "$MODE" in
                user) GITHUB_OWNER="$(read_gitconfig_value 'github.username')" ;;
                org)  GITHUB_OWNER="$(read_gitconfig_value 'github.organization')" ;;
            esac
            [[ -n "$GITHUB_OWNER" ]] && GITHUB_OWNER_SOURCE="$GITCONFIG_PATH"
        fi

        if [[ -z "$GITHUB_TOKEN" ]]; then
            token_base64="$(read_gitconfig_value 'github.tokenBase64')"
            if [[ -n "$token_base64" ]]; then
                GITHUB_TOKEN="$(decode_token "$token_base64")" ||
                    error "Nie można zdekodować github.tokenBase64 z $GITCONFIG_PATH."
                GITHUB_TOKEN_SOURCE="$GITCONFIG_PATH"
            fi
        fi
        return
    fi

    [[ -f "$GITCONFIG_PATH" ]] || error "Brak $GITCONFIG_PATH dla profilu '$profile'."

    profile_mode="$(read_gitconfig_value "github.${profile}.mode")"
    [[ -n "$profile_mode" ]] && MODE="$profile_mode"

    profile_owner="$(read_gitconfig_value "github.${profile}.owner")"
    if [[ -z "$profile_owner" ]]; then
        case "$MODE" in
            user) profile_owner="$(read_gitconfig_value "github.${profile}.username")" ;;
            org)  profile_owner="$(read_gitconfig_value "github.${profile}.organization")" ;;
        esac
    fi

    GITHUB_OWNER="$profile_owner"
    [[ -n "$GITHUB_OWNER" ]] && GITHUB_OWNER_SOURCE="${GITCONFIG_PATH} [github \"$profile\"]"

    token_base64="$(read_gitconfig_value "github.${profile}.tokenBase64")"
    if [[ -n "$token_base64" ]]; then
        GITHUB_TOKEN="$(decode_token "$token_base64")" ||
            error "Nie można zdekodować tokenBase64 profilu '$profile'."
        GITHUB_TOKEN_SOURCE="${GITCONFIG_PATH} [github \"$profile\"]"
    fi

    labels="$(read_gitconfig_value "github.${profile}.labels")"
    [[ -n "$labels" ]] && CUSTOM_LABELS="$labels"
}

check_profile_config() {
    case "$MODE" in
        user|org) ;;
        *) error "Profil '$ACTIVE_PROFILE': mode musi być user albo org." ;;
    esac
    [[ -n "$GITHUB_OWNER" ]] || error "Profil '$ACTIVE_PROFILE': brak ownera GitHub."
    [[ -n "$GITHUB_TOKEN" ]] || error "Profil '$ACTIVE_PROFILE': brak tokenu GitHub."
}

print_api_error() {
    local status="$1" method="$2" endpoint="$3" body_file="$4" headers_file="$5"
    local message docs request_id remaining

    message="$(jq -r '.message // empty' "$body_file" 2>/dev/null || true)"
    docs="$(jq -r '.documentation_url // empty' "$body_file" 2>/dev/null || true)"
    request_id="$(awk -F': ' 'tolower($1)=="x-github-request-id" {gsub("\r","",$2); print $2}' "$headers_file" | tail -n1)"
    remaining="$(awk -F': ' 'tolower($1)=="x-ratelimit-remaining" {gsub("\r","",$2); print $2}' "$headers_file" | tail -n1)"

    echo >&2
    echo "GitHub API ERROR" >&2
    echo "  Profile:    $ACTIVE_PROFILE" >&2
    echo "  HTTP:       $status" >&2
    echo "  Request:    $method $endpoint" >&2
    [[ -n "$message" ]] && echo "  Message:    $message" >&2
    [[ -n "$request_id" ]] && echo "  Request ID: $request_id" >&2
    [[ -n "$remaining" ]] && echo "  Rate limit: $remaining remaining" >&2
    [[ -n "$docs" ]] && echo "  Docs:       $docs" >&2

    case "$status" in
        401) echo "Przyczyna: token jest nieprawidłowy, wygasł albo został unieważniony." >&2 ;;
        403)
            echo "Przyczyna: token/konto nie ma wymaganych uprawnień." >&2
            if [[ "$endpoint" == /repos/*/actions/runners/*-token ]]; then
                echo "Repo runner: Fine-grained PAT Administration=Read and write lub Classic PAT repo." >&2
            elif [[ "$endpoint" == /orgs/*/actions/runners/*-token ]]; then
                echo "Organization runner: Self-hosted runners=Read and write lub Classic PAT admin:org." >&2
            fi
            [[ "$remaining" == "0" ]] && echo "Wyczerpano limit GitHub API." >&2
            echo "Sprawdź także SAML/SSO." >&2
            ;;
        404) echo "Przyczyna: zasób nie istnieje albo token nie ma do niego dostępu." >&2 ;;
        422) echo "Przyczyna: GitHub odrzucił parametry żądania." >&2 ;;
        429) echo "Przyczyna: przekroczono limit GitHub API." >&2 ;;
    esac
}

github_api() {
    local method="$1" endpoint="$2" body_file headers_file status curl_rc
    body_file="$(mktemp)"
    headers_file="$(mktemp)"

    status="$(
        curl --silent --show-error --location \
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
        echo "Błąd połączenia z GitHub API (curl=$curl_rc): $method $endpoint" >&2
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
    log "Weryfikacja tokenu GitHub: profil $ACTIVE_PROFILE"

    response="$(github_api GET '/user')" ||
        error "Profil '$ACTIVE_PROFILE': uwierzytelnienie tokenu nie powiodło się."

    login="$(printf '%s' "$response" | jq -r '.login // empty')"
    [[ -n "$login" ]] || error "GitHub API nie zwrócił loginu."

    echo "Profile:         $ACTIVE_PROFILE"
    echo "Token owner:     $login"
    echo "GitHub owner:    $GITHUB_OWNER"
    echo "Mode:            $MODE"
    echo "Labels:          $CUSTOM_LABELS"
    echo "Token source:    ${GITHUB_TOKEN_SOURCE:-unknown}"
}

create_runner_user() {
    if ! id "$RUNNER_USER" &>/dev/null; then
        log "Tworzenie użytkownika $RUNNER_USER"
        useradd --system --create-home --shell /bin/bash "$RUNNER_USER"
    fi
    mkdir -p "$RUNNER_BASE"
    chown -R "$RUNNER_USER:$RUNNER_USER" "$RUNNER_BASE"
}

sanitize_name() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9._-'
}

profile_root() {
    if [[ "$ACTIVE_PROFILE" == "default" ]]; then
        printf '%s\n' "$RUNNER_BASE"
    else
        printf '%s/profiles/%s\n' "$RUNNER_BASE" "$(sanitize_name "$ACTIVE_PROFILE")"
    fi
}

repo_runner_dir() { printf '%s/%s\n' "$(profile_root)" "$(sanitize_name "$1")"; }
org_runner_dir() { printf '%s/organization\n' "$(profile_root)"; }

runner_name_for_repo() {
    local host
    host="$(hostname -s)"
    if [[ "$ACTIVE_PROFILE" == "default" ]]; then
        printf '%s-%s\n' "$host" "$(sanitize_name "$1")"
    else
        printf '%s-%s-%s\n' "$host" "$(sanitize_name "$ACTIVE_PROFILE")" "$(sanitize_name "$1")"
    fi
}

runner_name_for_org() {
    local host
    host="$(hostname -s)"
    if [[ "$ACTIVE_PROFILE" == "default" ]]; then
        printf '%s-%s\n' "$host" "$(sanitize_name "$GITHUB_OWNER")"
    else
        printf '%s-%s-%s\n' "$host" "$(sanitize_name "$ACTIVE_PROFILE")" "$(sanitize_name "$GITHUB_OWNER")"
    fi
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
    local destination="$1" version="$2" arch="$3"
    local archive="/tmp/actions-runner-${version}-${arch}.tar.gz"
    local url="https://github.com/actions/runner/releases/download/v${version}/actions-runner-linux-${arch}-${version}.tar.gz"

    log "Pobieranie GitHub Actions Runner ${version}"
    curl -L --fail --show-error "$url" -o "$archive"
    mkdir -p "$destination"
    tar xzf "$archive" -C "$destination"
    rm -f "$archive"
    chown -R "$RUNNER_USER:$RUNNER_USER" "$destination"
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

repo_spec_for_profile() {
    local spec="$1" profile="$2"
    if [[ "$spec" == *:* ]]; then
        [[ "${spec%%:*}" == "$profile" ]]
        return
    fi
    return 0
}

normalize_repo_spec() {
    local spec="$1"
    [[ "$spec" == *:* ]] && spec="${spec#*:}"
    [[ "$spec" == */* ]] && spec="${spec##*/}"
    printf '%s\n' "$spec"
}

select_requested_repositories() {
    local profile="$1"
    shift
    local -a available=("$@") chosen=()
    local spec requested repo found relevant_specs=0

    if [[ ${#SELECTED_REPOS[@]} -eq 0 ]]; then
        printf '%s\n' "${available[@]}"
        return
    fi

    for spec in "${SELECTED_REPOS[@]}"; do
        repo_spec_for_profile "$spec" "$profile" || continue
        ((relevant_specs+=1))
        requested="$(normalize_repo_spec "$spec")"
        found=""
        for repo in "${available[@]}"; do
            if [[ "${repo,,}" == "${requested,,}" ]]; then
                found="$repo"
                break
            fi
        done
        [[ -n "$found" ]] && chosen+=("$found") || warn "Profil '$profile': brak repo '$requested'."
    done

    [[ "$relevant_specs" -gt 0 ]] || return
    printf '%s\n' "${chosen[@]}" | awk 'NF && !seen[tolower($0)]++'
}

explicit_repositories_for_profile() {
    local profile="$1" spec repo
    local -a chosen=()

    for spec in "${SELECTED_REPOS[@]}"; do
        repo_spec_for_profile "$spec" "$profile" || continue
        repo="$(normalize_repo_spec "$spec")"
        [[ -n "$repo" ]] && chosen+=("$repo")
    done

    printf '%s\n' "${chosen[@]}" | awk 'NF && !seen[tolower($0)]++'
}

graphical_session_available() {
    [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]]
}

ensure_gui_dependencies() {
    [[ "$INTERACTIVE_REPOS" == true ]] || return
    [[ "$SELECTION_UI" != "tui" ]] || return

    if ! graphical_session_available; then
        [[ "$SELECTION_UI" == "gui" ]] &&
            error "-GUI/--gui wymaga sesji graficznej X11 lub Wayland."
        return
    fi

    if ! command -v zenity >/dev/null 2>&1; then
        log "Instalacja Zenity dla GUI"
        apt-get update
        apt-get install -y zenity
    fi
}

run_zenity() {
    local uid xdg_runtime
    uid="$(id -u "$INVOKING_USER")"
    xdg_runtime="${XDG_RUNTIME_DIR:-/run/user/${uid}}"

    sudo -u "$INVOKING_USER" env \
        HOME="$INVOKING_HOME" \
        DISPLAY="${DISPLAY:-}" \
        WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}" \
        XDG_RUNTIME_DIR="$xdg_runtime" \
        XAUTHORITY="${XAUTHORITY:-${INVOKING_HOME}/.Xauthority}" \
        DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${xdg_runtime}/bus}" \
        zenity "$@"
}

gui_select_repositories() {
    local profile="$1"
    shift
    local -a available=("$@") rows=() selected=()
    local repo checked result rc=0 summary

    command -v zenity >/dev/null 2>&1 || return 10
    graphical_session_available || return 10

    for repo in "${available[@]}"; do
        checked="FALSE"
        if [[ "$ACTION" == "uninstall" && -f "$(repo_runner_dir "$repo")/.runner" ]]; then
            checked="TRUE"
        fi
        rows+=("$checked" "$repo")
    done

    result="$(
        run_zenity \
            --list \
            --checklist \
            --title="GitHub Self-Hosted Runner Manager" \
            --text="Profil: ${profile}\nAkcja: ${ACTION}\n\nWybierz repozytoria:" \
            --column="Wybierz" \
            --column="Repozytorium" \
            --separator=$'\n' \
            --width=820 \
            --height=650 \
            "${rows[@]}"
    )" || rc=$?

    if [[ "$rc" -eq 1 ]]; then
        warn "Anulowano wybór repozytoriów w GUI."
        return 0
    fi
    [[ "$rc" -eq 0 ]] || return 10
    [[ -n "$result" ]] || return 0

    mapfile -t selected <<< "$result"
    summary="$(printf '%s\n' "${selected[@]}")"

    if ! run_zenity \
        --question \
        --title="Potwierdzenie" \
        --width=600 \
        --text="Akcja: ${ACTION}\nProfil: ${profile}\n\nWybrane repozytoria (${#selected[@]}):\n${summary}\n\nKontynuować?"; then
        warn "Operacja anulowana w oknie potwierdzenia."
        return 0
    fi

    printf '%s\n' "${selected[@]}"
}

terminal_select_repositories() {
    local profile="$1"
    shift
    local -a available=("$@") chosen=() tokens=()
    local input token repo_candidate repo="" i

    [[ -t 0 ]] || error "Brak GUI i brak interaktywnego terminala dla --select-repos."

    echo >&2
    echo "Repozytoria profilu '$profile':" >&2
    for i in "${!available[@]}"; do
        printf '  %3d) %s\n' "$((i + 1))" "${available[$i]}" >&2
    done
    echo >&2
    echo "Podaj numery lub nazwy oddzielone przecinkami. Dostępne: all, none" >&2
    read -r -p "Wybór [$profile]: " input

    case "${input,,}" in
        all) printf '%s\n' "${available[@]}"; return ;;
        none|"") return ;;
    esac

    IFS=',' read -r -a tokens <<< "$input"
    for token in "${tokens[@]}"; do
        token="${token#"${token%%[![:space:]]*}"}"
        token="${token%"${token##*[![:space:]]}"}"

        if [[ "$token" =~ ^[0-9]+$ ]]; then
            i=$((token - 1))
            (( i >= 0 && i < ${#available[@]} )) && chosen+=("${available[$i]}") || warn "Nieprawidłowy numer: $token"
            continue
        fi

        repo=""
        for repo_candidate in "${available[@]}"; do
            [[ "${repo_candidate,,}" == "${token,,}" ]] && { repo="$repo_candidate"; break; }
        done
        [[ -n "$repo" ]] && chosen+=("$repo") || warn "Nieznane repo: $token"
    done

    printf '%s\n' "${chosen[@]}" | awk 'NF && !seen[tolower($0)]++'
}

interactive_select_repositories() {
    local profile="$1"
    shift
    local -a available=("$@") selected=()
    local rc=0 gui_output=""

    if [[ "$SELECTION_UI" != "tui" ]]; then
        if gui_output="$(gui_select_repositories "$profile" "${available[@]}")"; then
            if [[ -n "$gui_output" ]]; then
                mapfile -t selected <<< "$gui_output"
                printf '%s\n' "${selected[@]}"
            fi
            return 0
        else
            rc=$?
            [[ "$SELECTION_UI" == "gui" ]] && error "Nie udało się uruchomić GUI Zenity (kod $rc)."
        fi
    fi

    terminal_select_repositories "$profile" "${available[@]}"
}

resolve_repositories() {
    local -a available=() chosen=()

    if [[ "$ACTION" == "uninstall" &&
          ${#SELECTED_REPOS[@]} -gt 0 &&
          "$INTERACTIVE_REPOS" == false &&
          "$LIST_REPOS" == false ]]; then
        explicit_repositories_for_profile "$ACTIVE_PROFILE"
        return
    fi

    mapfile -t available < <(get_user_repositories)
    [[ ${#available[@]} -gt 0 ]] || error "Profil '$ACTIVE_PROFILE': brak repozytoriów lub brak dostępu."

    if [[ "$LIST_REPOS" == true ]]; then
        printf '%s\n' "${available[@]}"
        return
    fi

    if [[ "$INTERACTIVE_REPOS" == true ]]; then
        mapfile -t chosen < <(interactive_select_repositories "$ACTIVE_PROFILE" "${available[@]}")
    else
        mapfile -t chosen < <(select_requested_repositories "$ACTIVE_PROFILE" "${available[@]}")
    fi

    printf '%s\n' "${chosen[@]}"
}

get_repo_registration_token() {
    local response
    response="$(github_api POST "/repos/${GITHUB_OWNER}/$1/actions/runners/registration-token")" || return 1
    printf '%s' "$response" | jq -r '.token // empty'
}

get_repo_remove_token() {
    local response
    response="$(github_api POST "/repos/${GITHUB_OWNER}/$1/actions/runners/remove-token")" || return 1
    printf '%s' "$response" | jq -r '.token // empty'
}

get_org_registration_token() {
    local response
    response="$(github_api POST "/orgs/${GITHUB_OWNER}/actions/runners/registration-token")" || return 1
    printf '%s' "$response" | jq -r '.token // empty'
}

get_org_remove_token() {
    local response
    response="$(github_api POST "/orgs/${GITHUB_OWNER}/actions/runners/remove-token")" || return 1
    printf '%s' "$response" | jq -r '.token // empty'
}

write_runner_metadata() {
    local runner_dir="$1" repo="${2:-}"
    cat > "${runner_dir}/.chrisscriptbase-runner" <<EOF
profile=${ACTIVE_PROFILE}
mode=${MODE}
owner=${GITHUB_OWNER}
repo=${repo}
labels=${CUSTOM_LABELS}
EOF
    chown "$RUNNER_USER:$RUNNER_USER" "${runner_dir}/.chrisscriptbase-runner"
    chmod 600 "${runner_dir}/.chrisscriptbase-runner"
}

install_repo_runner() {
    local repo="$1" version="$2" arch="$3"
    local runner_dir runner_name registration_token

    runner_dir="$(repo_runner_dir "$repo")"
    runner_name="$(runner_name_for_repo "$repo")"
    log "[$ACTIVE_PROFILE] Repozytorium: ${GITHUB_OWNER}/${repo}"

    if [[ -f "${runner_dir}/.runner" ]]; then
        echo "Runner już istnieje: $runner_dir"
        return 3
    fi

    registration_token="$(get_repo_registration_token "$repo")" || {
        echo "Nie można pobrać registration token dla ${GITHUB_OWNER}/${repo}." >&2
        return 1
    }

    [[ -n "$registration_token" && "$registration_token" != "null" ]] || return 1

    mkdir -p "$runner_dir"
    download_runner "$runner_dir" "$version" "$arch"

    sudo -u "$RUNNER_USER" bash -c "
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
    unset registration_token

    write_runner_metadata "$runner_dir" "$repo"
    (
        cd "$runner_dir"
        ./svc.sh install "$RUNNER_USER"
        ./svc.sh start
    )

    echo "Runner gotowy: $runner_name"
}

uninstall_repo_runner() {
    local repo="$1" runner_dir remove_token="" remote_ok=true
    runner_dir="$(repo_runner_dir "$repo")"
    log "[$ACTIVE_PROFILE] Usuwanie runnera: ${GITHUB_OWNER}/${repo}"

    if [[ ! -d "$runner_dir" ]]; then
        echo "Runner lokalny nie istnieje: $runner_dir"
        return 3
    fi

    if [[ -x "${runner_dir}/svc.sh" ]]; then
        (cd "$runner_dir" && ./svc.sh stop || true; ./svc.sh uninstall || true)
    fi

    if [[ -f "${runner_dir}/.runner" && -x "${runner_dir}/config.sh" ]]; then
        if remove_token="$(get_repo_remove_token "$repo")" && [[ -n "$remove_token" && "$remove_token" != "null" ]]; then
            sudo -u "$RUNNER_USER" bash -c "cd '$runner_dir' && ./config.sh remove --unattended --token '$remove_token'" || remote_ok=false
        else
            warn "Brak remove token; usuwam tylko lokalnie."
            remote_ok=false
        fi
    fi

    unset remove_token
    rm -rf "$runner_dir"
    [[ "$remote_ok" == true ]]
}

install_org_runner() {
    local version="$1" arch="$2" runner_dir runner_name registration_token
    runner_dir="$(org_runner_dir)"
    runner_name="$(runner_name_for_org)"

    [[ ! -f "${runner_dir}/.runner" ]] || { echo "Organization runner już istnieje: $runner_dir"; return 3; }

    registration_token="$(get_org_registration_token)" || error "Brak organization registration token."
    mkdir -p "$runner_dir"
    download_runner "$runner_dir" "$version" "$arch"

    sudo -u "$RUNNER_USER" bash -c "
        cd '$runner_dir'
        ./config.sh --unattended \
            --url 'https://github.com/${GITHUB_OWNER}' \
            --token '$registration_token' \
            --name '$runner_name' \
            --labels '$CUSTOM_LABELS' \
            --work '_work' --replace
    "
    unset registration_token

    write_runner_metadata "$runner_dir"
    (cd "$runner_dir" && ./svc.sh install "$RUNNER_USER" && ./svc.sh start)
}

uninstall_org_runner() {
    local runner_dir remove_token="" remote_ok=true
    runner_dir="$(org_runner_dir)"

    [[ -d "$runner_dir" ]] || { echo "Runner lokalny nie istnieje: $runner_dir"; return 3; }

    if [[ -x "${runner_dir}/svc.sh" ]]; then
        (cd "$runner_dir" && ./svc.sh stop || true; ./svc.sh uninstall || true)
    fi

    if [[ -f "${runner_dir}/.runner" && -x "${runner_dir}/config.sh" ]]; then
        if remove_token="$(get_org_remove_token)" && [[ -n "$remove_token" && "$remove_token" != "null" ]]; then
            sudo -u "$RUNNER_USER" bash -c "cd '$runner_dir' && ./config.sh remove --unattended --token '$remove_token'" || remote_ok=false
        else
            remote_ok=false
        fi
    fi

    unset remove_token
    rm -rf "$runner_dir"
    [[ "$remote_ok" == true ]]
}

purge_if_empty() {
    [[ "$PURGE" == true ]] || return

    if [[ -d "$RUNNER_BASE" ]] &&
       find "$RUNNER_BASE" -mindepth 1 -type f \( -name '.runner' -o -name '.chrisscriptbase-runner' \) -print -quit | grep -q .; then
        warn "--purge: w $RUNNER_BASE nadal istnieją runnery."
        return
    fi

    rm -rf "$RUNNER_BASE"
    id "$RUNNER_USER" &>/dev/null && userdel "$RUNNER_USER" 2>/dev/null || true
    echo "Purge zakończony."
}

process_user_profile() {
    local version="${1:-}" arch="${2:-}" repo rc
    local -a repositories=()
    local success=0 failed=0 skipped=0

    mapfile -t repositories < <(resolve_repositories)
    [[ "$LIST_REPOS" == false ]] || return 0

    if [[ ${#repositories[@]} -eq 0 ]]; then
        warn "Profil '$ACTIVE_PROFILE': nie wybrano żadnych repozytoriów."
        return 0
    fi

    echo
    echo "Wybrane repozytoria [$ACTIVE_PROFILE]: ${#repositories[@]}"
    printf ' - %s\n' "${repositories[@]}"

    for repo in "${repositories[@]}"; do
        if [[ "$ACTION" == "install" ]]; then
            if install_repo_runner "$repo" "$version" "$arch"; then
                ((success+=1))
            else
                rc=$?
                [[ "$rc" -eq 3 ]] && ((skipped+=1)) || ((failed+=1))
            fi
        else
            if uninstall_repo_runner "$repo"; then
                ((success+=1))
            else
                rc=$?
                [[ "$rc" -eq 3 ]] && ((skipped+=1)) || ((failed+=1))
            fi
        fi
    done

    log "Podsumowanie profilu: $ACTIVE_PROFILE"
    echo "Akcja:       $ACTION"
    echo "Wybrane:     ${#repositories[@]}"
    echo "Sukces:      $success"
    echo "Pominięte:   $skipped"
    echo "Błędy:       $failed"

    [[ "$failed" -eq 0 ]]
}

process_org_profile() {
    local version="${1:-}" arch="${2:-}"

    if [[ ${#SELECTED_REPOS[@]} -gt 0 || "$INTERACTIVE_REPOS" == true || "$LIST_REPOS" == true ]]; then
        warn "MODE=org: selekcja repozytoriów nie dotyczy organization-level runnera. Użyj Runner Groups w GitHub."
        [[ "$LIST_REPOS" == true ]] && return 0
    fi

    if [[ "$ACTION" == "install" ]]; then
        install_org_runner "$version" "$arch"
    else
        uninstall_org_runner
    fi
}

main() {
    parse_args "$@"
    init_gitconfig

    if [[ "$LIST_PROFILES" == true ]]; then
        list_profiles
        exit 0
    fi

    require_root
    ensure_dependencies
    ensure_gui_dependencies

    [[ ${#SELECTED_PROFILES[@]} -gt 0 ]] || SELECTED_PROFILES=("default")
    [[ "$ACTION" != "install" ]] || create_runner_user

    local version="" arch="" profile failed_profiles=0
    if [[ "$ACTION" == "install" ]]; then
        version="$(get_runner_version)"
        arch="$(detect_arch)"
    fi

    for profile in "${SELECTED_PROFILES[@]}"; do
        load_profile "$profile"
        check_profile_config
        validate_github_token

        echo "Invoking user:   $INVOKING_USER"
        echo "Git config:      $GITCONFIG_PATH"
        echo "Profile root:    $(profile_root)"

        if [[ "$MODE" == "org" ]]; then
            process_org_profile "$version" "$arch" || ((failed_profiles+=1))
        else
            process_user_profile "$version" "$arch" || ((failed_profiles+=1))
        fi
    done

    [[ "$LIST_REPOS" == false ]] || exit 0
    [[ "$ACTION" != "uninstall" ]] || purge_if_empty

    echo
    echo "Usługi runnerów:"
    systemctl --no-pager --type=service | grep 'actions.runner' || true

    [[ "$failed_profiles" -eq 0 ]] || exit 2
}

main "$@"
