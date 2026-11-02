#!/usr/bin/env python3
"""Refresh AWX host information in an existing Excel workbook, in place.

Run: python awx_refresh_excel.py
Custom workbook: python awx_refresh_excel.py -file hosts.xlsx -d
Custom host column / sheet: python awx_refresh_excel.py -file hosts.xlsx --host-column host_limit --sheet Sheet1

Only the host column supplies lookup data. Existing OS, credentials, job template,
variables and job action are overwritten using the current configuration and AWX.
Execution links, output, other columns and other worksheets are preserved.
Rows are sorted Windows, Linux, AIX, then unknown/missing hosts and empty rows.

Uses awx_invenotry_hosts_variables for AWX and inventory settings, and
awx_execute_jobs_variables for the default workbook, worksheet and job action.
Host columns are detected as hostname, host_limit, host or hosts (case-insensitive).
Each cell must contain one hostname. Duplicate host rows are refreshed together;
multiple credential matches are joined in Credential and Full_Credential.
Close the workbook in Excel before running. Requires openpyxl and both scripts.
"""

import argparse
import json
import sys
from copy import copy
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

import awx_execute_jobs as runner
import awx_inventory_hosts as inventory

AWXError = inventory.AWXError
HOST_COLUMNS = ("hostname", "host_limit", "host", "hosts")


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description="Read Excel hostnames and overwrite their AWX metadata in the same file.")
    parser.add_argument("-file", "--file", dest="input_file", type=Path, help="Workbook to refresh in place.")
    parser.add_argument("--sheet", help="Worksheet name; defaults to configured sheet or the active worksheet.")
    parser.add_argument("--host-column", help="Host column header; detected automatically if omitted.")
    parser.add_argument("-d", "--debug", action="store_true", help="Show API request details.")
    return parser.parse_args(argv)


def read_host_rows(sheet, host_column=None):
    columns = {}
    for cell in sheet[1]:
        if cell.value is None:
            continue
        name = str(cell.value).strip().casefold()
        if name in columns:
            raise AWXError(f"Duplicate column {cell.value!r} in worksheet {sheet.title!r}.")
        columns[name] = cell.column
    selected = host_column.strip().casefold() if host_column is not None else next(
        (name for name in HOST_COLUMNS if name in columns), None
    )
    if selected not in columns:
        raise AWXError("Cannot find the host column. Use hostname or host_limit, or specify --host-column HEADER.")
    if any(merged.max_row > 1 for merged in sheet.merged_cells.ranges):
        raise AWXError("Host data rows must not contain merged cells. Use a table with one host per row.")
    rows = {}
    for row_number in range(2, sheet.max_row + 1):
        cell = sheet.cell(row_number, columns[selected])
        if cell.value is None or not str(cell.value).strip():
            continue
        hostname = str(cell.value).strip()
        if cell.data_type == "f" or not runner.HOSTNAME_PATTERN.fullmatch(hostname):
            raise AWXError(f"Row {row_number}: the host column must contain one literal hostname per cell.")
        rows[row_number] = hostname
    if not rows:
        raise AWXError("The host column contains no hostnames.")
    return rows, columns, columns[selected]


def metadata_by_host(report, settings):
    """Reuse inventory lookup/export rules without creating duplicate worksheet rows."""
    headers, rows = inventory.build_output_rows(report)
    metadata = {}
    credentials = {}
    for row in rows:
        values = dict(zip(headers, row))
        hostname = values.pop("hostname")
        metadata[hostname] = values
        found_credentials = credentials.setdefault(hostname, [])
        if values["Credential"] not in found_credentials:
            found_credentials.append(values["Credential"])
    for host in report["hosts"]:
        hostname = host["name"]
        values = metadata[hostname]
        values["Credential"] = inventory.FULL_CREDENTIAL_SEPARATOR.join(credentials[hostname])
        variables = host["Job_Variables"]
        values["Job_Type"] = runner.apply_job_type(variables, None, settings)
        # apply_job_type may add the configured action to the fresh job variables.
        values["Job_Variables"] = json.dumps(variables, ensure_ascii=False)
        values["AWX_Status"] = "found"
    return metadata, [*headers[1:], "Job_Type", "AWX_Status"]


