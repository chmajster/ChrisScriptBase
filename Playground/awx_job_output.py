#!/usr/bin/env python3
"""AWX output launcher. Implementation: lib/awx.py; defaults: awx_variable."""

from lib.awx import run


# Uncomment only the values to override for this launcher.
# Priority: command-line arguments > VARIABLES below > awx_variable.
VARIABLES = {
    # "INPUT_FILE": 'hosts.xlsx',
    # "OUTPUT_COLUMN": 'output',
    # "OUTPUT_DIRECTORY": 'output',
    # "WAIT_FOR_COMPLETION": True,
    # "DEBUG": True,
}


if __name__ == "__main__":
    raise SystemExit(run("output", variables=VARIABLES))
