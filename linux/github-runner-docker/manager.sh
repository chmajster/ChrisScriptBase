#!/usr/bin/env bash
set -Eeuo pipefail

ACTION=install
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
UI=auto
PROFILES=()
REPOS=()

PROFILE=default
MODE="$MODE_DEFAULT"
OWNER=""
TOKEN=""
LABELS="$LABELS_DEFAULT"
CALLER=""
CALLER_HOME=""
GITCONFIG=""

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTEXT="${RUNNER_DOCKER_CONTEXT:-$HERE}"

die(){ echo "ERROR: $*" >&2; exit 1; }
warn(){ echo "WARNING: $*" >&2; }
log(){ echo; echo "=== $* ==="; }
san(){ printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_.-'; }

help(){
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
  --tui                    dialog/whiptail
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

csv(){
  local -n a="$1"; local x; IFS=',' read -ra xs <<<"$2"
  for x in "${xs[@]}"; do x="${x//[[:space:]]/}"; [[ -n "$x" ]] && a+=("$x"); done
}
selmode(){
  [[ -z "$SELECT_MODE" || "$SELECT_MODE" == "$1" ]] || die "Sprzeczne opcje wyboru repo."
  SELECT_MODE="$1"
}
args(){
  while (($#)); do
    case "$1" in
      -h|--help) help; exit;;
      --install) ACTION=install; shift;;
      --uninstall) ACTION=uninstall; shift;;
      --purge) PURGE=true; shift;;
      --docker-socket) SOCKET=true; shift;;
      --no-docker-socket) SOCKET=false; shift;;
      --rebuild-image) REBUILD=true; shift;;
      -p|--profile) PROFILES+=("${2:?brak profilu}"); shift 2;;
      --profiles) csv PROFILES "${2:?brak profili}"; shift 2;;
      -r|--repo) selmode explicit; REPOS+=("${2:?brak repo}"); shift 2;;
      --repos) selmode explicit; csv REPOS "${2:?brak repo}"; shift 2;;
      --all-repos) selmode all; shift;;
      --select-repos) selmode interactive; shift;;
      --gui|-GUI) UI=gui; shift;;
      --tui) UI=tui; shift;;
      --list-profiles) LIST_PROFILES=true; shift;;
      --list-repos) LIST_REPOS=true; shift;;
      *) die "Nieznana opcja: $1";;
    esac
  done
  [[ -n "$SELECT_MODE" ]] || SELECT_MODE=all
  [[ "$PURGE" == false || "$ACTION" == uninstall ]] || die "--purge wymaga --uninstall"
}

caller_init(){
  CALLER="${SUDO_USER:-$(id -un)}"; [[ "$CALLER" != root || -z "${SUDO_USER:-}" ]] || CALLER=root
  CALLER_HOME="$(getent passwd "$CALLER" | cut -d: -f6)"
  [[ -n "$CALLER_HOME" ]] || die "Nie można ustalić HOME: $CALLER"
  GITCONFIG="$CALLER_HOME/.gitconfig"
}
cfg(){ [[ -f "$GITCONFIG" ]] && git config --file "$GITCONFIG" --get "$1" 2>/dev/null || true; }
decode(){ printf '%s' "$1" | base64 --decode 2>/dev/null; }

profiles(){
  { [[ -n "$OWNER_ENV$TOKEN_ENV$(cfg github.username)$(cfg github.organization)$(cfg github.tokenBase64)" ]] && echo default || true
    [[ -f "$GITCONFIG" ]] && git config --file "$GITCONFIG" --name-only \
      --get-regexp '^github\..+\.(username|organization|owner|tokenBase64|mode|labels)$' 2>/dev/null |
      awk -F. 'NF>=3{print $2}' || true
  } | awk 'NF&&!s[$0]++'
}
load_profile(){
  PROFILE="$1"; MODE="$MODE_DEFAULT"; OWNER=""; TOKEN=""; LABELS="$LABELS_DEFAULT"
  local p="" b=""
  [[ "$PROFILE" == default ]] || p="$PROFILE."
  MODE="$(cfg "github.${p}mode")"; [[ -n "$MODE" ]] || MODE="$MODE_DEFAULT"
  if [[ "$PROFILE" == default ]]; then OWNER="$OWNER_ENV"; TOKEN="$TOKEN_ENV"; fi
  [[ -n "$OWNER" ]] || OWNER="$(cfg "github.${p}owner")"
  if [[ -z "$OWNER" ]]; then
    [[ "$MODE" == org ]] && OWNER="$(cfg "github.${p}organization")" || OWNER="$(cfg "github.${p}username")"
  fi
  if [[ -z "$TOKEN" ]]; then b="$(cfg "github.${p}tokenBase64")"; [[ -z "$b" ]] || TOKEN="$(decode "$b")"; fi
  b="$(cfg "github.${p}labels")"; [[ -n "$b" ]] && LABELS="$b"
  [[ "$MODE" == user || "$MODE" == org ]] || die "$PROFILE: mode musi być user albo org"
  [[ -n "$OWNER" && -n "$TOKEN" ]] || die "$PROFILE: brak ownera lub tokenu"
}

