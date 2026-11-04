"""Central implementation for all four AWX command-line launchers.

Run a root launcher, or: python -m lib.awx {inventory,refresh,execute,output} [options]
Defaults come from awx_variable; launchers can provide local VARIABLES overrides. API, metadata and workflow logic
live here; Excel layout and configuration parsing use sibling modules.
"""

import argparse
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from .awx_dependencies import ensure_virtual_environment

if __name__ == "__main__":
    ensure_virtual_environment()

from . import awx_config as common
from . import awx_console as console
from . import awx_excel as excel
from . import awx_jumphosts as jumphosts
from . import awx_survey as survey
from . import awx_survey_excel as survey_excel
from . import awx_templates as templates
from .awx_config import AWXError, apply_job_type
from .awx_excel import ensure_columns
from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException



# INVENTORY WORKFLOW

VARIABLES_FILE = common.VARIABLES_FILE
INVENTORY_DEFAULTS = common.INVENTORY_DEFAULTS
globals().update(deepcopy(INVENTORY_DEFAULTS))
LOGGER = logging.getLogger('awx_inventory_hosts')
LOGGER.addHandler(logging.NullHandler())
LOGGER.setLevel(logging.WARNING)
LOGGER.propagate = False

class DebugFormatter(logging.Formatter):
    """Redact the token, including any occurrences in API response data."""

    def __init__(self, token, stream=None):
        super().__init__('[%(levelname)s] %(message)s')
        self.token = token
        self.stream = stream

    def format(self, record):
        message = super().format(record)
        message = message.replace(self.token, '***') if self.token else message
        level = "error" if record.levelno >= logging.ERROR else "warning" if record.levelno >= logging.WARNING else "debug" if record.levelno <= logging.DEBUG else "info"
        return console.colorize(message, level, stream=self.stream if self.stream is not None else sys.stderr)

def configure_debug(enabled, token=''):
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(DebugFormatter(token, handler.stream))
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.DEBUG if enabled else logging.WARNING)

def debug_variables(label, values):
    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug('%s: %s', label, json.dumps(values, ensure_ascii=False, default=str))


def debug_step(operation, stage, **details):
    """Opisz etap operacji i jawnie wybrane dane diagnostyczne w osobnych liniach."""
    if LOGGER.isEnabledFor(logging.DEBUG):
        lines = [f'{operation} — {stage}']
        lines.extend(f'  {name.replace("_", " ")}: {value}' for name, value in details.items())
        LOGGER.debug('\n'.join(lines))

def validate_connection_settings(settings):
    for name in ('AWX_URL', 'AWX_TOKEN'):
        if not isinstance(settings[name], str) or not settings[name].strip():
            raise AWXError(f"{name} must be a nonempty string.")
    timeout = settings['TIMEOUT']
    if type(timeout) not in (int, float) or timeout <= 0 or not math.isfinite(timeout):
        raise AWXError('TIMEOUT must be a positive number of seconds.')
    if type(settings['PAGE_SIZE']) is not int or settings['PAGE_SIZE'] <= 0:
        raise AWXError('PAGE_SIZE must be a positive integer.')
    return settings


def validate_inventory_settings(settings, *, require_inventory=True):
    """Validate setting types and prepare output file paths."""
    validate_connection_settings(settings)
    if settings['OS_JOBS'] is None:
        settings['OS_JOBS'] = {}
    jobs = settings['OS_JOBS']
    if not isinstance(jobs, dict) or any((os not in ('windows', 'linux', 'aix') for os in jobs)):
        raise AWXError('OS_JOBS must be a dictionary with windows, linux, or aix keys.')
    for os, job in jobs.items():
        if not isinstance(job, dict) or set(job) - {'url', 'variables'}:
            raise AWXError(f"OS_JOBS[{os!r}] must contain url and variables settings.")
        url = job.get('url', '')
        if not isinstance(url, str) or (url.strip() and (urlsplit(url.strip()).scheme not in ('http', 'https') or not urlsplit(url.strip()).netloc)):
            raise AWXError(f"OS_JOBS[{os!r}].url must be an HTTP(S) link or an empty string.")
        if not isinstance(job.get('variables', {}), dict):
            raise AWXError(f"OS_JOBS[{os!r}].variables must be a dictionary.")
        try:
            json.dumps(job.get('variables', {}), allow_nan=False)
        except (TypeError, ValueError):
            raise AWXError(f"OS_JOBS[{os!r}].variables must contain valid JSON values.") from None
    inventory = settings['INVENTORY']
    if require_inventory and not (isinstance(inventory, str) and inventory.strip() or (type(inventory) is int and inventory > 0)):
        raise AWXError('INVENTORY must be a name or a positive numeric ID.')
    variables = settings['CREDENTIAL_VARIABLES']
    if not isinstance(variables, (list, tuple)) or any((not isinstance(name, str) or not name.strip() for name in variables)):
        raise AWXError('CREDENTIAL_VARIABLES must be a list of nonempty variable names.')
    extras = settings['EXTRA_CREDENTIAL_VARIABLES']
    if not isinstance(extras, (list, tuple)) or any((value is not None and (not isinstance(value, (str, int, float, bool))) for value in extras)):
        raise AWXError('EXTRA_CREDENTIAL_VARIABLES must be a list of literal credential values.')
    if not isinstance(settings['FULL_CREDENTIAL_SEPARATOR'], str):
        raise AWXError('FULL_CREDENTIAL_SEPARATOR must be a string.')
    for name in ('OUTPUT_FILE', 'EXCEL_OUTPUT_FILE'):
        value = settings[name]
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise AWXError(f"{name} must be a file path.")
        settings[name] = Path(value)
    return settings

def read_inventory_settings(variables_file=None, *, require_inventory=True):
    """Read shared variables and launcher overrides, then validate inventory fields."""
    LOGGER.debug("Konfiguracja: odczytuję ustawienia z %s", variables_file or common.current_variables_file())
    return validate_inventory_settings(common.read_inventory_settings(variables_file), require_inventory=require_inventory)

def apply_settings(settings):
    """Make validated settings available to the rest of the script."""
    globals().update({name: settings[name] for name in INVENTORY_DEFAULTS})


def apply_connection_settings(settings):
    globals().update({name: settings[name] for name in common.CONNECTION_KEYS})


def load_connection(debug=False, *, require_inventory=True):
    """Load the same AWX settings and redact debug logging for every operation."""
    configure_debug(debug, AWX_TOKEN if isinstance(AWX_TOKEN, str) else "")
    connection = read_inventory_settings(require_inventory=require_inventory)
    apply_settings(connection)
    configure_debug(debug, connection["AWX_TOKEN"].strip())
    return connection


def load_refresh_connection(debug=False):
    """Discard manual job settings before validation, including legacy overrides."""
    configure_debug(debug, AWX_TOKEN if isinstance(AWX_TOKEN, str) else '')
    connection = common.read_inventory_settings()
    connection['OS_JOBS'] = {}
    validate_inventory_settings(connection, require_inventory=False)
    apply_settings(connection)
    configure_debug(debug, connection['AWX_TOKEN'].strip())
    return connection


def workbook_path(input_file, connection, *preferences):
    selected = input_file or next((settings["INPUT_FILE"] for settings in preferences if settings.get("INPUT_FILE")), None)
    path = Path(selected or connection["EXCEL_OUTPUT_FILE"]).resolve()
    if path.suffix.casefold() != ".xlsx":
        raise AWXError("The input file must use the .xlsx extension.")
    return path


def open_workbook_sheet(path, preferred="Results", explicit=None):
    """One workbook loader, worksheet selection policy and cleanup path."""
    LOGGER.debug('Excel: otwieram plik %s', path)
    try:
        workbook = load_workbook(path, keep_links=True)
    except FileNotFoundError:
        raise AWXError(f"Plik Excel nie istnieje: {path}. "
                       "Sprawdź ścieżkę lub wskaż istniejący plik opcją --file.") from None
    except (OSError, ValueError, BadZipFile, ParseError, InvalidFileException) as exc:
        raise AWXError(f"Cannot read {path}: {exc}") from None
    try:
        sheet = excel.select_sheet(workbook, preferred, explicit)
        debug_step('Excel', 'wybrano arkusz', Plik=path, Arkusz=sheet.title,
                   Wiersze_danych=max(0, sheet.max_row - 1), Kolumny=sheet.max_column)
        return workbook, sheet
    except BaseException:
        workbook.close()
        raise


OPERATION_HELP = {
    "inventory": r"""Działanie:
  Pobiera hosty, inventory, ostype oraz credentiale z grup AWX.
  ApplicationID po INVENTORY: pierwszy człon nazwy inventory przed znakiem _.
  Job_Credentials po Job_Link: credentiale przypisane do szablonu w AWX.
  HOPS: nazwy grup hosta i liczby z wzorca <liczba>_hop lub <liczba>hop,
  także z grup nadrzędnych. Kilka dopasowań: osobne linie; brak: none.
  HOPS_COUNT po HOPS zawiera samą liczbę całkowitą. Brak dopasowania lub
  różne liczby w grupach pozostawiają pustą komórkę.
  jumphost_for_hops po HOPS_COUNT: credentiale z ApplicationID i dokładnie
  wskazaną liczbą hopów. Domyślnie najniższe ID; alternatywy na liście w Excelu.
  Extra_Credentials: niepusta EXTRA_CREDENTIAL_VARIABLES z konfiguracji,
  a przy [] credentiale przypisane do wybranego szablonu joba.
  Full_Credential łączy Extra_Credentials, OS Credential i wybrane jumphost_for_hops bez powtórzeń.
  Credential hosta zastępuje odziedziczony credential szablonu tego samego typu.
  Credentiale Vault są zachowywane; podczas startu sprawdzane są ich Vault IDs.
  Job_Variables pobiera extra_vars wybranego szablonu (JSON/YAML).
  OS_JOBS[system]['variables'] nadpisuje zmienne szablonu; JOB_TYPE ustawia akcję playbooka.
  Zapisuje nowy JSON i Excel pod OUTPUT_FILE oraz EXCEL_OUTPUT_FILE.
  W Excelu wykrywa włączony survey szablonów i dodaje Survey_<nazwa_zmiennej>.
  Komórki można edytować; wartości początkowe: Job_Variables > SURVEY_EXCEL_DEFAULTS
  > SURVEY_ANSWERS > default AWX. JOB_URL ma pierwszeństwo przed OS_JOBS w Job_Link
  i przy wykrywaniu survey; pusty JOB_URL korzysta z ustawień systemu hosta.
  Wynikowy Excel jest tworzony od nowa. Odświeżanie z zachowaniem historii:
  użyj awx_refresh_excel.py.

Wejście:
  Bez INPUT_FILE: wszystkie hosty wybranego INVENTORY (nazwa lub ID).
  Z --file lub INPUT_FILE: nazwy z kolumny host_limit w pliku .xlsx.
  Plik wejściowy i wynikowy Excel muszą mieć różne adresy.

Przykłady:
  python .\awx_inventory_hosts.py
  python .\awx_inventory_hosts.py --file .\lista_hostow.xlsx -d""",
    "refresh": r"""Działanie:
  Aktualizuje ten sam plik Excel, używając nazw hostów jako źródła wyszukiwania.
  Ignoruje INVENTORY z konfiguracji i dotychczasowe INVENTORY/ApplicationID w Excelu.
  Szuka nazw hostów w inventory pasujących do REFRESH_INVENTORY_PATTERN.
  Domyślnie: *_mirror_inventory; * oznacza dowolny ciąg, ? jeden znak.
  ApplicationID jest dynamiczne: pierwszy człon nazwy znalezionego inventory przed _.
  Hosty w jednym pliku mogą pochodzić z różnych pasujących inventory.
  Kilka dopasowań tej samej nazwy hosta powoduje błąd przed zapisem pliku.
  Zawsze wyszukuje szablon według JOB_TEMPLATE_NAME_PATTERN w organizacji AWX
  o nazwie równej ApplicationID hosta. template_job_name pokazuje znalezioną nazwę.
  Ignoruje ręczny Job_Link, stałą organizację z konfiguracji oraz stare dane w Excelu.
  {os_name}: Linux -> unix, AIX -> aix, Windows/window -> window (JOB_TEMPLATE_OS_NAMES).
  Wszystkie fragmenty oddzielone * lub spacjami muszą wystąpić w nazwie; kolejność dowolna.
  Wzorzec obsługuje też {hostname}, {application_id} i {ostype}.
  Job_Variables: świeże extra_vars szablonu z AWX (JSON/YAML), następnie nadpisania
  dla ostype z JOB_VARIABLE_OVERRIDES_BY_OS. Puste {} pozostawia dane AWX.
  Pozostałe zmienne są zachowywane, zagnieżdżone obiekty łączone, listy zastępowane.
  Job_Type odczytuje z końcowych zmiennych, pod kluczem JOB_TYPE_VARIABLE.
  Brak lub kilka szablonów dla hosta powoduje błąd przed zapisem pliku.
  Odświeża INVENTORY, ApplicationID, ostype, credentiale, szablon, zmienne i akcję joba.
  Znalezioną nazwę, organizację i link pokazuje również w konsoli.
  Job_Credentials pokazuje aktualne credentiale szablonu, niezależnie od Extra_Credentials.
  Extra_Credentials: EXTRA_CREDENTIAL_VARIABLES, a przy [] credentiale szablonu.
  Full_Credential jest przeliczane razem z credentialami hosta.
  jumphost_for_hops zachowuje wybór przy tym samym ApplicationID i HOPS_COUNT.
  Wyczyść komórkę, aby przywrócić domyślny credential przy odświeżeniu.
  Domyślny credential szablonu jest zastępowany przez credential hosta tego samego
  typu (poza Vault). Jawne EXTRA_CREDENTIAL_VARIABLES są zachowywane.
  Odtwarza brakujące kolumny oraz wspólne formatowanie i układ tabeli.
  Dodaje kolumny Survey_<nazwa_zmiennej> z pytań włączonego survey.
  Nadpisuje odpowiedzi wymienione w SURVEY_EXCEL_DEFAULTS / SURVEY_ANSWERS,
  również w już wypełnionych komórkach. SURVEY_EXCEL_DEFAULTS ma pierwszeństwo.
  Pozostałe odpowiedzi zachowuje, a puste komórki wypełnia defaultami.
  Zachowuje historię EXECUTION_COLUMN, output, własne kolumny i inne arkusze.
  Sortuje: Windows, Linux, AIX; pozostałe systemy i puste hosty na końcu.

Wejście i statusy:
  Jedna nazwa hosta w komórce. Automatyczne kolumny: hostname, host_limit, host, hosts.
  Przecinki są usuwane z nazw hostów przed wyszukiwaniem i zapisem do Excela.
  --host-column wskazuje własną nazwę kolumny. Pusta lista przygotowuje nagłówki.
  AWX_Status: found = znaleziony, not_found = brak w AWX, no_hostname = pusty host.

Przykłady:
  python .\awx_refresh_excel.py -file .\awx_inventory_hosts.xlsx -d
  python .\awx_refresh_excel.py -file .\hosty.xlsx --sheet Results --host-column host_limit""",
    "templates": r"""Działanie:
  Wyświetla nazwy, ID i linki szablonów z JOB_TEMPLATE_ORGANIZATION (nazwa lub ID).
  JOB_TEMPLATE_NAME_PATTERN wymaga wszystkich fragmentów, w dowolnej kolejności.
  Fragmenty oddzielaj * lub spacjami; ? dopasowuje jeden znak. Wielkość liter bez znaczenia.
  Domyślnie: *zabbix*install*{os_name}*.
  {os_name}: Linux -> unix, AIX -> aix, Windows/window -> window (JOB_TEMPLATE_OS_NAMES).
  Bez --hostname wyszukuje warianty dla wszystkich systemów z tego słownika.
  Z --hostname pobiera ostype i ApplicationID hosta z REFRESH_INVENTORY_PATTERN.
  Bez --organization używa organizacji o nazwie równej ApplicationID hosta.
  Wzorzec może korzystać z {hostname}, {application_id}, {ostype}, {os_name}.
  Ustawienia można nadpisać w VARIABLES launchera i TEMPLATES_OVERRIDES.

Przykłady:
  python .\awx_find_templates.py --organization IT2
  python .\awx_find_templates.py --organization IT2 --name-pattern '*zabbix*install*windows'
  python .\awx_find_templates.py --organization IT2 --hostname server1""",
    "execute": r"""Działanie:
  Czyta hostname, Job_Link, Job_Variables i Full_Credential z Excela.
  Łączy hosty o wspólnym szablonie (ID i nazwa), OS, zmiennych i credentialach w paczki BATCH_SIZE.
  Różne OS, Job_Variables lub Full_Credential oznaczają osobne uruchomienia.
  Podział uwzględnia też INVENTORY, ApplicationID, HOPS, HOPS_COUNT, Job_Type
  oraz kolumny credentiali i jumphostów. Różnice oznaczają oddzielne joby
  tego samego szablonu. Kolejność kluczy JSON i credentiali nie zmienia grupy.
  Różne Job_Variables z Excela pozostają rozdzielone również po nadpisaniach.
  Ten sam hostname z różnymi parametrami również może mieć osobne uruchomienia.
  Sprawdza template_job_name względem Job_Link w AWX oraz zgodność URL z hiperlinkiem.
  Przed pierwszym startem porównuje ostype z hostem w inwentarzu szablonu.
  Brak OS lub niezgodność zatrzymuje wszystkie paczki przed startem i zapisem.
  Niezgodność zatrzymuje start przed wysłaniem żądania uruchomienia.
  Limit każdej paczki zaczyna się od localhost, np. localhost,host1,host2.
  localhost występuje raz; dodany automatycznie nie zużywa miejsca w BATCH_SIZE.
  Full_Credential zawiera nazwy/ID oddzielone FULL_CREDENTIAL_SEPARATOR.
  Full_Credential jest uzupełniane wyłącznie o wybór z jumphost_for_hops.
  Zmiana wyboru zastępuje wcześniej dołączone jumphosty przy odświeżeniu/startowaniu.
  Lista rozwijana wybiera jedną pozycję; wiele nazw/ID można wpisać po separatorze.
  Puste pole lub none nie dodaje jumphostów. Nadal wymagany jest jeden credential
  każdego typu (Vault IDs mogą się różnić).
  Eksport/refresh bierze Extra_Credentials z EXTRA_CREDENTIAL_VARIABLES,
  a przy pustej liście z szablonu joba wskazanego przez Job_Link / JOB_URL.
  Są to credentiale joba zapisane w AWX, używane przez uruchamiany playbook.
  AWX_TOKEN służy do uwierzytelniania API. Odczyt /credentials/ zamienia nazwy
  credentiali joba na ID wymagane w polu credentials żądania uruchomienia.
  Puste Full_Credential lub none zachowuje credentiale skonfigurowane na szablonie.
  Szablon musi przyjmować Limit, a przy ich nadpisaniu także Credentials i Variables
  (albo przekazywane zmienne muszą być obsługiwane przez survey).
  Credentials: w edycji szablonu zaznacz Prompt on launch i zapisz ustawienie.
  Odpowiada mu pole API ask_credential_on_launch=True.
  Po walidacji pokazuje plan wszystkich jobów: nazwę i ID szablonu, URL, OS,
  typ joba oraz pełny limit hostów (z localhost). Czeka na potwierdzenie [tak/N].
  tak/t/y/yes zatwierdza cały plan; Enter, inna odpowiedź lub brak terminala
  anuluje wykonanie bez uruchamiania jobów i zapisu Excela.

Wybrane hosty i URL:
  --hosts host1 host2 lub HOSTS=['host1', 'host2'] ogranicza listę wykonania.
  HOSTS=[] oznacza dotychczasowy tryb: wszystkie hosty z Excela.
  Brakujące w arkuszu hosty są pobierane z INVENTORY w AWX i dopisywane.
  Przy podaniu hostów plik może nie istnieć: skrypt utworzy arkusz z wynikami.
  Szablon pochodzi z Excela. Jawne --job-url nadpisuje Job_Link i template_job_name.
  URL wskazuje szablon UI/API na serwerze z AWX_URL, np. /job_template/7/details.
  JOB_URL z konfiguracji nie zastępuje linków z Excela.

Akcja i historia:
  Job_Variables to obiekt JSON, np. {"action": "install"}.
  Akcja: --job-type > odpowiedzi survey > Job_Variables > kolumna Job_Type > JOB_TYPE.
  Trafia do extra_vars[JOB_TYPE_VARIABLE], domyślnie action.
  Link WebUI joba zapisuje w EXECUTION_COLUMN (domyślnie exceiute_job_link).
  Stara kolumna exceiute_job jest przenoszona z zachowaniem historii.
  Po zakończeniu joba zapisuje Artifacts (JSON) i artifact filter (message hosta).
  Gdy WAIT_FOR_COMPLETION=False, pobierz końcowe artefakty przez awx_job_output.py.
  Zakres: 1 / Enter — tylko wpisy bez linku, 2 — wszystkie wybrane wpisy,
  0 — anuluj. --execution-scope missing/all zastępuje pytanie o zakres.
  EXECUTION_SCOPE='ask' (domyślnie) włącza pytanie. HOSTS / --hosts nadal ogranicza zakres.
  Pełny plan wymaga osobnego potwierdzenia [tak/N] przed zapisem i startem.
  Przy ponowieniu poprzedni link zostaje w komentarzu komórki EXECUTION_COLUMN.
  --dry-run nie pyta o zakres i start; domyślnie pokazuje tylko wpisy bez linku.
  --execution-scope all --dry-run pokazuje również wpisy z historią.
  LAUNCH_PENDING wymaga sprawdzenia uruchomienia w AWX i nie jest ponawiany pytaniem.
  WAIT_FOR_COMPLETION=True czeka na sukces; błąd zatrzymuje kolejne paczki.

Survey:
  Odpowiedzi trafiają do extra_vars pod Answer Variable Name pytania w AWX.
  Kolumny Survey_<nazwa_zmiennej> pozwalają edytować odpowiedzi w Excelu.
  Wybór: --survey-answers JSON > kolumna Survey_... > Job_Variables
  > SURVEY_ANSWERS > default survey. Pusta komórka korzysta z wartości domyślnej.
  Odpowiedzi dla innych szablonów są pomijane. Liczby i listy są konwertowane
  do typów AWX; multiselect przyjmuje JSON, np. ["first", "second"].
  --job-type ma pierwszeństwo dla JOB_TYPE_VARIABLE; kolumna Survey_... ma
  pierwszeństwo przed Job_Type / JOB_TYPE. Nazwę zmiennej akcji można zmienić.
  --show-survey pokazuje pytania, wybory i defaulty bez startu jobów i zapisu.
  --survey / SURVEY_INTERACTIVE=True pyta w terminalu o brakujące odpowiedzi.
  Enter zachowuje default; pytania opcjonalne można pominąć. Hasła są ukryte.
  Odpowiedzi z terminala są wspólne dla paczek tego szablonu; nie trafiają do Excela.
  Eksport tworzy kolumny. Refresh synchronizuje także wypełnione komórki
  z SURVEY_EXCEL_DEFAULTS / SURVEY_ANSWERS dla wykrytych pytań.
  Ręczne odpowiedzi można zmienić po refresh, przed uruchomieniem joba.
  Klucze SURVEY_EXCEL_DEFAULTS dla innych szablonów nie są wysyłane do joba.
  Skrypt sprawdza wymagane pola, typy, limity i wybory przed pierwszym startem.
  Survey musi być włączone w AWX. Zdefiniowanie pytań nie włącza survey.

Przykłady:
  python .\awx_execute_jobs.py --dry-run
  python .\awx_execute_jobs.py --show-survey
  python .\awx_execute_jobs.py --survey
  python .\awx_execute_jobs.py --survey --dry-run
  python .\awx_execute_jobs.py --execution-scope missing
  python .\awx_execute_jobs.py --execution-scope all --dry-run
  python .\awx_execute_jobs.py -file .\hosty.xlsx --batch-size 10 --job-type install
  python .\awx_execute_jobs.py --hosts host1 --job-url "https://awx.example.com/#/templates/job_template/7/details" --dry-run
  --dry-run: bez startu jobów i zapisu. Brakujące metadane może odczytać z AWX.
  Zwykły --dry-run pokazuje nazwy zmiennych i maskuje wartości; bez walidacji survey.
  --survey --dry-run odczytuje i waliduje survey oraz umożliwia podanie odpowiedzi.
  --no-dry-run: wyłącza DRY_RUN ustawione w pliku lub launcherze.""",
    "output": r"""Działanie:
  Czyta linki z EXECUTION_COLUMN (domyślnie exceiute_job_link) i system z ostype.
  Pobiera pełny stdout raz na unikalny job, także failed/error/canceled.
  Wpisuje log do OUTPUT_COLUMN we wszystkich powiązanych wierszach tego joba.
  Artifacts: JSON z pola artifacts joba. artifact filter: message dla hostname.
  Szuka pełnej nazwy hosta w kluczach JSON oraz polach host/hostname/inventory_hostname.
  Obsługuje zagnieżdżone słowniki/listy; wiele komunikatów łączy nowymi liniami.
  Brak dopasowanego message zostawia pustą komórkę; brak artefaktów zapisuje {}.
  Zapisuje pełny TXT: OUTPUT_DIRECTORY/<id_joba>.<inventory>.<os>.txt.
  Inventory pochodzi z danych joba AWX, a OS z Excela. Niedozwolone znaki nazw
  plików są zastępowane podkreśleniami.

Oczekiwanie i rozmiar logu:
  WAIT_FOR_COMPLETION=True czeka na zakończenie. False pobiera tekst dostępny teraz.
  Excel: maksymalnie 32767 znaków w komórce; pełny tekst pozostaje w pliku TXT.
  Ponowne uruchomienie odświeża logi i artefakty. Puste linki i LAUNCH_PENDING są pomijane.
  Długie artefakty są skracane w Excelu z adresem pełnych danych w API AWX.

Przykłady:
  python .\awx_job_output.py
  python .\awx_job_output.py -file .\hosty.xlsx --output-dir .\output -d""",
}
CONFIGURATION_HELP = """Ustawienia:
  Domyślne wartości: awx_variable obok launcherów, niezależnie od katalogu startu.
  Lokalne nadpisania: słownik VARIABLES w wybranym launcherze; {} używa pliku.
  Kolejność: argumenty komendy > VARIABLES > *_OVERRIDES operacji > wartości wspólne.
  Względne ścieżki plików liczą się od bieżącego katalogu uruchomienia.
  DEBUG pokazuje szczegóły zapytań; sam DEBUG nie włącza trybu --dry-run.
  Tylko DEBUG wykonuje przed operacją test API i AWX_TOKEN przez GET /me/.
  Błąd testu zatrzymuje operację przed odczytem i zmianą plików. --no-debug pomija test.
  COLOR: auto (terminal), always (wymuszenie), never (bez kolorów).
  --color nadpisuje COLOR; --no-color wyłącza kolory. NO_COLOR wyłącza tryb auto.
  URL-e w awx_variable mogą zawierać nowe linie; parser łączy je w jeden adres.
  Dotyczy AWX_URL, JOB_URL i url w OS_JOBS; teksty w variables zachowują nowe linie.
  W OS_JOBS['linux']['variables'] można wkleić YAML między klamry, bez przecinków.
  Zachowaj wcięcia YAML. Dotychczasowe słowniki Pythona również działają.
  Nazwy hostów są dopasowywane bez rozróżniania wielkich i małych liter.
  Job używa pisowni hosta z inventory AWX; niejednoznaczne nazwy powodują błąd.
  Full_Credential pomija Machine odziedziczone wyłącznie z szablonu.
  Pozostają w Job_Credentials i są zachowywane przy starcie, jeśli nie wybrano zamiennika Machine.
  Credentiale z OS Credential i jawne EXTRA_CREDENTIAL_VARIABLES są zachowywane.
  Zamknij aktualizowany plik w Excelu przed zapisem.
  Poza venv skrypt tworzy/używa .venv obok launcherów i restartuje się w nim.
  Brakujące openpyxl instaluje się automatycznie w venv używanego Pythona.
  Obsługa YAML doinstalowuje PyYAML w venv przy pierwszym użyciu.
  Wspólna implementacja wszystkich launcherów: katalog lib/."""


