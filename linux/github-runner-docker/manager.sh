#!/usr/bin/env bash
set -Eeuo pipefail

ACTION="install"
MODE_DEFAULT="${MODE:-user}"
OWNER_ENV="${GITHUB_OWNER:-}"
TOKEN_ENV="${GITHUB_TOKEN:-}"
RUNNER_BASE="${RUNNER_BASE:-/opt/github-runners}"
STATE_BASE="${DOCKER_STATE_BASE:-$RUNNER_BASE/docker}"
IMAGE="${DOCKER_IMAGE:-chrisscriptbase/github-actions-runner:local}"
LABELS_DEFAULT="${CUSTOM_LABELS:-homelab,docker}"
API_VERSION="${GITHUB_API_VERSION:-2022-11-28}"
SOCKET="${RUNNER_DOCKER_SOCKET:-false}"
REBUILD=false
PURGE=false
LIST_PROFILES=false
LIST_REPOS=false
SELECT_MODE=""
UI="auto"
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

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTEXT="${RUNNER_DOCKER_CONTEXT:-$HERE}"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

warn() {
    echo "WARNING: $*" >&2
}

log() {
    echo
    echo "=== $* ==="
}

san() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_.-'
}

help() {
cat <<'EOF'
GitHub Self-Hosted Runner Manager (Docker)

Każdy runner repozytorium działa jako osobny kontener Docker z restart=unless-stopped.
Profile i tokenBase64 są czytane z ~/.gitconfig użytkownika wywołującego sudo.

Użycie:
  sudo bash install-github-selfhosted-runners.sh [--install|--uninstall] [opcje]

Opcje:
  -p, --profile NAME       profil; można powtórzyć
  --profiles A,B           kilka profili
  -r, --repo REPO          repo; można powtórzyć
  --repos A,B              kilka repo
  --all-repos              wszystkie repo (domyślne)
  --select-repos           interaktywny wybór
  --gui|-GUI               Zenity lokalnie albo dialog/whiptail przez SSH
  --tui                    wymuś dialog/whiptail
  --zenity                 wymuś Zenity; wymaga sesji graficznej
  --list-profiles          pokaż profile
  --list-repos             pokaż repo
  --docker-socket          udostępnij /var/run/docker.sock jobom
  --no-docker-socket       nie udostępniaj socketu (domyślne)
  --rebuild-image          przebuduj obraz runnera
  --purge                  przy uninstall usuń pusty stan i obraz

Przykład:
  sudo bash install-github-selfhosted-runners.sh \
    --profile home --select-repos --gui --docker-socket

Diagnostyka:
  docker ps -a --filter label=com.chrisscriptbase.github-runner=true
  docker logs -f <nazwa-kontenera>
EOF
}

append_csv() {
    local array_name="$1"
    local csv_value="$2"
    local item
    local -a parts=()
    local -n target_array="$array_name"

    IFS=',' read -r -a parts <<< "$csv_value"
    for item in "${parts[@]}"; do
        item="${item//[[:space:]]/}"
        if [[ -n "$item" ]]; then
            target_array+=("$item")
        fi
    done
}

set_selection_mode() {
    local requested="$1"
    if [[ -n "$SELECT_MODE" && "$SELECT_MODE" != "$requested" ]]; then
        die "Sprzeczne opcje wyboru repozytoriów."
    fi
    SELECT_MODE="$requested"
}

