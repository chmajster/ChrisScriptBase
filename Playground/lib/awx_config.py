"""One source of AWX variables and isolated per-launcher overrides.

Priority: command-line arguments > launcher VARIABLES > per-operation overrides
in awx_variable > shared values in awx_variable. Variables also accept YAML.
"""

import ast
import io
import json
import math
import re
import textwrap
import tokenize
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class AWXError(RuntimeError):
    """An API, configuration, workbook, or input error."""


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VARIABLES_FILE = PROJECT_ROOT / "awx_variable"
EXECUTION_SETTINGS_FILE = VARIABLES_FILE
OUTPUT_SETTINGS_FILE = VARIABLES_FILE
DEFAULT_EXECUTION_COLUMN = 'exceiute_job_link'
LEGACY_EXECUTION_COLUMN = 'exceiute_job'
OS_VERSION_COLUMN = 'OS VERSION'
METADATA_COLUMNS = (
    "hostname", "AWX_Status", "INVENTORY", "ApplicationID", "ostype", OS_VERSION_COLUMN, "template_job_name", "Job_Link", "Job_Credentials", "Job_Variables", "HOPS", "HOPS_COUNT", "jumphost_for_hops", "Extra_Credentials",
    "OS Credential", "Full_Credential", "Job_Type",
)
ARTIFACT_STATUS_COLUMN = 'artifact status'
ARTIFACT_COLUMNS = ("Artifacts", "artifact filter", ARTIFACT_STATUS_COLUMN)
CONNECTION_KEYS = ("AWX_URL", "AWX_TOKEN", "TIMEOUT", "PAGE_SIZE")
TEMPLATE_KEYS = ("JOB_TEMPLATE_AUTO_DISCOVERY", "JOB_TEMPLATE_ORGANIZATION",
                 "JOB_TEMPLATE_NAME_PATTERN", "JOB_TEMPLATE_OS_NAMES")
TEMPLATE_DEFAULTS = {
    "JOB_TEMPLATE_AUTO_DISCOVERY": False,
    "JOB_TEMPLATE_ORGANIZATION": "",
    "JOB_TEMPLATE_NAME_PATTERN": "*zabbix*install*{os_name}*",
    "JOB_TEMPLATE_OS_NAMES": {"linux": "unix", "aix": "aix", "windows": "window"},
}
INVENTORY_KEYS = (
    "AWX_URL", "AWX_TOKEN", "INVENTORY", "CREDENTIAL_VARIABLES", "EXTRA_CREDENTIAL_VARIABLES",
    "FULL_CREDENTIAL_SEPARATOR", "OUTPUT_FILE", "EXCEL_OUTPUT_FILE", "TIMEOUT", "PAGE_SIZE", "OS_JOBS",
)
EXECUTION_KEYS = (
    "INPUT_FILE", "SHEET_NAME", "BATCH_SIZE", "EXECUTION_COLUMN", "EXECUTION_SCOPE", "JOB_TYPE", "JOB_TYPE_VARIABLE",
    "WAIT_FOR_COMPLETION", "POLL_INTERVAL", "JOB_WAIT_TIMEOUT", "HOSTS", "JOB_URL",
    "SURVEY_ANSWERS", "SURVEY_INTERACTIVE", "SURVEY_EXCEL_DEFAULTS", "EXCEL_COLUMNS",
    "REFRESH_INVENTORY_PATTERN", "JOB_VARIABLE_OVERRIDES_BY_OS", *TEMPLATE_KEYS,
)
OUTPUT_KEYS = (
    "INPUT_FILE", "SHEET_NAME", "EXECUTION_COLUMN", "OUTPUT_COLUMN", "OUTPUT_DIRECTORY",
    "WAIT_FOR_COMPLETION", "POLL_INTERVAL", "JOB_WAIT_TIMEOUT", "EXCEL_COLUMNS",
)
RUNTIME_KEYS = ("DEBUG", "DRY_RUN", "HOST_COLUMN", "COLOR")
SETTING_NAMES = frozenset((*INVENTORY_KEYS, *EXECUTION_KEYS, *OUTPUT_KEYS, *RUNTIME_KEYS))
OPERATION_SCOPES = {
    "inventory": "INVENTORY_OVERRIDES",
    "refresh": "REFRESH_OVERRIDES",
    "execute": "EXECUTE_OVERRIDES",
    "output": "OUTPUT_OVERRIDES",
    "templates": "TEMPLATES_OVERRIDES",
}
_ACTIVE = ContextVar("awx_launcher_configuration", default=None)
HOSTNAME_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")


