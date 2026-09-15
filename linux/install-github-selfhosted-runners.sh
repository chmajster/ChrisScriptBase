#!/usr/bin/env bash
set -Eeuo pipefail

# ChrisScriptBase - GitHub Self-Hosted Runner Manager
# Single-file implementation: manager + generated Dockerfile + generated container entrypoint.

ACTION="install"
ACTION_EXPLICIT=false
REINSTALL_ONLY=false
MODE_DEFAULT="${MODE:-user}"
OWNER_ENV="${GITHUB_OWNER:-}"
TOKEN_ENV="${GITHUB_TOKEN:-}"
RUNNER_BASE="${RUNNER_BASE:-/opt/github-runners}"
STATE_BASE="${DOCKER_STATE_BASE:-$RUNNER_BASE/docker}"
IMAGE="${DOCKER_IMAGE:-chrisscriptbase/github-actions-runner:local}"
RUNNER_VERSION="${RUNNER_VERSION:-}"
LABELS_DEFAULT="${CUSTOM_LABELS:-homelab}"
API_VERSION="${GITHUB_API_VERSION:-2026-03-10}"
SOCKET="${RUNNER_DOCKER_SOCKET:-true}"
ALLOW_SUDO="${RUNNER_ALLOW_SUDO:-true}"
INCLUDE_PUBLIC="${RUNNER_INCLUDE_PUBLIC:-false}"
REBUILD=false
FORCE_RECREATE=false
FORCE_REMOTE_DELETE=false
PURGE=false
PREPARE_HOST=false
STATUS_ONLY=false
REPAIR_MODE=false
CHECK_UPDATES=false
UPDATE_RUNNER=false
LIST_PROFILES=false
LIST_REPOS=false
SELECT_MODE=""
UI="auto"
UI_LANGUAGE="${RUNNER_UI_LANGUAGE:-pl}"
RUNNER_CPUS="${RUNNER_CPUS:-}"
RUNNER_MEMORY="${RUNNER_MEMORY:-}"
RUNNER_PIDS_LIMIT="${RUNNER_PIDS_LIMIT:-512}"
LOG_MAX_SIZE="${RUNNER_LOG_MAX_SIZE:-20m}"
LOG_MAX_FILE="${RUNNER_LOG_MAX_FILE:-3}"
APT_UPDATED=false
PACKAGE_METADATA_UPDATED=false
PKG_MANAGER="${PACKAGE_MANAGER:-}"
HOST_REQUIRED_PACKAGES=(
    ca-certificates
    curl
    jq
    git
    coreutils
    gawk
    sudo
    libc-bin
    findutils
    grep
    sed
    hostname
    docker.io
    dialog
)
PROFILES=()
REPOS=()
PROFILE="default"
MODE="$MODE_DEFAULT"
OWNER=""
TOKEN=""
LABELS="$LABELS_DEFAULT"
CALLER=""
CALLER_HOME=""
GITCONFIG=""

die(){ echo "ERROR: $*" >&2; exit 1; }
warn(){ echo "WARNING: $*" >&2; }
log(){ echo; echo "=== $* ==="; }
san(){ printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_.-'; }

help(){
cat <<'EOF'
GitHub Self-Hosted Runner Manager (single-file, Docker)

Cała implementacja znajduje się w tym jednym skrypcie.
Dockerfile oraz entrypoint kontenera są generowane tymczasowo podczas docker build.

UŻYCIE
  sudo bash install-github-selfhosted-runners.sh [akcja] [opcje]

AKCJE
  --install                 Instalacja/reconciliation runnerów. Domyślne.
  --uninstall               Usuń wybrane runnery.
  --purge                   Z --uninstall usuń pusty stan i lokalny obraz.
  --prepare-host            Przygotuj hosta: zależności, Docker i dialog.
  --status                  Pokaż status kontenerów i rejestracji GitHub.
  --repair                  Napraw zatrzymane/offline/brakujące runnery.
  --check-updates           Porównaj wersję Actions Runner z najnowszą.
  --update-runner           Pobierz najnowszy runner, przebuduj obraz i przeinstaluj.

PROFILE
  -p, --profile NAME        Profil z ~/.gitconfig; można powtórzyć.
  --profiles A,B,C          Kilka profili.
  --list-profiles           Pokaż profile.

REPOZYTORIA
  -r, --repo REPO           Repo; można powtórzyć.
  --repos A,B,C             Kilka repo.
  --repo PROFILE:REPO       Repo tylko dla wskazanego profilu.
  --all-repos               Wszystkie dostępne repo.
  --select-repos            Interaktywny wybór repozytoriów.
  --list-repos              Pokaż repozytoria.
  --private-only            Uwzględniaj tylko prywatne repo. Domyślne.
  --include-public          Pozwól również na publiczne repozytoria.

UI
  -g, --gui                 Pełny polski TUI oparty o dialog. Wszystkie funkcje
                            i opcje operacyjne skryptu są dostępne bez flag CLI:
                            akcje, profile, repozytoria, runner i host.
  --tui                     Alias --gui.
  --zenity                  Wymuś graficzny interfejs Zenity dla wyboru repo.
  --language pl             Język interfejsu: polski. Domyślny.
  --polish                  Alias dla --language pl.

DOCKER / RUNNER
  --docker-socket           Udostępnij /var/run/docker.sock jobom. Domyślne.
  --no-docker-socket        Nie udostępniaj Docker socketa.
  --allow-sudo              Runner ma NOPASSWD sudo w kontenerze. Domyślne.
  --no-sudo                 Usuń NOPASSWD sudo.
  --rebuild-image           Wymuś ponowny docker build.
  --force-recreate          Odtwórz kontenery; przy HTTP 409/422 pozwól
                            kontynuować z config.sh --replace.
  --cpus N                  Limit CPU kontenera, np. 2 lub 1.5.
  --memory SIZE             Limit RAM, np. 4g.
  --pids-limit N            Limit procesów. Domyślnie 512.
  --runner-version VER      Wersja actions/runner; puste = latest.

HOST
  Obsługiwane managery pakietów: apt, dnf, yum i zypper.
  Można wymusić manager przez PACKAGE_MANAGER=apt|dnf|yum|zypper.

BEZPIECZEŃSTWO
  Długoterminowy GitHub PAT pozostaje wyłącznie na hoście.
  Kontener otrzymuje tylko krótkotrwały registration token.
  Publiczne repozytoria są domyślnie wyłączone.
  Docker socket daje workflow praktycznie uprawnienia root na hoście Docker.

PRZYKŁADY
  sudo bash install-github-selfhosted-runners.sh --prepare-host
  sudo bash install-github-selfhosted-runners.sh --status
  sudo bash install-github-selfhosted-runners.sh --repair
  sudo bash install-github-selfhosted-runners.sh --check-updates
  sudo bash install-github-selfhosted-runners.sh --update-runner
  sudo bash install-github-selfhosted-runners.sh -g
EOF
}

append_csv(){
    local array_name="$1" csv_value="$2" item
    local -a parts=()
    local -n target_array="$array_name"
    IFS=',' read -r -a parts <<< "$csv_value"
    for item in "${parts[@]}"; do
        item="${item//[[:space:]]/}"
        [[ -n "$item" ]] && target_array+=("$item")
    done
}

set_selection_mode(){
    local requested="$1"
    [[ -z "$SELECT_MODE" || "$SELECT_MODE" == "$requested" ]] || die "Sprzeczne opcje wyboru repozytoriów."
    SELECT_MODE="$requested"
}

args(){
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help) help; exit 0 ;;
            --install) ACTION="install"; ACTION_EXPLICIT=true; shift ;;
            --uninstall) ACTION="uninstall"; ACTION_EXPLICIT=true; shift ;;
            --purge) PURGE=true; shift ;;
            --prepare-host) PREPARE_HOST=true; shift ;;
            --status) STATUS_ONLY=true; ACTION_EXPLICIT=true; shift ;;
            --repair) REPAIR_MODE=true; ACTION="install"; REINSTALL_ONLY=true; ACTION_EXPLICIT=true; shift ;;
            --check-updates) CHECK_UPDATES=true; ACTION_EXPLICIT=true; shift ;;
            --update-runner) UPDATE_RUNNER=true; ACTION="install"; REINSTALL_ONLY=true; FORCE_RECREATE=true; REBUILD=true; ACTION_EXPLICIT=true; shift ;;
            --docker-socket) SOCKET=true; shift ;;
            --no-docker-socket) SOCKET=false; shift ;;
            --allow-sudo) ALLOW_SUDO=true; shift ;;
            --no-sudo) ALLOW_SUDO=false; shift ;;
            --include-public) INCLUDE_PUBLIC=true; shift ;;
            --private-only) INCLUDE_PUBLIC=false; shift ;;
            --rebuild-image) REBUILD=true; shift ;;
            --force-recreate) FORCE_RECREATE=true; FORCE_REMOTE_DELETE=true; shift ;;
            --cpus) [[ $# -ge 2 ]] || die "$1 wymaga wartości."; RUNNER_CPUS="$2"; shift 2 ;;
            --memory) [[ $# -ge 2 ]] || die "$1 wymaga wartości."; RUNNER_MEMORY="$2"; shift 2 ;;
            --pids-limit) [[ $# -ge 2 ]] || die "$1 wymaga wartości."; [[ "$2" =~ ^[0-9]+$ ]] || die "--pids-limit wymaga liczby całkowitej."; RUNNER_PIDS_LIMIT="$2"; shift 2 ;;
            --runner-version) [[ $# -ge 2 ]] || die "$1 wymaga wersji."; RUNNER_VERSION="$2"; shift 2 ;;
            -p|--profile) [[ $# -ge 2 ]] || die "$1 wymaga nazwy profilu."; PROFILES+=("$2"); shift 2 ;;
            --profiles) [[ $# -ge 2 ]] || die "$1 wymaga listy profili."; append_csv PROFILES "$2"; shift 2 ;;
            -r|--repo) [[ $# -ge 2 ]] || die "$1 wymaga repozytorium."; set_selection_mode explicit; REPOS+=("$2"); shift 2 ;;
            --repos) [[ $# -ge 2 ]] || die "$1 wymaga listy repozytoriów."; set_selection_mode explicit; append_csv REPOS "$2"; shift 2 ;;
            --all-repos) set_selection_mode all; shift ;;
            --select-repos) set_selection_mode interactive; shift ;;
            -g|--gui|-GUI) UI="dialog"; shift ;;
            --tui) UI="dialog"; shift ;;
            --zenity) UI="zenity"; shift ;;
            --language) [[ $# -ge 2 ]] || die "$1 wymaga języka."; UI_LANGUAGE="${2,,}"; shift 2 ;;
            --polish) UI_LANGUAGE="pl"; shift ;;
            --list-profiles) LIST_PROFILES=true; shift ;;
            --list-repos) LIST_REPOS=true; shift ;;
            *) die "Nieznana opcja: $1" ;;
        esac
    done
    if [[ "$UI" == dialog || "$UI" == zenity ]]; then
        if [[ -z "$SELECT_MODE" ]]; then
            SELECT_MODE="interactive"
        elif [[ "$SELECT_MODE" != interactive ]]; then
            die "-g/--gui, --tui i --zenity nie mogą być łączone z --all-repos, --repo ani --repos"
        fi
    fi
    [[ -n "$SELECT_MODE" ]] || SELECT_MODE="all"
    [[ "$PURGE" != true || "$ACTION" == uninstall ]] || die "--purge wymaga --uninstall"
    case "$SOCKET" in true|false) ;; *) die "RUNNER_DOCKER_SOCKET musi być true/false." ;; esac
    case "$ALLOW_SUDO" in true|false) ;; *) die "RUNNER_ALLOW_SUDO musi być true/false." ;; esac
    case "$INCLUDE_PUBLIC" in true|false) ;; *) die "RUNNER_INCLUDE_PUBLIC musi być true/false." ;; esac
    case "$UI_LANGUAGE" in pl|pl_pl|polski|polish) UI_LANGUAGE="pl" ;; *) die "Obsługiwany język interfejsu: pl (polski)." ;; esac
}

