#!/usr/bin/env python3
"""Fetch AWX hosts and group credentials, then add configured credential values.

Requires Python 3.9+ and openpyxl.
Install the dependency: python -m pip install openpyxl
Groups, their variables, and memberships are fetched in bulk for each inventory.

Run: python awx_inventory_hosts.py
Hosts from Excel: python awx_inventory_hosts.py -file input.xlsx
Debug logging: python awx_inventory_hosts.py -file input.xlsx -d
Input file: a host_limit column containing hostnames from the selected inventory.
Settings: awx_invenotry_hosts_variables next to the script, using NAME = value.
If the file is missing, the script uses the defaults defined below.
AWX_URL: a server address or full API base URL, e.g. https://server/api/controller/v2/.
API: https://docs.ansible.com/projects/awx/en/latest/open_api/explorer.html
"""

import argparse
import ast
import json
import logging
import math
import re
import sys
import time
from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.utils.exceptions import InvalidFileException
except ImportError:
    raise SystemExit(
        "The openpyxl package is missing. Install it with: python -m pip install openpyxl"
    ) from None


# ======================== DEFAULT SETTINGS =======================
# VARIABLES_FILE overrides these values; defaults apply when the file is missing.
# Server address or full API base URL, e.g. "https://server/api/controller/v2/".
AWX_URL = "http://10.0.0.204:30080"
AWX_TOKEN = 'XhUJQQAbseBOUzqr0sG24649GSahs6'
VARIABLES_FILE = Path(__file__).resolve().with_name("awx_invenotry_hosts_variables")
INVENTORY = "it2_inventory"  # Inventory name or numeric ID, e.g. 7.
# Group variable names whose values appear in the Credential column.
CREDENTIAL_VARIABLES = ["os_credential"]
# Literal credential values added to every host before Credential.
EXTRA_CREDENTIAL_VARIABLES = ["xd","123"]  # E.g. ["value_1", "value_2"]
FULL_CREDENTIAL_SEPARATOR = ", "  # Literal extras first, then values from CREDENTIAL_VARIABLES.
OUTPUT_FILE = Path("awx_inventory_hosts.json")
EXCEL_OUTPUT_FILE = Path("awx_inventory_hosts.xlsx")
TIMEOUT = 30  # Timeout for a single request, in seconds.
PAGE_SIZE = 200
# =================================================================

DEFAULT_SETTINGS = {
    "AWX_URL": AWX_URL,
    "AWX_TOKEN": AWX_TOKEN,
    "INVENTORY": INVENTORY,
    "CREDENTIAL_VARIABLES": CREDENTIAL_VARIABLES,
    "EXTRA_CREDENTIAL_VARIABLES": EXTRA_CREDENTIAL_VARIABLES,
    "FULL_CREDENTIAL_SEPARATOR": FULL_CREDENTIAL_SEPARATOR,
    "OUTPUT_FILE": OUTPUT_FILE,
    "EXCEL_OUTPUT_FILE": EXCEL_OUTPUT_FILE,
    "TIMEOUT": TIMEOUT,
    "PAGE_SIZE": PAGE_SIZE,
}

LOGGER = logging.getLogger("awx_inventory_hosts")
LOGGER.addHandler(logging.NullHandler())
LOGGER.setLevel(logging.WARNING)
LOGGER.propagate = False


class DebugFormatter(logging.Formatter):
    """Redact the token, including any occurrences in API response data."""

    def __init__(self, token):
        super().__init__("[%(levelname)s] %(message)s")
        self.token = token

    def format(self, record):
        message = super().format(record)
        return message.replace(self.token, "***") if self.token else message


def configure_debug(enabled, token=""):
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(DebugFormatter(token))
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.DEBUG if enabled else logging.WARNING)


def debug_variables(label, values):
    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug("%s: %s", label, json.dumps(values, ensure_ascii=False, default=str))


class AWXError(RuntimeError):
    """An API, configuration, or input error."""


