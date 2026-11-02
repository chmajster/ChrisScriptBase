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

from . import awx_config as common
from . import awx_excel as excel
from .awx_config import AWXError, apply_job_type
from .awx_excel import ensure_columns
from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
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

    def __init__(self, token):
        super().__init__('[%(levelname)s] %(message)s')
        self.token = token

    def format(self, record):
        message = super().format(record)
        return message.replace(self.token, '***') if self.token else message

def configure_debug(enabled, token=''):
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(DebugFormatter(token))
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


def operation_parser(description, *, with_sheet=True):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("-file", "--file", dest="input_file", type=Path, help="Input Excel workbook.")
    if with_sheet:
        parser.add_argument("--sheet", help="Worksheet name; otherwise configured sheet or active worksheet.")
    parser.add_argument("-d", "-debug", "--debug", action="store_true", default=None, help="Show API request details.")
    parser.add_argument("--no-debug", dest="debug", action="store_false", default=None, help="Disable debug logging set in variables.")
    return parser


def parse_operation_arguments(parser, argv=None):
    """Fill unset CLI options from the same launcher/file configuration."""
    args = parser.parse_args(argv)
    defaults = common.read_runtime_settings()
    if args.debug is None:
        args.debug = defaults["DEBUG"]
    if hasattr(args, "dry_run") and args.dry_run is None:
        args.dry_run = defaults["DRY_RUN"]
    if hasattr(args, "host_column") and args.host_column is None:
        args.host_column = defaults["HOST_COLUMN"]
    return args


def parse_inventory_arguments(argv=None):
    return parse_operation_arguments(operation_parser("Fetch AWX hosts and export the results to Excel.", with_sheet=False), argv)

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
            raise AWXError('The API returned a link to a different server than AWX_URL.')
        if not parsed.path.startswith(self.api_path):
            for prefix in ('/api/controller/v2/', '/controller/v2/', '/api/v2/'):
                if parsed.path.casefold().startswith(prefix):
                    new_path = self.api_path + parsed.path[len(prefix):]
                    LOGGER.debug('API path mapping: %s -> %s', parsed.path, new_path)
                    return urlunsplit((parsed.scheme, parsed.netloc, new_path, parsed.query, ''))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ''))

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
                detail = exc.read(8192).decode("utf-8", errors="replace").replace(self.token, "***") if method == "POST" else ""
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
            detail = str(exc).replace(self.token, "***")
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

def build_inventory_rows(report, settings=None, output_column=None):
    """Build shared output rows and add extra and full credentials to JSON host records."""
    settings = common.read_execution_settings() if settings is None else settings
    output_column = common.read_output_settings()['OUTPUT_COLUMN'] if output_column is None else output_column
    primary_variables = credential_variable_names()
    extra_credentials = extra_credential_values()
    if not isinstance(FULL_CREDENTIAL_SEPARATOR, str):
        raise AWXError('FULL_CREDENTIAL_SEPARATOR must be a string.')
    headers = excel.headers(settings['EXECUTION_COLUMN'], output_column)
    extra_text = FULL_CREDENTIAL_SEPARATOR.join(extra_credentials) or 'none'
    rows = []
    for host in report['hosts']:
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
        full_parts = list(extra_credentials)
        for variable in primary_variables:
            full_parts.extend(full_values_by_variable.get(variable, []))
        full_credential = FULL_CREDENTIAL_SEPARATOR.join(dict.fromkeys(full_parts)) or 'none'
        host['Extra_Credentials'] = list(extra_credentials)
        host['Full_Credential'] = full_credential
        LOGGER.debug('Host %s: adding %d literal credential values from configuration', host['name'], len(extra_credentials))
        debug_variables(f"Full_Credential for host {host['name']!r}", full_credential)
        ostype = host.get('ostype')
        ostype_text = 'none' if ostype is None else ostype if isinstance(ostype, str) else json.dumps(ostype, ensure_ascii=False)
        job = OS_JOBS.get(ostype.strip().casefold(), {}) if isinstance(ostype, str) else {}
        job_link = job.get('url', '').strip()
        job_variables = deepcopy(job.get('variables', {}))
        host['Job_Link'] = job_link
        host['Job_Variables'] = job_variables
        host['INVENTORY'] = host.get('INVENTORY') or report.get('inventory', {}).get('name') or 'none'
        credentials = list(dict.fromkeys((value for variable in primary_variables for value in values_by_variable.get(variable, []))))
        job_type = common.apply_job_type(job_variables, None, settings)
        host['Job_Type'] = job_type
        host['AWX_Status'] = 'found'
        values = {'hostname': host['name'], 'INVENTORY': host['INVENTORY'], 'ostype': ostype_text, 'Job_Link': job_link or 'none', 'Job_Variables': json.dumps(job_variables, ensure_ascii=False), 'Extra_Credentials': extra_text, 'Credential': FULL_CREDENTIAL_SEPARATOR.join(credentials) or 'none', 'Full_Credential': full_credential, 'Job_Type': job_type, 'AWX_Status': 'found'}
        rows.append([values.get(name, '') for name in headers])
    return (headers, rows)

