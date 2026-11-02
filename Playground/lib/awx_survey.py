"""AWX survey answers, defaults, validation and optional terminal questions."""

import getpass
import json
import math
import sys
from copy import deepcopy

from . import awx_console as console
from .awx_config import AWXError


MISSING = object()
QUESTION_TYPES = {'text', 'textarea', 'password', 'integer', 'float', 'multiplechoice', 'multiselect'}


def answer_mapping(value, label='SURVEY_ANSWERS'):
    if not isinstance(value, dict) or any(not isinstance(key, str) or not key.strip() for key in value):
        raise AWXError(f'{label} must be a dictionary with nonempty variable names.')
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        raise AWXError(f'{label} must contain valid JSON values.') from None
    return deepcopy(value)


def read_questions(spec, template_id):
    questions = spec.get('spec') if isinstance(spec, dict) else None
    if not isinstance(questions, list):
        raise AWXError(f'Template {template_id}: invalid survey specification.')
    seen = set()
    for question in questions:
        variable = question.get('variable') if isinstance(question, dict) else None
        if not isinstance(variable, str) or not variable.strip() or variable in seen:
            raise AWXError(f'Template {template_id}: survey has a missing or duplicate variable name.')
        seen.add(variable)
        if question.get('type') not in QUESTION_TYPES:
            raise AWXError(f'Template {template_id}: unsupported survey type for {variable!r}.')
    return questions


def choices(question):
    values = question.get('choices', [])
    if isinstance(values, str):
        values = [value for value in values.splitlines() if value.strip()]
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise AWXError(f"Survey {question['variable']!r}: invalid choices in AWX.")
    return values


def validate_answer(question, value):
    """Follow AWX types/ranges; errors never contain the answer's value."""
    kind = question['type']
    variable = question['variable']
    prefix = f'Survey {variable!r}: '
    if kind == 'password' and value == '$encrypted$':
        if question.get('default') in (None, ''):
            raise AWXError(prefix + 'no stored password default; provide an answer.')
        return
    if kind in ('text', 'textarea', 'password'):
        if not isinstance(value, str):
            raise AWXError(prefix + 'expected a string.')
        measured = len(value)
    elif kind == 'integer':
        if type(value) is not int:
            raise AWXError(prefix + 'expected an integer.')
        measured = value
    elif kind == 'float':
        if type(value) not in (int, float) or not math.isfinite(value):
            raise AWXError(prefix + 'expected a finite number.')
        measured = value
    elif kind in ('multiplechoice', 'multiselect'):
        options = choices(question)
        if kind == 'multiplechoice':
            if not isinstance(value, str) or value not in options:
                raise AWXError(prefix + 'answer must be one of the survey choices.')
        elif not isinstance(value, list) or any(item not in options for item in value):
            raise AWXError(prefix + 'expected a list of survey choices.')
        return
    else:
        raise AWXError(prefix + 'unsupported question type.')
    for name, comparison in (('min', lambda limit: measured < limit), ('max', lambda limit: measured > limit)):
        limit = question.get(name)
        if limit is None or limit == '':
            continue
        try:
            limit = float(limit) if kind == 'float' else int(limit)
        except (TypeError, ValueError, OverflowError):
            raise AWXError(prefix + f'invalid {name} limit in AWX.') from None
        if comparison(limit):
            raise AWXError(prefix + f'answer does not meet the {name} limit ({limit}).')


def default_answer(question):
    value = deepcopy(question.get('default', MISSING)) if 'default' in question else MISSING
    if value is MISSING or value is None:
        return MISSING
    if value == '' and question['type'] not in ('text', 'textarea', 'password'):
        return MISSING
    if question['type'] == 'multiselect' and isinstance(value, str):
        value = [part for part in value.splitlines() if part.strip()]
    try:
        validate_answer(question, value)
    except AWXError:
        return MISSING
    return value