def normalize_ostype(value):
    """Use a canonical system name while accepting AWX's singular 'window'."""
    if not isinstance(value, str):
        return value
    value = value.strip().casefold()
    return 'windows' if value == 'window' else value


def hostname_key(value):
    """Compare host identities without changing the name sent to AWX."""
    return str(value or "").strip().casefold()


def unique_hostnames(values):
    """Deduplicate host identities, preserving the first spelling and order."""
    hosts = {}
    for value in values:
        hosts.setdefault(hostname_key(value), value)
    return list(hosts.values())


def normalize_hostnames(values, *, required=False):
    """Accept literal host names as a list or a comma/space-separated string."""
    if values is None or values == "":
        values = []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise AWXError("HOSTS must be a list of hostnames or a comma/space-separated string.")
    hosts = {}
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise AWXError("HOSTS must contain nonempty literal hostnames.")
        parts = [part for part in re.split(r"[,;\s]+", value.strip()) if part]
        if not parts:
            raise AWXError("HOSTS contains no hostnames.")
        for hostname in parts:
            if not HOSTNAME_PATTERN.fullmatch(hostname):
                raise AWXError(f"Invalid hostname {hostname!r}; HOSTS accepts literal hostnames without Ansible patterns.")
            hosts.setdefault(hostname_key(hostname), hostname)
    if required and not hosts:
        raise AWXError("--hosts must contain at least one hostname.")
    return list(hosts.values())


def is_url_key(name):
    return isinstance(name, str) and name.casefold() in ("url", "awx_url", "job_url")


def prepare_multiline_url_literals(content):
    """Allow physical newlines inside quoted URLs without changing source line numbers.

    Ordinary Python quotes cannot span lines. Convert only URL value delimiters
    to triple quotes for AST parsing; normalize the resulting values afterwards.
    Comments and unrelated string literals are skipped as complete tokens.
    """
    tokens = []
    replacements = []
    index = 0
    while index < len(content):
        char = content[index]
        if char.isspace():
            index += 1
            continue
        if char == "#":
            end = content.find("\n", index)
            index = len(content) if end < 0 else end + 1
            continue
        if char in ("'", '"'):
            start = index
            delimiter = char * 3 if content.startswith(char * 3, start) else char
            index += len(delimiter)
            while index < len(content):
                if content[index] == "\\":
                    index += 2
                elif content.startswith(delimiter, index):
                    break
                else:
                    index += 1
            if index >= len(content):
                # Let ast.parse report an unfinished literal at its original line.
                break
            end = index + len(delimiter)
            previous = tokens
            # A raw/unicode string prefix belongs to the literal, not its context.
            if previous and previous[-1][0] == "name" and previous[-1][1].casefold() in ("r", "u") and previous[-1][2] == start:
                previous = previous[:-1]
            url_value = bool(previous and previous[-1][0] == "url_value")
            if len(previous) >= 2:
                kind, name, _ = previous[-2]
                operator = previous[-1][1]
                url_value = url_value or (
                    is_url_key(name) and ((kind == "string" and operator == ":") or (kind == "name" and operator == "="))
                )
            literal = content[start:end]
            if url_value and len(delimiter) == 1 and ("\n" in literal or "\r" in literal):
                replacements.extend(((start, start + 1, char * 3), (end - 1, end, char * 3)))
            if url_value:
                tokens.append(("url_value", None, end))
            else:
                try:
                    value = ast.literal_eval(literal)
                except (ValueError, SyntaxError):
                    value = None
                tokens.append(("string", value, end))
            tokens = tokens[-3:]
            index = end
        elif char.isalpha() or char == "_":
            start = index
            index += 1
            while index < len(content) and (content[index].isalnum() or content[index] == "_"):
                index += 1
            tokens.append(("name", content[start:index], index))
            tokens = tokens[-3:]
        else:
            tokens.append(("operator", char, index + 1))
            tokens = tokens[-3:]
            index += 1
    for start, end, replacement in reversed(replacements):
        content = content[:start] + replacement + content[end:]
    return content