def save_inventory_excel(headers, rows, output_file, settings=None, output_column=None):
    """Create the same complete, styled workbook used by every AWX script."""
    settings = common.read_execution_settings() if settings is None else settings
    output_column = common.read_output_settings()['OUTPUT_COLUMN'] if output_column is None else output_column
    workbook = excel.create_workbook(headers, rows, sheet_name=settings['SHEET_NAME'], execution_column=settings['EXECUTION_COLUMN'], output_column=output_column)
    try:
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
            print(f"Missing hosts ({len(missing)}): {details}", file=sys.stderr)
        workflow = common.read_execution_settings()
        output_column = common.read_output_settings()['OUTPUT_COLUMN']
        headers, rows = build_inventory_rows(report, workflow, output_column)
        for row in rows:
            print(' , '.join((f"{header} : {value}" for header, value in zip(headers, row))))
        sys.stdout.flush()
        LOGGER.debug('JSON export: %d hosts, file %s', report['host_count'], OUTPUT_FILE)
        OUTPUT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        save_inventory_excel(headers, rows, EXCEL_OUTPUT_FILE, workflow, output_column)
        debug_variables('Saved results', {'json': OUTPUT_FILE.resolve(), 'excel': EXCEL_OUTPUT_FILE.resolve(), 'rows': len(rows)})
        return 0
    finally:
        LOGGER.debug('Total API requests: %d', client.request_count if client is not None else 0)


# RUNNER WORKFLOW

EXECUTION_SETTINGS_FILE = common.EXECUTION_SETTINGS_FILE
EXECUTION_DEFAULTS = common.EXECUTION_DEFAULTS
HOSTNAME_PATTERN = re.compile('[A-Za-z0-9_][A-Za-z0-9_.-]*\\Z')
TEMPLATE_PATTERN = re.compile('(?:^|/)(?:job_templates|job_template|job-template)/([1-9][0-9]*)(?:/|$)')

def read_execution_settings(settings_file=None):
    return common.read_execution_settings(settings_file)

def parse_execute_arguments(argv=None):
    parser = operation_parser("Launch AWX jobs for batches of Excel hosts and save job links.")
    parser.add_argument("--batch-size", type=int, help="Maximum unique hosts in one job (default: BATCH_SIZE).")
    parser.add_argument("--job-type", help="Override the action for new jobs, e.g. install, reinstall, update.")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=None, help="Show batches without API requests or file changes.")
    return parse_operation_arguments(parser, argv)

