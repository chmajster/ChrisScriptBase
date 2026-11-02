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

def validate_inventory_settings(settings):
    """Validate setting types and prepare output file paths."""
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
    for name in ('AWX_URL', 'AWX_TOKEN'):
        if not isinstance(settings[name], str) or not settings[name].strip():
            raise AWXError(f"{name} must be a nonempty string.")
    inventory = settings['INVENTORY']
    if not (isinstance(inventory, str) and inventory.strip() or (type(inventory) is int and inventory > 0)):
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
    timeout = settings['TIMEOUT']
    if type(timeout) not in (int, float) or timeout <= 0 or (isinstance(timeout, float) and (not math.isfinite(timeout))):
        raise AWXError('TIMEOUT must be a positive number of seconds.')
    if type(settings['PAGE_SIZE']) is not int or settings['PAGE_SIZE'] <= 0:
        raise AWXError('PAGE_SIZE must be a positive integer.')
    return settings

def read_inventory_settings(variables_file=None):
    """Read shared variables and launcher overrides, then validate inventory fields."""
    LOGGER.debug("Configuration: reading settings from %s", variables_file or common.current_variables_file())
    return validate_inventory_settings(common.read_inventory_settings(variables_file))

def apply_settings(settings):
    """Make validated settings available to the rest of the script."""
    globals().update({name: settings[name] for name in INVENTORY_DEFAULTS})


def load_connection(debug=False):
    """Load the same AWX settings and redact debug logging for every operation."""
    configure_debug(debug, AWX_TOKEN if isinstance(AWX_TOKEN, str) else "")
    connection = read_inventory_settings()
    apply_settings(connection)
    configure_debug(debug, connection["AWX_TOKEN"].strip())
    return connection


def workbook_path(input_file, connection, *preferences):
    selected = input_file or next((settings["INPUT_FILE"] for settings in preferences if settings.get("INPUT_FILE")), None)
    path = Path(selected or connection["EXCEL_OUTPUT_FILE"]).resolve()
    if path.suffix.casefold() != ".xlsx":
        raise AWXError("The input file must use the .xlsx extension.")
    return path


def open_workbook_sheet(path, preferred="Results", explicit=None):
    """One workbook loader, worksheet selection policy and cleanup path."""
    try:
        workbook = load_workbook(path, keep_links=True)
    except (OSError, ValueError, BadZipFile, ParseError, InvalidFileException) as exc:
        raise AWXError(f"Cannot read {path}: {exc}") from None
    try:
        return workbook, excel.select_sheet(workbook, preferred, explicit)
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
  OS_JOBS dobiera szablon i Job_Variables; JOB_TYPE ustawia akcję playbooka.
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
  Odświeża INVENTORY, ostype, credentiale, szablon, zmienne i akcję joba.
  Job_Link: JOB_URL, jeśli ustawiony; w przeciwnym razie url z OS_JOBS dla ostype.
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
  --host-column wskazuje własną nazwę kolumny. Pusta lista przygotowuje nagłówki.
  AWX_Status: found = znaleziony, not_found = brak w AWX, no_hostname = pusty host.

Przykłady:
  python .\awx_refresh_excel.py -file .\awx_inventory_hosts.xlsx -d
  python .\awx_refresh_excel.py -file .\hosty.xlsx --sheet Results --host-column host_limit""",
    "execute": r"""Działanie:
  Czyta hostname, Job_Link, Job_Variables i Full_Credential z Excela.
  Łączy hosty o wspólnym szablonie, zmiennych i credentialach w paczki BATCH_SIZE.
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

Wybrane hosty i URL:
  --hosts host1 host2 lub HOSTS=['host1', 'host2'] ogranicza listę wykonania.
  HOSTS=[] oznacza dotychczasowy tryb: wszystkie hosty z Excela.
  Brakujące w arkuszu hosty są pobierane z INVENTORY w AWX i dopisywane.
  Przy podaniu hostów plik może nie istnieć: skrypt utworzy arkusz z wynikami.
  --job-url lub JOB_URL nadpisuje Job_Link dla nowych wykonań wybranych hostów.
  URL wskazuje szablon UI/API na serwerze z AWX_URL, np. /job_template/7/details.
  Pusty JOB_URL zachowuje linki z Excela lub OS_JOBS dla dopisanych hostów.