caller_init(){
    if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != root ]]; then CALLER="$SUDO_USER"; else CALLER="$(id -un)"; fi
    CALLER_HOME="$(getent passwd "$CALLER" | cut -d: -f6)"
    [[ -n "$CALLER_HOME" ]] || die "Nie można ustalić HOME użytkownika: $CALLER"
    GITCONFIG="$CALLER_HOME/.gitconfig"
}

cfg(){ local key="$1"; [[ -f "$GITCONFIG" ]] && git config --file "$GITCONFIG" --get "$key" 2>/dev/null || true; }
decode_token(){ printf '%s' "$1" | base64 --decode 2>/dev/null; }

profiles(){
    local base_config="${OWNER_ENV}${TOKEN_ENV}$(cfg github.username)$(cfg github.organization)$(cfg github.tokenBase64)"
    [[ -n "$base_config" ]] && echo default
    if [[ -f "$GITCONFIG" ]]; then
        git config --file "$GITCONFIG" --name-only --get-regexp '^github\..+\.(username|organization|owner|tokenBase64|mode|labels)$' 2>/dev/null | awk -F. 'NF>=3 {print $2}' || true
    fi
}

load_profile(){
    local profile_name="$1" prefix="" encoded_token="" configured_labels=""
    PROFILE="$profile_name"; MODE="$MODE_DEFAULT"; OWNER=""; TOKEN=""; LABELS="$LABELS_DEFAULT"
    [[ "$PROFILE" == default ]] || prefix="$PROFILE."
    MODE="$(cfg "github.${prefix}mode")"; [[ -n "$MODE" ]] || MODE="$MODE_DEFAULT"
    if [[ "$PROFILE" == default ]]; then OWNER="$OWNER_ENV"; TOKEN="$TOKEN_ENV"; fi
    [[ -n "$OWNER" ]] || OWNER="$(cfg "github.${prefix}owner")"
    if [[ -z "$OWNER" ]]; then
        if [[ "$MODE" == org ]]; then OWNER="$(cfg "github.${prefix}organization")"; else OWNER="$(cfg "github.${prefix}username")"; fi
    fi
    if [[ -z "$TOKEN" ]]; then
        encoded_token="$(cfg "github.${prefix}tokenBase64")"
        [[ -z "$encoded_token" ]] || TOKEN="$(decode_token "$encoded_token")" || die "$PROFILE: nie można zdekodować tokenBase64"
    fi
    configured_labels="$(cfg "github.${prefix}labels")"; [[ -z "$configured_labels" ]] || LABELS="$configured_labels"
    case "$MODE" in user|org) ;; *) die "$PROFILE: mode musi być user albo org" ;; esac
    [[ -n "$TOKEN" ]] || die "$PROFILE: brak tokenu"
    if [[ -z "$OWNER" && "$MODE" == user ]]; then
        OWNER="$(api GET /user | jq -r '.login // empty')" || die "$PROFILE: token odrzucony"
    fi
    [[ -n "$OWNER" ]] || die "$PROFILE: brak ownera"
}

effective_labels(){
    local label
    local -a out=() raw=()
    IFS=',' read -r -a raw <<< "$LABELS"
    for label in "${raw[@]}"; do
        label="${label//[[:space:]]/}"
        [[ -n "$label" ]] || continue
        [[ "${label,,}" != docker || "$SOCKET" == true ]] || continue
        out+=("$label")
    done
    [[ "$SOCKET" != true ]] || out+=(docker)
    printf '%s\n' "${out[@]}" | awk 'NF && !seen[tolower($0)]++' | paste -sd, -
}

api(){
    local method="$1" endpoint="$2" body_file="" headers_file="" http_code="" curl_rc=0 body="" message="" remaining="" retry_after=""
    body_file="$(mktemp)"; headers_file="$(mktemp)"
    http_code="$(curl --silent --show-error --location --request "$method" \
      --header "Accept: application/vnd.github+json" \
      --header "Authorization: Bearer $TOKEN" \
      --header "X-GitHub-Api-Version: $API_VERSION" \
      --dump-header "$headers_file" --output "$body_file" --write-out '%{http_code}' \
      "https://api.github.com$endpoint")" || curl_rc=$?
    if (( curl_rc != 0 )); then
        warn "GitHub API $method $endpoint: błąd transportu curl=$curl_rc"
        rm -f "$body_file" "$headers_file"; return 47
    fi
    body="$(cat "$body_file")"
    if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
        printf '%s' "$body"; rm -f "$body_file" "$headers_file"; return 0
    fi
    message="$(jq -r '.message // empty' "$body_file" 2>/dev/null || true)"
    remaining="$(awk -F': *' 'tolower($1)=="x-ratelimit-remaining" {gsub("\\r","",$2); print $2; exit}' "$headers_file" 2>/dev/null || true)"
    retry_after="$(awk -F': *' 'tolower($1)=="retry-after" {gsub("\\r","",$2); print $2; exit}' "$headers_file" 2>/dev/null || true)"
    warn "GitHub API $method $endpoint -> HTTP $http_code${message:+: $message}"
    rm -f "$body_file" "$headers_file"
    case "$http_code" in
        401) return 40 ;;
        403) [[ "$remaining" == 0 || -n "$retry_after" ]] && return 44 || return 41 ;;
        404) return 42 ;;
        409|422) return 43 ;;
        429) return 44 ;;
        5??) return 45 ;;
        *) return 46 ;;
    esac
}

auth(){ local login=""; login="$(api GET /user | jq -r '.login // empty')" || die "$PROFILE: token odrzucony"; [[ -n "$login" ]] || die "$PROFILE: GitHub API nie zwrócił loginu"; echo "Profil=$PROFILE owner=$OWNER mode=$MODE token-owner=$login labels=$(effective_labels)"; }

remote_repos(){
    local page=1 response="" count=0
    while true; do
        response="$(api GET "/user/repos?affiliation=owner&per_page=100&page=$page&sort=full_name")" || return $?
        count="$(jq 'length' <<< "$response")"; (( count > 0 )) || break
        if [[ "$INCLUDE_PUBLIC" == true ]]; then
            jq -r --arg owner "$OWNER" '.[] | select((.owner.login|ascii_downcase)==($owner|ascii_downcase)) | select(.archived==false) | .name' <<< "$response"
        else
            jq -r --arg owner "$OWNER" '.[] | select((.owner.login|ascii_downcase)==($owner|ascii_downcase)) | select(.archived==false) | select(.private==true) | .name' <<< "$response"
        fi
        ((page += 1))
    done
}

profile_root(){ if [[ "$PROFILE" == default ]]; then echo "$STATE_BASE/default"; else echo "$STATE_BASE/profiles/$(san "$PROFILE")"; fi; }
repo_state(){ echo "$(profile_root)/repositories/$(san "$1")"; }
org_state(){ echo "$(profile_root)/organization"; }
repo_runner(){ local host=""; host="$(hostname -s | tr '[:upper:]' '[:lower:]')"; if [[ "$PROFILE" == default ]]; then echo "${host}-$(san "$1")"; else echo "${host}-$(san "$PROFILE")-$(san "$1")"; fi; }
org_runner(){ local host=""; host="$(hostname -s | tr '[:upper:]' '[:lower:]')"; echo "${host}-$(san "$PROFILE")-$(san "$OWNER")"; }
repo_container(){ echo "github-runner-$(san "$PROFILE")-$(san "$1")"; }
org_container(){ echo "github-runner-$(san "$PROFILE")-org"; }

apt_update_once(){
    command -v apt-get >/dev/null 2>&1 || die "Brak apt-get."
    [[ "$APT_UPDATED" == true ]] && return 0
    apt-get update; APT_UPDATED=true; PACKAGE_METADATA_UPDATED=true
}

apt_install(){
    (( $# > 0 )) || return 0
    apt_update_once
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
}

detect_package_manager(){
    if [[ -n "$PKG_MANAGER" ]]; then
        case "$PKG_MANAGER" in apt|dnf|yum|zypper) printf '%s\n' "$PKG_MANAGER"; return 0 ;; *) die "Nieobsługiwany PACKAGE_MANAGER=$PKG_MANAGER" ;; esac
    fi
    if command -v apt-get >/dev/null 2>&1; then PKG_MANAGER=apt
    elif command -v dnf >/dev/null 2>&1; then PKG_MANAGER=dnf
    elif command -v yum >/dev/null 2>&1; then PKG_MANAGER=yum
    elif command -v zypper >/dev/null 2>&1; then PKG_MANAGER=zypper
    else die "Nie znaleziono obsługiwanego managera pakietów: apt/dnf/yum/zypper"
    fi
    printf '%s\n' "$PKG_MANAGER"
}

package_name_for(){
    local requirement="$1" manager="${2:-$(detect_package_manager)}"
    case "$requirement" in
        libc-bin) case "$manager" in apt) echo libc-bin ;; dnf|yum) echo glibc-common ;; zypper) echo glibc ;; esac ;;
        docker.io) case "$manager" in apt) echo docker.io ;; zypper) echo docker ;; dnf|yum) echo docker ;; esac ;;
        *) echo "$requirement" ;;
    esac
}

requirement_installed(){
    local requirement="$1"
    case "$requirement" in
        ca-certificates) [[ -s /etc/ssl/certs/ca-certificates.crt || -s /etc/pki/tls/certs/ca-bundle.crt ]] ;;
        coreutils) command -v base64 >/dev/null 2>&1 && command -v sha256sum >/dev/null 2>&1 ;;
        gawk) command -v awk >/dev/null 2>&1 ;;
        libc-bin) command -v getent >/dev/null 2>&1 ;;
        findutils) command -v find >/dev/null 2>&1 ;;
        docker.io) command -v docker >/dev/null 2>&1 ;;
        *) command -v "$requirement" >/dev/null 2>&1 ;;
    esac
}
package_installed(){ requirement_installed "$1"; }

package_metadata_update_once(){
    local manager="$(detect_package_manager)"
    [[ "$PACKAGE_METADATA_UPDATED" == true ]] && return 0
    case "$manager" in
        apt) apt_update_once; return ;;
        dnf) dnf -y makecache ;;
        yum) yum -y makecache ;;
        zypper) zypper --non-interactive refresh ;;
    esac
    PACKAGE_METADATA_UPDATED=true
}

package_install_names(){
    (( $# > 0 )) || return 0
    local manager="$(detect_package_manager)"
    package_metadata_update_once
    case "$manager" in
        apt) DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@" ;;
        dnf) dnf install -y "$@" ;;
        yum) yum install -y "$@" ;;
        zypper) zypper --non-interactive install --no-recommends "$@" ;;
    esac
}