def normalize_url_values(value, name=None):
    """Normalize configured URLs and per-OS override keys, preserving variable values."""
    name = name.upper() if isinstance(name, str) else name
    if name == 'JOB_VARIABLE_OVERRIDES_BY_OS':
        return normalize_job_variable_overrides(value)
    if isinstance(value, str) and name in ("AWX_URL", "JOB_URL") and ("\n" in value or "\r" in value):
        return "".join(line.strip() for line in value.splitlines())
    if isinstance(value, dict) and name in OPERATION_SCOPES.values():
        return {key: normalize_url_values(item, key) for key, item in value.items()}
    if isinstance(value, dict) and name == "OS_JOBS":
        return {
            os_name: {
                key: normalize_url_values(item, "JOB_URL") if key == "url" else item
                for key, item in job.items()
            } if isinstance(job, dict) else job
            for os_name, job in value.items()
        }
    return value


def mapping_end(content, start):
    """Find a Python/flow mapping's closing brace, ignoring quoted text."""
    lines = content[start:].splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    depth = 0
    try:
        for token in tokenize.generate_tokens(io.StringIO(content[start:]).readline):
            if token.type == tokenize.OP:
                if token.string == "{":
                    depth += 1
                elif token.string == "}":
                    depth -= 1
                    if depth == 0:
                        return start + offsets[token.start[0] - 1] + token.start[1] + 1
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return None


def prepare_yaml_variable_blocks(content, path):
    """Translate YAML pasted inside variables = {...} to safe Python literals.

    The surrounding assignments and dictionaries keep their existing syntax.
    Only nonliteral 'variables' mappings are interpreted as YAML. Source line
    counts and the file on disk are preserved, including YAML block strings.
    """
    while True:
        offsets = [0]
        for line in content.splitlines(keepends=True):
            offsets.append(offsets[-1] + len(line))
        previous = []
        replacement = None
        try:
            tokens = tokenize.generate_tokens(io.StringIO(content).readline)
            for token in tokens:
                if token.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                                  tokenize.INDENT, tokenize.DEDENT):
                    continue
                if token.string == "{" and len(previous) == 2 and previous[-1].string == ":":
                    key = previous[0]
                    try:
                        is_variables = key.type == tokenize.STRING and ast.literal_eval(key.string) == "variables"
                    except (ValueError, SyntaxError):
                        is_variables = False
                    if is_variables:
                        start = offsets[token.start[0] - 1] + token.start[1]
                        end = mapping_end(content, start)
                        if end is not None:
                            try:
                                ast.literal_eval(content[start:end])
                            except (ValueError, SyntaxError):
                                pass
                            else:
                                previous = [previous[-1], token]
                                continue
                        # A block mapping closes on a line outside its YAML
                        # indentation. Braces inside block strings are data.
                        for match in re.finditer(r"(?m)^([ \t]*)\}", content[start + 1:]):
                            candidate = start + 1 + match.start()
                            inner = content[start + 1:candidate]
                            meaningful = [line for line in inner.splitlines()
                                          if line.strip() and not line.lstrip().startswith("#")]
                            if meaningful and len(match[1]) <= min(len(line) - len(line.lstrip()) for line in meaningful):
                                end = candidate + len(match[1]) + 1
                                break
                        if end is None:
                            continue  # The normal parser reports an unfinished mapping.
                        original = content[start:end]
                        inner = original[1:-1]
                        # Preserve errors for broken Python quoted multiline
                        # strings; YAML block strings use | or > instead.
                        if not re.search(r"(?m)^\s*[\w.-]+\s*:", inner):
                            try:
                                ast.parse(original)
                            except SyntaxError as exc:
                                if "unterminated" in exc.msg:
                                    continue
                        from .awx_dependencies import ensure_yaml

                        yaml = ensure_yaml()
                        try:
                            value = yaml.safe_load(textwrap.dedent(inner) if inner.startswith("\n") else original)
                            if not isinstance(value, dict):
                                raise ValueError("expected a mapping")
                            json.dumps(value, allow_nan=False)
                            literal = repr(value)
                            ast.literal_eval(literal)
                        except (yaml.YAMLError, ValueError, TypeError, RecursionError):
                            raise AWXError(f"Invalid YAML variables in {path}, line {token.start[0]}. "
                                           "Use an indented YAML mapping with JSON-compatible values.") from None
                        replacement = (start, end, literal + "\n" * original.count("\n"))
                        break
                previous = (previous + [token])[-2:]
        except (tokenize.TokenError, IndentationError, SyntaxError):
            pass  # Report syntax errors through ast.parse with original line numbers.
        if replacement is None:
            return content
        start, end, literal = replacement
        content = content[:start] + literal + content[end:]


