#!/usr/bin/env bash
set -Eeuo pipefail

ACTION="install"
DEFAULT_MODE="${MODE:-user}"
ENV_GITHUB_OWNER="${GITHUB_OWNER:-}"
ENV_GITHUB_TOKEN="${GITHUB_TOKEN:-}"
RUNNER_USER="${RUNNER_USER:-github-runner}"
RUNNER_BASE="${RUNNER_BASE:-/opt/github-runners}"
DEFAULT_LABELS="${CUSTOM_LABELS:-homelab}"
API_VERSION="${GITHUB_API_VERSION:-2022-11-28}"

REPO_SELECTION_MODE=""
SELECTION_UI="auto"   # auto | gui | tui | zenity
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
GITHUB_TOKEN_SOURCE=""

GUI_DISPLAY="${DISPLAY:-}"
GUI_WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}"
GUI_XDG_RUNTIME_DIR=""
GUI_XAUTHORITY="${XAUTHORITY:-}"
GUI_DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-}"

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
Obsługuje profile z ~/.gitconfig, wybór repozytoriów oraz interfejs działający
zarówno lokalnie, jak i przez zwykłe SSH.

UŻYCIE
  sudo bash ./install-github-selfhosted-runners.sh [opcje]
  sudo bash ./install-github-selfhosted-runners.sh --install [opcje]
  sudo bash ./install-github-selfhosted-runners.sh --uninstall [opcje]

AKCJE
  --install                 Instaluje runnery. Akcja domyślna.
  --uninstall               Wyrejestrowuje runnery i usuwa ich usługi systemd.
  --purge                   Z --uninstall usuwa pusty RUNNER_BASE i RUNNER_USER.

PROFILE
  -p, --profile NAME        Wybiera profil; można podać wiele razy.
  --profiles A,B,C          Wybiera kilka profili.
  --list-profiles           Pokazuje profile wykryte w ~/.gitconfig.

REPOZYTORIA
  -r, --repo REPO           Jedno repo; można podać wiele razy.
  --repos A,B,C             Kilka repozytoriów.
  --repo PROFILE:REPO       Repo tylko dla wskazanego profilu.
  --all-repos               Wszystkie repozytoria.
  --list-repos              Wyświetla repozytoria i kończy działanie.

  --select-repos            Interaktywny wybór repozytoriów.
  -GUI, --gui               Interfejs okienkowy także przez SSH:
                            * lokalny desktop -> Zenity,
                            * zwykłe SSH -> dialog/whiptail w terminalu.
  --tui                     Wymusza terminalowe okno dialog/whiptail.
  --zenity                  Wymusza natywne Zenity; wymaga X11/Wayland.

WAŻNE
  --gui NIE wymaga X11/Wayland. Przez SSH wyświetla terminalowe okno
  z checkboxami. Klawisze: strzałki, SPACJA zaznacza, TAB zmienia przycisk,
  ENTER zatwierdza.

  --all-repos, --select-repos oraz --repo/--repos są wzajemnie wykluczające.

KONFIGURACJA ~/.gitconfig

  [github]
      mode = user
      username = chmajster
      tokenBase64 = <TOKEN_BASE64>
      labels = homelab,linux

  [github "home"]
      mode = user
      username = chmajster
      tokenBase64 = <TOKEN_BASE64>
      labels = homelab,linux

  [github "work"]
      mode = org
      organization = moja-organizacja
      tokenBase64 = <TOKEN_BASE64>
      labels = work,linux

PRZYKŁADY

  SSH — okno z checkboxami:
    sudo bash ./install-github-selfhosted-runners.sh \
      --profile home --select-repos --gui

  SSH — wybór runnerów do usunięcia:
    sudo bash ./install-github-selfhosted-runners.sh \
      --uninstall --profile home --select-repos --gui

  Wszystkie runnery profilu:
    sudo bash ./install-github-selfhosted-runners.sh \
      --uninstall --profile home --all-repos

  Wszystkie + pełne czyszczenie:
    sudo bash ./install-github-selfhosted-runners.sh \
      --uninstall --profile home --all-repos --purge

  Wymuszenie prawdziwego okna Zenity:
    sudo -E bash ./install-github-selfhosted-runners.sh \
      --profile home --select-repos --zenity

UPRAWNIENIA TOKENU
  MODE=user Fine-grained PAT:
      Repository permissions -> Administration: Read and write
  MODE=user Classic PAT:
      repo
  MODE=org Fine-grained PAT:
      Organization permissions -> Self-hosted runners: Read and write
  MODE=org Classic PAT:
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
    return 0
}

set_repo_selection_mode() {
    local requested="$1" option="$2"
    if [[ -n "$REPO_SELECTION_MODE" && "$REPO_SELECTION_MODE" != "$requested" ]]; then
        error "Opcja $option koliduje z trybem '$REPO_SELECTION_MODE'. Użyj tylko jednego z: --all-repos, --select-repos, --repo/--repos."
    fi
    REPO_SELECTION_MODE="$requested"
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
                set_repo_selection_mode explicit "$1"
                SELECTED_REPOS+=("$2"); shift 2 ;;
            --repos)
                [[ $# -ge 2 ]] || error "$1 wymaga listy repozytoriów."
                set_repo_selection_mode explicit "$1"
                append_csv SELECTED_REPOS "$2"; shift 2 ;;
            --select-repos)
                set_repo_selection_mode interactive "$1"; shift ;;
            -GUI|--gui) SELECTION_UI="gui"; shift ;;
            --tui) SELECTION_UI="tui"; shift ;;
            --zenity) SELECTION_UI="zenity"; shift ;;
            --all-repos)
                set_repo_selection_mode all "$1"; shift ;;
            --list-profiles) LIST_PROFILES=true; shift ;;
            --list-repos) LIST_REPOS=true; shift ;;
            *) error "Nieznana opcja: $1. Użyj --help." ;;
        esac
    done

    [[ -n "$REPO_SELECTION_MODE" ]] || REPO_SELECTION_MODE="all"

    if [[ "$SELECTION_UI" != "auto" && "$REPO_SELECTION_MODE" != "interactive" ]]; then
        error "--gui/-GUI, --tui i --zenity wymagają --select-repos."
    fi
    [[ "$PURGE" == false || "$ACTION" == "uninstall" ]] ||
        error "--purge działa tylko z --uninstall."
}

