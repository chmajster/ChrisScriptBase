#!/usr/bin/env bash

set -euo pipefail

# ============================================================
# KONFIGURACJA
# ============================================================

GIT_NAME="chmajster"
GIT_EMAIL="chmajster@domain.com"
GITHUB_USER="chmajster"

GITHUB_TOKEN="XXXXXXXXXXXXXXXX"

# Kodowanie tokena do Base64
GITHUB_TOKEN_BASE64="$(
    printf '%s' "$GITHUB_TOKEN" | base64 | tr -d '\n'
)"

# Usuń plaintext token z pamięci zmiennej shell
unset GITHUB_TOKEN

# ============================================================
# GLOBAL GIT CONFIG
# ============================================================

git config --global user.name "$GIT_NAME"
git config --global user.email "$GIT_EMAIL"

git config --global init.defaultBranch main

git config --global core.autocrlf input
git config --global core.eol lf

git config --global fetch.prune true
git config --global pull.rebase true
git config --global rebase.autoStash true
git config --global push.autoSetupRemote true

# ============================================================
# GITHUB CONFIG
# ============================================================

git config --global github.username "$GITHUB_USER"
git config --global github.tokenBase64 "$GITHUB_TOKEN_BASE64"

# Usuń poprzedni helper GitHub
git config --global --unset-all \
    'credential.https://github.com.helper' \
    2>/dev/null || true

# ============================================================
# CREDENTIAL HELPER
# ============================================================

git config --global 'credential.https://github.com.helper' \
'!f() {
    if [ "$1" = "get" ]; then
        username="$(git config --global --get github.username)"
        token_base64="$(git config --global --get github.tokenBase64)"
        token="$(printf "%s" "$token_base64" | base64 --decode)"

        printf "username=%s\n" "$username"
        printf "password=%s\n" "$token"
    fi
}; f'

# ============================================================
# PERMISSIONS
# ============================================================

chmod 600 "$HOME/.gitconfig"

unset GITHUB_TOKEN_BASE64

# ============================================================
# RESULT
# ============================================================

echo
echo "Git skonfigurowany."
echo "User:       $(git config --global user.name)"
echo "Email:      $(git config --global user.email)"
echo "GitHub:     $(git config --global github.username)"
echo "Config:     $HOME/.gitconfig"
echo "Token:      zapisany jako Base64"