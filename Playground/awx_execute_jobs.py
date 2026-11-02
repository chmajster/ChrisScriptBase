#!/usr/bin/env python3
"""Launch AWX jobs from the Excel export produced by awx_inventory_hosts.py.

Run: python awx_execute_jobs.py
Preview without API calls or file changes: python awx_execute_jobs.py --dry-run
Custom input / batch size: python awx_execute_jobs.py -file hosts.xlsx --batch-size 10
Runner settings: awx_execute_jobs_variables next to this script (NAME = value).
AWX_URL, AWX_TOKEN, TIMEOUT and default input come from awx_invenotry_hosts_variables.
Requires awx_inventory_hosts.py and openpyxl. Close the input workbook in Excel.

Each launch sends hostname batches as limit and Job_Variables as extra_vars.
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
        "hostname", "job_link", "job_variables"
    ):
        raise AWXError("EXECUTION_COLUMN must name a separate output column.")
    settings["EXECUTION_COLUMN"] = column.strip()
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


def build_plan(sheet, client, settings):
    """Validate the whole sheet, deduplicate hosts, and batch equal launch payloads."""
    columns = {}
    for cell in sheet[1]:
        if cell.value is None:
            continue
        name = str(cell.value).strip().casefold()
        if name in columns:
            raise AWXError(f"Duplicate column {cell.value!r} in worksheet {sheet.title!r}.")
        columns[name] = cell.column
    for name in ("hostname", "job_link", "job_variables"):
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
        key = (template_id, json.dumps(variables, sort_keys=True, ensure_ascii=False))
        execution = str(sheet.cell(row_number, execution_column).value or "").strip()
        if hostname not in hosts:
            hosts[hostname] = {
                "key": key, "template_id": template_id, "link": link, "variables": variables,
                "hostname": hostname, "rows": [], "execution": "",
            }
        host = hosts[hostname]
        if host["key"] != key:
            raise AWXError(f"Row {row_number}: duplicate host {hostname!r} has different job or variables.")
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
            if details.get("inventory_needed_to_start") or details.get("credential_needed_to_start"):
                raise AWXError(f"Template {template_id}: configure its required inventory and credentials in AWX.")
            if details.get("passwords_needed_to_start"):
                raise AWXError(f"Template {template_id}: credentials require interactive passwords; configure them in AWX.")
            cache[template_id] = (details, None)
        details, survey_variables = cache[template_id]
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
    if sheet.auto_filter.ref:
        sheet.auto_filter.ref = sheet.dimensions
    for entry in existing:
        write_execution(sheet, execution_column, [entry], entry["execution"])
    # Check file access and persist duplicate-row links before any launch.
    save_workbook(workbook, path)
    for index, batch in enumerate(batches, start=1):
        template_id = batch["template_id"]
        print(f"Batch {index}/{len(batches)}: template={template_id}; hosts={batch['limit']}", flush=True)
        marker = f"LAUNCH_PENDING template={template_id}; {datetime.now(timezone.utc).isoformat()}"
        write_execution(sheet, execution_column, batch["entries"], marker)
        save_workbook(workbook, path)
        try:
            result = client.post(f"job_templates/{template_id}/launch/", {
                "limit": batch["limit"], "extra_vars": batch["variables"],
            })
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
        batches, existing, column, skipped = build_plan(sheet, client, settings)
        print(f"Input: {path}; batch size: {settings['BATCH_SIZE']}; batches: {len(batches)}; "
              f"already recorded hosts: {len(existing)}; rows without job: {skipped}", flush=True)
        if args.dry_run:
            for index, batch in enumerate(batches, start=1):
                print(f"Batch {index}: template={batch['template_id']}; limit={batch['limit']}; "
                      f"extra_vars={json.dumps(batch['variables'], ensure_ascii=False)}")
            print("Dry run: no jobs launched and no workbook changes.")
            return 0
        if any(entry["execution"].startswith("LAUNCH_PENDING") for entry in existing):
            raise AWXError("An earlier launch has a LAUNCH_PENDING marker. Check AWX, then replace it with the job link "
                           "or clear it only if no job was launched.")
        if not batches:
            print("No new batches to launch.")
            return 0
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