require_root() {
    [[ "$EUID" -eq 0 ]] || error "Uruchom skrypt jako root lub przez sudo."
}

ensure_dependencies() {
    local missing=() cmd
    for cmd in curl jq tar gzip git base64 getent awk sudo; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        log "Instalacja brakujących zależności"
        apt-get update
        apt-get install -y curl jq tar gzip ca-certificates git coreutils gawk sudo
    fi
    return 0
}

get_invoking_user() {
    if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
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

init_invoking_user() {
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
       [[ -n "$(read_gitconfig_value 'github.owner')" ]] ||
       [[ -n "$(read_gitconfig_value 'github.tokenBase64')" ]]; then
        profiles+=(default)
    fi

    if [[ -f "$GITCONFIG_PATH" ]]; then
        while IFS= read -r profile; do
            [[ -n "$profile" ]] && profiles+=("$profile")
        done < <(
            git config --file "$GITCONFIG_PATH" --name-only \
                --get-regexp '^github\..+\.(username|organization|owner|tokenBase64|mode|labels)$' \
                2>/dev/null | awk -F. 'NF >= 3 {print $2}' | sort -u
        )
    fi

    if [[ ${#profiles[@]} -eq 0 ]]; then
        echo "Brak skonfigurowanych profili."
        return 0
    fi
    printf '%s\n' "${profiles[@]}" | awk '!seen[$0]++'
}

load_profile() {
    local profile="$1" token_base64="" profile_mode="" profile_owner="" labels=""
    ACTIVE_PROFILE="$profile"
    GITHUB_OWNER=""
    GITHUB_TOKEN=""
    GITHUB_TOKEN_SOURCE=""
    CUSTOM_LABELS="$DEFAULT_LABELS"
    MODE="$DEFAULT_MODE"

    if [[ "$profile" == "default" ]]; then
        profile_mode="$(read_gitconfig_value 'github.mode')"
        [[ -n "$profile_mode" ]] && MODE="$profile_mode"
        GITHUB_OWNER="$ENV_GITHUB_OWNER"
        GITHUB_TOKEN="$ENV_GITHUB_TOKEN"

        if [[ -z "$GITHUB_OWNER" ]]; then
            GITHUB_OWNER="$(read_gitconfig_value 'github.owner')"
            if [[ -z "$GITHUB_OWNER" ]]; then
                case "$MODE" in
                    user) GITHUB_OWNER="$(read_gitconfig_value 'github.username')" ;;
                    org) GITHUB_OWNER="$(read_gitconfig_value 'github.organization')" ;;
                esac
            fi
        fi
        if [[ -z "$GITHUB_TOKEN" ]]; then
            token_base64="$(read_gitconfig_value 'github.tokenBase64')"
            if [[ -n "$token_base64" ]]; then
                GITHUB_TOKEN="$(decode_token "$token_base64")" || error "Nie można zdekodować github.tokenBase64."
                GITHUB_TOKEN_SOURCE="$GITCONFIG_PATH"
            fi
        else
            GITHUB_TOKEN_SOURCE="environment"
        fi
        labels="$(read_gitconfig_value 'github.labels')"
        [[ -n "$labels" ]] && CUSTOM_LABELS="$labels"
        return 0
    fi

    [[ -f "$GITCONFIG_PATH" ]] || error "Brak $GITCONFIG_PATH dla profilu '$profile'."
    profile_mode="$(read_gitconfig_value "github.${profile}.mode")"
    [[ -n "$profile_mode" ]] && MODE="$profile_mode"
    profile_owner="$(read_gitconfig_value "github.${profile}.owner")"
    if [[ -z "$profile_owner" ]]; then
        case "$MODE" in
            user) profile_owner="$(read_gitconfig_value "github.${profile}.username")" ;;
            org) profile_owner="$(read_gitconfig_value "github.${profile}.organization")" ;;
        esac
    fi
    GITHUB_OWNER="$profile_owner"
    token_base64="$(read_gitconfig_value "github.${profile}.tokenBase64")"
    if [[ -n "$token_base64" ]]; then
        GITHUB_TOKEN="$(decode_token "$token_base64")" || error "Nie można zdekodować tokenBase64 profilu '$profile'."
        GITHUB_TOKEN_SOURCE="${GITCONFIG_PATH} [github \"$profile\"]"
    fi
    labels="$(read_gitconfig_value "github.${profile}.labels")"
    [[ -n "$labels" ]] && CUSTOM_LABELS="$labels"
    return 0
}