Akcja i historia:
  Job_Variables to obiekt JSON, np. {"action": "install"}.
  Akcja: --job-type > kolumna Job_Type > JOB_TYPE > wartość w Job_Variables.
  Trafia do extra_vars[JOB_TYPE_VARIABLE], domyślnie action.
  Link joba zapisuje w EXECUTION_COLUMN (domyślnie exceiute_job).
  Po zakończeniu joba zapisuje Artifacts (JSON) i artifact filter (message hosta).
  Gdy WAIT_FOR_COMPLETION=False, pobierz końcowe artefakty przez awx_job_output.py.
  Istniejące wpisy są pomijane. Przy braku nowych paczek pyta w terminalu
  o ponowne uruchomienie: tak/T/y/yes potwierdza, Enter lub inna odpowiedź kończy
  bez zapisu. Poprzedni link zostaje w komentarzu komórki EXECUTION_COLUMN.
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
  python .\awx_execute_jobs.py -file .\hosty.xlsx --batch-size 10 --job-type install
  python .\awx_execute_jobs.py --hosts host1 --job-url "https://awx.example.com/#/templates/job_template/7/details" --dry-run
  --dry-run: bez startu jobów i zapisu. Brakujące metadane może odczytać z AWX.
  Zwykły --dry-run pokazuje nazwy zmiennych i maskuje wartości; bez walidacji survey.
  --survey --dry-run odczytuje i waliduje survey oraz umożliwia podanie odpowiedzi.
  --no-dry-run: wyłącza DRY_RUN ustawione w pliku lub launcherze.""",
    "output": r"""Działanie:
  Czyta linki z EXECUTION_COLUMN (domyślnie exceiute_job) i system z ostype.
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
  COLOR: auto (terminal), always (wymuszenie), never (bez kolorów).
  --color nadpisuje COLOR; --no-color wyłącza kolory. NO_COLOR wyłącza tryb auto.
  URL-e w awx_variable mogą zawierać nowe linie; parser łączy je w jeden adres.
  Dotyczy AWX_URL, JOB_URL i url w OS_JOBS; teksty w variables zachowują nowe linie.
  Zamknij aktualizowany plik w Excelu przed zapisem.
  Poza venv skrypt tworzy/używa .venv obok launcherów i restartuje się w nim.
  Brakujące openpyxl instaluje się automatycznie w venv używanego Pythona.
  Wspólna implementacja wszystkich launcherów: katalog lib/."""


def operation_parser(description, *, with_sheet=True, operation=None):
    detail = OPERATION_HELP.get(operation, "")
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=(detail + "\n\n" if detail else "") + CONFIGURATION_HELP)
    file_help = "Filtr hostów z kolumny host_limit; wynik zapisuje pod EXCEL_OUTPUT_FILE." if operation == "inventory" else "Plik .xlsx aktualizowany w miejscu; nadpisuje INPUT_FILE."
    parser.add_argument("-file", "--file", dest="input_file", type=Path, help=file_help)
    if with_sheet:
        parser.add_argument("--sheet", help="Nazwa arkusza; domyślnie SHEET_NAME lub aktywny arkusz.")
    parser.add_argument("-d", "-debug", "--debug", action="store_true", default=None, help="Szczegóły zapytań API; token jest maskowany.")
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
    LOGGER.debug('Excel input: opening %s and looking for the host_limit column', input_file)
    workbook = None
    hostnames = {}
    found_column = False
    try:
        workbook = load_workbook(input_file, read_only=True, data_only=True, keep_links=False)
        for sheet in workbook.worksheets:
            LOGGER.debug('Excel input: searching worksheet %r', sheet.title)
            column_index = None
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if column_index is None:
                    matching_columns = [index for index, value in enumerate(row) if isinstance(value, str) and value.strip().casefold() == 'host_limit']
                    if len(matching_columns) > 1:
                        raise AWXError(f"Worksheet {sheet.title!r}, row {row_number}: more than one host_limit column was found.")
                    if matching_columns:
                        column_index = matching_columns[0]
                        found_column = True
                        LOGGER.debug('Excel input: found host_limit in worksheet %r, row %d, column %s', sheet.title, row_number, get_column_letter(column_index + 1))
                    continue
                value = row[column_index] if column_index < len(row) else None
                if value is None:
                    continue
                for hostname in re.split('[,;\\s]+', str(value).strip()):
                    if hostname:
                        hostnames.setdefault(hostname, None)
    except (OSError, InvalidFileException, BadZipFile, ParseError) as exc:
        raise AWXError(f"Cannot read Excel file {input_file}: {exc}") from None
    finally:
        if workbook is not None:
            workbook.close()
    if not found_column:
        raise AWXError(f"No host_limit column was found in {input_file}.")
    if not hostnames:
        raise AWXError(f"The host_limit column in {input_file} contains no hostnames.")
    LOGGER.debug('Excel input: read %d unique hostnames', len(hostnames))
    debug_variables(f"Hostnames from {input_file}", list(hostnames))
    return list(hostnames)

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
        self.opener = build_opener(NoRedirect())
        self.request_count = 0
        LOGGER.debug('API base URL used for requests: %s', self.base_url)

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
                    LOGGER.debug('API path mapping: %s -> %s', parsed.path, new_path)
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

    def request(self, method, path, payload=None, *, response_format="json", purpose="Fetch an AWX resource"):
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
        LOGGER.debug("Request #%d purpose: %s", number, purpose)
        LOGGER.debug("Request #%d: %s %s", number, method, url)
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
            LOGGER.debug("Response #%d: HTTP %d error; elapsed %.3f s", number, code, time.perf_counter() - started)
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
        LOGGER.debug("Response #%d: HTTP %s; elapsed %.3f s", number, status, time.perf_counter() - started)
        if isinstance(result, dict) and isinstance(result.get("results"), list):
            LOGGER.debug("Request #%d result: records on page=%d; total reported by API=%s; next page=%s",
                         number, len(result["results"]), result.get("count", "not provided"), "yes" if result.get("next") else "no")
        return result

    def get(self, path, *, purpose="Fetch an AWX resource"):
        return self.request("GET", path, purpose=purpose)

    def post(self, path, payload):
        return self.request("POST", path, payload, purpose="Launch an AWX job")

    def stdout(self, job_id):
        return self.request("GET", f"jobs/{job_id}/stdout/?format=txt_download", response_format="text",
                            purpose=f"Fetch complete stdout for job {job_id}")

    def items(self, path, *, purpose='Fetch an AWX resource list', **filters):
        """Fetch all pages by following the API's next links."""
        parameters = {'page_size': PAGE_SIZE, **filters}
        debug_variables(f"Parameters: {purpose}", parameters)
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
            page = self.get(url, purpose=f"{purpose}; page {page_number}")
            if not isinstance(page.get('results'), list):
                raise AWXError('The API response is missing the results list.')
            record_count += len(page['results'])
            yield from page['results']
            next_page = page.get('next')
            url = self._resolve_url(next_page, relative_to=url) if next_page else None
        LOGGER.debug('Completed: %s; fetched %d records across %d pages', purpose, record_count, page_number)