def resource_id_from_link(link, client, pattern, label):
    """Validate resource links consistently before using their IDs with the API."""
    parsed = urlsplit(link)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password:
        raise AWXError(f"{label} must be an HTTP(S) link.")
    if client._origin(parsed) != client.origin:
        raise AWXError(f"{label} points to a different server than AWX_URL.")
    match = pattern.search(parsed.fragment or parsed.path)
    if not match:
        raise AWXError(f"Cannot read a resource ID from {label}: {link}")
    return int(match.group(1))


def template_id_from_link(link, client):
    return resource_id_from_link(link, client, TEMPLATE_PATTERN, "Job_Link")

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

def parse_credentials(value, row_number):
    """Read credential references using the separator used by the inventory export."""
    if value is None or str(value).strip().casefold() in ('', 'none'):
        return ()
    separator = FULL_CREDENTIAL_SEPARATOR
    if not separator:
        raise AWXError('FULL_CREDENTIAL_SEPARATOR cannot be empty when reading Full_Credential.')
    references = [part.strip() for part in str(value).split(separator.strip() or separator)]
    if any((not reference or reference.casefold() == 'none' for reference in references)):
        raise AWXError(f"Row {row_number}: Full_Credential contains an empty or invalid credential reference.")
    return tuple(sorted(set(references)))

def build_execution_plan(sheet, client, settings, job_type_override=None):
    """Validate the whole sheet, deduplicate hosts, and batch equal launch payloads."""
    columns = excel.read_columns(sheet)
    for name in ('hostname', 'job_link', 'job_variables', 'full_credential'):
        if name not in columns:
            raise AWXError(f"Missing {name} column. Generate a new Excel export with awx_inventory_hosts.py.")
    execution_column = columns.get(settings['EXECUTION_COLUMN'].casefold(), sheet.max_column + 1)
    hosts = {}
    skipped_without_job = 0
    for row_number in range(2, sheet.max_row + 1):
        host_value = sheet.cell(row_number, columns['hostname']).value
        if host_value is None or not str(host_value).strip():
            continue
        hostname = str(host_value).strip()
        if not HOSTNAME_PATTERN.fullmatch(hostname):
            raise AWXError(f"Row {row_number}: hostname must be a single literal hostname, without Ansible patterns.")
        link_cell = sheet.cell(row_number, columns['job_link'])
        link = str(link_cell.value or '').strip()
        if link.casefold() in ('', 'none'):
            skipped_without_job += 1
            continue
        if link_cell.data_type == 'f':
            raise AWXError(f"Row {row_number}: Job_Link must contain a URL rather than an Excel formula.")
        if link_cell.hyperlink and link_cell.hyperlink.target:
            link = link_cell.hyperlink.target
        template_id = template_id_from_link(link, client)
        variables = parse_variables(sheet.cell(row_number, columns['job_variables']).value, row_number)
        job_type_cell = sheet.cell(row_number, columns['job_type']) if 'job_type' in columns else None
        credential_cell = sheet.cell(row_number, columns['full_credential'])
        if credential_cell.data_type == 'f' or (job_type_cell is not None and job_type_cell.data_type == 'f'):
            raise AWXError(f"Row {row_number}: Full_Credential and Job_Type must contain literal values.")
        job_type = apply_job_type(variables, job_type_cell.value if job_type_cell else None, settings, job_type_override)
        credential_refs = parse_credentials(credential_cell.value, row_number)
        key = (template_id, json.dumps(variables, sort_keys=True, ensure_ascii=False), credential_refs)
        execution = str(sheet.cell(row_number, execution_column).value or '').strip()
        if hostname not in hosts:
            hosts[hostname] = {'key': key, 'template_id': template_id, 'link': link, 'variables': variables, 'credential_refs': credential_refs, 'job_type': job_type, 'hostname': hostname, 'rows': [], 'execution': ''}
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
            batches.append({'template_id': entries[0]['template_id'], 'link': entries[0]['link'], 'variables': entries[0]['variables'], 'entries': entries, 'credential_refs': entries[0]['credential_refs'], 'job_type': entries[0]['job_type'], 'limit': ','.join((host['hostname'] for host in entries))})
    return (batches, existing, execution_column, skipped_without_job)

