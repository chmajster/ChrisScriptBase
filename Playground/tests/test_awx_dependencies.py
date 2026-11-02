"""Dependency installation tests never invoke a real package installer."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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


class VirtualEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="awx bootstrap ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.environment = self.root / ".venv"
        self.interpreter = self.environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.filename = patch.object(dependencies, "__file__", str(self.root / "lib" / "awx_dependencies.py"))
        self.filename.start()
        self.addCleanup(self.filename.stop)

    def fake_environment(self, _path=None):
        self.interpreter.parent.mkdir(parents=True)
        self.interpreter.touch()
        (self.environment / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")

    def run_bootstrap(self):
        if dependencies.os.name == "nt":
            with self.assertRaises(SystemExit) as result:
                dependencies.ensure_virtual_environment()
            self.assertEqual(result.exception.code, 0)
        else:
            dependencies.ensure_virtual_environment()

    def test_active_venv_is_respected_without_creating_or_restarting(self):
        with patch.object(dependencies, "is_virtual_environment", return_value=True), \
             patch.object(dependencies.venv, "EnvBuilder") as builder, \
             patch.object(dependencies.os, "execv") as restart:
            dependencies.ensure_virtual_environment()
        builder.assert_not_called()
        restart.assert_not_called()
        self.assertFalse(self.environment.exists())

    def test_missing_venv_is_created_with_pip_and_original_command_is_preserved(self):
        original = ["python", "-B", "-m", "lib.awx", "refresh", "--file", "hosts with spaces.xlsx"]
        with patch.object(dependencies, "is_virtual_environment", return_value=False), \
             patch.object(dependencies.venv, "EnvBuilder") as builder, \
             patch.object(dependencies.sys, "orig_argv", original, create=True), \
             patch.object(dependencies.subprocess, "call", return_value=0) as child, \
             patch.object(dependencies.os, "execv") as restart, contextlib.redirect_stderr(io.StringIO()):
            builder.return_value.create.side_effect = self.fake_environment
            self.run_bootstrap()
        builder.assert_called_once_with(with_pip=True)
        builder.return_value.create.assert_called_once_with(self.environment)
        arguments = [str(self.interpreter), *original[1:]]
        if os.name == "nt":
            child.assert_called_once_with(arguments)
            restart.assert_not_called()
        else:
            restart.assert_called_once_with(str(self.interpreter), arguments)
            child.assert_not_called()

    def test_existing_venv_is_reused(self):
        self.fake_environment()
        with patch.object(dependencies, "is_virtual_environment", return_value=False), \
             patch.object(dependencies.venv, "EnvBuilder") as builder, \
             patch.object(dependencies.subprocess, "call", return_value=0) as child, \
             patch.object(dependencies.os, "execv") as restart, contextlib.redirect_stderr(io.StringIO()):
            self.run_bootstrap()
        builder.assert_not_called()
        if os.name == "nt":
            self.assertEqual(child.call_args.args[0][0], str(self.interpreter))
        else:
            self.assertEqual(restart.call_args.args[0], str(self.interpreter))

    def test_incomplete_venv_is_reported_without_overwriting_it(self):
        self.environment.mkdir()
        marker = self.environment / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with patch.object(dependencies, "is_virtual_environment", return_value=False), \
             patch.object(dependencies.venv, "EnvBuilder") as builder, \
             patch.object(dependencies.os, "execv") as restart:
            with self.assertRaisesRegex(SystemExit, "Incomplete virtual environment"):
                dependencies.ensure_virtual_environment()
        builder.assert_not_called()
        restart.assert_not_called()
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_unrecognised_project_venv_stops_instead_of_restarting_forever(self):
        with patch.object(dependencies, "is_virtual_environment", return_value=False), \
             patch.object(dependencies.sys, "executable", str(self.interpreter)), \
             patch.object(dependencies.os, "execv") as restart:
            with self.assertRaisesRegex(SystemExit, "Run without -S"):
                dependencies.ensure_virtual_environment()
        restart.assert_not_called()

    def test_creation_failure_is_reported_without_restart(self):
        with patch.object(dependencies, "is_virtual_environment", return_value=False), \
             patch.object(dependencies.venv, "EnvBuilder") as builder, \
             patch.object(dependencies.os, "execv") as restart, contextlib.redirect_stderr(io.StringIO()):
            builder.return_value.create.side_effect = OSError("permission denied")
            with self.assertRaisesRegex(SystemExit, "Cannot start the virtual environment.*permission denied"):
                dependencies.ensure_virtual_environment()
        restart.assert_not_called()

    def test_windows_waits_for_child_while_posix_uses_execv(self):
        arguments = ["probe.py", "with spaces", "", 'a\"b']
        for platform in ("nt", "posix"):
            interpreter = self.environment / ("Scripts/python.exe" if platform == "nt" else "bin/python")
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            interpreter.touch()
            (self.environment / "pyvenv.cfg").touch()
            with self.subTest(platform=platform), \
                 patch.object(dependencies, "is_virtual_environment", return_value=False), \
                 patch.object(dependencies, "os", wraps=os) as platform_os, \
                 patch.object(dependencies.sys, "orig_argv", ["python", *arguments], create=True), \
                 patch.object(dependencies.subprocess, "call", return_value=0) as child, \
                 contextlib.redirect_stderr(io.StringIO()):
                platform_os.name = platform
                platform_os.execv.return_value = None
                self.run_bootstrap()
                expected = [str(interpreter), *arguments]
                if platform == "nt":
                    child.assert_called_once_with(expected)
                    platform_os.execv.assert_not_called()
                else:
                    platform_os.execv.assert_called_once_with(str(interpreter), expected)
                    child.assert_not_called()

    def test_python39_fallback_preserves_script_and_module_arguments(self):
        self.fake_environment()
        for spec, expected_args in ((None, ["probe.py", "--help"]),
                                    (SimpleNamespace(name="lib.awx"), ["-m", "lib.awx", "--help"])):
            with self.subTest(spec=spec), \
                 patch.object(dependencies, "is_virtual_environment", return_value=False), \
                 patch.object(dependencies.sys, "orig_argv", None, create=True), \
                 patch.object(dependencies.sys, "argv", ["probe.py", "--help"]), \
                 patch.object(sys.modules['__main__'], "__spec__", spec, create=True), \
                 patch.object(dependencies.subprocess, "call", return_value=0) as child, \
                 patch.object(dependencies.os, "execv") as restart, contextlib.redirect_stderr(io.StringIO()):
                self.run_bootstrap()
                expected = [str(self.interpreter), *expected_args]
                if os.name == "nt":
                    child.assert_called_once_with(expected)
                else:
                    restart.assert_called_once_with(str(self.interpreter), expected)

    def test_real_restart_creates_and_reuses_venv_and_preserves_arguments(self):
        project_lib = self.root / "lib"
        project_lib.mkdir()
        source_lib = Path(__file__).resolve().parent.parent / "lib"
        for filename in ("__init__.py", "awx_dependencies.py", "awx_console.py"):
            shutil.copyfile(source_lib / filename, project_lib / filename)
        probe = self.root / "probe.py"
        probe.write_text(
            "import json, sys\n"
            "from lib.awx_dependencies import ensure_virtual_environment\n"
            "ensure_virtual_environment()\n"
            "if '--interactive' in sys.argv:\n"
            "    print('READY', flush=True)\n"
            "    answer = input()\n"
            "    print(json.dumps({'answer': answer}), flush=True)\n"
            "    sys.exit(7)\n"
            "print(json.dumps({'prefix': sys.prefix, 'base': sys.base_prefix, "
            "'args': sys.argv[1:], 'no_bytecode': sys.dont_write_bytecode}))\n",
            encoding="utf-8")
        arguments = ["value with spaces", "", 'a\"quoted\"value', "za\u017c\u00f3\u0142\u0107", "trailing space "]
        commands = [([str(probe), *arguments], self.root.parent),
                    (["-m", "probe", *arguments], self.root)]
        for command, working_directory in commands:
            with self.subTest(command=command):
                base_python = getattr(sys, "_base_executable", sys.executable)
                result = subprocess.run([base_python, "-B", "-X", "utf8", *command], cwd=working_directory,
                                        capture_output=True, text=True, encoding="utf-8", timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(Path(payload['prefix']), self.environment)
                self.assertNotEqual(payload['prefix'], payload['base'])
                self.assertEqual(payload['args'], arguments)
                self.assertTrue(payload['no_bytecode'])
        self.assertTrue(self.interpreter.is_file())

        # The original process must remain alive while the restarted program
        # waits for input; otherwise PowerShell can take over the console.
        with subprocess.Popen([base_python, "-B", "-X", "utf8", str(probe), "--interactive"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8") as process:
            self.assertEqual(process.stdout.readline().strip(), 'READY')
            parent_still_running = process.poll() is None
            output, errors = process.communicate('tak\n', timeout=15)
            self.assertTrue(parent_still_running, 'Parent exited before the interactive answer')
            self.assertEqual(process.returncode, 7, errors)
            self.assertEqual(json.loads(output), {'answer': 'tak'})


if __name__ == "__main__":
    unittest.main()
