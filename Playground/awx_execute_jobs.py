#!/usr/bin/env python3
"""Launch AWX jobs from the Excel export produced by awx_inventory_hosts.py.

Run: python awx_execute_jobs.py
Preview without API calls or file changes: python awx_execute_jobs.py --dry-run
Custom input / batch size: python awx_execute_jobs.py -file hosts.xlsx --batch-size 10
Runner settings: awx_execute_jobs_variables next to this script (NAME = value).
AWX_URL, AWX_TOKEN, TIMEOUT and default input come from awx_invenotry_hosts_variables.
Requires awx_inventory_hosts.py and openpyxl. Close the input workbook in Excel.

Each launch sends hostname batches as limit, Job_Variables as extra_vars, and
credentials resolved from the Full_Credential column (names or numeric AWX IDs).
Job_Type / JOB_TYPE / --job-type select an action such as install/reinstall/update;
the action is sent as extra_vars[JOB_TYPE_VARIABLE], not AWX's run/check job_type.
Rows with an existing execution entry are skipped, including duplicate host rows.
An in-progress marker is saved BEFORE POST; uncertain requests are never retried.
"""

import argparse
import ast
import json
import math
import os
import re
import sys
import tempfile
import time
from copy import copy, deepcopy
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

import awx_inventory_hosts as inventory

AWXError = inventory.AWXError
SETTINGS_FILE = Path(__file__).resolve().with_name("awx_execute_jobs_variables")
DEFAULT_SETTINGS = {
    "INPUT_FILE": None,  # None uses EXCEL_OUTPUT_FILE from the inventory configuration.
    "SHEET_NAME": "Results",
    "BATCH_SIZE": 10,
    "EXECUTION_COLUMN": "exceiute_job",
    "JOB_TYPE": "",  # Empty preserves the action already present in Job_Variables.
    "JOB_TYPE_VARIABLE": "action",  # Playbook variable carrying install/reinstall/update.
    "WAIT_FOR_COMPLETION": True,
    "POLL_INTERVAL": 5,
    "JOB_WAIT_TIMEOUT": 3600,
}
HOSTNAME_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
TEMPLATE_PATTERN = re.compile(r"(?:^|/)(?:job_templates|job_template|job-template)/([1-9][0-9]*)(?:/|$)")


def read_settings(settings_file=SETTINGS_FILE):
    settings = deepcopy(DEFAULT_SETTINGS)
    try:
        content = Path(settings_file).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        content = ""
    except (OSError, UnicodeError) as exc:
        raise AWXError(f"Cannot read {settings_file}: {exc}") from None
    try:
        tree = ast.parse(content, filename=str(settings_file))
    except SyntaxError as exc:
        raise AWXError(f"Syntax error in {settings_file}, line {exc.lineno}.") from None
    seen = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise AWXError(f"Only NAME = value assignments are allowed in {settings_file}.")
        name = node.targets[0].id.upper()
        if name not in settings or name in seen:
            raise AWXError(f"Unknown or duplicate setting {name} in {settings_file}.")
        seen.add(name)
        try:
            settings[name] = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError):
            raise AWXError(f"Setting {name} must use a literal value.") from None
    if settings["INPUT_FILE"] is not None and (
        not isinstance(settings["INPUT_FILE"], str) or not settings["INPUT_FILE"].strip()
    ):
        raise AWXError("INPUT_FILE must be a file path or None.")
    if not isinstance(settings["SHEET_NAME"], str) or not settings["SHEET_NAME"].strip():
        raise AWXError("SHEET_NAME must be a nonempty worksheet name.")
    if type(settings["BATCH_SIZE"]) is not int or settings["BATCH_SIZE"] < 1:
        raise AWXError("BATCH_SIZE must be a positive integer.")
    column = settings["EXECUTION_COLUMN"]
    if not isinstance(column, str) or not column.strip() or column.strip().casefold() in (
        "hostname", "job_link", "job_variables", "full_credential", "job_type", "ostype", "output"
    ):
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


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description="Launch AWX jobs for batches of Excel hosts and save job links.")
    parser.add_argument("-file", "--file", dest="input_file", type=Path, help="Input Excel file; updated in place.")
    parser.add_argument("--batch-size", type=int, help="Maximum unique hosts in one job (default: BATCH_SIZE).")
    parser.add_argument("--job-type", help="Override the action for all new jobs, e.g. install, reinstall, update.")
    parser.add_argument("--dry-run", action="store_true", help="Show batches without API requests or file changes.")
    parser.add_argument("-d", "--debug", action="store_true", help="Show API request details.")
    return parser.parse_args(argv)


