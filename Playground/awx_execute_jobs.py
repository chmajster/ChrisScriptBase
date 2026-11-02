#!/usr/bin/env python3
"""AWX execute launcher. Implementation: lib/awx.py; defaults: awx_variable."""

from lib.awx import run


# Uncomment only the values to override for this launcher.
# Priority: command-line arguments > VARIABLES below > awx_variable.
VARIABLES = {
    # "INPUT_FILE": 'hosts.xlsx',
    # "BATCH_SIZE": 10,
    # "JOB_TYPE": 'install',
    # "JOB_TYPE_VARIABLE": 'action',
    # "WAIT_FOR_COMPLETION": True,
    # "DRY_RUN": True,
    # "DEBUG": True,
}


if __name__ == "__main__":
    raise SystemExit(run("execute", variables=VARIABLES))
