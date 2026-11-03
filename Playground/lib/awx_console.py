"""Shared terminal colors without adding dependencies or changing saved data."""

import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar


COLOR_MODES = ("auto", "always", "never")
RESET = "\033[0m"
LEVEL_COLORS = {
    "debug": "\033[90m",
    "info": "\033[36m",
    "data": "\033[36m",
    "progress": "\033[94m",
    "success": "\033[32m",
    "warning": "\033[33m",
    "error": "\033[91m",
}
_MODE = ContextVar("awx_terminal_color_mode", default="auto")
_WINDOWS_VT_HANDLES = set()


def set_color_mode(mode):
    if mode not in COLOR_MODES:
        raise ValueError("Color mode must be auto, always, or never.")
    return _MODE.set(mode)


def reset_color_mode(token):
    _MODE.reset(token)


@contextmanager
def color_mode(mode):
    token = set_color_mode(mode)
    try:
        yield
    finally:
        reset_color_mode(token)


def _is_terminal(stream):
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _enable_windows_vt(stream):
    """Enable ANSI sequences in the current Windows console, including PowerShell."""
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(stream.fileno())
        if handle in _WINDOWS_VT_HANDLES:
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetConsoleMode.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if not kernel32.SetConsoleMode(handle, mode.value | 0x0004):
            return False
        _WINDOWS_VT_HANDLES.add(handle)
        return True
    except (ImportError, AttributeError, OSError, ValueError):
        return False


def colors_enabled(stream):
    mode = _MODE.get()
    if mode == "never":
        return False
    terminal = _is_terminal(stream)
    if mode == "always":
        if terminal and sys.platform == "win32":
            _enable_windows_vt(stream)
        return True
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb" or not terminal:
        return False
    return sys.platform != "win32" or _enable_windows_vt(stream)


def colorize(message, level="info", *, stream=None):
    stream = sys.stdout if stream is None else stream
    code = LEVEL_COLORS.get(level)
    return f"{code}{message}{RESET}" if code and colors_enabled(stream) else message


def print_message(*values, level="info", file=None, sep=" ", end="\n", flush=False):
    stream = sys.stdout if file is None else file
    message = sep.join(str(value) for value in values)
    print(colorize(message, level, stream=stream), file=stream, end=end, flush=flush)


def job_status_level(status):
    if status == "successful":
        return "success"
    if status in ("failed", "error"):
        return "error"
    if status in ("new", "pending", "waiting", "canceled"):
        return "warning"
    return "progress"


def job_status_description(status):
    """Wyjaśnij status zadania, zachowując nazwę zwróconą przez API AWX."""
    descriptions = {
        "new": "utworzone, jeszcze nieuruchomione",
        "pending": "oczekuje w kolejce na uruchomienie",
        "waiting": "oczekuje na dostępne zasoby lub zależności",
        "running": "w trakcie wykonywania",
        "successful": "zakończone pomyślnie",
        "failed": "zakończone niepowodzeniem",
        "error": "błąd wykonania po stronie AWX",
        "canceled": "anulowane",
    }
    return f"{descriptions.get(status, 'nieznany status')} ({status or 'brak statusu'})"
