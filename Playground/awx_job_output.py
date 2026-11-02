#!/usr/bin/env python3
"""Collect stdout of AWX jobs recorded in the exceiute_job Excel column.

Run: python awx_job_output.py
Custom workbook: python awx_job_output.py -file hosts.xlsx
Custom output directory: python awx_job_output.py --output-dir output
Settings: awx_job_output_variables (NAME = value).
Connection settings are read from awx_invenotry_hosts_variables.
Workbook, worksheet and execution column default to awx_execute_jobs_variables.
Requires awx_inventory_hosts.py, awx_execute_jobs.py and openpyxl.

The workbook is updated in place; stdout is also saved as UTF-8 text in
output/<job_id>.<inventory_name>.<ostype>.txt. Repeated job IDs are fetched once.
Excel gets the complete job log, limited to 32767 characters per cell; text files
always contain the full downloaded log. Failed/canceled job logs are collected too.
"""

import argparse
import ast
import math
import os
import re
import sys
import tempfile
import time
from copy import copy, deepcopy
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

import awx_execute_jobs as runner
import awx_inventory_hosts as inventory

AWXError = inventory.AWXError
SETTINGS_FILE = Path(__file__).resolve().with_name("awx_job_output_variables")
DEFAULT_SETTINGS = {
    "INPUT_FILE": None,
    "SHEET_NAME": None,
    "EXECUTION_COLUMN": None,
    "OUTPUT_COLUMN": "output",
    "OUTPUT_DIRECTORY": "output",
    "WAIT_FOR_COMPLETION": True,
    "POLL_INTERVAL": 5,
    "JOB_WAIT_TIMEOUT": 3600,
}
JOB_PATTERN = re.compile(r"(?:^|/)jobs/(?:playbook/)?([1-9][0-9]*)(?:/|$)")
TERMINAL_STATUSES = {"successful", "failed", "error", "canceled"}
EXCEL_CELL_LIMIT = 32767


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
    for name in ("INPUT_FILE", "SHEET_NAME", "EXECUTION_COLUMN", "OUTPUT_COLUMN", "OUTPUT_DIRECTORY"):
        value = settings[name]
        if value is None and name in ("INPUT_FILE", "SHEET_NAME", "EXECUTION_COLUMN"):
            continue
        if not isinstance(value, str) or not value.strip():
            raise AWXError(f"{name} must be a nonempty string.")
    if type(settings["WAIT_FOR_COMPLETION"]) is not bool:
        raise AWXError("WAIT_FOR_COMPLETION must be True or False.")
    for name in ("POLL_INTERVAL", "JOB_WAIT_TIMEOUT"):
        value = settings[name]
        if type(value) not in (int, float) or value <= 0 or not math.isfinite(value):
            raise AWXError(f"{name} must be a positive number of seconds.")
    return settings


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description="Download AWX job stdout to Excel and text files.")
    parser.add_argument("-file", "--file", dest="input_file", type=Path, help="Input workbook; updated in place.")
    parser.add_argument("--output-dir", type=Path, help="Directory for <job_id>.<inventory>.<os>.txt files.")
    parser.add_argument("-d", "--debug", action="store_true", help="Show API request details.")
    return parser.parse_args(argv)


def job_id_from_link(link, client):
    parsed = urlsplit(link)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise AWXError("Execution link must be an HTTP(S) job URL.")
    if client._origin(parsed) != client.origin:
        raise AWXError("Execution link points to a different server than AWX_URL.")
    match = JOB_PATTERN.search(parsed.fragment or parsed.path)
    if not match:
        raise AWXError(f"Cannot read a job ID from execution link: {link}")
    return int(match.group(1))