args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)
                help
                exit 0
                ;;
            --install)
                ACTION="install"
                shift
                ;;
            --uninstall)
                ACTION="uninstall"
                shift
                ;;
            --purge)
                PURGE=true
                shift
                ;;
            --docker-socket)
                SOCKET=true
                shift
                ;;
            --no-docker-socket)
                SOCKET=false
                shift
                ;;
            --rebuild-image)
                REBUILD=true
                shift
                ;;
            -p|--profile)
                [[ $# -ge 2 ]] || die "$1 wymaga nazwy profilu."
                PROFILES+=("$2")
                shift 2
                ;;
            --profiles)
                [[ $# -ge 2 ]] || die "$1 wymaga listy profili."
                append_csv PROFILES "$2"
                shift 2
                ;;
            -r|--repo)
                [[ $# -ge 2 ]] || die "$1 wymaga repozytorium."
                set_selection_mode "explicit"
                REPOS+=("$2")
                shift 2
                ;;
            --repos)
                [[ $# -ge 2 ]] || die "$1 wymaga listy repozytoriów."
                set_selection_mode "explicit"
                append_csv REPOS "$2"
                shift 2
                ;;
            --all-repos)
                set_selection_mode "all"
                shift
                ;;
            --select-repos)
                set_selection_mode "interactive"
                shift
                ;;
            --gui|-GUI)
                UI="gui"
                shift
                ;;
            --tui)
                UI="tui"
                shift
                ;;
            --zenity)
                UI="zenity"
                shift
                ;;
            --list-profiles)
                LIST_PROFILES=true
                shift
                ;;
            --list-repos)
                LIST_REPOS=true
                shift
                ;;
            *)
                die "Nieznana opcja: $1"
                ;;
        esac
    done

    if [[ -z "$SELECT_MODE" ]]; then
        SELECT_MODE="all"
    fi
    if [[ "$PURGE" == true && "$ACTION" != "uninstall" ]]; then
        die "--purge wymaga --uninstall"
    fi
    if [[ "$UI" != "auto" && "$SELECT_MODE" != "interactive" ]]; then
        die "--gui/-GUI, --tui i --zenity wymagają --select-repos"
    fi
}

caller_init() {
    if [[ -n "${SUDO_USER:-}" ]]; then
        CALLER="$SUDO_USER"
    else
        CALLER="$(id -un)"
    fi
    CALLER_HOME="$(getent passwd "$CALLER" | cut -d: -f6)"
    [[ -n "$CALLER_HOME" ]] || die "Nie można ustalić HOME użytkownika: $CALLER"
    GITCONFIG="$CALLER_HOME/.gitconfig"
}

cfg() {
    local key="$1"
    if [[ -f "$GITCONFIG" ]]; then
        git config --file "$GITCONFIG" --get "$key" 2>/dev/null || true
    fi
}

decode_token() {
    printf '%s' "$1" | base64 --decode 2>/dev/null
}

profiles() {
    local base_config=""
    base_config="${OWNER_ENV}${TOKEN_ENV}$(cfg github.username)$(cfg github.organization)$(cfg github.tokenBase64)"
    if [[ -n "$base_config" ]]; then
        echo "default"
    fi

    if [[ -f "$GITCONFIG" ]]; then
        git config --file "$GITCONFIG" --name-only \
            --get-regexp '^github\..+\.(username|organization|owner|tokenBase64|mode|labels)$' \
            2>/dev/null | awk -F. 'NF>=3 {print $2}' || true
    fi
}