def validate_settings(settings):
    """Validate setting types and prepare output file paths."""
    for name in ("AWX_URL", "AWX_TOKEN"):
        if not isinstance(settings[name], str) or not settings[name].strip():
            raise AWXError(f"{name} must be a nonempty string.")
    inventory = settings["INVENTORY"]
    if not (
        isinstance(inventory, str) and inventory.strip()
        or type(inventory) is int and inventory > 0
    ):
        raise AWXError("INVENTORY must be a name or a positive numeric ID.")
    variables = settings["CREDENTIAL_VARIABLES"]
    if not isinstance(variables, (list, tuple)) or any(
        not isinstance(name, str) or not name.strip() for name in variables
    ):
        raise AWXError("CREDENTIAL_VARIABLES must be a list of nonempty variable names.")
    extras = settings["EXTRA_CREDENTIAL_VARIABLES"]
    if not isinstance(extras, (list, tuple)) or any(
        value is not None and not isinstance(value, (str, int, float, bool)) for value in extras
    ):
        raise AWXError("EXTRA_CREDENTIAL_VARIABLES must be a list of literal credential values.")
    if not isinstance(settings["FULL_CREDENTIAL_SEPARATOR"], str):
        raise AWXError("FULL_CREDENTIAL_SEPARATOR must be a string.")
    for name in ("OUTPUT_FILE", "EXCEL_OUTPUT_FILE"):
        value = settings[name]
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise AWXError(f"{name} must be a file path.")
        settings[name] = Path(value)
    timeout = settings["TIMEOUT"]
    if type(timeout) not in (int, float) or timeout <= 0 or (
        isinstance(timeout, float) and not math.isfinite(timeout)
    ):
        raise AWXError("TIMEOUT must be a positive number of seconds.")
    if type(settings["PAGE_SIZE"]) is not int or settings["PAGE_SIZE"] <= 0:
        raise AWXError("PAGE_SIZE must be a positive integer.")
    return settings


def read_settings(variables_file=None):
    """Override defaults with literal values read from the settings file."""
    variables_file = Path(variables_file) if variables_file is not None else VARIABLES_FILE
    settings = deepcopy(DEFAULT_SETTINGS)
    LOGGER.debug("Configuration: reading settings from %s", variables_file)
    try:
        content = variables_file.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        LOGGER.debug("Settings file %s is missing; using the defaults defined in the script", variables_file)
        return validate_settings(settings)
    except (OSError, UnicodeError):
        raise AWXError(f"Cannot read settings file {variables_file}. Check file access and UTF-8 encoding.") from None

    try:
        tree = ast.parse(content, filename=str(variables_file))
    except SyntaxError as exc:
        raise AWXError(f"Syntax error in {variables_file}, line {exc.lineno}. Use NAME = value.") from None
    seen = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise AWXError(f"File {variables_file}, line {node.lineno}: only NAME = value assignments are allowed.")
        name = node.targets[0].id.upper()
        if name not in DEFAULT_SETTINGS:
            raise AWXError(f"Unknown setting in {variables_file}, line {node.lineno}.")
        if name in seen:
            raise AWXError(f"Setting {name} is duplicated in {variables_file}.")
        seen.add(name)
        try:
            settings[name] = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError):
            raise AWXError(f"Setting {name}, line {node.lineno}: use a quoted string, a number, or a list.") from None
    LOGGER.debug("Loaded %d settings from the file; remaining settings use defaults", len(seen))
    return validate_settings(settings)


def apply_settings(settings):
    """Make validated settings available to the rest of the script."""
    globals().update({name: settings[name] for name in DEFAULT_SETTINGS})


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="Fetch hosts and credentials from AWX and export the results to Excel."
    )
    parser.add_argument(
        "-file", "--file", dest="input_file", type=Path, metavar="INPUT.xlsx",
        help="Use only hostnames from the host_limit column of the specified Excel file.",
    )
    parser.add_argument(
        "-d", "-debug", "--debug", action="store_true",
        help="Show variables, request purposes, responses, and the API request count on stderr.",
    )
    return parser.parse_args(argv)