def build_plan(sheet, client, settings):
    columns = {}
    for cell in sheet[1]:
        if cell.value is None:
            continue
        name = str(cell.value).strip().casefold()
        if name in columns:
            raise AWXError(f"Duplicate column {cell.value!r} in worksheet {sheet.title!r}.")
        columns[name] = cell.column
    execution_name = settings["EXECUTION_COLUMN"].strip().casefold()
    output_name = settings["OUTPUT_COLUMN"].strip().casefold()
    if output_name in (execution_name, "hostname", "ostype", "job_link", "job_variables",
                       "extra_credentials", "credential", "full_credential"):
        raise AWXError("OUTPUT_COLUMN must name a separate output column.")
    for name in (execution_name, "ostype"):
        if name not in columns:
            raise AWXError(f"Missing {name} column. Use the Excel workbook updated by awx_execute_jobs.py.")
    jobs = {}
    skipped = 0
    for row_number in range(2, sheet.max_row + 1):
        cell = sheet.cell(row_number, columns[execution_name])
        value = str(cell.value or "").strip()
        if value.casefold() in ("", "none") or value.startswith("LAUNCH_PENDING"):
            skipped += 1
            continue
        if cell.data_type == "f":
            raise AWXError(f"Row {row_number}: execution link must contain a URL rather than an Excel formula.")
        link = cell.hyperlink.target if cell.hyperlink and cell.hyperlink.target else value
        try:
            job_id = job_id_from_link(link, client)
        except (AWXError, ValueError) as exc:
            raise AWXError(f"Row {row_number}: {exc}") from None
        ostype = str(sheet.cell(row_number, columns["ostype"]).value or "unknown").strip().casefold() or "unknown"
        jobs.setdefault(job_id, []).append({"row": row_number, "ostype": ostype})
    return jobs, columns.get(output_name, sheet.max_column + 1), skipped


class OutputClient(inventory.AWXClient):
    def stdout(self, job_id):
        # Download format bypasses AWX's display-size limit and removes ANSI codes.
        url = self._resolve_url(f"jobs/{job_id}/stdout/?format=txt_download")
        request = Request(url, headers={"Authorization": f"Bearer {self.token}", "Accept": "text/plain"}, method="GET")
        self.request_count += 1
        inventory.LOGGER.debug("Request #%d: GET %s", self.request_count, url)
        try:
            with self.opener.open(request, timeout=inventory.TIMEOUT) as response:
                if response.headers.get_content_type() in ("application/json", "text/html"):
                    raise AWXError(f"Job {job_id}: expected text stdout, received {response.headers.get_content_type()}.")
                encoding = response.headers.get_content_charset() or "utf-8"
                content = response.read().decode(encoding, errors="replace")
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise AWXError(f"Job {job_id}: cannot download stdout (HTTP {code}).") from None
        except (URLError, OSError, LookupError) as exc:
            raise AWXError(f"Job {job_id}: cannot download stdout: {exc}") from None
        inventory.LOGGER.debug("Job %d: downloaded %d stdout characters", job_id, len(content))
        return content


def read_job(client, job_id, settings):
    deadline = time.monotonic() + settings["JOB_WAIT_TIMEOUT"]
    previous_status = None
    while True:
        job = client.get(f"jobs/{job_id}/", purpose=f"Read job {job_id} details and inventory")
        status = job.get("status")
        if status != previous_status:
            print(f"Job {job_id}: {status}", flush=True)
            previous_status = status
        if status in TERMINAL_STATUSES or not settings["WAIT_FOR_COMPLETION"]:
            return job
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AWXError(f"Job {job_id}: timed out waiting for completion; output was not collected.")
        time.sleep(min(settings["POLL_INTERVAL"], remaining))


def inventory_name(client, job, cache):
    summary = (job.get("summary_fields") or {}).get("inventory") or {}
    if summary.get("name"):
        return str(summary["name"])
    inventory_id = job.get("inventory")
    if not inventory_id:
        return "unknown_inventory"
    if inventory_id not in cache:
        try:
            details = client.get(f"inventories/{inventory_id}/", purpose=f"Read inventory name for ID {inventory_id}")
            cache[inventory_id] = str(details.get("name") or inventory_id)
        except AWXError:
            inventory.LOGGER.debug("Inventory %s name is unavailable; using its ID in filenames", inventory_id)
            cache[inventory_id] = str(inventory_id)
    return cache[inventory_id]


def filename_part(value):
    """Keep every filename inside the output directory, including on Windows."""
    part = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .")
    return part[:100].rstrip(" .") or "unknown"