load_profile() {
    local profile_name="$1"
    local prefix=""
    local encoded_token=""
    local configured_labels=""

    PROFILE="$profile_name"
    MODE="$MODE_DEFAULT"
    OWNER=""
    TOKEN=""
    LABELS="$LABELS_DEFAULT"

    if [[ "$PROFILE" != "default" ]]; then
        prefix="$PROFILE."
    fi

    MODE="$(cfg "github.${prefix}mode")"
    if [[ -z "$MODE" ]]; then
        MODE="$MODE_DEFAULT"
    fi

    if [[ "$PROFILE" == "default" ]]; then
        OWNER="$OWNER_ENV"
        TOKEN="$TOKEN_ENV"
    fi

    if [[ -z "$OWNER" ]]; then
        OWNER="$(cfg "github.${prefix}owner")"
    fi
    if [[ -z "$OWNER" ]]; then
        if [[ "$MODE" == "org" ]]; then
            OWNER="$(cfg "github.${prefix}organization")"
        else
            OWNER="$(cfg "github.${prefix}username")"
        fi
    fi

    if [[ -z "$TOKEN" ]]; then
        encoded_token="$(cfg "github.${prefix}tokenBase64")"
        if [[ -n "$encoded_token" ]]; then
            TOKEN="$(decode_token "$encoded_token")" || die "$PROFILE: nie można zdekodować tokenBase64"
        fi
    fi

    configured_labels="$(cfg "github.${prefix}labels")"
    if [[ -n "$configured_labels" ]]; then
        LABELS="$configured_labels"
    fi

    case "$MODE" in
        user|org) ;;
        *) die "$PROFILE: mode musi być user albo org" ;;
    esac
    [[ -n "$OWNER" ]] || die "$PROFILE: brak ownera"
    [[ -n "$TOKEN" ]] || die "$PROFILE: brak tokenu"
}

api() {
    local method="$1"
    local endpoint="$2"
    curl -fsSL -X "$method" \
        -H "Accept: application/vnd.github+json" \
        -H "Authorization: Bearer $TOKEN" \
        -H "X-GitHub-Api-Version: $API_VERSION" \
        "https://api.github.com$endpoint"
}

auth() {
    local login=""
    login="$(api GET /user | jq -r '.login // empty')" || die "$PROFILE: token odrzucony"
    [[ -n "$login" ]] || die "$PROFILE: GitHub API nie zwrócił loginu"
    echo "Profil=$PROFILE owner=$OWNER mode=$MODE token-owner=$login labels=$LABELS"
}

remote_repos() {
    local page=1
    local response=""
    local count=0

    while true; do
        response="$(api GET "/user/repos?affiliation=owner&per_page=100&page=$page&sort=full_name")" || return 1
        count="$(jq 'length' <<< "$response")"
        if (( count == 0 )); then
            break
        fi
        jq -r --arg owner "$OWNER" '
          .[]
          | select((.owner.login | ascii_downcase) == ($owner | ascii_downcase))
          | select(.archived == false)
          | .name
        ' <<< "$response"
        ((page += 1))
    done
}

profile_root() {
    if [[ "$PROFILE" == "default" ]]; then
        echo "$STATE_BASE/default"
    else
        echo "$STATE_BASE/profiles/$(san "$PROFILE")"
    fi
}

repo_state() {
    echo "$(profile_root)/repositories/$(san "$1")"
}

org_state() {
    echo "$(profile_root)/organization"
}

repo_runner() {
    local host=""
    host="$(hostname -s | tr '[:upper:]' '[:lower:]')"
    if [[ "$PROFILE" == "default" ]]; then
        echo "${host}-$(san "$1")"
    else
        echo "${host}-$(san "$PROFILE")-$(san "$1")"
    fi
}

org_runner() {
    local host=""
    host="$(hostname -s | tr '[:upper:]' '[:lower:]')"
    echo "${host}-$(san "$PROFILE")-$(san "$OWNER")"
}

repo_container() {
    echo "github-runner-$(san "$PROFILE")-$(san "$1")"
}

org_container() {
    echo "github-runner-$(san "$PROFILE")-org"
}