def read_host_limits(input_file):
    """Read unique hostnames from host_limit columns, preserving file order."""
    LOGGER.debug("Excel input: opening %s and looking for the host_limit column", input_file)
    workbook = None
    hostnames = {}
    found_column = False
    try:
        workbook = load_workbook(input_file, read_only=True, data_only=True, keep_links=False)
        for sheet in workbook.worksheets:
            LOGGER.debug("Excel input: searching worksheet %r", sheet.title)
            column_index = None
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if column_index is None:
                    matching_columns = [
                        index for index, value in enumerate(row)
                        if isinstance(value, str) and value.strip().casefold() == "host_limit"
                    ]
                    if len(matching_columns) > 1:
                        raise AWXError(
                            f"Worksheet {sheet.title!r}, row {row_number}: "
                            "more than one host_limit column was found."
                        )
                    if matching_columns:
                        column_index = matching_columns[0]
                        found_column = True
                        LOGGER.debug(
                            "Excel input: found host_limit in worksheet %r, row %d, column %s",
                            sheet.title, row_number, get_column_letter(column_index + 1),
                        )
                    continue

                value = row[column_index] if column_index < len(row) else None
                if value is None:
                    continue
                for hostname in re.split(r"[,;\s]+", str(value).strip()):
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
    LOGGER.debug("Excel input: read %d unique hostnames", len(hostnames))
    debug_variables(f"Hostnames from {input_file}", list(hostnames))
    return list(hostnames)


class NoRedirect(HTTPRedirectHandler):
    """Do not forward the token header across HTTP redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AWXClient:
    def __init__(self, base_url, token):
        parsed = urlsplit(base_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise AWXError("AWX_URL must include http:// or https:// and a server address.")
        if parsed.query or parsed.fragment:
            raise AWXError("AWX_URL must point to a server or API base URL without query parameters or a # fragment.")

        path = parsed.path.rstrip("/")
        if path.casefold().endswith("/v2"):
            self.api_path = path + "/"
        elif path.casefold().endswith("/api"):
            self.api_path = path + "/v2/"
        else:
            self.api_path = path + "/api/v2/"
        self.base_url = urlunsplit((parsed.scheme, parsed.netloc, self.api_path, "", ""))
        self.origin = self._origin(parsed)
        self.token = token
        self.opener = build_opener(NoRedirect())
        self.request_count = 0
        LOGGER.debug("API base URL used for requests: %s", self.base_url)

    @staticmethod
    def _origin(parsed):
        try:
            port = parsed.port
        except ValueError:
            raise AWXError("The API URL contains an invalid port number.") from None
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        return (parsed.scheme, (parsed.hostname or "").casefold(), port)

    def _resolve_url(self, path, *, relative_to=None):
        """Preserve the configured API base URL in pagination links."""
        url = urljoin(relative_to or self.base_url, path)
        parsed = urlsplit(url)
        if self._origin(parsed) != self.origin:
            raise AWXError("The API returned a link to a different server than AWX_URL.")

        # Some proxies return the internal /api/v2/ prefix in next links.
        # Map those links to the API base URL configured by the user.
        if not parsed.path.startswith(self.api_path):
            for prefix in ("/api/controller/v2/", "/controller/v2/", "/api/v2/"):
                if parsed.path.casefold().startswith(prefix):
                    new_path = self.api_path + parsed.path[len(prefix):]
                    LOGGER.debug("API path mapping: %s -> %s", parsed.path, new_path)
                    return urlunsplit((parsed.scheme, parsed.netloc, new_path, parsed.query, ""))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))

    def get(self, path, *, purpose="Fetch an AWX resource"):
        url = self._resolve_url(path)
        parsed = urlsplit(url)

        request = Request(
            url,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            method="GET",
        )
        self.request_count += 1
        request_number = self.request_count
        started = time.perf_counter()
        LOGGER.debug("Request #%d purpose: %s", request_number, purpose)
        LOGGER.debug("Request #%d: GET %s", request_number, url)
        try:
            with self.opener.open(request, timeout=TIMEOUT) as response:
                status = getattr(response, "status", None)
                payload = json.load(response)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            LOGGER.debug(
                "Response #%d: HTTP %d error; elapsed %.3f s",
                request_number, code, time.perf_counter() - started,
            )
            hint = {
                401: "The token is invalid or has expired.",
                403: "The token does not have permission to read this resource.",
                404: "The resource does not exist or is not visible to this token.",
                301: "Check AWX_URL: the server is redirecting the request.",
                302: "Check AWX_URL: the server is redirecting the request.",
            }.get(code, "The AWX server returned an error.")
            raise AWXError(f"HTTP {code}, {parsed.path}: {hint}") from None
        except (URLError, OSError) as exc:
            LOGGER.debug(
                "Response #%d: connection error after %.3f s: %s",
                request_number, time.perf_counter() - started, exc,
            )
            raise AWXError(f"Cannot fetch {parsed.path}: {exc}") from None
        except (ValueError, UnicodeError):
            LOGGER.debug("Response #%d: invalid JSON", request_number)
            raise AWXError(f"The response from {parsed.path} is not valid JSON.") from None

        if not isinstance(payload, dict):
            LOGGER.debug("Response #%d: unexpected data type %s", request_number, type(payload).__name__)
            raise AWXError(f"Unexpected response format from {parsed.path}.")
        LOGGER.debug(
            "Response #%d: HTTP %s; elapsed %.3f s",
            request_number, status if status is not None else "unknown", time.perf_counter() - started,
        )
        if isinstance(payload.get("results"), list):
            LOGGER.debug(
                "Request #%d result: records on page=%d; total reported by API=%s; next page=%s",
                request_number, len(payload["results"]), payload.get("count", "not provided"),
                "yes" if payload.get("next") else "no",
            )
        else:
            LOGGER.debug("Request #%d result: received a JSON object (%d fields)", request_number, len(payload))
        return payload

    def items(self, path, *, purpose="Fetch an AWX resource list", **filters):
        """Fetch all pages by following the API's next links."""
        parameters = {"page_size": PAGE_SIZE, **filters}
        debug_variables(f"Parameters: {purpose}", parameters)
        query = urlencode(parameters)
        url = self._resolve_url(path)
        url += ("&" if urlsplit(url).query else "?") + query
        visited = set()
        page_number = 0
        record_count = 0
        while url:
            if url in visited:
                raise AWXError("The API returned a repeated pagination link.")
            visited.add(url)
            page_number += 1
            page = self.get(url, purpose=f"{purpose}; page {page_number}")
            if not isinstance(page.get("results"), list):
                raise AWXError("The API response is missing the results list.")
            record_count += len(page["results"])
            yield from page["results"]
            next_page = page.get("next")
            url = self._resolve_url(next_page, relative_to=url) if next_page else None
        LOGGER.debug(
            "Completed: %s; fetched %d records across %d pages",
            purpose, record_count, page_number,
        )