def find_inventory(client, selector):
    if isinstance(selector, int):
        if selector < 1:
            raise AWXError('The inventory ID must be positive.')
        return client.get(f"inventories/{selector}/", purpose=f"Fetch details for inventory ID {selector}")
    if not isinstance(selector, str) or not selector.strip():
        raise AWXError('Set INVENTORY to an inventory name or numeric ID.')
    matches = list(client.items('inventories/', name=selector, purpose=f"Find inventory named {selector!r}"))
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
            debug_variables(f"Variables for group {name!r} (ID {self.groups[name]['id']})", self.variables[name])
            for hostname in group_data.get('hosts', []):
                self.direct_by_host.setdefault(hostname, set()).add(name)
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
        direct_names = self.direct_by_host.get(hostname, set())
        all_names = set()
        for name in direct_names:
            all_names.update(self.all_group_names(name))
        LOGGER.debug('Host %s: looking for variables %s locally in %d groups (direct=%d, inherited=%d)', hostname, self.credential_variables, len(all_names), len(direct_names), len(all_names - direct_names))
        matches = []
        sorted_names = sorted(all_names)
        for variable in self.credential_variables:
            previous_matches = len(matches)
            for name in sorted_names:
                if variable in self.variables[name]:
                    LOGGER.debug('Host %s: found %s in group %r (%s)', hostname, variable, name, 'direct' if name in direct_names else 'inherited')
                    matches.append({**self.groups[name], 'variable': variable, 'value': self.variables[name][variable], 'inherited': name not in direct_names})
            if len(matches) == previous_matches:
                LOGGER.debug('Host %s: variable %s was not found in its groups', hostname, variable)
        result = {'groups': [self.groups[name] for name in sorted(direct_names)], 'all_groups': [self.groups[name] for name in sorted_names], 'os_credential_groups': [{'id': match['id'], 'name': match['name'], 'os_credential': match['value'], 'inherited': match['inherited']} for match in matches if match['variable'] == 'os_credential'], 'credential_groups': matches}
        debug_variables(f"Host {hostname!r}", result)
        return result

def fetch_report(client, selector, hostnames=None, *, allow_empty=False):
    credential_variables = credential_variable_names()
    extra_credentials = extra_credential_values()
    debug_variables('Literal extra credential values from configuration', extra_credentials)
    inventory = find_inventory(client, selector)
    LOGGER.debug('Selected inventory: ID=%s; name=%r; type=%s', inventory['id'], inventory['name'], inventory.get('kind') or 'standard')
    debug_variables('Credential variables to look up', credential_variables)
    requested = list(dict.fromkeys(hostnames)) if hostnames is not None else None
    requested_names = set(requested) if requested is not None else None
    host_filters = {'order_by': 'id'}
    if requested:
        candidate = {**host_filters, 'page_size': PAGE_SIZE, 'name__in': ','.join(requested)}
        if len(urlencode(candidate)) <= 6000:
            host_filters['name__in'] = candidate['name__in']
            LOGGER.debug('Host search: sending %d names to AWX using the name__in filter', len(requested))
        else:
            LOGGER.debug('Host search: matching %d names locally after a bulk fetch', len(requested))
    else:
        LOGGER.debug('Host search: fetching all hosts from inventory ID %s (name: %r)', inventory['id'], inventory['name'])
    debug_variables('AWX host filters', host_filters)
    hosts = []
    group_indexes = {}
    host_variables = {}
    inventory_names = {inventory['id']: inventory['name']}
    host_purpose = f"Fetch hosts from inventory ID {inventory['id']} (name: {inventory['name']!r})"
    if requested is not None:
        host_purpose += f" for {len(requested)} names from the input file"
    for host in client.items(f"inventories/{inventory['id']}/hosts/", purpose=host_purpose, **host_filters):
        if requested_names is not None and host['name'] not in requested_names:
            continue
        source_id = host.get('inventory') or inventory['id']
        if source_id not in inventory_names:
            summary = (host.get('summary_fields') or {}).get('inventory') or {}
            inventory_names[source_id] = summary.get('name') or client.get(f"inventories/{source_id}/", purpose=f"Fetch source inventory name for ID {source_id}").get('name') or str(source_id)
        if source_id not in group_indexes:
            LOGGER.debug('Loading the group index for source inventory ID %s', source_id)
            groups = list(client.items(f"inventories/{source_id}/groups/", order_by='name', purpose=f"Fetch group names and IDs from source inventory ID {source_id}"))
            exported = client.get(f"inventories/{source_id}/script/?all=1&hostvars=1", purpose=f"Fetch host variables, memberships, group hierarchy, and group variables from source inventory ID {source_id}; include disabled hosts")
            host_variables[source_id] = exported.get('_meta', {}).get('hostvars', {})
            group_indexes[source_id] = InventoryGroupIndex(groups, exported, credential_variables)
            LOGGER.debug('Group index for source inventory ID %s is ready: %d groups and %d hosts with group memberships; credential lookup runs locally', source_id, len(groups), len(group_indexes[source_id].direct_by_host))
        else:
            LOGGER.debug('Host %s: reusing the cached group index for source inventory ID %s', host['name'], source_id)
        hosts.append({'id': host['id'], 'name': host['name'], 'INVENTORY': inventory_names[source_id], 'ostype': host_variables[source_id].get(host['name'], {}).get('ostype'), 'enabled': host.get('enabled', True), **group_indexes[source_id].for_host(host['name'])})
    report = {'inventory': {'id': inventory['id'], 'name': inventory['name']}, 'host_count': len(hosts), 'hosts': hosts}
    if requested is not None:
        if not hosts and (not allow_empty):
            raise AWXError(f"None of the hosts listed in the file exist in inventory {inventory['name']!r}.")
        order = {name: index for index, name in enumerate(requested)}
        hosts.sort(key=lambda host: order[host['name']])
        found_names = {host['name'] for host in hosts}
        report['missing_hosts'] = [name for name in requested if name not in found_names]
    debug_variables('Host summary', {'host_count': len(hosts), 'source_inventory_count': len(group_indexes), 'source_inventory_ids': list(group_indexes), 'missing_hosts': report.get('missing_hosts', [])})
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
                LOGGER.debug('Cannot compare credential %r for duplicates: %s', reference, exc)
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
                LOGGER.debug('Cannot compare host credential %r with template defaults: %s', reference, exc)
                continue
            host_ids.add(credential['id'])
            kind = credential.get('kind') or (credential.get('summary_fields') or {}).get('credential_type', {}).get('kind')
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


