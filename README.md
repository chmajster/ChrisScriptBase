# ChrisScriptBase

English | [Polski](#polski)

## English

ChrisScriptBase is a collection of utility scripts for Linux administration and ServiceNow dashboard work.

Repository: [chmajster/ChrisScriptBase](https://github.com/chmajster/ChrisScriptBase)

## Contents

| Path | Description |
| --- | --- |
| `linux/detect_os.sh` | Detects supported Linux distributions and versions. |
| `linux/os_patching.sh` | Runs system patching with OS detection, repository listing, optional service stops, update reporting, and logging. |
| `linux/swam_mem_usage.sh` | Shows swap usage and lists processes currently using swap. |
| `linux/konfiguracja_samba_www_ubuntu.sh` | Installs and configures Apache, PHP, MariaDB, phpMyAdmin, and Samba on Ubuntu. |
| `linux/web-hosts-file-manager.sh` | Installs a Flask-based web manager for `/etc/hosts`. |
| `linux/ldapsearch.sh` | Discovers SSSD/LDAP settings and looks up LDAP/AD users, groups and netgroups with NSS/SSSD fallback. |
| `linux/tailscale-manager.sh` | Installs and configures Tailscale with dialog/whiptail GUI, interactive CLI, or fully non-interactive silent mode. |
| `linux/nginx-manager.sh` | Safely manages Nginx sites, reverse proxies, SSL, backups, diagnostics and services through dialog or CLI. |
| `linux/awx-inventory-sync.sh` | Detects AWX in Kubernetes, bootstraps SSH key authentication, imports remote Ansible inventory and installs recurring synchronization. |
| `windows/install-wsl.ps1` | Enables WSL 2, installs Ubuntu, creates the default `Chris` Linux account and maps its home to the current Windows user's `Documents` directory. |
| `snow/watchdog-dashboard.js` | Browser-based ServiceNow watchdog dashboard snippet. |

## Usage

Review each script before running it, especially scripts that install packages, change system services, or edit files under `/etc`.

Most Linux scripts should be run with Bash:

```bash
bash linux/detect_os.sh
bash linux/swam_mem_usage.sh
bash linux/ldapsearch.sh USER
```

LDAP lookup examples:

```bash
bash linux/ldapsearch.sh krzysztof
bash linux/ldapsearch.sh --groups krzysztof
bash linux/ldapsearch.sh --netgroups krzysztof
bash linux/ldapsearch.sh --json krzysztof
bash linux/ldapsearch.sh --config
```

The LDAP utility reads existing system LDAP/SSSD configuration. It never prints configured bind passwords and does not install LDAP client packages automatically.

Tailscale manager examples:

```bash
sudo bash linux/tailscale-manager.sh --gui
sudo bash linux/tailscale-manager.sh --silent --install \
  --auth-key-file /root/tailscale.key --hostname server01 --ssh
sudo bash linux/tailscale-manager.sh --silent --diagnose
```

The Tailscale manager supports Tailscale SSH, subnet routes, exit nodes, DNS/routes preferences, tags, status, diagnostics, config files and dry-run. See [`linux/TAILSCALE.md`](linux/TAILSCALE.md) for the full documentation.

Nginx Manager examples:

```bash
sudo bash linux/nginx-manager.sh
bash linux/nginx-manager.sh --status
sudo bash linux/nginx-manager.sh --non-interactive --backup
sudo bash linux/nginx-manager.sh --non-interactive --add-proxy \
  --domain api.example.com --backend-host 127.0.0.1 --backend-port 8080
```

Nginx Manager supports Debian, Ubuntu, Linux Mint, RHEL, Rocky Linux, AlmaLinux, CentOS Stream and Fedora. Every configuration write is tested with `nginx -t`; a failed test triggers rollback and blocks reload. Global listen-port migration can remove all active Nginx listeners from port 80 while preserving unrelated ports. See [`linux/NGINX.md`](linux/NGINX.md).

HTTP ports can be changed without manual file editing:

```bash
sudo bash linux/nginx-manager.sh --non-interactive \
  --change-site-port --domain example.com --port 8080 --yes
sudo bash linux/nginx-manager.sh --non-interactive \
  --set-default-port --port 8080 --yes
sudo bash linux/nginx-manager.sh --non-interactive \
  --disable-port-80 --port 8080 --yes
```

AWX inventory synchronization is configured interactively on the Kubernetes host running AWX:

```bash
sudo bash linux/awx-inventory-sync.sh
```

The first run asks for the source SSH host, port, username and a one-time password. It then generates a dedicated ED25519 key, detects AWX, imports the inventory and installs a five-minute cron job. The SSH password is not persisted.

Scripts that modify the system usually require root privileges:

```bash
sudo bash linux/os_patching.sh
sudo bash linux/konfiguracja_samba_www_ubuntu.sh
sudo bash linux/web-hosts-file-manager.sh
```

Windows WSL installer should be run from an elevated PowerShell session:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\windows\install-wsl.ps1
```

By default it installs Ubuntu, creates the Linux account `Chris` with password `1`, sets it as the default WSL user and uses `C:\Users\<WindowsUser>\Documents` as its home directory through `/mnt/c/Users/<WindowsUser>/Documents`. If Windows reports that a reboot is required before the distribution can finish installing, reboot and run the same script again.

The ServiceNow dashboard script is intended to run inside an authenticated ServiceNow browser session as a browser snippet, userscript, or bookmarklet.

## Notes

- Test scripts in a safe environment before using them on production systems.
- Adjust configuration values inside scripts that require local settings.
- Some scripts are distribution-specific; check comments and detected OS support first.
- `linux/ldapsearch.sh` supports SSSD, OpenLDAP and nslcd-style configuration discovery and can fall back to NSS/SSSD when direct LDAP access is unavailable.
- `linux/tailscale-manager.sh` never stores an auth key in its saved configuration; prefer `--auth-key-file` with permissions `600` or `400`.

---

## Polski

ChrisScriptBase to kolekcja skryptow pomocniczych do administracji Linuxem oraz pracy z dashboardem ServiceNow.

Repozytorium: [chmajster/ChrisScriptBase](https://github.com/chmajster/ChrisScriptBase)

## Zawartosc

| Sciezka | Opis |
| --- | --- |
| `linux/detect_os.sh` | Wykrywa obslugiwane dystrybucje i wersje Linuxa. |
| `linux/os_patching.sh` | Wykonuje patchowanie systemu z wykrywaniem OS, lista repozytoriow, opcjonalnym zatrzymywaniem uslug, raportem aktualizacji i logowaniem. |
| `linux/swam_mem_usage.sh` | Pokazuje uzycie swap oraz procesy, ktore aktualnie korzystaja ze swap. |
| `linux/konfiguracja_samba_www_ubuntu.sh` | Instaluje i konfiguruje Apache, PHP, MariaDB, phpMyAdmin oraz Sambe na Ubuntu. |
| `linux/web-hosts-file-manager.sh` | Instaluje webowy manager pliku `/etc/hosts` oparty o Flask. |
| `linux/ldapsearch.sh` | Wykrywa konfiguracje SSSD/LDAP i wyszukuje uzytkownikow LDAP/AD, grupy oraz netgroupy z fallbackiem NSS/SSSD. |
| `linux/tailscale-manager.sh` | Instaluje i konfiguruje Tailscale w trybie GUI dialog/whiptail, interaktywnym CLI albo w pelni nieinteraktywnym silent. |
| `linux/nginx-manager.sh` | Bezpiecznie zarzadza Nginx, Virtual Hostami, reverse proxy, SSL, backupami i diagnostyka przez dialog lub CLI. |
| `linux/awx-inventory-sync.sh` | Wykrywa AWX w Kubernetes, konfiguruje logowanie SSH kluczem, importuje zdalne inventory Ansible i instaluje cykliczna synchronizacje. |
| `windows/install-wsl.ps1` | Wlacza WSL 2, instaluje Ubuntu, tworzy domyslne konto Linux `Chris` i mapuje jego HOME na katalog `Documents` aktualnego uzytkownika Windows. |
| `snow/watchdog-dashboard.js` | Dashboard watchdog dla ServiceNow uruchamiany w przegladarce. |

## Uzycie

Przed uruchomieniem przeczytaj kazdy skrypt, szczegolnie te, ktore instaluja pakiety, zmieniaja uslugi systemowe albo edytuja pliki w `/etc`.

Wiekszosc skryptow Linux uruchomisz przez Bash:

```bash
bash linux/detect_os.sh
bash linux/swam_mem_usage.sh
bash linux/ldapsearch.sh USER
```

Przyklady wyszukiwania LDAP:

```bash
bash linux/ldapsearch.sh krzysztof
bash linux/ldapsearch.sh --groups krzysztof
bash linux/ldapsearch.sh --netgroups krzysztof
bash linux/ldapsearch.sh --json krzysztof
bash linux/ldapsearch.sh --config
```

Skrypt LDAP korzysta z istniejacej konfiguracji LDAP/SSSD systemu. Nie wyswietla skonfigurowanych hasel bind i nie instaluje automatycznie pakietow klienta LDAP.

Przyklady Tailscale Manager:

```bash
sudo bash linux/tailscale-manager.sh --gui
sudo bash linux/tailscale-manager.sh --silent --install \
  --auth-key-file /root/tailscale.key --hostname server01 --ssh
sudo bash linux/tailscale-manager.sh --silent --diagnose
```

Tailscale Manager obsluguje Tailscale SSH, subnet routes, exit node, DNS/routes, tagi, status, diagnostyke, pliki konfiguracyjne i dry-run. Pelna dokumentacja znajduje sie w [`linux/TAILSCALE.md`](linux/TAILSCALE.md).

Przyklady Nginx Manager:

```bash
sudo bash linux/nginx-manager.sh
bash linux/nginx-manager.sh --status
sudo bash linux/nginx-manager.sh --non-interactive --backup
sudo bash linux/nginx-manager.sh --non-interactive --add-site \
  --domain example.com --root /var/www/example.com --port 80
```

Nginx Manager obsluguje Debian, Ubuntu, Linux Mint, RHEL, Rocky Linux, AlmaLinux, CentOS Stream i Fedore. Kazdy zapis konfiguracji przechodzi `nginx -t`; blad uruchamia rollback i blokuje reload. Pelna dokumentacja: [`linux/NGINX.md`](linux/NGINX.md).

Port HTTP strony lub domyslnego serwera mozna zmienic bez recznej edycji plikow:

```bash
sudo bash linux/nginx-manager.sh --non-interactive \
  --change-site-port --domain example.com --port 8080 --yes
sudo bash linux/nginx-manager.sh --non-interactive \
  --set-default-port --port 8080 --yes
```

Synchronizacje inventory AWX konfigurujesz interaktywnie na hoscie Kubernetes, na ktorym dziala AWX:

```bash
sudo bash linux/awx-inventory-sync.sh
```

Pierwsze uruchomienie pyta o host, port, uzytkownika i jednorazowe haslo SSH. Nastepnie generuje dedykowany klucz ED25519, wykrywa AWX, importuje inventory i instaluje cron co piec minut. Haslo SSH nie jest zapisywane.

Skrypty modyfikujace system zwykle wymagaja uprawnien root:

```bash
sudo bash linux/os_patching.sh
sudo bash linux/konfiguracja_samba_www_ubuntu.sh
sudo bash linux/web-hosts-file-manager.sh
```

Instalator WSL dla Windows uruchamiaj w PowerShell jako Administrator:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\windows\install-wsl.ps1
```

Domyslnie instaluje Ubuntu, tworzy konto Linux `Chris` z haslem `1`, ustawia je jako domyslnego uzytkownika WSL i ustawia HOME na `C:\Users\<uzytkownik Windows>\Documents` przez `/mnt/c/Users/<uzytkownik Windows>/Documents`. Jesli Windows wymaga restartu przed dokonczeniem instalacji dystrybucji, po restarcie uruchom ten sam skrypt ponownie.

Skrypt dashboardu ServiceNow jest przeznaczony do uruchomienia w zalogowanej sesji ServiceNow jako snippet w przegladarce, userscript albo bookmarklet.

## Uwagi

- Testuj skrypty w bezpiecznym srodowisku przed uzyciem na produkcji.
- Dostosuj wartosci konfiguracyjne w skryptach, ktore wymagaja lokalnych ustawien.
- Czesc skryptow jest przeznaczona dla konkretnych dystrybucji; najpierw sprawdz komentarze i obslugiwane systemy.
- `linux/ldapsearch.sh` obsluguje wykrywanie konfiguracji SSSD, OpenLDAP i nslcd oraz fallback NSS/SSSD, gdy bezposrednie zapytanie LDAP jest niedostepne.
- `linux/tailscale-manager.sh` nie zapisuje auth key do zapisywanego configu; preferowany jest `--auth-key-file` z uprawnieniami `600` lub `400`.