def write_execution(sheet, column, entries, value):
    for entry in entries:
        for row_number in entry['rows']:
            cell = sheet.cell(row_number, column)
            cell.value = value
            cell.data_type = 's'
            cell.number_format = '@'
            cell.hyperlink = value if value.startswith(('http://', 'https://')) else None



def resolve_credentials(client, batches):
    """Resolve credential names/IDs once and validate each selected credential set."""
    cache = {}
    for batch in batches:
        selected = {}
        for reference in batch['credential_refs']:
            if reference not in cache:
                if re.fullmatch('[0-9]+', reference):
                    credential = client.get(f"credentials/{int(reference)}/", purpose=f"Resolve credential ID {reference}")
                else:
                    matches = list(client.items('credentials/', name=reference, order_by='id', purpose=f"Resolve credential named {reference!r}"))
                    if not matches:
                        raise AWXError(f"Credential {reference!r} from Full_Credential was not found or is not accessible.")
                    if len(matches) != 1:
                        ids = ', '.join((str(match['id']) for match in matches))
                        raise AWXError(f"Credential name {reference!r} is ambiguous (IDs: {ids}); use its numeric ID in Full_Credential.")
                    credential = matches[0]
                if type(credential.get('id')) is not int or credential['id'] < 1:
                    raise AWXError(f"Invalid AWX credential response for {reference!r}.")
                cache[reference] = credential
                cache[str(credential['id'])] = credential
            credential = cache[reference]
            selected[credential['id']] = credential
        identities = {}
        for credential in selected.values():
            inputs = credential.get('inputs') or {}
            if any((inputs.get(name) == 'ASK' for name in ('password', 'become_password', 'ssh_key_unlock', 'vault_password'))):
                raise AWXError(f"Credential {credential['id']} requires an interactive password; configure it in AWX.")
            credential_type = credential.get('credential_type')
            kind = credential.get('kind') or (credential.get('summary_fields') or {}).get('credential_type', {}).get('kind')
            identity = (credential_type, inputs.get('vault_id') if kind == 'vault' else None)
            if credential_type is not None and identity in identities:
                raise AWXError(f"Full_Credential contains conflicting credentials {identities[identity]} and {credential['id']} of the same credential type. Select one credential per type (Vault IDs may differ).")
            identities[identity] = credential['id']
        batch['credentials'] = sorted(selected)
        batch['credential_types'] = {credential.get('credential_type') for credential in selected.values()}

