"""Library layout, launcher compatibility and shared request behavior."""

import ast
import contextlib
import io
import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from lib import awx
from lib import awx_config
from lib import awx_dependencies


ROOT = Path(__file__).resolve().parent.parent
LAUNCHERS = {
    "awx_inventory_hosts.py": "inventory",
    "awx_refresh_excel.py": "refresh",
    "awx_execute_jobs.py": "execute",
    "awx_job_output.py": "output",
}


class LibraryTests(unittest.TestCase):
    def test_all_root_launchers_delegate_to_the_same_library(self):
        for filename, operation in LAUNCHERS.items():
            with self.subTest(filename=filename), patch.object(awx, "run", return_value=0) as run, \
                 patch.object(awx_dependencies, "ensure_virtual_environment") as ensure_venv:
                with self.assertRaises(SystemExit) as result:
                    runpy.run_path(str(ROOT / filename), run_name="__main__")
                self.assertEqual(result.exception.code, 0)
                run.assert_called_once_with(operation, variables={})
                ensure_venv.assert_called_once_with()
                tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
                self.assertFalse(any(isinstance(node, (ast.FunctionDef, ast.ClassDef)) for node in tree.body))

    def test_launchers_work_from_another_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            for filename in LAUNCHERS:
                with self.subTest(filename=filename):
                    result = subprocess.run([sys.executable, "-B", str(ROOT / filename), "--help"], cwd=directory,
                                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("--file", result.stdout)
                    self.assertIn("Przykłady:", result.stdout)
                    self.assertIn("awx_variable", result.stdout)

    def test_module_dispatcher_forwards_operation_help(self):
        result = subprocess.run([sys.executable, "-B", "-m", "lib.awx", "refresh", "--help"], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--host-column", result.stdout)

    def test_all_settings_point_to_the_single_root_variables_file(self):
        self.assertEqual(awx_config.PROJECT_ROOT, ROOT)
        self.assertEqual(awx.VARIABLES_FILE, ROOT / "awx_variable")
        self.assertEqual(awx.EXECUTION_SETTINGS_FILE, ROOT / "awx_variable")
        self.assertEqual(awx.OUTPUT_SETTINGS_FILE, ROOT / "awx_variable")

    def test_launch_transport_failure_is_not_retried_and_token_is_redacted(self):
        client = awx.AWXClient("http://awx.test", "test-token")
        with patch.object(client.opener, "open", side_effect=URLError("connection lost: test-token")) as open_request:
            with self.assertRaises(awx.AWXError) as result:
                client.post("job_templates/7/launch/", {"limit": "host-1"})
            self.assertIn("uncertain", str(result.exception))
            self.assertNotIn("test-token", str(result.exception))
            open_request.assert_called_once()
            self.assertEqual(open_request.call_args.args[0].get_method(), "POST")

    def test_launch_http_error_is_not_retried_and_response_is_redacted(self):
        client = awx.AWXClient("http://awx.test", "test-token")
        error = HTTPError("http://awx.test/api/v2/job_templates/7/launch/", 400, "Bad Request", {},
                          io.BytesIO(b'{"detail":"test-token"}'))
        with patch.object(client.opener, "open", side_effect=error) as open_request:
            with self.assertRaises(awx.AWXError) as result:
                client.post("job_templates/7/launch/", {})
            self.assertIn("HTTP 400", str(result.exception))
            self.assertNotIn("test-token", str(result.exception))
            open_request.assert_called_once()

    def test_shared_client_blocks_requests_to_other_servers(self):
        client = awx.AWXClient("http://awx.test", "test-token")
        with patch.object(client.opener, "open") as open_request:
            with self.assertRaises(awx.AWXError) as result:
                client.get("http://other.example/api/v2/jobs/1/")
            message = str(result.exception)
            self.assertIn('http://other.example/api/v2/jobs/1/', message)
            self.assertIn("AWX_URL: 'http://awx.test'", message)
            self.assertIn("Expected server: scheme='http', host='awx.test', port=80", message)
            self.assertIn("Actual server: scheme='http', host='other.example', port=80", message)
            self.assertIn('Different: host.', message)
            open_request.assert_not_called()

    def test_template_mismatch_reports_protocol_host_and_port_without_exposing_token(self):
        client = awx.AWXClient('https://awx.test/api/controller/v2/', 'test-token')
        for url, difference in (
            ('http://awx.test/#/templates/job_template/7/details', 'scheme, port'),
            ('https://other.test/#/templates/job_template/7/details', 'host'),
            ('https://awx.test:8443/#/templates/job_template/7/details', 'port'),
        ):
            with self.subTest(url=url), self.assertRaises(awx.AWXError) as result:
                awx.template_id_from_link(url, client)
            message = str(result.exception)
            self.assertIn(f'Job_Link URL: {url!r}', message)
            self.assertIn("AWX_URL: 'https://awx.test/api/controller/v2/'", message)
            self.assertIn(f'Different: {difference}.', message)
        with self.assertRaises(awx.AWXError) as result:
            awx.template_id_from_link('https://other.test/?token=test-token#/templates/job_template/7/details', client)
        self.assertNotIn('test-token', str(result.exception))
        self.assertIn('***', str(result.exception))
        self.assertEqual(awx.template_id_from_link('https://AWX.TEST:443/#/templates/job_template/7/details', client), 7)

    def test_json_escaped_survey_password_is_redacted_from_launch_errors(self):
        secret = 'private-\u00e4-value"\\end'
        client = awx.AWXClient('http://awx.test', 'test-token')
        client.add_sensitive_values([secret])
        body = json.dumps({'detail': secret}).encode()
        error = HTTPError('http://awx.test/api/v2/job_templates/7/launch/', 400, 'Bad Request', {}, io.BytesIO(body))
        with patch.object(client.opener, 'open', side_effect=error), self.assertRaises(awx.AWXError) as result:
            client.post('job_templates/7/launch/', {'extra_vars': {'private': secret}})
        self.assertNotIn(secret, str(result.exception))
        self.assertNotIn(json.dumps(secret)[1:-1], str(result.exception))
        self.assertIn('***', str(result.exception))

    def test_one_error_handler_returns_consistent_exit_codes(self):
        for error, code in ((awx.AWXError("test error"), 1), (KeyboardInterrupt(), 130)):
            with self.subTest(code=code), patch.dict(awx.OPERATIONS, {"refresh": lambda argv: (_ for _ in ()).throw(error)}), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(awx.run("refresh", []), code)

    def test_library_and_launchers_keep_python_39_syntax(self):
        for path in [*(ROOT / "lib").glob("*.py"), *(ROOT / name for name in LAUNCHERS)]:
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