install_docker_engine(){
    command -v docker >/dev/null 2>&1 && return 0
    local manager="$(detect_package_manager)"
    case "$manager" in
        apt) package_install_names docker.io ;;
        zypper) package_install_names docker ;;
        dnf)
            package_metadata_update_once
            dnf install -y docker-ce docker-ce-cli containerd.io 2>/dev/null || dnf install -y moby-engine 2>/dev/null || dnf install -y docker 2>/dev/null || die "Brak Docker Engine w repozytoriach dnf. Skonfiguruj Docker CE lub Moby."
            ;;
        yum)
            package_metadata_update_once
            yum install -y docker-ce docker-ce-cli containerd.io 2>/dev/null || yum install -y moby-engine 2>/dev/null || yum install -y docker 2>/dev/null || die "Brak Docker Engine w repozytoriach yum. Skonfiguruj Docker CE lub Moby."
            ;;
    esac
    command -v docker >/dev/null 2>&1 || die "Instalacja Docker Engine nie udostępniła polecenia docker"
}

install_requirements(){
    local requirement="" package="" docker_needed=false; local -a packages=() unique=()
    for requirement in "$@"; do
        requirement_installed "$requirement" && continue
        if [[ "$requirement" == docker.io ]]; then docker_needed=true; continue; fi
        package="$(package_name_for "$requirement")"; packages+=("$package")
    done
    if (( ${#packages[@]} > 0 )); then
        mapfile -t unique < <(printf '%s\n' "${packages[@]}" | awk 'NF && !seen[$0]++')
        log "Instalacja brakujących pakietów hosta ($(detect_package_manager)): ${unique[*]}"
        package_install_names "${unique[@]}"
    fi
    [[ "$docker_needed" != true ]] || install_docker_engine
}

package_version(){
    local requirement="$1" manager="$(detect_package_manager)" package=""
    [[ "$requirement" != docker.io ]] || { docker --version 2>/dev/null | head -1; return 0; }
    package="$(package_name_for "$requirement" "$manager")"
    case "$manager" in
        apt) dpkg-query -W -f='${Version}' "$package" 2>/dev/null || echo n/d ;;
        dnf|yum|zypper) rpm -q --qf '%{VERSION}-%{RELEASE}' "$package" 2>/dev/null || echo n/d ;;
    esac
}

ensure_prepare_host_packages(){
    [[ "$PREPARE_HOST" == true ]] || return 0
    install_requirements "${HOST_REQUIRED_PACKAGES[@]}"
}

host_package_report(){
    local requirement="" version="" docker_version="" issues=0
    log "Weryfikacja pakietów hosta ($(detect_package_manager))"
    printf '%-22s %-10s %s\n' "Pakiet/wymaganie" "Status" "Wersja"
    printf '%-22s %-10s %s\n' "----------------------" "----------" "------------------------------"
    for requirement in "${HOST_REQUIRED_PACKAGES[@]}"; do
        if requirement_installed "$requirement"; then
            version="$(package_version "$requirement")"; [[ -n "$version" ]] || version=n/d
            printf '%-22s %-10s %s\n' "$requirement" "OK" "$version"
        else
            printf '%-22s %-10s %s\n' "$requirement" "BRAK" "-"; ((issues += 1))
        fi
    done
    echo
    if docker info >/dev/null 2>&1; then
        docker_version="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"; [[ -n "$docker_version" ]] || docker_version=n/d
        printf 'Docker Engine: OK (wersja %s)\n' "$docker_version"
    else echo 'Docker Engine: BŁĄD'; ((issues += 1)); fi
    (( issues == 0 )) || { warn "Weryfikacja hosta wykryła $issues braków/błędów."; return 1; }
    printf 'Wszystkie wymagane pakiety są zainstalowane (%d/%d).\n' "${#HOST_REQUIRED_PACKAGES[@]}" "${#HOST_REQUIRED_PACKAGES[@]}"
}

ensure_dependencies(){
    local -a requirements=(ca-certificates curl jq git coreutils gawk sudo libc-bin findutils grep sed hostname)
    [[ "$LIST_REPOS" == true && "$PREPARE_HOST" == false ]] || requirements+=(docker.io)
    [[ "$PREPARE_HOST" != true && "$UI" != dialog ]] || requirements+=(dialog)
    [[ "$UI" != zenity ]] || requirements+=(zenity)
    install_requirements "${requirements[@]}"
    local requirement=""; for requirement in "${requirements[@]}"; do requirement_installed "$requirement" || die "Po instalacji nadal brakuje wymagania: $requirement"; done
}

docker_ready(){
    local i
    command -v docker >/dev/null 2>&1 || install_docker_engine
    docker info >/dev/null 2>&1 && return 0
    log "Uruchamianie Docker Engine"
    if command -v systemctl >/dev/null 2>&1; then systemctl enable --now docker >/dev/null 2>&1 || true; fi
    if ! docker info >/dev/null 2>&1 && command -v service >/dev/null 2>&1; then service docker start >/dev/null 2>&1 || true; fi
    for ((i=1; i<=10; i++)); do docker info >/dev/null 2>&1 && return 0; sleep 1; done
    [[ -S /var/run/docker.sock ]] || die "Docker CLI działa, ale /var/run/docker.sock nie istnieje."
    docker info 2>&1 | tail -20 >&2 || true; die "Docker Engine nie odpowiada."
}

latest_runner_version(){
    local version=""
    version="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r '.tag_name // empty' | sed 's/^v//')" || return 1
    [[ -n "$version" && "$version" != null ]] || return 1
    printf '%s\n' "$version"
}

resolve_runner_version(){
    local version="${RUNNER_VERSION#v}"
    [[ -n "$version" ]] || version="$(latest_runner_version)" || return 1
    [[ -n "$version" && "$version" != null ]] || { warn "Nie udało się ustalić wersji actions/runner."; return 1; }
    [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]] || { warn "Nieprawidłowa wersja actions/runner: $version"; return 1; }
    printf '%s\n' "$version"
}

image_runner_version(){ docker image inspect -f '{{index .Config.Labels "com.chrisscriptbase.runner-version"}}' "$IMAGE" 2>/dev/null | grep -v '^<no value>$' || true; }
container_runner_version(){ local container_name="$1"; docker exec "$container_name" /actions-runner/bin/Runner.Listener --version 2>/dev/null | tail -1 | tr -d '\r' || true; }

ui_message(){
    local title="$1" text="$2"
    if [[ "$UI" == dialog && -r /dev/tty && -w /dev/tty ]]; then dialog --clear --backtitle "ChrisScriptBase • GitHub Runner" --title " $title " --msgbox "$text" 12 90 </dev/tty >/dev/tty 2>/dev/tty || true; else printf '%s\n' "$text"; fi
}

check_runner_updates(){
    local latest="" current="" container_name="" text=""
    latest="$(latest_runner_version)" || { warn "Nie udało się pobrać najnowszej wersji actions/runner."; return 1; }
    current="$(image_runner_version)"
    if [[ -z "$current" ]]; then container_name="$(docker ps -a --filter label=com.chrisscriptbase.github-runner=true --format '{{.Names}}' | head -1)"; [[ -z "$container_name" ]] || current="$(container_runner_version "$container_name")"; fi
    [[ -n "$current" ]] || current=n/d
    text="Actions Runner\nZainstalowana: $current\nNajnowsza: $latest\nStatus: $([[ "$current" == "$latest" ]] && echo aktualny || echo 'dostępna aktualizacja')"
    ui_message "Aktualizacje runnera" "$text"
}

render_docker_context(){
    local context_dir="$1"
    cat > "$context_dir/Dockerfile" <<'DOCKERFILE'
FROM ubuntu:24.04
ARG TARGETARCH
ARG RUNNER_VERSION=""
LABEL com.chrisscriptbase.runner-version="${RUNNER_VERSION}"
ENV DEBIAN_FRONTEND=noninteractive
ENV RUNNER_HOME=/actions-runner
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git git-lfs jq sudo tar gzip zip unzip xz-utils zstd \
      openssh-client rsync file build-essential pkg-config \
      python3 python3-pip python3-venv shellcheck docker.io \
 && git lfs install --system \
 && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1001 --shell /bin/bash runner \
 && mkdir -p "${RUNNER_HOME}" "${RUNNER_HOME}/_work" \
 && chown -R runner:runner "${RUNNER_HOME}"
WORKDIR ${RUNNER_HOME}
RUN set -eux; \
 arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
 case "$arch" in amd64) runner_arch=x64 ;; arm64) runner_arch=arm64 ;; arm) runner_arch=arm ;; *) echo "Unsupported architecture: $arch" >&2; exit 1 ;; esac; \
 version="${RUNNER_VERSION}"; \
 if [ -z "$version" ]; then version="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r '.tag_name' | sed 's/^v//')"; fi; \
 curl -fsSL "https://github.com/actions/runner/releases/download/v${version}/actions-runner-linux-${runner_arch}-${version}.tar.gz" -o /tmp/actions-runner.tar.gz; \
 tar xzf /tmp/actions-runner.tar.gz -C "${RUNNER_HOME}"; \
 rm -f /tmp/actions-runner.tar.gz; \
 "${RUNNER_HOME}/bin/installdependencies.sh"; \
 chown -R runner:runner "${RUNNER_HOME}"
COPY runner-entrypoint.sh /usr/local/bin/runner-entrypoint.sh
RUN chmod 0755 /usr/local/bin/runner-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/runner-entrypoint.sh"]
DOCKERFILE
    cat > "$context_dir/runner-entrypoint.sh" <<'ENTRYPOINT'
#!/usr/bin/env bash
set -Eeuo pipefail
cd /actions-runner
[[ "$(id -u)" -eq 0 ]] || { echo "ERROR: entrypoint must start as root." >&2; exit 1; }
REG_TOKEN_FILE="${RUNNER_REGISTRATION_TOKEN_FILE:-/run/secrets/runner_registration_token}"
RUNNER_SCOPE="${RUNNER_SCOPE:-repo}"
RUNNER_NAME="${RUNNER_NAME:-$(hostname)}"
RUNNER_LABELS="${RUNNER_LABELS:-docker}"
RUNNER_WORKDIR="${RUNNER_WORKDIR:-_work}"
ALLOW_SUDO="${RUNNER_ALLOW_SUDO:-true}"
[[ -n "${GITHUB_OWNER:-}" ]] || { echo "ERROR: GITHUB_OWNER is required." >&2; exit 1; }
if [[ -S /var/run/docker.sock ]]; then
    docker_gid="$(stat -c '%g' /var/run/docker.sock)"
    docker_group="$(getent group "$docker_gid" | cut -d: -f1 || true)"
    if [[ -z "$docker_group" ]]; then docker_group="docker-host"; groupadd --gid "$docker_gid" "$docker_group"; fi
    usermod -aG "$docker_group" runner
fi
case "$ALLOW_SUDO" in
 true) echo 'runner ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/runner; chmod 0440 /etc/sudoers.d/runner ;;
 false) rm -f /etc/sudoers.d/runner ;;
 *) echo "ERROR: RUNNER_ALLOW_SUDO must be true or false." >&2; exit 1 ;;
esac
mkdir -p "$RUNNER_WORKDIR"
chown -R runner:runner /actions-runner "$RUNNER_WORKDIR"
case "$RUNNER_SCOPE" in
 repo) [[ -n "${GITHUB_REPOSITORY:-}" ]] || { echo "ERROR: GITHUB_REPOSITORY is required for repo scope." >&2; exit 1; }; RUNNER_URL="https://github.com/${GITHUB_OWNER}/${GITHUB_REPOSITORY}" ;;
 org) RUNNER_URL="https://github.com/${GITHUB_OWNER}" ;;
 *) echo "ERROR: RUNNER_SCOPE must be repo or org." >&2; exit 1 ;;
