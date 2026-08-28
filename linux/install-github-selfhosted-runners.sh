#!/usr/bin/env bash
set -Eeuo pipefail

# ChrisScriptBase - GitHub Self-Hosted Runner Manager
# Single-file implementation: manager + generated Dockerfile + generated container entrypoint.

ACTION="install"
MODE_DEFAULT="${MODE:-user}"
OWNER_ENV="${GITHUB_OWNER:-}"
TOKEN_ENV="${GITHUB_TOKEN:-}"
RUNNER_BASE="${RUNNER_BASE:-/opt/github-runners}"
STATE_BASE="${DOCKER_STATE_BASE:-$RUNNER_BASE/docker}"
IMAGE="${DOCKER_IMAGE:-chrisscriptbase/github-actions-runner:local}"
RUNNER_VERSION="${RUNNER_VERSION:-}"
LABELS_DEFAULT="${CUSTOM_LABELS:-homelab}"
API_VERSION="${GITHUB_API_VERSION:-2026-03-10}"
SOCKET="${RUNNER_DOCKER_SOCKET:-false}"
ALLOW_SUDO="${RUNNER_ALLOW_SUDO:-false}"
INCLUDE_PUBLIC="${RUNNER_INCLUDE_PUBLIC:-false}"
REBUILD=false
FORCE_RECREATE=false
PURGE=false
LIST_PROFILES=false
LIST_REPOS=false
SELECT_MODE=""
UI="auto"
RUNNER_CPUS="${RUNNER_CPUS:-}"
RUNNER_MEMORY="${RUNNER_MEMORY:-}"
RUNNER_PIDS_LIMIT="${RUNNER_PIDS_LIMIT:-512}"
LOG_MAX_SIZE="${RUNNER_LOG_MAX_SIZE:-20m}"
LOG_MAX_FILE="${RUNNER_LOG_MAX_FILE:-3}"
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
  sudo bash install-github-selfhosted-runners.sh [--install|--uninstall] [opcje]

AKCJE
  --install                 Instalacja/reconciliation runnerów. Domyślne.
  --uninstall               Usuń wybrane runnery.
  --purge                   Z --uninstall usuń pusty stan i lokalny obraz.

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
  --gui|-GUI                Zenity lokalnie, dialog/whiptail przez SSH.
  --tui                     Wymuś dialog/whiptail.
  --zenity                  Wymuś Zenity.

DOCKER / RUNNER
  --docker-socket           Udostępnij /var/run/docker.sock jobom.
  --no-docker-socket        Nie udostępniaj Docker socketa. Domyślne.
  --allow-sudo              Runner ma NOPASSWD sudo wewnątrz kontenera.
  --no-sudo                 Usuń NOPASSWD sudo. Domyślne.
  --rebuild-image           Wymuś ponowny docker build.
  --force-recreate          Wymuś odtworzenie wybranych kontenerów.
  --cpus N                  Limit CPU kontenera, np. 2 lub 1.5.
  --memory SIZE             Limit RAM, np. 4g.
  --pids-limit N            Limit procesów. Domyślnie 512.
  --runner-version VER      Wersja actions/runner; puste = latest podczas build.

BEZPIECZEŃSTWO
  Długoterminowy GitHub PAT pozostaje wyłącznie na hoście.
  Kontener otrzymuje tylko krótkotrwały registration token.
  Publiczne repozytoria są domyślnie wyłączone.
  --docker-socket daje workflow praktycznie uprawnienia root na hoście Docker.

KONFIGURACJA ~/.gitconfig
  [github "home"]
      mode = user
      username = chmajster
      tokenBase64 = <TOKEN_BASE64>
      labels = homelab,linux

PRZYKŁAD
  sudo bash install-github-selfhosted-runners.sh \
    --profile home --select-repos --gui --docker-socket

