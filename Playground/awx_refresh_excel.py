#!/usr/bin/env python3
"""AWX refresh launcher. Implementation: lib/awx.py; defaults: awx_variable."""

from lib.awx import run


# Uncomment only the values to override for this launcher.
# Priority: command-line arguments > VARIABLES below > awx_variable.
VARIABLES = {
    # "INPUT_FILE": 'hosts.xlsx',
    # "INVENTORY": 'inventory-name',
    # "SHEET_NAME": 'Results',
    # "HOST_COLUMN": 'hostname',
    # "JOB_TYPE": 'install',
    # "DEBUG": True,
}


if __name__ == "__main__":
    raise SystemExit(run("refresh", variables=VARIABLES))
