"""Runner tests use temporary workbooks and a local fake AWX, never a live AWX."""

import contextlib
import io
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import awx_execute_jobs as runner


class FakeAWX(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.server.calls.append(("GET", self.path, None))
        if self.path.endswith("/launch/"):
            self.respond(200, self.server.launch_details)
        elif "/jobs/" in self.path:
            self.respond(200, {"status": self.server.job_status})
        else:
            self.respond(404, {"detail": "not found"})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append(("POST", self.path, payload))
        self.server.job_counter += 1
        self.respond(201, {"job": self.server.job_counter, "ignored_fields": self.server.ignored_fields})


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeAWX)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "hosts.xlsx"
        self.settings = deepcopy(runner.DEFAULT_SETTINGS)
        self.connection = deepcopy(runner.inventory.DEFAULT_SETTINGS)
        self.connection.update(AWX_URL=self.base, AWX_TOKEN="test-token", TIMEOUT=2)
        self.client = runner.LaunchClient(self.base, "test-token")
        self.server.calls = []
        self.server.job_counter = 100
        self.server.job_status = "successful"
        self.server.ignored_fields = {}
        self.server.launch_details = {
            "ask_limit_on_launch": True, "ask_variables_on_launch": True,
            "defaults": {"job_slice_count": 1},
        }
        self.output = io.StringIO()
        self.addCleanup(runner.inventory.apply_settings, runner.inventory.DEFAULT_SETTINGS)

    def link(self, template_id=7):
        return f"{self.base}/#/templates/job_template/{template_id}/details"

    def workbook(self, rows):
        workbook = runner.inventory.Workbook()
        sheet = workbook.active
        sheet.title = "Results"
        sheet.append(["hostname", "Job_Link", "Job_Variables", "exceiute_job"])
        for row in rows:
            sheet.append(row)
        return workbook

    def save_rows(self, rows):
        workbook = self.workbook(rows)
        workbook.save(self.path)
        workbook.close()

    def run_main(self, *args):
        with patch.object(runner.inventory, "read_settings", return_value=self.connection), \
             patch.object(runner, "read_settings", return_value=self.settings), \
             contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            return runner.main(["-file", str(self.path), *args])

    def test_batching_groups_variables_and_deduplicates(self):
        rows = [[f"linux-{i}", self.link(), '{"action":"check","reboot":false}', None] for i in range(23)]
        rows.extend([
            ["linux-0", self.link(), '{"reboot":false,"action":"check"}', None],
            ["win-1", self.link(8), '{}', None],
            ["linux-other", self.link(), '{"action":"update"}', None],
        ])
        workbook = self.workbook(rows)
        self.addCleanup(workbook.close)
        batches, existing, _, _ = runner.build_plan(workbook.active, self.client, self.settings)
        self.assertEqual([len(batch["entries"]) for batch in batches], [10, 10, 3, 1, 1])
        self.assertEqual(batches[0]["entries"][0]["rows"], [2, 25])
        self.assertEqual(len(existing), 0)

    def test_local_api_launches_batches_and_persists_clickable_links(self):
        rows = [[f"host-{i}", self.link(), '{"action":"check"}', None] for i in range(23)]
        rows.append(["host-0", self.link(), '{"action":"check"}', None])
        self.save_rows(rows)
        self.assertEqual(self.run_main(), 0)
        launches = [call for call in self.server.calls if call[0] == "POST"]
        self.assertEqual([len(call[2]["limit"].split(",")) for call in launches], [10, 10, 3])
        self.assertTrue(all(call[2]["extra_vars"] == {"action": "check"} for call in launches))
        self.assertEqual([call[0] for call in self.server.calls], ["GET", "POST", "GET", "POST", "GET", "POST", "GET"])
        workbook = runner.inventory.load_workbook(self.path)
        self.addCleanup(workbook.close)
        sheet = workbook.active
        self.assertEqual(sheet["D2"].hyperlink.target, f"{self.base}/#/jobs/playbook/101/output")
        self.assertEqual(sheet["D12"].hyperlink.target, f"{self.base}/#/jobs/playbook/102/output")
        self.assertEqual(sheet["D22"].hyperlink.target, f"{self.base}/#/jobs/playbook/103/output")
        self.assertEqual(sheet["D25"].value, sheet["D2"].value)
        self.server.calls.clear()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.server.calls, [])

    def test_dry_run_does_not_call_api_or_change_file(self):
        self.save_rows([["host-1", self.link(), '{}', None]])
        before = self.path.read_bytes()
        self.assertEqual(self.run_main("--dry-run", "--batch-size", "1"), 0)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_rejects_unsafe_or_invalid_rows_before_any_launch(self):
        for hostname, link, variables in (
            ("all:*", self.link(), '{}'),
            ("host-1", 'https://other.example/#/templates/job_template/7/details', '{}'),
            ("host-1", self.link(), '{invalid}'),
            ("host-1", self.link(), '{"number":NaN}'),
            ("host-1", self.link(), '[]'),
        ):
            with self.subTest(hostname=hostname, link=link, variables=variables):
                self.save_rows([[hostname, link, variables, None]])
                with self.assertRaises(runner.AWXError):
                    self.run_main()
                self.assertEqual(self.server.calls, [])

    def test_conflicting_duplicate_hosts_are_rejected(self):
        self.save_rows([["host-1", self.link(), '{}', None], ["host-1", self.link(8), '{}', None]])
        with self.assertRaisesRegex(runner.AWXError, "different job or variables"):
            self.run_main()
        self.assertEqual(self.server.calls, [])

    def test_preflight_rejects_ignored_limits_and_variables(self):
        self.save_rows([["host-1", self.link(), '{"action":"check"}', None]])
        for details, message in (
            ({"ask_limit_on_launch": False}, "Limit"),
            ({"ask_limit_on_launch": True, "ask_variables_on_launch": False}, "Variables"),
            ({"ask_limit_on_launch": True, "passwords_needed_to_start": ["ssh_password"]}, "password"),
            ({"ask_limit_on_launch": True, "defaults": {"job_slice_count": 2}}, "Slicing"),
        ):
            with self.subTest(details=details):
                self.server.calls = []
                self.server.launch_details = details
                with self.assertRaisesRegex(runner.AWXError, message):
                    self.run_main()
                self.assertFalse(any(call[0] == "POST" for call in self.server.calls))

    def test_uncertain_launch_keeps_marker_and_blocks_restart(self):
        self.save_rows([["host-1", self.link(), '{}', None]])
        with patch.object(runner.LaunchClient, "post", side_effect=runner.AWXError("uncertain result")):
            with self.assertRaisesRegex(runner.AWXError, "uncertain"):
                self.run_main()
        workbook = runner.inventory.load_workbook(self.path)
        self.assertTrue(workbook.active["D2"].value.startswith("LAUNCH_PENDING"))
        workbook.close()
        self.server.calls.clear()
        with self.assertRaisesRegex(runner.AWXError, "LAUNCH_PENDING"):
            self.run_main()
        self.assertEqual(self.server.calls, [])

    def test_failed_job_stops_after_persisting_first_batch(self):
        self.save_rows([[f"host-{i}", self.link(), '{}', None] for i in range(11)])
        self.server.job_status = "failed"
        with self.assertRaisesRegex(runner.AWXError, "failed"):
            self.run_main()
        self.assertEqual(len([call for call in self.server.calls if call[0] == "POST"]), 1)
        workbook = runner.inventory.load_workbook(self.path)
        self.assertIn("/jobs/playbook/101/", workbook.active["D2"].value)
        self.assertIsNone(workbook.active["D12"].value)
        workbook.close()

    def test_ignored_fields_stop_after_persisting_job_link(self):
        self.save_rows([[f"host-{i}", self.link(), '{}', None] for i in range(11)])
        self.server.ignored_fields = {"limit": "host-0"}
        with self.assertRaisesRegex(runner.AWXError, "ignored launch fields"):
            self.run_main()
        self.assertEqual(len([call for call in self.server.calls if call[0] == "POST"]), 1)
        workbook = runner.inventory.load_workbook(self.path)
        self.assertIsNotNone(workbook.active["D2"].hyperlink)
        workbook.close()

    def test_missing_job_rows_skipped_and_recorded_duplicates_not_relaunched(self):
        self.settings["WAIT_FOR_COMPLETION"] = False
        recorded = f"{self.base}/#/jobs/playbook/99/output"
        self.save_rows([
            ["host-1", self.link(), '{}', recorded],
            ["host-1", self.link(), '{}', None],
            ["host-2", self.link(), '{}', None],
            ["host-3", 'none', '{}', None],
        ])
        self.assertEqual(self.run_main(), 0)
        launches = [call for call in self.server.calls if call[0] == "POST"]
        self.assertEqual([call[2]["limit"] for call in launches], ["host-2"])
        workbook = runner.inventory.load_workbook(self.path)
        self.assertEqual(workbook.active["D3"].value, recorded)
        workbook.close()

    def test_template_url_formats(self):
        for link in (self.link(), f"{self.base}/templates/job-template/7/details", f"{self.base}/api/v2/job_templates/7/"):
            self.assertEqual(runner.template_id_from_link(link, self.client), 7)
            self.assertIn("101", runner.job_url(link, self.client, 101))

    def test_invalid_settings(self):
        for contents in ("BATCH_SIZE = 0", "BATCH_SIZE = True", "WAIT_FOR_COMPLETION = 'yes'", "EXECUTION_COLUMN = 'hostname'"):
            with self.subTest(contents=contents):
                settings_file = Path(self.directory.name) / "variables"
                settings_file.write_text(contents)
                with self.assertRaises(runner.AWXError):
                    runner.read_settings(settings_file)


if __name__ == "__main__":
    unittest.main()