esac
if [[ ! -f .runner ]]; then
    [[ -r "$REG_TOKEN_FILE" ]] || { echo "ERROR: registration token file is required for initial registration: $REG_TOKEN_FILE" >&2; exit 1; }
    registration_token="$(cat "$REG_TOKEN_FILE")"
    [[ -n "$registration_token" ]] || { echo "ERROR: registration token is empty." >&2; exit 1; }
    sudo -u runner -H ./config.sh --unattended --url "$RUNNER_URL" --token "$registration_token" --name "$RUNNER_NAME" --labels "$RUNNER_LABELS" --work "$RUNNER_WORKDIR" --replace
    unset registration_token
fi
exec sudo -u runner -H ./run.sh
ENTRYPOINT
    chmod 0755 "$context_dir/runner-entrypoint.sh"
}

build_image(){
    local context_dir="" resolved_version=""; local -a build_args=(build --pull -t "$IMAGE")
    if [[ "$REBUILD" == false ]] && docker image inspect "$IMAGE" >/dev/null 2>&1; then return 0; fi
    resolved_version="$(resolve_runner_version)" || return 1
    context_dir="$(mktemp -d)"; render_docker_context "$context_dir"
    build_args+=(--build-arg "RUNNER_VERSION=$resolved_version")
    build_args+=("$context_dir"); log "Budowanie obrazu $IMAGE z actions/runner v$resolved_version"
    if ! docker "${build_args[@]}"; then rm -rf "$context_dir"; return 1; fi
    rm -rf "$context_dir"
}

meta_get(){ local file="$1" key="$2"; awk -F= -v key="$key" '$1==key {sub(/^[^=]*=/, ""); print; exit}' "$file"; }
registration_token(){ local endpoint="" response="" rc=0; if [[ "$MODE" == org ]]; then endpoint="/orgs/$OWNER/actions/runners/registration-token"; else endpoint="/repos/$OWNER/$1/actions/runners/registration-token"; fi; response="$(api POST "$endpoint")" || { rc=$?; return "$rc"; }; jq -r '.token // empty' <<< "$response"; }
runner_endpoint(){ local repo_name="${1:-}"; if [[ "$MODE" == org ]]; then echo "/orgs/$OWNER/actions/runners"; else echo "/repos/$OWNER/$repo_name/actions/runners"; fi; }

runner_lookup(){
    local endpoint="$1" runner_name="$2" page=1 response="" page_size=0 row=""
    while true; do
        response="$(api GET "$endpoint?per_page=100&page=$page")" || return $?
        row="$(jq -r --arg name "$runner_name" '.runners[]? | select(.name==$name) | [.id,.status,.busy] | @tsv' <<< "$response" | head -1)"
        [[ -z "$row" ]] || { printf '%s\n' "$row"; return 0; }
        page_size="$(jq '.runners | length' <<< "$response")"; (( page_size == 100 )) || return 3
        ((page += 1))
    done
}

remote_delete(){
    local endpoint="$1" runner_name="$2" row="" id="" rc=0
    row="$(runner_lookup "$endpoint" "$runner_name")" || rc=$?
    (( rc == 3 || rc == 42 )) && return 0
    (( rc == 0 )) || return "$rc"
    id="${row%%$'\t'*}"; [[ -n "$id" ]] || return 0
    if api DELETE "$endpoint/$id" >/dev/null; then return 0; else rc=$?; fi
    (( rc == 42 )) && return 0
    return "$rc"
}

remote_delete_recreate(){
    local endpoint="$1" runner_name="$2" attempt=1 rc=1 max_attempts="${GITHUB_API_RETRIES:-3}" delay="${GITHUB_API_RETRY_DELAY:-2}"
    for ((attempt=1; attempt<=max_attempts; attempt++)); do
        if remote_delete "$endpoint" "$runner_name"; then return 0; else rc=$?; fi
        case "$rc" in 43|44|45|47) ;; *) return "$rc" ;; esac
        if (( attempt < max_attempts )); then warn "Usunięcie $runner_name nie powiodło się (próba $attempt/$max_attempts, rc=$rc). Ponawiam za ${delay}s..."; sleep "$delay"; (( delay *= 2 )); fi
    done
    if [[ "$FORCE_REMOTE_DELETE" == true && "$rc" -eq 43 ]]; then warn "FORCE: konflikt HTTP 409/422 dla $runner_name. Kontynuuję; config.sh --replace zastąpi wpis."; return 0; fi
    return "$rc"
}

wait_runner_online(){
    local endpoint="$1" runner_name="$2" container_name="$3" attempts="${RUNNER_HEALTH_ATTEMPTS:-20}" row="" status="" i
    for ((i=1;i<=attempts;i++)); do
        if [[ "$(docker inspect -f '{{.State.Running}}' "$container_name" 2>/dev/null || true)" != true ]]; then docker logs "$container_name" 2>&1 | tail -100 >&2 || true; return 1; fi
        row="$(runner_lookup "$endpoint" "$runner_name" 2>/dev/null || true)"
        if [[ -n "$row" ]]; then status="$(cut -f2 <<< "$row")"; [[ "$status" != online ]] || return 0; fi
        sleep 2
    done
    warn "Runner $runner_name nie osiągnął statusu online."; docker logs "$container_name" 2>&1 | tail -100 >&2 || true; return 1
}

config_hash(){
    local image_id="" labels=""; image_id="$(docker image inspect -f '{{.Id}}' "$IMAGE" 2>/dev/null || true)"; labels="$(effective_labels)"
    printf '%s\n' "$image_id" "$labels" "$SOCKET" "$ALLOW_SUDO" "$RUNNER_CPUS" "$RUNNER_MEMORY" "$RUNNER_PIDS_LIMIT" "$LOG_MAX_SIZE" "$LOG_MAX_FILE" "$API_VERSION" | sha256sum | awk '{print $1}'
}

write_state(){
    local state_dir="$1" repo_name="$2" runner_name="$3" container_name="$4" hash="$5" reg_token="$6"
    mkdir -p "$state_dir/work"; chown -R 1001:1001 "$state_dir/work"
    umask 077; printf '%s' "$reg_token" > "$state_dir/registration_token"; chown root:root "$state_dir/registration_token"; chmod 0600 "$state_dir/registration_token"
    cat > "$state_dir/metadata" <<EOF
profile=$PROFILE
mode=$MODE
owner=$OWNER
repo=$repo_name
runner_name=$runner_name
container_name=$container_name
image=$IMAGE
docker_socket=$SOCKET
allow_sudo=$ALLOW_SUDO
config_hash=$hash
EOF
    chmod 0600 "$state_dir/metadata"
}

legacy_metadata_profile(){ local legacy_dir="$1" metadata="$1/.chrisscriptbase-runner"; [[ -f "$metadata" ]] || return 1; awk -F= '$1=="profile" {sub(/^profile=/,""); print; exit}' "$metadata"; }
legacy_dir_allowed(){ local legacy_dir="$1" legacy_profile=""; [[ "$legacy_dir" != "$RUNNER_BASE/profiles/"* ]] || return 0; [[ "$PROFILE" != default ]] || return 0; legacy_profile="$(legacy_metadata_profile "$legacy_dir" 2>/dev/null || true)"; [[ "$legacy_profile" == "$PROFILE" ]]; }

list_action_runner_service_units(){
    command -v systemctl >/dev/null 2>&1 || return 0
    {
        systemctl list-unit-files --type=service --no-legend --no-pager 'actions.runner.*.service' 2>/dev/null | awk '{print $1}' || true
        systemctl list-units --type=service --all --no-legend --no-pager 'actions.runner.*.service' 2>/dev/null | awk '{print $1}' || true
        find /etc/systemd/system -maxdepth 1 -type f -name 'actions.runner.*.service' -printf '%f\n' 2>/dev/null || true
    } | awk '/^actions\.runner\..*\.service$/ && !seen[$0]++'
}

repo_from_service_unit(){
    local unit="$1" prefix="" rest="" host="" marker="" legacy_marker="" repo="" runner="" repo_sanitized="" profile_sanitized=""
    unit="${unit,,}"
    prefix="actions.runner.${OWNER,,}-"
    [[ "$unit" == "$prefix"*".service" ]] || return 1
    rest="${unit#"$prefix"}"
    rest="${rest%.service}"
    host="$(hostname -s | tr '[:upper:]' '[:lower:]')"
    marker=".${host}-"
    legacy_marker=".${host}"

    if [[ "$rest" == *"$marker"* ]]; then
        repo="${rest%%"$marker"*}"
        runner="${rest#*"$marker"}"
        [[ -n "$repo" && -n "$runner" ]] || return 1
        repo_sanitized="$(san "$repo")"
        if [[ "$PROFILE" == default ]]; then
            [[ "$runner" == "$repo_sanitized" ]] || return 1
        else
            profile_sanitized="$(san "$PROFILE")"
            [[ "$runner" == "${profile_sanitized}-${repo_sanitized}" || "$runner" == "$repo_sanitized" ]] || return 1
        fi
    elif [[ "$rest" == *"$legacy_marker" ]]; then
        # Najstarszy format nie zawiera suffixu runnera po hostname:
        # actions.runner.<owner>-<repo>.<host>.service
        repo="${rest%"$legacy_marker"}"
        [[ -n "$repo" ]] || return 1
    else
        return 1
    fi

    printf '%s\n' "$repo"
}

legacy_service_repos(){
    local unit="" repo_name=""
    while IFS= read -r unit; do
        [[ -n "$unit" ]] || continue
        repo_name="$(repo_from_service_unit "$unit" 2>/dev/null || true)"
        [[ -n "$repo_name" ]] && printf '%s\n' "$repo_name"
    done < <(list_action_runner_service_units)
}

runner_service_units_for_repo(){
    local requested="${1,,}" unit="" repo_name=""
    while IFS= read -r unit; do
        [[ -n "$unit" ]] || continue
        repo_name="$(repo_from_service_unit "$unit" 2>/dev/null || true)"
        [[ -n "$repo_name" && "$repo_name" == "$requested" ]] && printf '%s\n' "$unit"
    done < <(list_action_runner_service_units)
}

remove_runner_service_unit(){
    local unit="$1"
    [[ "$unit" == actions.runner.*.service ]] || return 1
    if command -v systemctl >/dev/null 2>&1; then
        systemctl stop "$unit" >/dev/null 2>&1 || true
        systemctl disable "$unit" >/dev/null 2>&1 || true
    fi
    rm -f "/etc/systemd/system/$unit"
    find /etc/systemd/system -type l -name "$unit" -delete 2>/dev/null || true
    if command -v systemctl >/dev/null 2>&1; then systemctl daemon-reload >/dev/null 2>&1 || true; fi
}

cleanup_legacy_service_units_for_repo(){
    local repo_name="$1" unit=""
    while IFS= read -r unit; do
        [[ -n "$unit" ]] || continue
        log "Usuwanie osieroconej usługi legacy: $unit"
        remove_runner_service_unit "$unit" || return 1
    done < <(runner_service_units_for_repo "$repo_name")
}

