#!/usr/bin/env bash

PROFILE="$HOME/.bashrc"

# Usuń poprzednią wersję, jeśli została dodana wcześniej
sed -i '/# ===== Git branch in Bash prompt =====/,/# ===== End Git branch in Bash prompt =====/d' "$PROFILE"

cat >> "$PROFILE" <<'EOF'

# ===== Git branch in Bash prompt =====

git_branch_prompt() {
    local branch
    branch=$(git symbolic-ref --quiet --short HEAD 2>/dev/null) || return
    printf ' [%s]' "$branch"
}

# Kolory
GREEN='\[\033[01;32m\]'
BLUE='\[\033[01;34m\]'
YELLOW='\[\033[01;33m\]'
RESET='\[\033[00m\]'

# user@host = zielony
# katalog    = niebieski
# branch Git = żółty
PS1="${GREEN}\u@\h${RESET}:${BLUE}\w${RESET}${YELLOW}\$(git_branch_prompt)${RESET}\$ "

# ===== End Git branch in Bash prompt =====
EOF

echo "Dodano kolorowy Git branch do prompta."
echo "Wczytywanie ~/.bashrc..."

source "$PROFILE"