def template_id_from_link(link, client):
    parsed = urlsplit(link)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise AWXError("Job_Link must be an HTTP(S) job template link.")
    if client._origin(parsed) != client.origin:
        raise AWXError("Job_Link points to a different server than AWX_URL.")
    # AWX hash routes, newer UI routes, and /api/.../job_templates/ID/ links.
    match = TEMPLATE_PATTERN.search(parsed.fragment or parsed.path)
    if not match:
        raise AWXError(f"Cannot read a job template ID from Job_Link: {link}")
    return int(match.group(1))


def parse_variables(value, row_number):
    if value is None or isinstance(value, str) and value.strip().casefold() in ("", "none"):
        return {}
    try:
        variables = json.loads(value)
        if not isinstance(variables, dict):
            raise ValueError("not a dictionary")
        json.dumps(variables, allow_nan=False)
    except (TypeError, ValueError):
        raise AWXError(f"Row {row_number}: Job_Variables must be a JSON object, e.g. {{\"action\": \"check\"}}.") from None
    return variables


def parse_credentials(value, row_number):
    """Read credential references using the separator used by the inventory export."""
    if value is None or str(value).strip().casefold() in ("", "none"):
        return ()
    separator = inventory.FULL_CREDENTIAL_SEPARATOR
    if not separator:
        raise AWXError("FULL_CREDENTIAL_SEPARATOR cannot be empty when reading Full_Credential.")
    # A comma separator also accepts manually edited cells without a space after ','.
    references = [part.strip() for part in str(value).split(separator.strip() or separator)]
    if any(not reference or reference.casefold() == "none" for reference in references):
        raise AWXError(f"Row {row_number}: Full_Credential contains an empty or invalid credential reference.")
    return tuple(sorted(set(references)))


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