legacy_dirs(){ local repo_name="$1" default_dir="$RUNNER_BASE/$(san "$1")" profile_dir="$RUNNER_BASE/profiles/$(san "$PROFILE")/$(san "$1")"; [[ "$PROFILE" == default || ! -d "$profile_dir" ]] || echo "$profile_dir"; if [[ -d "$default_dir" ]]; then if legacy_dir_allowed "$default_dir"; then echo "$default_dir"; else warn "Pomijam niejednoznaczny legacy runner $default_dir dla profilu $PROFILE."; fi; fi; }

cleanup_legacy_dir(){
    local legacy_dir="$1" endpoint="$2" service_unit="" agent_name=""
    [[ -d "$legacy_dir" ]] || return 0
    [[ -f "$legacy_dir/.runner" || -f "$legacy_dir/.service" || -f "$legacy_dir/.chrisscriptbase-runner" ]] || return 0
    log "Migracja systemd -> Docker: $legacy_dir"
    agent_name="$(jq -r '.agentName // .name // empty' "$legacy_dir/.runner" 2>/dev/null || true)"
    if [[ -x "$legacy_dir/svc.sh" ]]; then (cd "$legacy_dir"; ./svc.sh stop >/dev/null 2>&1 || true; ./svc.sh uninstall >/dev/null 2>&1 || true); fi
    service_unit="$(head -1 "$legacy_dir/.service" 2>/dev/null | tr -d '\r' || true)"
    if [[ "$service_unit" == actions.runner.*.service ]]; then remove_runner_service_unit "$service_unit" || true; fi
    if [[ -n "$agent_name" ]] && ! remote_delete "$endpoint" "$agent_name"; then warn "Nie udało się usunąć legacy runnera $agent_name z GitHub. Zachowuję katalog do ponowienia."; return 1; fi
    rm -rf "$legacy_dir"
}

legacy_cleanup(){
    local repo_name="$1" legacy_dir="" endpoint="/repos/$OWNER/$1/actions/runners"
    while IFS= read -r legacy_dir; do
        [[ -n "$legacy_dir" ]] || continue
        cleanup_legacy_dir "$legacy_dir" "$endpoint" || return 1
    done < <(legacy_dirs "$repo_name")
    cleanup_legacy_service_units_for_repo "$repo_name"
}
legacy_org_dirs(){ local default_dir="$RUNNER_BASE/organization" profile_dir="$RUNNER_BASE/profiles/$(san "$PROFILE")/organization"; [[ "$PROFILE" == default || ! -d "$profile_dir" ]] || echo "$profile_dir"; if [[ -d "$default_dir" ]]; then if legacy_dir_allowed "$default_dir"; then echo "$default_dir"; else warn "Pomijam niejednoznaczny legacy organization runner $default_dir dla profilu $PROFILE."; fi; fi; }
legacy_org_cleanup(){ local legacy_dir=""; while IFS= read -r legacy_dir; do [[ -n "$legacy_dir" ]] || continue; cleanup_legacy_dir "$legacy_dir" "/orgs/$OWNER/actions/runners" || return 1; done < <(legacy_org_dirs); }

legacy_repos(){
    local root_dir="" runner_dir="" repo_name="" runner_url=""; local -a roots=("$RUNNER_BASE")
    [[ "$PROFILE" == default ]] || roots=("$RUNNER_BASE/profiles/$(san "$PROFILE")")
    for root_dir in "${roots[@]}"; do
        [[ -d "$root_dir" ]] || continue
        for runner_dir in "$root_dir"/*; do
            [[ -d "$runner_dir" ]] || continue
            case "$(basename "$runner_dir")" in docker|profiles|organization) continue ;; esac
            [[ -f "$runner_dir/.runner" || -f "$runner_dir/.chrisscriptbase-runner" ]] || continue
            repo_name="$(awk -F= '$1=="repo" {sub(/^repo=/,""); print; exit}' "$runner_dir/.chrisscriptbase-runner" 2>/dev/null || true)"
            if [[ -z "$repo_name" ]]; then runner_url="$(jq -r '.gitHubUrl // empty' "$runner_dir/.runner" 2>/dev/null || true)"; runner_url="${runner_url%/}"; repo_name="${runner_url##*/}"; fi
            [[ -z "$repo_name" ]] || echo "$repo_name"
        done
    done
}

container_repos(){
    local container_name="" repo_name="" prefix="github-runner-$(san "$PROFILE")-"
    while IFS= read -r container_name; do
        [[ -n "$container_name" ]] || continue
        repo_name="$(docker inspect -f '{{index .Config.Labels "com.chrisscriptbase.repository"}}' "$container_name" 2>/dev/null || true)"; [[ "$repo_name" != '<no value>' ]] || repo_name=""
        if [[ -z "$repo_name" && "$container_name" == "$prefix"* ]]; then repo_name="${container_name#"$prefix"}"; fi
        [[ -n "$repo_name" && "$repo_name" != org ]] && printf '%s\n' "$repo_name"
    done < <(docker ps -a --filter label=com.chrisscriptbase.github-runner=true --filter "label=com.chrisscriptbase.profile=$PROFILE" --format '{{.Names}}' 2>/dev/null || true)
}