check_profile_config() {
    case "$MODE" in user|org) ;; *) error "Profil '$ACTIVE_PROFILE': mode musi być user albo org." ;; esac
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
        401) echo "Przyczyna: token jest nieprawidłowy lub wygasł." >&2 ;;
        403)
            echo "Przyczyna: brak wymaganych uprawnień tokenu/konta." >&2
            [[ "$endpoint" == /repos/*/actions/runners/*-token ]] &&
                echo "Repo runner: Fine-grained PAT Administration=Read and write lub Classic PAT repo." >&2
            [[ "$endpoint" == /orgs/*/actions/runners/*-token ]] &&
                echo "Organization runner: Self-hosted runners=Read and write lub Classic PAT admin:org." >&2
            ;;
        404) echo "Przyczyna: zasób nie istnieje albo token nie ma dostępu." >&2 ;;
        422) echo "Przyczyna: GitHub odrzucił parametry żądania." >&2 ;;
        429) echo "Przyczyna: przekroczono limit GitHub API." >&2 ;;
    esac
}

github_api() {
    local method="$1" endpoint="$2" body_file headers_file status curl_rc
    body_file="$(mktemp)"; headers_file="$(mktemp)"
    status="$(curl --silent --show-error --location --request "$method" \
        --header "Accept: application/vnd.github+json" \
        --header "Authorization: Bearer ${GITHUB_TOKEN}" \
        --header "X-GitHub-Api-Version: ${API_VERSION}" \
        --dump-header "$headers_file" --output "$body_file" --write-out '%{http_code}' \
        "https://api.github.com${endpoint}")" || {
        curl_rc=$?; rm -f "$body_file" "$headers_file"
        echo "Błąd połączenia z GitHub API (curl=$curl_rc): $method $endpoint" >&2
        return "$curl_rc"
    }
    if [[ ! "$status" =~ ^2[0-9][0-9]$ ]]; then
        print_api_error "$status" "$method" "$endpoint" "$body_file" "$headers_file"
        rm -f "$body_file" "$headers_file"; return 1
    fi
    cat "$body_file"; rm -f "$body_file" "$headers_file"
}

validate_github_token() {
    local response login
    log "Weryfikacja tokenu GitHub: profil $ACTIVE_PROFILE"
    response="$(github_api GET '/user')" || error "Profil '$ACTIVE_PROFILE': uwierzytelnienie nie powiodło się."
    login="$(printf '%s' "$response" | jq -r '.login // empty')"
    [[ -n "$login" ]] || error "GitHub API nie zwrócił loginu."
    echo "Profile:      $ACTIVE_PROFILE"
    echo "Token owner:  $login"
    echo "GitHub owner: $GITHUB_OWNER"
    echo "Mode:         $MODE"
    echo "Token source: ${GITHUB_TOKEN_SOURCE:-unknown}"
    echo "Labels:       $CUSTOM_LABELS"
}

create_runner_user() {
    if ! id "$RUNNER_USER" &>/dev/null; then
        log "Tworzenie użytkownika $RUNNER_USER"
        useradd --system --create-home --shell /bin/bash "$RUNNER_USER"
    fi
    mkdir -p "$RUNNER_BASE"
    chown -R "$RUNNER_USER:$RUNNER_USER" "$RUNNER_BASE"
}

sanitize_name() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9._-'; }
profile_root() {
    [[ "$ACTIVE_PROFILE" == "default" ]] && printf '%s\n' "$RUNNER_BASE" ||
        printf '%s/profiles/%s\n' "$RUNNER_BASE" "$(sanitize_name "$ACTIVE_PROFILE")"
}
repo_runner_dir() { printf '%s/%s\n' "$(profile_root)" "$(sanitize_name "$1")"; }
org_runner_dir() { printf '%s/organization\n' "$(profile_root)"; }

get_runner_version() {
    curl --silent --show-error --fail "https://api.github.com/repos/actions/runner/releases/latest" |
        jq -r '.tag_name' | sed 's/^v//'
}

detect_arch() {
    case "$(uname -m)" in
        x86_64|amd64) echo x64 ;;
        aarch64|arm64) echo arm64 ;;
        armv7l) echo arm ;;
        *) error "Nieobsługiwana architektura: $(uname -m)" ;;
    esac
}

download_runner() {
    local destination="$1" version="$2" arch="$3"
    local archive="/tmp/actions-runner-${version}-${arch}.tar.gz"
    local url="https://github.com/actions/runner/releases/download/v${version}/actions-runner-linux-${arch}-${version}.tar.gz"
    log "Pobieranie GitHub Actions Runner ${version}"
    curl -L --fail --show-error "$url" -o "$archive"
    mkdir -p "$destination"; tar xzf "$archive" -C "$destination"; rm -f "$archive"
    chown -R "$RUNNER_USER:$RUNNER_USER" "$destination"
}

get_user_repositories() {
    local page=1 response count
    while true; do
        response="$(github_api GET "/user/repos?affiliation=owner&per_page=100&page=${page}&sort=full_name")" || return 1
        count="$(printf '%s' "$response" | jq 'length')"
        [[ "$count" -gt 0 ]] || break
        printf '%s' "$response" | jq -r --arg owner "$GITHUB_OWNER" '
          .[] | select((.owner.login|ascii_downcase)==($owner|ascii_downcase))
          | select(.archived==false) | .name'
        ((page += 1))
    done
}

list_action_runner_service_units() {
    {
        systemctl list-units --all --type=service --no-legend 'actions.runner.*.service' 2>/dev/null |
  awk '{print $1}' || true
        systemctl list-unit-files --type=service --no-legend 'actions.runner.*.service' 2>/dev/null |
  awk '{print $1}' || true
    } | awk '/^actions\.runner\..*\.service$/ && !seen[$0]++'
}

repo_from_service_unit() {
    local unit="$1" prefix rest host marker repo runner repo_sanitized profile_sanitized
    unit="${unit,,}"
    prefix="actions.runner.${GITHUB_OWNER,,}-"
    [[ "$unit" == "$prefix"*".service" ]] || return 1

    rest="${unit#"$prefix"}"
    rest="${rest%.service}"
    host="$(hostname -s)"
    host="${host,,}"
    marker=".${host}-"
    [[ "$rest" == *"$marker"* ]] || return 1

    repo="${rest%%"$marker"*}"
    runner="${rest#*"$marker"}"
    [[ -n "$repo" && -n "$runner" ]] || return 1

    repo_sanitized="$(sanitize_name "$repo")"
    if [[ "$ACTIVE_PROFILE" == "default" ]]; then
        [[ "$runner" == "$repo_sanitized" ]] || return 1
    else
        profile_sanitized="$(sanitize_name "$ACTIVE_PROFILE")"
        [[ "$runner" == "${profile_sanitized}-${repo_sanitized}" || "$runner" == "$repo_sanitized" ]] || return 1
    fi

    printf '%s\n' "$repo"
}

runner_service_units_for_repo() {
    local repo="$1" unit parsed
    while IFS= read -r unit; do
        [[ -n "$unit" ]] || continue
        parsed="$(repo_from_service_unit "$unit")" || continue
        if [[ "${parsed,,}" == "${repo,,}" ]]; then
  printf '%s\n' "$unit"
        fi
    done < <(list_action_runner_service_units)
}

get_service_repositories() {
    local unit repo
    while IFS= read -r unit; do
        [[ -n "$unit" ]] || continue
        repo="$(repo_from_service_unit "$unit")" || continue
        printf '%s\n' "$repo"
    done < <(list_action_runner_service_units) |
        awk 'NF && !seen[tolower($0)]++'
}

scan_runner_root_repositories() {
    local root="$1" dir meta repo runner_url
    [[ -d "$root" ]] || return 0

    for dir in "$root"/*; do
        [[ -d "$dir" ]] || continue
        case "$(basename "$dir")" in
  organization|profiles) continue ;;
        esac
        [[ -f "$dir/.runner" || -f "$dir/.chrisscriptbase-runner" ]] || continue

        repo=""
        meta="$dir/.chrisscriptbase-runner"
        if [[ -f "$meta" ]]; then
  repo="$(awk -F= '$1=="repo" {sub(/^repo=/,""); print; exit}' "$meta")"
        fi

        if [[ -z "$repo" && -f "$dir/.runner" ]]; then
  runner_url="$(jq -r '.gitHubUrl // empty' "$dir/.runner" 2>/dev/null || true)"
  runner_url="${runner_url%/}"
  if [[ "${runner_url,,}" == "https://github.com/${GITHUB_OWNER,,}/"* ]]; then
      repo="${runner_url##*/}"
  fi
        fi

        [[ -n "$repo" ]] || repo="$(basename "$dir")"
        printf '%s\n' "$repo"
    done
}

