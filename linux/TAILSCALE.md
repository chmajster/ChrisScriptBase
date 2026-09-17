# Tailscale Manager

`linux/tailscale-manager.sh` instaluje, łączy i konfiguruje Tailscale na serwerach Linux. Udostępnia ten sam zestaw operacji przez trzy tryby: GUI (`dialog`/`whiptail`), interaktywny CLI oraz w pełni nieinteraktywny tryb silent.

## Tryby pracy

```bash
sudo bash linux/tailscale-manager.sh --gui
sudo bash linux/tailscale-manager.sh --cli
sudo bash linux/tailscale-manager.sh --silent --status
```

`--gui` używa `dialog`, następnie `whiptail`, a gdy żaden interfejs nie jest dostępny, przechodzi do CLI. W trybie silent skrypt nigdy nie zadaje pytań i kończy się kodem błędu, jeżeli brakuje wymaganej akcji lub danych.

## Instalacja i dołączenie serwera

Najbezpieczniej przekazać auth key przez plik dostępny tylko dla administratora:

```bash
sudo install -m 600 /dev/null /root/tailscale.key
sudo sh -c 'printf "%s" "tskey-..." > /root/tailscale.key'

sudo bash linux/tailscale-manager.sh \
  --silent \
  --install \
  --auth-key-file /root/tailscale.key \
  --hostname server01 \
  --ssh \
  --accept-dns
```

Można użyć `--auth-key KEY`, ale wartość podana w wierszu poleceń może być widoczna w historii powłoki lub w argumentach procesu nadrzędnego. Manager nie zapisuje klucza do logu; przekazuje go dalej do Tailscale przez tymczasowy plik `0600` i usuwa plik po zakończeniu. `--auth-key-file` pozostaje zalecaną metodą.

## Obsługiwane dystrybucje

Manager rozpoznaje rodziny:

- Debian/Ubuntu, Linux Mint, Raspberry Pi OS i pochodne,
- RHEL, Rocky Linux, AlmaLinux, CentOS Stream, Fedora, Oracle Linux i Amazon Linux,
- SLES/openSUSE,
- Arch Linux/Manjaro.

Dla Arch używany jest `pacman`. Dla pozostałych wspieranych rodzin instalacja pobiera oficjalny instalator Tailscale przez HTTPS do pliku tymczasowego, sprawdza odpowiedź i uruchamia ją lokalnie zamiast wykonywać `curl | sh`. Aktualizacja ma fallback do `apt-get`, `dnf`, `yum`, `zypper` lub `pacman`.

## Subnet router

```bash
sudo bash linux/tailscale-manager.sh \
  --silent \
  --install \
  --auth-key-file /root/tailscale.key \
  --hostname router01 \
  --advertise-routes 192.168.10.0/24,10.20.0.0/16
```

Przy reklamowaniu tras manager idempotentnie włącza forwarding IPv4 i IPv6 przez `/etc/sysctl.d/99-tailscale.conf` (z fallbackiem do `/etc/sysctl.conf`). Reklamowane trasy mogą wymagać zatwierdzenia w panelu administracyjnym tailnetu.

Wyczyszczenie reklamowanych tras:

```bash
sudo bash linux/tailscale-manager.sh --silent --clear-advertise-routes
```

## Exit node

Udostępnienie serwera jako exit node:

```bash
sudo bash linux/tailscale-manager.sh \
  --silent \
  --install \
  --auth-key-file /root/tailscale.key \
  --hostname exit01 \
  --advertise-exit-node
```

Użycie innego exit node:

```bash
sudo bash linux/tailscale-manager.sh \
  --silent \
  --exit-node exit01 \
  --exit-node-allow-lan-access
```

Wyłączenie używania exit node:

```bash
sudo bash linux/tailscale-manager.sh --silent --clear-exit-node
```

## Tailscale SSH, DNS, routes i bezpieczeństwo

Przykłady zmian pojedynczych ustawień:

```bash
sudo bash linux/tailscale-manager.sh --silent --ssh
sudo bash linux/tailscale-manager.sh --silent --no-ssh
sudo bash linux/tailscale-manager.sh --silent --accept-routes
sudo bash linux/tailscale-manager.sh --silent --no-accept-routes
sudo bash linux/tailscale-manager.sh --silent --accept-dns
sudo bash linux/tailscale-manager.sh --silent --shields-up
sudo bash linux/tailscale-manager.sh --silent --operator chris
```