def save_text(path, content):
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix=f".{path.stem}-", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
        os.replace(temporary_path, path)
    except OSError as exc:
        raise AWXError(f"Cannot save stdout file {path}: {exc}") from None
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def write_output(sheet, column, row_number, content, text_path):
    # XML control characters are kept in TXT but cannot be stored in an XLSX cell.
    text = ILLEGAL_CHARACTERS_RE.sub("", content)
    if len(text) > EXCEL_CELL_LIMIT:
        suffix = f"\n[Output truncated in Excel; full log: {text_path}]"
        text = text[:EXCEL_CELL_LIMIT - len(suffix)] + suffix
    cell = sheet.cell(row_number, column)
    cell.value = text
    cell.data_type = "s"  # Logs beginning with '=' must remain literal text.
    cell.number_format = "@"
    cell.hyperlink = None
    cell.font = inventory.Font(name="Arial", size=10, color="1F2937")
    cell.alignment = inventory.Alignment(vertical="top", wrap_text=True)
    height = min(120, max(26, (text.count("\n") + 1) * 16 + 6))
    sheet.row_dimensions[row_number].height = max(sheet.row_dimensions[row_number].height or 0, height)


def collect_outputs(workbook, sheet, path, output_directory, client, settings, jobs, column):
    header = sheet.cell(1, column)
    header.value = settings["OUTPUT_COLUMN"].strip()
    header._style = copy(sheet.cell(1, 1)._style)
    sheet.column_dimensions[inventory.get_column_letter(column)].width = 90
    if sheet.auto_filter.ref:
        sheet.auto_filter.ref = sheet.dimensions
    # Verify workbook access before downloading logs and creating output files.
    runner.save_workbook(workbook, path)
    output_directory.mkdir(parents=True, exist_ok=True)
    inventory_names = {}
    failures = 0
    completed = 0
    for job_id, entries in jobs.items():
        try:
            job = read_job(client, job_id, settings)
            name = filename_part(inventory_name(client, job, inventory_names))
            content = client.stdout(job_id)
            files = {}
            for entry in entries:
                os_name = filename_part(entry["ostype"])
                if os_name not in files:
                    text_path = output_directory / f"{job_id}.{name}.{os_name}.txt"
                    save_text(text_path, content)
                    files[os_name] = text_path
                    print(f"Job {job_id}: {text_path}", flush=True)
            for entry in entries:
                write_output(sheet, column, entry["row"], content, files[filename_part(entry["ostype"])])
            # Persist every completed job, including logs from failed AWX jobs.
            runner.save_workbook(workbook, path)
            completed += 1
        except AWXError as exc:
            failures += 1
            print(f"Error: {exc}", file=sys.stderr)
    print(f"Collected: {completed} jobs; errors: {failures}; workbook: {path}", flush=True)
    return 1 if failures else 0


def main(argv=None):
    args = parse_arguments(argv)
    connection = inventory.read_settings()
    inventory.apply_settings(connection)
    inventory.configure_debug(args.debug, connection["AWX_TOKEN"].strip())
    launch_settings = runner.read_settings()
    settings = read_settings()
    settings["SHEET_NAME"] = settings["SHEET_NAME"] or launch_settings["SHEET_NAME"]
    settings["EXECUTION_COLUMN"] = settings["EXECUTION_COLUMN"] or launch_settings["EXECUTION_COLUMN"]
    path = Path(args.input_file or settings["INPUT_FILE"] or launch_settings["INPUT_FILE"] or connection["EXCEL_OUTPUT_FILE"]).resolve()
    output_directory = Path(args.output_dir or settings["OUTPUT_DIRECTORY"]).resolve()
    if path.suffix.casefold() != ".xlsx":
        raise AWXError("The input file must use the .xlsx extension.")
    client = OutputClient(connection["AWX_URL"], connection["AWX_TOKEN"].strip())
    try:
        workbook = inventory.load_workbook(path, keep_links=True)
    except (OSError, ValueError, BadZipFile, ParseError, inventory.InvalidFileException) as exc:
        raise AWXError(f"Cannot read {path}: {exc}") from None
    try:
        if settings["SHEET_NAME"] not in workbook.sheetnames:
            raise AWXError(f"Worksheet {settings['SHEET_NAME']!r} does not exist in {path}.")
        sheet = workbook[settings["SHEET_NAME"]]
        jobs, column, skipped = build_plan(sheet, client, settings)
        print(f"Input: {path}; unique jobs: {len(jobs)}; rows without a job link: {skipped}", flush=True)
        if not jobs:
            print("No job output to collect.")
            return 0
        return collect_outputs(workbook, sheet, path, output_directory, client, settings, jobs, column)
    finally:
        workbook.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AWXError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted. Output collected so far remains saved.", file=sys.stderr)
        sys.exit(130)