ensure_dependencies() {
    local command_name
    local -a missing=()
    for command_name in curl jq git base64 getent awk sudo; do
        if ! command -v "$command_name" >/dev/null 2>&1; then
            missing+=("$command_name")
        fi
    done
    if (( ${#missing[@]} > 0 )); then
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y \
            curl jq git coreutils gawk sudo ca-certificates
    fi
}

docker_ready() {
    if ! command -v docker >/dev/null 2>&1; then
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
    fi
    if command -v systemctl >/dev/null 2>&1; then
        systemctl enable --now docker >/dev/null 2>&1 || true
    fi
    docker info >/dev/null 2>&1 || die "Docker Engine nie działa"
}

build_image() {
    [[ -f "$CONTEXT/Dockerfile" ]] || die "Brak $CONTEXT/Dockerfile"
    [[ -f "$CONTEXT/entrypoint.sh" ]] || die "Brak $CONTEXT/entrypoint.sh"
    if [[ "$REBUILD" == false ]] && docker image inspect "$IMAGE" >/dev/null 2>&1; then
        return 0
    fi
    docker build --pull -t "$IMAGE" "$CONTEXT"
}

meta_get() {
    local file="$1"
    local key="$2"
    awk -F= -v key="$key" '$1==key {sub(/^[^=]*=/, ""); print; exit}' "$file"
}

write_state() {
    local state_dir="$1"
    local repo_name="$2"
    local runner_name="$3"
    local container_name="$4"

    mkdir -p "$state_dir/work"
    chown -R 1001:1001 "$state_dir/work"

    umask 077
    printf '%s' "$TOKEN" > "$state_dir/github_token"
    chown root:root "$state_dir/github_token"
    chmod 600 "$state_dir/github_token"

    cat > "$state_dir/metadata" <<EOF
profile=$PROFILE
mode=$MODE
owner=$OWNER
repo=$repo_name
runner_name=$runner_name
container_name=$container_name
image=$IMAGE
docker_socket=$SOCKET
EOF
    chmod 600 "$state_dir/metadata"
}

runner_id() {
    local endpoint="$1"
    local runner_name="$2"
    local page=1
    local response=""
    local id=""
    local page_size=0

    while true; do
        response="$(api GET "$endpoint?per_page=100&page=$page")" || return 1
        id="$(jq -r --arg name "$runner_name" '.runners[]? | select(.name==$name) | .id' <<< "$response" | head -1)"
        if [[ -n "$id" ]]; then
            echo "$id"
            return 0
        fi
        page_size="$(jq '.runners | length' <<< "$response")"
        if (( page_size < 100 )); then
            return 1
        fi
        ((page += 1))
    done
}

remote_delete() {
    local endpoint="$1"
    local runner_name="$2"
    local id=""
    id="$(runner_id "$endpoint" "$runner_name" 2>/dev/null)" || return 0
    if [[ -n "$id" ]]; then
        api DELETE "$endpoint/$id" >/dev/null || true
    fi
}

legacy_dirs() {
    local repo_name="$1"
    local default_dir=""
    local profile_dir=""
    default_dir="$RUNNER_BASE/$(san "$repo_name")"
    profile_dir="$RUNNER_BASE/profiles/$(san "$PROFILE")/$(san "$repo_name")"

    if [[ "$PROFILE" != "default" && -d "$profile_dir" ]]; then
        echo "$profile_dir"
    fi
    if [[ -d "$default_dir" ]]; then
        echo "$default_dir"
    fi
}

cleanup_legacy_dir() {
    local legacy_dir="$1"
    local endpoint="$2"
    local service_unit=""
    local agent_name=""

    [[ -d "$legacy_dir" ]] || return 0
    if [[ ! -f "$legacy_dir/.runner" && ! -f "$legacy_dir/.service" && ! -f "$legacy_dir/.chrisscriptbase-runner" ]]; then
        return 0
    fi

    log "Migracja systemd -> Docker: $legacy_dir"
    agent_name="$(jq -r '.agentName // .name // empty' "$legacy_dir/.runner" 2>/dev/null || true)"

    if [[ -x "$legacy_dir/svc.sh" ]]; then
        (
            cd "$legacy_dir"
            ./svc.sh stop >/dev/null 2>&1 || true
            ./svc.sh uninstall >/dev/null 2>&1 || true
        )
    fi

    service_unit="$(head -1 "$legacy_dir/.service" 2>/dev/null | tr -d '\r' || true)"
    if [[ "$service_unit" == actions.runner.*.service ]] && command -v systemctl >/dev/null 2>&1; then
        systemctl stop "$service_unit" >/dev/null 2>&1 || true
        systemctl disable "$service_unit" >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/$service_unit"
        find /etc/systemd/system -type l -name "$service_unit" -delete 2>/dev/null || true
        systemctl daemon-reload >/dev/null 2>&1 || true
    fi

    if [[ -n "$agent_name" ]]; then
        remote_delete "$endpoint" "$agent_name"
    fi
    rm -rf "$legacy_dir"
}

legacy_cleanup() {
    local repo_name="$1"
    local legacy_dir=""
    while IFS= read -r legacy_dir; do
        [[ -n "$legacy_dir" ]] || continue
        cleanup_legacy_dir "$legacy_dir" "/repos/$OWNER/$repo_name/actions/runners"
    done < <(legacy_dirs "$repo_name")
}

legacy_org_dirs() {
    local default_dir=""
    local profile_dir=""
    default_dir="$RUNNER_BASE/organization"
    profile_dir="$RUNNER_BASE/profiles/$(san "$PROFILE")/organization"

    if [[ "$PROFILE" != "default" && -d "$profile_dir" ]]; then
        echo "$profile_dir"
    fi
    if [[ -d "$default_dir" ]]; then
        echo "$default_dir"
    fi
}

legacy_org_cleanup() {
    local legacy_dir=""
    while IFS= read -r legacy_dir; do
        [[ -n "$legacy_dir" ]] || continue
        cleanup_legacy_dir "$legacy_dir" "/orgs/$OWNER/actions/runners"
    done < <(legacy_org_dirs)
}

legacy_repos() {
    local root_dir=""
    local runner_dir=""
    local repo_name=""
    local runner_url=""
    local -a roots=("$RUNNER_BASE")

    if [[ "$PROFILE" != "default" ]]; then
        roots=("$RUNNER_BASE/profiles/$(san "$PROFILE")" "$RUNNER_BASE")
    fi

    for root_dir in "${roots[@]}"; do
        [[ -d "$root_dir" ]] || continue
        for runner_dir in "$root_dir"/*; do
            [[ -d "$runner_dir" ]] || continue
            case "$(basename "$runner_dir")" in
                docker|profiles|organization) continue ;;
            esac
            if [[ ! -f "$runner_dir/.runner" && ! -f "$runner_dir/.chrisscriptbase-runner" ]]; then
                continue
            fi
            repo_name="$(awk -F= '$1=="repo" {sub(/^repo=/, ""); print; exit}' "$runner_dir/.chrisscriptbase-runner" 2>/dev/null || true)"
            if [[ -z "$repo_name" ]]; then
                runner_url="$(jq -r '.gitHubUrl // empty' "$runner_dir/.runner" 2>/dev/null || true)"
                runner_url="${runner_url%/}"
                repo_name="${runner_url##*/}"
            fi
            if [[ -n "$repo_name" ]]; then
                echo "$repo_name"
            fi
        done
    done
}

local_repos() {
    local repositories_root=""
    local state_dir=""
    local repo_name=""
    repositories_root="$(profile_root)/repositories"

    if [[ -d "$repositories_root" ]]; then
        for state_dir in "$repositories_root"/*; do
            [[ -f "$state_dir/metadata" ]] || continue
            repo_name="$(meta_get "$state_dir/metadata" repo)"
            if [[ -n "$repo_name" ]]; then
                echo "$repo_name"
            fi
        done
    fi
    legacy_repos
}

run_container() {
    local state_dir="$1"
    local scope="$2"
    local repo_name="$3"
    local runner_name="$4"
    local container_name="$5"
    local work_dir=""
    local token_file=""
    local -a docker_args=()

    work_dir="$state_dir/work"
    token_file="$state_dir/github_token"
    write_state "$state_dir" "$repo_name" "$runner_name" "$container_name"

    if docker inspect "$container_name" >/dev/null 2>&1; then
        docker rm -f "$container_name" >/dev/null
    fi

    docker_args=(
        run -d
        --name "$container_name"
        --restart unless-stopped
        --label com.chrisscriptbase.github-runner=true
        --label "com.chrisscriptbase.profile=$PROFILE"
        -e "RUNNER_SCOPE=$scope"
        -e "GITHUB_OWNER=$OWNER"
        -e "GITHUB_REPOSITORY=$repo_name"
        -e "RUNNER_NAME=$runner_name"
        -e "RUNNER_LABELS=$LABELS"
        -e "RUNNER_WORKDIR=$work_dir"
        -e "GITHUB_API_VERSION=$API_VERSION"
        --mount "type=bind,src=$token_file,dst=/run/secrets/github_token,readonly"
        --mount "type=bind,src=$work_dir,dst=$work_dir"
    )

    if [[ "$SOCKET" == true ]]; then
        [[ -S /var/run/docker.sock ]] || die "Brak /var/run/docker.sock"
        docker_args+=(--mount "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock")
    fi

    docker_args+=("$IMAGE")
    docker "${docker_args[@]}" >/dev/null
    sleep 2

    if [[ "$(docker inspect -f '{{.State.Running}}' "$container_name" 2>/dev/null || true)" != "true" ]]; then
        docker logs "$container_name" | tail -100 >&2 || true
        return 1
    fi
}

install_repo() {
    local repo_name="$1"
    local state_dir=""
    local runner_name=""
    local container_name=""
    state_dir="$(repo_state "$repo_name")"
    runner_name="$(repo_runner "$repo_name")"
    container_name="$(repo_container "$repo_name")"

    legacy_cleanup "$repo_name"
    if docker inspect "$container_name" >/dev/null 2>&1; then
        if [[ "$(docker inspect -f '{{.State.Running}}' "$container_name")" == "true" ]]; then
            echo "Już działa: $container_name"
            return 3
        fi
    fi

    log "Instalacja Docker runnera $OWNER/$repo_name"
    run_container "$state_dir" "repo" "$repo_name" "$runner_name" "$container_name"
}

remove_repo() {
    local repo_name="$1"
    local state_dir=""
    local metadata_file=""
    local runner_name=""
    local container_name=""
    state_dir="$(repo_state "$repo_name")"
    metadata_file="$state_dir/metadata"
    runner_name="$(repo_runner "$repo_name")"
    container_name="$(repo_container "$repo_name")"

    legacy_cleanup "$repo_name"
    if [[ -f "$metadata_file" ]]; then
        runner_name="$(meta_get "$metadata_file" runner_name)"
        container_name="$(meta_get "$metadata_file" container_name)"
    fi

    if docker inspect "$container_name" >/dev/null 2>&1; then
        docker stop -t 30 "$container_name" >/dev/null 2>&1 || true
        docker rm -f "$container_name" >/dev/null 2>&1 || true
    fi
    remote_delete "/repos/$OWNER/$repo_name/actions/runners" "$runner_name"
    rm -rf "$state_dir"
}

install_org() {
    local state_dir=""
    local runner_name=""
    local container_name=""
    state_dir="$(org_state)"
    runner_name="$(org_runner)"
    container_name="$(org_container)"

    legacy_org_cleanup
    if docker inspect "$container_name" >/dev/null 2>&1; then
        if [[ "$(docker inspect -f '{{.State.Running}}' "$container_name")" == "true" ]]; then
            echo "Już działa: $container_name"
            return 3
        fi
    fi

    log "Instalacja Docker organization runnera $OWNER"
    run_container "$state_dir" "org" "" "$runner_name" "$container_name"
}

remove_org() {
    local state_dir=""
    local metadata_file=""
    local runner_name=""
    local container_name=""
    state_dir="$(org_state)"
    metadata_file="$state_dir/metadata"
    runner_name="$(org_runner)"
    container_name="$(org_container)"

    legacy_org_cleanup
    if [[ -f "$metadata_file" ]]; then
        runner_name="$(meta_get "$metadata_file" runner_name)"
        container_name="$(meta_get "$metadata_file" container_name)"
    fi

    if docker inspect "$container_name" >/dev/null 2>&1; then
        docker stop -t 30 "$container_name" >/dev/null 2>&1 || true
        docker rm -f "$container_name" >/dev/null 2>&1 || true
    fi
    remote_delete "/orgs/$OWNER/actions/runners" "$runner_name"
    rm -rf "$state_dir"
}

normalize_repo() {
    local value="$1"
    if [[ "$value" == *:* ]]; then
        value="${value#*:}"
    fi
    if [[ "$value" == */* ]]; then
        value="${value##*/}"
    fi
    echo "$value"
}

