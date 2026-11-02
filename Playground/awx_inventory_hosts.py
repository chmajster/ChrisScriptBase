#!/usr/bin/env python3
"""AWX inventory launcher. Implementation: lib/awx.py; defaults: awx_variable."""

from lib.awx import run


# Uncomment only the values to override for this launcher.
# Priority: command-line arguments > VARIABLES below > awx_variable.
VARIABLES = {
    # "AWX_URL": 'https://awx.example.com',
    # "AWX_TOKEN": 'your-token',
    # "INVENTORY": 'inventory-name',
    # "EXCEL_OUTPUT_FILE": 'hosts.xlsx',
    # "INPUT_FILE": 'host-list.xlsx',
    # "DEBUG": True,
}


if __name__ == "__main__":
    raise SystemExit(run("inventory", variables=VARIABLES))
