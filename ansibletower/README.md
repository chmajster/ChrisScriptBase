# Skrypty do hostow w Ansible Tower

Skrypt `check_hosts.py` wymaga Pythona 3.9 lub nowszego. Nie wymaga instalowania bibliotek.
W pliku zmien liste na swoje hosty:

```python
HOSTS = "CXCX CXC cxzczxc "
```

Spacje, tabulatory i nowe linie rozdzielaja hosty. Powtorzenia sa pomijane.
Skrypt porownuje pelne nazwy hostow bez rozrozniania wielkosci liter.
Nie porownuje adresow `ansible_host` ze zmiennych ani nazw skroconych z FQDN.

Wspolne dane uwierzytelniania sa w lokalnym pliku `tower_credentials.json`,
obok modulu `tower_auth.py`. Uzupelnij go adresem Tower oraz tokenem API:

```json
{
  "url": "https://tower.example.com",
  "token": "YOUR_API_TOKEN"
}
```

Plik `tower_credentials.json` jest ignorowany przez Git. W repozytorium jest
szablon `tower_credentials.example.json`; po sklonowaniu utworz lokalna kopie:

```powershell
Copy-Item tower_credentials.example.json tower_credentials.json
```

Po uzupelnieniu pliku uruchom z katalogu `ansibletower`:

```powershell
python check_hosts.py
```

Jesli nie wpiszesz tokenu w pliku, skrypt poprosi o niego (znaki sa ukryte).
Zmienne srodowiskowe `TOWER_URL` i `TOWER_TOKEN` maja pierwszenstwo przed plikiem.
Opcja `--url` ma pierwszenstwo przed zmienna `TOWER_URL`.
Token musi miec uprawnienia do odczytu odpowiednich inventory i hostow.

Inne skrypty Pythona w tym katalogu moga uzywac tego samego modulu:

```python
from tower_auth import load_credentials

credentials = load_credentials()
tower_url = credentials.url
headers = {"Authorization": "Bearer " + credentials.token}
```

Import nie wykonuje logowania ani zapytan sieciowych. Plik jest odczytywany
wzgledem polozenia `tower_auth.py`, niezaleznie od katalogu uruchomienia.
`load_credentials(prompt=False)` wylacza pytanie o token w automatyzacji.
Token nie jest pokazywany w reprezentacji obiektu `credentials`.

Liste mozna podac bez edytowania pliku; adres mozna podac przez `--url`:

```powershell
python check_hosts.py --hosts "CXCX CXC cxzczxc "
python check_hosts.py --hosts "host01 host02" --inventory 42
python check_hosts.py --url https://tower.example.com --ca-cert C:\certyfikaty\firma-ca.pem
```

Domyslnie przeszukiwane sa wszystkie hosty widoczne dla tokenu.
`--inventory` ogranicza wyszukiwanie do podanego identyfikatora inventory.
Wszystkie komunikaty skryptu sa w jezyku angielskim.
`MISSING` oznacza brak hosta w wynikach widocznych dla tokenu w wybranym zakresie.
Weryfikacja TLS jest wlaczona; `--ca-cert` pozwala zaufac firmowemu CA.
Limit czasu pojedynczego zapytania wynosi 15 sekund, mozna go zmienic przez `--timeout`.

Przykladowy wynik:

```text
EXISTS: CXCX
MISSING: CXC
EXISTS: cxzczxc
Summary: checked 3, exists 2, missing 1.
```

Kody zakonczenia: `0` - wszystkie hosty istnieja, `1` - co najmniej jednego brakuje,
`2` - blad konfiguracji/API/polaczenia lub przerwanie. Blad API przerywa sprawdzanie;
nie jest traktowany jako brak hosta.

Skrypt wykonuje tylko zapytania GET do `/api/v2/hosts/` w Ansible Tower.
Uzywa filtra `name__iexact` oraz pola `count`, wiec wystarczy jedna strona na hosta.
Dokumentacja: [filtrowanie API](https://docs.ansible.com/ansible-tower/2.4.3/html/towerapi/intro.html#filtering),
[uwierzytelnianie tokenem](https://origin-www.ansible.com/blog/summary-of-authentication-methods-in-red-hat-ansible-tower/).

## Dodawanie hostow

`add_hosts.py` korzysta z tego samego `tower_auth.py` i `tower_credentials.json`.
Token musi miec uprawnienia do odczytu oraz tworzenia hostow w docelowym inventory.
Podaj identyfikator istniejacego inventory oraz hosty rozdzielone bialymi znakami:

```powershell
python add_hosts.py --inventory 42 --hosts "host01 host02 host03"
```

Mozesz tez wkleic liste do stalej `HOSTS` w pliku `add_hosts.py`, a potem uruchomic:

```powershell
python add_hosts.py --inventory 42
```

Lista w nowym skrypcie jest domyslnie pusta; przed uruchomieniem trzeba podac hosty.
Kazdy host jest sprawdzany w docelowym inventory bez rozrozniania wielkosci liter.
Powtorzenia na liscie oraz hosty juz obecne sa pomijane. Istniejace hosty nie sa
modyfikowane. Nowe hosty sa wlaczone (`enabled: true`); skrypt nie dodaje ich do grup.
Host w innym inventory nie blokuje utworzenia hosta w docelowym inventory.

Podglad bez zapisu (wykonuje zapytania odczytujace do Tower):

```powershell
python add_hosts.py --inventory 42 --hosts "host01 host02" --dry-run
```

Opcje `--url`, `--ca-cert` i `--timeout` dzialaja tak samo jak w `check_hosts.py`.
Wynik jest po angielsku, np.:

```text
SKIPPED (already exists): host01
ADDED: host02 (ID 123)
Summary: processed 2, added 1, skipped 1.
```

Kod zakonczenia `0` oznacza powodzenie (takze gdy wszystkie hosty juz istnieja),
`2` oznacza blad lub przerwanie. Blad przerywa dalsze dodawanie; wczesniej utworzone
hosty pozostaja w Tower. Po zerwaniu polaczenia podczas POST wynik moze byc nieznany:
sprawdz inventory przed ponownym uruchomieniem. Skrypt nie ponawia automatycznie POST.

Tworzenie hosta korzysta z `POST /api/v2/hosts/`, a sprawdzanie z filtrowanego GET.
Dokumentacja: [referencja API AWX/Tower](https://docs.ansible.com/projects/awx/en/latest/rest_api/api_ref.html).

## Testy

Testy lokalne wszystkich skryptow (bez dostepu do Tower):

```powershell
python -m unittest discover -s tests -v
```
