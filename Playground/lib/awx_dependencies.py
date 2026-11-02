"""Install missing Excel dependencies only inside the running Python's venv."""

import importlib
import importlib.util
import subprocess
import sys


INSTALL_HINT = "python -m pip install openpyxl"


def is_virtual_environment():
    """Use interpreter state; VIRTUAL_ENV alone does not prove Python uses a venv."""
    return hasattr(sys, "real_prefix") or sys.prefix != getattr(sys, "base_prefix", sys.prefix)


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

    print("The openpyxl package is missing; installing it in the current virtual environment...",
          file=sys.stderr, flush=True)
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