def validate_templates(client, batches):
    """Check prompting and required inputs before launching the first batch."""
    cache = {}
    for batch in batches:
        template_id = batch['template_id']
        if template_id not in cache:
            details = client.get(f"job_templates/{template_id}/launch/", purpose=f"Check launch settings for template {template_id}")
            if not details.get('ask_limit_on_launch'):
                raise AWXError(f"Template {template_id}: enable Prompt on launch for Limit to restrict each job to its batch.")
            if (details.get('defaults', {}).get('job_slice_count') or 1) > 1:
                raise AWXError(f"Template {template_id}: set Job Slicing to 1; this script creates its own host batches.")
            if details.get('inventory_needed_to_start'):
                raise AWXError(f"Template {template_id}: configure its required inventory in AWX.")
            cache[template_id] = (details, None)
        details, survey_variables = cache[template_id]
        if batch['credentials']:
            if not details.get('ask_credential_on_launch'):
                raise AWXError(f"Template {template_id}: enable Prompt on launch for Credentials to accept Full_Credential.")
            for default in details.get('defaults', {}).get('credentials', []):
                if default['id'] not in batch['credentials'] and default.get('credential_type') not in batch['credential_types']:
                    raise AWXError(f"Template {template_id}: Full_Credential must include default credential {default['id']} or a replacement of the same credential type.")
        elif details.get('passwords_needed_to_start') or details.get('credential_needed_to_start'):
            raise AWXError(f"Template {template_id}: credentials require interactive passwords or are missing; fill Full_Credential.")
        variables = batch['variables']
        if variables and (not details.get('ask_variables_on_launch')):
            if not details.get('survey_enabled'):
                raise AWXError(f"Template {template_id}: enable Prompt on launch for Variables to accept Job_Variables.")
            if survey_variables is None:
                survey = client.get(f"job_templates/{template_id}/survey_spec/", purpose=f"Check survey variables for template {template_id}")
                survey_variables = {item['variable'] for item in survey.get('spec', [])}
                cache[template_id] = (details, survey_variables)
            unsupported = set(variables) - survey_variables
            if unsupported:
                raise AWXError(f"Template {template_id}: variables outside its survey: {', '.join(sorted(unsupported))}.")
        missing = [name for name in details.get('variables_needed_to_start', []) if name not in variables]
        if missing:
            raise AWXError(f"Template {template_id}: missing required Job_Variables: {', '.join(missing)}.")

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
            print(f"Job {job_id}: {status}", flush=True)
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
    job_type_column = excel.read_columns(sheet)['job_type']
    output_column = settings.get('OUTPUT_COLUMN', 'output')
    for entry in existing:
        write_execution(sheet, execution_column, [entry], entry['execution'])
    excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=output_column)
    for index, batch in enumerate(batches, start=1):
        template_id = batch['template_id']
        print(f"Batch {index}/{len(batches)}: template={template_id}; job_type={batch['job_type'] or 'none'}; credentials={batch['credentials']}; hosts={batch['limit']}", flush=True)
        for entry in batch['entries']:
            for row_number in entry['rows']:
                cell = sheet.cell(row_number, job_type_column)
                cell.value = batch['job_type']
                cell.data_type = 's'
                cell.number_format = '@'
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
            print(f"Job {job_id}: {link}", flush=True)
            write_execution(sheet, execution_column, batch['entries'], link)
            excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=output_column)
            if result.get('ignored_fields'):
                raise AWXError(f"Job {job_id}: AWX ignored launch fields {', '.join(result['ignored_fields'])}; stopped further launches.")
            if settings['WAIT_FOR_COMPLETION']:
                wait_for_job(client, job_id, settings)
        except BaseException:
            print('Stopped. Check the saved execution links/markers and AWX before retrying the affected batch.', file=sys.stderr)
            raise

