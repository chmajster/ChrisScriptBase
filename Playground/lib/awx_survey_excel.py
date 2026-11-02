"""Editable survey answer columns shared by inventory, refresh and execution."""

import json
from copy import deepcopy

from openpyxl.comments import Comment
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.utils import get_column_letter

from . import awx_excel as excel
from . import awx_survey as survey
from .awx_config import AWXError


COLUMN_PREFIX = 'Survey_'
VALIDATION_TITLE = 'AWX survey'


def column_name(variable):
    return COLUMN_PREFIX + variable


def cell_answer(question, value):
    """Convert editable Excel text/numbers to the JSON type expected by AWX."""
    kind = question['type']
    try:
        if kind == 'integer' and not isinstance(value, bool):
            if isinstance(value, str):
                value = int(value.strip())
            elif isinstance(value, float) and value.is_integer():
                value = int(value)
        elif kind == 'float' and isinstance(value, str):
            value = float(value.strip())
        elif kind == 'multiselect' and isinstance(value, str):
            value = json.loads(value) if value.lstrip().startswith('[') else [part.strip() for part in value.replace('\r', '').replace('\n', ',').split(',') if part.strip()]
        elif kind in ('text', 'textarea', 'password', 'multiplechoice') and not isinstance(value, str):
            value = str(value)
    except (ValueError, TypeError):
        raise AWXError(f"Survey column {column_name(question['variable'])!r}: invalid answer format.") from None
    survey.validate_answer(question, value)
    return value


def read_answers(sheet, row_number, questions=None, *, skip_variables=()):
    columns = excel.read_columns(sheet)
    result = {}
    if questions is None:
        selected = [(str(cell.value)[len(COLUMN_PREFIX):], cell.column, None) for cell in sheet[1]
                    if str(cell.value or '').casefold().startswith(COLUMN_PREFIX.casefold())]
    else:
        selected = [(question['variable'], columns.get(column_name(question['variable']).casefold()), question)
                    for question in questions]
    for variable, column, question in selected:
        if not column or not variable or variable in skip_variables:
            continue
        cell = sheet.cell(row_number, column)
        if cell.value is None or cell.value == '':
            continue
        if cell.data_type == 'f':
            raise AWXError(f'Row {row_number}: survey answers must contain literal values, not Excel formulas.')
        try:
            value = cell.value if isinstance(cell.value, (str, int, float, bool, list, dict)) else str(cell.value)
            result[variable] = cell_answer(question, value) if question is not None else value
        except AWXError as exc:
            raise AWXError(f'Row {row_number}: {exc}') from None
    return result


def populate_columns(sheet, template_rows, cache, settings, *, overwrite_configured=False):
    """Fill empty cells; refresh can synchronize explicitly configured answers."""
    columns = excel.read_columns(sheet)
    definitions = {}
    variants = {}
    configured = {**settings.get('SURVEY_ANSWERS', {}), **settings.get('SURVEY_EXCEL_DEFAULTS', {})}
    reserved = {name.casefold() for name in excel.headers(settings['EXECUTION_COLUMN'], settings.get('OUTPUT_COLUMN', 'output'))}
    for template_id, rows in template_rows.items():
        for question in cache[template_id][1]:
            variable = question['variable']
            name = column_name(variable)
            if name.casefold() in reserved:
                raise AWXError(f'Survey column {name!r} conflicts with an execution/output column.')
            previous = definitions.get(name.casefold())
            if previous is not None and previous['variable'] != variable:
                raise AWXError('Survey variable names that differ only in case cannot share an Excel column.')
            definitions[name.casefold()] = question
            variants.setdefault(name.casefold(), []).append(question)
    added = excel.ensure_columns(sheet, columns, [column_name(question['variable']) for question in definitions.values()])
    sheet.data_validations.dataValidation = [rule for rule in sheet.data_validations.dataValidation
                                            if rule.promptTitle != VALIDATION_TITLE]
    for key, question in definitions.items():
        header = sheet.cell(1, columns[key])
        header.value = column_name(question['variable'])
        description = [f"AWX survey: {question.get('question_name') or question['variable']}",
                       f"Variable: {question['variable']}", f"Type: {question['type']}",
                       'Required' if question.get('required') else 'Optional',
                       'Edit this cell to override Job_Variables when launching.',
                       'Refresh restores answers configured in SURVEY_EXCEL_DEFAULTS / SURVEY_ANSWERS.']
        if question['type'] in ('multiplechoice', 'multiselect'):
            options = list(dict.fromkeys(option for variant in variants[key]
                                        if variant['type'] in ('multiplechoice', 'multiselect')
                                        for option in survey.choices(variant)))
            description.append('Choices: ' + json.dumps(options, ensure_ascii=False))
            if question['type'] == 'multiselect':
                description.append('Multiple answers: JSON list, e.g. ["first", "second"].')
            elif all(variant['type'] == 'multiplechoice' for variant in variants[key]) and options and all(',' not in option and '"' not in option for option in options) and len(','.join(options)) <= 250:
                rule = DataValidation(type='list', formula1='"' + ','.join(options) + '"', allow_blank=True)
                rule.promptTitle = VALIDATION_TITLE
                rule.prompt = 'Choose an answer. Required fields are validated before launch.'
                rule.errorTitle = 'Survey choice'
                rule.error = 'Choose a value from the survey list.'
                rule.showErrorMessage = True
                sheet.add_data_validation(rule)
                letter = get_column_letter(columns[key])
                rule.add(f'{letter}2:{letter}1048576')
        header.comment = Comment('\n'.join(description), 'AWX')
    for template_id, rows in template_rows.items():
        for row_number in rows:
            raw = sheet.cell(row_number, columns['job_variables']).value if 'job_variables' in columns else None
            try:
                variables = json.loads(raw) if raw and str(raw).strip().casefold() != 'none' else {}
            except (TypeError, ValueError):
                raise AWXError(f'Row {row_number}: Job_Variables must be a JSON object.') from None
            if not isinstance(variables, dict):
                raise AWXError(f'Row {row_number}: Job_Variables must be a JSON object.')
            for question in cache[template_id][1]:
                variable = question['variable']
                cell = sheet.cell(row_number, columns[column_name(variable).casefold()])
                if overwrite_configured and variable in configured:
                    value = configured[variable]
                    survey.validate_answer(question, value)
                else:
                    if cell.value is not None and cell.value != '':
                        continue
                    value = variables.get(variable, configured.get(variable, survey.MISSING))
                if value is survey.MISSING:
                    value = survey.default_answer(question)
                if value is survey.MISSING or value is None:
                    continue
                if isinstance(value, (list, dict)):
                    value = json.dumps(value, ensure_ascii=False)
                if isinstance(value, str) and (len(value) > 32767 or ILLEGAL_CHARACTERS_RE.search(value)):
                    raise AWXError(f'Survey column {column_name(variable)!r}: default cannot be represented as an Excel cell.')
                cell.value = deepcopy(value)
                if isinstance(value, str):
                    cell.data_type = 's'
                    cell.number_format = '@'
    return added