def find_inventory(client, selector):
    if isinstance(selector, int):
        if selector < 1:
            raise AWXError("The inventory ID must be positive.")
        return client.get(
            f"inventories/{selector}/",
            purpose=f"Fetch details for inventory ID {selector}",
        )

    if not isinstance(selector, str) or not selector.strip():
        raise AWXError("Set INVENTORY to an inventory name or numeric ID.")
    matches = list(client.items(
        "inventories/", name=selector,
        purpose=f"Find inventory named {selector!r}",
    ))
    if not matches:
        raise AWXError(f"No accessible inventory named {selector!r} was found.")
    if len(matches) > 1:
        ids = ", ".join(str(item["id"]) for item in matches)
        raise AWXError(
            f"The name {selector!r} is ambiguous. Set INVENTORY to one of these IDs: {ids}."
        )
    return matches[0]


def credential_variable_names():
    """Return the variable names whose values are read from AWX groups."""
    variables = CREDENTIAL_VARIABLES
    if not isinstance(variables, (list, tuple)) or any(
        not isinstance(name, str) or not name.strip() for name in variables
    ):
        raise AWXError("CREDENTIAL_VARIABLES must be a list of nonempty variable names.")
    return tuple(dict.fromkeys(name.strip() for name in variables))


def extra_credential_values():
    """Return literal values from the configuration, shared by all hosts."""
    if not isinstance(EXTRA_CREDENTIAL_VARIABLES, (list, tuple)):
        raise AWXError("EXTRA_CREDENTIAL_VARIABLES must be a list of literal credential values.")
    values = []
    for value in EXTRA_CREDENTIAL_VARIABLES:
        if value is None:
            continue
        if not isinstance(value, (str, int, float, bool)):
            raise AWXError("Values in EXTRA_CREDENTIAL_VARIABLES must be strings or numbers.")
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if text.strip() and text not in values:
            values.append(text)
    return tuple(values)