def execute_main(argv=None):
    args = parse_execute_arguments(argv)
    if args.job_type is not None and (not args.job_type.strip()):
        raise AWXError('--job-type must be nonempty, e.g. install, reinstall, or update.')
    connection = load_connection(args.debug)
    settings = read_execution_settings()
    settings['OUTPUT_COLUMN'] = common.read_output_settings()['OUTPUT_COLUMN']
    if args.batch_size is not None:
        if args.batch_size < 1:
            raise AWXError('--batch-size must be positive.')
        settings['BATCH_SIZE'] = args.batch_size
    path = workbook_path(args.input_file, connection, settings)
    client = AWXClient(connection['AWX_URL'], connection['AWX_TOKEN'].strip())
    workbook, sheet = open_workbook_sheet(path, settings["SHEET_NAME"], args.sheet)
    try:
        excel.prepare_sheet(sheet, settings['EXECUTION_COLUMN'], settings['OUTPUT_COLUMN'])
        excel.sort_host_rows(sheet)
        batches, existing, column, skipped = build_execution_plan(sheet, client, settings, job_type_override=args.job_type)
        print(f"Input: {path}; batch size: {settings['BATCH_SIZE']}; batches: {len(batches)}; already recorded hosts: {len(existing)}; rows without job: {skipped}", flush=True)
        if args.dry_run:
            for index, batch in enumerate(batches, start=1):
                print(f"Batch {index}: template={batch['template_id']}; limit={batch['limit']}; credentials={list(batch['credential_refs'])}; job_type={batch['job_type'] or 'none'}; extra_vars={json.dumps(batch['variables'], ensure_ascii=False)}")
            print('Dry run: no jobs launched and no workbook changes.')
            return 0
        if any((entry['execution'].startswith('LAUNCH_PENDING') for entry in existing)):
            raise AWXError('An earlier launch has a LAUNCH_PENDING marker. Check AWX, then replace it with the job link or clear it only if no job was launched.')
        if not batches:
            excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=settings['OUTPUT_COLUMN'])
            print('No new batches to launch.')
            return 0
        resolve_credentials(client, batches)
        validate_templates(client, batches)
        if settings['WAIT_FOR_COMPLETION']:
            recorded_jobs = set()
            for entry in existing:
                parsed = urlsplit(entry['execution'])
                match = re.search('/jobs/(?:playbook/)?([1-9][0-9]*)(?:/|$)', parsed.fragment or parsed.path)
                if match and client._origin(parsed) == client.origin:
                    recorded_jobs.add(int(match.group(1)))
            for job_id in sorted(recorded_jobs):
                wait_for_job(client, job_id, settings)
        execute_plan(workbook, sheet, path, client, settings, batches, existing, column)
        print(f"Saved job links to {settings['EXECUTION_COLUMN']} in {path}.")
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
    parser = operation_parser("Download AWX job stdout to Excel and text files.")
    parser.add_argument("--output-dir", type=Path, help="Directory for <job_id>.<inventory>.<os>.txt files.")
    return parse_operation_arguments(parser, argv)

def job_id_from_link(link, client):
    return resource_id_from_link(link, client, JOB_PATTERN, "Execution link")

def build_output_plan(sheet, client, settings):
    columns = excel.read_columns(sheet)
    execution_name = settings['EXECUTION_COLUMN'].strip().casefold()
    output_name = settings['OUTPUT_COLUMN'].strip().casefold()
    if output_name in (execution_name, 'hostname', 'ostype', 'job_link', 'job_variables', 'extra_credentials', 'credential', 'full_credential'):
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

def write_output(sheet, column, row_number, content, text_path):
    text = ILLEGAL_CHARACTERS_RE.sub('', content)
    if len(text) > EXCEL_CELL_LIMIT:
        suffix = f"\n[Output truncated in Excel; full log: {text_path}]"
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
        try:
            job = read_job(client, job_id, settings)
            name = filename_part(job_inventory_name(client, job, inventory_names))
            content = client.stdout(job_id)
            files = {}
            for entry in entries:
                os_name = filename_part(entry['ostype'])
                if os_name not in files:
                    text_path = output_directory / f"{job_id}.{name}.{os_name}.txt"
                    save_text(text_path, content)
                    files[os_name] = text_path
                    print(f"Job {job_id}: {text_path}", flush=True)
            for entry in entries:
                write_output(sheet, column, entry['row'], content, files[filename_part(entry['ostype'])])
            excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=settings['OUTPUT_COLUMN'])
            completed += 1
        except AWXError as exc:
            failures += 1
            print(f"Error: {exc}", file=sys.stderr)
    print(f"Collected: {completed} jobs; errors: {failures}; workbook: {path}", flush=True)
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
        print(f"Input: {path}; unique jobs: {len(jobs)}; rows without a job link: {skipped}", flush=True)
        if not jobs:
            excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=settings['OUTPUT_COLUMN'])
            print('No job output to collect.')
            return 0
        return collect_outputs(workbook, sheet, path, output_directory, client, settings, jobs, column)
    finally:
        workbook.close()


# REFRESH WORKFLOW

HOST_COLUMNS = excel.HOST_COLUMNS

def parse_refresh_arguments(argv=None):
    parser = operation_parser("Read Excel hostnames and refresh AWX metadata in the same file.")
    parser.add_argument("--host-column", help="Host column header; detected automatically if omitted.")
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
        print('Added missing hostname column. Fill it with hostnames or use --host-column HEADER for an existing column.', flush=True)
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

