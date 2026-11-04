"""One Excel schema, layout, sorting rule and atomic writer for all AWX scripts."""

import logging
import math
import os
import re
import tempfile
from copy import copy
from datetime import datetime
from pathlib import Path

from .awx_config import ARTIFACT_COLUMNS, AWXError, METADATA_COLUMNS, normalize_excel_columns
from .awx_dependencies import ensure_openpyxl

ensure_openpyxl()

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.filters import AutoFilter
from openpyxl.worksheet.table import TableColumn


HOST_COLUMNS = ("hostname", "host_limit", "host", "hosts")
OS_ORDER = {"windows": 0, "linux": 1, "aix": 2}
LOGGER = logging.getLogger('awx_inventory_hosts')
SETTINGS_SHEET = "Ustawienia"


def add_settings_sheet(workbook, sections, *, sensitive_keys=(), secrets=()):
    """Add an informational snapshot without copying tokens or password values."""
    if any(name.casefold() == SETTINGS_SHEET.casefold() for name in workbook.sheetnames):
        raise AWXError('Nazwa arkusza "Ustawienia" jest zarezerwowana dla parametrów eksportu. Zmień SHEET_NAME.')
    hidden_keys = {str(name).casefold() for name in sensitive_keys}
    secret_values = tuple(value for value in secrets if isinstance(value, str) and value)
    sensitive_name = re.compile(r'password|passwd|passphrase|token|secret|private[_-]?key|api[_-]?key', re.IGNORECASE)
    sheet = workbook.create_sheet(SETTINGS_SHEET, 1)
    sheet.sheet_properties.tabColor = "1F3A5F"
    sheet.sheet_view.showGridLines = False
    sheet.merge_cells("A1:C1")
    sheet["A1"] = "Ustawienia eksportu AWX"
    sheet["A1"].font = Font(name="Arial", size=15, bold=True, color="1F3A5F")
    sheet.row_dimensions[1].height = 30
    sheet.merge_cells("A2:C2")
    sheet["A2"] = "Zapis informacyjny parametrów użytych podczas eksportu. Edycja tego arkusza nie zmienia konfiguracji skryptów."
    sheet["A2"].font = Font(name="Arial", size=10, color="475569")
    sheet["A2"].alignment = Alignment(vertical="center", wrap_text=True)
    sheet.row_dimensions[2].height = 30
    sheet.append([])
    sheet.append(["Sekcja", "Parametr", "Wartość"])

    def append_values(section, values, prefix=""):
        for name, value in values.items():
            name = str(name)
            if name.upper() == "AWX_TOKEN":
                continue
            parameter = f"{prefix}.{name}" if prefix else name
            if name.casefold() in hidden_keys or sensitive_name.search(name):
                value = "***"
            elif isinstance(value, dict) and value:
                append_values(section, value, parameter)
                continue
            elif isinstance(value, (list, tuple)) and value:
                append_values(section, {f"[{index}]": item for index, item in enumerate(value, 1)}, parameter)
                continue
            if isinstance(value, Path):
                value = str(value)
            elif value is None:
                value = "None"
            elif isinstance(value, (dict, list, tuple)):
                value = "{}" if isinstance(value, dict) else "[]"
            elif value == "":
                value = '""'
            if isinstance(value, str):
                for secret in secret_values:
                    value = value.replace(secret, "***")
                value = ILLEGAL_CHARACTERS_RE.sub("", value)
                if len(value) > 32767:
                    raise AWXError(f'Parametr {parameter!r} jest za długi na komórkę Excela.')
            sheet.append([section, parameter, value])
            cell = sheet.cell(sheet.max_row, 3)
            if isinstance(value, str):
                cell.data_type = "s"
                cell.number_format = "@"
            elif isinstance(value, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm:ss"

    for section, values in sections.items():
        append_values(section, values)
    widths = {"A": 25, "B": 48, "C": 100}
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    for row in sheet.iter_rows(min_row=4):
        header = row[0].row == 4
        height = 26
        for cell in row:
            cell.font = Font(name="Arial", size=10, bold=header, color="FFFFFF" if header else "1F2937")
            cell.fill = PatternFill("solid", fgColor="1F3A5F" if header else "F0F4F8" if cell.row % 2 else "FFFFFF")
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
            lines = sum(max(1, math.ceil(len(line) / (widths[cell.column_letter] - 3)))
                        for line in str(cell.value).split("\n"))
            height = max(height, lines * 16 + 6)
        sheet.row_dimensions[row[0].row].height = min(409, height)
    sheet.freeze_panes = "C5"
    sheet.auto_filter.ref = f"A4:C{sheet.max_row}"
    sheet.print_options.horizontalCentered = True
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.print_title_rows = "1:4"
    sheet.print_area = sheet.dimensions
    return sheet


def headers(execution_column="exceiute_job", output_column="output"):
    names = [*METADATA_COLUMNS, execution_column.strip(), output_column.strip(), *ARTIFACT_COLUMNS]
    keys = [name.casefold() for name in names]
    if any(not key for key in keys) or len(keys) != len(set(keys)):
        raise AWXError("Execution/output column names must be separate from metadata, artifact columns and each other.")
    return names


def read_columns(sheet):
    columns = {}
    for cell in sheet[1]:
        name = str(cell.value or "").strip().casefold()
        if not name:
            continue
        if name in columns:
            raise AWXError(f"Duplicate column {cell.value!r} in worksheet {sheet.title!r}.")
        columns[name] = cell.column
    return columns


def select_sheet(workbook, preferred="Results", explicit=None):
    if explicit is not None:
        if explicit not in workbook.sheetnames:
            raise AWXError(f"Worksheet {explicit!r} does not exist.")
        return workbook[explicit]
    return workbook[preferred] if preferred in workbook.sheetnames else workbook.active


def ensure_columns(sheet, columns, names, source_column=1):
    added = []
    for name in names:
        key = name.casefold()
        if key not in columns:
            empty = sheet.max_row == 1 and sheet.max_column == 1 and sheet.cell(1, 1).value is None
            column = 1 if empty else sheet.max_column + 1
            columns[key] = column
            sheet.cell(1, column).value = name
            added.append(name)
    return added


def copy_cell(source, target):
    target.value = source.value
    target.data_type = source.data_type
    target._style = copy(source._style)
    target.hyperlink = copy(source.hyperlink)
    target.comment = copy(source.comment)


def prepare_sheet(sheet, execution_column="exceiute_job", output_column="output", *, reorder=True):
    """Repair and order the complete schema without dropping custom columns or history."""
    if any(merged.max_row > 1 for merged in sheet.merged_cells.ranges):
        raise AWXError("Host data rows must not contain merged cells. Use one host per row.")
    # Excel/openpyxl may retain empty column objects after columns are deleted.
    # Keep unlabelled columns containing user data, and remove empty remnants.
    for column in range(sheet.max_column, 0, -1):
        if sheet.max_column == 1:
            break
        if all(cell.value in (None, "") and cell.hyperlink is None and cell.comment is None
               for cell in next(sheet.iter_cols(min_col=column, max_col=column))):
            sheet.delete_cols(column)
    columns = read_columns(sheet)
    if "credential" in columns and "os credential" not in columns:
        column = columns.pop("credential")
        sheet.cell(1, column).value = "OS Credential"
        columns["os credential"] = column
    for column in range(1, sheet.max_column + 1):
        if not str(sheet.cell(1, column).value or "").strip() and any(
            cell.value is not None or cell.hyperlink is not None or cell.comment is not None
            for cell in next(sheet.iter_cols(min_col=column, max_col=column, min_row=2))
        ):
            name = f"Unlabelled_{column}"
            while name.casefold() in columns:
                name += "_"
            sheet.cell(1, column).value = name
            columns[name.casefold()] = column
    names = headers(execution_column, output_column)
    alias = next((columns[name] for name in HOST_COLUMNS if name in columns), None)
    had_hostname = "hostname" in columns
    added = ensure_columns(sheet, columns, names)
    if not had_hostname and alias is not None:
        for row_number in range(2, sheet.max_row + 1):
            copy_cell(sheet.cell(row_number, alias), sheet.cell(row_number, columns["hostname"]))
    for name in names:
        sheet.cell(1, columns[name.casefold()]).value = name
    if reorder:
        canonical = [columns[name.casefold()] for name in names]
        order = [*canonical, *(column for column in range(1, sheet.max_column + 1) if column not in canonical)]
        if order != list(range(1, sheet.max_column + 1)):
            # Survey dropdowns cover whole columns; retain their targets after repair.
            mapping = {source: target for target, source in enumerate(order, 1)}
            for rule in sheet.data_validations.dataValidation:
                if rule.promptTitle in ('AWX survey', 'AWX jumphost'):
                    targets = []
                    for area in rule.sqref.ranges:
                        for source in range(area.min_col, area.max_col + 1):
                            if source not in mapping:
                                continue
                            letter = get_column_letter(mapping[source])
                            targets.append(f'{letter}{area.min_row}:{letter}{area.max_row}')
                    rule.sqref = ' '.join(targets)
            rows = [[copy(cell) for cell in row] for row in sheet.iter_rows()]
            dimensions = {column: copy(sheet.column_dimensions[get_column_letter(column)]) for column in order}
            for row_number, row in enumerate(rows, start=1):
                for target_column, source_column in enumerate(order, start=1):
                    copy_cell(row[source_column - 1], sheet.cell(row_number, target_column))
            sheet.column_dimensions.clear()
            for target_column, source_column in enumerate(order, start=1):
                dimension = dimensions[source_column]
                dimension.index = get_column_letter(target_column)
                dimension.min = target_column
                dimension.max = target_column
                sheet.column_dimensions[dimension.index] = dimension
    return read_columns(sheet), added


def sort_host_rows(sheet, hostname_column=None, os_column=None):
    columns = read_columns(sheet)
    hostname_column = hostname_column or columns.get("hostname")
    os_column = os_column or columns.get("ostype")
    if not hostname_column or not os_column:
        return
    source_rows = list(range(2, sheet.max_row + 1))

    def rank(row_number):
        if not str(sheet.cell(row_number, hostname_column).value or "").strip():
            return 4
        return OS_ORDER.get(str(sheet.cell(row_number, os_column).value or "").strip().casefold(), 3)

    sorted_rows = sorted(source_rows, key=rank)
    if sorted_rows == source_rows:
        return
    snapshots = {row_number: ([copy(cell) for cell in sheet[row_number]], copy(sheet.row_dimensions[row_number]))
                 for row_number in source_rows}
    for target_row, source_row in zip(source_rows, sorted_rows):
        cells, dimensions = snapshots[source_row]
        dimensions.index = target_row
        sheet.row_dimensions[target_row] = dimensions
        for source in cells:
            copy_cell(source, sheet.cell(target_row, source.column))
    mapping = dict(zip(sorted_rows, source_rows))
    for rule in sheet.data_validations.dataValidation:
        if rule.promptTitle == 'AWX jumphost':
            rule.sqref = ' '.join(f'{get_column_letter(area.min_col)}{mapping.get(area.min_row, area.min_row)}'
                                 for area in rule.sqref.ranges)


def format_sheet(sheet, execution_column="exceiute_job", output_column="output"):
    """Apply the same formatting and text/link handling at every workflow stage."""
    names = {name.casefold() for name in headers(execution_column, output_column)}
    columns = read_columns(sheet)
    inverse = {column: name for name, column in columns.items()}
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "B2"
    sheet.auto_filter.ref = sheet.dimensions
    widths = {}
    for column in sheet.iter_cols():
        name = inverse.get(column[0].column)
        longest = max((len(line) for cell in column for line in str(cell.value or "").split("\n")), default=0)
        width = min(90 if name == output_column.casefold() else 70, max(18, longest + 3))
        widths[column[0].column] = width
        sheet.column_dimensions[get_column_letter(column[0].column)].width = width
    link_columns = {"job_link", execution_column.casefold()}
    for row in sheet.iter_rows():
        line_count = 1
        for cell in row:
            name = inverse.get(cell.column)
            if name in names:
                if name == "hops_count" and cell.row > 1 and (cell.value is None or type(cell.value) is int):
                    cell.data_type = "n"
                    cell.number_format = "0"
                else:
                    # Other workflow data is literal text, including stdout starting with '='.
                    if cell.value is not None:
                        cell.value = str(cell.value)
                    cell.data_type = "s"
                    cell.number_format = "@"
                if name in link_columns:
                    cell.hyperlink = cell.value if str(cell.value or "").startswith(("http://", "https://")) else None
            cell.font = Font(name="Arial", size=10, bold=cell.row == 1,
                             color="FFFFFF" if cell.row == 1 else "0563C1" if cell.hyperlink else "1F2937",
                             underline="single" if cell.row != 1 and cell.hyperlink else None)
            cell.fill = PatternFill("solid", fgColor="1F3A5F" if cell.row == 1 else "F0F4F8" if cell.row % 2 == 0 else "FFFFFF")
            cell.alignment = Alignment(horizontal="center" if cell.row == 1 else "left", vertical="center", wrap_text=True)
            line_count = max(line_count, sum(max(1, math.ceil(len(line) / (widths[cell.column] - 3)))
                                             for line in str(cell.value or "").split("\n")))
        sheet.row_dimensions[row[0].row].height = min(120, max(26, line_count * 16 + 6))
    for table in sheet.tables.values():
        # Keep existing Excel table filters aware of newly restored columns.
        if table.ref.split(":")[0] == "A1":
            table.ref = sheet.dimensions
            table.autoFilter = AutoFilter(ref=sheet.dimensions)
            previous = {column.name.casefold(): column for column in table.tableColumns}
            table_columns = []
            for index, cell in enumerate(sheet[1], start=1):
                column = copy(previous.get(str(cell.value).casefold()))
                if column is None:
                    column = TableColumn(id=index, name=str(cell.value))
                column.id = index
                column.name = str(cell.value)
                table_columns.append(column)
            table.tableColumns = table_columns


def save_workbook(workbook, path, *, sheet=None, execution_column="exceiute_job", output_column="output",
                  excel_columns=None):
    path = Path(path)
    selected = sheet if sheet is not None else workbook.active
    LOGGER.debug('Zapis Excela: przygotowuję formatowanie i widoczność kolumn\n  Plik: %s\n  Arkusz: %s', path, selected.title)
    visible = normalize_excel_columns(excel_columns)
    if excel_columns is not None:
        # Własne kolumny dodajemy na końcu, bez zmiany indeksów używanych przez zadania.
        ensure_columns(selected, read_columns(selected), visible)
    format_sheet(selected, execution_column, output_column)
    if excel_columns is not None:
        visible_keys = {name.casefold() for name in visible}
        for name, column in read_columns(selected).items():
            dimension = selected.column_dimensions[get_column_letter(column)]
            dimension.min = dimension.max = column
            dimension.hidden = bool(visible_keys) and name not in visible_keys
    temporary_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.stem}-", suffix=path.suffix, delete=False) as temporary:
            temporary_path = Path(temporary.name)
        workbook.save(temporary_path)
        os.replace(temporary_path, path)
        LOGGER.debug('Zapis Excela zakończony: plik wynikowy zastąpiono gotowym plikiem tymczasowym\n  Plik: %s\n  Wiersze danych: %d\n  Kolumny: %d',
                     path, max(0, selected.max_row - 1), selected.max_column)
    except OSError as exc:
        raise AWXError(f"Cannot save {path}: {exc}. Close the file in Excel before running the script.") from None
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def create_workbook(names, rows, *, sheet_name="Results", execution_column="exceiute_job", output_column="output"):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    sheet.append(names)
    for row in rows:
        sheet.append(row)
    prepare_sheet(sheet, execution_column, output_column)
    sort_host_rows(sheet)
    return workbook