def build_inventory_rows(report, settings=None, output_column=None, *, client=None, template_cache=None, job_url_override=None):
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
        ostype = host.get('ostype')
        ostype_text = 'none' if ostype is None else ostype if isinstance(ostype, str) else json.dumps(ostype, ensure_ascii=False)
        job = OS_JOBS.get(ostype.strip().casefold(), {}) if isinstance(ostype, str) else {}
        job_link = common.job_template_url(settings, job.get('url', ''), override=job_url_override)
        try:
            template_credentials = template_credential_entries(client, job_link, template_cache)
        except AWXError as exc:
            if common.job_template_url(settings, override=job_url_override):
                source = 'JOB_URL / --job-url'
            elif isinstance(ostype, str):
                source = f"OS_JOBS[{ostype.strip().casefold()!r}]['url']"
            else:
                source = 'OS_JOBS'
            raise AWXError(client.redact(f'Host {host["name"]!r}, ostype={ostype!r}, source: {source}: {exc}')) from None
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
        full_parts = merge_full_credentials(client, extra_credentials, host_values, inherited)
        host['Extra_Credentials'] = list(extra_credentials)
        LOGGER.debug('Host %s: using %d extra credentials from %s', host['name'], len(extra_credentials),
                     'EXTRA_CREDENTIAL_VARIABLES' if configured_extras else 'job template')
        job_variables = deepcopy(job.get('variables', {}))
        host['Job_Link'] = job_link
        host['Job_Variables'] = job_variables
        host['INVENTORY'] = host.get('INVENTORY') or report.get('inventory', {}).get('name') or 'none'
        host['ApplicationID'] = host['INVENTORY'].split('_', 1)[0].strip() or 'none'
        credentials = list(dict.fromkeys((value for variable in primary_variables for value in values_by_variable.get(variable, []))))
        job_type = common.apply_job_type(job_variables, None, settings)
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
        debug_variables(f"Full_Credential for host {host['name']!r}", full_credential)
        debug_variables(f"HOPS for host {host['name']!r}", host['HOPS'])
        values = {'hostname': host['name'], 'INVENTORY': host['INVENTORY'], 'ApplicationID': host['ApplicationID'], 'ostype': ostype_text, 'Job_Link': job_link or 'none', 'Job_Credentials': FULL_CREDENTIAL_SEPARATOR.join(host['Job_Credentials']) or 'none', 'Job_Variables': json.dumps(job_variables, ensure_ascii=False), 'HOPS': host['HOPS'], 'HOPS_COUNT': host['HOPS_COUNT'], 'jumphost_for_hops': host['jumphost_for_hops'], 'Extra_Credentials': extra_text, 'OS Credential': FULL_CREDENTIAL_SEPARATOR.join(credentials) or 'none', 'Full_Credential': full_credential, 'Job_Type': job_type, 'AWX_Status': 'found'}
        rows.append([values.get(name, '') for name in headers])
    return (headers, rows)

def save_inventory_excel(headers, rows, output_file, settings=None, output_column=None, client=None, template_cache=None):
    """Create the same complete, styled workbook used by every AWX script."""
    settings = common.read_execution_settings() if settings is None else settings
    output_column = common.read_output_settings()['OUTPUT_COLUMN'] if output_column is None else output_column
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
        excel.save_workbook(workbook, output_file, execution_column=settings['EXECUTION_COLUMN'], output_column=output_column)
    finally:
        workbook.close()

def inventory_main(argv=None):
    args = parse_inventory_arguments(argv)
    configure_debug(args.debug, AWX_TOKEN if isinstance(AWX_TOKEN, str) else '')
    client = None
    try:
        settings = load_connection(args.debug)
        token = AWX_TOKEN.strip()
        debug_variables('Configuration', {'AWX_URL': AWX_URL, 'VARIABLES_FILE': VARIABLES_FILE, 'AWX_TOKEN': '***', 'INVENTORY': INVENTORY, 'CREDENTIAL_VARIABLES': CREDENTIAL_VARIABLES, 'EXTRA_CREDENTIAL_VARIABLES': EXTRA_CREDENTIAL_VARIABLES, 'FULL_CREDENTIAL_SEPARATOR': FULL_CREDENTIAL_SEPARATOR, 'OUTPUT_FILE': OUTPUT_FILE, 'EXCEL_OUTPUT_FILE': EXCEL_OUTPUT_FILE, 'TIMEOUT': TIMEOUT, 'PAGE_SIZE': PAGE_SIZE, 'input_file': args.input_file})
        hostnames = None
        input_file = args.input_file or common.read_execution_settings()["INPUT_FILE"]
        if input_file is not None:
            input_file = Path(input_file)
            if input_file.resolve() == EXCEL_OUTPUT_FILE.resolve():
                raise AWXError('The input and output Excel files must use different paths. Change EXCEL_OUTPUT_FILE in the configuration.')
            hostnames = read_host_limits(input_file)
        client = AWXClient(AWX_URL, token)
        report = fetch_report(client, INVENTORY, hostnames=hostnames)
        missing = report.get('missing_hosts', [])
        if missing:
            details = ', '.join(missing[:20])
            if len(missing) > 20:
                details += f" ... (+{len(missing) - 20})"
            console.print_message(f"Missing hosts ({len(missing)}): {details}", file=sys.stderr, level="warning")
        workflow = common.read_execution_settings()
        output_column = common.read_output_settings()['OUTPUT_COLUMN']
        template_cache = {}
        headers, rows = build_inventory_rows(report, workflow, output_column, client=client, template_cache=template_cache)
        for row in rows:
            console.print_message(' , '.join((f"{header} : {value}" for header, value in zip(headers, row))), level="data")
        sys.stdout.flush()
        LOGGER.debug('JSON export: %d hosts, file %s', report['host_count'], OUTPUT_FILE)
        OUTPUT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        save_inventory_excel(headers, rows, EXCEL_OUTPUT_FILE, workflow, output_column, client=client, template_cache=template_cache)
        debug_variables('Saved results', {'json': OUTPUT_FILE.resolve(), 'excel': EXCEL_OUTPUT_FILE.resolve(), 'rows': len(rows)})
        return 0
    finally:
        LOGGER.debug('Total API requests: %d', client.request_count if client is not None else 0)


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
    parser.add_argument("--survey", action=argparse.BooleanOptionalAction, default=None,
                        help="Pytaj w terminalu o brakujące odpowiedzi survey; --no-survey wyłącza pytania.")
    parser.add_argument("--survey-answers", help="Odpowiedzi survey jako obiekt JSON; nadpisuje SURVEY_ANSWERS i Job_Variables.")
    parser.add_argument("--show-survey", action="store_true", help="Pokaż pytania/defaulty survey; bez startu jobów i zapisu Excela.")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=None, help="Podgląd paczek; --no-dry-run wyłącza podgląd ustawiony w zmiennych.")
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