def operation_parser(description, *, with_sheet=True, with_file=True, operation=None):
    detail = OPERATION_HELP.get(operation, "")
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=(detail + "\n\n" if detail else "") + CONFIGURATION_HELP)
    file_help = "Filtr hostów z kolumny host_limit; wynik zapisuje pod EXCEL_OUTPUT_FILE." if operation == "inventory" else "Plik .xlsx aktualizowany w miejscu; nadpisuje INPUT_FILE."
    if with_file:
        parser.add_argument("-file", "--file", dest="input_file", type=Path, help=file_help)
    if with_sheet:
        parser.add_argument("--sheet", help="Nazwa arkusza; domyślnie SHEET_NAME lub aktywny arkusz.")
    parser.add_argument("-d", "-debug", "--debug", action="store_true", default=None,
                        help="Test połączenia i AWX_TOKEN przed operacją oraz szczegóły API; token jest maskowany.")
    parser.add_argument("--no-debug", dest="debug", action="store_false", default=None, help="Wyłącz DEBUG ustawione w zmiennych.")
    parser.add_argument("--color", choices=console.COLOR_MODES, default=None, help="Kolory: auto, always, never; nadpisuje COLOR.")
    parser.add_argument("--no-color", dest="color", action="store_const", const="never", default=None, help="Wyłącz kolory w terminalu.")
    return parser


def parse_operation_arguments(parser, argv=None):
    """Fill unset CLI options from the same launcher/file configuration."""
    args = parser.parse_args(argv)
    if args.color is not None:
        console.set_color_mode(args.color)
    defaults = common.read_runtime_settings()
    args.color = args.color if args.color is not None else defaults["COLOR"]
    console.set_color_mode(args.color)
    if args.debug is None:
        args.debug = defaults["DEBUG"]
    if hasattr(args, "dry_run") and args.dry_run is None:
        args.dry_run = defaults["DRY_RUN"]
    if hasattr(args, "host_column") and args.host_column is None:
        args.host_column = defaults["HOST_COLUMN"]
    return args


def parse_inventory_arguments(argv=None):
    return parse_operation_arguments(operation_parser("Pobierz hosty z AWX i utwórz eksport JSON oraz Excel.",
                                                     with_sheet=False, operation="inventory"), argv)

def read_host_limits(input_file):
    """Read unique hostnames from host_limit columns, preserving file order."""
    LOGGER.debug('Filtr hostów: otwieram %s i szukam kolumny host_limit', input_file)
    workbook = None
    hostnames = {}
    found_column = False
    try:
        workbook = load_workbook(input_file, read_only=True, data_only=True, keep_links=False)
        for sheet in workbook.worksheets:
            LOGGER.debug('Filtr hostów: przeszukuję arkusz %r', sheet.title)
            column_index = None
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if column_index is None:
                    matching_columns = [index for index, value in enumerate(row) if isinstance(value, str) and value.strip().casefold() == 'host_limit']
                    if len(matching_columns) > 1:
                        raise AWXError(f"Worksheet {sheet.title!r}, row {row_number}: more than one host_limit column was found.")
                    if matching_columns:
                        column_index = matching_columns[0]
                        found_column = True
                        LOGGER.debug('Filtr hostów: znaleziono host_limit\n  Arkusz: %r\n  Wiersz: %d\n  Kolumna: %s', sheet.title, row_number, get_column_letter(column_index + 1))
                    continue
                value = row[column_index] if column_index < len(row) else None
                if value is None:
                    continue
                for hostname in re.split('[,;\\s]+', str(value).strip()):
                    if hostname:
                        hostnames.setdefault(common.hostname_key(hostname), hostname)
    except (OSError, InvalidFileException, BadZipFile, ParseError) as exc:
        raise AWXError(f"Cannot read Excel file {input_file}: {exc}") from None
    finally:
        if workbook is not None:
            workbook.close()
    if not found_column:
        raise AWXError(f"No host_limit column was found in {input_file}.")
    if not hostnames:
        raise AWXError(f"The host_limit column in {input_file} contains no hostnames.")
    LOGGER.debug('Filtr hostów: odczytano %d unikalnych nazw hostów', len(hostnames))
    debug_variables(f"Nazwy hostów z pliku {input_file}", list(hostnames.values()))
    return list(hostnames.values())

class NoRedirect(HTTPRedirectHandler):
    """Do not forward the token header across HTTP redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class AWXClient:

    def __init__(self, base_url, token):
        self.configured_url = base_url.strip()
        parsed = urlsplit(base_url.strip())
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            raise AWXError('AWX_URL must include http:// or https:// and a server address.')
        if parsed.query or parsed.fragment:
            raise AWXError('AWX_URL must point to a server or API base URL without query parameters or a # fragment.')
        path = parsed.path.rstrip('/')
        if path.casefold().endswith('/v2'):
            self.api_path = path + '/'
        elif path.casefold().endswith('/api'):
            self.api_path = path + '/v2/'
        else:
            self.api_path = path + '/api/v2/'
        self.base_url = urlunsplit((parsed.scheme, parsed.netloc, self.api_path, '', ''))
        self.origin = self._origin(parsed)
        self.token = token
        self.sensitive_values = {token} if token else set()
        self.credential_cache = {}
        self.jumphost_credential_cache = {}
        self.organization_cache = {}
        self.template_search_cache = {}
        self.template_detail_cache = {}
        self.opener = build_opener(NoRedirect())
        self.request_count = 0
        LOGGER.debug('Połączenie z AWX: bazowy adres API dla żądań: %s', self.base_url)

    @staticmethod
    def _origin(parsed):
        try:
            port = parsed.port
        except ValueError:
            raise AWXError('The API URL contains an invalid port number.') from None
        if port is None:
            port = 443 if parsed.scheme == 'https' else 80
        return (parsed.scheme, (parsed.hostname or '').casefold(), port)

    def _resolve_url(self, path, *, relative_to=None):
        """Preserve the configured API base URL in pagination links."""
        url = urljoin(relative_to or self.base_url, path)
        parsed = urlsplit(url)
        if self._origin(parsed) != self.origin:
            raise AWXError(self.server_mismatch_message(url, 'API link'))
        if not parsed.path.startswith(self.api_path):
            for prefix in ('/api/controller/v2/', '/controller/v2/', '/api/v2/'):
                if parsed.path.casefold().startswith(prefix):
                    new_path = self.api_path + parsed.path[len(prefix):]
                    LOGGER.debug('Mapowanie ścieżki API: %s -> %s', parsed.path, new_path)
                    return urlunsplit((parsed.scheme, parsed.netloc, new_path, parsed.query, ''))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ''))

    def server_mismatch_message(self, url, label):
        """Show the effective URL and both origins, including protocol and port."""
        actual = self._origin(urlsplit(url))
        names = ('scheme', 'host', 'port')
        expected_text = ', '.join(f'{name}={value!r}' for name, value in zip(names, self.origin))
        actual_text = ', '.join(f'{name}={value!r}' for name, value in zip(names, actual))
        differences = ', '.join(name for name, expected, found in zip(names, self.origin, actual) if expected != found)
        return self.redact(
            f'{label} points to a different server than AWX_URL.\n'
            f'  {label} URL: {url!r}\n'
            f'  AWX_URL: {self.configured_url!r}\n'
            f'  Expected server: {expected_text}\n'
            f'  Actual server: {actual_text}\n'
            f'  Different: {differences}.\n'
            '  Check AWX_URL, OS_JOBS / JOB_URL and the Excel hyperlink target.'
        )

    def add_sensitive_values(self, values):
        self.sensitive_values.update(value for value in values if isinstance(value, str) and value and value != '$encrypted$')

    def redact(self, text):
        for value in sorted(self.sensitive_values, key=len, reverse=True):
            text = text.replace(value, '***').replace(json.dumps(value)[1:-1], '***')
        return text

    def request(self, method, path, payload=None, *, response_format="json", purpose="Pobranie zasobu AWX"):
        """One authenticated request path; launch requests are never automatically retried."""
        url = self._resolve_url(path)
        parsed = urlsplit(url)
        text_response = response_format == "text"
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "text/plain" if text_response else "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        self.request_count += 1
        number = self.request_count
        started = time.perf_counter()
        LOGGER.debug("Żądanie #%d — cel: %s", number, purpose)
        LOGGER.debug("Żądanie #%d: %s %s", number, method, url)
        try:
            with self.opener.open(request, timeout=TIMEOUT) as response:
                status = getattr(response, "status", None)
                if text_response:
                    content_type = response.headers.get_content_type()
                    if content_type in ("application/json", "text/html"):
                        raise AWXError(f"Expected text stdout from {parsed.path}, received {content_type}.")
                    result = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
                else:
                    result = json.load(response)
        except HTTPError as exc:
            code = exc.code
            try:
                detail = self.redact(exc.read(8192).decode("utf-8", errors="replace")) if method == "POST" else ""
            finally:
                exc.close()
            LOGGER.debug("Odpowiedź #%d: błąd HTTP %d\n  Czas żądania: %.3f s", number, code, time.perf_counter() - started)
            if method == "POST":
                raise AWXError(f"Launch returned HTTP {code}: {detail}") from None
            hint = {
                401: "The token is invalid or has expired.",
                403: "The token does not have permission to read this resource.",
                404: "The resource does not exist or is not visible to this token.",
                301: "Check AWX_URL: the server is redirecting the request.",
                302: "Check AWX_URL: the server is redirecting the request.",
            }.get(code, "The AWX server returned an error.")
            raise AWXError(f"HTTP {code}, {parsed.path}: {hint}") from None
        except (URLError, OSError, ValueError, LookupError) as exc:
            detail = self.redact(str(exc))
            if method == "POST":
                raise AWXError(f"Launch result is uncertain ({detail}); check AWX before clearing the Excel marker.") from None
            raise AWXError(f"Cannot fetch {parsed.path}: {detail}") from None
        if not text_response and not isinstance(result, dict):
            hint = " Check AWX before clearing the Excel marker." if method == "POST" else ""
            raise AWXError(f"Unexpected response format from {parsed.path}.{hint}")
        LOGGER.debug("Odpowiedź #%d: HTTP %s\n  Czas żądania: %.3f s", number, status, time.perf_counter() - started)
        if isinstance(result, dict) and isinstance(result.get("results"), list):
            LOGGER.debug("Wynik żądania #%d: rekordy na stronie=%d\n  Łącznie według API=%s\n  Kolejna strona=%s",
                         number, len(result["results"]), result.get("count", "brak danych"), "tak" if result.get("next") else "nie")
        return result

    def get(self, path, *, purpose="Pobranie zasobu AWX"):
        return self.request("GET", path, purpose=purpose)

    def post(self, path, payload):
        return self.request("POST", path, payload, purpose="Uruchomienie zadania AWX")

    def stdout(self, job_id):
        return self.request("GET", f"jobs/{job_id}/stdout/?format=txt_download", response_format="text",
                            purpose=f"Pobranie pełnego logu wykonania (stdout) zadania {job_id}")

    def items(self, path, *, purpose='Pobranie listy zasobów AWX', **filters):
        """Fetch all pages by following the API's next links."""
        parameters = {'page_size': PAGE_SIZE, **filters}
        debug_variables(f"Parametry żądania: {purpose}", parameters)
        query = urlencode(parameters)
        url = self._resolve_url(path)
        url += ('&' if urlsplit(url).query else '?') + query
        visited = set()
        page_number = 0
        record_count = 0
        while url:
            if url in visited:
                raise AWXError('The API returned a repeated pagination link.')
            visited.add(url)
            page_number += 1
            page = self.get(url, purpose=f"{purpose}\n  Strona: {page_number}")
            if not isinstance(page.get('results'), list):
                raise AWXError('The API response is missing the results list.')
            record_count += len(page['results'])
            yield from page['results']
            next_page = page.get('next')
            url = self._resolve_url(next_page, relative_to=url) if next_page else None
        LOGGER.debug('Zakończono: %s\n  Pobrane rekordy: %d\n  Pobrane strony: %d', purpose, record_count, page_number)