def build_plan(sheet, client, settings, job_type_override=None):
    """Validate the whole sheet, deduplicate hosts, and batch equal launch payloads."""
    columns = {}
    for cell in sheet[1]:
        if cell.value is None:
            continue
        name = str(cell.value).strip().casefold()
        if name in columns:
            raise AWXError(f"Duplicate column {cell.value!r} in worksheet {sheet.title!r}.")
        columns[name] = cell.column
    for name in ("hostname", "job_link", "job_variables", "full_credential"):
        if name not in columns:
            raise AWXError(f"Missing {name} column. Generate a new Excel export with awx_inventory_hosts.py.")
    execution_column = columns.get(settings["EXECUTION_COLUMN"].casefold(), sheet.max_column + 1)
    hosts = {}
    skipped_without_job = 0
    for row_number in range(2, sheet.max_row + 1):
        host_value = sheet.cell(row_number, columns["hostname"]).value
        if host_value is None or not str(host_value).strip():
            continue
        hostname = str(host_value).strip()
        if not HOSTNAME_PATTERN.fullmatch(hostname):
            raise AWXError(f"Row {row_number}: hostname must be a single literal hostname, without Ansible patterns.")
        link_cell = sheet.cell(row_number, columns["job_link"])
        link = str(link_cell.value or "").strip()
        if link.casefold() in ("", "none"):
            skipped_without_job += 1
            continue
        if link_cell.data_type == "f":
            raise AWXError(f"Row {row_number}: Job_Link must contain a URL rather than an Excel formula.")
        if link_cell.hyperlink and link_cell.hyperlink.target:
            link = link_cell.hyperlink.target
        template_id = template_id_from_link(link, client)
        variables = parse_variables(sheet.cell(row_number, columns["job_variables"]).value, row_number)
        job_type_cell = sheet.cell(row_number, columns["job_type"]) if "job_type" in columns else None
        credential_cell = sheet.cell(row_number, columns["full_credential"])
        if credential_cell.data_type == "f" or job_type_cell is not None and job_type_cell.data_type == "f":
            raise AWXError(f"Row {row_number}: Full_Credential and Job_Type must contain literal values.")
        job_type = apply_job_type(variables, job_type_cell.value if job_type_cell else None, settings, job_type_override)
        credential_refs = parse_credentials(credential_cell.value, row_number)
        key = (template_id, json.dumps(variables, sort_keys=True, ensure_ascii=False), credential_refs)
        execution = str(sheet.cell(row_number, execution_column).value or "").strip()
        if hostname not in hosts:
            hosts[hostname] = {
                "key": key, "template_id": template_id, "link": link, "variables": variables,
                "credential_refs": credential_refs, "job_type": job_type,
                "hostname": hostname, "rows": [], "execution": "",
            }
        host = hosts[hostname]
        if host["key"] != key:
            raise AWXError(f"Row {row_number}: duplicate host {hostname!r} has different job or variables or credentials.")
        if execution:
            if host["execution"] and host["execution"] != execution:
                raise AWXError(f"Host {hostname!r} has conflicting execution links/markers; check its rows.")
            host["execution"] = execution
        host["rows"].append(row_number)

    groups = {}
    existing = []
    for host in hosts.values():
        if host["execution"]:
            existing.append(host)
        else:
            groups.setdefault(host["key"], []).append(host)
    batches = []
    for group in groups.values():
        for start in range(0, len(group), settings["BATCH_SIZE"]):
            entries = group[start:start + settings["BATCH_SIZE"]]
            batches.append({
                "template_id": entries[0]["template_id"], "link": entries[0]["link"],
                "variables": entries[0]["variables"], "entries": entries,
                "credential_refs": entries[0]["credential_refs"], "job_type": entries[0]["job_type"],
                "limit": ",".join(host["hostname"] for host in entries),
            })
    return batches, existing, execution_column, skipped_without_job


def save_workbook(workbook, path):
    """Replace the workbook only after the new ZIP has been fully written."""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.stem}-", suffix=path.suffix, delete=False) as temporary:
            temporary_path = Path(temporary.name)
        workbook.save(temporary_path)
        os.replace(temporary_path, path)
    except OSError as exc:
        raise AWXError(f"Cannot save {path}: {exc}. Close the file in Excel before running the script.") from None
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def write_execution(sheet, column, entries, value):
    for entry in entries:
        for row_number in entry["rows"]:
            cell = sheet.cell(row_number, column)
            cell.value = value
            cell.data_type = "s"
            cell.number_format = "@"
            cell.hyperlink = value if value.startswith(("http://", "https://")) else None
            if cell.hyperlink:
                cell.font = inventory.Font(name="Arial", size=10, color="0563C1", underline="single")
            cell.alignment = inventory.Alignment(vertical="center", wrap_text=True)


class LaunchClient(inventory.AWXClient):
    def post(self, path, payload):
        url = self._resolve_url(path)
        request = Request(url, data=json.dumps(payload, allow_nan=False).encode("utf-8"), headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/json", "Content-Type": "application/json",
        }, method="POST")
        self.request_count += 1
        inventory.LOGGER.debug("Request #%d: POST %s", self.request_count, url)
        try:
            with self.opener.open(request, timeout=inventory.TIMEOUT) as response:
                result = json.load(response)
        except HTTPError as exc:
            code = exc.code
            detail = exc.read(8192).decode("utf-8", errors="replace").replace(self.token, "***")
            exc.close()
            raise AWXError(f"Launch returned HTTP {code}: {detail}") from None
        except (URLError, OSError, ValueError) as exc:
            raise AWXError(f"Launch result is uncertain ({exc}); check AWX before clearing the Excel marker.") from None
        if not isinstance(result, dict):
            raise AWXError("Unexpected launch response; check AWX before clearing the Excel marker.")
        return result