get_local_repositories() {
    local root
    root="$(profile_root)"
    {
        scan_runner_root_repositories "$root"
        if [[ "$ACTIVE_PROFILE" != "default" ]]; then
  scan_runner_root_repositories "$RUNNER_BASE"
        fi
        get_service_repositories
    } | awk 'NF && !seen[tolower($0)]++'
}

repo_spec_for_profile() {
    local spec="$1" profile="$2"
    [[ "$spec" != *:* ]] || [[ "${spec%%:*}" == "$profile" ]]
}
normalize_repo_spec() {
    local spec="$1"
    [[ "$spec" == *:* ]] && spec="${spec#*:}"
    [[ "$spec" == */* ]] && spec="${spec##*/}"
    printf '%s\n' "$spec"
}

select_requested_repositories() {
    local profile="$1"; shift
    local -a available=("$@") chosen=()
    local spec requested repo
    for spec in "${SELECTED_REPOS[@]}"; do
        repo_spec_for_profile "$spec" "$profile" || continue
        requested="$(normalize_repo_spec "$spec")"
        for repo in "${available[@]}"; do
            if [[ "${repo,,}" == "${requested,,}" ]]; then chosen+=("$repo"); break; fi
        done
    done
    printf '%s\n' "${chosen[@]}" | awk 'NF && !seen[tolower($0)]++'
}

read_user_session_environment() {
    local uid="$1"
    command -v systemctl >/dev/null 2>&1 || return 0
    [[ -S "/run/user/${uid}/bus" ]] || return 0
    sudo -u "$INVOKING_USER" env XDG_RUNTIME_DIR="/run/user/${uid}" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
      systemctl --user show-environment 2>/dev/null || true
}
session_env_value() {
    printf '%s\n' "$1" | awk -v key="$2" 'index($0,key"=")==1{sub("^[^=]*=","");print;exit}'
}
detect_graphical_session() {
    local uid session_env=""
    uid="$(id -u "$INVOKING_USER")"; GUI_XDG_RUNTIME_DIR="/run/user/${uid}"
    if [[ -z "$GUI_DISPLAY" && -z "$GUI_WAYLAND_DISPLAY" ]]; then
        session_env="$(read_user_session_environment "$uid")"
        GUI_DISPLAY="$(session_env_value "$session_env" DISPLAY)"
        GUI_WAYLAND_DISPLAY="$(session_env_value "$session_env" WAYLAND_DISPLAY)"
        [[ -n "$GUI_XAUTHORITY" ]] || GUI_XAUTHORITY="$(session_env_value "$session_env" XAUTHORITY)"
        [[ -n "$GUI_DBUS_SESSION_BUS_ADDRESS" ]] || GUI_DBUS_SESSION_BUS_ADDRESS="$(session_env_value "$session_env" DBUS_SESSION_BUS_ADDRESS)"
    fi
    [[ -n "$GUI_XAUTHORITY" || ! -f "$INVOKING_HOME/.Xauthority" ]] || GUI_XAUTHORITY="$INVOKING_HOME/.Xauthority"
    [[ -n "$GUI_DBUS_SESSION_BUS_ADDRESS" || ! -S "/run/user/${uid}/bus" ]] || GUI_DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus"
}
graphical_session_available() { [[ -n "$GUI_DISPLAY" || -n "$GUI_WAYLAND_DISPLAY" ]]; }

ensure_selection_ui_dependencies() {
    [[ "$REPO_SELECTION_MODE" == "interactive" ]] || return 0
    detect_graphical_session

    if [[ "$SELECTION_UI" == "zenity" ]]; then
        graphical_session_available || error "--zenity wymaga X11/Wayland. Przez zwykłe SSH użyj --gui."
        if ! command -v zenity >/dev/null 2>&1; then
            apt-get update; apt-get install -y zenity
        fi
        return 0
    fi

    if [[ "$SELECTION_UI" == "auto" || "$SELECTION_UI" == "gui" ]]; then
        if graphical_session_available; then
            command -v zenity >/dev/null 2>&1 || { apt-get update; apt-get install -y zenity; }
            return 0
        fi
    fi

    if ! command -v dialog >/dev/null 2>&1 && ! command -v whiptail >/dev/null 2>&1; then
        log "Instalacja terminalowego GUI (dialog)"
        apt-get update
        apt-get install -y dialog
    fi
    return 0
}

run_zenity() {
    local -a env_args=("HOME=$INVOKING_HOME" "XDG_RUNTIME_DIR=$GUI_XDG_RUNTIME_DIR")
    [[ -n "$GUI_DISPLAY" ]] && env_args+=("DISPLAY=$GUI_DISPLAY")
    [[ -n "$GUI_WAYLAND_DISPLAY" ]] && env_args+=("WAYLAND_DISPLAY=$GUI_WAYLAND_DISPLAY")
    [[ -n "$GUI_XAUTHORITY" ]] && env_args+=("XAUTHORITY=$GUI_XAUTHORITY")
    [[ -n "$GUI_DBUS_SESSION_BUS_ADDRESS" ]] && env_args+=("DBUS_SESSION_BUS_ADDRESS=$GUI_DBUS_SESSION_BUS_ADDRESS")
    sudo -u "$INVOKING_USER" env "${env_args[@]}" zenity "$@"
}

zenity_select_repositories() {
    local profile="$1"; shift
    local -a available=("$@") rows=() selected=()
    local repo checked result rc=0 summary
    command -v zenity >/dev/null 2>&1 || return 10
    graphical_session_available || return 10
    for repo in "${available[@]}"; do
        checked=FALSE
        [[ "$ACTION" == uninstall && -f "$(repo_runner_dir "$repo")/.runner" ]] && checked=TRUE
        rows+=("$checked" "$repo")
    done
    result="$(run_zenity --list --checklist --title="GitHub Self-Hosted Runner Manager" \
      --text="Profil: ${profile}\nAkcja: ${ACTION}\n\nWybierz repozytoria:" \
      --column="Wybierz" --column="Repozytorium" --separator=$'\n' --width=820 --height=650 "${rows[@]}")" || rc=$?
    [[ "$rc" -eq 1 ]] && return 0
    [[ "$rc" -eq 0 ]] || return 10
    [[ -n "$result" ]] || return 0
    mapfile -t selected <<< "$result"
    summary="$(printf '%s\n' "${selected[@]}")"
    run_zenity --question --title="Potwierdzenie" --width=600 \
      --text="Akcja: ${ACTION}\nProfil: ${profile}\n\nWybrane (${#selected[@]}):\n${summary}\n\nKontynuować?" || return 0
    printf '%s\n' "${selected[@]}"
}

terminal_gui_select_repositories() {
    local profile="$1"; shift
    local -a available=("$@") items=()
    local repo status output rc=0
    [[ -t 0 && -t 1 ]] || error "Terminalowe GUI wymaga interaktywnej sesji SSH/terminala (alokuj TTY: ssh -t)."

    for repo in "${available[@]}"; do
        status=off
        [[ "$ACTION" == uninstall && -f "$(repo_runner_dir "$repo")/.runner" ]] && status=on
        items+=("$repo" "" "$status")
    done

    if command -v dialog >/dev/null 2>&1; then
        output="$(dialog --stdout --separate-output --title "GitHub Self-Hosted Runner Manager" \
          --checklist "Profil: $profile   Akcja: $ACTION\n\nSPACJA = zaznacz, TAB = przycisk, ENTER = zatwierdź" \
          24 100 16 "${items[@]}")" || rc=$?
        clear || true
    else
        output="$(whiptail --title "GitHub Self-Hosted Runner Manager" \
          --checklist "Profil: $profile | Akcja: $ACTION | SPACJA zaznacza" 24 100 16 \
          "${items[@]}" 3>&1 1>&2 2>&3)" || rc=$?
        output="$(printf '%s\n' "$output" | sed 's/" " /\n/g; s/^"//; s/"$//')"
    fi

    [[ "$rc" -eq 0 ]] || return 0
    [[ -n "$output" ]] || return 0

    if command -v dialog >/dev/null 2>&1; then
        dialog --title "Potwierdzenie" --yesno \
          "Akcja: $ACTION\nProfil: $profile\n\nWybrane:\n$output\n\nKontynuować?" 22 90 || { clear || true; return 0; }
        clear || true
    fi
    printf '%s\n' "$output"
}

plain_select_repositories() {
    local profile="$1"; shift
    local -a available=("$@") chosen=() tokens=()
    local input token i repo candidate
    [[ -t 0 ]] || error "Brak interaktywnego terminala. Użyj SSH z TTY: ssh -t host."
    echo "Repozytoria profilu '$profile':" >&2
    for i in "${!available[@]}"; do printf '%3d) %s\n' "$((i+1))" "${available[$i]}" >&2; done
    read -r -p "Numery/nazwy (przecinki), all lub none: " input
    case "${input,,}" in all) printf '%s\n' "${available[@]}"; return 0;; none|"") return 0;; esac
    IFS=',' read -r -a tokens <<< "$input"
    for token in "${tokens[@]}"; do
        token="${token//[[:space:]]/}"
        if [[ "$token" =~ ^[0-9]+$ ]]; then
            i=$((token-1)); (( i>=0 && i<${#available[@]} )) && chosen+=("${available[$i]}"); continue
        fi
        repo=""; for candidate in "${available[@]}"; do [[ "${candidate,,}" == "${token,,}" ]] && { repo="$candidate"; break; }; done
        [[ -n "$repo" ]] && chosen+=("$repo")
    done
    printf '%s\n' "${chosen[@]}" | awk 'NF && !seen[tolower($0)]++'
}

interactive_select_repositories() {
    local profile="$1"; shift
    local -a available=("$@")

    case "$SELECTION_UI" in
        zenity)
            zenity_select_repositories "$profile" "${available[@]}"
            ;;
        gui)
            if graphical_session_available && command -v zenity >/dev/null 2>&1; then
                zenity_select_repositories "$profile" "${available[@]}"
            else
                terminal_gui_select_repositories "$profile" "${available[@]}"
            fi
            ;;
        tui)
            terminal_gui_select_repositories "$profile" "${available[@]}"
            ;;
        auto)
            if graphical_session_available && command -v zenity >/dev/null 2>&1; then
                zenity_select_repositories "$profile" "${available[@]}"
            elif command -v dialog >/dev/null 2>&1 || command -v whiptail >/dev/null 2>&1; then
                terminal_gui_select_repositories "$profile" "${available[@]}"
            else
                plain_select_repositories "$profile" "${available[@]}"
            fi
            ;;
    esac
}