def check_connection(client, *, debug=False):
    """Verify API connectivity and token authentication only when debug is enabled."""
    if not debug:
        return
    console.print_record('TEST POŁĄCZENIA AWX (DEBUG)', {
        'Adres API': client.redact(client._resolve_url('me/')),
        'Uwierzytelnienie': 'AWX_TOKEN (wartość ukryta)',
    })
    started = time.perf_counter()
    try:
        response = client.get('me/', purpose='Test połączenia z AWX i uwierzytelnienia AWX_TOKEN przed operacją')
        users = response.get('results') if isinstance(response, dict) else None
        if not isinstance(users, list) or len(users) != 1 or not isinstance(users[0], dict):
            raise AWXError('Endpoint /me/ nie zwrócił danych uwierzytelnionego użytkownika.')
        user = users[0]
        if (type(user.get('id')) is not int or user['id'] < 1
                or not isinstance(user.get('username'), str) or not user['username'].strip()
                or user.get('is_active') is False):
            raise AWXError('Endpoint /me/ nie potwierdził aktywnego konta użytkownika.')
    except AWXError as exc:
        raise AWXError(client.redact(
            'Test połączenia z AWX nie powiódł się. Operacja została zatrzymana.\n'
            'Sprawdź AWX_URL, AWX_TOKEN i dostępność serwera.\n'
            f'Szczegóły: {exc}'
        )) from None
    console.print_message(client.redact(
        f"Połączenie z AWX i uwierzytelnienie: OK.\nUżytkownik: {user['username']} (ID: {user['id']}).\n"
        f'Czas testu: {time.perf_counter() - started:.2f} s. Rozpoczynam operację.'
    ), level='success', flush=True)


def find_inventory(client, selector):
    if isinstance(selector, int):
        if selector < 1:
            raise AWXError('The inventory ID must be positive.')
        return client.get(f"inventories/{selector}/", purpose=f"Odczyt szczegółów inwentarza ID {selector}")
    if not isinstance(selector, str) or not selector.strip():
        raise AWXError('Set INVENTORY to an inventory name or numeric ID.')
    matches = list(client.items('inventories/', name=selector, purpose=f"Wyszukanie inwentarza o nazwie {selector!r}"))
    if not matches:
        raise AWXError(f"No accessible inventory named {selector!r} was found.")
    if len(matches) > 1:
        ids = ', '.join((str(item['id']) for item in matches))
        raise AWXError(f"The name {selector!r} is ambiguous. Set INVENTORY to one of these IDs: {ids}.")
    return matches[0]

def credential_variable_names():
    """Return the variable names whose values are read from AWX groups."""
    variables = CREDENTIAL_VARIABLES
    if not isinstance(variables, (list, tuple)) or any((not isinstance(name, str) or not name.strip() for name in variables)):
        raise AWXError('CREDENTIAL_VARIABLES must be a list of nonempty variable names.')
    return tuple(dict.fromkeys((name.strip() for name in variables)))

def extra_credential_values():
    """Return literal values from the configuration, shared by all hosts."""
    if not isinstance(EXTRA_CREDENTIAL_VARIABLES, (list, tuple)):
        raise AWXError('EXTRA_CREDENTIAL_VARIABLES must be a list of literal credential values.')
    values = []
    for value in EXTRA_CREDENTIAL_VARIABLES:
        if value is None:
            continue
        if not isinstance(value, (str, int, float, bool)):
            raise AWXError('Values in EXTRA_CREDENTIAL_VARIABLES must be strings or numbers.')
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if text.strip() and text not in values:
            values.append(text)
    return tuple(values)

class InventoryGroupIndex:
    """A local index of groups and memberships, shared by all hosts."""

    def __init__(self, groups, exported, credential_variables=('os_credential',)):
        self.credential_variables = credential_variables
        self.groups = {group['name']: {'id': group['id'], 'name': group['name']} for group in groups}
        self.direct_by_host = {}
        self.parents = {name: set() for name in self.groups}
        self.variables = {}
        self.all_groups_cache = {}
        for name in self.groups:
            group_data = exported.get(name, {})
            if not isinstance(group_data, dict) or not isinstance(group_data.get('vars', {}), dict):
                raise AWXError(f"Invalid data for group {name!r} in the inventory export.")
            self.variables[name] = group_data.get('vars', {})
            LOGGER.debug('Grupa %r (ID %s): dostępne zmienne: %s\n  Wartości zmiennych grupy nie są wypisywane.',
                         name, self.groups[name]['id'], ', '.join(map(str, self.variables[name])) or 'brak')
            for hostname in group_data.get('hosts', []):
                self.direct_by_host.setdefault(common.hostname_key(hostname), set()).add(name)
            for child_name in group_data.get('children', []):
                if child_name not in self.parents:
                    raise AWXError(f"Group {child_name!r} is missing from the group list. The inventory may have changed during retrieval; run the script again.")
                self.parents[child_name].add(name)

    def all_group_names(self, name):
        """Resolve each group and its ancestors once, including multiple inheritance paths."""
        if name not in self.all_groups_cache:
            found = set()
            pending = [name]
            while pending:
                current = pending.pop()
                if current in found:
                    continue
                found.add(current)
                pending.extend(self.parents[current] - found)
            self.all_groups_cache[name] = found
        return self.all_groups_cache[name]

    def for_host(self, hostname):
        direct_names = self.direct_by_host.get(common.hostname_key(hostname), set())
        all_names = set()
        for name in direct_names:
            all_names.update(self.all_group_names(name))
        LOGGER.debug('Host %s: szukam zmiennych %s lokalnie w %d grupach\n  Grupy bezpośrednie: %d\n  Grupy nadrzędne: %d', hostname, self.credential_variables, len(all_names), len(direct_names), len(all_names - direct_names))
        matches = []
        sorted_names = sorted(all_names)
        for variable in self.credential_variables:
            previous_matches = len(matches)
            for name in sorted_names:
                if variable in self.variables[name]:
                    LOGGER.debug('Host %s: znaleziono zmienną %s w grupie %r (%s)', hostname, variable, name, 'bezpośredniej' if name in direct_names else 'nadrzędnej')
                    matches.append({**self.groups[name], 'variable': variable, 'value': self.variables[name][variable], 'inherited': name not in direct_names})
            if len(matches) == previous_matches:
                LOGGER.debug('Host %s: nie znaleziono zmiennej %s w jego grupach.', hostname, variable)
        result = {'groups': [self.groups[name] for name in sorted(direct_names)], 'all_groups': [self.groups[name] for name in sorted_names], 'os_credential_groups': [{'id': match['id'], 'name': match['name'], 'os_credential': match['value'], 'inherited': match['inherited']} for match in matches if match['variable'] == 'os_credential'], 'credential_groups': matches}
        debug_step('Host', 'zakończono wyszukiwanie poświadczeń w grupach', Host=hostname,
                   Grupy=', '.join(sorted_names) or 'brak', Dopasowania=len(matches))
        return result

def host_index(hosts):
    """Reject ambiguous inventory identities instead of selecting a random host."""
    result = {}
    for host in hosts:
        key = common.hostname_key(host['name'])
        if key in result and result[key] != host:
            raise AWXError(f"Ambiguous hostname: {result[key]['name']!r} and {host['name']!r} "
                           "match without case sensitivity. Rename the duplicate hosts in AWX.")
        result[key] = host
    return result


def host_name_filters(hostnames):
    """Use anchored, escaped, case-insensitive AWX filtering for literal names."""
    filters = {'order_by': 'id'}
    if hostnames:
        pattern = '^(' + '|'.join(re.escape(name) for name in hostnames) + ')$'
        if len(urlencode({**filters, 'page_size': PAGE_SIZE, 'name__iregex': pattern})) <= 6000:
            filters['name__iregex'] = pattern
    return filters


def fetch_report(client, selector, hostnames=None, *, allow_empty=False):
    inventory = find_inventory(client, selector)
    LOGGER.debug('Wybrano inwentarz: ID=%s\n  Nazwa=%r\n  Typ=%s', inventory['id'], inventory['name'], inventory.get('kind') or 'standard')
    requested = common.unique_hostnames(hostnames) if hostnames is not None else None
    requested_names = {common.hostname_key(name) for name in requested} if requested is not None else None
    host_filters = host_name_filters(requested)
    debug_variables('Filtry hostów wysyłane do AWX', host_filters)
    host_purpose = f"Pobranie hostów z inwentarza ID {inventory['id']} (nazwa: {inventory['name']!r})"
    if requested is not None:
        host_purpose += f" dla {len(requested)} nazw z pliku wejściowego"
    candidates = [{**host, 'inventory': host.get('inventory') or inventory['id']}
                  for host in client.items(f"inventories/{inventory['id']}/hosts/", purpose=host_purpose, **host_filters)
                  if requested_names is None or common.hostname_key(host['name']) in requested_names]
    if requested is not None and not candidates and not allow_empty:
        raise AWXError(f"None of the hosts listed in the file exist in inventory {inventory['name']!r}.")
    report = build_host_report(client, candidates, requested, inventory_names={inventory['id']: inventory['name']})
    return {'inventory': {'id': inventory['id'], 'name': inventory['name']}, **report}


def host_inventory_name(client, host, inventory_names):
    """Resolve each source inventory name once, preferring the host's API summary."""
    source_id = host['inventory']
    if source_id not in inventory_names:
        summary = (host.get('summary_fields') or {}).get('inventory') or {}
        inventory_names[source_id] = summary.get('name') or client.get(
            f"inventories/{source_id}/", purpose=f"Odczyt nazwy inwentarza źródłowego ID {source_id}"
        ).get('name') or str(source_id)
    return inventory_names[source_id]


def fetch_refresh_report(client, hostnames, inventory_pattern):
    """Find Excel hostnames across inventories matching the editable wildcard."""
    requested = common.unique_hostnames(hostnames)
    requested_names = {common.hostname_key(name) for name in requested}
    regex = '^' + ''.join('.*' if char == '*' else '.' if char == '?' else re.escape(char)
                         for char in inventory_pattern) + '$'
    filters = {**host_name_filters(requested), 'inventory__name__iregex': regex}
    debug_variables('Filtry wyszukiwania hostów z Excela w AWX', filters)
    candidates = []
    inventory_names = {}
    matched = {}
    if requested:
        for host in client.items('hosts/', purpose=f"Wyszukanie {len(requested)} hostów w inventory pasujących do {inventory_pattern!r}", **filters):
            key = common.hostname_key(host['name'])
            if key not in requested_names:
                continue
            inventory_name = host_inventory_name(client, host, inventory_names)
            if re.fullmatch(regex, inventory_name, re.IGNORECASE) is None:
                continue
            if key in matched and matched[key]['id'] != host['id']:
                previous = matched[key]
                raise AWXError(
                    f"Ambiguous hostname {host['name']!r}: found in inventories "
                    f"{inventory_names[previous['inventory']]!r} (ID {previous['inventory']}) and "
                    f"{inventory_name!r} (ID {host['inventory']}). "
                    "Narrow REFRESH_INVENTORY_PATTERN to select one host. Excel was not saved."
                )
            if key not in matched:
                matched[key] = host
                candidates.append(host)
    return build_host_report(client, candidates, requested, inventory_names=inventory_names)


def build_host_report(client, candidates, requested=None, *, inventory_names=None):
    """Load complete metadata once per source inventory for the selected hosts."""
    credential_variables = credential_variable_names()
    debug_variables('Dodatkowe nazwy lub ID poświadczeń z konfiguracji', extra_credential_values())
    debug_variables('Nazwy zmiennych poświadczeń do wyszukania', credential_variables)
    inventory_names = {} if inventory_names is None else dict(inventory_names)
    hosts = []
    group_indexes = {}
    host_variables = {}
    host_index(candidates)
    for host in candidates:
        source_id = host['inventory']
        host_inventory_name(client, host, inventory_names)
        if source_id not in group_indexes:
            LOGGER.debug('Buduję indeks grup dla inwentarza źródłowego ID %s.', source_id)
            groups = list(client.items(f"inventories/{source_id}/groups/", order_by='name', purpose=f"Pobranie nazw i ID grup z inwentarza źródłowego ID {source_id}"))
            exported = client.get(f"inventories/{source_id}/script/?all=1&hostvars=1", purpose=f"Pobranie zmiennych hostów i grup oraz hierarchii grup z inwentarza źródłowego ID {source_id}\n  Uwzględniam również hosty wyłączone")
            host_variables[source_id] = {common.hostname_key(name): values
                                         for name, values in exported.get('_meta', {}).get('hostvars', {}).items()}
            group_indexes[source_id] = InventoryGroupIndex(groups, exported, credential_variables)
            LOGGER.debug('Indeks inwentarza ID %s gotowy\n  Grupy: %d\n  Hosty z przypisanymi grupami: %d\n  Poświadczenia wyszukuję lokalnie w pobranych danych.', source_id, len(groups), len(group_indexes[source_id].direct_by_host))
        else:
            LOGGER.debug('Host %s: korzystam z gotowego indeksu grup inwentarza ID %s, bez ponownego pobierania.', host['name'], source_id)
        ostype = common.normalize_ostype(host_variables[source_id].get(common.hostname_key(host['name']), {}).get('ostype'))
        hosts.append({'id': host['id'], 'name': host['name'], 'INVENTORY': inventory_names[source_id], 'ostype': ostype, 'enabled': host.get('enabled', True), **group_indexes[source_id].for_host(host['name'])})
    report = {'host_count': len(hosts), 'hosts': hosts}
    if requested is not None:
        order = {common.hostname_key(name): index for index, name in enumerate(requested)}
        hosts.sort(key=lambda host: order[common.hostname_key(host['name'])])
        found_names = {common.hostname_key(host['name']) for host in hosts}
        report['missing_hosts'] = [name for name in requested if common.hostname_key(name) not in found_names]
    debug_step('Inwentarz', 'zakończono pobieranie hostów', Hosty=len(hosts),
               Inwentarze_źródłowe=len(group_indexes), Brakujące_hosty=', '.join(report.get('missing_hosts', [])) or 'brak')
    return report

def template_credential_entries(client, link, cache):
    """Read the selected template's launch credentials without fetching secrets."""
    if client is None or not link:
        return []
    template_id = template_id_from_link(link, client)
    details, _, _ = template_survey(client, template_id, cache)
    credentials = details.get('defaults', {}).get('credentials', [])
    if not isinstance(credentials, list):
        raise AWXError(f'Template {template_id}: invalid default credentials response.')
    names = {}
    for credential in credentials:
        if not isinstance(credential, dict) or type(credential.get('id')) is not int or credential['id'] < 1:
            raise AWXError(f'Template {template_id}: invalid default credential.')
        name = credential.get('name')
        if isinstance(name, str) and name.strip():
            names.setdefault(name.strip(), set()).add(credential['id'])
    entries = []
    seen = set()
    for credential in credentials:
        name = credential.get('name')
        # Numeric or repeated names are ambiguous; use the actual object ID.
        value = (name.strip() if isinstance(name, str) and name.strip() and not name.strip().isdigit()
                 and len(names[name.strip()]) == 1 else str(credential['id']))
        if credential['id'] not in seen:
            entries.append((value, credential))
            seen.add(credential['id'])
    return entries


def template_credential_values(client, link, cache):
    return [reference for reference, _ in template_credential_entries(client, link, cache)]


def credential_kind(credential):
    return credential.get('kind') or (credential.get('summary_fields') or {}).get('credential_type', {}).get('kind')


def template_machine_entries(client, entries):
    """Identify Machine credentials by AWX kind, never by a fixed type ID/name."""
    result = []
    for reference, credential in entries:
        if not credential_kind(credential) and client is not None:
            credential = lookup_credential(client, str(credential['id']))
        if credential_kind(credential) == 'ssh':
            result.append((reference, credential))
    return result


def unique_credential_references(references, client=None, *, resolve_aliases=False):
    """Keep the first reference, trimming spaces and comparing known AWX IDs.

    Names are case sensitive. Optional lookups only disambiguate mixed name/ID
    lists during export; launch preparation can reuse its populated cache.
    """
    references = tuple(dict.fromkeys(reference.strip() for reference in references if reference.strip()))
    has_numeric_ids = any(re.fullmatch('[0-9]+', reference) for reference in references)
    cache = client.credential_cache if client is not None else {}
    seen = set()
    result = []
    for reference in references:
        credential = cache.get(reference)
        numeric = bool(re.fullmatch('[0-9]+', reference))
        if credential is None and client is not None and resolve_aliases and has_numeric_ids and not numeric:
            try:
                credential = lookup_credential(client, reference)
            except AWXError as exc:
                # Keep unresolved references for the existing launch validation.
                LOGGER.debug('Nie można porównać poświadczenia %r przy usuwaniu powtórzeń: %s', reference, exc)
        if credential is not None:
            identity = ('id', credential['id'])
        elif numeric:
            identity = ('id', int(reference))
        else:
            identity = ('name', reference)
        if identity not in seen:
            seen.add(identity)
            result.append(reference)
    return tuple(result)


def merge_full_credentials(client, extras, host_values, inherited):
    """Host credentials replace inherited non-Vault credentials of the same type."""
    extras = unique_credential_references(extras)
    host_values = unique_credential_references(host_values)
    inherited_ids = {credential['id'] for _, credential in inherited}
    inherited_refs = {reference for reference, _ in inherited}
    host_ids = set()
    replaced_types = set()
    if client is not None and inherited:
        for reference in host_values:
            reference = reference.strip()
            if reference in inherited_refs:
                continue
            if reference.isascii() and reference.isdigit() and int(reference) in inherited_ids:
                host_ids.add(int(reference))
                continue
            try:
                credential = lookup_credential(client, reference)
            except AWXError as exc:
                LOGGER.debug('Nie można porównać poświadczenia hosta %r z domyślnymi poświadczeniami szablonu: %s', reference, exc)
                continue
            host_ids.add(credential['id'])
            kind = credential_kind(credential)
            if kind and kind != 'vault' and type(credential.get('credential_type')) is int:
                replaced_types.add(credential['credential_type'])
        extras = [reference for reference, credential in inherited
                  if credential['id'] not in host_ids and credential.get('credential_type') not in replaced_types]
    return list(unique_credential_references([*extras, *host_values], client, resolve_aliases=True))


def host_hop_details(host):
    """Return group descriptions and a count only when groups agree on it."""
    matches = {}
    for group in host.get('all_groups', host.get('groups', [])):
        name = group['name']
        match = re.search(r'([0-9]+)_?hop', name, re.IGNORECASE)
        if match:
            matches[name] = int(match.group(1))
    description = '\n'.join(f'{name} (HOPS: {count})' for name, count in sorted(matches.items())) or 'none'
    counts = set(matches.values())
    return description, next(iter(counts)) if len(counts) == 1 else None


def host_hops(host):
    """Show matching direct/ancestor group names and hop counts, once per group."""
    return host_hop_details(host)[0]