class InventoryGroupIndex:
    """A local index of groups and memberships, shared by all hosts."""

    def __init__(self, groups, exported, credential_variables=("os_credential",)):
        self.credential_variables = credential_variables
        self.groups = {
            group["name"]: {"id": group["id"], "name": group["name"]} for group in groups
        }
        self.direct_by_host = {}
        self.parents = {name: set() for name in self.groups}
        self.variables = {}
        self.all_groups_cache = {}

        for name in self.groups:
            group_data = exported.get(name, {})
            if not isinstance(group_data, dict) or not isinstance(group_data.get("vars", {}), dict):
                raise AWXError(f"Invalid data for group {name!r} in the inventory export.")
            # The export returns dictionaries even for variables stored as YAML in AWX.
            self.variables[name] = group_data.get("vars", {})
            debug_variables(f"Variables for group {name!r} (ID {self.groups[name]['id']})", self.variables[name])
            for hostname in group_data.get("hosts", []):
                self.direct_by_host.setdefault(hostname, set()).add(name)
            for child_name in group_data.get("children", []):
                if child_name not in self.parents:
                    raise AWXError(
                        f"Group {child_name!r} is missing from the group list. "
                        "The inventory may have changed during retrieval; run the script again."
                    )
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
        LOGGER.debug(
            "Host %s: looking for variables %s locally in %d groups (direct=%d, inherited=%d)",
            hostname, self.credential_variables, len(all_names), len(direct_names),
            len(all_names - direct_names),
        )

        matches = []
        sorted_names = sorted(all_names)
        for variable in self.credential_variables:
            previous_matches = len(matches)
            for name in sorted_names:
                if variable in self.variables[name]:
                    LOGGER.debug(
                        "Host %s: found %s in group %r (%s)",
                        hostname, variable, name,
                        "direct" if name in direct_names else "inherited",
                    )
                    matches.append(
                        {
                            **self.groups[name],
                            "variable": variable,
                            "value": self.variables[name][variable],
                            "inherited": name not in direct_names,
                        }
                    )
            if len(matches) == previous_matches:
                LOGGER.debug("Host %s: variable %s was not found in its groups", hostname, variable)
        result = {
            "groups": [self.groups[name] for name in sorted(direct_names)],
            "all_groups": [self.groups[name] for name in sorted_names],
            "os_credential_groups": [
                {
                    "id": match["id"],
                    "name": match["name"],
                    "os_credential": match["value"],
                    "inherited": match["inherited"],
                }
                for match in matches if match["variable"] == "os_credential"
            ],
            "credential_groups": matches,
        }
        debug_variables(f"Host {hostname!r}", result)
        return result