def parse_variables(value, row_number):
    if value is None or (isinstance(value, str) and value.strip().casefold() in ('', 'none')):
        return {}
    try:
        variables = json.loads(value)
        if not isinstance(variables, dict):
            raise ValueError('not a dictionary')
        json.dumps(variables, allow_nan=False)
    except (TypeError, ValueError):
        raise AWXError(f'Row {row_number}: Job_Variables must be a JSON object, e.g. {{"action": "check"}}.') from None
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

def build_execution_plan(sheet, client, settings, job_type_override=None, hostnames=None, job_url_override=None, survey_overrides=None, survey_cache=None):
    """Validate the whole sheet, deduplicate hosts, and batch equal launch payloads."""
    columns = excel.read_columns(sheet)
    for name in ('hostname', 'job_link', 'job_variables', 'full_credential'):
        if name not in columns:
            raise AWXError(f"Missing {name} column. Generate a new Excel export with awx_inventory_hosts.py.")
    execution_column = columns.get(settings['EXECUTION_COLUMN'].casefold(), sheet.max_column + 1)
    hosts = {}
    selected = set(hostnames) if hostnames is not None else None
    skipped_without_job = 0
    selected_job_url = common.job_template_url(settings, override=job_url_override)
    for row_number in range(2, sheet.max_row + 1):
        host_value = sheet.cell(row_number, columns['hostname']).value
        if host_value is None or not str(host_value).strip():
            continue
        hostname = str(host_value).strip()
        if selected is not None and hostname not in selected:
            continue
        if not HOSTNAME_PATTERN.fullmatch(hostname):
            raise AWXError(f"Row {row_number}: hostname must be a single literal hostname, without Ansible patterns.")
        link_cell = sheet.cell(row_number, columns['job_link'])
        link = common.job_template_url(settings, str(link_cell.value or ''), override=job_url_override)
        if link.casefold() in ('', 'none'):
            skipped_without_job += 1
            continue
        if not selected_job_url and link_cell.data_type == 'f':
            raise AWXError(f"Row {row_number}: Job_Link must contain a URL rather than an Excel formula.")
        if not selected_job_url and link_cell.hyperlink and link_cell.hyperlink.target:
            link = link_cell.hyperlink.target
        template_id = template_id_from_row(link, client, sheet, row_number, override=bool(selected_job_url))
        variables = deepcopy(settings.get('SURVEY_ANSWERS', {}))
        variables.update(parse_variables(sheet.cell(row_number, columns['job_variables']).value, row_number))
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
        # Survey cells override saved/configured job types; the explicit CLI action wins.
        action_override = job_type_override
        action_variable = settings['JOB_TYPE_VARIABLE']
        if action_override is None and (action_variable in answers or action_variable in (survey_overrides or {})):
            action_override = variables[action_variable]
            if not isinstance(action_override, str):
                raise AWXError(f"Job_Variables[{action_variable!r}] must be a string for the job type.")
        job_type = apply_job_type(variables, job_type_cell.value if job_type_cell else None, settings, action_override)
        base_refs, jump_refs = synchronise_full_credential(sheet, row_number)
        credential_refs = tuple(sorted(set(base_refs + jump_refs)))
        preserve_defaults = bool(jump_refs and not base_refs)
        key = (template_id, json.dumps(variables, sort_keys=True, ensure_ascii=False), credential_refs, preserve_defaults)
        execution = str(sheet.cell(row_number, execution_column).value or '').strip()
        if hostname not in hosts:
            hosts[hostname] = {'key': key, 'template_id': template_id, 'link': link, 'variables': variables, 'credential_refs': credential_refs, 'preserve_template_credentials': preserve_defaults, 'job_type': job_type, 'hostname': hostname, 'rows': [], 'execution': ''}
        host = hosts[hostname]
        if host['key'] != key:
            raise AWXError(f"Row {row_number}: duplicate host {hostname!r} has different job or variables or credentials.")
        if execution:
            if host['execution'] and host['execution'] != execution:
                raise AWXError(f"Host {hostname!r} has conflicting execution links/markers; check its rows.")
            host['execution'] = execution
        host['rows'].append(row_number)
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
            batches.append({'template_id': entries[0]['template_id'], 'link': entries[0]['link'], 'variables': entries[0]['variables'], 'entries': entries, 'credential_refs': entries[0]['credential_refs'], 'preserve_template_credentials': entries[0]['preserve_template_credentials'], 'job_type': entries[0]['job_type'], 'limit': ','.join((host['hostname'] for host in entries)), 'job_url_overridden': bool(selected_job_url)})
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
    if reference not in cache:
        if re.fullmatch('[0-9]+', reference):
            credential = client.get(f"credentials/{int(reference)}/", purpose=f"Resolve credential ID {reference}")
        else:
            matches = list(client.items('credentials/', name=reference, order_by='id', purpose=f"Resolve credential named {reference!r}"))
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
    kind = credential.get('kind') or (credential.get('summary_fields') or {}).get('credential_type', {}).get('kind')
    return (credential.get('credential_type'),
            (credential.get('inputs') or {}).get('vault_id') if kind == 'vault' else None)