api(){
  local m="$1" e="$2"
  curl -fsSL -X "$m" -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer $TOKEN" -H "X-GitHub-Api-Version: $API_VERSION" \
    "https://api.github.com$e"
}
auth(){
  local who; who="$(api GET /user | jq -r '.login//empty')" || die "$PROFILE: token odrzucony"
  echo "Profil=$PROFILE owner=$OWNER mode=$MODE token-owner=$who labels=$LABELS"
}
remote_repos(){
  local p=1 r n
  while :; do
    r="$(api GET "/user/repos?affiliation=owner&per_page=100&page=$p&sort=full_name")" || return 1
    n="$(jq length <<<"$r")"; ((n)) || break
    jq -r --arg o "$OWNER" '.[]|select((.owner.login|ascii_downcase)==($o|ascii_downcase) and .archived==false)|.name' <<<"$r"
    ((p++))
  done
}

root(){ [[ "$PROFILE" == default ]] && echo "$STATE_BASE/default" || echo "$STATE_BASE/profiles/$(san "$PROFILE")"; }
repo_state(){ echo "$(root)/repositories/$(san "$1")"; }
org_state(){ echo "$(root)/organization"; }
repo_runner(){ local h="$(hostname -s | tr A-Z a-z)"; [[ "$PROFILE" == default ]] && echo "${h}-$(san "$1")" || echo "${h}-$(san "$PROFILE")-$(san "$1")"; }
org_runner(){ echo "$(hostname -s | tr A-Z a-z)-$(san "$PROFILE")-$(san "$OWNER")"; }
repo_container(){ echo "github-runner-$(san "$PROFILE")-$(san "$1")"; }
org_container(){ echo "github-runner-$(san "$PROFILE")-org"; }

