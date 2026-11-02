"""Terminal coloring, redaction and CLI/config precedence without live AWX."""

import contextlib
import io
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import awx
from lib import awx_config as config
from lib import awx_console as console


class Stream(io.StringIO):
    def __init__(self, terminal=False):
        super().__init__()
        self.terminal = terminal

    def isatty(self):
        return self.terminal


class ConsoleTests(unittest.TestCase):
    def test_auto_colors_terminal_and_resets_at_end(self):
        with console.color_mode("auto"), patch.dict(console.os.environ, {"NO_COLOR": "", "TERM": "xterm"}), \
             patch.object(console, "_enable_windows_vt", return_value=True):
            self.assertEqual(console.colorize("done", "success", stream=Stream(True)), "\033[32mdone\033[0m")

    def test_auto_leaves_redirected_output_plain(self):
        with console.color_mode("auto"):
            self.assertEqual(console.colorize("message", "error", stream=Stream()), "message")

    def test_always_colors_a_pipe_and_never_disables_a_terminal(self):
        with console.color_mode("always"):
            self.assertEqual(console.colorize("failed", "error", stream=Stream()), "\033[91mfailed\033[0m")
        with console.color_mode("never"):
            self.assertEqual(console.colorize("done", "success", stream=Stream(True)), "done")

    def test_no_color_environment_disables_auto_but_explicit_force_wins(self):
        with patch.dict(console.os.environ, {"NO_COLOR": "1", "TERM": "xterm"}), console.color_mode("auto"):
            self.assertEqual(console.colorize("warning", "warning", stream=Stream(True)), "warning")
            with console.color_mode("always"):
                self.assertIn("\033[33m", console.colorize("warning", "warning", stream=Stream()))

    def test_dumb_terminal_disables_automatic_colors(self):
        with patch.dict(console.os.environ, {"NO_COLOR": "", "TERM": "dumb"}), console.color_mode("auto"):
            self.assertEqual(console.colorize("message", stream=Stream(True)), "message")

    def test_unsupported_windows_console_falls_back_to_plain_text(self):
        with patch.dict(console.os.environ, {"NO_COLOR": "", "TERM": "xterm"}), \
             patch.object(console.sys, "platform", "win32"), patch.object(console, "_enable_windows_vt", return_value=False), \
             console.color_mode("auto"):
            self.assertEqual(console.colorize("message", stream=Stream(True)), "message")

    def test_stdout_and_stderr_are_checked_independently(self):
        output, errors = Stream(False), Stream(True)
        with patch.object(console.sys, "stdout", output), patch.object(console.sys, "stderr", errors), \
             patch.dict(console.os.environ, {"NO_COLOR": "", "TERM": "xterm"}), \
             patch.object(console, "_enable_windows_vt", return_value=True), console.color_mode("auto"):
            console.print_message("plain")
            console.print_message("error", level="error", file=errors)
        self.assertEqual(output.getvalue(), "plain\n")
        self.assertEqual(errors.getvalue(), "\033[91merror\033[0m\n")

    def test_print_keeps_separator_and_end_outside_color_reset(self):
        output = Stream()
        with console.color_mode("always"):
            console.print_message("a", "b", sep="|", end="!", file=output, level="progress")
        self.assertEqual(output.getvalue(), "\033[94ma|b\033[0m!")

    def test_debug_token_is_redacted_before_adding_color(self):
        record = logging.LogRecord("test", logging.DEBUG, "test", 1, "token=secret", (), None)
        with console.color_mode("always"):
            message = awx.DebugFormatter("secret", stream=Stream()).format(record)
        self.assertEqual(message, "\033[90m[DEBUG] token=***\033[0m")
        self.assertNotIn("secret", message)

    def test_job_statuses_use_distinct_levels(self):
        for status, expected in {"successful": "success", "failed": "error", "error": "error", "canceled": "warning",
                                 "pending": "warning", "waiting": "warning", "running": "progress"}.items():
            with self.subTest(status=status):
                self.assertEqual(console.job_status_level(status), expected)

    def test_cli_color_wins_over_variables_and_run_restores_previous_mode(self):
        def operation(argv):
            awx.parse_execute_arguments(argv)
            console.print_message("batch", level="progress")
            raise awx.AWXError("test error")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "variables"
            path.write_text("DEBUG = False\nDRY_RUN = False\nHOST_COLUMN = None\nCOLOR = 'auto'\n", encoding="utf-8")
            for argv, setting, expected_ansi in ((["--color", "always"], "never", True), (["--no-color"], "always", False)):
                with self.subTest(argv=argv), patch.dict(awx.OPERATIONS, {"execute": operation}), \
                     contextlib.redirect_stdout(Stream()) as output, contextlib.redirect_stderr(Stream()) as errors, \
                     console.color_mode("auto"):
                    self.assertEqual(awx.run("execute", argv, variables={"COLOR": setting}, variables_file=path), 1)
                    self.assertEqual("\033[" in output.getvalue(), expected_ansi)
                    self.assertEqual("\033[" in errors.getvalue(), expected_ansi)
                    self.assertEqual(console.colorize("plain", stream=Stream()), "plain")

    def test_invalid_color_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "variables"
            path.write_text("DEBUG = False\nDRY_RUN = False\nHOST_COLUMN = None\nCOLOR = 'invalid'\n", encoding="utf-8")
            with config.launcher_configuration("execute", variables_file=path):
                with self.assertRaisesRegex(config.AWXError, "COLOR must be"):
                    config.read_runtime_settings()


if __name__ == "__main__":
    unittest.main()