def read_literal_settings(path, defaults, *, required=False):
    """Read literal assignments and YAML job variables without executing them."""
    settings = deepcopy(defaults)
    try:
        content = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        if required:
            raise AWXError(f"Cannot find variables file {path}.") from None
        return settings
    except (OSError, UnicodeError) as exc:
        raise AWXError(f"Cannot read {path}: {exc}") from None
    try:
        prepared = prepare_multiline_url_literals(content)
        tree = ast.parse(prepare_yaml_variable_blocks(prepared, path), filename=str(path))
    except SyntaxError as exc:
        raise AWXError(f"Syntax error in {path}, line {exc.lineno}.") from None
    seen = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise AWXError(f"Only NAME = value assignments are allowed in {path}.")
        name = node.targets[0].id.upper()
        if name not in settings or name in seen:
            raise AWXError(f"Unknown or duplicate setting {name} in {path}.")
        seen.add(name)
        try:
            settings[name] = normalize_url_values(ast.literal_eval(node.value), name)
        except (ValueError, TypeError, SyntaxError):
            raise AWXError(f"Setting {name} must use a literal value.") from None
    return settings


def normalize_overrides(values):
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise AWXError("Launcher VARIABLES and per-operation overrides must be dictionaries.")
    result = {}
    for name, value in values.items():
        key = name.upper() if isinstance(name, str) else name
        if key not in SETTING_NAMES or key in result:
            raise AWXError(f"Unknown or duplicate launcher setting {name!r}.")
        result[key] = normalize_url_values(deepcopy(value), key)
    return result


def merge_values(base, overrides):
    """Merge nested settings or variable objects without changing either input."""
    result = deepcopy(base)
    for name, value in overrides.items():
        result[name] = merge_values(result[name], value) if isinstance(result.get(name), dict) and isinstance(value, dict) else deepcopy(value)
    return result


