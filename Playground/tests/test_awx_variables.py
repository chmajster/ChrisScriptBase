"""Single variables file, launcher overrides and CLI precedence without live AWX."""

import contextlib
import io
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from lib import awx
from lib import awx_config as config


class VariablesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "awx_variable"
        self.values = config.read_variables_file(config.VARIABLES_FILE)
        self.values.update(
            AWX_URL="http://awx.test", AWX_TOKEN="test-token", INVENTORY="base_inventory",
            EXCEL_OUTPUT_FILE=str(self.root / "hosts.xlsx"), OUTPUT_FILE=str(self.root / "hosts.json"),
            INPUT_FILE=None, SHEET_NAME="Results", BATCH_SIZE=10, JOB_TYPE="install", DEBUG=False,
            DRY_RUN=False, HOST_COLUMN=None, EXECUTION_COLUMN="exceiute_job", OUTPUT_COLUMN="output",
            HOSTS=[], JOB_URL="",
            SURVEY_ANSWERS={}, SURVEY_EXCEL_DEFAULTS={}, SURVEY_INTERACTIVE=False,
            OS_JOBS={"linux": {"url": "http://awx.test/#/templates/job_template/7/details", "variables": {"channel": "stable"}},
                     "windows": {"url": "", "variables": {}}},
        )
        for name in config.OPERATION_SCOPES.values():
            self.values[name] = {}
        self.write_values()
        default_file = patch.object(config, "VARIABLES_FILE", self.path)
        default_file.start()
        self.addCleanup(default_file.stop)
        self.addCleanup(awx.apply_settings, awx.INVENTORY_DEFAULTS)

    def write_values(self):
        self.path.write_text("\n".join(f"{name} = {value!r}" for name, value in self.values.items()) + "\n", encoding="utf-8")

    def test_all_groups_use_the_same_file_by_default(self):
        self.assertEqual(awx.read_inventory_settings()["INVENTORY"], "base_inventory")
        self.assertEqual(config.read_execution_settings()["BATCH_SIZE"], 10)
        self.assertEqual(config.read_output_settings()["OUTPUT_COLUMN"], "output")
        self.assertEqual(config.current_variables_file(), self.path)

    def test_all_launchers_accept_the_multiline_os_jobs_layout(self):
        content = self.path.read_text(encoding="utf-8")
        original = "OS_JOBS = " + repr(self.values["OS_JOBS"])
        replacement = "OS_JOBS = {'linux': {'url': '\n https://awx.test/#/templates/job_template/7/details\n', 'variables': {'action': 'install'}}}"
        self.assertIn(original, content)
        self.path.write_text(content.replace(original, replacement), encoding="utf-8")
        before = self.path.read_bytes()
        for operation in config.OPERATION_SCOPES:
            with self.subTest(operation=operation), config.launcher_configuration(operation):
                jobs = awx.read_inventory_settings()["OS_JOBS"]
                self.assertEqual(jobs["linux"]["url"], "https://awx.test/#/templates/job_template/7/details")
                self.assertEqual(jobs["linux"]["variables"], {"action": "install"})
        self.assertEqual(self.path.read_bytes(), before)

    def test_launcher_overrides_file_and_operation_overrides(self):
        self.values["EXECUTE_OVERRIDES"] = {"BATCH_SIZE": 4, "JOB_TYPE": "update"}
        self.write_values()
        with config.launcher_configuration("execute", {"BATCH_SIZE": 2, "JOB_TYPE": "reinstall"}):
            settings = config.read_execution_settings()
            self.assertEqual(settings["BATCH_SIZE"], 2)
            self.assertEqual(settings["JOB_TYPE"], "reinstall")
        self.assertEqual(config.read_execution_settings()["BATCH_SIZE"], 4)

    def test_operation_overrides_apply_to_nested_readers(self):
        self.values["OUTPUT_OVERRIDES"] = {"SHEET_NAME": "Logs", "WAIT_FOR_COMPLETION": False}
        self.write_values()
        with config.launcher_configuration("output"):
            self.assertEqual(config.read_output_settings()["SHEET_NAME"], "Logs")
            self.assertEqual(config.read_execution_settings()["SHEET_NAME"], "Logs")
            self.assertFalse(config.read_output_settings()["WAIT_FOR_COMPLETION"])
        with config.launcher_configuration("refresh"):
            self.assertEqual(config.read_execution_settings()["SHEET_NAME"], "Results")

    def test_nested_job_overrides_preserve_unspecified_file_values(self):
        overrides = {"OS_JOBS": {"linux": {"url": "http://awx.test/#/templates/job_template/8/details"}}}
        before = deepcopy(overrides)
        with config.launcher_configuration("refresh", overrides):
            jobs = awx.read_inventory_settings()["OS_JOBS"]
            self.assertIn("/8/", jobs["linux"]["url"])
            self.assertEqual(jobs["linux"]["variables"], {"channel": "stable"})
            self.assertIn("windows", jobs)
            jobs["linux"]["variables"]["channel"] = "modified"
            self.assertEqual(awx.read_inventory_settings()["OS_JOBS"]["linux"]["variables"]["channel"], "stable")
        self.assertEqual(overrides, before)

    def test_overrides_are_isolated_between_runs_and_do_not_change_file(self):
        before = self.path.read_bytes()
        with config.launcher_configuration("inventory", {"INVENTORY": "override_inventory"}):
            self.assertEqual(awx.read_inventory_settings()["INVENTORY"], "override_inventory")
        with config.launcher_configuration("inventory"):
            self.assertEqual(awx.read_inventory_settings()["INVENTORY"], "base_inventory")
        self.assertEqual(self.path.read_bytes(), before)

    def test_one_run_uses_a_consistent_file_snapshot(self):
        with config.launcher_configuration("execute"):
            self.assertEqual(config.read_execution_settings()["BATCH_SIZE"], 10)
            self.values["BATCH_SIZE"] = 5
            self.write_values()
            self.assertEqual(config.read_execution_settings()["BATCH_SIZE"], 10)
        self.assertEqual(config.read_execution_settings()["BATCH_SIZE"], 5)

    def test_cli_flags_override_launcher_and_file_runtime_values(self):
        self.values.update(DEBUG=True, DRY_RUN=True)
        self.write_values()
        with config.launcher_configuration("execute", {"DEBUG": True, "DRY_RUN": True}):
            args = awx.parse_execute_arguments(["--no-debug", "--no-dry-run"])
            self.assertFalse(args.debug)
            self.assertFalse(args.dry_run)
        with config.launcher_configuration("refresh", {"HOST_COLUMN": "servers"}):
            self.assertEqual(awx.parse_refresh_arguments([]).host_column, "servers")
            self.assertEqual(awx.parse_refresh_arguments(["--host-column", "hostname"]).host_column, "hostname")

    def test_actual_dry_run_respects_cli_batch_size_before_launcher_defaults(self):
        path = self.root / "input.xlsx"
        workbook = awx.excel.Workbook()
        sheet = workbook.active
        sheet.title = "Results"
        sheet.append(["hostname", "Job_Link", "Job_Variables", "Full_Credential"])
        for hostname in ("host-1", "host-2"):
            sheet.append([hostname, "http://awx.test/#/templates/job_template/7/details", "{}", "machine-cred"])
        workbook.save(path)
        workbook.close()
        before = path.read_bytes()
        console = io.StringIO()
        with patch.object(awx.AWXClient, "request", side_effect=AssertionError("No API requests allowed")), \
             contextlib.redirect_stdout(console):
            code = awx.run("execute", ["--batch-size", "1", "--job-type", "reinstall"],
                           variables={"INPUT_FILE": str(path), "BATCH_SIZE": 2, "DRY_RUN": True}, variables_file=self.path)
        self.assertEqual(code, 0)
        self.assertIn("batch size: 1; batches: 2", console.getvalue())
        self.assertIn("reinstall", console.getvalue())
        self.assertEqual(path.read_bytes(), before)

    def test_bad_override_reports_error_without_reading_awx(self):
        with patch.object(awx, "AWXClient", side_effect=AssertionError("No API requests allowed")), \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            code = awx.run("refresh", [], variables={"BATCH_SIZEE": 1})
        self.assertEqual(code, 1)
        self.assertIn("Unknown", errors.getvalue())

    def test_missing_variables_file_has_clear_error(self):
        with config.launcher_configuration("inventory", variables_file=self.root / "missing"):
            with self.assertRaisesRegex(config.AWXError, "Cannot find variables file"):
                awx.read_inventory_settings()

    def test_host_and_job_url_launcher_values_are_normalized(self):
        with config.launcher_configuration("execute", {"HOSTS": "host-2,host-1 host-2", "JOB_URL": " http://awx.test/#/templates/job_template/9/details "}):
            settings = config.read_execution_settings()
            self.assertEqual(settings["HOSTS"], ["host-2", "host-1"])
            self.assertEqual(settings["JOB_URL"], "http://awx.test/#/templates/job_template/9/details")

    def test_cli_hosts_and_url_override_launcher_values_in_a_real_dry_run(self):
        path = self.root / "input.xlsx"
        workbook = awx.excel.Workbook()
        sheet = workbook.active
        sheet.title = "Results"
        sheet.append(["hostname", "Job_Link", "Job_Variables", "Full_Credential"])
        for hostname in ("host-1", "host-2"):
            sheet.append([hostname, "http://awx.test/#/templates/job_template/7/details", "{}", "machine-cred"])
        workbook.save(path)
        workbook.close()
        before = path.read_bytes()
        output = io.StringIO()
        with patch.object(awx.AWXClient, "request", side_effect=AssertionError("No API requests allowed")), \
             contextlib.redirect_stdout(output):
            code = awx.run("execute", ["--hosts", "host-1", "--job-url", "http://awx.test/#/templates/job_template/8/details", "--dry-run"],
                           variables={"INPUT_FILE": str(path), "HOSTS": ["host-2"], "JOB_URL": "http://awx.test/#/templates/job_template/9/details"},
                           variables_file=self.path)
        self.assertEqual(code, 0)
        self.assertIn("template=8; limit=host-1", output.getvalue())
        self.assertNotIn("limit=host-2", output.getvalue())
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_host_and_job_url_configuration_is_rejected(self):
        for overrides in ({"HOSTS": ["*"]}, {"HOSTS": 3}, {"JOB_URL": 7}):
            with self.subTest(overrides=overrides), config.launcher_configuration("execute", overrides):
                with self.assertRaises(config.AWXError):
                    config.read_execution_settings()

    def test_survey_defaults_launcher_merge_and_cli_interactive_override(self):
        self.values.update(SURVEY_ANSWERS={'operation': 'install', 'count': 2}, SURVEY_INTERACTIVE=True)
        self.write_values()
        with config.launcher_configuration('execute', {'SURVEY_ANSWERS': {'operation': 'reinstall'}}):
            settings = config.read_execution_settings()
            self.assertEqual(settings['SURVEY_ANSWERS'], {'operation': 'reinstall', 'count': 2})
            self.assertTrue(settings['SURVEY_INTERACTIVE'])
            self.assertFalse(awx.parse_execute_arguments(['--no-survey']).survey)

    def test_invalid_survey_settings_are_rejected(self):
        for values in ({'SURVEY_ANSWERS': []}, {'SURVEY_ANSWERS': {'': 'value'}}, {'SURVEY_INTERACTIVE': 'yes'},
                       {'SURVEY_EXCEL_DEFAULTS': []}, {'SURVEY_EXCEL_DEFAULTS': {'': 'value'}}):
            with self.subTest(values=values), config.launcher_configuration('execute', values):
                with self.assertRaises(config.AWXError):
                    config.read_execution_settings()


if __name__ == "__main__":
    unittest.main()