def resolve_credentials(client, batches):
    """Resolve job credential references into IDs required by the launch API.

    AWX_TOKEN authenticates these requests. Full_Credential selects the stored
    credentials used by the job/playbook; it never authenticates the API client.
    """
    template_cache = {}
    for batch in batches:
        selected = {}
        inherited = {}
        if batch.get('preserve_template_credentials'):
            for _, credential in template_credential_entries(client, batch['link'], template_cache):
                inherited[credential['id']] = lookup_credential(client, str(credential['id']))
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

def template_survey(client, template_id, cache):
    if template_id not in cache:
        details = client.get(f"job_templates/{template_id}/launch/", purpose=f"Check launch settings for template {template_id}")
        questions = []
        if details.get('survey_enabled'):
            spec = client.get(f"job_templates/{template_id}/survey_spec/", purpose=f"Read survey for template {template_id}")
            questions = survey.read_questions(spec, template_id)
        cache[template_id] = (details, questions, {})
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
    if added:
        console.print_message('Added survey columns: ' + ', '.join(added), level='info')
    return cache


def validate_templates(client, batches, *, interactive=False, check_credentials=True, cache=None):
    """Check prompting and required inputs before launching the first batch."""
    cache = {} if cache is None else cache
    for batch in batches:
        template_id = batch['template_id']
        details, questions, prompted = template_survey(client, template_id, cache)
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


def show_template_surveys(client, links):
    for template_id in links:
        details = client.get(f"job_templates/{template_id}/launch/", purpose=f"Read survey state for template {template_id}")
        spec = client.get(f"job_templates/{template_id}/survey_spec/", purpose=f"Read survey questions for template {template_id}")
        survey.display_questions(template_id, details.get('survey_enabled', False), survey.read_questions(spec or {'spec': []}, template_id))

def job_url(template_link, client, job_id):
    parsed = urlsplit(template_link)
    route = f"/jobs/playbook/{job_id}/output"
    if parsed.fragment:
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', route))
    if '/templates/' in parsed.path:
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.split('/templates/', 1)[0] + route, '', ''))
    return client._resolve_url(f"jobs/{job_id}/")

def wait_for_job(client, job_id, settings, *, require_success=True):
    """One status polling loop for execution and collection of successful/failed job logs."""
    deadline = time.monotonic() + settings["JOB_WAIT_TIMEOUT"]
    last_status = None
    while True:
        job = client.get(f"jobs/{job_id}/", purpose=f"Read status and metadata for job {job_id}")
        status = job.get("status")
        if status != last_status:
            console.print_message(f"Job {job_id}: {status}", flush=True, level=console.job_status_level(status))
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
        time.sleep(min(settings["POLL_INTERVAL"], remaining))

def execute_plan(workbook, sheet, path, client, settings, batches, existing, execution_column):
    columns = excel.read_columns(sheet)
    job_type_column = columns['job_type']
    output_column = settings.get('OUTPUT_COLUMN', 'output')
    for entry in existing:
        write_execution(sheet, execution_column, [entry], entry['execution'])
    for batch in batches:
        for entry in batch['entries']:
            for row_number in entry['rows']:
                synchronise_full_credential(sheet, row_number, client=client)
    excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=output_column)
    for index, batch in enumerate(batches, start=1):
        template_id = batch['template_id']
        console.print_message(f"Batch {index}/{len(batches)}: template={template_id}; job_type={batch['job_type'] or 'none'}; credentials={batch['credentials']}; hosts={batch['limit']}", flush=True, level="progress")
        for entry in batch['entries']:
            for row_number in entry['rows']:
                cell = sheet.cell(row_number, job_type_column)
                cell.value = batch['job_type']
                cell.data_type = 's'
                cell.number_format = '@'
                if batch.get('job_url_overridden'):
                    template_cell = sheet.cell(row_number, columns['job_link'])
                    template_cell.value = batch['link']
                    template_cell.data_type = 's'
                    template_cell.hyperlink = batch['link']
        marker = f"LAUNCH_PENDING template={template_id}; {datetime.now(timezone.utc).isoformat()}"
        write_execution(sheet, execution_column, batch['entries'], marker)
        excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=output_column)
        try:
            payload = {'limit': batch['limit'], 'extra_vars': batch['variables']}
            if batch['credentials']:
                payload['credentials'] = batch['credentials']
            result = client.post(f"job_templates/{template_id}/launch/", payload)
            job_id = result.get('job') or result.get('id')
            if type(job_id) is not int or job_id < 1:
                raise AWXError('Launch response has no job ID; check AWX before clearing the Excel marker.')
            link = job_url(batch['link'], client, job_id)
            console.print_message(f"Job {job_id}: {link}", flush=True, level="success")
            write_execution(sheet, execution_column, batch['entries'], link)
            excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=output_column)
            if result.get('ignored_fields'):
                raise AWXError(f"Job {job_id}: AWX ignored launch fields {', '.join(result['ignored_fields'])}; stopped further launches.")
            if settings['WAIT_FOR_COMPLETION']:
                job = wait_for_job(client, job_id, settings, require_success=False)
                write_job_artifacts(sheet, job, batch['entries'], client._resolve_url(f"jobs/{job_id}/"))
                excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=output_column)
                if job['status'] != 'successful':
                    raise AWXError(f"Job {job_id} finished with status {job['status']}; remaining batches were not launched.")
            else:
                write_job_artifacts(sheet, result, batch['entries'], client._resolve_url(f"jobs/{job_id}/"))
                excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=output_column)
        except BaseException:
            console.print_message('Stopped. Check the saved execution links/markers and AWX before retrying the affected batch.', file=sys.stderr, level="warning")
            raise