def normalize_job_variable_overrides(value):
    """Validate JSON values and canonicalize OS keys before scope merging."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AWXError('JOB_VARIABLE_OVERRIDES_BY_OS must map OS names to variable objects.')
    result = {}
    for os_name, variables in value.items():
        if not isinstance(os_name, str) or not os_name.strip() or not isinstance(variables, dict):
            raise AWXError('JOB_VARIABLE_OVERRIDES_BY_OS must map nonempty OS names to variable objects.')
        if any(not isinstance(name, str) or not name.strip() for name in variables):
            raise AWXError('JOB_VARIABLE_OVERRIDES_BY_OS variable names must be nonempty strings.')
        try:
            json.dumps(variables, allow_nan=False)
        except (TypeError, ValueError):
            raise AWXError('JOB_VARIABLE_OVERRIDES_BY_OS values must be valid JSON values.') from None
        os_key = normalize_ostype(os_name)
        result[os_key] = merge_values(result.get(os_key, {}), variables)
    return result


def read_variables_file(path):
    schema = {name: None for name in SETTING_NAMES}
    schema.update({name: {} for name in OPERATION_SCOPES.values()})
    values = read_literal_settings(path, schema, required=True)
    for name in OPERATION_SCOPES.values():
        values[name] = normalize_overrides(values[name])
    return values


@contextmanager
def launcher_configuration(operation, variables=None, variables_file=None):
    """Keep overrides local to one run, including nested readers and subsequent runs."""
    if operation not in OPERATION_SCOPES:
        raise AWXError(f"Unknown AWX operation: {operation}")
    state = {"operation": operation, "variables": normalize_overrides(variables),
             "path": Path(variables_file) if variables_file is not None else VARIABLES_FILE, "snapshot": None}
    token = _ACTIVE.set(state)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def current_variables_file():
    state = _ACTIVE.get()
    return state["path"] if state is not None else VARIABLES_FILE


def effective_values(operation):
    state = _ACTIVE.get()
    if state is not None:
        if state["snapshot"] is None:
            state["snapshot"] = read_variables_file(state["path"])
        values = deepcopy(state["snapshot"])
        operation = state["operation"]
    else:
        values = read_variables_file(VARIABLES_FILE)
    scoped = values[OPERATION_SCOPES[operation]]
    values = merge_values({name: values[name] for name in SETTING_NAMES}, scoped)
    return merge_values(values, state["variables"]) if state is not None else values


def group_settings(names, settings_file, operation):
    values = effective_values(operation)
    if settings_file is not None and Path(settings_file).resolve() != current_variables_file().resolve():
        values.update(read_literal_settings(settings_file, {name: values[name] for name in names}))
        state = _ACTIVE.get()
        if state is not None:
            values = merge_values(values, state["variables"])
    return {name: deepcopy(values[name]) for name in names}


# Bootstrap snapshots expose the same public mappings used by library clients/tests.
# Missing/invalid configuration is reported when an operation reads it, so --help works.
try:
    _BOOTSTRAP = read_variables_file(VARIABLES_FILE)
except AWXError:
    _BOOTSTRAP = {name: None for name in SETTING_NAMES}
INVENTORY_DEFAULTS = {name: deepcopy(_BOOTSTRAP[name]) for name in INVENTORY_KEYS}
EXECUTION_DEFAULTS = {name: deepcopy(_BOOTSTRAP[name]) for name in EXECUTION_KEYS}
OUTPUT_DEFAULTS = {name: deepcopy(_BOOTSTRAP[name]) for name in OUTPUT_KEYS}


def read_inventory_settings(settings_file=None):
    return group_settings(INVENTORY_KEYS, settings_file, "inventory")


def normalize_excel_columns(value):
    """Sprawdź listę widocznych kolumn; None i [] oznaczają wszystkie kolumny."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise AWXError("EXCEL_COLUMNS musi być listą nazw kolumn; [] oznacza wszystkie kolumny.")
    names = []
    seen = set()
    for name in value:
        if not isinstance(name, str) or not name.strip():
            raise AWXError("EXCEL_COLUMNS musi zawierać niepuste nazwy kolumn.")
        name = normalize_execution_column(name)
        if name.casefold() in seen:
            raise AWXError(f"Powtórzona nazwa kolumny w EXCEL_COLUMNS: {name!r}.")
        seen.add(name.casefold())
        names.append(name)
    if 'ostype' in seen and OS_VERSION_COLUMN.casefold() not in seen:
        index = next(index for index, name in enumerate(names) if name.casefold() == 'ostype')
        names.insert(index + 1, OS_VERSION_COLUMN)
    if 'artifact filter' in seen and ARTIFACT_STATUS_COLUMN not in seen:
        index = next(index for index, name in enumerate(names) if name.casefold() == 'artifact filter')
        names.insert(index + 1, ARTIFACT_STATUS_COLUMN)
    return names