def build_inventory_rows(report, settings=None, output_column=None, *, client=None, template_cache=None, job_url_override=None,
                         auto_find_template=False):
    """Build shared output rows and add extra and full credentials to JSON host records."""
    settings = common.read_execution_settings() if settings is None else settings
    output_column = common.read_output_settings()['OUTPUT_COLUMN'] if output_column is None else output_column
    primary_variables = credential_variable_names()
    configured_extras = extra_credential_values()
    template_cache = {} if template_cache is None else template_cache
    if not isinstance(FULL_CREDENTIAL_SEPARATOR, str):
        raise AWXError('FULL_CREDENTIAL_SEPARATOR must be a string.')
    headers = excel.headers(settings['EXECUTION_COLUMN'], output_column)
    rows = []
    for host in report['hosts']:
        ostype = common.normalize_ostype(host.get('ostype'))
        host['ostype'] = ostype
        ostype_text = 'none' if ostype is None else ostype if isinstance(ostype, str) else json.dumps(ostype, ensure_ascii=False)
        job = OS_JOBS.get(ostype.strip().casefold(), {}) if not auto_find_template and isinstance(ostype, str) else {}
        host['INVENTORY'] = host.get('INVENTORY') or report.get('inventory', {}).get('name') or 'none'
        host['ApplicationID'] = host['INVENTORY'].split('_', 1)[0].strip() or 'none'
        job_link = '' if auto_find_template else common.job_template_url(settings, job.get('url', ''), override=job_url_override)
        selected_template = None
        try:
            if auto_find_template:
                if client is None:
                    raise AWXError('Automatic template discovery requires an AWX connection.')
                selected_template = templates.template_for_host(client, settings, host)
                job_link = client._resolve_url(f"job_templates/{selected_template['id']}/")
            elif client is not None and job_link:
                selected_template = templates.template_details(client, template_id_from_link(job_link, client))
            host['template_job_name'] = selected_template['name'] if selected_template else 'none'
            template_credentials = template_credential_entries(client, job_link, template_cache)
            template_machines = template_machine_entries(client, template_credentials)
        except AWXError as exc:
            if auto_find_template:
                source = 'ApplicationID / JOB_TEMPLATE_NAME_PATTERN'
            elif common.job_template_url(settings, override=job_url_override):
                source = 'JOB_URL / --job-url'
            elif isinstance(ostype, str):
                source = f"OS_JOBS[{ostype.strip().casefold()!r}]['url']"
            else:
                source = 'OS_JOBS'
            message = f'Host {host["name"]!r}, ostype={ostype!r}, source: {source}: {exc}'
            raise AWXError(client.redact(message) if client is not None else message) from None
        inherited = template_credentials if not configured_extras else []
        host['Job_Credentials'] = [reference for reference, _ in template_credentials]
        extra_credentials = configured_extras or [reference for reference, _ in inherited]
        extra_text = FULL_CREDENTIAL_SEPARATOR.join(extra_credentials) or 'none'
        values_by_variable = {}
        full_values_by_variable = {}
        for group in host['credential_groups']:
            if group['variable'] not in primary_variables:
                continue
            value = group['value']
            credential = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            values = values_by_variable.setdefault(group['variable'], [])
            if credential not in values:
                values.append(credential)
            if value is not None and credential.strip():
                full_values = full_values_by_variable.setdefault(group['variable'], [])
                if credential not in full_values:
                    full_values.append(credential)
        host_values = [value for variable in primary_variables for value in full_values_by_variable.get(variable, [])]
        # Filter template defaults before merging so explicit host/config choices survive.
        machine_ids = {credential['id'] for _, credential in template_machines}
        full_inherited = [(reference, credential) for reference, credential in inherited
                          if credential['id'] not in machine_ids]
        full_extras = configured_extras or [reference for reference, _ in full_inherited]
        full_parts = merge_full_credentials(client, full_extras, host_values, full_inherited)
        host['Extra_Credentials'] = list(extra_credentials)
        LOGGER.debug('Host %s: dodatkowe poświadczenia: %d\n  Źródło: %s', host['name'], len(extra_credentials),
                     'EXTRA_CREDENTIAL_VARIABLES' if configured_extras else 'szablon zadania')
        job_variables = templates.template_variables(selected_template) if selected_template else {}
        job_variables.update(deepcopy(job.get('variables', {})))
        if auto_find_template:
            job_variables = templates.apply_job_variable_overrides(
                job_variables, ostype, settings.get('JOB_VARIABLE_OVERRIDES_BY_OS') or {})
        host['Job_Link'] = job_link
        host['Job_Variables'] = job_variables
        credentials = list(dict.fromkeys((value for variable in primary_variables for value in values_by_variable.get(variable, []))))
        job_type = common.apply_job_type(job_variables, None, {**settings, 'JOB_TYPE': ''} if auto_find_template else settings)
        host['Job_Type'] = job_type
        host['AWX_Status'] = 'found'
        host['HOPS'], host['HOPS_COUNT'] = host_hop_details(host)
        options = jumphosts.credential_options(client, host['ApplicationID'], host['HOPS_COUNT'], FULL_CREDENTIAL_SEPARATOR)
        host['jumphost_for_hops_options'] = options
        host['jumphost_for_hops'] = options[0] if options else 'none'
        selected_jumps = options[:1]
        full_credential = FULL_CREDENTIAL_SEPARATOR.join(unique_credential_references(
            [*full_parts, *selected_jumps], client, resolve_aliases=True)) or 'none'
        host['Full_Credential'] = full_credential
        debug_step('Eksport hosta', 'przygotowano metadane', Host=host['name'], System=ostype_text,
                   Szablon=job_link or 'brak', Akcja=job_type or 'zachowana w zmiennych',
                   Nazwy_zmiennych=', '.join(map(str, job_variables)) or 'brak', HOPS=host['HOPS'],
                   Wybrany_jumphost=host['jumphost_for_hops'], Full_Credential=full_credential)
        values = {'hostname': host['name'], 'INVENTORY': host['INVENTORY'], 'ApplicationID': host['ApplicationID'], 'ostype': ostype_text, 'template_job_name': host['template_job_name'], 'Job_Link': job_link or 'none', 'Job_Credentials': FULL_CREDENTIAL_SEPARATOR.join(host['Job_Credentials']) or 'none', 'Job_Variables': json.dumps(job_variables, ensure_ascii=False), 'HOPS': host['HOPS'], 'HOPS_COUNT': host['HOPS_COUNT'], 'jumphost_for_hops': host['jumphost_for_hops'], 'Extra_Credentials': extra_text, 'OS Credential': FULL_CREDENTIAL_SEPARATOR.join(credentials) or 'none', 'Full_Credential': full_credential, 'Job_Type': job_type, 'AWX_Status': 'found'}
        rows.append([values.get(name, '') for name in headers])
    return (headers, rows)

def save_inventory_excel(headers, rows, output_file, settings=None, output_column=None, client=None, template_cache=None,
                         *, export_settings=None, export_info=None):
    """Create the same complete, styled workbook used by every AWX script."""
    settings = common.read_execution_settings() if settings is None else settings
    output_column = common.read_output_settings()['OUTPUT_COLUMN'] if output_column is None else output_column
    template_cache = {} if template_cache is None else template_cache
    workbook = excel.create_workbook(headers, rows, sheet_name=settings['SHEET_NAME'], execution_column=settings['EXECUTION_COLUMN'], output_column=output_column)
    try:
        columns = excel.read_columns(workbook.active)
        for row in range(2, workbook.active.max_row + 1):
            jump_cell = workbook.active.cell(row, columns['jumphost_for_hops'])
            selected = parse_credentials(jump_cell.value, row, 'jumphost_for_hops')
            synchronise_full_credential(workbook.active, row, previous_jumps=selected)
        if client is not None:
            update_survey_columns(client, workbook.active, settings, cache=template_cache)
            jumphosts.populate_choices(workbook.active, client, FULL_CREDENTIAL_SEPARATOR)
        snapshot = {**(export_settings if export_settings is not None else settings),
                    'SHEET_NAME': workbook.active.title, 'OUTPUT_COLUMN': output_column,
                    'EXCEL_OUTPUT_FILE': Path(output_file).resolve()}
        password_variables = {question['variable'] for _, questions, _ in (template_cache or {}).values()
                              for question in questions if question['type'] == 'password'}
        excel.add_settings_sheet(workbook, {
            'Eksport': {'Skrypt': 'awx_inventory_hosts.py',
                        'Data eksportu (UTC)': datetime.now(timezone.utc).replace(tzinfo=None),
                        'Liczba hostów': len(rows), **(export_info or {})},
            'Parametry': snapshot,
        }, sensitive_keys=password_variables, secrets=(snapshot.get('AWX_TOKEN'),))
        excel.save_workbook(workbook, output_file, execution_column=settings['EXECUTION_COLUMN'], output_column=output_column, excel_columns=settings.get('EXCEL_COLUMNS'))
    finally:
        workbook.close()

def inventory_main(argv=None):
    started = time.perf_counter()
    args = parse_inventory_arguments(argv)
    configure_debug(args.debug, AWX_TOKEN if isinstance(AWX_TOKEN, str) else '')
    client = None
    try:
        settings = load_connection(args.debug)
        token = AWX_TOKEN.strip()
        client = AWXClient(AWX_URL, token)
        check_connection(client, debug=args.debug)
        debug_step('Eksport', 'ustawienia połączenia i plików', Serwer=AWX_URL, Inwentarz=INVENTORY,
                   Limit_HTTP_s=TIMEOUT, Rekordy_na_stronę=PAGE_SIZE,
                   Plik_JSON=OUTPUT_FILE, Plik_Excel=EXCEL_OUTPUT_FILE)
        console.print_message(f'Eksport hostów z AWX\n  Inwentarz: {INVENTORY}', flush=True)
        hostnames = None
        input_file = args.input_file or common.read_execution_settings()["INPUT_FILE"]
        if input_file is not None:
            input_file = Path(input_file)
            if input_file.resolve() == EXCEL_OUTPUT_FILE.resolve():
                raise AWXError('The input and output Excel files must use different paths. Change EXCEL_OUTPUT_FILE in the configuration.')
            console.print_message(f'Odczytuję listę hostów z pliku: {input_file.resolve()}',
                                  level='progress', flush=True)
            hostnames = read_host_limits(input_file)
        debug_step('Eksport', 'wybór hostów', Filtr=input_file or 'wszystkie hosty inwentarza',
                   Liczba_nazw=len(hostnames) if hostnames is not None else 'bez ograniczenia')
        scope = f'hosty z listy ({len(hostnames)})' if hostnames is not None else 'wszystkie hosty inwentarza'
        console.print_message(f'[1/4] Pobieram hosty, grupy i zmienne z AWX.\n  Zakres: {scope}',
                              level='progress', flush=True)
        report = fetch_report(client, INVENTORY, hostnames=hostnames)
        missing = report.get('missing_hosts', [])
        if missing:
            details = ', '.join(missing[:20])
            if len(missing) > 20:
                details += f" ... (+{len(missing) - 20})"
            console.print_message(f"Nie znaleziono hostów w AWX ({len(missing)}): {details}",
                                  file=sys.stderr, level="warning", flush=True)
        workflow = common.read_execution_settings()
        output_column = common.read_output_settings()['OUTPUT_COLUMN']
        template_cache = {}
        console.print_message(
            f'[2/4] Pobrano hostów: {report["host_count"]}. Przygotowuję szablony, zmienne i poświadczenia.',
            level='progress', flush=True)
        headers, rows = build_inventory_rows(report, workflow, output_column, client=client, template_cache=template_cache)
        debug_step('Eksport', 'przygotowano dane wynikowe', Wiersze=len(rows),
                   Kolumny=', '.join(headers), Widoczne_kolumny=workflow.get('EXCEL_COLUMNS') or 'wszystkie')
        console.print_message(f'\nWyniki hostów ({len(rows)}):', flush=True)
        if not rows:
            console.print_message('Brak hostów do eksportu.\n', level='warning', flush=True)
        for index, row in enumerate(rows, start=1):
            values = dict(zip(headers, row))
            title = f'Host [{index}/{len(rows)}]: {values["hostname"]}'
            if args.debug:
                fields = {name: values[name] for name in common.METADATA_COLUMNS if name != 'hostname'}
                console.print_record(title, fields)
            else:
                console.print_message(title, level='data', flush=True)
        console.print_message('[3/4] Zapisuję plik JSON z danymi hostów.', level='progress', flush=True)
        LOGGER.debug('Zapisuję eksport JSON: %s\n  Liczba hostów: %d', OUTPUT_FILE, report['host_count'])
        OUTPUT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        console.print_message('[4/4] Zapisuję Excel z arkuszem Ustawienia, ankietami i listami poświadczeń.',
                              level='progress', flush=True)
        export_keys = ('SHEET_NAME', 'EXCEL_COLUMNS', 'EXECUTION_COLUMN', 'JOB_URL', 'JOB_TYPE',
                       'JOB_TYPE_VARIABLE', 'SURVEY_ANSWERS', 'SURVEY_EXCEL_DEFAULTS')
        export_settings = {**settings, **{name: workflow[name] for name in export_keys},
                           'INPUT_FILE': input_file.resolve() if input_file is not None else None,
                           'OUTPUT_FILE': OUTPUT_FILE.resolve(), 'DEBUG': args.debug, 'COLOR': args.color}
        export_info = {'Plik konfiguracji': common.current_variables_file().resolve(),
                       'Kolejność nadpisań': 'Argumenty komendy > VARIABLES > INVENTORY_OVERRIDES > wspólne wartości pliku',
                       'Źródło hostów': scope, 'Filtr hostów': hostnames,
                       'Inwentarz w AWX': report['inventory'], 'Nie znaleziono hostów': missing}
        save_inventory_excel(headers, rows, EXCEL_OUTPUT_FILE, workflow, output_column, client=client,
                             template_cache=template_cache, export_settings=export_settings, export_info=export_info)
        debug_step('Eksport', 'zapisano wyniki', JSON=OUTPUT_FILE.resolve(), Excel=EXCEL_OUTPUT_FILE.resolve(), Wiersze=len(rows))
        console.print_message(
            f'\nEksport zakończony. Zapisano hostów: {len(rows)}. Nie znaleziono: {len(missing)}.\n'
            f'  JSON:  {OUTPUT_FILE.resolve()}\n'
            f'  Excel: {EXCEL_OUTPUT_FILE.resolve()}\n'
            f'  Czas:  {time.perf_counter() - started:.1f} s',
            level='warning' if missing else 'success', flush=True)
        return 0
    finally:
        debug_step('Eksport', 'koniec obsługi operacji', Żądania_API=client.request_count if client is not None else 0,
                   Czas_s=f'{time.perf_counter() - started:.3f}')


# RUNNER WORKFLOW

EXECUTION_SETTINGS_FILE = common.EXECUTION_SETTINGS_FILE
EXECUTION_DEFAULTS = common.EXECUTION_DEFAULTS
HOSTNAME_PATTERN = common.HOSTNAME_PATTERN
TEMPLATE_PATTERN = re.compile('(?:^|/)(?:job_templates|job_template|job-template)/([1-9][0-9]*)(?:/|$)')

def read_execution_settings(settings_file=None):
    return common.read_execution_settings(settings_file)

def parse_execute_arguments(argv=None):
    parser = operation_parser("Uruchom joby AWX paczkami i zapisz linki do wykonań w Excelu.", operation="execute")
    parser.add_argument("--hosts", "--host", nargs="+", help="Wybrane hosty, np. host1 host2 lub host1,host2; nadpisuje HOSTS.")
    parser.add_argument("--job-url", help="URL szablonu joba dla wskazanych hostów; nadpisuje JOB_URL i Job_Link.")
    parser.add_argument("--batch-size", type=int, help="Maksymalna liczba hostów w jobie; nadpisuje BATCH_SIZE.")
    parser.add_argument("--job-type", help="Akcja playbooka, np. install/reinstall/update; nadpisuje także kolumnę Job_Type.")
    parser.add_argument('--execution-scope', choices=('ask', 'missing', 'all'),
                        help='Zakres: ask pyta (domyślnie), missing uruchamia tylko wpisy bez linku, all także wpisy z historią. Końcowe potwierdzenie planu jest wymagane.')
    parser.add_argument("--survey", action=argparse.BooleanOptionalAction, default=None,
                        help="Pytaj w terminalu o brakujące odpowiedzi survey; --no-survey wyłącza pytania.")
    parser.add_argument("--survey-answers", help="Odpowiedzi survey jako obiekt JSON; nadpisuje SURVEY_ANSWERS i Job_Variables.")
    parser.add_argument("--show-survey", action="store_true", help="Pokaż pytania/defaulty survey; bez startu jobów i zapisu Excela.")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=None,
                        help="Pokaż plan bez uruchamiania jobów i zapisu Excela; --no-dry-run wyłącza DRY_RUN z konfiguracji.")
    return parse_operation_arguments(parser, argv)

def resource_id_from_link(link, client, pattern, label):
    """Validate resource links consistently before using their IDs with the API."""
    parsed = urlsplit(link)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password:
        raise AWXError(f"{label} must be an HTTP(S) link.")
    if client._origin(parsed) != client.origin:
        raise AWXError(client.server_mismatch_message(link, label))
    match = pattern.search(parsed.fragment or parsed.path)
    if not match:
        raise AWXError(f"Cannot read a resource ID from {label}: {link}")
    return int(match.group(1))


def template_id_from_link(link, client):
    return resource_id_from_link(link, client, TEMPLATE_PATTERN, "Job_Link")


def template_id_from_row(link, client, sheet, row_number, *, override=False):
    """Attach worksheet, host and the source of the effective template link."""
    try:
        return template_id_from_link(link, client)
    except AWXError as exc:
        columns = excel.read_columns(sheet)
        hostname = sheet.cell(row_number, columns['hostname']).value
        cell = sheet.cell(row_number, columns['job_link']) if 'job_link' in columns else None
        if override:
            source = 'JOB_URL / --job-url override'
        elif cell is not None and cell.hyperlink and cell.hyperlink.target:
            source = f'Job_Link hyperlink target ({cell.coordinate})'
        else:
            source = f'Job_Link cell value ({cell.coordinate})' if cell is not None else 'Job_Link'
        message = (f'Worksheet {sheet.title!r}, row {row_number}, host {hostname!r}: {exc}\n'
                   f'  Link source: {source}.')
        if cell is not None:
            message += f'\n  Job_Link cell text ({cell.coordinate}): {cell.value!r}'
        raise AWXError(client.redact(message)) from None

def execution_template_from_row(sheet, row_number, columns, client, override=''):
    """Verify the Excel URL, hyperlink and template name before choosing a job."""
    cell = sheet.cell(row_number, columns['job_link'])
    displayed = str(cell.value or '').strip()
    link = override or displayed
    if link.casefold() in ('', 'none'):
        return None
    if not override:
        if cell.data_type == 'f':
            raise AWXError(f'Row {row_number}: Job_Link must contain a literal template URL.')
        if cell.hyperlink and cell.hyperlink.target:
            link = cell.hyperlink.target
            target_id = template_id_from_row(link, client, sheet, row_number)
            if displayed.casefold().startswith(('http://', 'https://')):
                displayed_id = template_id_from_row(displayed, client, sheet, row_number)
                if displayed_id != target_id:
                    raise AWXError(f'Row {row_number}: Job_Link text points to template {displayed_id}, '
                                   f'but its hyperlink points to template {target_id}. Refresh or correct both links.')
    template_id = template_id_from_row(link, client, sheet, row_number, override=bool(override))
    name_cell = sheet.cell(row_number, columns['template_job_name']) if 'template_job_name' in columns else None
    if not override and name_cell is not None and name_cell.data_type == 'f':
        raise AWXError(f'Row {row_number}: template_job_name must contain a literal name.')
    details = templates.template_details(client, template_id)
    name = details['name']
    expected = str(name_cell.value or '').strip() if name_cell is not None else ''
    if not override and expected.casefold() not in ('', 'none') and expected != name.strip():
        raise AWXError(f'Row {row_number}: template_job_name {expected!r} does not match '
                       f'template {template_id} from Job_Link ({name!r}). Refresh Excel before launching.')
    return template_id, name, link