def resolve_credentials(client, batches):
    """Resolve credential names/IDs once and validate each selected credential set."""
    cache = {}
    for batch in batches:
        selected = {}
        for reference in batch["credential_refs"]:
            if reference not in cache:
                if re.fullmatch(r"[0-9]+", reference):
                    credential = client.get(f"credentials/{int(reference)}/", purpose=f"Resolve credential ID {reference}")
                else:
                    matches = list(client.items("credentials/", name=reference, order_by="id",
                                                purpose=f"Resolve credential named {reference!r}"))
                    if not matches:
                        raise AWXError(f"Credential {reference!r} from Full_Credential was not found or is not accessible.")
                    if len(matches) != 1:
                        ids = ", ".join(str(match["id"]) for match in matches)
                        raise AWXError(f"Credential name {reference!r} is ambiguous (IDs: {ids}); use its numeric ID in Full_Credential.")
                    credential = matches[0]
                if type(credential.get("id")) is not int or credential["id"] < 1:
                    raise AWXError(f"Invalid AWX credential response for {reference!r}.")
                cache[reference] = credential
                cache[str(credential["id"])] = credential
            credential = cache[reference]
            selected[credential["id"]] = credential
        identities = {}
        for credential in selected.values():
            inputs = credential.get("inputs") or {}
            if any(inputs.get(name) == "ASK" for name in ("password", "become_password", "ssh_key_unlock", "vault_password")):
                raise AWXError(f"Credential {credential['id']} requires an interactive password; configure it in AWX.")
            credential_type = credential.get("credential_type")
            kind = credential.get("kind") or (credential.get("summary_fields") or {}).get("credential_type", {}).get("kind")
            identity = (credential_type, inputs.get("vault_id") if kind == "vault" else None)
            if credential_type is not None and identity in identities:
                raise AWXError(f"Full_Credential contains conflicting credentials {identities[identity]} and {credential['id']} "
                               "of the same credential type. Select one credential per type (Vault IDs may differ).")
            identities[identity] = credential["id"]
        batch["credentials"] = sorted(selected)
        batch["credential_types"] = {credential.get("credential_type") for credential in selected.values()}


def validate_templates(client, batches):
    """Check prompting and required inputs before launching the first batch."""
    cache = {}
    for batch in batches:
        template_id = batch["template_id"]
        if template_id not in cache:
            details = client.get(f"job_templates/{template_id}/launch/", purpose=f"Check launch settings for template {template_id}")
            if not details.get("ask_limit_on_launch"):
                raise AWXError(f"Template {template_id}: enable Prompt on launch for Limit to restrict each job to its batch.")
            if (details.get("defaults", {}).get("job_slice_count") or 1) > 1:
                raise AWXError(f"Template {template_id}: set Job Slicing to 1; this script creates its own host batches.")
            if details.get("inventory_needed_to_start"):
                raise AWXError(f"Template {template_id}: configure its required inventory in AWX.")
            cache[template_id] = (details, None)
        details, survey_variables = cache[template_id]
        if batch["credentials"]:
            if not details.get("ask_credential_on_launch"):
                raise AWXError(f"Template {template_id}: enable Prompt on launch for Credentials to accept Full_Credential.")
            for default in details.get("defaults", {}).get("credentials", []):
                if default["id"] not in batch["credentials"] and default.get("credential_type") not in batch["credential_types"]:
                    raise AWXError(f"Template {template_id}: Full_Credential must include default credential {default['id']} "
                                   "or a replacement of the same credential type.")
        elif details.get("passwords_needed_to_start") or details.get("credential_needed_to_start"):
            raise AWXError(f"Template {template_id}: credentials require interactive passwords or are missing; fill Full_Credential.")
        variables = batch["variables"]
        if variables and not details.get("ask_variables_on_launch"):
            if not details.get("survey_enabled"):
                raise AWXError(f"Template {template_id}: enable Prompt on launch for Variables to accept Job_Variables.")
            if survey_variables is None:
                survey = client.get(f"job_templates/{template_id}/survey_spec/", purpose=f"Check survey variables for template {template_id}")
                survey_variables = {item["variable"] for item in survey.get("spec", [])}
                cache[template_id] = (details, survey_variables)
            unsupported = set(variables) - survey_variables
            if unsupported:
                raise AWXError(f"Template {template_id}: variables outside its survey: {', '.join(sorted(unsupported))}.")
        missing = [name for name in details.get("variables_needed_to_start", []) if name not in variables]
        if missing:
            raise AWXError(f"Template {template_id}: missing required Job_Variables: {', '.join(missing)}.")