def artifact_filter_status(value):
    """Classify a host message by the whole word completed, ignoring letter case."""
    return 'completed' if re.search(r'\bcompleted\b', str(value or ''), re.IGNORECASE) else 'error'


def normalize_execution_column(value):
    """Upgrade the former default while retaining custom history column names."""
    name = value.strip()
    return DEFAULT_EXECUTION_COLUMN if name.casefold() == LEGACY_EXECUTION_COLUMN else name


def job_web_url(source_url, job_id=None):
    """Build a job output link in the UI, preserving a known UI prefix/router."""
    try:
        parsed = urlsplit(source_url)
    except ValueError:
        return source_url
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return source_url
    if job_id is None:
        match = re.search(r'/jobs/(?:playbook/)?([1-9][0-9]*)(?:/|$)', parsed.fragment or parsed.path)
        if not match:
            return source_url
        job_id = int(match.group(1))
    route = f'/jobs/playbook/{job_id}/output'
    if parsed.fragment:
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', route))
    api = re.search(r'/(?:api(?:/controller)?|controller)/v2(?:/|$)', parsed.path, re.IGNORECASE)
    if api:
        prefix = parsed.path[:api.start()].rstrip('/') + '/'
        return urlunsplit((parsed.scheme, parsed.netloc, prefix, '', route))
    for marker in ('/templates/', '/jobs/'):
        if marker in parsed.path:
            prefix = parsed.path.split(marker, 1)[0]
            return urlunsplit((parsed.scheme, parsed.netloc, prefix + route, '', ''))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/') + '/', '', route))


def normalize_template_settings(settings, *, require_organization=False):
    for name, default in TEMPLATE_DEFAULTS.items():
        if settings.get(name) is None:
            settings[name] = deepcopy(default)
    if type(settings['JOB_TEMPLATE_AUTO_DISCOVERY']) is not bool:
        raise AWXError('JOB_TEMPLATE_AUTO_DISCOVERY must be True or False.')
    organization = settings['JOB_TEMPLATE_ORGANIZATION']
    if isinstance(organization, str):
        organization = organization.strip()
        settings['JOB_TEMPLATE_ORGANIZATION'] = organization
    if not (isinstance(organization, str) or type(organization) is int and organization > 0):
        raise AWXError('JOB_TEMPLATE_ORGANIZATION must be an organization name or a positive ID.')
    if require_organization and not organization:
        raise AWXError('Set JOB_TEMPLATE_ORGANIZATION to an organization name/ID, or use --organization.')
    pattern = settings['JOB_TEMPLATE_NAME_PATTERN']
    if not isinstance(pattern, str) or not pattern.strip():
        raise AWXError('JOB_TEMPLATE_NAME_PATTERN must be a nonempty template name pattern.')
    settings['JOB_TEMPLATE_NAME_PATTERN'] = pattern.strip()
    names = settings['JOB_TEMPLATE_OS_NAMES']
    if not isinstance(names, dict) or not names or any(
        not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip()
        for key, value in names.items()
    ):
        raise AWXError('JOB_TEMPLATE_OS_NAMES must map operating systems to nonempty name fragments.')
    settings['JOB_TEMPLATE_OS_NAMES'] = {normalize_ostype(key): value.strip() for key, value in names.items()}
    return settings