def add_selected_hosts(sheet, hostnames, client, connection, settings, *, job_url_override=None, template_cache=None):
    """Populate only selected hosts missing from Excel, using the existing AWX lookup."""
    columns = excel.read_columns(sheet)
    existing = {str(sheet.cell(row, columns['hostname']).value or '').strip()
                for row in range(2, sheet.max_row + 1)}
    missing = [hostname for hostname in hostnames if hostname not in existing]
    if not missing:
        return
    console.print_message(f"Fetching AWX metadata for {len(missing)} selected hosts missing from Excel.", flush=True, level="info")
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


def confirm_job_rerun(sheet, execution_column, entries):
    """Confirm a repeat interactively; changes stay in memory until preflight passes."""
    if not sys.stdin.isatty():
        return False
    try:
        answer = input('Czy potwierdzasz ponowne uruchomienie joba? [tak/N]: ')
    except EOFError:
        return False
    if answer.strip().casefold() not in ('tak', 't', 'yes', 'y'):
        return False
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
    return True


def execute_main(argv=None):
    args = parse_execute_arguments(argv)
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
    selected_job_url = common.job_template_url(settings, override=args.job_url)
    path = workbook_path(args.input_file, connection, settings)
    client = AWXClient(connection['AWX_URL'], connection['AWX_TOKEN'].strip())
    template_cache = {}
    if selected_job_url:
        try:
            template_id = template_id_from_link(selected_job_url, client)
        except AWXError as exc:
            raise AWXError(f'JOB_URL / --job-url: {exc}') from None
        if args.show_survey:
            show_template_surveys(client, {template_id: selected_job_url})
            return 0
    if path.exists() or not selected_hosts:
        workbook, sheet = open_workbook_sheet(path, settings["SHEET_NAME"], args.sheet)
    else:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = args.sheet or settings['SHEET_NAME']
    try:
        excel.prepare_sheet(sheet, settings['EXECUTION_COLUMN'], settings['OUTPUT_COLUMN'])
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
        console.print_message(f"Input: {path}; batch size: {settings['BATCH_SIZE']}; batches: {len(batches)}; already recorded hosts: {len(existing)}; rows without job: {skipped}", flush=True, level="info")
        if args.dry_run:
            if interactive_survey:
                rows = [row for batch in batches for entry in batch['entries'] for row in entry['rows']]
                cache = update_survey_columns(client, sheet, settings, job_url_override=selected_job_url,
                                              row_numbers=rows, cache=template_cache)
                batches, existing, column, skipped = build_execution_plan(
                    sheet, client, settings, job_type_override=args.job_type, hostnames=selected_hosts or None,
                    job_url_override=selected_job_url or None, survey_overrides=survey_overrides, survey_cache=cache)
                validate_templates(client, batches, interactive=True, check_credentials=False, cache=cache)
            for index, batch in enumerate(batches, start=1):
                # No schema is fetched for an ordinary dry run, so mask all answer values.
                preview = {key: '***' for key in batch['variables']}
                console.print_message(f"Batch {index}: template={batch['template_id']}; limit={batch['limit']}; credentials={list(batch['credential_refs'])}; job_type={batch['job_type'] or 'none'}; extra_vars={json.dumps(preview, ensure_ascii=False)}", level="progress")
            console.print_message('Dry run: no jobs launched and no workbook changes.', level="warning")
            return 0
        if any((entry['execution'].startswith('LAUNCH_PENDING') for entry in existing)):
            raise AWXError('An earlier launch has a LAUNCH_PENDING marker. Check AWX, then replace it with the job link or clear it only if no job was launched.')
        rerun_entries = []
        if not batches and existing:
            if not confirm_job_rerun(sheet, column, existing):
                console.print_message('Ponowne uruchomienie anulowane.', level='warning')
                return 0
            rerun_entries = existing
            batches, existing, column, skipped = build_execution_plan(
                sheet, client, settings, job_type_override=args.job_type, hostnames=selected_hosts or None,
                job_url_override=selected_job_url or None, survey_overrides=survey_overrides)
        if not batches:
            excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=settings['OUTPUT_COLUMN'])
            console.print_message('No new batches to launch.', level="warning")
            return 0
        rows = [row for batch in batches for entry in batch['entries'] for row in entry['rows']]
        cache = update_survey_columns(client, sheet, settings, job_url_override=selected_job_url,
                                      row_numbers=rows, cache=template_cache)
        batches, existing, column, skipped = build_execution_plan(
            sheet, client, settings, job_type_override=args.job_type, hostnames=selected_hosts or None,
            job_url_override=selected_job_url or None, survey_overrides=survey_overrides, survey_cache=cache)
        resolve_credentials(client, batches)
        validate_templates(client, batches, interactive=interactive_survey, cache=cache)
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
                wait_for_job(client, job_id, settings, require_success=recorded_jobs[job_id])
        execute_plan(workbook, sheet, path, client, settings, batches, existing, column)
        console.print_message(f"Saved job links to {settings['EXECUTION_COLUMN']} in {path}.", level="success")
        return 0
    finally:
        workbook.close()


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
    columns = excel.read_columns(sheet)
    execution_name = settings['EXECUTION_COLUMN'].strip().casefold()
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
            details = client.get(f"inventories/{inventory_id}/", purpose=f"Read inventory name for ID {inventory_id}")
            cache[inventory_id] = str(details.get('name') or inventory_id)
        except AWXError:
            LOGGER.debug('Inventory %s name is unavailable; using its ID in filenames', inventory_id)
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
    excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=settings['OUTPUT_COLUMN'])
    output_directory.mkdir(parents=True, exist_ok=True)
    inventory_names = {}
    failures = 0
    completed = 0
    for job_id, entries in jobs.items():
        artifacts_written = False
        try:
            job = read_job(client, job_id, settings)
            name = filename_part(job_inventory_name(client, job, inventory_names))
            write_job_artifacts(sheet, job, entries, client._resolve_url(f"jobs/{job_id}/"))
            artifacts_written = True
            content = client.stdout(job_id)
            files = {}
            for entry in entries:
                os_name = filename_part(entry['ostype'])
                if os_name not in files:
                    text_path = output_directory / f"{job_id}.{name}.{os_name}.txt"
                    save_text(text_path, content)
                    files[os_name] = text_path
                    console.print_message(f"Job {job_id}: {text_path}", flush=True, level="success")
            for entry in entries:
                write_output(sheet, column, entry['row'], content, files[filename_part(entry['ostype'])])
            completed += 1
        except AWXError as exc:
            failures += 1
            console.print_message(f"Error: {exc}", file=sys.stderr, level="error")
        finally:
            if artifacts_written:
                excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=settings['OUTPUT_COLUMN'])
    console.print_message(f"Collected: {completed} jobs; errors: {failures}; workbook: {path}", flush=True, level="error" if failures else "success")
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
    workbook, sheet = open_workbook_sheet(path, settings["SHEET_NAME"], args.sheet)
    try:
        excel.prepare_sheet(sheet, settings['EXECUTION_COLUMN'], settings['OUTPUT_COLUMN'])
        excel.sort_host_rows(sheet)
        jobs, column, skipped = build_output_plan(sheet, client, settings)
        console.print_message(f"Input: {path}; unique jobs: {len(jobs)}; rows without a job link: {skipped}", flush=True, level="info")
        if not jobs:
            excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=settings['OUTPUT_COLUMN'])
            console.print_message('No job output to collect.', level="warning")
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
        hostname = str(cell.value).strip()
        if cell.data_type == 'f' or not HOSTNAME_PATTERN.fullmatch(hostname):
            raise AWXError(f"Row {row_number}: the host column must contain one literal hostname per cell.")
        rows[row_number] = hostname
    return (rows, columns, columns[selected])