def prompt_answer(question, default, template_id):
    if not sys.stdin.isatty():
        raise AWXError('Interactive survey needs a terminal; set SURVEY_ANSWERS or Job_Variables for unattended runs.')
    variable = question['variable']
    kind = question['type']
    label = question.get('question_name') or variable
    console.print_message(f'Template {template_id}: {label} [{variable}; {kind}]', level='info')
    options = choices(question) if kind in ('multiplechoice', 'multiselect') else []
    for index, option in enumerate(options, 1):
        console.print_message(f'  {index}. {option}', level='info')
    if default is not MISSING:
        display = '***' if kind == 'password' else json.dumps(default, ensure_ascii=False)
        console.print_message(f'Default: {display}; Enter keeps it.', level='info')
    elif not question.get('required'):
        console.print_message('Optional: Enter skips this answer.', level='info')
    while True:
        try:
            raw = getpass.getpass('Answer: ') if kind == 'password' else input('Answer: ')
            if raw == '':
                if default is not MISSING:
                    return deepcopy(default)
                if not question.get('required'):
                    return '' if kind in ('text', 'textarea') and question.get('min') in (0, '0', '', None) else MISSING
                raise AWXError(f'Survey {variable!r}: answer is required.')
            if kind == 'integer':
                value = int(raw)
            elif kind == 'float':
                value = float(raw)
            elif kind == 'multiselect':
                value = json.loads(raw) if raw.lstrip().startswith('[') else [part.strip() for part in raw.split(',')]
                if isinstance(value, list):
                    value = [options[int(item) - 1] if isinstance(item, str) and item not in options
                             and item.isdigit() and 1 <= int(item) <= len(options) else item for item in value]
            elif kind == 'multiplechoice' and raw not in options and raw.isdigit() and 1 <= int(raw) <= len(options):
                value = options[int(raw) - 1]
            else:
                value = raw
            validate_answer(question, value)
            return value
        except EOFError:
            raise AWXError('Survey input ended; no new jobs were launched.') from None
        except (ValueError, AWXError) as exc:
            # ValueError from numeric/JSON conversion may include a password-like value.
            message = str(exc) if isinstance(exc, AWXError) else f'Survey {variable!r}: invalid answer format.'
            console.print_message(message, level='warning')


def prepare_variables(questions, variables, template_id, *, interactive=False, prompted=None):
    result = deepcopy(variables)
    prompted = {} if prompted is None else prompted
    errors = []
    for question in questions:
        variable = question['variable']
        if variable not in result:
            value = default_answer(question)
            if variable in prompted:
                value = prompted[variable]
            elif interactive:
                value = prompt_answer(question, value, template_id)
                prompted[variable] = value
            if value is not MISSING:
                result[variable] = deepcopy(value)
        if variable not in result:
            if question.get('required'):
                errors.append(f'{variable!r} is required')
            elif question['type'] in ('text', 'textarea') and question.get('min') in (0, '0', '', None):
                result[variable] = ''
            continue
        try:
            validate_answer(question, result[variable])
        except AWXError as exc:
            errors.append(str(exc))
    if errors:
        raise AWXError(f"Template {template_id}: survey validation failed: {'; '.join(errors)}. "
                       'Set SURVEY_ANSWERS / Job_Variables or use --survey.')
    return result


def display_questions(template_id, enabled, questions):
    console.print_message(f'Template {template_id}: survey {"enabled" if enabled else "disabled"}; questions: {len(questions)}',
                          level='info' if enabled else 'warning')
    for question in questions:
        required = 'required' if question.get('required') else 'optional'
        metadata = {'type': question['type'], 'required': required,
                    'min': question.get('min'), 'max': question.get('max')}
        if question['type'] in ('multiplechoice', 'multiselect'):
            metadata['choices'] = choices(question)
        default = default_answer(question)
        if default is not MISSING:
            metadata['default'] = '***' if question['type'] == 'password' else default
        console.print_message(f"  {question['variable']}: {json.dumps(metadata, ensure_ascii=False)}", level='info')