def normalize_refresh_settings(settings):
    """Refresh derives its template from AWX and validates explicit per-OS overrides."""
    settings.update(JOB_TEMPLATE_AUTO_DISCOVERY=True, JOB_TEMPLATE_ORGANIZATION='',
                    JOB_URL='', JOB_TYPE='')
    settings['JOB_VARIABLE_OVERRIDES_BY_OS'] = normalize_job_variable_overrides(settings.get('JOB_VARIABLE_OVERRIDES_BY_OS'))
    return normalize_template_settings(settings)


def read_template_settings():
    settings = group_settings((*CONNECTION_KEYS, *TEMPLATE_KEYS, 'REFRESH_INVENTORY_PATTERN'), None, 'templates')
    settings['REFRESH_INVENTORY_PATTERN'] = settings['REFRESH_INVENTORY_PATTERN'] or '*_mirror_inventory'
    return settings


def read_execution_settings(settings_file=None, *, refresh=False):
    from .awx_survey import answer_mapping

    settings = group_settings(EXECUTION_KEYS, settings_file, "execute")
    if refresh:
        normalize_refresh_settings(settings)
    else:
        normalize_template_settings(settings)
    pattern = settings["REFRESH_INVENTORY_PATTERN"]
    if pattern is None:
        pattern = "*_mirror_inventory"
    if not isinstance(pattern, str) or not pattern.strip():
        raise AWXError("REFRESH_INVENTORY_PATTERN must be a nonempty inventory name pattern, e.g. '*_mirror_inventory'.")
    settings["REFRESH_INVENTORY_PATTERN"] = pattern.strip()
    settings["EXCEL_COLUMNS"] = normalize_excel_columns(settings["EXCEL_COLUMNS"])
    settings["SURVEY_ANSWERS"] = answer_mapping(settings["SURVEY_ANSWERS"])
    settings["SURVEY_EXCEL_DEFAULTS"] = answer_mapping(settings["SURVEY_EXCEL_DEFAULTS"], "SURVEY_EXCEL_DEFAULTS")
    if type(settings["SURVEY_INTERACTIVE"]) is not bool:
        raise AWXError("SURVEY_INTERACTIVE must be True or False.")
    settings["HOSTS"] = normalize_hostnames(settings["HOSTS"])
    if settings["JOB_URL"] is None:
        settings["JOB_URL"] = ""
    if not isinstance(settings["JOB_URL"], str):
        raise AWXError("JOB_URL must be a job template URL or an empty string.")
    settings["JOB_URL"] = settings["JOB_URL"].strip()
    if settings["INPUT_FILE"] is not None and (
        not isinstance(settings["INPUT_FILE"], str) or not settings["INPUT_FILE"].strip()
    ):
        raise AWXError("INPUT_FILE must be a file path or None.")
    if not isinstance(settings["SHEET_NAME"], str) or not settings["SHEET_NAME"].strip():
        raise AWXError("SHEET_NAME must be a nonempty worksheet name.")
    if type(settings["BATCH_SIZE"]) is not int or settings["BATCH_SIZE"] < 1:
        raise AWXError("BATCH_SIZE must be a positive integer.")
    scope = settings["EXECUTION_SCOPE"]
    if scope is None:
        scope = 'ask'
    if not isinstance(scope, str) or scope.strip().casefold() not in ('ask', 'missing', 'all', 'errors'):
        raise AWXError('EXECUTION_SCOPE must be ask, missing, all, or errors.')
    settings['EXECUTION_SCOPE'] = scope.strip().casefold()
    column = settings["EXECUTION_COLUMN"]
    if column is None:
        column = DEFAULT_EXECUTION_COLUMN
    reserved = {name.casefold() for name in (*METADATA_COLUMNS, *ARTIFACT_COLUMNS)} | {"output"}
    if not isinstance(column, str) or not column.strip() or column.strip().casefold() in reserved:
        raise AWXError("EXECUTION_COLUMN must name a separate output column.")
    settings["EXECUTION_COLUMN"] = normalize_execution_column(column)
    if not isinstance(settings["JOB_TYPE"], str):
        raise AWXError("JOB_TYPE must be a string, e.g. install, reinstall, or update.")
    settings["JOB_TYPE"] = settings["JOB_TYPE"].strip()
    variable = settings["JOB_TYPE_VARIABLE"]
    if not isinstance(variable, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
        raise AWXError("JOB_TYPE_VARIABLE must be a valid playbook variable name, e.g. action.")
    if type(settings["WAIT_FOR_COMPLETION"]) is not bool:
        raise AWXError("WAIT_FOR_COMPLETION must be True or False.")
    for name in ("POLL_INTERVAL", "JOB_WAIT_TIMEOUT"):
        value = settings[name]
        if type(value) not in (int, float) or value <= 0 or not math.isfinite(value):
            raise AWXError(f"{name} must be a positive number of seconds.")
    return settings


def read_output_settings(settings_file=None):
    settings = group_settings(OUTPUT_KEYS, settings_file, "output")
    settings["EXCEL_COLUMNS"] = normalize_excel_columns(settings["EXCEL_COLUMNS"])
    for name in ("INPUT_FILE", "SHEET_NAME", "EXECUTION_COLUMN", "OUTPUT_COLUMN", "OUTPUT_DIRECTORY"):
        value = settings[name]
        if value is None and name in ("INPUT_FILE", "SHEET_NAME", "EXECUTION_COLUMN"):
            continue
        if not isinstance(value, str) or not value.strip():
            raise AWXError(f"{name} must be a nonempty string.")
    settings["OUTPUT_COLUMN"] = settings["OUTPUT_COLUMN"].strip()
    if settings['EXECUTION_COLUMN'] is not None:
        settings['EXECUTION_COLUMN'] = normalize_execution_column(settings['EXECUTION_COLUMN'])
    if settings["OUTPUT_COLUMN"].casefold() in {name.casefold() for name in (*METADATA_COLUMNS, *ARTIFACT_COLUMNS)}:
        raise AWXError("OUTPUT_COLUMN must name a separate output column.")
    if type(settings["WAIT_FOR_COMPLETION"]) is not bool:
        raise AWXError("WAIT_FOR_COMPLETION must be True or False.")
    for name in ("POLL_INTERVAL", "JOB_WAIT_TIMEOUT"):
        value = settings[name]
        if type(value) not in (int, float) or value <= 0 or not math.isfinite(value):
            raise AWXError(f"{name} must be a positive number of seconds.")
    return settings


def read_runtime_settings():
    settings = group_settings(RUNTIME_KEYS, None, "execute")
    color = "auto" if settings["COLOR"] is None else settings["COLOR"]
    if not isinstance(color, str) or color.strip().lower() not in ("auto", "always", "never"):
        raise AWXError("COLOR must be auto, always, or never.")
    settings["COLOR"] = color.strip().lower()
    for name in ("DEBUG", "DRY_RUN"):
        if type(settings[name]) is not bool:
            raise AWXError(f"{name} must be True or False.")
    column = settings["HOST_COLUMN"]
    if column is not None and (not isinstance(column, str) or not column.strip()):
        raise AWXError("HOST_COLUMN must be a nonempty header name or None.")
    return settings


def apply_job_type(variables, cell_value, settings, override=None):
    variable = settings["JOB_TYPE_VARIABLE"]
    selected = override if override is not None else (
        cell_value if cell_value is not None and str(cell_value).strip() else settings["JOB_TYPE"]
    )
    if not selected:
        selected = variables.get(variable, "")
    if not isinstance(selected, str):
        raise AWXError(f"Job_Type and Job_Variables[{variable!r}] must be strings.")
    selected = selected.strip()
    if selected:
        variables[variable] = selected
    return selected


def job_template_url(settings, fallback='', *, override=None):
    """One template URL priority for export, refresh, surveys and execution."""
    selected = settings.get('JOB_URL', '') if override is None else override
    return (selected or fallback or '').strip()