def parse_variables(value, row_number):
    if value is None or (isinstance(value, str) and value.strip().casefold() in ('', 'none')):
        return {}
    def unique_keys(pairs):
        result = {}
        for name, item in pairs:
            if name in result:
                raise ValueError('Duplicate JSON key')
            result[name] = item
        return result

    try:
        variables = json.loads(value, object_pairs_hook=unique_keys)
        if not isinstance(variables, dict):
            raise ValueError('not a dictionary')
        json.dumps(variables, allow_nan=False)
    except (TypeError, ValueError):
        raise AWXError(f'Row {row_number}: Job_Variables must be a JSON object with unique keys, '
                       'e.g. {"action": "check"}.') from None
    return variables

def parse_credentials(value, row_number, column_name='Full_Credential', *, keep_order=False):
    """Read credential references using the separator used by the inventory export."""
    if value is None or str(value).strip().casefold() in ('', 'none'):
        return ()
    separator = FULL_CREDENTIAL_SEPARATOR
    if not separator:
        raise AWXError(f'FULL_CREDENTIAL_SEPARATOR cannot be empty when reading {column_name}.')
    references = [part.strip() for part in str(value).split(separator.strip() or separator)]
    if any((not reference or reference.casefold() == 'none' for reference in references)):
        raise AWXError(f"Row {row_number}: {column_name} contains an empty or invalid credential reference.")
    unique = unique_credential_references(references)
    return unique if keep_order else tuple(sorted(unique))


def synchronise_full_credential(sheet, row_number, *, previous_jumps=None, client=None):
    """Merge only the cell selection, replacing the previously merged jump refs.

    The returned base refs retain the blank-Full_Credential/defaults behaviour.
    Only cached identities are used when a client is provided; no API calls.
    """
    columns = excel.read_columns(sheet)
    full_cell = sheet.cell(row_number, columns['full_credential'])
    if full_cell.data_type == 'f':
        raise AWXError(f'Row {row_number}: Full_Credential must contain literal credential names/IDs.')
    full_refs = parse_credentials(full_cell.value, row_number, keep_order=True)
    base_refs, selected = full_refs, ()
    if 'jumphost_for_hops' in columns:
        jump_cell = sheet.cell(row_number, columns['jumphost_for_hops'])
        if jump_cell.data_type == 'f':
            raise AWXError(f'Row {row_number}: jumphost_for_hops must contain literal credential names/IDs.')
        selected = parse_credentials(jump_cell.value, row_number, 'jumphost_for_hops', keep_order=True)
        previous = set(jumphosts.previous_full_selection(full_cell))
        if previous_jumps is not None:
            previous.update(previous_jumps)
        base_refs = tuple(reference for reference in full_refs if reference not in previous)
    combined = unique_credential_references([*base_refs, *selected], client)
    full_cell.value = FULL_CREDENTIAL_SEPARATOR.join(combined) or 'none'
    full_cell.data_type = 's'
    full_cell.number_format = '@'
    if 'jumphost_for_hops' in columns:
        jumphosts.remember_full_selection(full_cell, selected)
    return base_refs, selected

def batch_host_limit(hostnames):
    """Put localhost first exactly once without adding a synthetic Excel entry."""
    names = common.unique_hostnames(hostnames)
    return ','.join(['localhost', *(name for name in names
                                   if common.hostname_key(name) not in ('', 'localhost'))])


def execution_row_context(sheet, row_number, columns, excel_variables):
    """Keep different Excel launch metadata separate, including overridden inputs."""
    handled = {'hostname', 'AWX_Status', 'ostype', 'template_job_name', 'Job_Link',
               'Job_Variables', 'Full_Credential'}
    credential_columns = {'Job_Credentials', 'Extra_Credentials', 'OS Credential', 'jumphost_for_hops'}
    context = {'Job_Variables': excel_variables}
    for name in common.METADATA_COLUMNS:
        if name in handled:
            continue
        cell = sheet.cell(row_number, columns[name.casefold()]) if name.casefold() in columns else None
        if cell is not None and cell.data_type == 'f':
            raise AWXError(f'Row {row_number}: {name} must contain a literal value.')
        value = cell.value if cell is not None else None
        if name in credential_columns:
            value = parse_credentials(value, row_number, name)
        else:
            text = str(value).strip() if value is not None else ''
            if text.casefold() in ('', 'none'):
                value = None
            elif name == 'HOPS_COUNT':
                if isinstance(value, bool) or not re.fullmatch(r'[0-9]+(?:\.0+)?', text):
                    raise AWXError(f'Row {row_number}: HOPS_COUNT must be a nonnegative integer or empty.')
                value = int(text.split('.')[0])
            else:
                value = text
        context[name] = value
    return context


def build_execution_plan(sheet, client, settings, job_type_override=None, hostnames=None, job_url_override=None, survey_overrides=None, survey_cache=None):
    """Verify Excel templates and batch equal OS/payloads, preserving distinct runs per host."""
    excel.migrate_execution_column(sheet, common.normalize_execution_column(settings['EXECUTION_COLUMN']))
    columns = excel.read_columns(sheet)
    for name in ('hostname', 'job_link', 'job_variables', 'full_credential'):
        if name not in columns:
            raise AWXError(f"Missing {name} column. Generate a new Excel export with awx_inventory_hosts.py.")
    execution_column = columns.get(common.normalize_execution_column(settings['EXECUTION_COLUMN']).casefold(), sheet.max_column + 1)
    hosts = {}
    selected = {common.hostname_key(name) for name in hostnames} if hostnames is not None else None
    skipped_without_job = 0
    selected_job_url = str(job_url_override or '').strip()
    for row_number in range(2, sheet.max_row + 1):
        host_value = sheet.cell(row_number, columns['hostname']).value
        if host_value is None or not str(host_value).strip():
            LOGGER.debug('Plan wykonania: pomijam wiersz %d — pusta nazwa hosta.', row_number)
            continue
        hostname = str(host_value).strip()
        host_key = common.hostname_key(hostname)
        if selected is not None and host_key not in selected:
            LOGGER.debug('Plan wykonania: pomijam wiersz %d — host %s nie należy do wybranej listy.', row_number, hostname)
            continue
        if not HOSTNAME_PATTERN.fullmatch(hostname):
            raise AWXError(f"Row {row_number}: hostname must be a single literal hostname, without Ansible patterns.")
        os_cell = sheet.cell(row_number, columns['ostype']) if 'ostype' in columns else None
        if os_cell is not None and os_cell.data_type == 'f':
            raise AWXError(f'Row {row_number}: ostype must contain a literal operating system name.')
        ostype = common.normalize_ostype(os_cell.value) if os_cell is not None else None
        selected_template = execution_template_from_row(sheet, row_number, columns, client, selected_job_url)
        if selected_template is None:
            LOGGER.debug('Plan wykonania: pomijam wiersz %d — host %s nie ma szablonu zadania.', row_number, hostname)
            skipped_without_job += 1
            continue
        template_id, template_name, link = selected_template
        if not isinstance(ostype, str) or ostype in ('', 'none'):
            raise AWXError(f'Row {row_number}, host {hostname!r}: ostype is required to verify the job against AWX. Refresh Excel.')
        status = sheet.cell(row_number, columns['awx_status']).value if 'awx_status' in columns else None
        if status is not None and str(status).strip().casefold() not in ('', 'found'):
            raise AWXError(f'Row {row_number}, host {hostname!r}: AWX_Status must be found before launching. Refresh Excel.')
        excel_variables = parse_variables(sheet.cell(row_number, columns['job_variables']).value, row_number)
        variables = deepcopy(settings.get('SURVEY_ANSWERS', {}))
        variables.update(excel_variables)
        job_type_cell = sheet.cell(row_number, columns['job_type']) if 'job_type' in columns else None
        credential_cell = sheet.cell(row_number, columns['full_credential'])
        if credential_cell.data_type == 'f' or (job_type_cell is not None and job_type_cell.data_type == 'f'):
            raise AWXError(f"Row {row_number}: Full_Credential and Job_Type must contain literal values.")
        questions = survey_cache[template_id][1] if survey_cache is not None and template_id in survey_cache else None
        overridden = set(survey_overrides or {})
        if job_type_override is not None:
            overridden.add(settings['JOB_TYPE_VARIABLE'])
        answers = survey_excel.read_answers(sheet, row_number, questions, skip_variables=overridden)
        variables.update(answers)
        variables.update(deepcopy(survey_overrides or {}))
        # Explicit CLI/survey answers win; otherwise preserve the action in Excel's variables.
        action_override = job_type_override
        action_variable = settings['JOB_TYPE_VARIABLE']
        if action_override is None and (action_variable in answers or action_variable in (survey_overrides or {})
                                        or action_variable in excel_variables):
            action_override = variables[action_variable]
            if not isinstance(action_override, str):
                raise AWXError(f"Job_Variables[{action_variable!r}] must be a string for the job type.")
        job_type = apply_job_type(variables, job_type_cell.value if job_type_cell else None, settings, action_override)
        base_refs, jump_refs = synchronise_full_credential(sheet, row_number)
        credential_refs = tuple(sorted(set(base_refs + jump_refs)))
        preserve_defaults = bool(jump_refs and not base_refs)
        context = execution_row_context(sheet, row_number, columns, excel_variables)
        key = (template_id, template_name, ostype, json.dumps(variables, sort_keys=True, ensure_ascii=False),
               credential_refs, preserve_defaults, json.dumps(context, sort_keys=True, ensure_ascii=False))
        execution_key = (host_key, key)
        execution = str(sheet.cell(row_number, execution_column).value or '').strip()
        if execution.casefold() == 'none':
            execution = ''
        if execution_key not in hosts:
            hosts[execution_key] = {'key': key, 'template_id': template_id, 'template_job_name': template_name,
                                    'ostype': ostype, 'link': link, 'variables': variables,
                                    'credential_refs': credential_refs, 'preserve_template_credentials': preserve_defaults,
                                    'job_type': job_type, 'hostname': hostname, 'rows': [], 'execution': ''}
        host = hosts[execution_key]
        if execution:
            if host['execution'] and host['execution'] != execution:
                raise AWXError(f"Host {hostname!r} has conflicting execution links/markers; check its rows.")
            host['execution'] = execution
        host['rows'].append(row_number)
        debug_step('Plan wykonania', 'przypisano wiersz do hosta', Wiersz=row_number, Host=hostname,
                   Szablon_ID=template_id, Źródło_szablonu='--job-url' if selected_job_url else 'Job_Link',
                   Nazwy_zmiennych=', '.join(map(str, variables)) or 'brak',
                   Historia='istnieje wpis wykonania' if execution else 'nowe wykonanie')
    groups = {}
    existing = []
    for host in hosts.values():
        if host['execution']:
            existing.append(host)
        else:
            groups.setdefault(host['key'], []).append(host)
    batches = []
    for group in groups.values():
        for start in range(0, len(group), settings['BATCH_SIZE']):
            entries = group[start:start + settings['BATCH_SIZE']]
            batches.append({'template_id': entries[0]['template_id'], 'template_job_name': entries[0]['template_job_name'],
                            'ostype': entries[0]['ostype'], 'link': entries[0]['link'], 'variables': entries[0]['variables'],
                            'entries': entries, 'credential_refs': entries[0]['credential_refs'],
                            'preserve_template_credentials': entries[0]['preserve_template_credentials'],
                            'job_type': entries[0]['job_type'], 'limit': batch_host_limit(host['hostname'] for host in entries),
                            'job_url_overridden': bool(selected_job_url)})
    debug_step('Plan wykonania', 'utworzono paczki według szablonu, zmiennych i poświadczeń',
               Unikalne_hosty=len({common.hostname_key(host['hostname']) for host in hosts.values()}),
               Wpisy_wykonania=len(hosts), Paczki=len(batches), Uruchomienia_z_historią=len(existing),
               Hosty_bez_szablonu=skipped_without_job, Limit_hostów_na_paczkę=settings['BATCH_SIZE'])
    return (batches, existing, execution_column, skipped_without_job)

def write_execution(sheet, column, entries, value):
    for entry in entries:
        for row_number in entry['rows']:
            cell = sheet.cell(row_number, column)
            cell.value = value
            cell.data_type = 's'
            cell.number_format = '@'
            cell.hyperlink = value if value.startswith(('http://', 'https://')) else None



def lookup_credential(client, reference):
    """One cached name/ID lookup used by metadata merging and launching."""
    cache = client.credential_cache
    LOGGER.debug('Poświadczenie %r: %s', reference,
                 'korzystam z pamięci podręcznej' if reference in cache else 'wyszukuję nazwę lub ID w AWX')
    if reference not in cache:
        if re.fullmatch('[0-9]+', reference):
            credential = client.get(f"credentials/{int(reference)}/", purpose=f"Odczyt poświadczenia o ID {reference}")
        else:
            matches = list(client.items('credentials/', name=reference, order_by='id', purpose=f"Wyszukanie poświadczenia o nazwie {reference!r}"))
            if not matches:
                raise AWXError(f"Credential {reference!r} was not found or is not accessible.")
            if len(matches) != 1:
                ids = ', '.join(str(match['id']) for match in matches)
                raise AWXError(f"Credential name {reference!r} is ambiguous (IDs: {ids}); use its numeric ID in Full_Credential.")
            credential = matches[0]
        if type(credential.get('id')) is not int or credential['id'] < 1:
            raise AWXError(f"Invalid AWX credential response for {reference!r}.")
        cache[reference] = credential
        cache[str(credential['id'])] = credential
    return cache[reference]


def credential_identity(credential):
    kind = credential_kind(credential)
    return (credential.get('credential_type'),
            (credential.get('inputs') or {}).get('vault_id') if kind == 'vault' else None)


def resolve_credentials(client, batches, *, template_cache=None):
    """Resolve job credential references into IDs required by the launch API.

    AWX_TOKEN authenticates these requests. Full_Credential selects the stored
    credentials used by the job/playbook; it never authenticates the API client.
    """
    template_cache = {} if template_cache is None else template_cache
    for batch in batches:
        selected = {}
        inherited = {}
        for reference in batch['credential_refs']:
            try:
                credential = lookup_credential(client, reference)
            except AWXError as exc:
                raise AWXError(
                    f"Cannot resolve credential {reference!r} from Full_Credential / jumphost_for_hops for hosts [{batch['limit']}]. "
                    f"{exc} Full_Credential selects job/template credentials; API authentication uses AWX_TOKEN. "
                    "The launch API requires IDs of job credentials stored in AWX. "
                    "Check Full_Credential and EXTRA_CREDENTIAL_VARIABLES in awx_variable or launcher VARIABLES; "
                    "use existing job credential names/IDs, then refresh Excel after changing the settings. "
                    "Playbook input values belong in OS_JOBS[os]['variables'] / Job_Variables."
                ) from None
            selected[credential['id']] = credential
        explicit_identities = {credential_identity(credential) for credential in selected.values()}
        if batch.get('link') and (selected or batch.get('preserve_template_credentials')):
            defaults = template_credential_entries(client, batch['link'], template_cache)
            if batch.get('preserve_template_credentials'):
                inherited = {credential['id']: lookup_credential(client, str(credential['id']))
                             for _, credential in defaults}
            else:
                # Full_Credential omits inherited-only Machine credentials. Preserve
                # them in the API payload unless an explicit Machine replaces them.
                explicit_types = {credential.get('credential_type') for credential in selected.values()}
                candidates = [(reference, credential) for reference, credential in defaults
                              if credential.get('credential_type') not in explicit_types]
                inherited = {credential['id']: lookup_credential(client, str(credential['id']))
                             for _, credential in template_machine_entries(client, candidates)}
        for credential in inherited.values():
            if credential_identity(credential) not in explicit_identities:
                selected[credential['id']] = credential
        identities = {}
        for credential in selected.values():
            inputs = credential.get('inputs') or {}
            if any((inputs.get(name) == 'ASK' for name in ('password', 'become_password', 'ssh_key_unlock', 'vault_password'))):
                raise AWXError(f"Credential {credential['id']} requires an interactive password; configure it in AWX.")
            credential_type = credential.get('credential_type')
            identity = credential_identity(credential)
            if credential_type is not None and identity in identities:
                raise AWXError(f"Full_Credential / jumphost_for_hops contains conflicting credentials {identities[identity]} and {credential['id']} of the same credential type. Select one credential per type (Vault IDs may differ).")
            identities[identity] = credential['id']
        batch['credentials'] = sorted(selected)
        batch['credential_types'] = {credential.get('credential_type') for credential in selected.values()}
        debug_step('Poświadczenia', 'zakończono dobór dla paczki', Szablon_ID=batch.get('template_id', 'nie podano'),
                   Hosty=batch.get('limit', 'nie podano'), Wybrane_ID=batch['credentials'],
                   ID_odziedziczone_z_szablonu=sorted(inherited))

def template_survey(client, template_id, cache):
    LOGGER.debug('Szablon %s: %s', template_id,
                 'korzystam z zapisanych ustawień i pytań ankiety' if template_id in cache else 'pobieram ustawienia uruchamiania i ankietę')
    if template_id not in cache:
        details = client.get(f"job_templates/{template_id}/launch/", purpose=f"Sprawdzenie ustawień uruchamiania szablonu {template_id}")
        questions = []
        if details.get('survey_enabled'):
            spec = client.get(f"job_templates/{template_id}/survey_spec/", purpose=f"Odczyt ankiety szablonu {template_id}")
            questions = survey.read_questions(spec, template_id)
        cache[template_id] = (details, questions, {})
        debug_step('Ankieta', 'odczytano definicję szablonu', Szablon_ID=template_id,
                   Ankieta_włączona=bool(details.get('survey_enabled')), Pytania=len(questions),
                   Nazwy_zmiennych=', '.join(question['variable'] for question in questions) or 'brak')
    return cache[template_id]


def update_survey_columns(client, sheet, settings, *, job_url_override=None, cache=None, row_numbers=None, overwrite_configured=False):
    cache = {} if cache is None else cache
    columns = excel.read_columns(sheet)
    template_rows = {}
    if 'hostname' not in columns:
        return cache
    override = common.job_template_url(settings, override=job_url_override)
    rows = range(2, sheet.max_row + 1) if row_numbers is None else row_numbers
    for row_number in rows:
        if not str(sheet.cell(row_number, columns['hostname']).value or '').strip():
            continue
        cell = sheet.cell(row_number, columns['job_link']) if 'job_link' in columns else None
        link = common.job_template_url(settings, str(cell.value or '') if cell is not None else '', override=job_url_override)
        if not override and cell is not None and cell.hyperlink and cell.hyperlink.target:
            link = cell.hyperlink.target
        if link.casefold() in ('', 'none'):
            continue
        template_id = template_id_from_row(link, client, sheet, row_number, override=bool(override))
        template_survey(client, template_id, cache)
        template_rows.setdefault(template_id, []).append(row_number)
    added = survey_excel.populate_columns(sheet, template_rows, cache, settings, overwrite_configured=overwrite_configured)
    debug_step('Ankiety w Excelu', 'uzupełniono kolumny odpowiedzi', Szablony=len(template_rows),
               Wiersze=sum(map(len, template_rows.values())), Nowe_kolumny=', '.join(added) or 'brak',
               Nadpisywanie_odpowiedzi_z_konfiguracji=overwrite_configured)
    if added:
        console.print_message('Dodano kolumny odpowiedzi survey w arkuszu: ' + ', '.join(added), level='info')
    return cache


