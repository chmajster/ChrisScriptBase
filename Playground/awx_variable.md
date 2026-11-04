# Konfiguracja skryptów AWX — awx_variable

Dokument opisuje plik `awx_variable` i zachowanie skryptów w tym katalogu. Przykłady odnoszą się do bieżącej implementacji, w tym grupowania jobów, weryfikacji OS, ankiet i arkusza `Ustawienia`.

Adres `https://awx.example.com`, identyfikatory szablonów i przykładowe nazwy credentiali trzeba zastąpić wartościami ze swojego AWX. Rzeczywisty token z lokalnej konfiguracji nie został skopiowany do dokumentacji.

Fragmenty konfiguracji pokazują zmiany w istniejących ustawieniach. Zastępuj odpowiednie przypisanie; nie dopisuj drugiego przypisania tej samej zmiennej. Kompletny przykład znajduje się w [sekcji 16](#16-kompletny-przykład-konfiguracji).

## 1. Do czego służy plik

`awx_variable` jest wspólną konfiguracją pięciu skryptów:

| Skrypt | Zadanie | Co zapisuje |
| --- | --- | --- |
| `awx_inventory_hosts.py` | Pobiera hosty z wybranego inventory, ich OS, grupy i metadane. Przygotowuje nowy eksport. | JSON i nowy Excel, w tym arkusz `Ustawienia`. |
| `awx_refresh_excel.py` | Odnajduje hosty z Excela w AWX, dobiera szablony i odświeża dane. | Aktualizuje ten sam Excel, zachowując historię jobów, wyniki i inne arkusze. |
| `awx_execute_jobs.py` | Buduje paczki według parametrów z Excela, sprawdza wymagania i uruchamia joby. | Linki jobów i dostępne artefakty w tym samym Excelu. `--dry-run` nie zapisuje pliku. |
| `awx_job_output.py` | Pobiera stdout i artefakty jobów wskazanych w Excelu. | Logi w Excelu oraz pełne pliki TXT. |
| `awx_find_templates.py` | Wyszukuje szablony w organizacji AWX według wzorca nazwy. | Wyniki w terminalu; nie uruchamia jobów i nie zapisuje Excela. |

Typowa kolejność pracy:

1. Eksport hostów do Excela.
2. Odświeżenie i automatyczny dobór szablonów, jeżeli eksport nie przypisał ich przez `OS_JOBS` lub `JOB_URL`.
3. Sprawdzenie wierszy, credentiali i odpowiedzi survey w Excelu.
4. Podgląd paczek przez `--dry-run`.
5. Uruchomienie jobów.
6. Pobranie logów i końcowych artefaktów.

`inventory` tworzy Excel od nowa. Do zachowania istniejącej historii używaj `refresh`.

## 2. Jak czytana jest konfiguracja

### Lokalizacja i ścieżki

Skrypty szukają pliku `awx_variable` obok launcherów, niezależnie od katalogu, z którego uruchomiono Pythona.

Ścieżki zapisane **wewnątrz konfiguracji** są natomiast względne wobec bieżącego katalogu uruchomienia. Przykład:

```python
EXCEL_OUTPUT_FILE = 'awx_inventory_hosts.xlsx'
OUTPUT_DIRECTORY = 'output'
```

Przy uruchomieniu z katalogu `Playground` wyniki trafią do tego katalogu i podkatalogu `output`. Dla stałej lokalizacji można podać ścieżki bezwzględne:

```python
EXCEL_OUTPUT_FILE = r'C:\AWX\awx_inventory_hosts.xlsx'
OUTPUT_DIRECTORY = r'C:\AWX\output'
```

### Priorytety ustawień

Dla wybranej operacji obowiązuje kolejność od najwyższego priorytetu:

1. Argument komendy, jeżeli dana opcja jest obsługiwana.
2. Słownik `VARIABLES` w uruchamianym skrypcie.
3. Słownik `*_OVERRIDES` tej operacji w `awx_variable`.
4. Wspólne ustawienie z `awx_variable`.

Przykład we wspólnym pliku:

```python
BATCH_SIZE = 10
EXECUTE_OVERRIDES = {
    'BATCH_SIZE': 5,
}
```

Przykład w `awx_execute_jobs.py`:

```python
VARIABLES = {
    'BATCH_SIZE': 3,
}
```

Komenda:

```powershell
python .\awx_execute_jobs.py --batch-size 2 --dry-run
```

Efektywny `BATCH_SIZE` wyniesie `2`. Bez argumentu będzie `3`, bez lokalnego nadpisania `5`, a bez nadpisania operacji `10`.

To jest priorytet **konfiguracji**. Wartości zapisane w Excelu i odpowiedzi survey mają dodatkowe reguły opisane w sekcjach o szablonach i ankietach. Na przykład `EXECUTE_OVERRIDES['SURVEY_ANSWERS']` nadal nie nadpisuje wypełnionej komórki `Survey_...`.

### Łączenie słowników i zastępowanie list

Słowniki konfiguracji są łączone rekurencyjnie. Listy i pojedyncze wartości są zastępowane.

```python
JOB_VARIABLE_OVERRIDES_BY_OS = {
    'linux': {
        'action': 'install',
        'settings': {'retries': 2, 'timeout': 30},
    },
}

REFRESH_OVERRIDES = {
    'JOB_VARIABLE_OVERRIDES_BY_OS': {
        'linux': {
            'settings': {'retries': 5},
        },
    },
}
```

Efekt dla Linuxa: `action='install'`, `settings.retries=5` i `settings.timeout=30`.

Pusty słownik w nadpisaniu nie usuwa kluczy odziedziczonych ze wspólnego słownika. Przykładowo `REFRESH_OVERRIDES={'JOB_VARIABLE_OVERRIDES_BY_OS': {}}` nie wyczyści wspólnych nadpisań. Aby ich nie stosować, usuń odpowiednie wpisy z konfiguracji wspólnej albo ustaw ją jako `{}` bez innych nadpisań.

Pusta lista może zastąpić wcześniejszą listę, np.:

```python
EXTRA_CREDENTIAL_VARIABLES = ['shared-vault']
INVENTORY_OVERRIDES = {
    'EXTRA_CREDENTIAL_VARIABLES': [],
}
```

Dla eksportu zostanie użyta lista `[]` i credentiale z szablonu.

### Składnia

Plik używa przypisań podobnych do Pythona:

```python
DEBUG = False
BATCH_SIZE = 10
INVENTORY = 'it2_inventory'
HOSTS = ['host1', 'host2']
INPUT_FILE = None
```

Zasady:

- Tekst zapisuj w cudzysłowach lub apostrofach.
- W ustawieniach Pythona używaj `True`, `False` i `None`.
- Komentarze zaczynają się od `#`.
- W słownikach i listach oddzielaj elementy przecinkami.
- Nazwy ustawień zapisuj w pokazanej postaci, np. `BATCH_SIZE`.
- Nie powtarzaj ustawienia na poziomie głównym.
- Nieznana nazwa ustawienia powoduje błąd.
- Parser odczytuje literały; nie wykonuje funkcji, importów ani wyrażeń typu `os.getenv(...)` czy `5 * 60`. Wpisz gotową wartość, np. `300`.
- Brak przypisania zwykle daje `None`, a nie uniwersalną wartość domyślną. Część ustawień ma własne normalizacje, ale wymagane liczby, nazwy i flagi trzeba ustawić poprawnie.

Cały plik nie jest YAML-em. Specjalna obsługa YAML dotyczy obiektów pod kluczem `variables`, co pokazano w sekcji `OS_JOBS`.

## 3. Co oznacza obecna konfiguracja

Poniżej stan istotnych ustawień z lokalnego `awx_variable` w chwili przygotowania opisu. Są to wartości wspólne, przed ewentualnymi nadpisaniami launcherów i komendy.

| Ustawienie | Obecna wartość / skutek |
| --- | --- |
| `AWX_URL` | Adres lokalnego serwera AWX wskazany w pliku. |
| `AWX_TOKEN` | Skonfigurowany; wartość pominięta w dokumentacji. |
| `INVENTORY` | `'it2_inventory'`. |
| `TIMEOUT` / `PAGE_SIZE` | `30` sekund / `200` rekordów na stronę. |
| `CREDENTIAL_VARIABLES` | `['os_credential']`. |
| `EXTRA_CREDENTIAL_VARIABLES` | `[]`, czyli źródłem dodatków jest szablon. |
| `FULL_CREDENTIAL_SEPARATOR` | `', '`. |
| `OUTPUT_FILE` | `'awx_inventory_hosts.json'`. |
| `EXCEL_OUTPUT_FILE` / `INPUT_FILE` | `'awx_inventory_hosts.xlsx'` / `None`. |
| `SHEET_NAME` / `EXCEL_COLUMNS` | `'Results'` / `[]`, czyli wszystkie kolumny widoczne. |
| `REFRESH_INVENTORY_PATTERN` | `'*_mirror_inventory'`. |
| `JOB_TEMPLATE_NAME_PATTERN` | `'*hello*{os_name}*'`. |
| `JOB_TEMPLATE_OS_NAMES` | Linux → `unix`, AIX → `aix`, Windows → `window`. |
| `JOB_VARIABLE_OVERRIDES_BY_OS` | Puste słowniki dla Windows, Linux i AIX; bez zmian zmiennych z AWX. |
| `HOST_COLUMN` / `HOSTS` | `None` / `[]`. |
| `BATCH_SIZE` | `10` hostów na job, w ramach zgodnej grupy parametrów. |
| `EXECUTION_COLUMN` | `'exceiute_job'`. |
| `JOB_TYPE` / `JOB_TYPE_VARIABLE` | `''` / `'action'`. |
| `SURVEY_EXCEL_DEFAULTS` | `{'installtesttypename': 'testads'}`. |
| `SURVEY_ANSWERS` | `{'installtesttypename': 'reinstall'}`. |
| `SURVEY_INTERACTIVE` | `False`. |
| `WAIT_FOR_COMPLETION` | `False`: execute nie czeka na zakończenie jobów. |
| `POLL_INTERVAL` / `JOB_WAIT_TIMEOUT` | `5` sekund / `3600` sekund. |
| `OUTPUT_COLUMN` / `OUTPUT_DIRECTORY` | `'output'` / `'output'`. |
| `DEBUG` / `DRY_RUN` / `COLOR` | `False` / `False` / `'auto'`. |
| Pięć słowników `*_OVERRIDES` | Wszystkie są puste. |

W pliku nie ma aktywnych przypisań `OS_JOBS` i `JOB_URL`. Obecna implementacja traktuje brak `OS_JOBS` jak `{}`, a brak `JOB_URL` jak pusty URL. Sam eksport nie wybierze wówczas szablonu dla hosta: `template_job_name` i `Job_Link` mogą mieć wartość `none`. Szablony można później dobrać przez refresh lub skonfigurować jawnie.

`JOB_TEMPLATE_AUTO_DISCOVERY` i `JOB_TEMPLATE_ORGANIZATION` także nie są obecnie przypisane. Ich znaczenie opisano w sekcji 7.

Różnica `testads` / `reinstall` w ustawieniach survey jest istotna: komórka Excela wypełniona przez `SURVEY_EXCEL_DEFAULTS` ma przy wykonaniu wyższy priorytet niż `SURVEY_ANSWERS`.

## 4. Połączenie i pobieranie danych

### AWX_URL

**Typ:** niepusty tekst. **Użycie:** wszystkie operacje korzystające z AWX.

Adres serwera lub API v2:

```python
AWX_URL = 'https://awx.example.com'
```

Można podać też bazę API, np.:

```python
AWX_URL = 'https://awx.example.com/api/v2/'
```

Klient obsługuje również bazę API controllera, gdy taki adres jest podany w konfiguracji. Adresy `Job_Link`, jawne `--job-url` i linki jobów muszą wskazywać ten sam serwer co efektywny `AWX_URL`: zgodny protokół, host i port.

Przykład: `http://awx.example.com` i `https://awx.example.com` są różnymi serwerami w tej walidacji. Podobnie adres bez portu i adres z niestandardowym portem.

Parser potrafi połączyć fizyczne nowe linie w literałach URL. Dotyczy to `AWX_URL`, `JOB_URL` i `url` w `OS_JOBS`. Najczytelniej zapisywać adres w jednej linii.

### AWX_TOKEN

**Typ:** niepusty tekst. **Użycie:** uwierzytelnianie API.

```python
AWX_TOKEN = 'WSTAW_WLASNY_TOKEN_AWX'
```

Token określa, do jakich obiektów użytkownik ma dostęp i jakie operacje może wykonać. Odczyt inventory, hostów, grup, organizacji, szablonów, credentiali i wyników wymaga odpowiednich uprawnień. Execute potrzebuje także uprawnień do uruchamiania wskazanego szablonu.

`AWX_TOKEN` nie wybiera credentiali dla playbooka. Do tego służą szablon AWX i `Full_Credential`.

Token jest pomijany w arkuszu `Ustawienia` i maskowany w diagnostyce API. W przykładach dokumentacji stosuj wartość zastępczą.

### INVENTORY

**Typ:** dokładna nazwa inventory jako tekst albo dodatnie ID jako liczba.

```python
INVENTORY = 'it2_inventory'
```

Wariant z ID:

```python
INVENTORY = 10
```

Liczbowe ID zapisz bez cudzysłowów. Dla wyszukiwania inventory tekst jest traktowany jako nazwa.

Znaczenie zależy od operacji:

- Eksport pobiera hosty z tego inventory.
- Execute korzysta z niego przy odszukiwaniu i dopisywaniu hostów podanych przez `HOSTS` / `--hosts`, których nie ma w Excelu.
- Rzeczywista weryfikacja hostów przed startem joba używa inventory przypisanego do **szablonu AWX**.
- Refresh ignoruje to ustawienie przy wyszukiwaniu hostów i stosuje `REFRESH_INVENTORY_PATTERN`.
- Wyszukiwanie szablonów z `--hostname` także korzysta ze wzorca inventory.

Nawet operacje korzystające głównie z Excela mogą walidować wspólną konfigurację połączenia. Pozostaw tu poprawną nazwę lub ID.

### TIMEOUT

**Typ:** dodatnia, skończona liczba sekund. **Obecnie:** `30`.

```python
TIMEOUT = 60
```

Limit pojedynczego żądania HTTP. Nie jest limitem czasu działania joba. Długi job kontroluje `JOB_WAIT_TIMEOUT`, jeżeli włączono oczekiwanie.

### PAGE_SIZE

**Typ:** dodatnia liczba całkowita. **Obecnie:** `200`.

```python
PAGE_SIZE = 100
```

Rozmiar strony w zapytaniach listujących obiekty AWX. Nie ogranicza łącznej liczby pobranych hostów: klient przechodzi przez kolejne strony. Nie określa też liczby hostów w jobie; do tego służy `BATCH_SIZE`.

## 5. Pliki, arkusze i widoczne kolumny

### OUTPUT_FILE

**Typ:** niepusta ścieżka. **Użycie:** eksport inventory.

```python
OUTPUT_FILE = 'awx_inventory_hosts.json'
```

Plik JSON z raportem hostów i ich metadanymi. Eksport zapisuje go ponownie. Refresh, execute i pobieranie logów nie używają go jako głównego źródła danych — pracują na Excelu.

### EXCEL_OUTPUT_FILE

**Typ:** niepusta ścieżka do pliku wynikowego, zalecane rozszerzenie `.xlsx`.

```python
EXCEL_OUTPUT_FILE = 'awx_inventory_hosts.xlsx'
```

Eksport tworzy pod tą ścieżką nowy workbook. W pozostałych operacjach jest to ścieżka zapasowa, gdy nie wskazano `--file` ani `INPUT_FILE`.

### INPUT_FILE

**Typ:** `None` albo niepusty tekst ze ścieżką. **CLI:** `-file` / `--file`.

```python
INPUT_FILE = None
```

| Operacja | `None` | Wskazana ścieżka |
| --- | --- | --- |
| Eksport | Wszystkie hosty z `INVENTORY`. | Excel z kolumną `host_limit`, używany jako filtr hostów. |
| Refresh | Aktualizuje `EXCEL_OUTPUT_FILE`. | Aktualizuje wskazany Excel w miejscu. |
| Execute | Czyta `EXCEL_OUTPUT_FILE`. | Czyta i aktualizuje wskazany Excel. |
| Output | Czyta `EXCEL_OUTPUT_FILE`. | Czyta i aktualizuje wskazany Excel. |

Dla eksportu plik filtra i plik wynikowy muszą mieć różne ścieżki.

Przykład filtra:

| host_limit |
| --- |
| host1 |
| host2,host3 |

Eksport rozdziela nazwy z `host_limit` po przecinkach, średnikach lub białych znakach. Po filtracji nadal pobiera dane z AWX.

```powershell
python .\awx_inventory_hosts.py --file .\lista_hostow.xlsx
python .\awx_refresh_excel.py --file .\awx_inventory_hosts.xlsx
python .\awx_execute_jobs.py --file .\awx_inventory_hosts.xlsx --dry-run
```

Przy execute brak pliku jest błędem, jeżeli nie wybrano hostów. Przy `HOSTS` / `--hosts` skrypt może przygotować nowy workbook na podstawie AWX. W `--dry-run` taki nowy plik nie jest zapisywany.

### SHEET_NAME

**Typ:** niepusta nazwa arkusza. **Obecnie:** `'Results'`. **CLI:** `--sheet` dla refresh, execute i output.

```python
SHEET_NAME = 'Results'
```

Eksport tworzy arkusz hostów o tej nazwie. Przy otwieraniu istniejącego pliku skrypt wybiera ten arkusz, a jeśli go nie ma — aktywny arkusz.

Jawne `--sheet` wymaga, aby wskazany arkusz istniał. Nie oznacza automatycznego tworzenia go w istniejącym workbooku.

```powershell
python .\awx_execute_jobs.py --file .\hosty.xlsx --sheet Results --dry-run
```

Nazwa `Ustawienia` jest zarezerwowana dla informacyjnego arkusza eksportu.

### EXCEL_COLUMNS

**Typ:** lista nazw kolumn; `[]` i `None` oznaczają wszystkie widoczne.

```python
EXCEL_COLUMNS = [
    'hostname',
    'AWX_Status',
    'ostype',
    'exceiute_job',
    'output',
    'Survey_installtesttypename',
    'Uwagi',
]
```

Efekty:

- Wymienione kolumny są widoczne.
- Pozostałe kolumny są ukryte, ale zachowują dane i nadal służą do wykonania jobów.
- Własna brakująca kolumna, np. `Uwagi`, jest dodawana jako pusta.
- Nazwy porównywane są bez rozróżniania wielkości liter.
- Powtórzona nazwa jest błędem.
- Lista steruje widocznością, a nie kolejnością kolumn.

Standardowy układ zaczyna się od `hostname → AWX_Status → INVENTORY`. Lista nie przenosi kolumn w podanej przez użytkownika kolejności i nie ukrywa samego arkusza `Ustawienia`.

Działa przy zapisie przez eksport, refresh, execute i output. `--dry-run` nie zapisuje zmian widoczności.

## 6. Hosty i wyszukiwanie inventory

### REFRESH_INVENTORY_PATTERN

**Typ:** niepusty wzorzec nazw inventory. **Obecnie:** `'*_mirror_inventory'`.

```python
REFRESH_INVENTORY_PATTERN = '*_mirror_inventory'
```

Używany przez refresh oraz wyszukiwanie szablonów z `--hostname`. Dopasowanie ignoruje wielkość liter.

| Wzorzec | Przykładowe dopasowania |
| --- | --- |
| `*_mirror_inventory` | `it2_mirror_inventory`, `app1_mirror_inventory`. |
| `it2_*` | Inventory zaczynające się od `it2_`. |
| `app?_mirror_inventory` | Np. `app1_mirror_inventory`; `?` oznacza jeden znak. |
| `it2_inventory` | Tylko inventory o tej nazwie. |

`*` oznacza dowolny ciąg znaków. To wzorzec nazw, nie dowolne wyrażenie regularne.

Refresh:

1. Odczytuje nazwy hostów z Excela.
2. Szuka ich w inventory pasujących do wzorca.
3. Weryfikuje pełną nazwę hosta bez rozróżniania wielkości liter.
4. Zapisuje faktyczne `INVENTORY` i wylicza `ApplicationID`.
5. Jeżeli ten sam host występuje w kilku pasujących inventory, zgłasza niejednoznaczność i nie zapisuje Excela.

Przykład:

`it2_mirror_inventory` → `ApplicationID = 'it2'`.

Jeżeli w środowisku jest tylko `it2_inventory`, zmień wzorzec na tę nazwę zamiast oczekiwać dopasowania do `*_mirror_inventory`.

### HOST_COLUMN

**Typ:** `None` lub nazwa kolumny. **Użycie:** refresh. **CLI:** `--host-column`.

```python
HOST_COLUMN = None
```

Automatyczne rozpoznawanie sprawdza kolejno:

1. `hostname`
2. `host_limit`
3. `host`
4. `hosts`

Własny nagłówek:

```python
HOST_COLUMN = 'Nazwa_serwera'
```

```powershell
python .\awx_refresh_excel.py --file .\hosty.xlsx --host-column Nazwa_serwera
```

Refresh oczekuje jednej nazwy hosta na wiersz. Usuwa przecinki z nazwy, np. `server1,` staje się `server1`; nie dzieli takiej komórki na kilka hostów jak filtr eksportu `host_limit`. Formuły i wzorce Ansible w nazwie hosta są odrzucane.

### HOSTS

**Typ:** lista nazw lub tekst rozdzielony przecinkami/białymi znakami. **Użycie:** execute. **CLI:** `--hosts` / `--host`.

Wszystkie hosty z Excela:

```python
HOSTS = []
```

Wybrane hosty:

```python
HOSTS = ['host1', 'host2']
```

Alternatywny zapis:

```python
HOSTS = 'host1, host2'
```

```powershell
python .\awx_execute_jobs.py --hosts host1 host2 --dry-run
```

Wybór nie rozróżnia wielkości liter i usuwa powtórzenia nazw. Nazwy muszą być literalne; `all` jako wzorzec Ansible, `web*` czy `group:&linux` nie służą tu do wyboru grup. Tekst `all` jest traktowany jako dosłowna nazwa hosta.

Hosty nieobecne w Excelu są odszukiwane w `INVENTORY` i dopisywane do arkusza. To oddzielne ustawienie od `HOST_COLUMN` i filtra eksportu.

## 7. Szablony jobów i zmienne zależne od OS

### Trzy sposoby wyboru szablonu

| Operacja | Źródło szablonu |
| --- | --- |
| Eksport | Niepusty `JOB_URL`; w przeciwnym razie `OS_JOBS[ostype]['url']`. |
| Refresh | Automatyczne wyszukiwanie według wzorca w organizacji równej `ApplicationID`. |
| Execute | `Job_Link` z konkretnego wiersza Excela; jawne `--job-url` może go nadpisać. |

Globalny `JOB_URL` nie zastępuje istniejących `Job_Link` podczas normalnego execute. Refresh ignoruje ręczny wybór `JOB_URL`, `OS_JOBS` i `JOB_TEMPLATE_ORGANIZATION` dla doboru szablonu hosta.

### JOB_TEMPLATE_NAME_PATTERN

**Typ:** niepusty wzorzec fragmentów nazwy. **Obecnie:** `'*hello*{os_name}*'`.

```python
JOB_TEMPLATE_NAME_PATTERN = '*hello*{os_name}*'
```

Wszystkie fragmenty oddzielone `*` lub spacjami muszą wystąpić w nazwie szablonu, w dowolnej kolejności i bez rozróżniania wielkości liter. `?` dopasowuje jeden znak we fragmencie.

Dla Linuxa i mapowania `linux → unix` wzorzec wymaga fragmentów `hello` i `unix`. Przykładowo pasują zarówno `Hello Unix`, jak i `Unix Hello job`.

Obsługiwane pola:

| Pole | Znaczenie |
| --- | --- |
| `{os_name}` | Fragment nazwy OS z `JOB_TEMPLATE_OS_NAMES`, np. `unix`. |
| `{ostype}` | Znormalizowany OS, np. `linux` lub `windows`. |
| `{hostname}` | Nazwa hosta. |
| `{application_id}` | ApplicationID hosta. |

Przykład dokładniejszego wzorca:

```python
JOB_TEMPLATE_NAME_PATTERN = '*agent*install*{application_id}*{os_name}*'
```

Pola wymagające hosta nie działają przy ogólnym wyszukiwaniu bez `--hostname`. Odświeżenie hosta wymaga dokładnie jednego pasującego szablonu; zero lub kilka dopasowań zatrzymuje zapis.

### JOB_TEMPLATE_OS_NAMES

**Typ:** niepusty słownik: OS → fragment nazwy szablonu.

```python
JOB_TEMPLATE_OS_NAMES = {
    'linux': 'unix',
    'aix': 'aix',
    'windows': 'window',
}
```

Nie zmienia rzeczywistego OS hosta. Określa tekst podstawiany za `{os_name}`.

Jeżeli nazwy szablonów zawierają `Linux` i `Windows` zamiast `Unix` i `Window`, można użyć:

```python
JOB_TEMPLATE_OS_NAMES = {
    'linux': 'linux',
    'aix': 'aix',
    'windows': 'windows',
}
```

W metadanych hostów `window` i `windows` są normalizowane do `windows`. Wielkość liter i otaczające spacje w OS są normalizowane.

### JOB_VARIABLE_OVERRIDES_BY_OS

**Typ:** słownik OS → obiekt zmiennych. **Użycie:** refresh po pobraniu `extra_vars` szablonu.

```python
JOB_VARIABLE_OVERRIDES_BY_OS = {
    'windows': {'action': 'install'},
    'linux': {'action': 'reinstall', 'settings': {'retries': 3}},
    'aix': {},
}
```

Przykładowe dane z szablonu AWX:

```json
{
  "action": "update",
  "settings": {"retries": 1, "timeout": 30},
  "packages": ["agent"]
}
```

Wynik po nadpisaniu dla Linuxa:

```json
{
  "action": "reinstall",
  "settings": {"retries": 3, "timeout": 30},
  "packages": ["agent"]
}
```

Słowniki są łączone rekurencyjnie, listy zastępowane. Niewymienione wartości pozostają z AWX. Globalne `{}` oznacza brak nadpisań, jeżeli żadne nadpisanie operacji nie dodaje własnych wpisów.

Refresh zapisuje wynik do `Job_Variables` i wyznacza `Job_Type` według `JOB_TYPE_VARIABLE`. Kolejny refresh ponownie pobiera dane z AWX i nakłada tę konfigurację, więc ręczne zmiany `Job_Variables` w Excelu mogą zostać zastąpione.

Nie jest to dodatkowa warstwa nakładana automatycznie na każdy istniejący wiersz podczas execute.

### OS_JOBS

**Typ:** opcjonalny słownik z kluczami `windows`, `linux`, `aix`. **Użycie:** ręczna konfiguracja eksportu.

Brak ustawienia, `None` lub `{}` są normalizowane do braku ręcznych mapowań.

```python
OS_JOBS = {
    'linux': {
        'url': 'https://awx.example.com/#/templates/job_template/7/details',
        'variables': {'action': 'install', 'retries': 2},
    },
    'windows': {
        'url': 'https://awx.example.com/#/templates/job_template/8/details',
        'variables': {'action': 'install'},
    },
    'aix': {
        'url': 'https://awx.example.com/#/templates/job_template/9/details',
        'variables': {'action': 'reinstall'},
    },
}
```

`url` wskazuje szablon, a `variables` zawiera dodatkowe `extra_vars`. Brak URL oznacza brak ręcznie wskazanego szablonu dla danego OS.

Przy eksporcie zmienne powstają następująco:

1. Odczyt `extra_vars` wybranego szablonu AWX.
2. Nałożenie `OS_JOBS[ostype]['variables']` na najwyższym poziomie.
3. Zastosowanie akcji `JOB_TYPE`, jeżeli ją ustawiono.
4. Zapis wyniku jako JSON w `Job_Variables`.

Krok 2 zastępuje cały obiekt pod powtarzającym się kluczem. To nie jest ten sam etap co rekurencyjne `JOB_VARIABLE_OVERRIDES_BY_OS` używane przez refresh.

Można wkleić YAML wewnątrz `variables`:

```text
OS_JOBS = {
    'linux': {
        'url': 'https://awx.example.com/api/v2/job_templates/7/',
        'variables': {
            action: install
            retries: 2
            enabled: true
            packages:
              - agent
              - tools
        },
    },
}
```

Otaczające przypisanie nadal ma składnię konfiguracji. Sam blok `variables` jest konwertowany do obiektu JSON. Wymaga obsługi PyYAML, którą skrypty potrafią doinstalować w używanym venv.

`variables` musi być obiektem, a nie listą, pojedynczym tekstem czy zestawem dowolnych instrukcji Pythona.

### JOB_URL

**Typ:** opcjonalny tekst URL; `None` i `''` oznaczają brak wspólnego wyboru.

```python
JOB_URL = 'https://awx.example.com/api/v2/job_templates/7/'
```

W eksporcie ma pierwszeństwo przed `OS_JOBS[ostype]['url']`, także przy wykrywaniu ankiety wybranego szablonu.

W execute użyj jawnego argumentu, jeżeli chcesz zmienić szablon istniejących wierszy:

```powershell
python .\awx_execute_jobs.py --job-url "https://awx.example.com/api/v2/job_templates/7/" --dry-run
```

Przy rzeczywistym wykonaniu jawne `--job-url` aktualizuje `Job_Link` i `template_job_name`. Bez tego argumentu źródłem jest Excel, nawet jeżeli w konfiguracji zapisano `JOB_URL`.

### JOB_TEMPLATE_ORGANIZATION

**Typ:** opcjonalna nazwa organizacji albo dodatnie ID. **Domyślna normalizacja:** `''`.

Dotyczy przede wszystkim `awx_find_templates.py`:

```python
JOB_TEMPLATE_ORGANIZATION = 'it2'
```

```powershell
python .\awx_find_templates.py --organization it2
python .\awx_find_templates.py --organization 13 --name-pattern "*hello*unix*"
python .\awx_find_templates.py --hostname it2-testvm1
```

Bez `--hostname` wyszukiwanie wymaga organizacji z konfiguracji albo `--organization`. Z `--hostname` organizacja może być ustalona z ApplicationID; jawne `--organization` ją nadpisuje.

Refresh zawsze wyznacza organizację dla hosta z ApplicationID. Dla nazwy inventory `it2_mirror_inventory` jest to organizacja o nazwie `it2`.

### JOB_TEMPLATE_AUTO_DISCOVERY

**Typ:** opcjonalne `True` / `False`. **Domyślna normalizacja:** `False`.

Ustawienie pozostaje obsługiwane przez parser, ale nie jest uniwersalnym przełącznikiem wszystkich launcherów:

- Refresh wymusza automatyczny dobór szablonu niezależnie od tej flagi.
- Eksport nadal wybiera szablon przez `JOB_URL` / `OS_JOBS`.
- Execute nadal używa `Job_Link` z Excela lub jawnego `--job-url`.

Samo dodanie `JOB_TEMPLATE_AUTO_DISCOVERY = True` nie zastępuje konfiguracji właściwej dla danej operacji.

## 8. Credentiale, grupy i jumphosty

### CREDENTIAL_VARIABLES

**Typ:** lista niepustych nazw zmiennych grup AWX. **Obecnie:** `['os_credential']`.

```python
CREDENTIAL_VARIABLES = ['os_credential']
```

Skrypt odczytuje zmienne z grup bezpośrednich i nadrzędnych hosta. Przykład zmiennej grupy w AWX:

```yaml
os_credential: it2_id_rsa_it2uaxt2_XYZ
```

To odwołanie do credentiala istniejącego w AWX, nie jego hasło ani klucz prywatny.

Można szukać kilku nazw zmiennych:

```python
CREDENTIAL_VARIABLES = ['os_credential', 'service_credential']
```

Wartości z dopasowanych zmiennych składają się na listę credentiali hosta prezentowaną w `OS Credential` i uwzględnianą w `Full_Credential`.

### EXTRA_CREDENTIAL_VARIABLES

**Typ:** lista nazw lub ID credentiali AWX.

Credentiale przypisane do szablonu:

```python
EXTRA_CREDENTIAL_VARIABLES = []
```

Jawne dodatki:

```python
EXTRA_CREDENTIAL_VARIABLES = ['shared-vault', 201]
```

Mimo nazwy ustawienia są to **odwołania do credentiali**, a nie nazwy zmiennych z grup.

`[]` powoduje pobranie dodatków z szablonu wskazanego przez `Job_Link` / `JOB_URL`. Niepusta lista wybiera dodatki z konfiguracji.

Zasady składania:

- `Job_Credentials` pokazuje credentiale przypisane do szablonu.
- `Extra_Credentials` pokazuje źródło dodatków wybrane powyższą regułą.
- `Full_Credential` łączy credentiale hosta, odpowiednie dodatki i wybrane jumphosty.
- Machine odziedziczone wyłącznie z szablonu nie jest kopiowane do `Full_Credential`.
- Machine z `OS Credential` lub jawnej konfiguracji jest zachowywane.
- Przy starcie Machine szablonu pozostaje używane, jeżeli nie wybrano zamiennika tego samego typu.
- Pozostałe typy są uwzględniane zgodnie z wybraną konfiguracją; dla Vault istotne są także Vault IDs.
- Dwa credentiale tego samego typu mogą powodować konflikt. Różne Vault IDs są rozróżniane.

Nazwy są rozwiązywane do ID przed pierwszym startem. Brak dostępu, brak credentiala albo niejednoznaczna nazwa zatrzymują wykonanie. W przypadku niejednoznaczności użyj ID.

### FULL_CREDENTIAL_SEPARATOR

**Typ:** tekst separatora. **Obecnie:** przecinek i spacja.

```python
FULL_CREDENTIAL_SEPARATOR = ', '
```

Przykład komórki `Full_Credential`:

```text
it2_id_rsa_it2uaxt2_XYZ, shared-vault, it2_3hop_cred
```

Taki sam separator stosuje się przy ręcznym wpisywaniu kilku credentiali do `jumphost_for_hops`. Kolejność credentiali i powtórzenia nie tworzą dodatkowych jobów, jeżeli zestaw jest równoważny.

Puste `Full_Credential` lub `none` zachowuje credentiale szablonu. Jeśli wybrano wyłącznie jumphosty, skrypt dołącza je do zachowanych ustawień szablonu.

Nie ustawiaj pustego separatora przy niepustych listach credentiali. Jeżeli nazwa credentiala zawiera separator, użyj jego ID.

### Skąd biorą się HOPS i jumphost

Nie mają osobnych ustawień głównych; wynikają z grup i credentiali w AWX:

1. Skrypt szuka nazw grup zawierających liczbę i `hop`, np. `it2_grp_cred_unix_3hop`.
2. `HOPS` zawiera opis grup.
3. `HOPS_COUNT` zawiera liczbę, jeżeli jest jednoznaczna; inaczej pozostaje puste.
4. Dla ApplicationID `it2` i liczby `3` wyszukiwane są credentiale zawierające ten identyfikator i dokładnie ten numer hopa.
5. Domyślnie wybierany jest credential o najniższym ID. Excel udostępnia alternatywy na liście.

Przykład:

| Pole | Wartość |
| --- | --- |
| `ApplicationID` | `it2` |
| `HOPS` | `it2_grp_cred_unix_3hop (HOPS: 3)` |
| `HOPS_COUNT` | `3` |
| `jumphost_for_hops` | `it2_3hop_cred` |
| `OS Credential` | `it2_id_rsa_it2uaxt2_XYZ` |
| `Full_Credential` | `it2_id_rsa_it2uaxt2_XYZ, it2_3hop_cred` |

Refresh zachowuje wybór jumphosta przy niezmienionych ApplicationID i HOPS_COUNT. Zmiana wyboru zastępuje wcześniej dołączone jumphosty w `Full_Credential`. Wyczyszczenie komórki przed refresh pozwala przywrócić wybór domyślny.

## 9. Wykonanie, akcja i grupowanie jobów

### BATCH_SIZE

**Typ:** dodatnia liczba całkowita. **Obecnie:** `10`. **CLI:** `--batch-size`.

```python
BATCH_SIZE = 10
```

To maksymalna liczba unikalnych hostów w jednej paczce zgodnych parametrów. Nie oznacza, że pierwsze 10 dowolnych wierszy trafi do jednego joba.

Do wspólnej paczki wymagane są zgodne:

- ID szablonu i zweryfikowana nazwa.
- `ostype`.
- Zmienne wykonania, w tym odpowiedzi survey.
- `Full_Credential` po uwzględnieniu jumphostów i sposobu zachowania credentiali szablonu.
- `Job_Variables` zapisane w Excelu, również gdy później część wartości nadpisuje komenda.
- `INVENTORY`, `ApplicationID`, `HOPS`, `HOPS_COUNT` i `Job_Type`.
- Kolumny `Job_Credentials`, `Extra_Credentials`, `OS Credential` i `jumphost_for_hops`.

`hostname` tworzy listę hostów joba. Notatki, dotychczasowy stdout i artefakty nie rozdzielają grup. `AWX_Status` jest walidowany, ale nie stanowi osobnego klucza grupowania.

Przykład przy `BATCH_SIZE = 10`:

| Host | Szablon | OS | `version` | `Full_Credential` | Wynik |
| --- | --- | --- | --- | --- | --- |
| host1 | 7 | linux | 1 | machine-a, vault | Job A |
| host2 | 7 | linux | 1 | machine-a, vault | Job A |
| host3 | 7 | linux | 2 | machine-a, vault | Job B |
| host4 | 7 | linux | 1 | machine-b, vault | Job C |
| host5 | 7 | windows | 1 | machine-a, vault | Job D |

Pozostałe metadane w przykładzie są zgodne. Różna wartość oznacza osobny job **tego samego szablonu**, jeżeli ID szablonu się nie zmienia.

Dla 23 zgodnych hostów i limitu `10` powstaną trzy joby: `10 + 10 + 3`.

Dodatkowe reguły:

- Kolejność kluczy JSON nie rozdziela równoważnych obiektów.
- Typy wartości są istotne: liczba `1` i tekst `"1"` to różne zmienne.
- `window` / `windows` i wielkość liter w OS są normalizowane.
- Ten sam szablon wskazany linkiem UI lub API jest rozpoznawany po ID.
- Identyczne wpisy tego samego hosta są łączone; różne konfiguracje tego samego hosta mogą tworzyć osobne joby.
- Każdy limit zaczyna się od `localhost`. Dodany automatycznie `localhost` nie zużywa miejsca w `BATCH_SIZE`.

### EXECUTION_COLUMN

**Typ:** nazwa osobnej kolumny. **Obecnie:** `'exceiute_job'`.

```python
EXECUTION_COLUMN = 'exceiute_job'
```

Pisownia `exceiute_job` jest faktycznie używana przez skrypty. Execute zapisuje tam linki, a output odczytuje je z tej samej kolumny. Zmiana nazwy wymaga spójnej konfiguracji obu operacji i uwzględnienia historii pod dotychczasową nazwą.

Nie używaj nazwy kolumny metadanych, `Artifacts` ani kolumny stdout.

Historia:

- Istniejące wpisy są pomijane podczas zwykłego uruchomienia.
- Jeżeli nie ma nowych paczek, skrypt może poprosić o potwierdzenie ponowienia.
- Potwierdzony rerun zachowuje poprzedni link w komentarzu.
- Przed żądaniem startu zapisywany jest `LAUNCH_PENDING`.
- `LAUNCH_PENDING` oznacza zamiar uruchomienia z niepotwierdzonym wynikiem; przed ponowieniem trzeba ustalić stan joba w AWX.
- `--dry-run` nie dopisuje historii i nie potwierdza rerunu.

### JOB_TYPE

**Typ:** tekst, np. `'install'`, `'reinstall'`, `'update'` lub `''`.

```python
JOB_TYPE = 'install'
```

To wartość zmiennej playbooka, a nie parametr AWX `job_type` wybierający tryb `run` / `check`.

W execute kolejność dla akcji jest następująca:

1. Jawne `--job-type`.
2. Odpowiedź dla zmiennej akcji z `--survey-answers` lub `Survey_...`.
3. Ta zmienna w `Job_Variables`.
4. Kolumna `Job_Type`.
5. Konfiguracja `JOB_TYPE`.

Pusty tekst nie narzuca dodatkowej akcji.

```powershell
python .\awx_execute_jobs.py --job-type reinstall --dry-run
```

Przy eksporcie niepusty `JOB_TYPE` ustawia akcję w przygotowanych zmiennych. Refresh ignoruje globalną akcję i odczytuje ją z końcowych zmiennych szablonu po `JOB_VARIABLE_OVERRIDES_BY_OS`.

### JOB_TYPE_VARIABLE

**Typ:** poprawna nazwa zmiennej: litery/cyfry/`_`, bez cyfry na początku. **Obecnie:** `'action'`.

```python
JOB_TYPE_VARIABLE = 'action'
```

W połączeniu z `JOB_TYPE='install'` oznacza przekazanie:

```json
{"action": "install"}
```

Jeżeli akcję określa pytanie survey o zmiennej `installtesttypename`, ustaw:

```python
JOB_TYPE_VARIABLE = 'installtesttypename'
```

Wtedy `--job-type install` ustawi `extra_vars['installtesttypename']`. Przy obecnym `JOB_TYPE_VARIABLE='action'` ten argument nie zmienia automatycznie pytania `installtesttypename`.

### Co jest sprawdzane przed rzeczywistym startem

Przed uruchomieniem pierwszej nowej paczki skrypt sprawdza:

- Zgodność `template_job_name` z definicją szablonu z `Job_Link`.
- Zgodność tekstu URL z hiperlinkiem komórki i serwerem `AWX_URL`.
- Literalne nazwy hostów i pola wejściowe; formuły nie zastępują parametrów wykonania.
- Niepusty OS oraz zgodność `ostype` z hostem w inventory przypisanym do szablonu AWX.
- Obecność hostów w tym inventory i ich faktyczną pisownię.
- `AWX_Status='found'`, jeżeli status jest wypełniony; pusty status jest dopuszczony w starszych arkuszach.
- Poprawny obiekt JSON w `Job_Variables`, bez powtórzonych kluczy i wartości niezgodnych z JSON.
- Istnienie credentiali, rozwiązywanie nazw do ID i konflikty typów.
- Przyjmowanie Limit, Credentials i Variables zgodnie z używanymi nadpisaniami.
- Typy, wymagane odpowiedzi, zakresy i wybory survey.
- Brak niesprawdzonych wpisów `LAUNCH_PENDING` oraz wymagania potrzebne do startu.

Szablon powinien przyjmować `Limit` przez `Prompt on launch`. Przy nadpisaniu credentiali wymagane jest `Prompt on launch` obok Credentials; przy zmiennych — odpowiednie Variables albo obsługa przez survey. Job Slicing musi wynosić `1`, ponieważ paczki tworzy ten skrypt.

Brak lub niezgodność OS i błąd credentiala w późniejszej paczce blokują również wcześniejsze paczki przed pierwszym startem. Rzeczywisty błąd już uruchomionego joba przy oczekiwaniu zatrzymuje kolejne paczki.

## 10. Ankiety AWX: ustawienia i priorytety

Kluczem odpowiedzi jest **Answer Variable Name** pytania w AWX, a nie etykieta widoczna dla użytkownika.

Dla zmiennej `installtesttypename` kolumna Excela nazywa się `Survey_installtesttypename`. Samo wpisanie klucza w konfiguracji nie tworzy pytania po stronie AWX.

### SURVEY_EXCEL_DEFAULTS

**Typ:** słownik odpowiedzi do wypełniania kolumn ankiet. **Obecnie:**

```python
SURVEY_EXCEL_DEFAULTS = {
    'installtesttypename': 'testads',
}
```

Eksport wypełnia nowe/puste komórki tylko dla pytań wykrytych w wybranych szablonach. Kolejność źródeł przy wypełnianiu:

1. `Job_Variables` danego hosta.
2. `SURVEY_EXCEL_DEFAULTS`.
3. `SURVEY_ANSWERS`.
4. Default pytania w AWX.

Refresh dodatkowo nadpisuje wypełnione komórki wartościami skonfigurowanymi w `SURVEY_EXCEL_DEFAULTS` / `SURVEY_ANSWERS`. Dla wspólnego klucza wygrywa `SURVEY_EXCEL_DEFAULTS`.

Klucze nieobecne w wykrytych pytaniach nie tworzą dodatkowych kolumn survey. Samo execute nie traktuje tego słownika jako ogólnego nadpisania wszystkich istniejących odpowiedzi.

### SURVEY_ANSWERS

**Typ:** słownik wspólnych odpowiedzi używanych przy wykonaniu. **Obecnie:**

```python
SURVEY_ANSWERS = {
    'installtesttypename': 'reinstall',
}
```

Przykład z różnymi typami:

```python
SURVEY_ANSWERS = {
    'installtesttypename': 'install',
    'retries': 3,
    'ratio': 0.5,
    'components': ['agent', 'tools'],
}
```

Wartości muszą pasować do faktycznych pytań. Liczba całkowita, liczba zmiennoprzecinkowa, lista multiselect i tekst nie są wymienne we wszystkich typach pytań.

Priorytet odpowiedzi przy wykonaniu, od najwyższego:

1. `--survey-answers` jako obiekt JSON.
2. Niepusta komórka `Survey_<zmienna>`.
3. `Job_Variables` hosta.
4. `SURVEY_ANSWERS`.
5. Default pytania w AWX.

Dla zmiennej określonej przez `JOB_TYPE_VARIABLE` jawne `--job-type` ma dodatkowo najwyższy priorytet.

`SURVEY_ANSWERS` jest też bazą `extra_vars`, więc klucze spoza ankiety nie są uniwersalnie ignorowane przy uruchomieniu. Mogą wymagać `Prompt on launch` dla Variables albo zostać odrzucone przez walidację szablonu. Nie myl tego z pomijaniem niepasujących kluczy podczas tworzenia kolumn przez `SURVEY_EXCEL_DEFAULTS`.

### SURVEY_INTERACTIVE

**Typ:** `True` / `False`. **CLI:** `--survey` / `--no-survey`.

```python
SURVEY_INTERACTIVE = False
```

`False` oznacza wykonanie na podstawie istniejących odpowiedzi i defaultów, z błędem przy brakujących wymaganych danych.

`True` włącza pytania w terminalu dla odpowiedzi, których nie podano jawnie. Pokazuje dostępne wybory i defaulty; Enter może zachować default. Hasła są wpisywane bez echa. Odpowiedzi z terminala są współdzielone przez paczki tego szablonu i nie są dopisywane do komórek ankiety.

```powershell
python .\awx_execute_jobs.py --show-survey
python .\awx_execute_jobs.py --survey --dry-run
python .\awx_execute_jobs.py --no-survey --dry-run
```

`--show-survey` wyświetla definicję pytań bez uruchamiania jobów i zapisu Excela. Interaktywne pytania wymagają terminala. Survey musi być włączone w szablonie dla `--survey`.

### Przykład dla obecnych wartości testads i reinstall

Zakładamy, że szablon ma pytanie `installtesttypename` i dopuszcza podane wartości.

| Stan | Odpowiedź użyta przy execute |
| --- | --- |
| W komórce `Survey_installtesttypename` jest `testads`, a `SURVEY_ANSWERS` ma `reinstall`. | `testads`. |
| Komórka jest pusta, `Job_Variables` nie zawiera tej zmiennej, `SURVEY_ANSWERS` ma `reinstall`. | `reinstall`. |
| Komórka jest pusta, `Job_Variables` zawiera `install`, a `SURVEY_ANSWERS` ma `reinstall`. | `install`. |
| Jawne `--survey-answers` ustawia tę zmienną na `install`. | `install`, niezależnie od komórki. |
| `JOB_TYPE_VARIABLE='action'` i argument `--job-type install`. | Ustawia `action`; nie zastępuje `installtesttypename`. |
| `JOB_TYPE_VARIABLE='installtesttypename'` i argument `--job-type install`. | Ustawia `installtesttypename='install'`. |

Jeżeli chcesz jedną wartość domyślną dla nowych komórek i wykonania, ustaw oba słowniki spójnie. Przykład:

```python
SURVEY_EXCEL_DEFAULTS = {
    'installtesttypename': 'install',
}
SURVEY_ANSWERS = {
    'installtesttypename': 'install',
}
```

Po zmianie konfiguracji odśwież Excel albo świadomie zmień istniejące komórki. Sama zmiana `SURVEY_ANSWERS` nie usuwa wcześniejszej odpowiedzi zapisanej w Excelu.

Dla multiselect komórka przyjmuje listę JSON, np. `["agent", "tools"]`. Krótkie listy multiplechoice mogą otrzymać listę rozwijaną w Excelu. Wymagane pola, typy i dozwolone wybory są sprawdzane przed startem.

## 11. Oczekiwanie oraz logi

### WAIT_FOR_COMPLETION

**Typ:** `True` / `False`. **Obecnie:** `False`.

```python
WAIT_FOR_COMPLETION = True
```

| Operacja | `True` | `False` |
| --- | --- | --- |
| Execute | Czeka na zakończenie paczki i jej sukces przed kolejną. Błąd zatrzymuje następne. | Wysyła kolejne paczki po otrzymaniu odpowiedzi startu, bez oczekiwania na ich koniec. |
| Output | Czeka na końcowy status joba, również failed/error/canceled, następnie pobiera wyniki. | Pobiera aktualnie dostępny log, także gdy job trwa. |

Przy `False` końcowe artefakty i logi uzupełnia się przez ponowne uruchomienie `awx_job_output.py`.

### POLL_INTERVAL

**Typ:** dodatnia, skończona liczba sekund. **Obecnie:** `5`.

```python
POLL_INTERVAL = 10
```

Odstęp sprawdzania statusu joba podczas oczekiwania. Nie jest odstępem między pobieraniem stron hostów ani pomiędzy wszystkimi żądaniami API.

### JOB_WAIT_TIMEOUT

**Typ:** dodatnia, skończona liczba sekund. **Obecnie:** `3600`.

```python
JOB_WAIT_TIMEOUT = 7200
```

Maksymalny czas oczekiwania na konkretny job w danym wywołaniu mechanizmu oczekiwania. Nie jest łącznym limitem całego skryptu i nie anuluje automatycznie joba w AWX.

Przykład rozróżnienia:

```python
TIMEOUT = 30
POLL_INTERVAL = 5
JOB_WAIT_TIMEOUT = 3600
```

Każde żądanie HTTP ma limit 30 sekund, status jest sprawdzany co około 5 sekund, a oczekiwanie na joba ma limit godziny.

### OUTPUT_COLUMN

**Typ:** nazwa osobnej kolumny stdout. **Obecnie:** `'output'`.

```python
OUTPUT_COLUMN = 'output'
```

`awx_job_output.py` wpisuje tu stdout joba do wszystkich powiązanych wierszy. Nazwa musi być różna od `EXECUTION_COLUMN` i nazw metadanych/artefaktów. Brakująca kolumna jest dodawana.

Nie zmienia nazw osobnych kolumn `Artifacts` i `artifact filter`.

### OUTPUT_DIRECTORY

**Typ:** niepusta ścieżka katalogu. **Obecnie:** `'output'`. **CLI:** `--output-dir`.

```python
OUTPUT_DIRECTORY = r'C:\AWX\logs'
```

```powershell
python .\awx_job_output.py --file .\awx_inventory_hosts.xlsx --output-dir .\output
```

Nazwy plików mają postać:

```text
<id_joba>.<inventory>.<os>.txt
99.it2_inventory.linux.txt
```

Inventory pochodzi z danych joba AWX, OS z wierszy Excela. Znaki niedozwolone w nazwach plików są zastępowane.

Każdy unikalny job jest odczytywany raz, a jego wynik trafia do odpowiednich wierszy. Przy wielu OS dla tego samego joba mogą powstać osobne pliki z tym samym stdout.

Komórka Excela mieści do 32767 znaków. Dłuższy log jest skracany z adresem pełnego pliku TXT. Zapis TXT zachowuje pełną treść.

`Artifacts` przechowuje JSON artefaktów. `artifact filter` zawiera komunikaty `message` dopasowane do hosta. Artefakty są aktualizowane również wtedy, gdy późniejszy odczyt stdout się nie powiedzie. Długi JSON artefaktów może zostać skrócony z odwołaniem do pełnych danych w AWX.

Pobranie logu joba o statusie `failed` może zakończyć się sukcesem — sukces pobrania i sukces joba są różnymi informacjami.

## 12. Diagnostyka i tryb podglądu

### DEBUG

**Typ:** `True` / `False`. **Obecnie:** `False`. **CLI:** `-d`, `-debug`, `--debug` i `--no-debug`.

```python
DEBUG = True
```

Pokazuje cele i przebieg zapytań API, etapy przetwarzania, przyczyny pomijania wierszy, tworzenie paczek, walidację i zapis.

Normalny eksport pokazuje skrócone linie:

```text
Host [1/2]: it2-testvm
Host [2/2]: it2-testvm1
```

Szczegółowy blok pól hosta jest widoczny dopiero przy debug. Komunikaty etapów i końcowe podsumowanie są dostępne także bez debug.

```powershell
python .\awx_inventory_hosts.py --debug
python .\awx_execute_jobs.py --no-debug --dry-run
```

`DEBUG` nie włącza `DRY_RUN`. Diagnostyka API maskuje token. Podgląd wykonania maskuje wartości `extra_vars`, ale szczegółowy blok eksportowanego hosta w debug pokazuje dane wynikowe, w tym `Job_Variables`.

### DRY_RUN

**Typ:** `True` / `False`. **Użycie:** execute. **CLI:** `--dry-run` / `--no-dry-run`.

```python
DRY_RUN = True
```

```powershell
python .\awx_execute_jobs.py --dry-run
```

Tryb pokazuje podział na paczki: szablon, OS, hosty, credentiale, akcję i nazwy zmiennych z zamaskowanymi wartościami. Nie uruchamia jobów i nie zapisuje Excela, także jeżeli plik trzeba było przygotować w pamięci dla wybranych hostów.

To nie musi być praca offline. Skrypt odczytuje definicje szablonów, a dla brakujących hostów może pobrać metadane z AWX.

Zwykły podgląd nie przechodzi całej ścieżki przedstartowej rzeczywistego wykonania: końcowe rozwiązywanie credentiali, porównanie OS z inventory szablonu i pełne wymagania startu są wykonywane przed rzeczywistym startem. `--survey --dry-run` dodatkowo odczytuje i waliduje ankietę.

```powershell
python .\awx_execute_jobs.py --survey --dry-run
python .\awx_execute_jobs.py --no-dry-run
```

`--no-dry-run` wyłącza podgląd z konfiguracji i pozwala na rzeczywiste wykonanie po walidacji. `DRY_RUN` nie jest globalną blokadą zapisu dla eksportu, refresh i output.

### COLOR

**Typ:** `'auto'`, `'always'` lub `'never'`. **CLI:** `--color` / `--no-color`.

```python
COLOR = 'auto'
```

| Wartość | Działanie |
| --- | --- |
| `auto` | Kolory w terminalu, gdy środowisko je obsługuje. Przy przekierowaniu zwykle brak ANSI. |
| `always` | Wymusza kody ANSI także w przekierowanym wyjściu. |
| `never` | Wyłącza kolory. |

```powershell
python .\awx_execute_jobs.py --dry-run --color never
python .\awx_inventory_hosts.py --no-color
```

Przy `auto` zmienna środowiskowa `NO_COLOR` lub terminal `TERM=dumb` wyłącza kolory. Kolory nie zmieniają danych zapisanych w plikach.

## 13. Nadpisania poszczególnych operacji

Każda sekcja jest słownikiem z takimi samymi nazwami ustawień jak na poziomie głównym. Pusty słownik oznacza brak dodatkowych nadpisań.

| Sekcja | Skrypt |
| --- | --- |
| `INVENTORY_OVERRIDES` | `awx_inventory_hosts.py` |
| `REFRESH_OVERRIDES` | `awx_refresh_excel.py` |
| `EXECUTE_OVERRIDES` | `awx_execute_jobs.py` |
| `OUTPUT_OVERRIDES` | `awx_job_output.py` |
| `TEMPLATES_OVERRIDES` | `awx_find_templates.py` |

Przykład:

```python
INVENTORY_OVERRIDES = {
    'INPUT_FILE': None,
    'EXCEL_COLUMNS': ['hostname', 'AWX_Status', 'ostype', 'Uwagi'],
}

REFRESH_OVERRIDES = {
    'REFRESH_INVENTORY_PATTERN': '*_mirror_inventory',
}

EXECUTE_OVERRIDES = {
    'INPUT_FILE': 'awx_inventory_hosts.xlsx',
    'BATCH_SIZE': 5,
    'WAIT_FOR_COMPLETION': True,
    'DRY_RUN': True,
}

OUTPUT_OVERRIDES = {
    'WAIT_FOR_COMPLETION': True,
    'OUTPUT_DIRECTORY': 'output',
}

TEMPLATES_OVERRIDES = {
    'JOB_TEMPLATE_ORGANIZATION': 'it2',
    'JOB_TEMPLATE_NAME_PATTERN': '*hello*{os_name}*',
}
```

Ustawienie w danej sekcji ma znaczenie tylko tam, gdzie operacja je wykorzystuje. Na przykład `HOSTS` nie staje się filtrem eksportu, a `DRY_RUN=True` w `REFRESH_OVERRIDES` nie wprowadza podglądu refresh.

W każdym launcherze można zamiast tego odkomentować lokalne `VARIABLES`. Mają wyższy priorytet niż sekcja w `awx_variable`. Pamiętaj o szczególnym zachowaniu refresh: automatyczny wybór organizacji i szablonu pozostaje wymuszony.

## 14. Kolumny Excela i arkusz Ustawienia

Standardowe kolumny są następujące. Nazwy konfigurowalnych kolumn wykonania i stdout podano z obecnymi wartościami.

| Kolumna | Znaczenie |
| --- | --- |
| `hostname` | Host przypisany do wiersza. |
| `AWX_Status` | `found`, `not_found` lub `no_hostname`; znajduje się bezpośrednio po `hostname`. |
| `INVENTORY` | Inventory, z którego pobrano metadane hosta. |
| `ApplicationID` | Pierwszy człon nazwy inventory przed `_`. |
| `ostype` | OS odczytany z AWX, np. `linux`, `windows`, `aix`. |
| `template_job_name` | Nazwa wybranego szablonu, sprawdzana przy execute. |
| `Job_Link` | Link do szablonu joba. To nie jest link do uruchomionego joba. |
| `Job_Credentials` | Credentiale przypisane do szablonu. |
| `Job_Variables` | Obiekt JSON ze zmiennymi joba. W Excelu obowiązuje JSON, nie blok YAML konfiguracji. |
| `HOPS` | Opis grup hopów. |
| `HOPS_COUNT` | Jednoznaczna liczba hopów lub pusta komórka. |
| `jumphost_for_hops` | Edytowalny wybór credentiali jumphosta. |
| `Extra_Credentials` | Credentiale dodatkowe wynikające z konfiguracji lub szablonu. |
| `OS Credential` | Credentiale hosta z dopasowanych zmiennych grup. |
| `Full_Credential` | Credentiale wybrane dla wykonania, z uwzględnieniem jumphostów. |
| `Job_Type` | Wartość akcji określonej przez `JOB_TYPE_VARIABLE`. |
| `exceiute_job` | Link do uruchomienia lub znacznik `LAUNCH_PENDING`. Nazwę określa `EXECUTION_COLUMN`. |
| `output` | Stdout; nazwę określa `OUTPUT_COLUMN`. |
| `Artifacts` | Artefakty joba w JSON. |
| `artifact filter` | Komunikaty `message` przypisane do hosta. |
| `Survey_<zmienna>` | Odpowiedź na konkretne pytanie survey. |

Eksport i odświeżanie porządkują hosty według systemu: Windows, Linux, AIX, następnie pozostałe/puste wartości. Nazwy kolumn są identyfikowane bez rozróżniania wielkości liter.

### Arkusz Ustawienia

Eksport dodaje osobny arkusz `Ustawienia`. Zawiera między innymi:

- Datę eksportu w UTC i liczbę hostów.
- Źródło konfiguracji i kolejność nadpisań.
- Filtr hostów i rozpoznane inventory.
- Efektywne parametry eksportu po nadpisaniach.
- Ścieżki plików, ustawienia szablonów, credentiali i ankiet.

Zagnieżdżone wartości są pokazane pod pełnymi nazwami, np. `OS_JOBS.linux.url`. Elementy list otrzymują indeksy.

`AWX_TOKEN` jest pomijany. Rozpoznane klucze sekretów i wartości pytań typu password są maskowane.

To zapis informacyjny z chwili eksportu. Edycja arkusza nie zmienia `awx_variable` i nie steruje jobami. Refresh i kolejne operacje zachowują ten arkusz; nie zamieniają go automatycznie w historię konfiguracji wszystkich późniejszych uruchomień.

## 15. Przykładowe przebiegi pracy

### A. Eksport i automatyczny dobór szablonów przez refresh

Konfiguracja:

```python
INVENTORY = 'it2_inventory'
OS_JOBS = {}
JOB_URL = ''
REFRESH_INVENTORY_PATTERN = '*_mirror_inventory'
JOB_TEMPLATE_NAME_PATTERN = '*hello*{os_name}*'
```

W AWX muszą istnieć inventory pasujące do wzorca oraz organizacja odpowiadająca ApplicationID hosta. Jeżeli używasz wyłącznie `it2_inventory`, ustaw odpowiednio `REFRESH_INVENTORY_PATTERN='it2_inventory'`.

Kolejne komendy uruchamiaj po sprawdzeniu wyniku poprzedniej:

```powershell
python .\awx_inventory_hosts.py
python .\awx_refresh_excel.py --file .\awx_inventory_hosts.xlsx
python .\awx_find_templates.py --hostname it2-testvm1
python .\awx_execute_jobs.py --file .\awx_inventory_hosts.xlsx --dry-run
python .\awx_execute_jobs.py --file .\awx_inventory_hosts.xlsx
python .\awx_job_output.py --file .\awx_inventory_hosts.xlsx
```

Przy `DRY_RUN=False` komenda execute bez `--dry-run` jest rzeczywistym uruchomieniem. Przy `WAIT_FOR_COMPLETION=False` ostatnie pobranie logów można powtórzyć po zakończeniu jobów.

### B. Ręczne mapowanie szablonów dla OS

Wstaw właściwe `OS_JOBS` według sekcji 7, a następnie:

```powershell
python .\awx_inventory_hosts.py
python .\awx_execute_jobs.py --dry-run
python .\awx_execute_jobs.py
```

Refresh nie zachowuje ręcznego mapowania szablonów: zastępuje je swoim automatycznym doborem. W tym wariancie uruchamiaj refresh tylko wtedy, gdy chcesz przejść na ten sposób doboru.

### C. Wybrane hosty i jawny szablon

```powershell
python .\awx_execute_jobs.py --hosts host1 host2 --job-url "https://awx.example.com/api/v2/job_templates/7/" --batch-size 2 --dry-run
```

Podgląd obejmuje tylko wybrane nazwy. Brakujące hosty mogą zostać odczytane z AWX. Po przejściu do rzeczywistego wykonania nadal wymagane są zgodność OS, poprawne credentiale i ustawienia szablonu.

### D. Szybka lista opcji CLI

| Opcja | Operacje i znaczenie |
| --- | --- |
| `--help` | Pomoc wybranego skryptu. |
| `-file` / `--file` | Eksport: plik filtra; pozostałe operacje plikowe: docelowy Excel do aktualizacji. |
| `--sheet` | Refresh, execute, output: jawny arkusz. |
| `-d` / `-debug` / `--debug`, `--no-debug` | Włączenie/wyłączenie diagnostyki. |
| `--color auto/always/never`, `--no-color` | Kolory terminala. |
| `--host-column` | Refresh: nagłówek z nazwami hostów. |
| `--hosts` / `--host` | Execute: wybrane nazwy hostów. |
| `--job-url` | Execute: jawne nadpisanie szablonu. |
| `--batch-size` | Execute: maksymalna liczba hostów w paczce. |
| `--job-type` | Execute: akcja przekazywana pod `JOB_TYPE_VARIABLE`. |
| `--dry-run` / `--no-dry-run` | Execute: podgląd / wyłączenie podglądu. |
| `--survey` / `--no-survey` | Execute: pytania survey / wyłączenie pytań. |
| `--survey-answers` | Execute: tekst zawierający obiekt JSON odpowiedzi, np. `{"installtesttypename":"install"}`. |
| `--show-survey` | Execute: wyświetlenie definicji ankiet bez startu i zapisu. |
| `--output-dir` | Output: katalog pełnych logów TXT. |
| `--organization` | Finder: organizacja dla wyszukiwania szablonów. |
| `--name-pattern` / `--name` | Finder: wzorzec nazwy szablonu. |
| `--hostname` | Finder: host do ustalenia OS i ApplicationID. |

Nie wszystkie ustawienia mają odpowiadający argument CLI. Na przykład oczekiwanie można zmienić w konfiguracji lub w odpowiednim `*_OVERRIDES`.

## 16. Kompletny przykład konfiguracji

To kompletny przykład przypisań, oparty na obecnych wartościach i uzupełniony o opcjonalne ustawienia. Token i adres są zastępcze. Wariant używa automatycznego doboru szablonów przy refresh; sam eksport z `OS_JOBS={}` nie przypisuje szablonów.

```python
AWX_URL = 'https://awx.example.com'
AWX_TOKEN = 'WSTAW_WLASNY_TOKEN_AWX'
INVENTORY = 'it2_inventory'
TIMEOUT = 30
PAGE_SIZE = 200

CREDENTIAL_VARIABLES = ['os_credential']
EXTRA_CREDENTIAL_VARIABLES = []
FULL_CREDENTIAL_SEPARATOR = ', '

OS_JOBS = {}
JOB_URL = ''
JOB_TEMPLATE_AUTO_DISCOVERY = False
JOB_TEMPLATE_ORGANIZATION = ''
JOB_TEMPLATE_NAME_PATTERN = '*hello*{os_name}*'
JOB_TEMPLATE_OS_NAMES = {
    'linux': 'unix',
    'aix': 'aix',
    'windows': 'window',
}
JOB_VARIABLE_OVERRIDES_BY_OS = {
    'windows': {},
    'linux': {},
    'aix': {},
}

OUTPUT_FILE = 'awx_inventory_hosts.json'
EXCEL_OUTPUT_FILE = 'awx_inventory_hosts.xlsx'
INPUT_FILE = None
SHEET_NAME = 'Results'
EXCEL_COLUMNS = []
REFRESH_INVENTORY_PATTERN = '*_mirror_inventory'
HOST_COLUMN = None
HOSTS = []

BATCH_SIZE = 10
EXECUTION_COLUMN = 'exceiute_job'
JOB_TYPE = ''
JOB_TYPE_VARIABLE = 'action'

SURVEY_EXCEL_DEFAULTS = {
    'installtesttypename': 'testads',
}
SURVEY_ANSWERS = {
    'installtesttypename': 'reinstall',
}
SURVEY_INTERACTIVE = False

WAIT_FOR_COMPLETION = False
POLL_INTERVAL = 5
JOB_WAIT_TIMEOUT = 3600
OUTPUT_COLUMN = 'output'
OUTPUT_DIRECTORY = 'output'

DEBUG = False
DRY_RUN = False
COLOR = 'auto'

INVENTORY_OVERRIDES = {}
REFRESH_OVERRIDES = {}
EXECUTE_OVERRIDES = {}
OUTPUT_OVERRIDES = {}
TEMPLATES_OVERRIDES = {}
```

Przykład zachowuje obecną różnicę między defaultem Excela `testads` a odpowiedzią `reinstall`. Zmień ją świadomie według sekcji o survey.

Plik `awx_variable.example` w repozytorium zawiera fragmenty konfiguracji. Nie zawiera całego zestawu połączenia i wszystkich wymaganych ustawień, więc nie jest zamiennikiem kompletnego pliku bez uzupełnienia.

## 17. Typowe problemy i ich przyczyny

| Objaw | Co sprawdzić |
| --- | --- |
| Nie można znaleźć Excela. | Efektywne `--file` / `INPUT_FILE` / `EXCEL_OUTPUT_FILE` i katalog uruchomienia. Wybrane `HOSTS` pozwalają przygotować nowy plik, ale zwykły odczyt wymaga istniejącego. |
| `template_job_name` i `Job_Link` mają `none`. | Czy eksport dostał `JOB_URL` lub `OS_JOBS`? Jeśli nie, użyj poprawnie skonfigurowanego refresh. |
| Refresh nie znajduje hostów z `INVENTORY`. | Refresh używa `REFRESH_INVENTORY_PATTERN`, nie wspólnego `INVENTORY`. |
| Kilka inventory zawiera ten sam host. | Zawęź `REFRESH_INVENTORY_PATTERN`. |
| Brak szablonu albo kilka dopasowań. | Organizację równą ApplicationID, mapowanie OS i wszystkie fragmenty `JOB_TEMPLATE_NAME_PATTERN`. |
| Nazwa szablonu w Excelu nie zgadza się z AWX. | Odśwież Excel albo popraw nazwę i link. Nie zmieniaj tylko tekstu przy pozostawionym innym hiperlinku. |
| OS z Excela nie zgadza się z AWX. | `ostype` hosta w inventory przypisanym do szablonu; odśwież dane przed rzeczywistym uruchomieniem. |
| Host ma `AWX_Status=not_found`. | Wyszukiwanie inventory i rzeczywistą obecność hosta. Sam wpis hostname nie wystarcza do startu. |
| Kilka jobów używa tego samego szablonu. | Różnice OS, zmiennych, credentiali, metadanych albo przekroczenie `BATCH_SIZE`. To oczekiwany podział. |
| Zmiana `SURVEY_ANSWERS` nie zmienia akcji. | Komórkę `Survey_...`, `Job_Variables` i nazwę `JOB_TYPE_VARIABLE`. Excel może mieć wyższy priorytet. |
| Pytanie survey nie pojawia się w Excelu. | Czy ankieta jest wykryta dla wybranego szablonu i klucz odpowiada Answer Variable Name. |
| Zmienna spoza survey jest odrzucana. | Czy szablon przyjmuje Variables przez Prompt on launch. |
| Credential nie istnieje / nazwa jest niejednoznaczna. | Nazwę, ID i uprawnienia tokena do odczytu credentiali. |
| AWX nie przyjmuje `Full_Credential`. | Prompt on launch dla Credentials oraz konflikty typów credentiali. |
| Skrypt nie uruchamia ponownie wierszy. | Istniejące wpisy w `EXECUTION_COLUMN` i potwierdzenie rerunu. |
| Pozostał `LAUNCH_PENDING`. | Stan w AWX przed zmianą znacznika; wynik wcześniejszego żądania startu nie został potwierdzony. |
| Nie można zapisać Excela. | Czy plik jest otwarty w Excelu, uprawnienia do katalogu i poprawność ścieżki. |
| Log w Excelu jest skrócony. | Pełny plik w `OUTPUT_DIRECTORY`; obowiązuje limit komórki. |
| Log jest jeszcze niepełny. | Status joba i `WAIT_FOR_COMPLETION`; ponów pobieranie po zakończeniu. |
| `DEBUG=True`, ale joby się uruchamiają. | Debug jest diagnostyką; podgląd włącza `DRY_RUN` lub `--dry-run`. |
| Błąd nieznanego/powtórzonego ustawienia. | Literówkę, drugie przypisanie na poziomie głównym albo próbę wpisania nieobsługiwanej nazwy. |

## 18. Środowisko i źródła zachowania

Launchery uruchamiają wspólną implementację z `lib/`. Jeżeli Python działa poza venv, mechanizm startowy tworzy lub wykorzystuje `.venv` obok skryptów i uruchamia w nim launcher ponownie. Już aktywny zewnętrzny venv jest respektowany.

Brakujące openpyxl, a w razie potrzeby także PyYAML, mogą być instalowane w używanym środowisku wirtualnym. Zmiana `awx_variable` nie wymaga przebudowania środowiska; konfiguracja jest odczytywana przy kolejnym uruchomieniu.

Testy znajdują się w katalogu `testy`. Z katalogu `Playground` uruchom cały zestaw:

```powershell
python -m unittest discover -s testy -v
```

Pojedynczy moduł można uruchomić jako pakiet, np. `python -m unittest testy.test_awx_credentials -v`. Testy używają klienta AWX z danymi testowymi i nie uruchamiają rzeczywistych jobów.

Dokumentację zweryfikowano względem lokalnych plików:

- [awx_variable](awx_variable) — bieżące ustawienia i komentarze.
- [lib/awx_config.py](lib/awx_config.py) — składnia, priorytety, łączenie i walidacja konfiguracji.
- [lib/awx.py](lib/awx.py) — eksport, wykonanie, odświeżanie, logi i argumenty komendy.
- [lib/awx_templates.py](lib/awx_templates.py) — dobór organizacji i szablonu.
- [lib/awx_survey.py](lib/awx_survey.py) oraz [lib/awx_survey_excel.py](lib/awx_survey_excel.py) — odpowiedzi, typy i kolumny ankiet.
- [lib/awx_excel.py](lib/awx_excel.py) — układ i zapis Excela oraz arkusz `Ustawienia`.
- [lib/awx_jumphosts.py](lib/awx_jumphosts.py) — wybór jumphostów i synchronizacja credentiali.
- [lib/awx_console.py](lib/awx_console.py) oraz [lib/awx_dependencies.py](lib/awx_dependencies.py) — kolory i środowisko uruchomieniowe.
