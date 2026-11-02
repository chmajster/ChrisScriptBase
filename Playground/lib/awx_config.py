"""One source of AWX variables and isolated per-launcher overrides.

Priority: command-line arguments > launcher VARIABLES > per-operation overrides
in awx_variable > shared values in awx_variable. Config files contain literals.
"""

import ast
import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path


class AWXError(RuntimeError):
    """An API, configuration, workbook, or input error."""


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VARIABLES_FILE = PROJECT_ROOT / "awx_variable"
EXECUTION_SETTINGS_FILE = VARIABLES_FILE
OUTPUT_SETTINGS_FILE = VARIABLES_FILE
METADATA_COLUMNS = (
    "hostname", "INVENTORY", "ostype", "Job_Link", "Job_Variables", "Extra_Credentials",
    "Credential", "Full_Credential", "Job_Type", "AWX_Status",
)
INVENTORY_KEYS = (
    "AWX_URL", "AWX_TOKEN", "INVENTORY", "CREDENTIAL_VARIABLES", "EXTRA_CREDENTIAL_VARIABLES",
    "FULL_CREDENTIAL_SEPARATOR", "OUTPUT_FILE", "EXCEL_OUTPUT_FILE", "TIMEOUT", "PAGE_SIZE", "OS_JOBS",
)
EXECUTION_KEYS = (
    "INPUT_FILE", "SHEET_NAME", "BATCH_SIZE", "EXECUTION_COLUMN", "JOB_TYPE", "JOB_TYPE_VARIABLE",
    "WAIT_FOR_COMPLETION", "POLL_INTERVAL", "JOB_WAIT_TIMEOUT",
)
OUTPUT_KEYS = (
    "INPUT_FILE", "SHEET_NAME", "EXECUTION_COLUMN", "OUTPUT_COLUMN", "OUTPUT_DIRECTORY",
    "WAIT_FOR_COMPLETION", "POLL_INTERVAL", "JOB_WAIT_TIMEOUT",
)
RUNTIME_KEYS = ("DEBUG", "DRY_RUN", "HOST_COLUMN")
SETTING_NAMES = frozenset((*INVENTORY_KEYS, *EXECUTION_KEYS, *OUTPUT_KEYS, *RUNTIME_KEYS))
OPERATION_SCOPES = {
    "inventory": "INVENTORY_OVERRIDES",
    "refresh": "REFRESH_OVERRIDES",
    "execute": "EXECUTE_OVERRIDES",
    "output": "OUTPUT_OVERRIDES",
}
_ACTIVE = ContextVar("awx_launcher_configuration", default=None)


def read_literal_settings(path, defaults, *, required=False):
    """Read literal NAME = value assignments; never execute configuration files."""
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
        tree = ast.parse(content, filename=str(path))
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
            settings[name] = ast.literal_eval(node.value)
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
        result[key] = deepcopy(value)
    return result


def merge_values(base, overrides):
    """Merge nested OS_JOBS overrides while keeping independent launcher settings."""
    result = deepcopy(base)
    for name, value in overrides.items():
        result[name] = merge_values(result[name], value) if isinstance(result.get(name), dict) and isinstance(value, dict) else deepcopy(value)
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


def read_execution_settings(settings_file=None):
    settings = group_settings(EXECUTION_KEYS, settings_file, "execute")
    if settings["INPUT_FILE"] is not None and (
        not isinstance(settings["INPUT_FILE"], str) or not settings["INPUT_FILE"].strip()
    ):
        raise AWXError("INPUT_FILE must be a file path or None.")
    if not isinstance(settings["SHEET_NAME"], str) or not settings["SHEET_NAME"].strip():
        raise AWXError("SHEET_NAME must be a nonempty worksheet name.")
    if type(settings["BATCH_SIZE"]) is not int or settings["BATCH_SIZE"] < 1:
        raise AWXError("BATCH_SIZE must be a positive integer.")
    column = settings["EXECUTION_COLUMN"]
    reserved = {name.casefold() for name in METADATA_COLUMNS} | {"output"}
    if not isinstance(column, str) or not column.strip() or column.strip().casefold() in reserved:
        raise AWXError("EXECUTION_COLUMN must name a separate output column.")
    settings["EXECUTION_COLUMN"] = column.strip()
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
    for name in ("INPUT_FILE", "SHEET_NAME", "EXECUTION_COLUMN", "OUTPUT_COLUMN", "OUTPUT_DIRECTORY"):
        value = settings[name]
        if value is None and name in ("INPUT_FILE", "SHEET_NAME", "EXECUTION_COLUMN"):
            continue
        if not isinstance(value, str) or not value.strip():
            raise AWXError(f"{name} must be a nonempty string.")
    settings["OUTPUT_COLUMN"] = settings["OUTPUT_COLUMN"].strip()
    if settings["OUTPUT_COLUMN"].casefold() in {name.casefold() for name in METADATA_COLUMNS}:
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