resolve_repositories() {
    local -a available=() chosen=()

    if [[ "$LIST_REPOS" == true ]]; then
        mapfile -t available < <(get_user_repositories)
        printf '%s\n' "${available[@]}"; return 0
    fi

    if [[ "$ACTION" == uninstall ]]; then
        mapfile -t available < <(get_local_repositories)
        if [[ "$REPO_SELECTION_MODE" == explicit ]]; then
            mapfile -t chosen < <(select_requested_repositories "$ACTIVE_PROFILE" "${available[@]}")
            printf '%s\n' "${chosen[@]}"; return 0
        fi
    else
        mapfile -t available < <(get_user_repositories)
    fi

    [[ ${#available[@]} -gt 0 ]] || { warn "Profil '$ACTIVE_PROFILE': brak repozytoriów do operacji."; return 0; }

    case "$REPO_SELECTION_MODE" in
        all) chosen=("${available[@]}") ;;
        explicit) mapfile -t chosen < <(select_requested_repositories "$ACTIVE_PROFILE" "${available[@]}") ;;
        interactive) mapfile -t chosen < <(interactive_select_repositories "$ACTIVE_PROFILE" "${available[@]}") ;;
        *) error "Nieznany tryb repozytoriów: $REPO_SELECTION_MODE" ;;
    esac
    printf '%s\n' "${chosen[@]}"
}