def validate_templates(client, batches, *, interactive=False, check_credentials=True, cache=None):
    """Check prompting and required inputs before launching the first batch."""
    cache = {} if cache is None else cache
    for batch in batches:
        template_id = batch['template_id']
        details, questions, prompted = template_survey(client, template_id, cache)
        debug_step('Walidacja przed uruchomieniem', 'sprawdzam wymagania szablonu', Szablon_ID=template_id,
                   Hosty=batch['limit'], Limit=bool(details.get('ask_limit_on_launch')),
                   Poświadczenia=bool(details.get('ask_credential_on_launch')),
                   Zmienne=bool(details.get('ask_variables_on_launch')), Ankieta=bool(details.get('survey_enabled')),
                   Pytania_w_terminalu=interactive)
        if not details.get('ask_limit_on_launch'):
            raise AWXError(f"Template {template_id}: enable Prompt on launch for Limit to restrict each job to its batch.")
        if (details.get('defaults', {}).get('job_slice_count') or 1) > 1:
            raise AWXError(f"Template {template_id}: set Job Slicing to 1; this script creates its own host batches.")
        if details.get('inventory_needed_to_start'):
            raise AWXError(f"Template {template_id}: configure its required inventory in AWX.")
        if check_credentials and batch['credentials']:
            if not details.get('ask_credential_on_launch'):
                credential_ids = ', '.join(str(value) for value in batch['credentials'])
                raise AWXError(
                    f"Template {template_id}: enable Prompt on launch for Credentials to accept Full_Credential. "
                    f"Edit {batch['link']}, select Prompt on launch next to Credentials "
                    "(ask_credential_on_launch=True), save the template and rerun the script. "
                    f"Requested credential IDs: {credential_ids}; hosts: {batch['limit']}."
                )
            for default in details.get('defaults', {}).get('credentials', []):
                if default['id'] not in batch['credentials'] and default.get('credential_type') not in batch['credential_types']:
                    raise AWXError(f"Template {template_id}: Full_Credential must include default credential {default['id']} or a replacement of the same credential type.")
        elif check_credentials and (details.get('passwords_needed_to_start') or details.get('credential_needed_to_start')):
            raise AWXError(f"Template {template_id}: credentials require interactive passwords or are missing; fill Full_Credential.")
        variables = batch['variables']
        if interactive and not details.get('survey_enabled'):
            raise AWXError(f"Template {template_id}: survey is disabled. Enable Survey in AWX: {batch['link']}.")
        if details.get('survey_enabled'):
            password_keys = {question['variable'] for question in questions if question['type'] == 'password'}
            client.add_sensitive_values(variables.get(key) for key in password_keys)
            variables = survey.prepare_variables(questions, variables, template_id, interactive=interactive, prompted=prompted)
            client.add_sensitive_values(variables.get(key) for key in password_keys)
            batch['variables'] = variables
        if variables and (not details.get('ask_variables_on_launch')):
            if not details.get('survey_enabled'):
                raise AWXError(f"Template {template_id}: enable its Survey or Prompt on launch for Variables to accept Job_Variables / SURVEY_ANSWERS.")
            unsupported = set(variables) - {question['variable'] for question in questions}
            if unsupported:
                raise AWXError(f"Template {template_id}: variables outside its survey: {', '.join(sorted(unsupported))}.")
        missing = [name for name in details.get('variables_needed_to_start', []) if name not in variables]
        if missing:
            raise AWXError(f"Template {template_id}: missing required Job_Variables: {', '.join(missing)}.")
        LOGGER.debug('Szablon %s: walidacja paczki zakończona poprawnie\n  Nazwy zmiennych: %s\n  Wartości zmiennych pozostają ukryte.',
                     template_id, ', '.join(map(str, batch['variables'])) or 'brak')


def show_template_surveys(client, links):
    for template_id in links:
        details = client.get(f"job_templates/{template_id}/launch/", purpose=f"Odczyt stanu ankiety szablonu {template_id}")
        spec = client.get(f"job_templates/{template_id}/survey_spec/", purpose=f"Odczyt pytań ankiety szablonu {template_id}")
        survey.display_questions(template_id, details.get('survey_enabled', False), survey.read_questions(spec or {'spec': []}, template_id))

def resolve_execution_hostnames(client, batches, template_cache):
    """Verify Excel OS against the template inventory before resolving host limits."""
    inventories = {}
    for batch in batches:
        batch['limit'] = batch_host_limit(entry['hostname'] for entry in batch['entries'])
        details, _, _ = template_survey(client, batch['template_id'], template_cache)
        defaults = details.get('defaults', {})
        inventory = (defaults['inventory'] if 'inventory' in defaults
                     else templates.template_details(client, batch['template_id']).get('inventory'))
        inventory_id = inventory.get('id') if isinstance(inventory, dict) else inventory
        if type(inventory_id) is not int or inventory_id < 1:
            raise AWXError(f"Template {batch['template_id']}: configure its required inventory in AWX.")
        inventories.setdefault(inventory_id, []).append(batch)
    for inventory_id, inventory_batches in inventories.items():
        names = common.unique_hostnames(entry['hostname'] for batch in inventory_batches for entry in batch['entries']
                                        if common.hostname_key(entry['hostname']) != 'localhost')
        wanted = {common.hostname_key(name) for name in names}
        candidates = [host for host in client.items(f"inventories/{inventory_id}/hosts/",
                                                   purpose=f"Sprawdzenie dokładnej pisowni nazw hostów w inwentarzu szablonu {inventory_id}",
                                                   **host_name_filters(names))
                      if common.hostname_key(host['name']) in wanted] if names else []
        index = host_index(candidates)
        missing = [name for name in names if common.hostname_key(name) not in index]
        if missing:
            raise AWXError(f"Hosts not found in template inventory {inventory_id}: {', '.join(missing)}")
        hostvars = {}
        if names:
            exported = client.get(f'inventories/{inventory_id}/script/?all=1&hostvars=1',
                                  purpose=f'Weryfikacja ostype hostów w inwentarzu szablonu {inventory_id}')
            metadata = exported.get('_meta') if isinstance(exported, dict) else None
            values = metadata.get('hostvars') if isinstance(metadata, dict) else None
            if not isinstance(values, dict):
                raise AWXError(f'Inventory {inventory_id}: cannot verify ostype; AWX returned no host variables.')
            hostvars = {common.hostname_key(name): variables for name, variables in values.items()}
        for batch in inventory_batches:
            for entry in batch['entries']:
                key = common.hostname_key(entry['hostname'])
                if key != 'localhost':
                    variables = hostvars.get(key)
                    actual_os = common.normalize_ostype(variables.get('ostype')) if isinstance(variables, dict) else None
                    rows = ', '.join(map(str, entry['rows']))
                    label = f"Template {batch['template_id']}, host {entry['hostname']!r}, Excel rows {rows}"
                    if not isinstance(actual_os, str) or actual_os in ('', 'none'):
                        raise AWXError(f'{label}: cannot verify ostype; it is missing in AWX inventory {inventory_id}.')
                    if actual_os != entry['ostype']:
                        raise AWXError(f"{label}: Excel ostype {entry['ostype']!r} does not match AWX ostype {actual_os!r} "
                                       f'in inventory {inventory_id}. Refresh Excel before launching.')
                entry['hostname'] = 'localhost' if key == 'localhost' else index[key]['name']
            batch['limit'] = batch_host_limit(entry['hostname'] for entry in batch['entries'])
        debug_step('Walidacja hostów', 'nazwy i OS zgodne z inwentarzem szablonów',
                   Inwentarz_ID=inventory_id, Hosty=len(names), Paczki=len(inventory_batches))


def job_url(template_link, client, job_id):
    return common.job_web_url(template_link, job_id)

def wait_for_job(client, job_id, settings, *, require_success=True):
    """One status polling loop for execution and collection of successful/failed job logs."""
    deadline = time.monotonic() + settings["JOB_WAIT_TIMEOUT"]
    last_status = None
    while True:
        job = client.get(f"jobs/{job_id}/", purpose=f"Odczyt statusu, metadanych i artefaktów zadania {job_id}")
        status = job.get("status")
        if status != last_status:
            console.print_message(f"Zadanie {job_id}: {console.job_status_description(status)}.",
                                  flush=True, level=console.job_status_level(status))
            last_status = status
        if status in ("successful", "failed", "error", "canceled"):
            if require_success and status != "successful":
                raise AWXError(f"Job {job_id} finished with status {status}; remaining batches were not launched.")
            return job
        if not settings["WAIT_FOR_COMPLETION"]:
            return job
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AWXError(f"Timed out waiting for job {job_id}; no further processing was performed.")
        LOGGER.debug("Zadanie %s nadal trwa: kolejne sprawdzenie za %.3f s\n  Pozostały limit oczekiwania: %.3f s",
                     job_id, min(settings["POLL_INTERVAL"], remaining), remaining)
        time.sleep(min(settings["POLL_INTERVAL"], remaining))

def execute_plan(workbook, sheet, path, client, settings, batches, existing, execution_column):
    columns = excel.read_columns(sheet)
    job_type_column = columns['job_type']
    output_column = settings.get('OUTPUT_COLUMN', 'output')
    for entry in existing:
        write_execution(sheet, execution_column, [entry], entry['execution'])
    for batch in batches:
        batch['limit'] = batch_host_limit(entry['hostname'] for entry in batch['entries'])
        for entry in batch['entries']:
            for row_number in entry['rows']:
                host_cell = sheet.cell(row_number, columns['hostname'])
                host_cell.value = entry['hostname']
                host_cell.data_type = 's'
                synchronise_full_credential(sheet, row_number, client=client)
    excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=output_column, excel_columns=settings.get('EXCEL_COLUMNS'))
    for index, batch in enumerate(batches, start=1):
        template_id = batch['template_id']
        console.print_message(client.redact(
            f"Uruchamiam paczkę {index}/{len(batches)}: {batch.get('template_job_name') or 'szablon bez nazwy'} "
            f"(ID szablonu: {template_id})."
        ), flush=True, level='progress')
        for entry in batch['entries']:
            for row_number in entry['rows']:
                cell = sheet.cell(row_number, job_type_column)
                cell.value = batch['job_type']
                cell.data_type = 's'
                cell.number_format = '@'
                if 'template_job_name' in columns and batch.get('template_job_name'):
                    name_cell = sheet.cell(row_number, columns['template_job_name'])
                    name_cell.value = batch['template_job_name']
                    name_cell.data_type = 's'
                if batch.get('job_url_overridden'):
                    template_cell = sheet.cell(row_number, columns['job_link'])
                    template_cell.value = batch['link']
                    template_cell.data_type = 's'
                    template_cell.hyperlink = batch['link']
        marker = f"LAUNCH_PENDING template={template_id}; {datetime.now(timezone.utc).isoformat()}"
        debug_step('Uruchamianie', 'zapisuję zamiar startu przed wysłaniem żądania', Paczka=f'{index}/{len(batches)}',
                   Szablon_ID=template_id, Hosty=batch['limit'], Kolumna=settings['EXECUTION_COLUMN'],
                   Znacznik='LAUNCH_PENDING — zabezpieczenie przed podwójnym uruchomieniem')
        write_execution(sheet, execution_column, batch['entries'], marker)
        excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=output_column, excel_columns=settings.get('EXCEL_COLUMNS'))
        try:
            payload = {'limit': batch['limit'], 'extra_vars': batch['variables']}
            if batch['credentials']:
                payload['credentials'] = batch['credentials']
            debug_step('Uruchamianie', 'wysyłam żądanie startu zadania', Szablon_ID=template_id,
                       Hosty=batch['limit'], ID_poświadczeń=batch.get('credentials', []),
                       Nazwy_zmiennych=', '.join(map(str, batch['variables'])) or 'brak')
            result = client.post(f"job_templates/{template_id}/launch/", payload)
            job_id = result.get('job') or result.get('id')
            if type(job_id) is not int or job_id < 1:
                raise AWXError('AWX nie zwrócił ID uruchomionego joba. Sprawdź stan w AWX przed usunięciem znacznika LAUNCH_PENDING z Excela.')
            link = job_url(batch['link'], client, job_id)
            console.print_message(client.redact(
                f'Paczka {index}/{len(batches)}: AWX przyjął uruchomienie joba o ID {job_id}.\n'
                f'Podgląd joba: {link}'
            ), flush=True, level='success')
            write_execution(sheet, execution_column, batch['entries'], link)
            excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=output_column, excel_columns=settings.get('EXCEL_COLUMNS'))
            debug_step('Uruchamianie', 'zapisano potwierdzony link zadania w Excelu', Zadanie_ID=job_id,
                       Link=link, Plik=path, Oczekiwanie=settings['WAIT_FOR_COMPLETION'])
            if result.get('ignored_fields'):
                raise AWXError(f"Job {job_id}: AWX pominął parametry uruchomienia: {', '.join(result['ignored_fields'])}. Kolejne paczki zostały zatrzymane.")
            if settings['WAIT_FOR_COMPLETION']:
                next_batch = ' Kolejna paczka ruszy po jego sukcesie.' if index < len(batches) else ''
                console.print_message(f'Czekam na zakończenie joba {job_id}.{next_batch}',
                                      level='progress', flush=True)
                job = wait_for_job(client, job_id, settings, require_success=False)
                write_job_artifacts(sheet, job, batch['entries'], client._resolve_url(f"jobs/{job_id}/"))
                excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=output_column, excel_columns=settings.get('EXCEL_COLUMNS'))
                if job['status'] != 'successful':
                    raise AWXError(f"Job {job_id}: {console.job_status_description(job['status'])}. Pozostałe paczki nie zostały uruchomione.")
            else:
                write_job_artifacts(sheet, result, batch['entries'], client._resolve_url(f"jobs/{job_id}/"))
                excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=output_column, excel_columns=settings.get('EXCEL_COLUMNS'))
        except BaseException:
            console.print_message(
                f'Przerwano wykonanie przy paczce {index}/{len(batches)}.\n'
                'Przed ponowieniem sprawdź joby w AWX oraz linki i znaczniki LAUNCH_PENDING zapisane w Excelu.',
                file=sys.stderr, level='warning', flush=True)
            raise

def add_selected_hosts(sheet, hostnames, client, connection, settings, *, job_url_override=None, template_cache=None):
    """Populate only selected hosts missing from Excel, using the existing AWX lookup."""
    columns = excel.read_columns(sheet)
    existing = {common.hostname_key(sheet.cell(row, columns['hostname']).value)
                for row in range(2, sheet.max_row + 1)}
    missing = [hostname for hostname in common.unique_hostnames(hostnames) if common.hostname_key(hostname) not in existing]
    debug_step('Wybór hostów', 'porównano listę z arkuszem', Wybrane=len(hostnames),
               Brakujące_w_Excelu=', '.join(missing) or 'brak')
    if not missing:
        return
    console.print_message(f'Pobieram dane z AWX dla {len(missing)} wybranych hostów, których brakuje w Excelu.',
                          flush=True, level='info')
    report = fetch_report(client, connection['INVENTORY'], hostnames=missing, allow_empty=True)
    if report.get('missing_hosts'):
        raise AWXError("Selected hosts not found in inventory: " + ", ".join(report['missing_hosts']))
    headers, rows = build_inventory_rows(report, settings, settings['OUTPUT_COLUMN'], client=client,
                                        template_cache=template_cache, job_url_override=job_url_override)
    for values in rows:
        row_number = sheet.max_row + 1
        for name, value in zip(headers, values):
            cell = sheet.cell(row_number, columns[name.casefold()])
            cell.value = value
            cell.data_type = 'n' if name == 'HOPS_COUNT' and (value is None or type(value) is int) else 's'
        selected = parse_credentials(sheet.cell(row_number, columns['jumphost_for_hops']).value,
                                     row_number, 'jumphost_for_hops')
        synchronise_full_credential(sheet, row_number, previous_jumps=selected)
    jumphosts.populate_choices(sheet, client, FULL_CREDENTIAL_SEPARATOR)


def display_execution_plan(client, batches, *, preview=False, job_type_variable='action'):
    """Describe each planned job and its exact hosts without printing variable values."""
    hostnames = {common.hostname_key(entry['hostname']) for batch in batches for entry in batch['entries']}
    console.print_record('PODGLĄD PLANU (dry-run)' if preview else 'PLAN DO POTWIERDZENIA', {
        'Joby do uruchomienia': len(batches),
        'Unikalne hosty z arkusza': len(hostnames),
    })
    console.print_message('Każda paczka poniżej uruchamia jeden job w AWX.\n'
                          'Hosty z różnymi ustawieniami trafiają do osobnych paczek.\n',
                          level='info', flush=True)
    for index, batch in enumerate(batches, start=1):
        source_hosts = {common.hostname_key(entry['hostname']) for entry in batch['entries']}
        hosts = []
        for hostname in batch['limit'].split(','):
            note = ' (dodany automatycznie)' if hostname == 'localhost' and hostname not in source_hosts else ''
            hosts.append(f'- {hostname}{note}')
        if 'credentials' in batch:
            credentials = []
            for credential_id in batch['credentials']:
                name = client.credential_cache.get(str(credential_id), {}).get('name')
                credentials.append(f'{name} (ID: {credential_id})' if name else f'ID: {credential_id}')
        else:
            credentials = list(batch['credential_refs'])
        fields = {
            'Nazwa joba': batch.get('template_job_name') or 'szablon bez nazwy',
            'ID szablonu AWX': batch['template_id'],
            'Akcja playbooka': batch['variables'].get(job_type_variable, batch['job_type']) or 'nie podano osobnej akcji',
            'System': {'windows': 'Windows', 'linux': 'Linux', 'aix': 'AIX'}.get(batch['ostype'], batch['ostype']),
            'Hosty w limicie AWX': '\n'.join(hosts),
            'Poświadczenia': '\n'.join(credentials) or 'domyślne z szablonu AWX',
            'Zmienne (wartości ukryte)': '\n'.join(f'{key}: ***' for key in batch['variables']) or 'brak dodatkowych zmiennych',
            'Link do szablonu': batch['link'],
        }
        console.print_record(f'Paczka {index}/{len(batches)} — jeden job',
                             {name: client.redact(str(value)) for name, value in fields.items()})


def confirm_execution_plan(client, batches, *, job_type_variable='action'):
    """Show every validated batch and require approval before saving or launching."""
    display_execution_plan(client, batches, job_type_variable=job_type_variable)
    if not sys.stdin.isatty():
        console.print_message('Brak interaktywnego terminala do potwierdzenia planu.', level='warning')
        return False
    try:
        console.print_message("Wpisz 'tak', aby uruchomić cały powyższy plan. Enter anuluje.", flush=True)
        answer = input('Czy uruchomić wszystkie pokazane joby? [tak/N]: ')
    except EOFError:
        return False
    return answer.strip().casefold() in ('tak', 't', 'yes', 'y')


def choose_execution_scope(column_name, batches, existing, *, requested='ask', preview=False):
    """Select new entries or all selected entries; previews default to new entries."""
    if requested != 'ask':
        return requested
    if preview or not sys.stdin.isatty():
        return 'missing'
    console.print_record('WYBÓR ZAKRESU URUCHOMIENIA', {
        f'Wpisy bez linku w {column_name}': sum(len(batch['entries']) for batch in batches),
        'Wpisy z wcześniejszym jobem': len(existing),
    })
    console.print_message(
        '1 — Tylko wpisy bez linku do wcześniejszego joba (domyślnie).\n'
        '2 — Wszystkie wybrane wpisy, również te z wcześniejszym jobem.\n'
        '0 — Anuluj.\n'
        'Po wyborze zobaczysz pełny plan do potwierdzenia.', flush=True)
    while True:
        try:
            answer = input('Na których wpisach uruchomić joby? [1/2/0, Enter=1]: ').strip().casefold()
        except EOFError:
            return None
        if answer in ('', '1', 'missing'):
            return 'missing'
        if answer in ('2', 'all'):
            return 'all'
        if answer in ('0', 'n', 'nie', 'anuluj'):
            return None
        console.print_message('Wpisz 1 (bez linku), 2 (wszystkie) albo 0 (anuluj).', level='warning', flush=True)