DIAGNOSTYKA
  docker ps -a --filter label=com.chrisscriptbase.github-runner=true
  docker logs -f <nazwa-kontenera>
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
            --install) ACTION="install"; shift ;;
            --uninstall) ACTION="uninstall"; shift ;;
            --purge) PURGE=true; shift ;;
            --docker-socket) SOCKET=true; shift ;;
            --no-docker-socket) SOCKET=false; shift ;;
            --allow-sudo) ALLOW_SUDO=true; shift ;;
            --no-sudo) ALLOW_SUDO=false; shift ;;
            --include-public) INCLUDE_PUBLIC=true; shift ;;
            --private-only) INCLUDE_PUBLIC=false; shift ;;
            --rebuild-image) REBUILD=true; shift ;;
            --force-recreate) FORCE_RECREATE=true; shift ;;
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
            --gui|-GUI) UI="gui"; shift ;;
            --tui) UI="tui"; shift ;;
            --zenity) UI="zenity"; shift ;;
            --list-profiles) LIST_PROFILES=true; shift ;;
            --list-repos) LIST_REPOS=true; shift ;;
            *) die "Nieznana opcja: $1" ;;
        esac
    done
    [[ -n "$SELECT_MODE" ]] || SELECT_MODE="all"
    [[ "$PURGE" != true || "$ACTION" == uninstall ]] || die "--purge wymaga --uninstall"
    [[ "$UI" == auto || "$SELECT_MODE" == interactive ]] || die "--gui/-GUI, --tui i --zenity wymagają --select-repos"
    case "$SOCKET" in true|false) ;; *) die "RUNNER_DOCKER_SOCKET musi być true/false." ;; esac
    case "$ALLOW_SUDO" in true|false) ;; *) die "RUNNER_ALLOW_SUDO musi być true/false." ;; esac
    case "$INCLUDE_PUBLIC" in true|false) ;; *) die "RUNNER_INCLUDE_PUBLIC musi być true/false." ;; esac
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
    [[ -n "$OWNER" ]] || die "$PROFILE: brak ownera"
    [[ -n "$TOKEN" ]] || die "$PROFILE: brak tokenu"
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
    local method="$1" endpoint="$2"
    curl --silent --show-error --fail-with-body --location --request "$method" \
      --header "Accept: application/vnd.github+json" \
      --header "Authorization: Bearer $TOKEN" \
      --header "X-GitHub-Api-Version: $API_VERSION" \
      "https://api.github.com$endpoint"
}

auth(){ local login=""; login="$(api GET /user | jq -r '.login // empty')" || die "$PROFILE: token odrzucony"; [[ -n "$login" ]] || die "$PROFILE: GitHub API nie zwrócił loginu"; echo "Profil=$PROFILE owner=$OWNER mode=$MODE token-owner=$login labels=$(effective_labels)"; }