get_repo_registration_token() {
    local response; response="$(github_api POST "/repos/${GITHUB_OWNER}/$1/actions/runners/registration-token")" || return 1
    printf '%s' "$response" | jq -r '.token // empty'
}
get_repo_remove_token() {
    local response; response="$(github_api POST "/repos/${GITHUB_OWNER}/$1/actions/runners/remove-token")" || return 1
    printf '%s' "$response" | jq -r '.token // empty'
}
get_org_registration_token() {
    local response; response="$(github_api POST "/orgs/${GITHUB_OWNER}/actions/runners/registration-token")" || return 1
    printf '%s' "$response" | jq -r '.token // empty'
}
get_org_remove_token() {
    local response; response="$(github_api POST "/orgs/${GITHUB_OWNER}/actions/runners/remove-token")" || return 1
    printf '%s' "$response" | jq -r '.token // empty'
}

write_runner_metadata() {
    local runner_dir="$1" repo="${2:-}"
    cat > "$runner_dir/.chrisscriptbase-runner" <<EOF_META
profile=${ACTIVE_PROFILE}
mode=${MODE}
owner=${GITHUB_OWNER}
repo=${repo}
labels=${CUSTOM_LABELS}
EOF_META
    chown "$RUNNER_USER:$RUNNER_USER" "$runner_dir/.chrisscriptbase-runner"
    chmod 600 "$runner_dir/.chrisscriptbase-runner"
}