local_repos(){ local repositories_root="$(profile_root)/repositories" state_dir="" repo_name=""; if [[ -d "$repositories_root" ]]; then for state_dir in "$repositories_root"/*; do [[ -f "$state_dir/metadata" ]] || continue; repo_name="$(meta_get "$state_dir/metadata" repo)"; [[ -z "$repo_name" ]] || echo "$repo_name"; done; fi; legacy_repos; legacy_service_repos; container_repos; }

container_socket_status(){ local container_name="$1" mounted=""; mounted="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/run/docker.sock"}}yes{{end}}{{end}}' "$container_name" 2>/dev/null || true)"; [[ "$mounted" == yes ]] && echo yes || echo no; }

status_repo_line(){
    local repo_name="$1" state_dir="$(repo_state "$1")" metadata="$state_dir/metadata" runner_name="$(repo_runner "$1")" container_name="$(repo_container "$1")"
    local container_state=missing github_state=missing busy=- socket=- docker_api=- cfg=- version=- row="" running=false stored_hash="" expected_hash=""
    if [[ -f "$metadata" ]]; then runner_name="$(meta_get "$metadata" runner_name)"; container_name="$(meta_get "$metadata" container_name)"; fi
    if docker inspect "$container_name" >/dev/null 2>&1; then
        running="$(docker inspect -f '{{.State.Running}}' "$container_name" 2>/dev/null || echo false)"; [[ "$running" == true ]] && container_state=running || container_state=stopped
        socket="$(container_socket_status "$container_name")"; version="$(container_runner_version "$container_name")"; [[ -n "$version" ]] || version=?
        if [[ "$running" == true && "$socket" == yes ]]; then docker_api="$(docker exec "$container_name" docker version --format '{{.Server.APIVersion}}' 2>/dev/null || true)"; [[ -n "$docker_api" ]] || docker_api=error; fi
    fi
    row="$(runner_lookup "$(runner_endpoint "$repo_name")" "$runner_name" 2>/dev/null || true)"; if [[ -n "$row" ]]; then github_state="$(cut -f2 <<< "$row")"; busy="$(cut -f3 <<< "$row")"; fi
    if [[ -f "$metadata" ]]; then stored_hash="$(meta_get "$metadata" config_hash)"; expected_hash="$(config_hash)"; [[ -n "$stored_hash" && "$stored_hash" == "$expected_hash" ]] && cfg=ok || cfg=drift; fi
    printf '%-24s %-10s %-9s %-5s %-6s %-10s %-10s %-8s\n' "$repo_name" "$container_state" "$github_state" "$busy" "$socket" "$docker_api" "$version" "$cfg"
}

status_org_line(){
    local state_dir="$(org_state)" metadata="$state_dir/metadata" runner_name="$(org_runner)" container_name="$(org_container)" row="" container_state=missing github_state=missing busy=- socket=- docker_api=- cfg=- version=- running=false stored_hash="" expected_hash=""
    if [[ -f "$metadata" ]]; then runner_name="$(meta_get "$metadata" runner_name)"; container_name="$(meta_get "$metadata" container_name)"; fi
    if docker inspect "$container_name" >/dev/null 2>&1; then running="$(docker inspect -f '{{.State.Running}}' "$container_name" 2>/dev/null || echo false)"; [[ "$running" == true ]] && container_state=running || container_state=stopped; socket="$(container_socket_status "$container_name")"; version="$(container_runner_version "$container_name")"; [[ -n "$version" ]] || version=?; if [[ "$running" == true && "$socket" == yes ]]; then docker_api="$(docker exec "$container_name" docker version --format '{{.Server.APIVersion}}' 2>/dev/null || true)"; [[ -n "$docker_api" ]] || docker_api=error; fi; fi
    row="$(runner_lookup "/orgs/$OWNER/actions/runners" "$runner_name" 2>/dev/null || true)"; if [[ -n "$row" ]]; then github_state="$(cut -f2 <<< "$row")"; busy="$(cut -f3 <<< "$row")"; fi
    if [[ -f "$metadata" ]]; then stored_hash="$(meta_get "$metadata" config_hash)"; expected_hash="$(config_hash)"; [[ -n "$stored_hash" && "$stored_hash" == "$expected_hash" ]] && cfg=ok || cfg=drift; fi
    printf '%-24s %-10s %-9s %-5s %-6s %-10s %-10s %-8s\n' '(organization)' "$container_state" "$github_state" "$busy" "$socket" "$docker_api" "$version" "$cfg"
}

status_profile(){
    local repo_name="" tmp=""; local -a repositories=()
    tmp="$(mktemp)"
    { echo "Profil: $PROFILE owner=$OWNER mode=$MODE"; printf '%-24s %-10s %-9s %-5s %-6s %-10s %-10s %-8s\n' Repo Kontener GitHub Zajęty Socket DockerAPI Runner Konfig.; if [[ "$MODE" == org ]]; then status_org_line; else mapfile -t repositories < <(local_repos | awk 'NF && !seen[tolower($0)]++'); if (( ${#repositories[@]} == 0 )); then echo 'Brak lokalnych runnerów.'; else for repo_name in "${repositories[@]}"; do status_repo_line "$repo_name"; done; fi; fi; } >"$tmp"
    if [[ "$UI" == dialog && -r /dev/tty && -w /dev/tty ]]; then dialog --clear --backtitle "ChrisScriptBase • GitHub Runner" --title " Status runnerów " --textbox "$tmp" 28 120 </dev/tty >/dev/tty 2>/dev/tty || true; else cat "$tmp"; fi
    rm -f "$tmp"
}


run_container(){
    local state_dir="$1" scope="$2" repo_name="$3" runner_name="$4" container_name="$5"
    local work_dir="$1/work" token_file="$1/registration_token" labels="" hash="" reg_token="" endpoint=""; local -a docker_args=()
    labels="$(effective_labels)"; hash="$(config_hash)"; reg_token="$(registration_token "$repo_name")" || return 1
    [[ -n "$reg_token" && "$reg_token" != null ]] || { warn "GitHub nie zwrócił registration token."; return 1; }
    write_state "$state_dir" "$repo_name" "$runner_name" "$container_name" "$hash" "$reg_token"; unset reg_token
    if docker inspect "$container_name" >/dev/null 2>&1; then docker rm -f "$container_name" >/dev/null; fi
    docker_args=(run -d --name "$container_name" --restart unless-stopped --label com.chrisscriptbase.github-runner=true --label "com.chrisscriptbase.profile=$PROFILE" --label "com.chrisscriptbase.scope=$scope" --label "com.chrisscriptbase.repository=$repo_name" --label "com.chrisscriptbase.runner-name=$runner_name" --log-opt "max-size=$LOG_MAX_SIZE" --log-opt "max-file=$LOG_MAX_FILE" -e "RUNNER_SCOPE=$scope" -e "GITHUB_OWNER=$OWNER" -e "GITHUB_REPOSITORY=$repo_name" -e "RUNNER_NAME=$runner_name" -e "RUNNER_LABELS=$labels" -e "RUNNER_WORKDIR=$work_dir" -e "RUNNER_ALLOW_SUDO=$ALLOW_SUDO" --mount "type=bind,src=$token_file,dst=/run/secrets/runner_registration_token,readonly" --mount "type=bind,src=$work_dir,dst=$work_dir")
    [[ -z "$RUNNER_CPUS" ]] || docker_args+=(--cpus "$RUNNER_CPUS")
    [[ -z "$RUNNER_MEMORY" ]] || docker_args+=(--memory "$RUNNER_MEMORY")
    [[ -z "$RUNNER_PIDS_LIMIT" ]] || docker_args+=(--pids-limit "$RUNNER_PIDS_LIMIT")
    if [[ "$SOCKET" == true ]]; then [[ -S /var/run/docker.sock ]] || die "Brak /var/run/docker.sock"; docker_args+=(--mount "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock"); fi
    docker_args+=("$IMAGE"); docker "${docker_args[@]}" >/dev/null
    endpoint="$(runner_endpoint "$repo_name")"; wait_runner_online "$endpoint" "$runner_name" "$container_name"
}

retire_existing_container(){
    local endpoint="$1" runner_name="$2" container_name="$3" was_running=false
    docker inspect "$container_name" >/dev/null 2>&1 || return 0
    was_running="$(docker inspect -f '{{.State.Running}}' "$container_name" 2>/dev/null || echo false)"
    [[ "$was_running" != true ]] || docker stop -t 30 "$container_name" >/dev/null 2>&1 || true
    if ! remote_delete_recreate "$endpoint" "$runner_name"; then
        warn "Rollback: wyrejestrowanie $runner_name nie powiodło się. Zachowuję stary kontener $container_name."
        [[ "$was_running" != true ]] || docker start "$container_name" >/dev/null 2>&1 || true
        return 1
    fi
    docker rm -f "$container_name" >/dev/null 2>&1 || true
}

existing_runner_healthy(){
    local state_dir="$1" repo_name="$2" runner_name="$3" container_name="$4" metadata="$1/metadata" expected_hash="" stored_hash="" row="" status=""
    [[ "$FORCE_RECREATE" == false ]] || return 1; [[ -f "$metadata" ]] || return 1; docker inspect "$container_name" >/dev/null 2>&1 || return 1
    [[ "$(docker inspect -f '{{.State.Running}}' "$container_name" 2>/dev/null || true)" == true ]] || return 1
    expected_hash="$(config_hash)"; stored_hash="$(meta_get "$metadata" config_hash)"; [[ -n "$stored_hash" && "$stored_hash" == "$expected_hash" ]] || return 1
    row="$(runner_lookup "$(runner_endpoint "$repo_name")" "$runner_name" 2>/dev/null || true)"; [[ -n "$row" ]] || return 1; status="$(cut -f2 <<< "$row")"; [[ "$status" == online ]]
}

install_repo(){
    local repo_name="$1" state_dir="$(repo_state "$1")" runner_name="$(repo_runner "$1")" container_name="$(repo_container "$1")"
    legacy_cleanup "$repo_name" || return 1
    if existing_runner_healthy "$state_dir" "$repo_name" "$runner_name" "$container_name"; then echo "Już działa i jest online: $container_name"; return 3; fi
    if docker inspect "$container_name" >/dev/null 2>&1; then log "Reconciliation: bezpiecznie odtwarzam $container_name"; retire_existing_container "$(runner_endpoint "$repo_name")" "$runner_name" "$container_name" || return 1; fi
    log "Instalacja Docker runnera $OWNER/$repo_name"; run_container "$state_dir" repo "$repo_name" "$runner_name" "$container_name"
}

repair_repo(){
    local repo_name="$1" state_dir="$(repo_state "$1")" metadata="$state_dir/metadata" runner_name="$(repo_runner "$1")" container_name="$(repo_container "$1")" row="" status="" running=false stored_hash="" expected_hash="" rc=0 saved_force="$FORCE_RECREATE"
    if [[ -f "$metadata" ]]; then runner_name="$(meta_get "$metadata" runner_name)"; container_name="$(meta_get "$metadata" container_name)"; fi
    if docker inspect "$container_name" >/dev/null 2>&1; then
        running="$(docker inspect -f '{{.State.Running}}' "$container_name" 2>/dev/null || echo false)"; row="$(runner_lookup "$(runner_endpoint "$repo_name")" "$runner_name" 2>/dev/null || true)"; [[ -z "$row" ]] || status="$(cut -f2 <<< "$row")"
        if [[ -f "$metadata" ]]; then stored_hash="$(meta_get "$metadata" config_hash)"; expected_hash="$(config_hash)"; fi
        if [[ "$running" == true && "$status" == online && -n "$stored_hash" && "$stored_hash" == "$expected_hash" ]]; then echo "Repair: $repo_name jest zdrowy"; return 3; fi
        if [[ -n "$stored_hash" && "$stored_hash" == "$expected_hash" ]]; then log "Repair: restartuję $container_name"; docker restart "$container_name" >/dev/null 2>&1 || docker start "$container_name" >/dev/null 2>&1 || true; if wait_runner_online "$(runner_endpoint "$repo_name")" "$runner_name" "$container_name"; then echo "Repair: naprawiono restartem $repo_name"; return 0; fi; fi
    else warn "Repair: brak kontenera $container_name; odtwarzam runner $repo_name."; fi
    FORCE_RECREATE=true; if install_repo "$repo_name"; then rc=0; else rc=$?; fi; FORCE_RECREATE="$saved_force"; return "$rc"
}

remove_repo(){
    local repo_name="$1" state_dir="$(repo_state "$1")" metadata_file="$(repo_state "$1")/metadata" runner_name="$(repo_runner "$1")" container_name="$(repo_container "$1")"
    legacy_cleanup "$repo_name" || return 1
    if [[ -f "$metadata_file" ]]; then runner_name="$(meta_get "$metadata_file" runner_name)"; container_name="$(meta_get "$metadata_file" container_name)"; fi
    if docker inspect "$container_name" >/dev/null 2>&1; then docker stop -t 30 "$container_name" >/dev/null 2>&1 || true; docker rm -f "$container_name" >/dev/null 2>&1 || true; fi
    if ! remote_delete "/repos/$OWNER/$repo_name/actions/runners" "$runner_name"; then warn "Nie udało się wyrejestrować $runner_name. Zachowuję state $state_dir."; return 1; fi
    rm -rf "$state_dir"
}

install_org(){
    local state_dir="$(org_state)" runner_name="$(org_runner)" container_name="$(org_container)"
    legacy_org_cleanup || return 1
    if existing_runner_healthy "$state_dir" "" "$runner_name" "$container_name"; then echo "Już działa i jest online: $container_name"; return 3; fi
    if docker inspect "$container_name" >/dev/null 2>&1; then retire_existing_container "/orgs/$OWNER/actions/runners" "$runner_name" "$container_name" || return 1; fi
    log "Instalacja Docker organization runnera $OWNER"; run_container "$state_dir" org "" "$runner_name" "$container_name"
}

repair_org(){
    local saved_force="$FORCE_RECREATE" rc=0 state_dir="$(org_state)" metadata="$state_dir/metadata" runner_name="$(org_runner)" container_name="$(org_container)" row="" status="" running=false stored_hash="" expected_hash=""
    if [[ -f "$metadata" ]]; then runner_name="$(meta_get "$metadata" runner_name)"; container_name="$(meta_get "$metadata" container_name)"; fi
    if docker inspect "$container_name" >/dev/null 2>&1; then running="$(docker inspect -f '{{.State.Running}}' "$container_name" 2>/dev/null || echo false)"; row="$(runner_lookup "/orgs/$OWNER/actions/runners" "$runner_name" 2>/dev/null || true)"; [[ -z "$row" ]] || status="$(cut -f2 <<< "$row")"; if [[ -f "$metadata" ]]; then stored_hash="$(meta_get "$metadata" config_hash)"; expected_hash="$(config_hash)"; fi; if [[ "$running" == true && "$status" == online && -n "$stored_hash" && "$stored_hash" == "$expected_hash" ]]; then return 3; fi; if [[ -n "$stored_hash" && "$stored_hash" == "$expected_hash" ]]; then docker restart "$container_name" >/dev/null 2>&1 || true; if wait_runner_online "/orgs/$OWNER/actions/runners" "$runner_name" "$container_name"; then return 0; fi; fi; fi
    FORCE_RECREATE=true; if install_org; then rc=0; else rc=$?; fi; FORCE_RECREATE="$saved_force"; return "$rc"
}

remove_org(){
    local state_dir="$(org_state)" metadata_file="$(org_state)/metadata" runner_name="$(org_runner)" container_name="$(org_container)"
    legacy_org_cleanup || return 1
    if [[ -f "$metadata_file" ]]; then runner_name="$(meta_get "$metadata_file" runner_name)"; container_name="$(meta_get "$metadata_file" container_name)"; fi
    if docker inspect "$container_name" >/dev/null 2>&1; then docker stop -t 30 "$container_name" >/dev/null 2>&1 || true; docker rm -f "$container_name" >/dev/null 2>&1 || true; fi
    if ! remote_delete "/orgs/$OWNER/actions/runners" "$runner_name"; then warn "Nie udało się wyrejestrować $runner_name. Zachowuję state $state_dir."; return 1; fi
    rm -rf "$state_dir"
}

normalize_repo(){ local value="$1"; [[ "$value" != *:* ]] || value="${value#*:}"; [[ "$value" != */* ]] || value="${value##*/}"; echo "$value"; }
repo_for_profile(){ local value="$1"; [[ "$value" == *:* ]] || return 0; [[ "${value%%:*}" == "$PROFILE" ]]; }
explicit_repos(){ local spec="" requested="" candidate=""; local -a available=("$@"); for spec in "${REPOS[@]}"; do repo_for_profile "$spec" || continue; requested="$(normalize_repo "$spec")"; for candidate in "${available[@]}"; do if [[ "${candidate,,}" == "${requested,,}" ]]; then echo "$candidate"; break; fi; done; done; }

ensure_dialog(){
    command -v dialog >/dev/null 2>&1 && return 0
    [[ -r /dev/tty && -w /dev/tty ]] || die "Brak interaktywnego terminala /dev/tty dla interfejsu dialog"
    echo "Instaluję wymagany pakiet 'dialog'..." >/dev/tty
    install_requirements dialog >/dev/tty 2>&1
    command -v dialog >/dev/null 2>&1 || die "Nie udało się zainstalować programu dialog"
}

runner_installation_detected(){
    if command -v docker >/dev/null 2>&1 && docker ps -a --filter label=com.chrisscriptbase.github-runner=true --format '{{.ID}}' 2>/dev/null | grep -q .; then
        return 0
    fi
    if [[ -d "$STATE_BASE" ]] && find "$STATE_BASE" -type f -name metadata -print -quit 2>/dev/null | grep -q .; then
        return 0
    fi
    if [[ -d "$RUNNER_BASE" ]] && find "$RUNNER_BASE" -maxdepth 5 -type f \( -name '.runner' -o -name '.chrisscriptbase-runner' \) -print -quit 2>/dev/null | grep -q .; then
        return 0
    fi
    if list_action_runner_service_units | grep -q .; then
        return 0
    fi
    return 1
}