remote_repos(){
    local page=1 response="" count=0
    while true; do
        response="$(api GET "/user/repos?affiliation=owner&per_page=100&page=$page&sort=full_name")" || return 1
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

ensure_dependencies(){
    local command_name; local -a missing=()
    for command_name in curl jq git base64 getent awk sudo sha256sum; do command -v "$command_name" >/dev/null 2>&1 || missing+=("$command_name"); done
    if (( ${#missing[@]} > 0 )); then
        command -v apt-get >/dev/null 2>&1 || die "Brak wymaganych zależności: ${missing[*]}. Automatyczna instalacja obsługuje obecnie apt."
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y curl jq git coreutils gawk sudo ca-certificates
    fi
}

docker_ready(){
    if ! command -v docker >/dev/null 2>&1; then
        command -v apt-get >/dev/null 2>&1 || die "Docker nie jest zainstalowany i brak apt-get do automatycznej instalacji."
        log "Instalacja Docker Engine"
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
    fi
    if command -v systemctl >/dev/null 2>&1; then systemctl enable --now docker >/dev/null 2>&1 || true; fi
    docker info >/dev/null 2>&1 || die "Docker Engine nie działa"
}

render_docker_context(){
    local context_dir="$1"
    cat > "$context_dir/Dockerfile" <<'DOCKERFILE'
FROM ubuntu:24.04
ARG TARGETARCH
ARG RUNNER_VERSION=""
ENV DEBIAN_FRONTEND=noninteractive
ENV RUNNER_HOME=/actions-runner
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git jq sudo tar gzip docker.io \
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
ALLOW_SUDO="${RUNNER_ALLOW_SUDO:-false}"
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
    local context_dir=""; local -a build_args=(build --pull -t "$IMAGE")
    if [[ "$REBUILD" == false ]] && docker image inspect "$IMAGE" >/dev/null 2>&1; then return 0; fi
    context_dir="$(mktemp -d)"; render_docker_context "$context_dir"
    [[ -z "$RUNNER_VERSION" ]] || build_args+=(--build-arg "RUNNER_VERSION=$RUNNER_VERSION")
    build_args+=("$context_dir"); log "Budowanie obrazu $IMAGE"
    if ! docker "${build_args[@]}"; then rm -rf "$context_dir"; return 1; fi
    rm -rf "$context_dir"
}

meta_get(){ local file="$1" key="$2"; awk -F= -v key="$key" '$1==key {sub(/^[^=]*=/, ""); print; exit}' "$file"; }
registration_token(){ local endpoint="" response=""; if [[ "$MODE" == org ]]; then endpoint="/orgs/$OWNER/actions/runners/registration-token"; else endpoint="/repos/$OWNER/$1/actions/runners/registration-token"; fi; response="$(api POST "$endpoint")" || return 1; jq -r '.token // empty' <<< "$response"; }
runner_endpoint(){ local repo_name="${1:-}"; if [[ "$MODE" == org ]]; then echo "/orgs/$OWNER/actions/runners"; else echo "/repos/$OWNER/$repo_name/actions/runners"; fi; }

runner_lookup(){
    local endpoint="$1" runner_name="$2" page=1 response="" page_size=0 row=""
    while true; do
        response="$(api GET "$endpoint?per_page=100&page=$page")" || return 1
        row="$(jq -r --arg name "$runner_name" '.runners[]? | select(.name==$name) | [.id,.status,.busy] | @tsv' <<< "$response" | head -1)"
        [[ -z "$row" ]] || { printf '%s\n' "$row"; return 0; }
        page_size="$(jq '.runners | length' <<< "$response")"; (( page_size == 100 )) || return 3
        ((page += 1))
    done
}

remote_delete(){
    local endpoint="$1" runner_name="$2" row="" id="" rc=0
    row="$(runner_lookup "$endpoint" "$runner_name")" || rc=$?
    (( rc != 3 )) || return 0
    (( rc == 0 )) || return "$rc"
    id="${row%%$'\t'*}"; [[ -n "$id" ]] || return 0
    api DELETE "$endpoint/$id" >/dev/null
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
legacy_dirs(){ local repo_name="$1" default_dir="$RUNNER_BASE/$(san "$1")" profile_dir="$RUNNER_BASE/profiles/$(san "$PROFILE")/$(san "$1")"; [[ "$PROFILE" == default || ! -d "$profile_dir" ]] || echo "$profile_dir"; if [[ -d "$default_dir" ]]; then if legacy_dir_allowed "$default_dir"; then echo "$default_dir"; else warn "Pomijam niejednoznaczny legacy runner $default_dir dla profilu $PROFILE."; fi; fi; }

cleanup_legacy_dir(){
    local legacy_dir="$1" endpoint="$2" service_unit="" agent_name=""
    [[ -d "$legacy_dir" ]] || return 0
    [[ -f "$legacy_dir/.runner" || -f "$legacy_dir/.service" || -f "$legacy_dir/.chrisscriptbase-runner" ]] || return 0
    log "Migracja systemd -> Docker: $legacy_dir"
    agent_name="$(jq -r '.agentName // .name // empty' "$legacy_dir/.runner" 2>/dev/null || true)"
    if [[ -x "$legacy_dir/svc.sh" ]]; then (cd "$legacy_dir"; ./svc.sh stop >/dev/null 2>&1 || true; ./svc.sh uninstall >/dev/null 2>&1 || true); fi
    service_unit="$(head -1 "$legacy_dir/.service" 2>/dev/null | tr -d '\r' || true)"
    if [[ "$service_unit" == actions.runner.*.service ]] && command -v systemctl >/dev/null 2>&1; then
        systemctl stop "$service_unit" >/dev/null 2>&1 || true; systemctl disable "$service_unit" >/dev/null 2>&1 || true; rm -f "/etc/systemd/system/$service_unit"; find /etc/systemd/system -type l -name "$service_unit" -delete 2>/dev/null || true; systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    if [[ -n "$agent_name" ]] && ! remote_delete "$endpoint" "$agent_name"; then warn "Nie udało się usunąć legacy runnera $agent_name z GitHub. Zachowuję katalog do ponowienia."; return 1; fi
    rm -rf "$legacy_dir"
}

legacy_cleanup(){ local repo_name="$1" legacy_dir="" endpoint="/repos/$OWNER/$1/actions/runners"; while IFS= read -r legacy_dir; do [[ -n "$legacy_dir" ]] || continue; cleanup_legacy_dir "$legacy_dir" "$endpoint" || return 1; done < <(legacy_dirs "$repo_name"); }
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

local_repos(){ local repositories_root="$(profile_root)/repositories" state_dir="" repo_name=""; if [[ -d "$repositories_root" ]]; then for state_dir in "$repositories_root"/*; do [[ -f "$state_dir/metadata" ]] || continue; repo_name="$(meta_get "$state_dir/metadata" repo)"; [[ -z "$repo_name" ]] || echo "$repo_name"; done; fi; legacy_repos; }

run_container(){
    local state_dir="$1" scope="$2" repo_name="$3" runner_name="$4" container_name="$5"
    local work_dir="$1/work" token_file="$1/registration_token" labels="" hash="" reg_token="" endpoint=""; local -a docker_args=()
    labels="$(effective_labels)"; hash="$(config_hash)"; reg_token="$(registration_token "$repo_name")" || return 1
    [[ -n "$reg_token" && "$reg_token" != null ]] || { warn "GitHub nie zwrócił registration token."; return 1; }
    write_state "$state_dir" "$repo_name" "$runner_name" "$container_name" "$hash" "$reg_token"; unset reg_token
    if docker inspect "$container_name" >/dev/null 2>&1; then docker rm -f "$container_name" >/dev/null; fi
    docker_args=(run -d --name "$container_name" --restart unless-stopped --label com.chrisscriptbase.github-runner=true --label "com.chrisscriptbase.profile=$PROFILE" --log-opt "max-size=$LOG_MAX_SIZE" --log-opt "max-file=$LOG_MAX_FILE" -e "RUNNER_SCOPE=$scope" -e "GITHUB_OWNER=$OWNER" -e "GITHUB_REPOSITORY=$repo_name" -e "RUNNER_NAME=$runner_name" -e "RUNNER_LABELS=$labels" -e "RUNNER_WORKDIR=$work_dir" -e "RUNNER_ALLOW_SUDO=$ALLOW_SUDO" --mount "type=bind,src=$token_file,dst=/run/secrets/runner_registration_token,readonly" --mount "type=bind,src=$work_dir,dst=$work_dir")
    [[ -z "$RUNNER_CPUS" ]] || docker_args+=(--cpus "$RUNNER_CPUS")
    [[ -z "$RUNNER_MEMORY" ]] || docker_args+=(--memory "$RUNNER_MEMORY")
    [[ -z "$RUNNER_PIDS_LIMIT" ]] || docker_args+=(--pids-limit "$RUNNER_PIDS_LIMIT")
    if [[ "$SOCKET" == true ]]; then [[ -S /var/run/docker.sock ]] || die "Brak /var/run/docker.sock"; docker_args+=(--mount "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock"); fi
    docker_args+=("$IMAGE"); docker "${docker_args[@]}" >/dev/null
    endpoint="$(runner_endpoint "$repo_name")"; wait_runner_online "$endpoint" "$runner_name" "$container_name"
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
    if docker inspect "$container_name" >/dev/null 2>&1; then
        log "Reconciliation: odtwarzam $container_name"; docker rm -f "$container_name" >/dev/null 2>&1 || true
        remote_delete "$(runner_endpoint "$repo_name")" "$runner_name" || { warn "Nie udało się usunąć starej rejestracji $runner_name."; return 1; }
    fi
    log "Instalacja Docker runnera $OWNER/$repo_name"; run_container "$state_dir" repo "$repo_name" "$runner_name" "$container_name"
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
    if docker inspect "$container_name" >/dev/null 2>&1; then docker rm -f "$container_name" >/dev/null 2>&1 || true; remote_delete "/orgs/$OWNER/actions/runners" "$runner_name" || return 1; fi
    log "Instalacja Docker organization runnera $OWNER"; run_container "$state_dir" org "" "$runner_name" "$container_name"
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

terminal_select(){
    local repo_name="" output="" rc=0; local -a available=("$@") items=()
    [[ -t 0 && -t 1 ]] || die "--select-repos wymaga interaktywnego TTY"
    if ! command -v dialog >/dev/null 2>&1 && ! command -v whiptail >/dev/null 2>&1; then apt-get update; apt-get install -y dialog; fi
    for repo_name in "${available[@]}"; do items+=("$repo_name" "" off); done
    if command -v dialog >/dev/null 2>&1; then output="$(dialog --stdout --separate-output --checklist "Repozytoria" 24 100 16 "${items[@]}")" || rc=$?; clear || true; else output="$(whiptail --checklist "Repozytoria" 24 100 16 "${items[@]}" 3>&1 1>&2 2>&3)" || rc=$?; output="$(sed 's/" "/\n/g; s/^"//; s/"$//' <<< "$output")"; fi
    if (( rc == 0 )) && [[ -n "$output" ]]; then printf '%s\n' "$output"; fi
}

zenity_select(){
    local repo_name=""; local -a available=("$@") rows=()
    [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]] || die "Zenity wymaga X11/Wayland"
    command -v zenity >/dev/null 2>&1 || { apt-get update; apt-get install -y zenity; }
    for repo_name in "${available[@]}"; do rows+=(FALSE "$repo_name"); done
    sudo -u "$CALLER" env HOME="$CALLER_HOME" DISPLAY="${DISPLAY:-}" WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}" XAUTHORITY="${XAUTHORITY:-$CALLER_HOME/.Xauthority}" zenity --list --checklist --title="GitHub Docker Runners" --column="Wybierz" --column="Repo" --separator=$'\n' "${rows[@]}" || true
}

interactive_repos(){ local -a available=("$@"); case "$UI" in zenity) zenity_select "${available[@]}" ;; gui) if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then zenity_select "${available[@]}"; else terminal_select "${available[@]}"; fi ;; tui|auto) terminal_select "${available[@]}" ;; *) die "Nieznany UI: $UI" ;; esac; }

resolve(){
    local spec=""; local -a available=() chosen=()
    if [[ "$LIST_REPOS" == true ]]; then remote_repos; return 0; fi
    if [[ "$ACTION" == uninstall ]]; then mapfile -t available < <(local_repos | awk 'NF && !seen[tolower($0)]++'); else mapfile -t available < <(remote_repos | awk 'NF && !seen[tolower($0)]++'); fi
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
    if [[ "$MODE" == org ]]; then
        if [[ "$LIST_REPOS" == true ]]; then warn "MODE=org: --list-repos pominięte"; return 0; fi
        if [[ "$ACTION" == install ]]; then if install_org; then return 0; fi; rc=$?; (( rc == 3 )) && return 0; return "$rc"; fi
        remove_org; return $?
    fi
    mapfile -t repositories < <(resolve)
    if [[ "$LIST_REPOS" == true ]]; then printf '%s\n' "${repositories[@]}"; return 0; fi
    if (( ${#repositories[@]} == 0 )); then warn "$PROFILE: brak repozytoriów"; return 0; fi
    for repo_name in "${repositories[@]}"; do
        if [[ "$ACTION" == install ]]; then
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
    local profile_name="" failed_profiles=0
    args "$@"; caller_init
    if [[ "$LIST_PROFILES" == true ]]; then profiles | awk 'NF && !seen[$0]++'; return 0; fi
    [[ $EUID -eq 0 ]] || die "Uruchom przez sudo/root"
    ensure_dependencies
    if [[ "$LIST_REPOS" == false ]]; then docker_ready; fi
    (( ${#PROFILES[@]} > 0 )) || PROFILES=(default)
    if [[ "$ACTION" == install && "$LIST_REPOS" == false ]]; then build_image || die "Nie udało się zbudować obrazu runnera."; fi
    [[ "$SOCKET" != true ]] || warn "--docker-socket daje workflow kontrolę nad Docker daemonem hosta."
    [[ "$INCLUDE_PUBLIC" != true ]] || warn "--include-public: self-hosted runner w publicznym repo może wykonać niezaufany kod."
    for profile_name in "${PROFILES[@]}"; do load_profile "$profile_name"; auth; if ! process; then ((failed_profiles += 1)); fi; done
    [[ "$LIST_REPOS" == false ]] || return "$failed_profiles"
    purge_if_empty
    echo; docker ps -a --filter label=com.chrisscriptbase.github-runner=true --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
    return "$failed_profiles"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