install_repo_runner() {
    local repo="$1" version="$2" arch="$3" dir token name host
    dir="$(repo_runner_dir "$repo")"; host="$(hostname -s)"
    name="${host}-$(sanitize_name "$ACTIVE_PROFILE")-$(sanitize_name "$repo")"
    [[ "$ACTIVE_PROFILE" != default ]] || name="${host}-$(sanitize_name "$repo")"
    log "[$ACTIVE_PROFILE] Instalacja: ${GITHUB_OWNER}/${repo}"
    [[ ! -f "$dir/.runner" ]] || { echo "Runner już istnieje: $dir"; return 3; }
    token="$(get_repo_registration_token "$repo")" || return 1
    [[ -n "$token" && "$token" != null ]] || return 1
    mkdir -p "$dir"; download_runner "$dir" "$version" "$arch"
    sudo -u "$RUNNER_USER" bash -c "cd '$dir' && ./config.sh --unattended \
      --url 'https://github.com/${GITHUB_OWNER}/${repo}' --token '$token' \
      --name '$name' --labels '$CUSTOM_LABELS' --work '_work' --replace"
    unset token; write_runner_metadata "$dir" "$repo"
    (cd "$dir" && ./svc.sh install "$RUNNER_USER" && ./svc.sh start)
}

find_repo_runner_dirs() {
    local repo="$1" current legacy dir
    current="$(repo_runner_dir "$repo")"
    legacy="${RUNNER_BASE}/$(sanitize_name "$repo")"

    for dir in "$current" "$legacy"; do
        [[ -d "$dir" ]] || continue
        printf '%s\n' "$dir"
    done | awk '!seen[$0]++'
}

service_name_from_runner_dir() {
    local dir="$1"
    [[ -f "$dir/.service" ]] || return 0
    head -n 1 "$dir/.service" | tr -d '\r'
}

remove_runner_service_unit() {
    local unit="$1" fragment=""
    case "$unit" in
        actions.runner.*.service) ;;
        *) warn "Pomijam nieoczekiwaną nazwę usługi: $unit"; return 1 ;;
    esac

    systemctl stop "$unit" >/dev/null 2>&1 || true
    systemctl disable "$unit" >/dev/null 2>&1 || true
    fragment="$(systemctl show --property=FragmentPath --value "$unit" 2>/dev/null || true)"

    if [[ "$fragment" == /etc/systemd/system/actions.runner.*.service ]]; then
        rm -f -- "$fragment"
    fi
    rm -f -- "/etc/systemd/system/$unit"
    find /etc/systemd/system -type l -name "$unit" -delete 2>/dev/null || true

    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl reset-failed "$unit" >/dev/null 2>&1 || true
    return 0
}

