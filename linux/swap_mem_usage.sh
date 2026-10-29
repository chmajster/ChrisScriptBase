#!/usr/bin/env bash
# Repository: [chmajster/ChrisScriptBase](https://github.com/chmajster/ChrisScriptBase)

set -o errexit
set -o nounset
set -o pipefail

if [[ ! -r /proc/meminfo ]]; then
    echo "Blad: /proc/meminfo nie jest dostepny. Uruchom skrypt na systemie Linux."
    exit 1
fi

format_kb() {
    local kb="$1"
    awk -v kb="$kb" 'BEGIN {
        if (kb >= 1048576) {
            printf "%.2f GiB", kb / 1048576
        } else {
            printf "%.2f MiB", kb / 1024
        }
    }'
}

swap_total_kb="$(awk '/^SwapTotal:/ {print $2}' /proc/meminfo)"
swap_free_kb="$(awk '/^SwapFree:/ {print $2}' /proc/meminfo)"
swap_used_kb="$((swap_total_kb - swap_free_kb))"

echo "Uzycie swap: $(format_kb "$swap_used_kb") / $(format_kb "$swap_total_kb")"
echo
printf "%-10s %-30s %12s %12s\n" "PID" "NAZWA" "SWAP_KB" "SWAP"
printf "%-10s %-30s %12s %12s\n" "----------" "------------------------------" "------------" "------------"

found_processes=0

while IFS=$'\t' read -r swap_kb pid name; do
    [[ "$swap_kb" -gt 0 ]] || continue
    found_processes=1
    printf "%-10s %-30.30s %12s %12s\n" "$pid" "$name" "$swap_kb" "$(format_kb "$swap_kb")"
done < <(
    awk '
        /^Name:/ {
            name = $2
        }
        /^Pid:/ {
            pid = $2
        }
        /^VmSwap:/ {
            swap = $2
            if (swap > 0) {
                print swap "\t" pid "\t" name
            }
        }
    ' /proc/[0-9]*/status 2>/dev/null | sort -nr
)

if [[ "$found_processes" -eq 0 ]]; then
    echo "Brak procesow korzystajacych ze swap."
fi
