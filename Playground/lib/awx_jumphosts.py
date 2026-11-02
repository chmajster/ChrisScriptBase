"""Discover jumphost credentials and offer editable Excel choices."""

import hashlib
import re
from collections import Counter

from openpyxl.comments import Comment
from openpyxl.utils import get_column_letter, quote_sheetname
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation

from . import awx_excel as excel
from .awx_config import AWXError


COLUMN = 'jumphost_for_hops'
VALIDATION_TITLE = 'AWX jumphost'
OPTIONS_MARKER = 'AWX jumphost credential choices'
SELECTION_MARKER = '\nJumphosty dołączone z jumphost_for_hops:\n'


def previous_full_selection(cell):
    """Read the last merged selection so a changed dropdown replaces it."""
    text = cell.comment.text if cell.comment else ''
    _, marker, selected = text.partition(SELECTION_MARKER)
    return tuple(line for line in selected.splitlines() if line and line != 'none') if marker else ()


def remember_full_selection(cell, selected):
    text = cell.comment.text if cell.comment else ''
    original = text.partition(SELECTION_MARKER)[0]
    author = cell.comment.author if cell.comment else 'AWX'
    cell.comment = Comment(original + SELECTION_MARKER + ('\n'.join(selected) or 'none'), author)


def credential_options(client, application_id, hop_count, separator=', '):
    """Match one exact hop count and ApplicationID; return lowest-ID first."""
    if client is None or not application_id or str(application_id).casefold() == 'none':
        return []
    if type(hop_count) is not int or hop_count <= 0:
        return []
    application_id = str(application_id).strip()
    cache = client.jumphost_credential_cache
    key = application_id.casefold()
    if key not in cache:
        cache[key] = list(client.items('credentials/', name__icontains=application_id, order_by='id',
                                      purpose=f'Find jumphost credentials for ApplicationID {application_id!r}'))
    credentials = cache[key]
    names = Counter(credential['name'] for credential in credentials)
    application_pattern = re.compile(r'(?<![a-z0-9])' + re.escape(application_id) + r'(?![a-z0-9])', re.IGNORECASE)
    options = []
    for credential in sorted(credentials, key=lambda item: item['id']):
        name = credential['name']
        hops = re.search(r'([0-9]+)_?hop(?![a-z0-9])', name, re.IGNORECASE)
        if not application_pattern.search(name) or hops is None or int(hops.group(1)) != hop_count:
            continue
        # Numeric/duplicate names or separators cannot be unambiguous cell references.
        numeric_reference = (name.isdigit() or names[name] != 1 or name != name.strip()
                             or '\n' in name or '\r' in name or ',' in name
                             or bool(separator.strip() and separator.strip() in name))
        reference = str(credential['id']) if numeric_reference else name
        options.append(reference)
        client.credential_cache[str(credential['id'])] = credential
        if not numeric_reference:
            client.credential_cache[reference] = credential
    return options


def populate_choices(sheet, client, separator=', '):
    """Rebuild row-specific dropdowns without overwriting user selections.

    Named ranges on a hidden worksheet avoid Excel's 255-character inline-list
    limit and allow commas/quotes in display data to be handled through IDs.
    Multiple credential references can be typed using the configured separator.
    """
    columns = excel.read_columns(sheet)
    if COLUMN not in columns:
        return
    sheet.data_validations.dataValidation = [rule for rule in sheet.data_validations.dataValidation
                                            if rule.promptTitle != VALIDATION_TITLE]
    book = sheet.parent
    suffix = hashlib.sha256(sheet.title.encode('utf-8')).hexdigest()[:8]
    helper_name = '_AWX_JH_' + suffix
    prefix = 'AWX_JH_' + suffix + '_'
    for name in list(book.defined_names):
        if name.startswith(prefix):
            del book.defined_names[name]
    helper = book[helper_name] if helper_name in book.sheetnames else None
    if helper is not None:
        if helper['A1'].value != OPTIONS_MARKER:
            raise AWXError(f'Worksheet {helper_name!r} is reserved for jumphost credential choices.')
        helper.delete_rows(1, helper.max_row)
    ranges = {}
    instructions = ('Wybierz credential z listy. Kilka nazw/ID oddziel separatorem: '
                    + repr(separator) + '. Wpis none wyłącza dodatki. '
                    'Wyczyść komórkę przed odświeżeniem, aby przywrócić wybór domyślny. '
                    'Full_Credential jest aktualizowane przy odświeżeniu i przed startem joba.')
    sheet.cell(1, columns[COLUMN]).comment = Comment(instructions, 'AWX')
    for row in range(2, sheet.max_row + 1):
        app = sheet.cell(row, columns['applicationid']).value if 'applicationid' in columns else None
        count = sheet.cell(row, columns['hops_count']).value if 'hops_count' in columns else None
        options = tuple(credential_options(client, app, count, separator))
        cell = sheet.cell(row, columns[COLUMN])
        cell.comment = Comment(instructions + '\n\nDostępne credentiale:\n' + ('\n'.join(options) or 'Brak dopasowań.'), 'AWX')
        if not options:
            continue
        if options not in ranges:
            if helper is None:
                helper = book.create_sheet(helper_name)
            helper['A1'] = OPTIONS_MARKER
            helper.sheet_state = 'hidden'
            column = len(ranges) + 1
            for index, option in enumerate(options, start=2):
                entry = helper.cell(index, column, option)
                entry.data_type = 's'
            letter = get_column_letter(column)
            name = prefix + str(column)
            definition = DefinedName(name, attr_text=f'{quote_sheetname(helper_name)}!${letter}$2:${letter}${len(options) + 1}')
            book.defined_names.add(definition)
            ranges[options] = name
        rule = DataValidation(type='list', formula1='=' + ranges[options], allow_blank=True)
        rule.promptTitle = VALIDATION_TITLE
        rule.prompt = 'Wybierz z listy lub wpisz kilka nazw/ID po separatorze.'
        rule.showInputMessage = True
        rule.showErrorMessage = False
        sheet.add_data_validation(rule)
        rule.add(cell)
    if helper is not None and not ranges:
        book.remove(helper)