def fetch_report(client, selector, hostnames=None):
    credential_variables = credential_variable_names()
    extra_credentials = extra_credential_values()
    debug_variables("Literal extra credential values from configuration", extra_credentials)
    inventory = find_inventory(client, selector)
    LOGGER.debug(
        "Selected inventory: ID=%s; name=%r; type=%s",
        inventory["id"], inventory["name"], inventory.get("kind") or "standard",
    )
    debug_variables("Credential variables to look up", credential_variables)
    requested = list(dict.fromkeys(hostnames)) if hostnames is not None else None
    requested_names = set(requested) if requested is not None else None
    host_filters = {"order_by": "id"}
    if requested:
        # AWX filters short lists. Match long lists locally after a bulk fetch
        # to stay within URL length limits and avoid per-host requests.
        candidate = {**host_filters, "page_size": PAGE_SIZE, "name__in": ",".join(requested)}
        if len(urlencode(candidate)) <= 6000:
            host_filters["name__in"] = candidate["name__in"]
            LOGGER.debug("Host search: sending %d names to AWX using the name__in filter", len(requested))
        else:
            LOGGER.debug("Host search: matching %d names locally after a bulk fetch", len(requested))
    else:
        LOGGER.debug(
            "Host search: fetching all hosts from inventory ID %s (name: %r)",
            inventory["id"], inventory["name"],
        )
    debug_variables("AWX host filters", host_filters)
    hosts = []
    group_indexes = {}
    host_purpose = f"Fetch hosts from inventory ID {inventory['id']} (name: {inventory['name']!r})"
    if requested is not None:
        host_purpose += f" for {len(requested)} names from the input file"
    for host in client.items(
        f"inventories/{inventory['id']}/hosts/", purpose=host_purpose, **host_filters
    ):
        if requested_names is not None and host["name"] not in requested_names:
            continue
        # Smart Inventory hosts may belong to different source inventories.
        source_id = host.get("inventory") or inventory["id"]
        if source_id not in group_indexes:
            LOGGER.debug("Loading the group index for source inventory ID %s", source_id)
            groups = list(client.items(
                f"inventories/{source_id}/groups/", order_by="name",
                purpose=f"Fetch group names and IDs from source inventory ID {source_id}",
            ))
            # all=1 includes disabled hosts. Omitting hostvars avoids fetching
            # unnecessary host variables. Exported vars contain group variables.
            exported = client.get(
                f"inventories/{source_id}/script/?all=1",
                purpose=(
                    f"Fetch host memberships, group hierarchy, and group variables from source inventory ID {source_id}; "
                    "include disabled hosts"
                ),
            ) if groups else {}
            group_indexes[source_id] = InventoryGroupIndex(groups, exported, credential_variables)
            LOGGER.debug(
                "Group index for source inventory ID %s is ready: %d groups and %d hosts with group memberships; "
                "credential lookup runs locally",
                source_id, len(groups), len(group_indexes[source_id].direct_by_host),
            )
        else:
            LOGGER.debug("Host %s: reusing the cached group index for source inventory ID %s", host["name"], source_id)
        hosts.append(
            {
                "id": host["id"],
                "name": host["name"],
                "enabled": host.get("enabled", True),
                **group_indexes[source_id].for_host(host["name"]),
            }
        )
    report = {
        "inventory": {"id": inventory["id"], "name": inventory["name"]},
        "host_count": len(hosts),
        "hosts": hosts,
    }
    if requested is not None:
        if not hosts:
            raise AWXError(
                f"None of the hosts listed in the file exist in inventory {inventory['name']!r}."
            )
        order = {name: index for index, name in enumerate(requested)}
        hosts.sort(key=lambda host: order[host["name"]])
        found_names = {host["name"] for host in hosts}
        report["missing_hosts"] = [name for name in requested if name not in found_names]
    debug_variables("Host summary", {
        "host_count": len(hosts), "source_inventory_count": len(group_indexes),
        "source_inventory_ids": list(group_indexes),
        "missing_hosts": report.get("missing_hosts", []),
    })
    return report


def build_output_rows(report):
    """Build shared output rows and add extra and full credentials to JSON host records."""
    primary_variables = credential_variable_names()
    extra_credentials = extra_credential_values()
    if not isinstance(FULL_CREDENTIAL_SEPARATOR, str):
        raise AWXError("FULL_CREDENTIAL_SEPARATOR must be a string.")
    headers = ["hostname", "Extra_Credentials", "Credential", "Full_Credential"]
    extra_text = FULL_CREDENTIAL_SEPARATOR.join(extra_credentials) or "none"
    rows = []
    for host in report["hosts"]:
        values_by_variable = {}
        full_values_by_variable = {}
        for group in host["credential_groups"]:
            if group["variable"] not in primary_variables:
                continue
            value = group["value"]
            credential = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            values = values_by_variable.setdefault(group["variable"], [])
            if credential not in values:
                values.append(credential)
            if value is not None and credential.strip():
                full_values = full_values_by_variable.setdefault(group["variable"], [])
                if credential not in full_values:
                    full_values.append(credential)

        full_parts = list(extra_credentials)
        for variable in primary_variables:
            full_parts.extend(full_values_by_variable.get(variable, []))
        full_credential = FULL_CREDENTIAL_SEPARATOR.join(dict.fromkeys(full_parts)) or "none"
        host["Extra_Credentials"] = list(extra_credentials)
        host["Full_Credential"] = full_credential
        LOGGER.debug("Host %s: adding %d literal credential values from configuration", host["name"], len(extra_credentials))
        debug_variables(f"Full_Credential for host {host['name']!r}", full_credential)

        fields = [host["name"], extra_text]
        credentials = list(dict.fromkeys(
            value for variable in primary_variables for value in values_by_variable.get(variable, [])
        ))
        for credential in credentials or ["none"]:
            rows.append([*fields, credential, full_credential])
    return headers, rows