def sort_host_rows(sheet, hostname_column, os_column):
    order = {"windows": 0, "linux": 1, "aix": 2}
    source_rows = list(range(2, sheet.max_row + 1))

    def rank(row_number):
        if not str(sheet.cell(row_number, hostname_column).value or "").strip():
            return 4
        return order.get(str(sheet.cell(row_number, os_column).value or "").strip().casefold(), 3)

    sorted_rows = sorted(source_rows, key=rank)
    if sorted_rows == source_rows:
        return
    # Snapshot complete rows so execution links/output/custom data move with hosts.
    snapshots = {
        row_number: (
            [copy(cell) for cell in sheet[row_number]],
            copy(sheet.row_dimensions[row_number]),
        ) for row_number in source_rows
    }
    for target_row, source_row in zip(source_rows, sorted_rows):
        cells, dimensions = snapshots[source_row]
        dimensions.index = target_row
        sheet.row_dimensions[target_row] = dimensions
        for source in cells:
            target = sheet.cell(target_row, source.column)
            target.value = source.value
            target.data_type = source.data_type
            target._style = copy(source._style)
            target.hyperlink = copy(source.hyperlink)
            target.comment = copy(source.comment)


def refresh_sheet(sheet, host_rows, columns, source_column, metadata, headers):
    missing_values = {name: "none" for name in headers}
    missing_values.update(Job_Variables="{}", Job_Type="", AWX_Status="not_found")
    for name in ["hostname", *headers]:
        if name.casefold() not in columns:
            column = sheet.max_column + 1
            columns[name.casefold()] = column
            header = sheet.cell(1, column)
            header.value = name
            header._style = copy(sheet.cell(1, source_column)._style)
            sheet.column_dimensions[inventory.get_column_letter(column)].width = (
                65 if name in ("Job_Link", "Job_Variables", "Full_Credential") else 22
            )
    for row_number, hostname in host_rows.items():
        values = {"hostname": hostname, **metadata.get(hostname, missing_values)}
        for name, value in values.items():
            cell = sheet.cell(row_number, columns[name.casefold()])
            cell.value = value
            cell.data_type = "s"
            cell.number_format = "@"
            cell.hyperlink = value if name == "Job_Link" and value.startswith(("http://", "https://")) else None
            cell.font = inventory.Font(name="Arial", size=10, color="0563C1" if cell.hyperlink else "1F2937",
                                       underline="single" if cell.hyperlink else None)
            cell.alignment = inventory.Alignment(vertical="center", wrap_text=True)
    sort_host_rows(sheet, columns["hostname"], columns["ostype"])
    sheet.auto_filter.ref = sheet.dimensions
    if not sheet.freeze_panes:
        sheet.freeze_panes = "B2"


def main(argv=None):
    args = parse_arguments(argv)
    connection = inventory.read_settings()
    inventory.apply_settings(connection)
    inventory.configure_debug(args.debug, connection["AWX_TOKEN"].strip())
    settings = runner.read_settings()
    path = Path(args.input_file or settings["INPUT_FILE"] or connection["EXCEL_OUTPUT_FILE"]).resolve()
    if path.suffix.casefold() != ".xlsx":
        raise AWXError("The input file must use the .xlsx extension.")
    try:
        workbook = inventory.load_workbook(path, keep_links=True)
    except (OSError, ValueError, BadZipFile, ParseError, inventory.InvalidFileException) as exc:
        raise AWXError(f"Cannot read {path}: {exc}") from None
    try:
        sheet_name = args.sheet or (settings["SHEET_NAME"] if settings["SHEET_NAME"] in workbook.sheetnames else workbook.active.title)
        if sheet_name not in workbook.sheetnames:
            raise AWXError(f"Worksheet {sheet_name!r} does not exist in {path}.")
        sheet = workbook[sheet_name]
        host_rows, columns, source_column = read_host_rows(sheet, args.host_column)
        hostnames = list(dict.fromkeys(host_rows.values()))
        print(f"Input: {path}; worksheet: {sheet_name}; unique hosts: {len(hostnames)}", flush=True)
        client = inventory.AWXClient(connection["AWX_URL"], connection["AWX_TOKEN"].strip())
        report = inventory.fetch_report(client, connection["INVENTORY"], hostnames=hostnames, allow_empty=True)
        metadata, headers = metadata_by_host(report, settings)
        protected = {settings["EXECUTION_COLUMN"].casefold(), "exceiute_job", "output"}
        managed = {name.casefold() for name in ["hostname", *headers]}
        if protected & managed or columns.get(settings["EXECUTION_COLUMN"].casefold()) == source_column or columns.get("output") == source_column:
            raise AWXError("The host or metadata column conflicts with a job history column.")
        refresh_sheet(sheet, host_rows, columns, source_column, metadata, headers)
        runner.save_workbook(workbook, path)
        missing = report.get("missing_hosts", [])
        if missing:
            print(f"Not found in AWX ({len(missing)}): {', '.join(missing)}", file=sys.stderr)
        print(f"Refreshed: {report['host_count']} unique hosts; not found: {len(missing)}; saved: {path}", flush=True)
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
        print("\nInterrupted. The workbook is saved only after all host data has been fetched.", file=sys.stderr)
        sys.exit(130)