Dostęp Tailscale SSH jest nadal kontrolowany przez politykę tailnetu. Manager nie omija ACL/grants ani zatwierdzania tras/exit node po stronie administracyjnej Tailscale.

## Tagi

Tagi są przekazywane przy `tailscale up`:

```bash
sudo bash linux/tailscale-manager.sh \
  --silent \
  --connect \
  --auth-key-file /root/tailscale.key \
  --advertise-tags tag:server,tag:prod
```

Tag musi istnieć i być dozwolony przez politykę tailnetu.

## Status i diagnostyka

```bash
sudo bash linux/tailscale-manager.sh --silent --status
sudo bash linux/tailscale-manager.sh --silent --status --json
sudo bash linux/tailscale-manager.sh --silent --ip
sudo bash linux/tailscale-manager.sh --silent --diagnose
sudo bash linux/tailscale-manager.sh --silent --ping server02
```

Diagnostyka sprawdza usługę `tailscaled`, backend state, IPv4/IPv6, DNS, `tailscale netcheck`, bieżące preferencje i listę peerów.

## Zarządzanie usługą

```bash
sudo bash linux/tailscale-manager.sh --silent --service start
sudo bash linux/tailscale-manager.sh --silent --service restart
sudo bash linux/tailscale-manager.sh --silent --service enable
sudo bash linux/tailscale-manager.sh --silent --service status
```

## Aktualizacja i odinstalowanie

```bash
sudo bash linux/tailscale-manager.sh --silent --update
sudo bash linux/tailscale-manager.sh --silent --uninstall --force
```

W trybie silent odinstalowanie wymaga jawnego `--force`.

## Plik konfiguracyjny

Przykład znajduje się w `linux/tailscale.conf.example`.

```bash
sudo bash linux/tailscale-manager.sh \
  --silent \
  --config linux/tailscale.conf.example \
  --hostname server02
```

Priorytet wartości:

```text
defaults < config file < CLI arguments
```

Parser nie wykonuje pliku jako kodu shellowego. Wartość `AUTH_KEY` jest w pliku konfiguracyjnym zabroniona; użyj `AUTH_KEY_FILE`.

Zapis bieżącej konfiguracji bez sekretów:

```bash
sudo bash linux/tailscale-manager.sh --silent --status --save-config /etc/chrisscriptbase/tailscale.conf
```

## Dry-run

```bash
sudo bash linux/tailscale-manager.sh \
  --silent \
  --install \
  --auth-key-file /root/tailscale.key \
  --hostname server01 \
  --ssh \
  --advertise-routes 192.168.10.0/24 \
  --dry-run
```

Dry-run pokazuje planowane zmiany bez instalowania pakietów, zmieniania `sysctl`, uruchamiania usługi ani modyfikowania konfiguracji Tailscale. Auth key jest maskowany.

## Najważniejsze argumenty

Pełna lista jest dostępna przez:

```bash
bash linux/tailscale-manager.sh --help
```

Obsługiwane są między innymi: `--install`, `--update`, `--uninstall`, `--connect`, `--disconnect`, `--logout`, `--auth-key-file`, `--hostname`, `--ssh`, `--accept-routes`, `--accept-dns`, `--advertise-routes`, `--advertise-exit-node`, `--exit-node`, `--advertise-tags`, `--operator`, `--shields-up`, `--snat-subnet-routes`, `--stateful-filtering`, `--netfilter-mode`, `--auto-update`, `--webclient`, `--status`, `--ip`, `--diagnose`, `--ping`, `--config`, `--save-config`, `--dry-run`, `--json`, `--verbose` i `--quiet`.

## Exit codes

| Kod | Znaczenie |
| ---: | --- |
| 0 | sukces |
| 1 | błąd ogólny |
| 2 | niepoprawne argumenty |
| 3 | nieobsługiwany system |
| 4 | brak zależności |
| 5 | błąd instalacji/aktualizacji/usuwania |
| 6 | błąd `tailscaled`/usługi |
| 7 | błąd autoryzacji |
| 8 | błąd konfiguracji |
| 9 | błąd łączności |
| 10 | błąd walidacji |

## Testy

```bash
bash linux/tests/test-tailscale-manager.sh
shellcheck -x linux/tailscale-manager.sh
```

Testy nie instalują Tailscale ani nie zmieniają sieci hosta; używają atrap `tailscale` i `systemctl` dla operacji dry-run.