repo_for_profile() {
    local value="$1"
    if [[ "$value" != *:* ]]; then
        return 0
    fi
    [[ "${value%%:*}" == "$PROFILE" ]]
}

explicit_repos() {
    local spec=""
    local requested=""
    local candidate=""
    local -a available=("$@")

    for spec in "${REPOS[@]}"; do
        repo_for_profile "$spec" || continue
        requested="$(normalize_repo "$spec")"
        for candidate in "${available[@]}"; do
            if [[ "${candidate,,}" == "${requested,,}" ]]; then
                echo "$candidate"
                break
            fi
        done
    done
}

terminal_select() {
    local repo_name=""
    local output=""
    local rc=0
    local -a available=("$@")
    local -a items=()

    [[ -t 0 && -t 1 ]] || die "--select-repos wymaga interaktywnego TTY"
    if ! command -v dialog >/dev/null 2>&1 && ! command -v whiptail >/dev/null 2>&1; then
        apt-get update
        apt-get install -y dialog
    fi

    for repo_name in "${available[@]}"; do
        items+=("$repo_name" "" off)
    done

    if command -v dialog >/dev/null 2>&1; then
        output="$(dialog --stdout --separate-output --checklist "Repozytoria" 24 100 16 "${items[@]}")" || rc=$?
        clear || true
    else
        output="$(whiptail --checklist "Repozytoria" 24 100 16 "${items[@]}" 3>&1 1>&2 2>&3)" || rc=$?
        output="$(sed 's/" "/\n/g; s/^"//; s/"$//' <<< "$output")"
    fi

    if (( rc == 0 )) && [[ -n "$output" ]]; then
        printf '%s\n' "$output"
    fi
}

