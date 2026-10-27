# Sprawdzanie hostow w Ansible Tower

Skrypt `check_hosts.py` wymaga Pythona 3.9 lub nowszego. Nie wymaga instalowania bibliotek.
W pliku zmien liste na swoje hosty:

```python
HOSTS = "CXCX CXC cxzczxc "
```

Spacje, tabulatory i nowe linie rozdzielaja hosty. Powtorzenia sa pomijane.
Skrypt porownuje pelne nazwy hostow bez rozrozniania wielkosci liter.
Nie porownuje adresow `ansible_host` ze zmiennych ani nazw skroconych z FQDN.

Uruchomienie w PowerShell, z katalogu `ansibletower`:

```powershell
$env:TOWER_URL = "https://tower.example.com"
python check_hosts.py
```

Skrypt poprosi o token API (wpisywane znaki sa ukryte). Mozesz tez ustawic
zmienna srodowiskowa `TOWER_TOKEN`, np. na potrzeby automatyzacji.
Token musi miec uprawnienia do odczytu odpowiednich inventory i hostow.

Liste mozna podac bez edytowania pliku; adres mozna podac przez `--url`:

```powershell
python check_hosts.py --hosts "CXCX CXC cxzczxc "
python check_hosts.py --hosts "host01 host02" --inventory 42
python check_hosts.py --url https://tower.example.com --ca-cert C:\certyfikaty\firma-ca.pem
```

Domyslnie przeszukiwane sa wszystkie hosty widoczne dla tokenu.
`--inventory` ogranicza wyszukiwanie do podanego identyfikatora inventory.
`BRAK` oznacza brak hosta w wynikach widocznych dla tokenu w wybranym zakresie.
Weryfikacja TLS jest wlaczona; `--ca-cert` pozwala zaufac firmowemu CA.
Limit czasu pojedynczego zapytania wynosi 15 sekund, mozna go zmienic przez `--timeout`.

Przykladowy wynik:

```text
ISTNIEJE: CXCX
BRAK: CXC
ISTNIEJE: cxzczxc
Podsumowanie: sprawdzono 3, istnieje 2, brak 1.
```

Kody zakonczenia: `0` - wszystkie hosty istnieja, `1` - co najmniej jednego brakuje,
`2` - blad konfiguracji/API/polaczenia lub przerwanie. Blad API przerywa sprawdzanie;
nie jest traktowany jako brak hosta.

Skrypt wykonuje tylko zapytania GET do `/api/v2/hosts/` w Ansible Tower.
Uzywa filtra `name__iexact` oraz pola `count`, wiec wystarczy jedna strona na hosta.
Dokumentacja: [filtrowanie API](https://docs.ansible.com/ansible-tower/2.4.3/html/towerapi/intro.html#filtering),
[uwierzytelnianie tokenem](https://origin-www.ansible.com/blog/summary-of-authentication-methods-in-red-hat-ansible-tower/).

Testy lokalne (bez dostepu do Tower):

```powershell
python -m unittest discover -s tests -v
```
