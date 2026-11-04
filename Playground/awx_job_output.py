#!/usr/bin/env python3
r"""Pobieranie logów jobów AWX do Excela i plików TXT.

Przykłady:
    python .\awx_job_output.py
    python .\awx_job_output.py -file .\hosty.xlsx --output-dir .\output -d
    python .\awx_job_output.py --help

Przed odczytem Excela i zapytaniami do AWX skrypt sprawdza, czy plik jest dostępny
do zapisu. Otwarty/zablokowany plik zatrzymuje operację z podaniem ścieżki i prośbą
o zamknięcie go w Excelu. Sprawdzenie działa również bez debug i nie zmienia danych.

Skrypt odczytuje linki z EXECUTION_COLUMN (domyślnie exceiute_job_link) i system
z ostype. Pobiera pełny stdout każdego unikalnego joba i wpisuje go do OUTPUT_COLUMN
we wszystkich powiązanych wierszach. Puste linki i LAUNCH_PENDING są pomijane.
Odczytuje ID z linków WebUI /excecution/jobs/playbook/<ID>/output, starszych
/#/job/playbook/<ID>/output i /#/jobs/playbook/<ID>/output oraz API /api/v2/jobs/<ID>/.
Adres bez protokołu (np. awx.example.com/excecution/jobs/playbook/123/output) korzysta
z protokołu AWX_URL. Serwer, port i protokół muszą odpowiadać AWX_URL.
Nieprawidłowy link (np. sam adres AWX bez ID joba) powoduje ostrzeżenie z nazwą
arkusza i kolumny, numerem wiersza, adresem komórki, jej dokładną wartością,
celem hiperłącza i powodem pominięcia. Komunikat wskazuje źródło sprawdzanego
adresu i oczekiwany format linku. Tokeny są maskowane. Skrypt pomija ten wiersz
i pobiera wyniki dla pozostałych poprawnych linków, zachowując dotychczasowe
logi i artefakty pominiętego wiersza.

Artifacts zawiera JSON z pola artifacts joba. Sąsiednia kolumna artifact filter
zawiera message znalezione dla hostname z tego wiersza. Skrypt przeszukuje
zagnieżdżone słowniki i listy: host jako klucz JSON lub wartość pola host,
hostname albo inventory_hostname. Dopasowanie pełnej nazwy ignoruje wielkość
liter. Kilka komunikatów łączy nowymi liniami; brak dopasowania daje puste pole.
Bezpośrednio za artifact filter jest artifact status: completed, jeśli pole
zawiera całe słowo completed (bez rozróżniania wielkości liter), inaczej error.
Puste pole oznacza error. Status jest uzupełniany również dla starszych wierszy.
W awx_execute_jobs.py opcja 3 / --execution-scope errors uruchamia tylko te wpisy.
Artefakty są aktualizowane również wtedy, gdy pobranie stdout się nie powiedzie.

Pełny log zapisuje również jako <id_joba>.<inventory>.<os>.txt w OUTPUT_DIRECTORY.
Inventory pochodzi z danych joba AWX, a OS z Excela. Komórka Excela mieści do
32767 znaków; dłuższy tekst jest skracany z podaniem adresu pełnego pliku TXT.
Kolejne pobranie aktualizuje logi tych samych jobów, także nieudanych lub anulowanych.
Artefakty przekraczające limit komórki są skracane z adresem pełnych danych w AWX.

Komunikaty opisują odczyt arkusza, liczbę zadań i powiązanych wierszy,
status każdego zadania, pobranie stdout, zapis TXT oraz aktualizację Excela.
Podsumowanie rozróżnia liczbę pobranych logów od błędów ich pobierania:
log zadania ze statusem failed także może zostać poprawnie pobrany.
Opcja -d dodaje cele żądań API, adresy, kody HTTP, czas odpowiedzi,
powody pomijania wierszy i informacje o widocznych kolumnach.
WAIT_FOR_COMPLETION=False pobiera log dostępny teraz; True czeka na koniec
zadania według POLL_INTERVAL i JOB_WAIT_TIMEOUT.

Domyślne ustawienia: awx_variable. Nadpisania tego launchera: VARIABLES poniżej.
Argumenty komendy mają pierwszeństwo. Implementacja znajduje się w lib/.
"""

from lib.awx_dependencies import ensure_virtual_environment

if __name__ == "__main__":
    ensure_virtual_environment()

from lib.awx import run


# Odkomentuj potrzebne nadpisania. Puste VARIABLES = {} korzysta z awx_variable.
# Kolejność: komenda > VARIABLES > OUTPUT_OVERRIDES > wspólne ustawienia z pliku.
VARIABLES = {
    # Ten sam plik .xlsx, do którego execute zapisał linki uruchomień.
    # "INPUT_FILE": 'hosts.xlsx',
    # "SHEET_NAME": 'Results',
    # Widoczne kolumny; [] pokazuje wszystkie. Pozostałe są ukrywane z zachowaniem danych.
    # "EXCEL_COLUMNS": ['hostname', 'exceiute_job_link', 'output', 'Artifacts', 'artifact filter', 'artifact status'],
    # Nazwa musi odpowiadać kolumnie używanej przez launcher execute.
    # "EXECUTION_COLUMN": 'exceiute_job_link',

    # Kolumna z treścią logu; zostanie dodana, jeśli jej brak.
    # "OUTPUT_COLUMN": 'output',
    # Katalog pełnych logów TXT. Ścieżki względne liczą się od katalogu uruchomienia.
    # "OUTPUT_DIRECTORY": 'output',

    # True: oczekiwanie na koniec joba, również failed/error/canceled, i pobranie logu.
    # False: pobranie tekstu dostępnego teraz; ponowne uruchomienie odświeży wynik.
    # "WAIT_FOR_COMPLETION": True,
    # Odstęp sprawdzania statusu i limit oczekiwania, w sekundach.
    # "POLL_INTERVAL": 5,
    # "JOB_WAIT_TIMEOUT": 3600,
    # Limit pojedynczego zapytania HTTP, niezależny od czasu trwania joba.
    # "TIMEOUT": 30,
    # Szczegóły pobierania. Nie uruchamia nowych jobów.
    # "DEBUG": True,
    # Kolory: auto w terminalu, always wymusza ANSI, never wy??cza kolory.
    # "COLOR": 'auto',
}


if __name__ == "__main__":
    # Wspólny kod pobiera każdy job raz, a wynik przypisuje do wszystkich jego wierszy.
    raise SystemExit(run("output", variables=VARIABLES))