def archive_execution_history(sheet, execution_column, entries):
    """Prepare reruns in memory; keep the previous job in the cell comment."""
    for entry in entries:
        for row_number in entry['rows']:
            cell = sheet.cell(row_number, execution_column)
            text = cell.comment.text if cell.comment else ''
            note = 'Poprzedni job: ' + entry['execution']
            if note not in text.splitlines():
                text = (text + '\n' if text else '') + note
            cell.comment = Comment(text, (cell.comment.author or 'AWX') if cell.comment else 'AWX')
            cell.value = None
            cell.hyperlink = None


def execute_main(argv=None):
    started = time.perf_counter()
    args = parse_execute_arguments(argv)
    if args.dry_run:
        console.print_message(
            'Tryb podglądu (dry-run): przygotowuję plan wykonania.\n'
            'Joby nie zostaną uruchomione, a plik Excel nie zostanie zapisany.',
            level='warning', flush=True)
    if args.job_type is not None and (not args.job_type.strip()):
        raise AWXError('--job-type must be nonempty, e.g. install, reinstall, or update.')
    if args.job_url is not None and not args.job_url.strip():
        raise AWXError('--job-url must contain a job template URL.')
    connection = load_connection(args.debug)
    settings = read_execution_settings()
    settings['OUTPUT_COLUMN'] = common.read_output_settings()['OUTPUT_COLUMN']
    interactive_survey = args.survey if args.survey is not None else settings['SURVEY_INTERACTIVE']
    survey_overrides = None
    if args.survey_answers is not None:
        try:
            survey_overrides = json.loads(args.survey_answers)
        except ValueError:
            raise AWXError('--survey-answers must contain a JSON object.') from None
        survey_overrides = survey.answer_mapping(survey_overrides, '--survey-answers')
    if args.batch_size is not None:
        if args.batch_size < 1:
            raise AWXError('--batch-size must be positive.')
        settings['BATCH_SIZE'] = args.batch_size
    selected_hosts = common.normalize_hostnames(args.hosts, required=True) if args.hosts is not None else settings.get('HOSTS', [])
    selected_job_url = args.job_url.strip() if args.job_url is not None else ''
    path = workbook_path(args.input_file, connection, settings)
    debug_step('Wykonanie', 'efektywne ustawienia uruchomienia', Plik=path,
               Hosty=selected_hosts or 'wszystkie hosty arkusza', Szablon=selected_job_url or 'Job_Link z każdego wiersza',
               Limit_hostów_na_paczkę=settings['BATCH_SIZE'], Tylko_podgląd=args.dry_run,
               Oczekiwanie=settings['WAIT_FOR_COMPLETION'], Pytania_ankiety=interactive_survey,
               Widoczne_kolumny=settings.get('EXCEL_COLUMNS') or 'wszystkie')
    client = AWXClient(connection['AWX_URL'], connection['AWX_TOKEN'].strip())
    check_connection(client, debug=args.debug)
    template_cache = {}
    if selected_job_url:
        try:
            template_id = template_id_from_link(selected_job_url, client)
        except AWXError as exc:
            raise AWXError(f'--job-url: {exc}') from None
        if args.show_survey:
            show_template_surveys(client, {template_id: selected_job_url})
            return 0
    if path.exists() or not selected_hosts:
        workbook, sheet = open_workbook_sheet(path, settings["SHEET_NAME"], args.sheet)
    else:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = args.sheet or settings['SHEET_NAME']
        LOGGER.debug('Wykonanie: plik nie istnieje — przygotowano nowy arkusz dla wybranych hostów.')
    try:
        _, added = excel.prepare_sheet(sheet, settings['EXECUTION_COLUMN'], settings['OUTPUT_COLUMN'])
        LOGGER.debug('Wykonanie: uzupełnione kolumny: %s\n  Hosty porządkuję według systemów: Windows, Linux, AIX.',
                     ', '.join(added) or 'brak')
        if selected_hosts:
            add_selected_hosts(sheet, selected_hosts, client, connection, settings,
                               job_url_override=selected_job_url, template_cache=template_cache)
        excel.sort_host_rows(sheet)
        batches, existing, column, skipped = build_execution_plan(
            sheet, client, settings, job_type_override=args.job_type, hostnames=selected_hosts or None,
            job_url_override=selected_job_url or None, survey_overrides=survey_overrides)
        if args.show_survey:
            links = {entry['template_id']: entry['link'] for entry in [*batches, *existing]}
            show_template_surveys(client, links)
            return 0
        console.print_record('PRZYGOTOWANIE URUCHOMIENIA', {
            'Plik Excel': client.redact(str(path)),
            'Arkusz': client.redact(sheet.title),
            'Maks. hostów w paczce': settings['BATCH_SIZE'],
            'Nowe paczki': len(batches),
            'Wpisy z historią': len(existing),
            'Pominięte wiersze bez szablonu': skipped,
            'Tryb wykonania': ('kolejne paczki po sukcesie poprzedniego joba' if settings['WAIT_FOR_COMPLETION']
                               else 'wysłanie wszystkich paczek bez czekania na wyniki'),
        })
        if not args.dry_run and any(entry['execution'].startswith('LAUNCH_PENDING') for entry in existing):
            raise AWXError('W Excelu jest znacznik LAUNCH_PENDING po wcześniejszej próbie uruchomienia. Sprawdź w AWX, czy job powstał. Zastąp znacznik linkiem do joba lub usuń go dopiero po upewnieniu się, że job nie został uruchomiony.')
        scope = choose_execution_scope(
            settings['EXECUTION_COLUMN'], batches, existing,
            requested=args.execution_scope or settings['EXECUTION_SCOPE'], preview=args.dry_run)
        if scope is None:
            console.print_message('Uruchomienie anulowane. Joby nie zostały uruchomione; plik Excel nie został zmieniony.',
                                  level='warning', flush=True)
            return 0
        console.print_message('Wybrany zakres: ' + ('wszystkie wybrane wpisy, również z historią.' if scope == 'all'
                                                   else 'tylko wpisy bez linku do wcześniejszego joba.'), flush=True)
        rerun_entries = []
        if scope == 'all' and existing:
            rerun_entries = existing
            archive_execution_history(sheet, column, rerun_entries)
            batches, existing, column, skipped = build_execution_plan(
                sheet, client, settings, job_type_override=args.job_type, hostnames=selected_hosts or None,
                job_url_override=selected_job_url or None, survey_overrides=survey_overrides)
        if args.dry_run:
            LOGGER.debug('Podgląd wykonania: nie uruchamiam zadań i nie zapisuję pliku\n  Walidacja ankiety w AWX: %s',
                         'włączona' if interactive_survey else 'wyłączona w zwykłym podglądzie')
            if interactive_survey:
                rows = [row for batch in batches for entry in batch['entries'] for row in entry['rows']]
                cache = update_survey_columns(client, sheet, settings, job_url_override=selected_job_url,
                                              row_numbers=rows, cache=template_cache)
                batches, existing, column, skipped = build_execution_plan(
                    sheet, client, settings, job_type_override=args.job_type, hostnames=selected_hosts or None,
                    job_url_override=selected_job_url or None, survey_overrides=survey_overrides, survey_cache=cache)
                validate_templates(client, batches, interactive=True, check_credentials=False, cache=cache)
            display_execution_plan(client, batches, preview=True, job_type_variable=settings['JOB_TYPE_VARIABLE'])
            console.print_message('Podgląd zakończony: joby nie zostały uruchomione; plik Excel nie został zmieniony.',
                                  level="warning", flush=True)
            return 0
        if not batches:
            excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=settings['OUTPUT_COLUMN'], excel_columns=settings.get('EXCEL_COLUMNS'))
            console.print_message('Brak wpisów do uruchomienia w wybranym zakresie. Zapisano układ arkusza.\n'
                                  'Aby ponowić joby z historią, wybierz opcję 2 lub --execution-scope all.',
                                  level='warning', flush=True)
            return 0
        console.print_message('Sprawdzam w AWX szablony jobów, hosty, systemy, poświadczenia i odpowiedzi survey.',
                              level='progress', flush=True)
        rows = [row for batch in batches for entry in batch['entries'] for row in entry['rows']]
        cache = update_survey_columns(client, sheet, settings, job_url_override=selected_job_url,
                                      row_numbers=rows, cache=template_cache)
        batches, existing, column, skipped = build_execution_plan(
            sheet, client, settings, job_type_override=args.job_type, hostnames=selected_hosts or None,
            job_url_override=selected_job_url or None, survey_overrides=survey_overrides, survey_cache=cache)
        resolve_execution_hostnames(client, batches, cache)
        resolve_credentials(client, batches, template_cache=cache)
        validate_templates(client, batches, interactive=interactive_survey, cache=cache)
        LOGGER.debug('Wykonanie: przygotowanie zakończone — zweryfikowano szablony, poświadczenia, zmienne i nazwy hostów.')
        if not confirm_execution_plan(client, batches, job_type_variable=settings['JOB_TYPE_VARIABLE']):
            console.print_message('Uruchomienie anulowane. Joby nie zostały uruchomione; plik Excel nie został zmieniony.',
                                  level='warning', flush=True)
            return 0
        if settings['WAIT_FOR_COMPLETION']:
            recorded_jobs = {}
            previous = [(entry, True) for entry in existing] + [(entry, False) for entry in rerun_entries]
            for entry, require_success in previous:
                parsed = urlsplit(entry['execution'])
                match = re.search('/jobs/(?:playbook/)?([1-9][0-9]*)(?:/|$)', parsed.fragment or parsed.path)
                if match and client._origin(parsed) == client.origin:
                    job_id = int(match.group(1))
                    recorded_jobs[job_id] = recorded_jobs.get(job_id, False) or require_success
            for job_id in sorted(recorded_jobs):
                console.print_message(f'Sprawdzam zakończenie wcześniejszego joba {job_id} przed startem nowego planu.',
                                      level='progress', flush=True)
                wait_for_job(client, job_id, settings, require_success=recorded_jobs[job_id])
        execute_plan(workbook, sheet, path, client, settings, batches, existing, column)
        console.print_message(f'Zakończono uruchamianie planu. Liczba uruchomionych jobów: {len(batches)}.',
                              level='success', flush=True)
        console.print_message(client.redact(
            f"Linki do jobów zapisano w kolumnie {settings['EXECUTION_COLUMN']!r}.\nPlik Excel: {path}"
        ), level='success', flush=True)
        if settings['WAIT_FOR_COMPLETION']:
            console.print_message('Wszystkie joby z planu zakończyły się pomyślnie. Artefakty zapisano w Excelu.',
                                  level='success', flush=True)
        else:
            console.print_message('Joby mogą nadal działać w AWX. Końcowe logi i artefakty pobierz przez awx_job_output.py.',
                                  level='info', flush=True)
        return 0
    finally:
        workbook.close()
        debug_step('Wykonanie', 'zamknięto plik Excel', Plik=path,
                   Żądania_API=client.request_count, Czas_s=f'{time.perf_counter() - started:.3f}')


# COLLECTOR WORKFLOW

OUTPUT_SETTINGS_FILE = common.OUTPUT_SETTINGS_FILE
OUTPUT_DEFAULTS = common.OUTPUT_DEFAULTS
JOB_PATTERN = re.compile('(?:^|/)jobs/(?:playbook/)?([1-9][0-9]*)(?:/|$)')
TERMINAL_STATUSES = {'successful', 'failed', 'error', 'canceled'}
EXCEL_CELL_LIMIT = 32767

def read_output_settings(settings_file=None):
    return common.read_output_settings(settings_file)

def parse_output_arguments(argv=None):
    parser = operation_parser("Pobierz stdout jobów AWX do Excela i plików TXT.", operation="output")
    parser.add_argument("--output-dir", type=Path, help="Katalog pełnych logów TXT; nadpisuje OUTPUT_DIRECTORY.")
    return parse_operation_arguments(parser, argv)

def job_id_from_link(link, client):
    return resource_id_from_link(link, client, JOB_PATTERN, "Execution link")

def build_output_plan(sheet, client, settings):
    execution_name = common.normalize_execution_column(settings['EXECUTION_COLUMN']).casefold()
    excel.migrate_execution_column(sheet, execution_name)
    columns = excel.read_columns(sheet)
    output_name = settings['OUTPUT_COLUMN'].strip().casefold()
    reserved = {name.casefold() for name in (*common.METADATA_COLUMNS, *common.ARTIFACT_COLUMNS)}
    if output_name == execution_name or output_name in reserved:
        raise AWXError('OUTPUT_COLUMN must name a separate output column.')
    for name in (execution_name, 'ostype'):
        if name not in columns:
            raise AWXError(f"Missing {name} column. Use the Excel workbook updated by awx_execute_jobs.py.")
    jobs = {}
    skipped = 0
    for row_number in range(2, sheet.max_row + 1):
        cell = sheet.cell(row_number, columns[execution_name])
        value = str(cell.value or '').strip()
        if value.casefold() in ('', 'none') or value.startswith('LAUNCH_PENDING'):
            reason = ('zapisano zamiar uruchomienia (LAUNCH_PENDING), ale brak potwierdzonego linku\n  Sprawdź zadanie w AWX'
                      if value.startswith('LAUNCH_PENDING') else 'brak linku do uruchomionego zadania')
            LOGGER.debug('Pomijam wiersz %d: %s.', row_number, reason)
            skipped += 1
            continue
        if cell.data_type == 'f':
            raise AWXError(f"Row {row_number}: execution link must contain a URL rather than an Excel formula.")
        link = cell.hyperlink.target if cell.hyperlink and cell.hyperlink.target else value
        try:
            job_id = job_id_from_link(link, client)
        except (AWXError, ValueError) as exc:
            raise AWXError(f"Row {row_number}: {exc}") from None
        ostype = str(sheet.cell(row_number, columns['ostype']).value or 'unknown').strip().casefold() or 'unknown'
        jobs.setdefault(job_id, []).append({'row': row_number, 'ostype': ostype})
    return (jobs, columns.get(output_name, sheet.max_column + 1), skipped)



def read_job(client, job_id, settings):
    return wait_for_job(client, job_id, settings, require_success=False)

def job_inventory_name(client, job, cache):
    summary = (job.get('summary_fields') or {}).get('inventory') or {}
    if summary.get('name'):
        return str(summary['name'])
    inventory_id = job.get('inventory')
    if not inventory_id:
        return 'unknown_inventory'
    if inventory_id not in cache:
        try:
            details = client.get(f"inventories/{inventory_id}/", purpose=f"Odczyt nazwy inwentarza o ID {inventory_id} do nazwy pliku TXT")
            cache[inventory_id] = str(details.get('name') or inventory_id)
        except AWXError:
            LOGGER.debug('Nazwa inwentarza %s jest niedostępna\n  W nazwach plików używam jego ID.', inventory_id)
            cache[inventory_id] = str(inventory_id)
    return cache[inventory_id]

def filename_part(value):
    """Keep every filename inside the output directory, including on Windows."""
    part = re.sub('[<>:"/\\\\|?*\\x00-\\x1f]', '_', str(value)).strip(' .')
    return part[:100].rstrip(' .') or 'unknown'

def save_text(path, content):
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='', dir=path.parent, prefix=f".{path.stem}-", suffix='.tmp', delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
        os.replace(temporary_path, path)
    except OSError as exc:
        raise AWXError(f"Cannot save stdout file {path}: {exc}") from None
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()

def artifact_messages(artifacts, hostname):
    """Find message values under this exact host, across nested dict/list results."""
    target = str(hostname or '').strip().casefold()
    if not target:
        return ''
    if isinstance(artifacts, str):
        try:
            artifacts = json.loads(artifacts)
        except ValueError:
            return ''
    messages = {}
    pending = [(artifacts, False)]
    while pending:
        value, selected = pending.pop()
        if isinstance(value, dict):
            identifiers = [item for key, item in value.items()
                           if str(key).casefold() in ('host', 'hostname', 'inventory_hostname')
                           and isinstance(item, str)]
            if identifiers:
                selected = any(item.strip().casefold() == target for item in identifiers)
            elif isinstance(value.get('name'), str) and value['name'].strip().casefold() == target:
                selected = True
            for key, item in reversed(list(value.items())):
                key = str(key).strip().casefold()
                if key == 'message':
                    if selected and item is not None:
                        message = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                        messages.setdefault(message, None)
                    continue
                pending.append((item, selected or key == target))
        elif isinstance(value, list):
            pending.extend((item, selected) for item in reversed(value))
    return '\n'.join(messages)


def write_job_artifacts(sheet, job, entries, source):
    """Shared job results writer for the launcher and the output collector."""
    artifacts = job.get('artifacts')
    artifacts = {} if artifacts is None else artifacts
    try:
        content = json.dumps(artifacts, ensure_ascii=False, indent=2, allow_nan=False)
    except (TypeError, ValueError):
        raise AWXError('Job artifacts must contain valid JSON data.') from None
    columns = excel.read_columns(sheet)
    ensure_columns(sheet, columns, common.ARTIFACT_COLUMNS)
    for entry in entries:
        rows = entry['rows'] if 'rows' in entry else [entry['row']]
        for row_number in rows:
            hostname = sheet.cell(row_number, columns['hostname']).value if 'hostname' in columns else ''
            message = artifact_messages(artifacts, hostname)
            write_output(sheet, columns['artifacts'], row_number, content, source, label='Artifacts')
            write_output(sheet, columns['artifact filter'], row_number, message, source, label='Artifact message')


def write_output(sheet, column, row_number, content, text_path, *, label='Output'):
    text = ILLEGAL_CHARACTERS_RE.sub('', content)
    if len(text) > EXCEL_CELL_LIMIT:
        location = 'full log' if label == 'Output' else 'full artifacts'
        suffix = f"\n[{label} truncated in Excel; {location}: {text_path}]"
        text = text[:EXCEL_CELL_LIMIT - len(suffix)] + suffix
    cell = sheet.cell(row_number, column)
    cell.value = text
    cell.data_type = 's'
    cell.number_format = '@'
    cell.hyperlink = None

