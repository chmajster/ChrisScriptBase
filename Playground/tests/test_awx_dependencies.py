"""Dependency installation tests never invoke a real package installer."""

import contextlib
import io
import subprocess
import unittest
from unittest.mock import patch, sentinel

from lib import awx_dependencies as dependencies


def missing_openpyxl():
    return ModuleNotFoundError("No module named 'openpyxl'", name="openpyxl")


class DependencyTests(unittest.TestCase):
    def test_installed_package_is_used_without_running_installer(self):
        with patch.object(dependencies.importlib, "import_module", return_value=sentinel.package), \
             patch.object(dependencies.subprocess, "run") as run:
            self.assertIs(dependencies.ensure_openpyxl(), sentinel.package)
            run.assert_not_called()

    def test_missing_package_outside_venv_requires_manual_installation(self):
        with patch.object(dependencies.importlib, "import_module", side_effect=missing_openpyxl()), \
             patch.object(dependencies, "is_virtual_environment", return_value=False), \
             patch.object(dependencies.subprocess, "run") as run:
            with self.assertRaisesRegex(SystemExit, "python -m pip install openpyxl"):
                dependencies.ensure_openpyxl()
            run.assert_not_called()

    def test_missing_package_in_venv_installs_with_current_interpreter_and_reimports(self):
        with patch.object(dependencies.importlib, "import_module", side_effect=[missing_openpyxl(), sentinel.package]) as imports, \
             patch.object(dependencies, "is_virtual_environment", return_value=True), \
             patch.object(dependencies.importlib.util, "find_spec", return_value=sentinel.pip), \
             patch.object(dependencies.importlib, "invalidate_caches") as invalidate, \
             patch.object(dependencies.subprocess, "run") as run, contextlib.redirect_stderr(io.StringIO()):
            self.assertIs(dependencies.ensure_openpyxl(), sentinel.package)
            run.assert_called_once_with([dependencies.sys.executable, "-m", "pip", "install", "openpyxl"], check=True)
            self.assertEqual(imports.call_count, 2)
            invalidate.assert_called_once()

    def test_venv_without_pip_bootstraps_pip_before_installing(self):
        with patch.object(dependencies.importlib, "import_module", side_effect=[missing_openpyxl(), sentinel.package]), \
             patch.object(dependencies, "is_virtual_environment", return_value=True), \
             patch.object(dependencies.importlib.util, "find_spec", return_value=None), \
             patch.object(dependencies.subprocess, "run") as run, contextlib.redirect_stderr(io.StringIO()):
            dependencies.ensure_openpyxl()
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                [dependencies.sys.executable, "-m", "ensurepip", "--upgrade"],
                [dependencies.sys.executable, "-m", "pip", "install", "openpyxl"],
            ])

    def test_failed_installation_stops_without_reimport_or_retry(self):
        with patch.object(dependencies.importlib, "import_module", side_effect=missing_openpyxl()) as imports, \
             patch.object(dependencies, "is_virtual_environment", return_value=True), \
             patch.object(dependencies.importlib.util, "find_spec", return_value=sentinel.pip), \
             patch.object(dependencies.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["pip"])) as run, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, "Cannot install openpyxl"):
                dependencies.ensure_openpyxl()
            imports.assert_called_once_with("openpyxl")
            run.assert_called_once()

    def test_failed_reimport_after_installation_stops_with_clear_error(self):
        with patch.object(dependencies.importlib, "import_module", side_effect=[missing_openpyxl(), missing_openpyxl()]), \
             patch.object(dependencies, "is_virtual_environment", return_value=True), \
             patch.object(dependencies.importlib.util, "find_spec", return_value=sentinel.pip), \
             patch.object(dependencies.subprocess, "run") as run, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, "still unavailable after installation"):
                dependencies.ensure_openpyxl()
            run.assert_called_once()

    def test_internal_import_error_does_not_trigger_an_install(self):
        errors = [ModuleNotFoundError("No module named 'other_dependency'", name="other_dependency"),
                  ImportError("Cannot import an incompatible symbol")]
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                 patch.object(dependencies.importlib, "import_module", side_effect=error), \
                 patch.object(dependencies.subprocess, "run") as run:
                with self.assertRaisesRegex(SystemExit, "Cannot import openpyxl"):
                    dependencies.ensure_openpyxl()
                run.assert_not_called()

    def test_venv_detection_uses_python_prefixes(self):
        with patch.object(dependencies.sys, "prefix", "venv"), patch.object(dependencies.sys, "base_prefix", "base"):
            self.assertTrue(dependencies.is_virtual_environment())
        with patch.object(dependencies.sys, "prefix", "base"), patch.object(dependencies.sys, "base_prefix", "base"), \
             patch.dict("os.environ", {"VIRTUAL_ENV": "some-other-environment"}):
            self.assertFalse(dependencies.is_virtual_environment())

    def test_legacy_virtualenv_is_recognized(self):
        with patch.object(dependencies.sys, "real_prefix", "base", create=True):
            self.assertTrue(dependencies.is_virtual_environment())


if __name__ == "__main__":
    unittest.main()
