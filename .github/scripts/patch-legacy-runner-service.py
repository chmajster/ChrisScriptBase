from pathlib import Path
import re

path = Path("linux/install-github-selfhosted-runners.sh")
text = path.read_text()

pattern = r"(?ms)^repo_from_service_unit\(\) \{\n.*?^\}\n"
replacement = r'''repo_from_service_unit() {
    local unit="$1" prefix rest host marker legacy_marker repo runner repo_sanitized profile_sanitized
    unit="${unit,,}"
    prefix="actions.runner.${GITHUB_OWNER,,}-"
    [[ "$unit" == "$prefix"*".service" ]] || return 1

    rest="${unit#"$prefix"}"
    rest="${rest%.service}"
    host="$(hostname -s)"
    host="${host,,}"
    marker=".${host}-"
    legacy_marker=".${host}"

    if [[ "$rest" == *"$marker"* ]]; then
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
    elif [[ "$rest" == *"$legacy_marker" ]]; then
        # Najstarszy format usługi nie zawiera nazwy runnera po hostname:
        # actions.runner.<owner>-<repo>.<host>.service
        repo="${rest%"$legacy_marker"}"
        [[ -n "$repo" ]] || return 1
    else
        return 1
    fi

    printf '%s\n' "$repo"
}
'''

matches = list(re.finditer(pattern, text))
if len(matches) != 1:
    raise SystemExit(f"repo_from_service_unit: expected 1 function, got {len(matches)}")

text = text[: matches[0].start()] + replacement + text[matches[0].end() :]
path.write_text(text)