uninstall_repo_runner() {
    local repo="$1" dir token="" service_unit="" remote_ok=true local_ok=true
    local -a dirs=() units=() remaining=()

    log "[$ACTIVE_PROFILE] Usuwanie: ${GITHUB_OWNER}/${repo}"
    mapfile -t dirs < <(find_repo_runner_dirs "$repo")
    mapfile -t units < <(runner_service_units_for_repo "$repo")

    if [[ ${#dirs[@]} -eq 0 && ${#units[@]} -eq 0 ]]; then
        echo "Runner lokalny ani usługa systemd nie istnieją dla: ${GITHUB_OWNER}/${repo}"
        return 3
    fi

    for dir in "${dirs[@]}"; do
        service_unit="$(service_name_from_runner_dir "$dir")"

        if [[ -x "$dir/svc.sh" ]]; then
  if ! (
      cd "$dir"
      ./svc.sh stop
      ./svc.sh uninstall
  ); then
      warn "svc.sh nie usunął poprawnie usługi z $dir; wykonuję wymuszone czyszczenie systemd."
  fi
        fi

        if [[ -n "$service_unit" ]]; then
  remove_runner_service_unit "$service_unit" || local_ok=false
        fi

        if [[ -f "$dir/.runner" && -x "$dir/config.sh" ]]; then
  token=""
  if token="$(get_repo_remove_token "$repo")" && [[ -n "$token" && "$token" != null ]]; then
      sudo -u "$RUNNER_USER" bash -c "cd '$dir' && ./config.sh remove --unattended --token '$token'" || remote_ok=false
  else
      warn "Brak remove token; usuwam runnera i usługę tylko lokalnie."
      remote_ok=false
  fi
  unset token
        fi

        rm -rf "$dir"
    done

    mapfile -t units < <(runner_service_units_for_repo "$repo")
    for service_unit in "${units[@]}"; do
        remove_runner_service_unit "$service_unit" || local_ok=false
    done

    mapfile -t remaining < <(runner_service_units_for_repo "$repo")
    if [[ ${#remaining[@]} -gt 0 ]]; then
        warn "Nie udało się usunąć usług systemd dla ${GITHUB_OWNER}/${repo}: ${remaining[*]}"
        local_ok=false
    fi

    [[ "$remote_ok" == true && "$local_ok" == true ]]
}

install_org_runner() {
    local version="$1" arch="$2" dir token name
    dir="$(org_runner_dir)"; name="$(hostname -s)-$(sanitize_name "$ACTIVE_PROFILE")-$(sanitize_name "$GITHUB_OWNER")"
    [[ ! -f "$dir/.runner" ]] || { echo "Organization runner już istnieje: $dir"; return 3; }
    token="$(get_org_registration_token)" || return 1
    mkdir -p "$dir"; download_runner "$dir" "$version" "$arch"
    sudo -u "$RUNNER_USER" bash -c "cd '$dir' && ./config.sh --unattended \
      --url 'https://github.com/${GITHUB_OWNER}' --token '$token' --name '$name' \
      --labels '$CUSTOM_LABELS' --work '_work' --replace"
    unset token; write_runner_metadata "$dir"
    (cd "$dir" && ./svc.sh install "$RUNNER_USER" && ./svc.sh start)
}

uninstall_org_runner() {
    local dir token="" remote_ok=true
    dir="$(org_runner_dir)"; [[ -d "$dir" ]] || { echo "Runner lokalny nie istnieje: $dir"; return 3; }
    if [[ -x "$dir/svc.sh" ]]; then
        (
            cd "$dir"
            ./svc.sh stop || true
            ./svc.sh uninstall || true
        )
    fi
    if [[ -f "$dir/.runner" && -x "$dir/config.sh" ]]; then
        if token="$(get_org_remove_token)" && [[ -n "$token" && "$token" != null ]]; then
            sudo -u "$RUNNER_USER" bash -c "cd '$dir' && ./config.sh remove --unattended --token '$token'" || remote_ok=false
        else remote_ok=false; fi
    fi
    unset token; rm -rf "$dir"; [[ "$remote_ok" == true ]]
}

purge_if_empty() {
    local -a remaining_services=()
    [[ "$PURGE" == true ]] || return 0
    if [[ -d "$RUNNER_BASE" ]] && find "$RUNNER_BASE" -mindepth 1 -type f \
      \( -name .runner -o -name .chrisscriptbase-runner \) -print -quit | grep -q .; then
        warn "--purge: w $RUNNER_BASE nadal istnieją runnery."
        return 0
    fi

    mapfile -t remaining_services < <(list_action_runner_service_units)
    if [[ ${#remaining_services[@]} -gt 0 ]]; then
        warn "--purge: nadal istnieją usługi GitHub Actions Runner; nie usuwam użytkownika $RUNNER_USER."
        printf ' - %s\n' "${remaining_services[@]}" >&2
        return 0
    fi

    rm -rf "$RUNNER_BASE"
    if id "$RUNNER_USER" &>/dev/null; then
        userdel "$RUNNER_USER" 2>/dev/null || true
    fi
    echo "Purge zakończony."
}

process_user_profile() {
    local version="${1:-}" arch="${2:-}" repo rc
    local -a repositories=()
    local success=0 failed=0 skipped=0
    if [[ "$LIST_REPOS" == true ]]; then
        resolve_repositories
        return 0
    fi
    mapfile -t repositories < <(resolve_repositories)
    [[ ${#repositories[@]} -gt 0 ]] || { warn "Profil '$ACTIVE_PROFILE': nic nie wybrano."; return 0; }
    echo "Wybrane repozytoria [$ACTIVE_PROFILE]: ${#repositories[@]}"
    printf ' - %s\n' "${repositories[@]}"
    for repo in "${repositories[@]}"; do
        if [[ "$ACTION" == install ]]; then
            if install_repo_runner "$repo" "$version" "$arch"; then
                ((success += 1))
            else
                rc=$?
                if [[ "$rc" -eq 3 ]]; then
                    ((skipped += 1))
                else
                    ((failed += 1))
                fi
            fi
        else
            if uninstall_repo_runner "$repo"; then
                ((success += 1))
            else
                rc=$?
                if [[ "$rc" -eq 3 ]]; then
                    ((skipped += 1))
                else
                    ((failed += 1))
                fi
            fi
        fi
    done
    log "Podsumowanie profilu: $ACTIVE_PROFILE"
    echo "Akcja: $ACTION | Sukces: $success | Pominięte: $skipped | Błędy: $failed"
    [[ "$failed" -eq 0 ]]
}

process_org_profile() {
    local version="${1:-}" arch="${2:-}"
    if [[ "$LIST_REPOS" == true ]]; then
        warn "MODE=org: --list-repos dotyczy repozytoriów użytkownika; pomijam organization-level runnera."
        return 0
    fi
    [[ "$REPO_SELECTION_MODE" == all ]] ||
      warn "MODE=org: selekcja repo nie dotyczy organization-level runnera; użyj Runner Groups."
    if [[ "$ACTION" == install ]]; then
        install_org_runner "$version" "$arch"
    else
        uninstall_org_runner
    fi
}

main() {
    parse_args "$@"
    init_invoking_user
    if [[ "$LIST_PROFILES" == true ]]; then list_profiles; exit 0; fi
    require_root
    ensure_dependencies
    ensure_selection_ui_dependencies
    [[ ${#SELECTED_PROFILES[@]} -gt 0 ]] || SELECTED_PROFILES=(default)
    [[ "$ACTION" != install || "$LIST_REPOS" == true ]] || create_runner_user

    local version="" arch="" profile failed_profiles=0
    if [[ "$ACTION" == install && "$LIST_REPOS" == false ]]; then version="$(get_runner_version)"; arch="$(detect_arch)"; fi

    for profile in "${SELECTED_PROFILES[@]}"; do
        load_profile "$profile"; check_profile_config; validate_github_token
        echo "Invoking user: $INVOKING_USER"
        echo "Git config:    $GITCONFIG_PATH"
        echo "Profile root:  $(profile_root)"
        if [[ "$MODE" == org ]]; then
            process_org_profile "$version" "$arch" || ((failed_profiles+=1))
        else
            process_user_profile "$version" "$arch" || ((failed_profiles+=1))
        fi
    done

    [[ "$LIST_REPOS" == false ]] || exit 0
    [[ "$ACTION" != uninstall ]] || purge_if_empty
    echo; echo "Usługi runnerów:"
    systemctl --no-pager --type=service | grep actions.runner || true
    [[ "$failed_profiles" -eq 0 ]] || exit 2
}

main "$@"
