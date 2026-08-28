#!/usr/bin/env bash
set -Eeuo pipefail

cd /actions-runner

TOKEN_FILE="${GITHUB_TOKEN_FILE:-/run/secrets/github_token}"
API_VERSION="${GITHUB_API_VERSION:-2022-11-28}"
RUNNER_SCOPE="${RUNNER_SCOPE:-repo}"
RUNNER_NAME="${RUNNER_NAME:-$(hostname)}"
RUNNER_LABELS="${RUNNER_LABELS:-docker}"
RUNNER_WORKDIR="${RUNNER_WORKDIR:-_work}"

[[ -r "$TOKEN_FILE" ]] || {
    echo "ERROR: cannot read GitHub token file: $TOKEN_FILE" >&2
    exit 1
}
[[ -n "${GITHUB_OWNER:-}" ]] || {
    echo "ERROR: GITHUB_OWNER is required." >&2
    exit 1
}

GITHUB_PAT="$(cat "$TOKEN_FILE")"
[[ -n "$GITHUB_PAT" ]] || {
    echo "ERROR: GitHub token file is empty." >&2
    exit 1
}

case "$RUNNER_SCOPE" in
    repo)
        [[ -n "${GITHUB_REPOSITORY:-}" ]] || {
            echo "ERROR: GITHUB_REPOSITORY is required for repo scope." >&2
            exit 1
        }
        RUNNER_URL="https://github.com/${GITHUB_OWNER}/${GITHUB_REPOSITORY}"
        REG_ENDPOINT="/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}/actions/runners/registration-token"
        REMOVE_ENDPOINT="/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}/actions/runners/remove-token"
        ;;
    org)
        RUNNER_URL="https://github.com/${GITHUB_OWNER}"
        REG_ENDPOINT="/orgs/${GITHUB_OWNER}/actions/runners/registration-token"
        REMOVE_ENDPOINT="/orgs/${GITHUB_OWNER}/actions/runners/remove-token"
        ;;
    *)
        echo "ERROR: RUNNER_SCOPE must be repo or org." >&2
        exit 1
        ;;
esac

github_api() {
    local method="$1" endpoint="$2"
    curl --silent --show-error --fail-with-body --location \
        --request "$method" \
        --header "Accept: application/vnd.github+json" \
        --header "Authorization: Bearer ${GITHUB_PAT}" \
        --header "X-GitHub-Api-Version: ${API_VERSION}" \
        "https://api.github.com${endpoint}"
}

remove_registration() {
    local remove_token=""
    [[ -f .runner ]] || return 0
    remove_token="$(github_api POST "$REMOVE_ENDPOINT" | jq -r '.token // empty')" || {
        echo "WARNING: cannot obtain remove token; leaving remote registration for manager cleanup." >&2
        return 0
    }
    [[ -n "$remove_token" ]] || return 0
    ./config.sh remove --unattended --token "$remove_token" || true
}

child_pid=""
terminate() {
    if [[ -n "$child_pid" ]]; then
        kill -TERM "$child_pid" 2>/dev/null || true
        wait "$child_pid" 2>/dev/null || true
        child_pid=""
    fi
    remove_registration
    exit 0
}
trap terminate TERM INT
trap remove_registration EXIT

# A restarted container can retain its previous .runner configuration.
# Remove it first and register again with a fresh short-lived token.
remove_registration

registration_token="$(github_api POST "$REG_ENDPOINT" | jq -r '.token // empty')"
[[ -n "$registration_token" ]] || {
    echo "ERROR: GitHub did not return a registration token." >&2
    exit 1
}

./config.sh --unattended \
    --url "$RUNNER_URL" \
    --token "$registration_token" \
    --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" \
    --work "$RUNNER_WORKDIR" \
    --replace \
    --disableupdate

unset registration_token

./run.sh &
child_pid="$!"
set +e
wait "$child_pid"
rc=$?
set -e
child_pid=""
exit "$rc"