gui_reset_action_flags(){
    ACTION="install"
    REINSTALL_ONLY=false
    FORCE_RECREATE=false
    FORCE_REMOTE_DELETE=false
    PREPARE_HOST=false
    STATUS_ONLY=false
    REPAIR_MODE=false
    CHECK_UPDATES=false
    UPDATE_RUNNER=false
    PURGE=false
}

gui_show_help(){
    local tmp=""
    tmp="$(mktemp)"; help >"$tmp"
    dialog --clear --backtitle "ChrisScriptBase • GitHub Runner" --title " Pomoc " --textbox "$tmp" 30 110 </dev/tty >/dev/tty 2>/dev/tty || true
    rm -f "$tmp"
}

gui_select_profiles(){
    local output="" rc=0 profile_name="" state="off"; local -a available=() items=() selected=()
    mapfile -t available < <(profiles | awk 'NF && !seen[$0]++')
    if (( ${#available[@]} == 0 )); then
        dialog --msgbox "Nie znaleziono profili GitHub w $GITCONFIG.\n\nSkonfiguruj profil w ~/.gitconfig lub przez GITHUB_OWNER/GITHUB_TOKEN." 11 82 </dev/tty >/dev/tty 2>/dev/tty || true
        return 1
    fi
    for profile_name in "${available[@]}"; do
        state=off
        if (( ${#PROFILES[@]} == 0 )); then [[ "$profile_name" == default ]] && state=on
        else
            local p=""; for p in "${PROFILES[@]}"; do [[ "$p" == "$profile_name" ]] && state=on; done
        fi
        items+=("$profile_name" "Profil GitHub" "$state")
    done
    output="$(exec 3>&1; dialog --clear --output-fd 3 --separate-output --backtitle "ChrisScriptBase • GitHub Runner" --title " Profile " --ok-label "Zapisz" --cancel-label "Anuluj" --checklist "Wybierz profile obsługiwane przez operacje GUI:" 22 90 14 "${items[@]}" </dev/tty >/dev/tty 2>/dev/tty)" || rc=$?
    (( rc == 0 )) || return "$rc"
    [[ -n "$output" ]] || { dialog --msgbox "Wybierz co najmniej jeden profil." 7 55 </dev/tty >/dev/tty 2>/dev/tty || true; return 1; }
    mapfile -t selected <<<"$output"
    PROFILES=("${selected[@]}")
}

gui_settings(){
    local output="" rc=0 item="" current_pkg="auto" current_repo="interactive"; local -a values=()
    local socket_state=off sudo_state=off public_state=off rebuild_state=off
    [[ "$SOCKET" == true ]] && socket_state=on
    [[ "$ALLOW_SUDO" == true ]] && sudo_state=on
    [[ "$INCLUDE_PUBLIC" == true ]] && public_state=on
    [[ "$REBUILD" == true ]] && rebuild_state=on

    output="$(exec 3>&1; dialog --clear --output-fd 3 --separate-output --backtitle "ChrisScriptBase • GitHub Runner" --title " Ustawienia bezpieczeństwa i obrazu " --ok-label "Dalej" --cancel-label "Anuluj" --checklist "Spacja zmienia wartość:" 18 100 10 \
      docker_socket "Udostępnij /var/run/docker.sock jobom" "$socket_state" \
      allow_sudo "NOPASSWD sudo wewnątrz kontenera" "$sudo_state" \
      include_public "Uwzględniaj publiczne repozytoria" "$public_state" \
      rebuild "Wymuś przebudowę obrazu przed instalacją" "$rebuild_state" \
      </dev/tty >/dev/tty 2>/dev/tty)" || rc=$?
    (( rc == 0 )) || return "$rc"
    SOCKET=false; ALLOW_SUDO=false; INCLUDE_PUBLIC=false; REBUILD=false
    while IFS= read -r item; do
        case "$item" in
            docker_socket) SOCKET=true ;;
            allow_sudo) ALLOW_SUDO=true ;;
            include_public) INCLUDE_PUBLIC=true ;;
            rebuild) REBUILD=true ;;
        esac
    done <<<"$output"

    output="$(exec 3>&1; dialog --clear --output-fd 3 --backtitle "ChrisScriptBase • GitHub Runner" --title " Zasoby i wersja runnera " --ok-label "Dalej" --cancel-label "Anuluj" --form "Puste CPU/RAM = bez dodatkowego limitu. Pusta wersja = latest." 20 96 10 \
      "CPU (--cpus):"        1 1 "${RUNNER_CPUS:-}"       1 28 24 0 \
      "RAM (--memory):"      2 1 "${RUNNER_MEMORY:-}"     2 28 24 0 \
      "PID limit:"           3 1 "${RUNNER_PIDS_LIMIT:-512}" 3 28 24 0 \
      "Runner version:"      4 1 "${RUNNER_VERSION:-}"    4 28 24 0 \
      "Log max-size:"        5 1 "${LOG_MAX_SIZE:-20m}"   5 28 24 0 \
      "Log max-file:"        6 1 "${LOG_MAX_FILE:-3}"     6 28 24 0 \
      </dev/tty >/dev/tty 2>/dev/tty)" || rc=$?
    (( rc == 0 )) || return "$rc"
    mapfile -t values <<<"$output"
    RUNNER_CPUS="${values[0]:-}"
    RUNNER_MEMORY="${values[1]:-}"
    RUNNER_PIDS_LIMIT="${values[2]:-512}"
    RUNNER_VERSION="${values[3]:-}"
    LOG_MAX_SIZE="${values[4]:-20m}"
    LOG_MAX_FILE="${values[5]:-3}"
    [[ "$RUNNER_PIDS_LIMIT" =~ ^[0-9]+$ ]] || { dialog --msgbox "PID limit musi być liczbą całkowitą." 7 60 </dev/tty >/dev/tty 2>/dev/tty || true; RUNNER_PIDS_LIMIT=512; }
    [[ "$LOG_MAX_FILE" =~ ^[0-9]+$ ]] || { dialog --msgbox "Log max-file musi być liczbą całkowitą." 7 60 </dev/tty >/dev/tty 2>/dev/tty || true; LOG_MAX_FILE=3; }

    current_pkg="${PKG_MANAGER:-auto}"
    output="$(exec 3>&1; dialog --clear --output-fd 3 --backtitle "ChrisScriptBase • GitHub Runner" --title " Manager pakietów " --ok-label "Dalej" --cancel-label "Anuluj" --radiolist "Automatyczne wykrywanie jest zalecane:" 17 76 7 \
      auto "Automatycznie wykryj" "$([[ "$current_pkg" == auto ]] && echo on || echo off)" \
      apt "Debian / Ubuntu" "$([[ "$current_pkg" == apt ]] && echo on || echo off)" \
      dnf "RHEL / Rocky / Alma / Fedora" "$([[ "$current_pkg" == dnf ]] && echo on || echo off)" \
      yum "Starsze systemy RHEL/CentOS" "$([[ "$current_pkg" == yum ]] && echo on || echo off)" \
      zypper "SUSE / openSUSE" "$([[ "$current_pkg" == zypper ]] && echo on || echo off)" \
      </dev/tty >/dev/tty 2>/dev/tty)" || rc=$?
    (( rc == 0 )) || return "$rc"
    [[ "$output" == auto ]] && PKG_MANAGER="" || PKG_MANAGER="$output"

    [[ "$SELECT_MODE" == all ]] && current_repo=all || current_repo=interactive
    output="$(exec 3>&1; dialog --clear --output-fd 3 --backtitle "ChrisScriptBase • GitHub Runner" --title " Wybór repozytoriów " --ok-label "Zapisz" --cancel-label "Anuluj" --radiolist "Sposób wyboru repozytoriów dla operacji:" 12 76 4 \
      interactive "Wybieraj repozytoria checklistą" "$([[ "$current_repo" == interactive ]] && echo on || echo off)" \
      all "Automatycznie wykonuj dla wszystkich dostępnych" "$([[ "$current_repo" == all ]] && echo on || echo off)" \
      </dev/tty >/dev/tty 2>/dev/tty)" || rc=$?
    (( rc == 0 )) || return "$rc"
    SELECT_MODE="$output"
}

gui_show_inventory(){
    local tmp="" p="" repo=""; local -a inventory_profiles=()
    docker_ready
    if (( ${#PROFILES[@]} > 0 )); then inventory_profiles=("${PROFILES[@]}"); else mapfile -t inventory_profiles < <(profiles | awk 'NF && !seen[$0]++'); fi
    tmp="$(mktemp)"
    {
        echo "INWENTARZ PROFILI / REPOZYTORIÓW"
        echo
        for p in "${inventory_profiles[@]}"; do
            echo "=== Profil: $p ==="
            if ! load_profile "$p"; then echo "Błąd wczytania profilu"; echo; continue; fi
            echo "Owner: $OWNER   mode: $MODE"
            echo "Repozytoria GitHub:"
            while IFS= read -r repo; do [[ -n "$repo" ]] && printf '  zdalne: %s\n' "$repo"; done < <(remote_repos 2>/dev/null || true)
            echo "Lokalne runnery:"
            while IFS= read -r repo; do [[ -n "$repo" ]] && printf '  lokalne: %s\n' "$repo"; done < <(local_repos 2>/dev/null | awk 'NF && !seen[$0]++')
            echo
        done
    } >"$tmp"
    dialog --clear --backtitle "ChrisScriptBase • GitHub Runner" --title " Profile i repozytoria " --textbox "$tmp" 30 110 </dev/tty >/dev/tty 2>/dev/tty || true
    rm -f "$tmp"
}

gui_choose_action(){
    local choice="" rc=0 message="" detected="nie"
    [[ "$UI" == dialog ]] || return 0
    [[ "$ACTION_EXPLICIT" == false ]] || return 0
    [[ -r /dev/tty && -w /dev/tty ]] || die "-g/--gui wymaga interaktywnego terminala"
    ensure_dialog
    runner_installation_detected && detected="tak"
    while true; do
        message="Pełny tryb GUI — wszystkie funkcje skryptu są dostępne z tego menu.\nIstniejąca instalacja runnerów: $detected\nProfile: $([[ ${#PROFILES[@]} -gt 0 ]] && printf '%s' "${PROFILES[*]}" || echo 'domyślny')\nRepo mode: ${SELECT_MODE:-interactive}\n\nWybierz operację:"
        choice="$(exec 3>&1; dialog --clear --output-fd 3 --backtitle "ChrisScriptBase • GitHub Self-Hosted Runner Manager" --title " Pełne zarządzanie runnerami " --ok-label "Wybierz" --cancel-label "Wyjście" --menu "$message" 31 112 18 \
          install "Instalacja       - dodaj runner / uzgodnij stan" \
          reinstall "Ponowna instalacja - bezpiecznie, z wycofaniem przy błędzie" \
          force_reinstall "Wymuś reinstalację - kontynuuj przy konflikcie 409/422" \
          status "Status            - kontenery, GitHub, socket i wersja" \
          repair "Napraw            - automatyczna naprawa runnerów" \
          check_updates "Sprawdź aktualizacje - porównaj wersję actions/runner" \
          update_runner "Aktualizuj runnera - najnowsza wersja + przebudowa + reinstall" \
          prepare_host "Przygotuj host    - zależności + Docker + raport" \
          settings "Ustawienia       - socket/sudo/CPU/RAM/wersja/repo" \
          profiles "Profile          - wybierz jeden lub wiele profili" \
          language "Język            - polski (aktywny)" \
          inventory "Profile i repozytoria - pokaż skonfigurowane zasoby" \
          uninstall "Odinstaluj        - usuń wybrane runnery" \
          help "Pomoc             - pełna dokumentacja opcji" \
          exit "Wyjście" \
          </dev/tty >/dev/tty 2>/dev/tty)" || rc=$?
        clear >/dev/tty 2>/dev/null || true
        (( rc == 0 )) || return 130
        case "$choice" in
            settings) gui_settings || true; continue ;;
            profiles) gui_select_profiles || true; continue ;;
            inventory) gui_show_inventory || true; continue ;;
            language) dialog --msgbox "Język interfejsu: polski (pl).\n\nPolski jest językiem domyślnym GUI i komunikatów użytkowych." 10 76 </dev/tty >/dev/tty 2>/dev/tty || true; continue ;;
            help) gui_show_help; continue ;;
            exit) return 130 ;;
            prepare_host) gui_reset_action_flags; PREPARE_HOST=true; return 0 ;;
            install) gui_reset_action_flags; ACTION=install; return 0 ;;
            reinstall) gui_reset_action_flags; ACTION=install; FORCE_RECREATE=true; REINSTALL_ONLY=true; return 0 ;;
            force_reinstall) gui_reset_action_flags; ACTION=install; FORCE_RECREATE=true; FORCE_REMOTE_DELETE=true; REINSTALL_ONLY=true; return 0 ;;
            status) gui_reset_action_flags; STATUS_ONLY=true; return 0 ;;
            repair) gui_reset_action_flags; ACTION=install; REPAIR_MODE=true; REINSTALL_ONLY=true; return 0 ;;
            check_updates) gui_reset_action_flags; CHECK_UPDATES=true; return 0 ;;
            update_runner) gui_reset_action_flags; ACTION=install; UPDATE_RUNNER=true; REINSTALL_ONLY=true; FORCE_RECREATE=true; REBUILD=true; return 0 ;;
            uninstall)
                gui_reset_action_flags; ACTION=uninstall
                if dialog --clear --backtitle "ChrisScriptBase • GitHub Runner" --title " Uninstall " --yes-label "Tak" --no-label "Nie" --yesno "Po usunięciu ostatniego runnera wykonać także PURGE stanu i lokalnego obrazu?" 9 88 </dev/tty >/dev/tty 2>/dev/tty; then PURGE=true; fi
                return 0
                ;;
        esac
    done
}

