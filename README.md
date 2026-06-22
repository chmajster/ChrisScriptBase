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
| `snow/watchdog-dashboard.js` | Browser-based ServiceNow watchdog dashboard snippet. |

## Usage

Review each script before running it, especially scripts that install packages, change system services, or edit files under `/etc`.

Most Linux scripts should be run with Bash:

```bash
bash linux/detect_os.sh
bash linux/swam_mem_usage.sh
```

Scripts that modify the system usually require root privileges:

```bash
sudo bash linux/os_patching.sh
sudo bash linux/konfiguracja_samba_www_ubuntu.sh
sudo bash linux/web-hosts-file-manager.sh
```

The ServiceNow dashboard script is intended to run inside an authenticated ServiceNow browser session as a browser snippet, userscript, or bookmarklet.

## Notes

- Test scripts in a safe environment before using them on production systems.
- Adjust configuration values inside the scripts before running them.
- Some scripts are distribution-specific; check comments and detected OS support first.

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
| `snow/watchdog-dashboard.js` | Dashboard watchdog dla ServiceNow uruchamiany w przegladarce. |

## Uzycie

Przed uruchomieniem przeczytaj kazdy skrypt, szczegolnie te, ktore instaluja pakiety, zmieniaja uslugi systemowe albo edytuja pliki w `/etc`.

Wiekszosc skryptow Linux uruchomisz przez Bash:

```bash
bash linux/detect_os.sh
bash linux/swam_mem_usage.sh
```

Skrypty modyfikujace system zwykle wymagaja uprawnien root:

```bash
sudo bash linux/os_patching.sh
sudo bash linux/konfiguracja_samba_www_ubuntu.sh
sudo bash linux/web-hosts-file-manager.sh
```

Skrypt dashboardu ServiceNow jest przeznaczony do uruchomienia w zalogowanej sesji ServiceNow jako snippet w przegladarce, userscript albo bookmarklet.

## Uwagi

- Testuj skrypty w bezpiecznym srodowisku przed uzyciem na produkcji.
- Dostosuj wartosci konfiguracyjne w skryptach przed uruchomieniem.
- Czesc skryptow jest przeznaczona dla konkretnych dystrybucji; najpierw sprawdz komentarze i obslugiwane systemy.