def metadata_by_host(report, settings, *, client=None, template_cache=None):
    """Use exactly the same host records as the inventory generator."""
    headers, rows = build_inventory_rows(report, settings, settings.get('OUTPUT_COLUMN', 'output'),
                                        client=client, template_cache=template_cache)
    metadata = {}
    for row in rows:
        record = dict(zip(headers, row))
        hostname = record['hostname']
        metadata[hostname] = {name: record[name] for name in excel.METADATA_COLUMNS if name != 'hostname'}
    return (metadata, list(excel.METADATA_COLUMNS[1:]))

def refresh_sheet(sheet, host_rows, columns, source_column, metadata, headers, execution_column='exceiute_job', preserve_unidentified_rows=False, output_column='output'):
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
            values = {'hostname': hostname, **metadata.get(hostname, missing_values)}
        elif preserve_unidentified_rows:
            continue
        elif any((cell.value is not None and str(cell.value).strip() for cell in sheet[row_number])):
            values = {name: '' for name in ['hostname', *headers]}
            values['AWX_Status'] = 'no_hostname'
        else:
            continue
        for name, value in values.items():
            cell = sheet.cell(row_number, columns[name.casefold()])
            if (name == 'jumphost_for_hops' and row_number in host_rows and host_rows[row_number] in metadata
                    and previous_context == (values.get('ApplicationID'), values.get('HOPS_COUNT'))
                    and cell.value is not None and str(cell.value).strip()):
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
    args = parse_refresh_arguments(argv)
    connection = load_connection(args.debug)
    settings = read_execution_settings()
    settings['OUTPUT_COLUMN'] = common.read_output_settings()['OUTPUT_COLUMN']
    path = workbook_path(args.input_file, connection, settings)

    workbook, sheet = open_workbook_sheet(path, settings["SHEET_NAME"], args.sheet)
    try:
        sheet_name = sheet.title
        had_host_column = any((str(cell.value or '').strip().casefold() in HOST_COLUMNS for cell in sheet[1])) or args.host_column is not None
        host_rows, columns, source_column = read_host_rows(sheet, args.host_column)
        hostnames = list(dict.fromkeys(host_rows.values()))
        console.print_message(f"Input: {path}; worksheet: {sheet_name}; unique hosts: {len(hostnames)}", flush=True, level="info")
        client = None
        template_cache = {}
        if hostnames:
            client = AWXClient(connection['AWX_URL'], connection['AWX_TOKEN'].strip())
            report = fetch_report(client, connection['INVENTORY'], hostnames=hostnames, allow_empty=True)
        else:
            console.print_message('No hostnames in Excel. Updating column headers without querying AWX.', flush=True, level="warning")
            report = {'hosts': [], 'host_count': 0, 'missing_hosts': []}
        metadata, headers = metadata_by_host(report, settings, client=client, template_cache=template_cache)
        protected = {settings['EXECUTION_COLUMN'].casefold(), 'exceiute_job', settings['OUTPUT_COLUMN'].casefold()}
        managed = {name.casefold() for name in ['hostname', *headers]}
        if protected & managed or columns.get(settings['EXECUTION_COLUMN'].casefold()) == source_column or columns.get(settings['OUTPUT_COLUMN'].casefold()) == source_column:
            raise AWXError('The host or metadata column conflicts with a job history column.')
        refresh_sheet(sheet, host_rows, columns, source_column, metadata, headers, execution_column=settings['EXECUTION_COLUMN'], preserve_unidentified_rows=not had_host_column, output_column=settings['OUTPUT_COLUMN'])
        if hostnames:
            update_survey_columns(client, sheet, settings, overwrite_configured=True, cache=template_cache)
            jumphosts.populate_choices(sheet, client, FULL_CREDENTIAL_SEPARATOR)
        excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=settings['OUTPUT_COLUMN'])
        missing = report.get('missing_hosts', [])
        if missing:
            console.print_message(f"Not found in AWX ({len(missing)}): {', '.join(missing)}", file=sys.stderr, level="warning")
        console.print_message(f"Refreshed: {report['host_count']} unique hosts; not found: {len(missing)}; saved: {path}", flush=True, level="warning" if missing else "success")
        return 0
    finally:
        workbook.close()


OPERATIONS = {
    "inventory": inventory_main,
    "refresh": refresh_main,
    "execute": execute_main,
    "output": output_main,
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
                                help="inventory: nowy eksport; refresh: aktualizacja; execute: joby; output: logi.")
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