zenity_select() {
    local repo_name=""
    local -a available=("$@")
    local -a rows=()

    [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]] || die "Zenity wymaga X11/Wayland"
    command -v zenity >/dev/null 2>&1 || die "Brak zenity"
    for repo_name in "${available[@]}"; do
        rows+=(FALSE "$repo_name")
    done
    sudo -u "$CALLER" env \
        HOME="$CALLER_HOME" \
        DISPLAY="${DISPLAY:-}" \
        WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}" \
        XAUTHORITY="${XAUTHORITY:-$CALLER_HOME/.Xauthority}" \
        zenity --list --checklist \
        --title="GitHub Docker Runners" \
        --column="Wybierz" --column="Repo" \
        --separator=$'\n' "${rows[@]}" || true
}

interactive_repos() {
    local -a available=("$@")
    case "$UI" in
        zenity)
            zenity_select "${available[@]}"
            ;;
        gui)
            if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v zenity >/dev/null 2>&1; then
                zenity_select "${available[@]}"
            else
                terminal_select "${available[@]}"
            fi
            ;;
        tui|auto)
            terminal_select "${available[@]}"
            ;;
        *)
            die "Nieznany UI: $UI"
            ;;
    esac
}

resolve() {
    local spec=""
    local -a available=()
    local -a chosen=()

    if [[ "$LIST_REPOS" == true ]]; then
        remote_repos
        return 0
    fi

    if [[ "$ACTION" == "uninstall" ]]; then
        mapfile -t available < <(local_repos)
    else
        mapfile -t available < <(remote_repos)
    fi

    case "$SELECT_MODE" in
        all)
            chosen=("${available[@]}")
            ;;
        explicit)
            mapfile -t chosen < <(explicit_repos "${available[@]}")
            if [[ "$ACTION" == "uninstall" && ${#chosen[@]} -eq 0 ]]; then
                for spec in "${REPOS[@]}"; do
                    if repo_for_profile "$spec"; then
                        chosen+=("$(normalize_repo "$spec")")
                    fi
                done
            fi
            ;;
        interactive)
            mapfile -t chosen < <(interactive_repos "${available[@]}")
            ;;
        *)
            die "Nieznany tryb wyboru repo: $SELECT_MODE"
            ;;
    esac

    printf '%s\n' "${chosen[@]}"
}