terminal_select(){
    local repo_name="" output="" rc=0 message="" action_label=Instalacja; local -a available=("$@") items=()
    [[ -r /dev/tty && -w /dev/tty ]] || die "-g/--gui wymaga interaktywnego terminala"; ensure_dialog
    if (( ${#available[@]} == 0 )); then dialog --clear --backtitle "ChrisScriptBase • GitHub Self-Hosted Runner Manager" --title " Brak repozytoriów " --msgbox "Nie znaleziono repozytoriów dostępnych dla profilu: $PROFILE" 9 70 </dev/tty >/dev/tty 2>/dev/tty || true; return 0; fi
    for repo_name in "${available[@]}"; do items+=("$repo_name" "" off); done
    if [[ "$ACTION" == uninstall ]]; then action_label=Odinstaluj; elif [[ "$REPAIR_MODE" == true ]]; then action_label=Napraw; elif [[ "$UPDATE_RUNNER" == true ]]; then action_label='Aktualizuj runnera'; elif [[ "$REINSTALL_ONLY" == true && "$FORCE_REMOTE_DELETE" == true ]]; then action_label='Wymuś reinstalację'; elif [[ "$REINSTALL_ONLY" == true ]]; then action_label='Ponowna instalacja'; fi
    message="Profil: $PROFILE\nOwner: $OWNER\nAkcja: $action_label\n\nSpacja: zaznacz/odznacz   Enter: zatwierdź"
    output="$(exec 3>&1; dialog --clear --colors --output-fd 3 --separate-output --backtitle "ChrisScriptBase • GitHub Self-Hosted Runner Manager" --title " Wybór repozytoriów " --ok-label "Zatwierdź" --cancel-label "Anuluj" --checklist "$message" 24 100 16 "${items[@]}" </dev/tty >/dev/tty 2>/dev/tty)" || rc=$?
    (( rc == 0 )) || return "$rc"; [[ -z "$output" ]] || printf '%s\n' "$output"
}

zenity_select(){
    local repo_name=""; local -a available=("$@") rows=()
    [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]] || die "Zenity wymaga X11/Wayland"
    command -v zenity >/dev/null 2>&1 || install_requirements zenity
    for repo_name in "${available[@]}"; do rows+=(FALSE "$repo_name"); done
    sudo -u "$CALLER" env HOME="$CALLER_HOME" DISPLAY="${DISPLAY:-}" WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}" XAUTHORITY="${XAUTHORITY:-$CALLER_HOME/.Xauthority}" zenity --list --checklist --title="GitHub Docker Runners" --column="Wybierz" --column="Repo" --separator=$'\n' "${rows[@]}" || true
}

interactive_repos(){ local -a available=("$@"); case "$UI" in zenity) zenity_select "${available[@]}" ;; dialog|auto) terminal_select "${available[@]}" ;; *) die "Nieznany UI: $UI" ;; esac; }

resolve(){
    local spec=""; local -a available=() chosen=()
    if [[ "$LIST_REPOS" == true ]]; then remote_repos; return 0; fi
    if [[ "$ACTION" == uninstall || "$REINSTALL_ONLY" == true ]]; then mapfile -t available < <(local_repos | awk 'NF && !seen[tolower($0)]++'); else mapfile -t available < <(remote_repos | awk 'NF && !seen[tolower($0)]++'); fi
    case "$SELECT_MODE" in
      all) chosen=("${available[@]}") ;;
      explicit) mapfile -t chosen < <(explicit_repos "${available[@]}"); if [[ "$ACTION" == uninstall && ${#chosen[@]} -eq 0 ]]; then for spec in "${REPOS[@]}"; do repo_for_profile "$spec" || continue; chosen+=("$(normalize_repo "$spec")"); done; fi ;;
      interactive) mapfile -t chosen < <(interactive_repos "${available[@]}") ;;
      *) die "Nieznany tryb wyboru repo: $SELECT_MODE" ;;
    esac
    printf '%s\n' "${chosen[@]}" | awk 'NF && !seen[tolower($0)]++'
}

process(){
    local repo_name="" rc=0 success=0 skipped=0 failed=0; local -a repositories=()
    if [[ "$STATUS_ONLY" == true ]]; then status_profile; return $?; fi
    if [[ "$MODE" == org ]]; then
        if [[ "$LIST_REPOS" == true ]]; then warn "MODE=org: --list-repos pominięte"; return 0; fi
        if [[ "$REPAIR_MODE" == true ]]; then if repair_org; then return 0; fi; rc=$?; (( rc == 3 )) && return 0; return "$rc"; fi
        if [[ "$ACTION" == install ]]; then if install_org; then return 0; fi; rc=$?; (( rc == 3 )) && return 0; return "$rc"; fi
        remove_org; return $?
    fi
    mapfile -t repositories < <(resolve)
    if [[ "$LIST_REPOS" == true ]]; then printf '%s\n' "${repositories[@]}"; return 0; fi
    if (( ${#repositories[@]} == 0 )); then warn "$PROFILE: brak repozytoriów"; return 0; fi
    for repo_name in "${repositories[@]}"; do
        if [[ "$REPAIR_MODE" == true ]]; then
            if repair_repo "$repo_name"; then ((success += 1)); else rc=$?; if (( rc == 3 )); then ((skipped += 1)); else ((failed += 1)); fi; fi
        elif [[ "$ACTION" == install ]]; then
            if install_repo "$repo_name"; then ((success += 1)); else rc=$?; if (( rc == 3 )); then ((skipped += 1)); else ((failed += 1)); fi; fi
        else
            if remove_repo "$repo_name"; then ((success += 1)); else ((failed += 1)); fi
        fi
    done
    echo "$PROFILE: sukces=$success pominięte=$skipped błędy=$failed"; (( failed == 0 ))
}

purge_if_empty(){
    [[ "$PURGE" == true ]] || return 0
    if docker ps -a --filter label=com.chrisscriptbase.github-runner=true --format '{{.ID}}' | grep -q .; then warn "--purge: istnieją jeszcze kontenery runnerów"; return 0; fi
    rm -rf "$STATE_BASE"; docker image rm "$IMAGE" >/dev/null 2>&1 || true
}

main(){
    local profile_name="" failed_profiles=0 latest=""
    args "$@"
    if [[ "$LIST_PROFILES" == true ]]; then caller_init; profiles | awk 'NF && !seen[$0]++'; return 0; fi
    [[ $EUID -eq 0 ]] || die "Uruchom przez sudo/root"
    ensure_dependencies
    caller_init

    # In GUI mode the action is selected after dependencies/caller initialization,
    # so Prepare Host, Settings, Profiles, Inventory and every operational action
    # can be started without any additional CLI flag.
    if [[ "$LIST_REPOS" == false ]] && ! gui_choose_action; then return 0; fi

    if [[ "$PREPARE_HOST" == true ]]; then
        ensure_prepare_host_packages
        docker_ready
        host_package_report || die "Host nie przeszedł końcowej weryfikacji pakietów."
        if [[ "$UI" == dialog ]]; then ui_message "Prepare Host" "Host przygotowany. Docker działa, a wymagane pakiety są zainstalowane."; else echo "Host przygotowany. Docker działa, a wymagane pakiety są zainstalowane."; fi
        return 0
    fi

    if [[ "$LIST_REPOS" == false ]]; then docker_ready; fi

    if [[ "$CHECK_UPDATES" == true ]]; then check_runner_updates; return $?; fi
    if [[ "$UPDATE_RUNNER" == true ]]; then latest="$(latest_runner_version)" || die "Nie udało się pobrać latest actions/runner"; RUNNER_VERSION="$latest"; REBUILD=true; FORCE_RECREATE=true; REINSTALL_ONLY=true; fi

    (( ${#PROFILES[@]} > 0 )) || PROFILES=(default)

    if [[ "$ACTION" == install && "$LIST_REPOS" == false && "$STATUS_ONLY" == false ]]; then build_image || die "Nie udało się zbudować obrazu runnera."; fi
    [[ "$SOCKET" != true ]] || warn "Dostęp do Docker socketa daje workflow kontrolę nad Docker daemonem hosta."
    [[ "$INCLUDE_PUBLIC" != true ]] || warn "--include-public: self-hosted runner w publicznym repo może wykonać niezaufany kod."
    for profile_name in "${PROFILES[@]}"; do load_profile "$profile_name"; auth; if ! process; then ((failed_profiles += 1)); fi; done
    [[ "$LIST_REPOS" == false ]] || return "$failed_profiles"
    [[ "$STATUS_ONLY" == true ]] || purge_if_empty
    [[ "$STATUS_ONLY" == true ]] || { echo; docker ps -a --filter label=com.chrisscriptbase.github-runner=true --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'; }
    return "$failed_profiles"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
