"""Bootstrap the project's venv and install missing Excel dependencies there."""

import importlib
import importlib.util
import os
import subprocess
import sys
import venv
from pathlib import Path

from .awx_console import print_message


INSTALL_HINT = "python -m pip install openpyxl"


def is_virtual_environment():
    """Use interpreter state; VIRTUAL_ENV alone does not prove Python uses a venv."""
    return hasattr(sys, "real_prefix") or sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def ensure_virtual_environment():
    """Create/reuse .venv beside the launchers, then run with its interpreter.

    Run this before importing third-party packages, only in a CLI entry point.
    An already running venv (including one outside this project) is respected.
    """
    if is_virtual_environment():
        return

    environment = Path(__file__).resolve().parent.parent / ".venv"
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # In particular, Python < 3.14 with -S can fail to recognise its own venv.
    if os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(str(interpreter)):
        raise SystemExit(f"Python at {interpreter} did not activate its virtual environment. "
                         "Run without -S or recreate .venv.")

    try:
        if not environment.exists():
            print_message(f"Creating virtual environment: {environment}",
                          level="info", file=sys.stderr, flush=True)
            venv.EnvBuilder(with_pip=True).create(environment)
        if not (environment / "pyvenv.cfg").is_file() or not interpreter.is_file():
            raise SystemExit(f"Incomplete virtual environment at {environment}. "
                             "Rename or remove it, then run the script again.")

        original_arguments = getattr(sys, "orig_argv", None)
        if original_arguments:
            arguments = list(original_arguments[1:])
        else:
            # Python 3.9 does not expose orig_argv; preserve module invocation.
            main_spec = getattr(sys.modules["__main__"], "__spec__", None)
            arguments = (["-m", main_spec.name, *sys.argv[1:]]
                         if main_spec is not None else list(sys.argv))
        print_message(f"Restarting with virtual environment: {interpreter}",
                      level="info", file=sys.stderr, flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
        command = [str(interpreter), *arguments]
        if os.name == "nt":
            # Windows execv exits the process PowerShell waits for, letting the
            # shell compete with input() in the child. Keep this parent alive
            # and inherit the console streams until the venv process finishes.
            raise SystemExit(subprocess.call(command))
        os.execv(str(interpreter), command)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Cannot start the virtual environment at {environment}: {exc}") from None


def ensure_openpyxl():
    """Import openpyxl, installing it with this interpreter if it is missing in a venv."""
    try:
        return importlib.import_module("openpyxl")
    except ModuleNotFoundError as exc:
        if exc.name != "openpyxl":
            raise SystemExit(f"Cannot import openpyxl: {exc}. Repair the installation with: {INSTALL_HINT}") from None
    except ImportError as exc:
        raise SystemExit(f"Cannot import openpyxl: {exc}. Repair the installation with: {INSTALL_HINT}") from None

    if not is_virtual_environment():
        raise SystemExit(f"The openpyxl package is missing. Install it with: {INSTALL_HINT}") from None

    print_message("The openpyxl package is missing; installing it in the current virtual environment...",
                  level="warning", file=sys.stderr, flush=True)
    try:
        if importlib.util.find_spec("pip") is None:
            subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"], check=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "openpyxl"], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Cannot install openpyxl in the current virtual environment: {exc}. "
                         f"Activate it and run: {INSTALL_HINT}") from None

    importlib.invalidate_caches()
    try:
        return importlib.import_module("openpyxl")
    except ImportError as exc:
        raise SystemExit(f"openpyxl is still unavailable after installation: {exc}. "
                         f"Repair the virtual environment with: {INSTALL_HINT}") from None