process() {
    local repo_name=""
    local rc=0
    local success=0
    local skipped=0
    local failed=0
    local -a repositories=()

    if [[ "$MODE" == "org" ]]; then
        if [[ "$LIST_REPOS" == true ]]; then
            warn "MODE=org: --list-repos pominięte"
            return 0
        fi
        if [[ "$ACTION" == "install" ]]; then
            install_org
        else
            remove_org
        fi
        return $?
    fi

    mapfile -t repositories < <(resolve)
    if [[ "$LIST_REPOS" == true ]]; then
        printf '%s\n' "${repositories[@]}"
        return 0
    fi
    if (( ${#repositories[@]} == 0 )); then
        warn "$PROFILE: brak repozytoriów"
        return 0
    fi

    for repo_name in "${repositories[@]}"; do
        if [[ "$ACTION" == "install" ]]; then
            if install_repo "$repo_name"; then
                ((success += 1))
            else
                rc=$?
                if (( rc == 3 )); then
                    ((skipped += 1))
                else
                    ((failed += 1))
                fi
            fi
        else
            if remove_repo "$repo_name"; then
                ((success += 1))
            else
                ((failed += 1))
            fi
        fi
    done

    echo "$PROFILE: sukces=$success pominięte=$skipped błędy=$failed"
    (( failed == 0 ))
}

purge_if_empty() {
    [[ "$PURGE" == true ]] || return 0
    if docker ps -a --filter label=com.chrisscriptbase.github-runner=true --format '{{.ID}}' | grep -q .; then
        warn "--purge: istnieją jeszcze kontenery runnerów"
        return 0
    fi
    rm -rf "$STATE_BASE"
    docker image rm "$IMAGE" >/dev/null 2>&1 || true
}

main() {
    local profile_name=""
    local failed_profiles=0

    args "$@"
    caller_init

    if [[ "$LIST_PROFILES" == true ]]; then
        profiles | awk 'NF && !seen[$0]++'
        return 0
    fi

    [[ $EUID -eq 0 ]] || die "Uruchom przez sudo/root"
    ensure_dependencies

    if [[ "$LIST_REPOS" == false ]]; then
        docker_ready
    fi

    if (( ${#PROFILES[@]} == 0 )); then
        PROFILES=(default)
    fi

    if [[ "$ACTION" == "install" && "$LIST_REPOS" == false ]]; then
        build_image
    fi

    for profile_name in "${PROFILES[@]}"; do
        load_profile "$profile_name"
        auth
        if ! process; then
            ((failed_profiles += 1))
        fi
    done

    if [[ "$LIST_REPOS" == true ]]; then
        return "$failed_profiles"
    fi

    purge_if_empty
    docker ps -a --filter label=com.chrisscriptbase.github-runner=true \
        --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
    return "$failed_profiles"
}

main "$@"