deps(){
  local c miss=()
  for c in curl jq git base64 getent awk sudo; do command -v "$c" >/dev/null || miss+=("$c"); done
  ((${#miss[@]}==0)) || { apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y curl jq git coreutils gawk sudo ca-certificates; }
}
docker_ready(){
  command -v docker >/dev/null || { apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io; }
  command -v systemctl >/dev/null && systemctl enable --now docker >/dev/null 2>&1 || true
  docker info >/dev/null 2>&1 || die "Docker Engine nie działa"
}
build_image(){
  [[ -f "$CONTEXT/Dockerfile" && -f "$CONTEXT/entrypoint.sh" ]] || die "Brak plików obrazu w $CONTEXT"
  if [[ "$REBUILD" == false ]] && docker image inspect "$IMAGE" >/dev/null 2>&1; then return 0; fi
  docker build --pull -t "$IMAGE" "$CONTEXT"
}

meta_get(){ awk -F= -v k="$2" '$1==k{sub(/^[^=]*=/,"");print;exit}' "$1"; }
write_state(){
  local d="$1" repo="$2" rn="$3" cn="$4"
  mkdir -p "$d/work"; chown -R 1001:1001 "$d/work"
  umask 077; printf '%s' "$TOKEN" >"$d/github_token"; chown root:root "$d/github_token"; chmod 600 "$d/github_token"
  cat >"$d/metadata" <<EOF
profile=$PROFILE
mode=$MODE
owner=$OWNER
repo=$repo
runner_name=$rn
container_name=$cn
image=$IMAGE
docker_socket=$SOCKET
EOF
  chmod 600 "$d/metadata"
}

runner_id(){
  local e="$1" n="$2" p=1 r id
  while :; do
    r="$(api GET "$e?per_page=100&page=$p")" || return 1
    id="$(jq -r --arg n "$n" '.runners[]?|select(.name==$n)|.id' <<<"$r" | head -1)"
    [[ -z "$id" ]] || { echo "$id"; return; }
    (($(jq '.runners|length' <<<"$r")==100)) || return 1
    ((p++))
  done
}
remote_delete(){
  local id; id="$(runner_id "$1" "$2" 2>/dev/null)" || return 0
  [[ -n "$id" ]] && api DELETE "$1/$id" >/dev/null || true
}

legacy_dirs(){
  local repo="$1" a="$RUNNER_BASE/$(san "$repo")" b="$RUNNER_BASE/profiles/$(san "$PROFILE")/$(san "$repo")"
  [[ "$PROFILE" != default && -d "$b" ]] && echo "$b"; [[ -d "$a" ]] && echo "$a"
}
legacy_cleanup(){
  local repo="$1" d u agent
  while read -r d; do
    [[ -n "$d" && ( -f "$d/.runner" || -f "$d/.service" || -f "$d/.chrisscriptbase-runner" ) ]] || continue
    log "Migracja systemd -> Docker: $d"
    agent="$(jq -r '.agentName//.name//empty' "$d/.runner" 2>/dev/null || true)"
    [[ -x "$d/svc.sh" ]] && (cd "$d"; ./svc.sh stop >/dev/null 2>&1 || true; ./svc.sh uninstall >/dev/null 2>&1 || true)
    u="$(head -1 "$d/.service" 2>/dev/null | tr -d '\r' || true)"
    if [[ "$u" == actions.runner.*.service ]] && command -v systemctl >/dev/null; then
      systemctl stop "$u" >/dev/null 2>&1 || true; systemctl disable "$u" >/dev/null 2>&1 || true
      rm -f "/etc/systemd/system/$u"; find /etc/systemd/system -type l -name "$u" -delete 2>/dev/null || true
      systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    [[ -n "$agent" ]] && remote_delete "/repos/$OWNER/$repo/actions/runners" "$agent"
    rm -rf "$d"
  done < <(legacy_dirs "$repo")
}

legacy_org_dirs(){
  local a="$RUNNER_BASE/organization" b="$RUNNER_BASE/profiles/$(san "$PROFILE")/organization"
  [[ "$PROFILE" != default && -d "$b" ]] && echo "$b"; [[ -d "$a" ]] && echo "$a"
}
legacy_org_cleanup(){
  local d u agent
  while read -r d; do
    [[ -n "$d" && ( -f "$d/.runner" || -f "$d/.service" || -f "$d/.chrisscriptbase-runner" ) ]] || continue
    log "Migracja organization systemd -> Docker: $d"
    agent="$(jq -r '.agentName//.name//empty' "$d/.runner" 2>/dev/null || true)"
    [[ -x "$d/svc.sh" ]] && (cd "$d"; ./svc.sh stop >/dev/null 2>&1 || true; ./svc.sh uninstall >/dev/null 2>&1 || true)
    u="$(head -1 "$d/.service" 2>/dev/null | tr -d '\r' || true)"
    if [[ "$u" == actions.runner.*.service ]] && command -v systemctl >/dev/null; then
      systemctl stop "$u" >/dev/null 2>&1 || true; systemctl disable "$u" >/dev/null 2>&1 || true
      rm -f "/etc/systemd/system/$u"; find /etc/systemd/system -type l -name "$u" -delete 2>/dev/null || true
      systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    [[ -n "$agent" ]] && remote_delete "/orgs/$OWNER/actions/runners" "$agent"
    rm -rf "$d"
  done < <(legacy_org_dirs)
}

legacy_repos(){
  local roots=("$RUNNER_BASE"); [[ "$PROFILE" != default ]] && roots=("$RUNNER_BASE/profiles/$(san "$PROFILE")" "$RUNNER_BASE")
  local r d repo url
  for r in "${roots[@]}"; do
    [[ -d "$r" ]] || continue
    for d in "$r"/*; do
      [[ -d "$d" ]] || continue; case "$(basename "$d")" in docker|profiles|organization) continue;; esac
      [[ -f "$d/.runner" || -f "$d/.chrisscriptbase-runner" ]] || continue
      repo="$(awk -F= '$1=="repo"{sub(/^repo=/,"");print;exit}' "$d/.chrisscriptbase-runner" 2>/dev/null || true)"
      if [[ -z "$repo" ]]; then url="$(jq -r '.gitHubUrl//empty' "$d/.runner" 2>/dev/null || true)"; repo="${url%/}"; repo="${repo##*/}"; fi
      [[ -n "$repo" ]] && echo "$repo"
    done
  done | awk 'NF&&!s[tolower($0)]++'
}
local_repos(){
  local d m repo p="$(root)/repositories"
  { if [[ -d "$p" ]]; then for d in "$p"/*; do [[ -f "$d/metadata" ]] || continue; repo="$(meta_get "$d/metadata" repo)"; [[ -n "$repo" ]] && echo "$repo"; done; fi
    legacy_repos
  } | awk 'NF&&!s[tolower($0)]++'
}

run_container(){
  local d="$1" scope="$2" repo="$3" rn="$4" cn="$5" w="$d/work" t="$d/github_token"; shift 5 || true
  write_state "$d" "$repo" "$rn" "$cn"
  docker inspect "$cn" >/dev/null 2>&1 && docker rm -f "$cn" >/dev/null || true
  local a=(run -d --name "$cn" --restart unless-stopped
    --label com.chrisscriptbase.github-runner=true --label "com.chrisscriptbase.profile=$PROFILE"
    -e "RUNNER_SCOPE=$scope" -e "GITHUB_OWNER=$OWNER" -e "GITHUB_REPOSITORY=$repo"
    -e "RUNNER_NAME=$rn" -e "RUNNER_LABELS=$LABELS" -e "RUNNER_WORKDIR=$w" -e "GITHUB_API_VERSION=$API_VERSION"
    --mount "type=bind,src=$t,dst=/run/secrets/github_token,readonly"
    --mount "type=bind,src=$w,dst=$w")
  if [[ "$SOCKET" == true ]]; then
    [[ -S /var/run/docker.sock ]] || die "Brak /var/run/docker.sock"
    a+=(--mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock)
  fi
  a+=("$IMAGE"); docker "${a[@]}" >/dev/null
  sleep 2; [[ "$(docker inspect -f '{{.State.Running}}' "$cn" 2>/dev/null || true)" == true ]] || { docker logs "$cn" | tail -100 >&2; return 1; }
}

install_repo(){
  local r="$1" d="$(repo_state "$1")" rn="$(repo_runner "$1")" cn="$(repo_container "$1")"
  legacy_cleanup "$r"
  if docker inspect "$cn" >/dev/null 2>&1 && [[ "$(docker inspect -f '{{.State.Running}}' "$cn")" == true ]]; then echo "Już działa: $cn"; return 3; fi
  log "Instalacja Docker runnera $OWNER/$r"; run_container "$d" repo "$r" "$rn" "$cn"
}
remove_repo(){
  local r="$1" d="$(repo_state "$1")" m rn="$(repo_runner "$1")" cn="$(repo_container "$1")"
  legacy_cleanup "$r"; m="$d/metadata"
  [[ -f "$m" ]] && { rn="$(meta_get "$m" runner_name)"; cn="$(meta_get "$m" container_name)"; }
  docker inspect "$cn" >/dev/null 2>&1 && { docker stop -t 30 "$cn" >/dev/null 2>&1 || true; docker rm -f "$cn" >/dev/null 2>&1 || true; }
  remote_delete "/repos/$OWNER/$r/actions/runners" "$rn"; rm -rf "$d"
}
install_org(){
  local d="$(org_state)" rn="$(org_runner)" cn="$(org_container)"
  legacy_org_cleanup
  docker inspect "$cn" >/dev/null 2>&1 && [[ "$(docker inspect -f '{{.State.Running}}' "$cn")" == true ]] && { echo "Już działa: $cn"; return 3; }
  log "Instalacja Docker organization runnera $OWNER"; run_container "$d" org "" "$rn" "$cn"
}
remove_org(){
  local d="$(org_state)" m="$d/metadata" rn="$(org_runner)" cn="$(org_container)"
  legacy_org_cleanup
  [[ -f "$m" ]] && { rn="$(meta_get "$m" runner_name)"; cn="$(meta_get "$m" container_name)"; }
  docker inspect "$cn" >/dev/null 2>&1 && { docker stop -t 30 "$cn" >/dev/null 2>&1 || true; docker rm -f "$cn" >/dev/null 2>&1 || true; }
  remote_delete "/orgs/$OWNER/actions/runners" "$rn"; rm -rf "$d"
}

norm(){ local x="$1"; [[ "$x" == *:* ]] && x="${x#*:}"; [[ "$x" == */* ]] && x="${x##*/}"; echo "$x"; }
for_profile(){ [[ "$1" != *:* || "${1%%:*}" == "$PROFILE" ]]; }
explicit(){
  local -a avail=("$@"); local s n a
  for s in "${REPOS[@]}"; do for_profile "$s" || continue; n="$(norm "$s")"; for a in "${avail[@]}"; do [[ "${a,,}" == "${n,,}" ]] && { echo "$a"; break; }; done; done | awk 'NF&&!s[tolower($0)]++'
}
interactive(){
  local -a a=("$@") items=(); local x out rc=0
  [[ -t 0 && -t 1 ]] || die "--select-repos wymaga TTY"
  if [[ "$UI" == gui && -n "${DISPLAY:-}" ]] && command -v zenity >/dev/null; then
    for x in "${a[@]}"; do items+=(FALSE "$x"); done
    sudo -u "$CALLER" env DISPLAY="$DISPLAY" XAUTHORITY="${XAUTHORITY:-$CALLER_HOME/.Xauthority}" \
      zenity --list --checklist --title="GitHub Docker Runners" --column="Wybierz" --column="Repo" --separator=$'\n' "${items[@]}" || true
    return
  fi
  command -v dialog >/dev/null || command -v whiptail >/dev/null || { apt-get update; apt-get install -y dialog; }
  for x in "${a[@]}"; do items+=("$x" "" off); done
  if command -v dialog >/dev/null; then out="$(dialog --stdout --separate-output --checklist "Repozytoria" 24 100 16 "${items[@]}")" || rc=$?; clear || true
  else out="$(whiptail --checklist "Repozytoria" 24 100 16 "${items[@]}" 3>&1 1>&2 2>&3)" || rc=$?; out="$(sed 's/" "/\n/g;s/^"//;s/"$//' <<<"$out")"; fi
  ((rc==0)) && printf '%s\n' "$out"
}
resolve(){
  local -a a=() c=()
  [[ "$LIST_REPOS" == true ]] && { remote_repos; return; }
  [[ "$ACTION" == uninstall ]] && mapfile -t a < <(local_repos) || mapfile -t a < <(remote_repos)
  case "$SELECT_MODE" in
    all) c=("${a[@]}");;
    explicit)
      mapfile -t c < <(explicit "${a[@]}")
      if [[ "$ACTION" == uninstall && ${#c[@]} -eq 0 ]]; then local x; for x in "${REPOS[@]}"; do for_profile "$x" && c+=("$(norm "$x")"); done; fi;;
    interactive) mapfile -t c < <(interactive "${a[@]}");;
  esac
  printf '%s\n' "${c[@]}"
}

process(){
  if [[ "$MODE" == org ]]; then
    [[ "$LIST_REPOS" == true ]] && { warn "org: --list-repos pominięte"; return; }
    if [[ "$ACTION" == install ]]; then install_org; else remove_org; fi
    return
  fi
  local -a rr=(); mapfile -t rr < <(resolve); [[ "$LIST_REPOS" == true ]] && { printf '%s\n' "${rr[@]}"; return; }
  ((${#rr[@]})) || { warn "$PROFILE: brak repo"; return; }
  local r rc ok=0 skip=0 fail=0
  for r in "${rr[@]}"; do
    if [[ "$ACTION" == install ]]; then
      if install_repo "$r"; then ((ok += 1)); else rc=$?; if ((rc==3)); then ((skip += 1)); else ((fail += 1)); fi; fi
    else
      if remove_repo "$r"; then ((ok += 1)); else ((fail += 1)); fi
    fi
  done
  echo "$PROFILE: sukces=$ok pominięte=$skip błędy=$fail"; ((fail==0))
}

main(){
  args "$@"; caller_init
  [[ "$LIST_PROFILES" == true ]] && { profiles; exit; }
  [[ $EUID -eq 0 ]] || die "Uruchom przez sudo/root"
  deps
  [[ "$LIST_REPOS" == true ]] || docker_ready
  ((${#PROFILES[@]})) || PROFILES=(default)
  [[ "$ACTION" == install && "$LIST_REPOS" == false ]] && build_image
  local p failed=0
  for p in "${PROFILES[@]}"; do load_profile "$p"; auth; process || ((failed += 1)); done
  [[ "$LIST_REPOS" == true ]] && exit "$failed"
  if [[ "$PURGE" == true ]]; then
    if ! docker ps -a --filter label=com.chrisscriptbase.github-runner=true --format '{{.ID}}' | grep -q .; then rm -rf "$STATE_BASE"; docker image rm "$IMAGE" >/dev/null 2>&1 || true; fi
  fi
  docker ps -a --filter label=com.chrisscriptbase.github-runner=true --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
  exit "$failed"
}
main "$@"