def collect_outputs(workbook, sheet, path, output_directory, client, settings, jobs, column):
    started = time.perf_counter()
    console.print_message('Przygotowuję i zapisuję arkusz przed pobieraniem wyników.', level='progress', flush=True)
    excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=settings['OUTPUT_COLUMN'], excel_columns=settings.get('EXCEL_COLUMNS'))
    output_directory.mkdir(parents=True, exist_ok=True)
    inventory_names = {}
    failures = 0
    completed = 0
    saved_files = 0
    updated_rows = 0
    for index, (job_id, entries) in enumerate(jobs.items(), start=1):
        artifacts_written = False
        log_written = False
        stage = 'odczyt statusu i metadanych'
        console.print_message(
            f'[{index}/{len(jobs)}] Zadanie {job_id}: sprawdzam status i wyniki.\n'
            f'Liczba powiązanych wierszy Excela: {len(entries)}.', level='progress', flush=True)
        LOGGER.debug('Zadanie %s: wiersze arkusza do aktualizacji: %s', job_id,
                     ', '.join(str(entry['row']) for entry in entries))
        try:
            job = read_job(client, job_id, settings)
            if job.get('status') not in TERMINAL_STATUSES:
                console.print_message(
                    f'Zadanie {job_id} jeszcze się nie zakończyło. Pobieram log dostępny teraz.\n'
                    'Ponowne uruchomienie skryptu uzupełni wynik.', level='warning')
            elif job.get('status') != 'successful':
                console.print_message(
                    f'Zadanie {job_id} nie zakończyło się sukcesem. Pobieram jego log i artefakty do analizy.',
                    level='warning')
            stage = 'przygotowanie artefaktów i nazwy pliku'
            name = filename_part(job_inventory_name(client, job, inventory_names))
            write_job_artifacts(sheet, job, entries, client._resolve_url(f"jobs/{job_id}/"))
            artifacts_written = True
            console.print_message(
                f'Zadanie {job_id}: przygotowano Artifacts (JSON) i artifact filter '
                f'(komunikaty message dopasowane do hostname).\nLiczba wierszy: {len(entries)}.', level='info')
            stage = 'pobieranie pełnego logu z AWX'
            console.print_message(f'Zadanie {job_id}: pobieram pełny log wykonania (stdout).', level='progress', flush=True)
            content = client.stdout(job_id)
            console.print_message(f'Zadanie {job_id}: pobrano {len(content)} znaków logu.'
                                  + (' AWX zwrócił pusty log.' if not content else ''), level='info')
            stage = 'zapis pełnego logu do plików TXT'
            files = {}
            for entry in entries:
                os_name = filename_part(entry['ostype'])
                if os_name not in files:
                    text_path = output_directory / f"{job_id}.{name}.{os_name}.txt"
                    save_text(text_path, content)
                    files[os_name] = text_path
                    saved_files += 1
                    console.print_message(f'Zadanie {job_id}: zapisano pełny log TXT (UTF-8): {text_path}',
                                          flush=True, level='success')
            clean_length = len(ILLEGAL_CHARACTERS_RE.sub('', content))
            if clean_length > EXCEL_CELL_LIMIT:
                console.print_message(
                    f'Zadanie {job_id}: log przekracza limit komórki Excela ({EXCEL_CELL_LIMIT} znaków). '
                    'W Excelu zapisuję skrót z adresem pełnego pliku TXT.', level='warning')
            if clean_length != len(content):
                LOGGER.debug('Zadanie %s: usuwam %d znaków niedozwolonych w Excelu\n  Plik TXT zachowuje pełną treść.',
                             job_id, len(content) - clean_length)
            stage = 'wpisywanie logu do komórek Excela'
            for entry in entries:
                write_output(sheet, column, entry['row'], content, files[filename_part(entry['ostype'])])
                updated_rows += 1
            log_written = True
            completed += 1
        except AWXError as exc:
            failures += 1
            console.print_message(
                f'Zadanie {job_id}: błąd pobierania wyników.\nEtap: {stage}.\n{exc}',
                file=sys.stderr, level='error', flush=True)
        finally:
            if artifacts_written:
                excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=settings['OUTPUT_COLUMN'], excel_columns=settings.get('EXCEL_COLUMNS'))
                if log_written:
                    console.print_message(
                        f'Zadanie {job_id}: zapisano Excel — log w kolumnie {settings["OUTPUT_COLUMN"]!r}, '
                        f'artefakty w Artifacts i artifact filter.\nLiczba wierszy: {len(entries)}.', level='success')
                else:
                    console.print_message(
                        f'Zadanie {job_id}: zapisano artefakty w Excelu. Pobieranie logu nie zostało ukończone.\n'
                        'Dotychczasowa zawartość kolumny logu pozostaje zachowana.', level='warning')
    console.print_message(
        f'Zakończono pobieranie.\nZadania z pobranymi logami: {completed}/{len(jobs)}\n'
        f'Błędy pobierania: {failures}\nZaktualizowane wiersze logów: {updated_rows}\n'
        f'Zapisane pliki TXT: {saved_files}\nCzas: {time.perf_counter() - started:.1f} s.',
        flush=True, level='error' if failures else 'success')
    console.print_message(f'Plik Excel z wynikami: {path}', level='info')
    return 1 if failures else 0

def output_main(argv=None):
    args = parse_output_arguments(argv)
    connection = load_connection(args.debug)
    launch_settings = read_execution_settings()
    settings = read_output_settings()
    settings['SHEET_NAME'] = settings['SHEET_NAME'] or launch_settings['SHEET_NAME']
    settings['EXECUTION_COLUMN'] = settings['EXECUTION_COLUMN'] or launch_settings['EXECUTION_COLUMN']
    path = workbook_path(args.input_file, connection, settings, launch_settings)
    output_directory = Path(args.output_dir or settings['OUTPUT_DIRECTORY']).resolve()
    client = AWXClient(connection['AWX_URL'], connection['AWX_TOKEN'].strip())
    check_connection(client, debug=args.debug)
    console.print_message(f'Odczytuję plik Excel: {path}', level='info', flush=True)
    workbook, sheet = open_workbook_sheet(path, settings["SHEET_NAME"], args.sheet)
    try:
        _, added = excel.prepare_sheet(sheet, settings['EXECUTION_COLUMN'], settings['OUTPUT_COLUMN'])
        LOGGER.debug('Arkusz %r: uzupełnione brakujące kolumny: %s', sheet.title, ', '.join(added) or 'brak')
        excel.sort_host_rows(sheet)
        jobs, column, skipped = build_output_plan(sheet, client, settings)
        console.print_message(
            f'Arkusz {sheet.title!r}\nWiersze danych: {max(0, sheet.max_row - 1)}\n'
            f'Zadania do odczytu: {len(jobs)}\nPowiązane wiersze: {sum(map(len, jobs.values()))}\n'
            f'Pominięte wiersze (brak linku lub LAUNCH_PENDING): {skipped}.', level='info')
        console.print_message(
            f'Linki do zadań odczytuję z kolumny {settings["EXECUTION_COLUMN"]!r}\n'
            f'Logi zapisuję w {settings["OUTPUT_COLUMN"]!r}\nPełne pliki TXT: {output_directory}.', level='info')
        console.print_message(
            'Każde zadanie pobieram raz, a jego log kopiuję do wszystkich powiązanych wierszy. '
            'Artifacts zawiera artefakty zadania w JSON, a artifact filter — komunikaty message dla hosta z danego wiersza.',
            level='info')
        if settings['WAIT_FOR_COMPLETION']:
            console.print_message(
                f'Oczekiwanie włączone: sprawdzam status co {settings["POLL_INTERVAL"]} s\n'
                f'Limit oczekiwania na zadanie: {settings["JOB_WAIT_TIMEOUT"]} s.', level='info')
        else:
            console.print_message(
                'Oczekiwanie wyłączone: pobieram aktualnie dostępne wyniki, również zadań jeszcze trwających.',
                level='info')
        if settings.get('EXCEL_COLUMNS'):
            LOGGER.debug('Widoczne kolumny Excela: %s\n  Pozostałe dane pozostają w ukrytych kolumnach.',
                         ', '.join(settings['EXCEL_COLUMNS']))
        else:
            LOGGER.debug('Widoczne kolumny Excela: wszystkie (EXCEL_COLUMNS jest puste).')
        if not jobs:
            excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=settings['OUTPUT_COLUMN'], excel_columns=settings.get('EXCEL_COLUMNS'))
            console.print_message(
                'Brak zadań z potwierdzonym linkiem do pobrania wyników. Zapisano układ arkusza.\n'
                'Nie wysłano żądań pobrania logów do AWX.', level='warning')
            return 0
        return collect_outputs(workbook, sheet, path, output_directory, client, settings, jobs, column)
    finally:
        workbook.close()


# REFRESH WORKFLOW

HOST_COLUMNS = excel.HOST_COLUMNS

def parse_refresh_arguments(argv=None):
    parser = operation_parser("Odśwież dane hostów w tym samym Excelu, zachowując historię jobów.", operation="refresh")
    parser.add_argument("--host-column", help="Nagłówek kolumny hostów; nadpisuje HOST_COLUMN i automatyczne wykrywanie.")
    return parse_operation_arguments(parser, argv)

def read_host_rows(sheet, host_column=None):
    columns = excel.read_columns(sheet)
    selected = host_column.strip().casefold() if host_column is not None else next((name for name in HOST_COLUMNS if name in columns), None)
    if any((merged.max_row > 1 for merged in sheet.merged_cells.ranges)):
        raise AWXError('Host data rows must not contain merged cells. Use a table with one host per row.')
    if selected not in columns:
        if host_column is not None:
            raise AWXError(f"Cannot find host column {host_column!r}. Check --host-column HEADER.")
        source_column = next(iter(columns.values()), 1)
        ensure_columns(sheet, columns, ['hostname'], source_column)
        console.print_message('Added missing hostname column. Fill it with hostnames or use --host-column HEADER for an existing column.', flush=True, level="warning")
        return ({}, columns, columns['hostname'])
    rows = {}
    for row_number in range(2, sheet.max_row + 1):
        cell = sheet.cell(row_number, columns[selected])
        if cell.value is None or not str(cell.value).strip():
            continue
        hostname = str(cell.value).replace(',', '').strip()
        if cell.data_type == 'f' or (hostname and not HOSTNAME_PATTERN.fullmatch(hostname)):
            raise AWXError(f"Row {row_number}: the host column must contain one literal hostname per cell.")
        if hostname:
            rows[row_number] = hostname
    return (rows, columns, columns[selected])

def metadata_by_host(report, settings, *, client=None, template_cache=None):
    """Refresh uses the host's ApplicationID organization and live AWX template."""
    settings = common.normalize_refresh_settings(dict(settings))
    headers, rows = build_inventory_rows(report, settings, settings.get('OUTPUT_COLUMN', 'output'),
                                        client=client, template_cache=template_cache,
                                        auto_find_template=True)
    metadata = {}
    for row in rows:
        record = dict(zip(headers, row))
        hostname = record['hostname']
        metadata[common.hostname_key(hostname)] = {name: record[name] for name in excel.METADATA_COLUMNS}
    return (metadata, list(excel.METADATA_COLUMNS[1:]))

def refresh_sheet(sheet, host_rows, columns, source_column, metadata, headers, execution_column=common.DEFAULT_EXECUTION_COLUMN, preserve_unidentified_rows=False, output_column='output'):
    missing_values = {name: 'none' for name in headers}
    missing_values.update(Job_Variables='{}', Job_Type='', AWX_Status='not_found', HOPS_COUNT=None)
    repaired, added = excel.prepare_sheet(sheet, execution_column, output_column, reorder=False)
    columns.clear()
    columns.update(repaired)
    if added:
        console.print_message(f"Added missing columns: {', '.join(added)}", flush=True, level="info")
    for row_number in range(2, sheet.max_row + 1):
        previous_context = tuple(sheet.cell(row_number, columns[name]).value for name in ('applicationid', 'hops_count'))
        if row_number in host_rows:
            hostname = host_rows[row_number]
            values = {'hostname': hostname, **metadata.get(common.hostname_key(hostname), missing_values)}
        elif preserve_unidentified_rows:
            LOGGER.debug('Odświeżanie: pozostawiam wiersz %d bez zmian — brak rozpoznanej kolumny hostów.', row_number)
            continue
        elif any((cell.value is not None and str(cell.value).strip() for cell in sheet[row_number])):
            values = {name: '' for name in ['hostname', *headers]}
            values['AWX_Status'] = 'no_hostname'
        else:
            continue
        debug_step('Odświeżanie wiersza', 'aktualizuję metadane hosta', Wiersz=row_number,
                   Host=values.get('hostname') or 'brak', AWX_Status=values.get('AWX_Status'),
                   Chronione_dane='linki wykonań, logi, artefakty i kolumny użytkownika')
        for name, value in values.items():
            cell = sheet.cell(row_number, columns[name.casefold()])
            if (name == 'jumphost_for_hops' and row_number in host_rows and common.hostname_key(host_rows[row_number]) in metadata
                    and previous_context == (values.get('ApplicationID'), values.get('HOPS_COUNT'))
                    and cell.value is not None and str(cell.value).strip()):
                LOGGER.debug('Wiersz %d: zachowuję wybrany jumphost — ApplicationID i HOPS_COUNT nie zmieniły się.', row_number)
                continue
            cell.value = value
            numeric = name == 'HOPS_COUNT' and (value is None or type(value) is int)
            cell.data_type = 'n' if numeric else 's'
            cell.number_format = '0' if numeric else '@'
            cell.hyperlink = value if name == 'Job_Link' and value.startswith(('http://', 'https://')) else None
        selected_by_metadata = parse_credentials(values.get('jumphost_for_hops'), row_number, 'jumphost_for_hops')
        synchronise_full_credential(sheet, row_number, previous_jumps=selected_by_metadata)
    repaired, _ = excel.prepare_sheet(sheet, execution_column, output_column)
    columns.clear()
    columns.update(repaired)
    if not preserve_unidentified_rows:
        excel.sort_host_rows(sheet)

def refresh_main(argv=None):
    started = time.perf_counter()
    args = parse_refresh_arguments(argv)
    connection = load_refresh_connection(args.debug)
    settings = common.read_execution_settings(refresh=True)
    settings['OUTPUT_COLUMN'] = common.read_output_settings()['OUTPUT_COLUMN']
    path = workbook_path(args.input_file, connection, settings)

    debug_step('Odświeżanie', 'ustawienia operacji', Plik=path, Wzorzec_inventory=settings['REFRESH_INVENTORY_PATTERN'],
               Kolumna_hostów=args.host_column or 'automatyczne wykrywanie',
               Kolumna_wykonań=settings['EXECUTION_COLUMN'], Kolumna_logów=settings['OUTPUT_COLUMN'],
               Widoczne_kolumny=settings.get('EXCEL_COLUMNS') or 'wszystkie')
    client = None
    if args.debug:
        client = AWXClient(connection['AWX_URL'], connection['AWX_TOKEN'].strip())
        check_connection(client, debug=True)
    workbook, sheet = open_workbook_sheet(path, settings["SHEET_NAME"], args.sheet)
    try:
        sheet_name = sheet.title
        had_host_column = any((str(cell.value or '').strip().casefold() in HOST_COLUMNS for cell in sheet[1])) or args.host_column is not None
        host_rows, columns, source_column = read_host_rows(sheet, args.host_column)
        hostnames = common.unique_hostnames(host_rows.values())
        debug_step('Odświeżanie', 'odczytano hosty z arkusza', Arkusz=sheet_name,
                   Kolumna_źródłowa=get_column_letter(source_column), Wiersze_z_hostami=len(host_rows),
                   Unikalne_hosty=len(hostnames), Powtórzone_wiersze=len(host_rows) - len(hostnames))
        console.print_message(f"Input: {path}\nworksheet: {sheet_name}\nunique hosts: {len(hostnames)}", flush=True, level="info")
        template_cache = {}
        if hostnames:
            if client is None:
                client = AWXClient(connection['AWX_URL'], connection['AWX_TOKEN'].strip())
            report = fetch_refresh_report(client, hostnames, settings['REFRESH_INVENTORY_PATTERN'])
        else:
            console.print_message('Brak hostów w Excelu. Aktualizuję nagłówki bez pobierania danych hostów z AWX.',
                                  flush=True, level='warning')
            report = {'hosts': [], 'host_count': 0, 'missing_hosts': []}
        metadata, headers = metadata_by_host(report, settings, client=client, template_cache=template_cache)
        debug_step('Odświeżanie', 'porównano hosty z AWX', Znalezione=len(metadata),
                   Brakujące=', '.join(report.get('missing_hosts', [])) or 'brak',
                   Aktualizowane_kolumny=', '.join(headers))
        for record in metadata.values():
            console.print_message(client.redact(
                f"Host: {record['hostname']}\nOrganizacja (ApplicationID): {record['ApplicationID']}\n"
                f"template_job_name: {record['template_job_name']}\nJob_Link: {record['Job_Link']}"
            ), level='data')
        protected = {settings['EXECUTION_COLUMN'].casefold(), common.DEFAULT_EXECUTION_COLUMN,
                     common.LEGACY_EXECUTION_COLUMN, settings['OUTPUT_COLUMN'].casefold()}
        managed = {name.casefold() for name in ['hostname', *headers]}
        if protected & managed or columns.get(settings['EXECUTION_COLUMN'].casefold()) == source_column or columns.get(settings['OUTPUT_COLUMN'].casefold()) == source_column:
            raise AWXError('The host or metadata column conflicts with a job history column.')
        refresh_sheet(sheet, host_rows, columns, source_column, metadata, headers, execution_column=settings['EXECUTION_COLUMN'], preserve_unidentified_rows=not had_host_column, output_column=settings['OUTPUT_COLUMN'])
        if hostnames:
            update_survey_columns(client, sheet, settings, overwrite_configured=True, cache=template_cache,
                                  job_url_override='')
            jumphosts.populate_choices(sheet, client, FULL_CREDENTIAL_SEPARATOR)
        excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=settings['OUTPUT_COLUMN'], excel_columns=settings.get('EXCEL_COLUMNS'))
        missing = report.get('missing_hosts', [])
        if missing:
            console.print_message(f"Not found in AWX ({len(missing)}): {', '.join(missing)}", file=sys.stderr, level="warning")
        console.print_message(f"Refreshed: {report['host_count']} unique hosts\nnot found: {len(missing)}\nsaved: {path}", flush=True, level="warning" if missing else "success")
        debug_step('Odświeżanie', 'zapisano wyniki', Plik=path, Arkusz=sheet_name,
                   Żądania_API=client.request_count if client else 0, Czas_s=f'{time.perf_counter() - started:.3f}')
        return 0
    finally:
        workbook.close()


OPERATIONS = {
    "inventory": inventory_main,
    "refresh": refresh_main,
    "execute": execute_main,
    "output": output_main,
    "templates": templates.main,
}


def run(operation=None, argv=None, *, variables=None, variables_file=None):
    """Shared dispatcher and error handling for all root launchers."""
    color_token = console.set_color_mode("auto")
    try:
        # Windows pipes otherwise use the local code page. Preserve readable Polish
        # help/logs while respecting an explicitly requested PYTHONIOENCODING.
        if os.name == "nt" and "PYTHONIOENCODING" not in os.environ:
            for stream in (sys.stdout, sys.stderr):
                reconfigure = getattr(stream, "reconfigure", None)
                if callable(reconfigure):
                    reconfigure(encoding="utf-8")
        if operation is None:
            parser = argparse.ArgumentParser(
                description="AWX: eksport hostów, odświeżanie Excela, uruchamianie jobów i pobieranie logów.",
                epilog="Pomoc operacji: python -m lib.awx refresh --help. Ustawienia domyślne: awx_variable.")
            parser.add_argument("operation", choices=OPERATIONS,
                                help="inventory: nowy eksport; refresh: aktualizacja; execute: joby; output: logi; templates: wyszukiwanie szablonów.")
            arguments = list(sys.argv[1:] if argv is None else argv)
            selected = parser.parse_args(arguments[:1] if arguments and arguments[0] in OPERATIONS else arguments)
            operation = selected.operation
            argv = arguments[1:]
        if operation not in OPERATIONS:
            raise AWXError(f"Unknown AWX operation: {operation}")
        with common.launcher_configuration(operation, variables, variables_file):
            return OPERATIONS[operation](argv)
    except (AWXError, OSError) as exc:
        console.print_message(f"Error: {exc}", file=sys.stderr, level="error")
        return 1
    except KeyboardInterrupt:
        console.print_message("\nInterrupted. Check saved execution links/markers before retrying a launch.", file=sys.stderr, level="warning")
        return 130
    finally:
        console.reset_color_mode(color_token)


if __name__ == "__main__":
    raise SystemExit(run())