def job_url(template_link, client, job_id):
    parsed = urlsplit(template_link)
    route = f"/jobs/playbook/{job_id}/output"
    if parsed.fragment:
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", route))
    if "/templates/" in parsed.path:
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.split("/templates/", 1)[0] + route, "", ""))
    # An API template link does not identify a UI route. The job API URL is still clickable.
    return client._resolve_url(f"jobs/{job_id}/")


def wait_for_job(client, job_id, settings):
    deadline = time.monotonic() + settings["JOB_WAIT_TIMEOUT"]
    last_status = None
    while True:
        job = client.get(f"jobs/{job_id}/", purpose=f"Check status of job {job_id}")
        status = job.get("status")
        if status != last_status:
            print(f"Job {job_id}: {status}", flush=True)
            last_status = status
        if status == "successful":
            return
        if status in ("failed", "error", "canceled"):
            raise AWXError(f"Job {job_id} finished with status {status}; remaining batches were not launched.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AWXError(f"Timed out waiting for job {job_id}; its link is saved. Remaining batches were not launched.")
        time.sleep(min(settings["POLL_INTERVAL"], remaining))


def execute_plan(workbook, sheet, path, client, settings, batches, existing, execution_column):
    header = sheet.cell(1, execution_column)
    header.value = settings["EXECUTION_COLUMN"]
    header._style = copy(sheet.cell(1, 1)._style)
    sheet.column_dimensions[inventory.get_column_letter(execution_column)].width = 65
    job_type_column = next((cell.column for cell in sheet[1] if str(cell.value or "").strip().casefold() == "job_type"),
                           sheet.max_column + 1)
    type_header = sheet.cell(1, job_type_column)
    type_header.value = "Job_Type"
    type_header._style = copy(sheet.cell(1, 1)._style)
    sheet.column_dimensions[inventory.get_column_letter(job_type_column)].width = 20
    if sheet.auto_filter.ref:
        sheet.auto_filter.ref = sheet.dimensions
    for entry in existing:
        write_execution(sheet, execution_column, [entry], entry["execution"])
    # Check file access and persist duplicate-row links before any launch.
    save_workbook(workbook, path)
    for index, batch in enumerate(batches, start=1):
        template_id = batch["template_id"]
        print(f"Batch {index}/{len(batches)}: template={template_id}; job_type={batch['job_type'] or 'none'}; "
              f"credentials={batch['credentials']}; hosts={batch['limit']}", flush=True)
        for entry in batch["entries"]:
            for row_number in entry["rows"]:
                cell = sheet.cell(row_number, job_type_column)
                cell.value = batch["job_type"]
                cell.data_type = "s"
                cell.number_format = "@"
        marker = f"LAUNCH_PENDING template={template_id}; {datetime.now(timezone.utc).isoformat()}"
        write_execution(sheet, execution_column, batch["entries"], marker)
        save_workbook(workbook, path)
        try:
            payload = {
                "limit": batch["limit"], "extra_vars": batch["variables"],
            }
            if batch["credentials"]:
                payload["credentials"] = batch["credentials"]
            result = client.post(f"job_templates/{template_id}/launch/", payload)
            job_id = result.get("job") or result.get("id")
            if type(job_id) is not int or job_id < 1:
                raise AWXError("Launch response has no job ID; check AWX before clearing the Excel marker.")
            link = job_url(batch["link"], client, job_id)
            # Print before saving so a file error does not hide the already launched job.
            print(f"Job {job_id}: {link}", flush=True)
            write_execution(sheet, execution_column, batch["entries"], link)
            save_workbook(workbook, path)
            if result.get("ignored_fields"):
                raise AWXError(f"Job {job_id}: AWX ignored launch fields {', '.join(result['ignored_fields'])}; stopped further launches.")
            if settings["WAIT_FOR_COMPLETION"]:
                wait_for_job(client, job_id, settings)
        except BaseException:
            print("Stopped. Check the saved execution links/markers and AWX before retrying the affected batch.", file=sys.stderr)
            raise


def main(argv=None):
    args = parse_arguments(argv)
    if args.job_type is not None and not args.job_type.strip():
        raise AWXError("--job-type must be nonempty, e.g. install, reinstall, or update.")
    connection = inventory.read_settings()
    inventory.apply_settings(connection)
    inventory.configure_debug(args.debug, connection["AWX_TOKEN"].strip())
    settings = read_settings()
    if args.batch_size is not None:
        if args.batch_size < 1:
            raise AWXError("--batch-size must be positive.")
        settings["BATCH_SIZE"] = args.batch_size
    path = Path(args.input_file or settings["INPUT_FILE"] or connection["EXCEL_OUTPUT_FILE"]).resolve()
    if path.suffix.casefold() != ".xlsx":
        raise AWXError("The input file must use the .xlsx extension.")
    client = LaunchClient(connection["AWX_URL"], connection["AWX_TOKEN"].strip())
    try:
        workbook = inventory.load_workbook(path, keep_links=True)
    except (OSError, ValueError, BadZipFile, ParseError, inventory.InvalidFileException) as exc:
        raise AWXError(f"Cannot read {path}: {exc}") from None
    try:
        if settings["SHEET_NAME"] not in workbook.sheetnames:
            raise AWXError(f"Worksheet {settings['SHEET_NAME']!r} does not exist in {path}.")
        sheet = workbook[settings["SHEET_NAME"]]
        batches, existing, column, skipped = build_plan(sheet, client, settings, job_type_override=args.job_type)
        print(f"Input: {path}; batch size: {settings['BATCH_SIZE']}; batches: {len(batches)}; "
              f"already recorded hosts: {len(existing)}; rows without job: {skipped}", flush=True)
        if args.dry_run:
            for index, batch in enumerate(batches, start=1):
                print(f"Batch {index}: template={batch['template_id']}; limit={batch['limit']}; "
                      f"credentials={list(batch['credential_refs'])}; job_type={batch['job_type'] or 'none'}; "
                      f"extra_vars={json.dumps(batch['variables'], ensure_ascii=False)}")
            print("Dry run: no jobs launched and no workbook changes.")
            return 0
        if any(entry["execution"].startswith("LAUNCH_PENDING") for entry in existing):
            raise AWXError("An earlier launch has a LAUNCH_PENDING marker. Check AWX, then replace it with the job link "
                           "or clear it only if no job was launched.")
        if not batches:
            print("No new batches to launch.")
            return 0
        resolve_credentials(client, batches)
        validate_templates(client, batches)
        if settings["WAIT_FOR_COMPLETION"]:
            # Resume without overlapping a job recorded before an interruption.
            recorded_jobs = set()
            for entry in existing:
                parsed = urlsplit(entry["execution"])
                match = re.search(r"/jobs/(?:playbook/)?([1-9][0-9]*)(?:/|$)", parsed.fragment or parsed.path)
                if match and client._origin(parsed) == client.origin:
                    recorded_jobs.add(int(match.group(1)))
            for job_id in sorted(recorded_jobs):
                wait_for_job(client, job_id, settings)
        execute_plan(workbook, sheet, path, client, settings, batches, existing, column)
        print(f"Saved job links to {settings['EXECUTION_COLUMN']} in {path}.")
        return 0
    finally:
        workbook.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AWXError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted. Check execution links/markers before restarting.", file=sys.stderr)
        sys.exit(130)