def save_excel(headers, rows, output_file):
    """Save text results with filtering and frozen headers."""
    output_file = Path(output_file)
    LOGGER.debug("Excel export: %d result rows, %d columns, file %s", len(rows), len(headers), output_file)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Results"
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "B2"
    sheet.append(headers)
    for row in rows:
        sheet.append(row)

    widths = []
    for column in sheet.iter_cols():
        longest = max(len(line) for cell in column for line in str(cell.value).split("\n"))
        width = min(70, max(18, longest + 3))
        widths.append(width)
        sheet.column_dimensions[get_column_letter(column[0].column)].width = width

    body_font = Font(name="Arial", size=10, color="1F2937")
    header_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F3A5F")
    alternate_fill = PatternFill("solid", fgColor="F0F4F8")
    for row in sheet.iter_rows():
        line_count = 1
        for index, cell in enumerate(row):
            # Credential names and values are text, even when they start with '='.
            cell.data_type = "s"
            cell.number_format = "@"
            cell.font = header_font if cell.row == 1 else body_font
            cell.alignment = Alignment(
                horizontal="center" if cell.row == 1 else "left",
                vertical="center", wrap_text=True,
            )
            if cell.row == 1:
                cell.fill = header_fill
            elif cell.row % 2 == 0:
                cell.fill = alternate_fill
            line_count = max(line_count, sum(
                max(1, math.ceil(len(line) / (widths[index] - 3)))
                for line in str(cell.value).split("\n")
            ))
        sheet.row_dimensions[row[0].row].height = min(409, max(26, line_count * 16 + 6))
    sheet.auto_filter.ref = sheet.dimensions
    output_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        workbook.save(output_file)
    except PermissionError:
        raise AWXError(f"Cannot save {output_file}. Close the file in Excel and try again.") from None
    finally:
        workbook.close()


def main(argv=None):
    args = parse_arguments(argv)
    configure_debug(args.debug, AWX_TOKEN if isinstance(AWX_TOKEN, str) else "")
    client = None
    try:
        settings = read_settings()
        apply_settings(settings)
        token = AWX_TOKEN.strip()
        configure_debug(args.debug, token)
        debug_variables("Configuration", {
            "AWX_URL": AWX_URL, "VARIABLES_FILE": VARIABLES_FILE,
            "AWX_TOKEN": "***", "INVENTORY": INVENTORY,
            "CREDENTIAL_VARIABLES": CREDENTIAL_VARIABLES,
            "EXTRA_CREDENTIAL_VARIABLES": EXTRA_CREDENTIAL_VARIABLES,
            "FULL_CREDENTIAL_SEPARATOR": FULL_CREDENTIAL_SEPARATOR,
            "OUTPUT_FILE": OUTPUT_FILE, "EXCEL_OUTPUT_FILE": EXCEL_OUTPUT_FILE,
            "TIMEOUT": TIMEOUT, "PAGE_SIZE": PAGE_SIZE, "input_file": args.input_file,
        })
        hostnames = None
        if args.input_file is not None:
            if args.input_file.resolve() == EXCEL_OUTPUT_FILE.resolve():
                raise AWXError(
                    "The input and output Excel files must use different paths. "
                    "Change EXCEL_OUTPUT_FILE in the configuration."
                )
            hostnames = read_host_limits(args.input_file)

        client = AWXClient(AWX_URL, token)
        report = fetch_report(client, INVENTORY, hostnames=hostnames)
        missing = report.get("missing_hosts", [])
        if missing:
            details = ", ".join(missing[:20])
            if len(missing) > 20:
                details += f" ... (+{len(missing) - 20})"
            print(f"Missing hosts ({len(missing)}): {details}", file=sys.stderr)
        headers, rows = build_output_rows(report)
        for row in rows:
            print(" , ".join(f"{header} : {value}" for header, value in zip(headers, row)))
        sys.stdout.flush()

        LOGGER.debug("JSON export: %d hosts, file %s", report["host_count"], OUTPUT_FILE)
        OUTPUT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        save_excel(headers, rows, EXCEL_OUTPUT_FILE)
        debug_variables("Saved results", {
            "json": OUTPUT_FILE.resolve(), "excel": EXCEL_OUTPUT_FILE.resolve(), "rows": len(rows),
        })
        return 0
    finally:
        LOGGER.debug("Total API requests: %d", client.request_count if client is not None else 0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AWXError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