def metadata_by_host(report, settings):
    """Use exactly the same host records as the inventory generator."""
    headers, rows = build_inventory_rows(report, settings, settings.get('OUTPUT_COLUMN', 'output'))
    metadata = {}
    for row in rows:
        record = dict(zip(headers, row))
        hostname = record['hostname']
        metadata[hostname] = {name: record[name] for name in excel.METADATA_COLUMNS if name != 'hostname'}
    return (metadata, list(excel.METADATA_COLUMNS[1:]))

def refresh_sheet(sheet, host_rows, columns, source_column, metadata, headers, execution_column='exceiute_job', preserve_unidentified_rows=False, output_column='output'):
    missing_values = {name: 'none' for name in headers}
    missing_values.update(Job_Variables='{}', Job_Type='', AWX_Status='not_found')
    repaired, added = excel.prepare_sheet(sheet, execution_column, output_column, reorder=False)
    columns.clear()
    columns.update(repaired)
    if added:
        print(f"Added missing columns: {', '.join(added)}", flush=True)
    for row_number in range(2, sheet.max_row + 1):
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
            cell.value = value
            cell.data_type = 's'
            cell.number_format = '@'
            cell.hyperlink = value if name == 'Job_Link' and value.startswith(('http://', 'https://')) else None
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
        print(f"Input: {path}; worksheet: {sheet_name}; unique hosts: {len(hostnames)}", flush=True)
        if hostnames:
            client = AWXClient(connection['AWX_URL'], connection['AWX_TOKEN'].strip())
            report = fetch_report(client, connection['INVENTORY'], hostnames=hostnames, allow_empty=True)
        else:
            print('No hostnames in Excel. Updating column headers without querying AWX.', flush=True)
            report = {'hosts': [], 'host_count': 0, 'missing_hosts': []}
        metadata, headers = metadata_by_host(report, settings)
        protected = {settings['EXECUTION_COLUMN'].casefold(), 'exceiute_job', settings['OUTPUT_COLUMN'].casefold()}
        managed = {name.casefold() for name in ['hostname', *headers]}
        if protected & managed or columns.get(settings['EXECUTION_COLUMN'].casefold()) == source_column or columns.get(settings['OUTPUT_COLUMN'].casefold()) == source_column:
            raise AWXError('The host or metadata column conflicts with a job history column.')
        refresh_sheet(sheet, host_rows, columns, source_column, metadata, headers, execution_column=settings['EXECUTION_COLUMN'], preserve_unidentified_rows=not had_host_column, output_column=settings['OUTPUT_COLUMN'])
        excel.save_workbook(workbook, path, sheet=sheet, execution_column=settings['EXECUTION_COLUMN'], output_column=settings['OUTPUT_COLUMN'])
        missing = report.get('missing_hosts', [])
        if missing:
            print(f"Not found in AWX ({len(missing)}): {', '.join(missing)}", file=sys.stderr)
        print(f"Refreshed: {report['host_count']} unique hosts; not found: {len(missing)}; saved: {path}", flush=True)
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
    try:
        if operation is None:
            parser = argparse.ArgumentParser(description="AWX inventory, Excel refresh, job execution and output.")
            parser.add_argument("operation", choices=OPERATIONS)
            arguments = list(sys.argv[1:] if argv is None else argv)
            selected = parser.parse_args(arguments[:1] if arguments and arguments[0] in OPERATIONS else arguments)
            operation = selected.operation
            argv = arguments[1:]
        if operation not in OPERATIONS:
            raise AWXError(f"Unknown AWX operation: {operation}")
        with common.launcher_configuration(operation, variables, variables_file):
            return OPERATIONS[operation](argv)
    except (AWXError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Check saved execution links/markers before retrying a launch.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(run())
