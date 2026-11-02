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
from urllib.parse import parse_qs, urlsplit
from lib import awx as runner

class FakeAWX(BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def respond(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.server.calls.append(('GET', self.path, None))
        parsed = urlsplit(self.path)
        if parsed.path == '/api/v2/credentials/':
            name = parse_qs(parsed.query).get('name', [''])[0]
            matches = [credential for credential in self.server.credentials.values() if credential['name'] == name]
            self.respond(200, {'results': matches, 'next': None, 'count': len(matches)})
        elif parsed.path.startswith('/api/v2/credentials/'):
            credential = self.server.credentials.get(int(parsed.path.rstrip('/').split('/')[-1]))
            self.respond(200 if credential else 404, credential or {'detail': 'Not found'})
        elif self.path.endswith('/launch/'):
            self.respond(200, self.server.launch_details)
        elif '/jobs/' in self.path:
            self.respond(200, {'status': self.server.job_status})
        else:
            self.respond(404, {'detail': 'not found'})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append(('POST', self.path, payload))
        self.server.job_counter += 1
        self.respond(201, {'job': self.server.job_counter, 'ignored_fields': self.server.ignored_fields})

class RunnerTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), FakeAWX)
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
        self.path = Path(self.directory.name) / 'hosts.xlsx'
        self.settings = deepcopy(runner.EXECUTION_DEFAULTS)
        self.connection = deepcopy(runner.INVENTORY_DEFAULTS)
        self.connection.update(AWX_URL=self.base, AWX_TOKEN='test-token', TIMEOUT=2)
        self.client = runner.AWXClient(self.base, 'test-token')
        self.server.calls = []
        self.server.job_counter = 100
        self.server.job_status = 'successful'
        self.server.ignored_fields = {}
        self.server.launch_details = {'ask_limit_on_launch': True, 'ask_variables_on_launch': True, 'ask_credential_on_launch': True, 'defaults': {'job_slice_count': 1}}
        self.server.credentials = {31: {'id': 31, 'name': 'machine-cred', 'credential_type': 1, 'kind': 'ssh', 'inputs': {}}, 32: {'id': 32, 'name': 'vault-cred', 'credential_type': 2, 'kind': 'vault', 'inputs': {'vault_id': 'one'}}, 33: {'id': 33, 'name': 'other-machine', 'credential_type': 1, 'kind': 'ssh', 'inputs': {}}}
        self.output = io.StringIO()
        self.addCleanup(runner.apply_settings, runner.INVENTORY_DEFAULTS)

    def link(self, template_id=7):
        return f"{self.base}/#/templates/job_template/{template_id}/details"

    def workbook(self, rows):
        workbook = runner.Workbook()
        sheet = workbook.active
        sheet.title = 'Results'
        sheet.append(['hostname', 'Job_Link', 'Job_Variables', 'exceiute_job', 'Full_Credential'])
        for row in rows:
            sheet.append([*row, 'machine-cred'] if len(row) == 4 else row)
        return workbook

    def cell(self, sheet, row, name):
        return sheet.cell(row, runner.excel.read_columns(sheet)[name.casefold()])

    def save_rows(self, rows):
        workbook = self.workbook(rows)
        workbook.save(self.path)
        workbook.close()

    def run_main(self, *args):
        with patch.object(runner, 'read_inventory_settings', return_value=self.connection), patch.object(runner, 'read_execution_settings', return_value=self.settings), contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            return runner.execute_main(['-file', str(self.path), *args])

    def test_batching_groups_variables_and_deduplicates(self):
        rows = [[f"linux-{i}", self.link(), '{"action":"check","reboot":false}', None] for i in range(23)]
        rows.extend([['linux-0', self.link(), '{"reboot":false,"action":"check"}', None], ['win-1', self.link(8), '{}', None], ['linux-other', self.link(), '{"action":"update"}', None]])
        workbook = self.workbook(rows)
        self.addCleanup(workbook.close)
        batches, existing, _, _ = runner.build_execution_plan(workbook.active, self.client, self.settings)
        self.assertEqual([len(batch['entries']) for batch in batches], [10, 10, 3, 1, 1])
        self.assertEqual(batches[0]['entries'][0]['rows'], [2, 25])
        self.assertEqual(len(existing), 0)

    def test_local_api_launches_batches_and_persists_clickable_links(self):
        rows = [[f"host-{i}", self.link(), '{"action":"check"}', None] for i in range(23)]
        rows.append(['host-0', self.link(), '{"action":"check"}', None])
        self.save_rows(rows)
        self.assertEqual(self.run_main(), 0)
        launches = [call for call in self.server.calls if call[0] == 'POST']
        self.assertEqual([len(call[2]['limit'].split(',')) for call in launches], [10, 10, 3])
        self.assertTrue(all((call[2]['extra_vars'] == {'action': 'check'} for call in launches)))
        self.assertTrue(all((call[2]['credentials'] == [31] for call in launches)))
        self.assertEqual([call[0] for call in self.server.calls if '/credentials/' not in call[1]], ['GET', 'POST', 'GET', 'POST', 'GET', 'POST', 'GET'])
        self.assertEqual(len([call for call in self.server.calls if '/credentials/' in call[1]]), 1)
        workbook = runner.load_workbook(self.path)
        self.addCleanup(workbook.close)
        sheet = workbook.active
        self.assertEqual(self.cell(sheet, 2, 'exceiute_job').hyperlink.target, f"{self.base}/#/jobs/playbook/101/output")
        self.assertEqual(self.cell(sheet, 12, 'exceiute_job').hyperlink.target, f"{self.base}/#/jobs/playbook/102/output")
        self.assertEqual(self.cell(sheet, 22, 'exceiute_job').hyperlink.target, f"{self.base}/#/jobs/playbook/103/output")
        self.assertEqual(self.cell(sheet, 25, 'exceiute_job').value, self.cell(sheet, 2, 'exceiute_job').value)
        self.assertEqual(self.cell(sheet, 1, 'Job_Type').value, 'Job_Type')
        self.assertEqual(self.cell(sheet, 2, 'Job_Type').value, 'check')
        self.server.calls.clear()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.server.calls, [])

    def test_dry_run_does_not_call_api_or_change_file(self):
        self.save_rows([['host-1', self.link(), '{}', None]])
        before = self.path.read_bytes()
        self.assertEqual(self.run_main('--dry-run', '--batch-size', '1'), 0)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_rejects_unsafe_or_invalid_rows_before_any_launch(self):
        for hostname, link, variables in (('all:*', self.link(), '{}'), ('host-1', 'https://other.example/#/templates/job_template/7/details', '{}'), ('host-1', self.link(), '{invalid}'), ('host-1', self.link(), '{"number":NaN}'), ('host-1', self.link(), '[]')):
            with self.subTest(hostname=hostname, link=link, variables=variables):
                self.save_rows([[hostname, link, variables, None]])
                with self.assertRaises(runner.AWXError):
                    self.run_main()
                self.assertEqual(self.server.calls, [])

    def test_conflicting_duplicate_hosts_are_rejected(self):
        self.save_rows([['host-1', self.link(), '{}', None], ['host-1', self.link(8), '{}', None]])
        with self.assertRaisesRegex(runner.AWXError, 'different job or variables'):
            self.run_main()
        self.assertEqual(self.server.calls, [])

    def test_preflight_rejects_ignored_limits_and_variables(self):
        self.save_rows([['host-1', self.link(), '{"action":"check"}', None]])
        for details, message in (({'ask_limit_on_launch': False}, 'Limit'), ({'ask_limit_on_launch': True, 'ask_variables_on_launch': False}, 'Variables'), ({'ask_limit_on_launch': True, 'passwords_needed_to_start': ['ssh_password']}, 'password'), ({'ask_limit_on_launch': True, 'defaults': {'job_slice_count': 2}}, 'Slicing')):
            with self.subTest(details=details):
                self.server.calls = []
                self.server.launch_details = {'ask_credential_on_launch': True, **details}
                if message == 'password':
                    self.save_rows([['host-1', self.link(), '{"action":"check"}', None, None]])
                else:
                    self.save_rows([['host-1', self.link(), '{"action":"check"}', None]])
                with self.assertRaisesRegex(runner.AWXError, message):
                    self.run_main()
                self.assertFalse(any((call[0] == 'POST' for call in self.server.calls)))

    def test_uncertain_launch_keeps_marker_and_blocks_restart(self):
        self.save_rows([['host-1', self.link(), '{}', None]])
        with patch.object(runner.AWXClient, 'post', side_effect=runner.AWXError('uncertain result')):
            with self.assertRaisesRegex(runner.AWXError, 'uncertain'):
                self.run_main()
        workbook = runner.load_workbook(self.path)
        self.assertTrue(self.cell(workbook.active, 2, 'exceiute_job').value.startswith('LAUNCH_PENDING'))
        workbook.close()
        self.server.calls.clear()
        with self.assertRaisesRegex(runner.AWXError, 'LAUNCH_PENDING'):
            self.run_main()
        self.assertEqual(self.server.calls, [])

    def test_failed_job_stops_after_persisting_first_batch(self):
        self.save_rows([[f"host-{i}", self.link(), '{}', None] for i in range(11)])
        self.server.job_status = 'failed'
        with self.assertRaisesRegex(runner.AWXError, 'failed'):
            self.run_main()
        self.assertEqual(len([call for call in self.server.calls if call[0] == 'POST']), 1)
        workbook = runner.load_workbook(self.path)
        self.assertIn('/jobs/playbook/101/', self.cell(workbook.active, 2, 'exceiute_job').value)
        self.assertIsNone(self.cell(workbook.active, 12, 'exceiute_job').value)
        workbook.close()

    def test_ignored_fields_stop_after_persisting_job_link(self):
        self.save_rows([[f"host-{i}", self.link(), '{}', None] for i in range(11)])
        self.server.ignored_fields = {'limit': 'host-0'}
        with self.assertRaisesRegex(runner.AWXError, 'ignored launch fields'):
            self.run_main()
        self.assertEqual(len([call for call in self.server.calls if call[0] == 'POST']), 1)
        workbook = runner.load_workbook(self.path)
        self.assertIsNotNone(self.cell(workbook.active, 2, 'exceiute_job').hyperlink)
        workbook.close()

    def test_missing_job_rows_skipped_and_recorded_duplicates_not_relaunched(self):
        self.settings['WAIT_FOR_COMPLETION'] = False
        recorded = f"{self.base}/#/jobs/playbook/99/output"
        self.save_rows([['host-1', self.link(), '{}', recorded], ['host-1', self.link(), '{}', None], ['host-2', self.link(), '{}', None], ['host-3', 'none', '{}', None]])
        self.assertEqual(self.run_main(), 0)
        launches = [call for call in self.server.calls if call[0] == 'POST']
        self.assertEqual([call[2]['limit'] for call in launches], ['host-2'])
        workbook = runner.load_workbook(self.path)
        self.assertEqual(self.cell(workbook.active, 3, 'exceiute_job').value, recorded)
        workbook.close()

    def test_template_url_formats(self):
        for link in (self.link(), f"{self.base}/templates/job-template/7/details", f"{self.base}/api/v2/job_templates/7/"):
            self.assertEqual(runner.template_id_from_link(link, self.client), 7)
            self.assertIn('101', runner.job_url(link, self.client, 101))

    def test_credential_names_and_ids_resolve_and_are_sent_to_awx(self):
        self.save_rows([['host-1', self.link(), '{}', None, 'machine-cred, 32, 31']])
        self.assertEqual(self.run_main('--job-type', 'install'), 0)
        launch = next((call for call in self.server.calls if call[0] == 'POST'))
        self.assertEqual(launch[2]['credentials'], [31, 32])
        self.assertEqual(launch[2]['extra_vars'], {'action': 'install'})

    def test_batch_groups_include_credentials_and_optional_job_type_column(self):
        workbook = self.workbook([['host-1', self.link(), '{}', None, 'machine-cred', 'install'], ['host-2', self.link(), '{}', None, 'other-machine', 'install'], ['host-3', self.link(), '{}', None, 'machine-cred', 'update'], ['host-4', self.link(), '{}', None, 'machine-cred', 'install']])
        self.addCleanup(workbook.close)
        workbook.active['F1'] = 'Job_Type'
        batches, _, _, _ = runner.build_execution_plan(workbook.active, self.client, self.settings)
        self.assertEqual([batch['limit'] for batch in batches], ['host-1,host-4', 'host-2', 'host-3'])
        self.assertEqual([batch['job_type'] for batch in batches], ['install', 'install', 'update'])

    def test_job_type_precedence_and_custom_variable_name(self):
        self.settings.update(JOB_TYPE='install', JOB_TYPE_VARIABLE='operation')
        for override, cell, variables, expected in (('reinstall', 'update', {'operation': 'check'}, 'reinstall'), (None, 'update', {'operation': 'check'}, 'update'), (None, None, {'operation': 'check'}, 'install')):
            with self.subTest(override=override, cell=cell):
                self.assertEqual(runner.apply_job_type(variables, cell, self.settings, override), expected)
                self.assertEqual(variables['operation'], expected)
        self.settings['JOB_TYPE'] = ''
        variables = {'operation': 'uninstall'}
        self.assertEqual(runner.apply_job_type(variables, None, self.settings), 'uninstall')

    def test_dry_run_shows_action_and_credentials_without_resolving_them(self):
        self.save_rows([['host-1', self.link(), '{}', None, 'machine-cred,32']])
        self.assertEqual(self.run_main('--dry-run', '--job-type', 'reinstall'), 0)
        self.assertIn('reinstall', self.output.getvalue())
        self.assertIn('machine-cred', self.output.getvalue())
        self.assertEqual(self.server.calls, [])

    def test_missing_or_ambiguous_credentials_stop_before_launch(self):
        self.save_rows([['host-1', self.link(), '{}', None, 'missing-cred']])
        with self.assertRaisesRegex(runner.AWXError, 'not found'):
            self.run_main()
        self.server.credentials[34] = {**self.server.credentials[31], 'id': 34}
        self.save_rows([['host-1', self.link(), '{}', None, 'machine-cred']])
        with self.assertRaisesRegex(runner.AWXError, 'ambiguous'):
            self.run_main()
        self.assertFalse(any((call[0] == 'POST' for call in self.server.calls)))

    def test_conflicting_credential_types_stop_before_launch(self):
        self.save_rows([['host-1', self.link(), '{}', None, 'machine-cred,other-machine']])
        with self.assertRaisesRegex(runner.AWXError, 'conflicting credentials'):
            self.run_main()
        self.assertFalse(any((call[0] == 'POST' for call in self.server.calls)))

    def test_credential_prompt_must_be_enabled(self):
        self.server.launch_details['ask_credential_on_launch'] = False
        self.save_rows([['host-1', self.link(), '{}', None]])
        with self.assertRaisesRegex(runner.AWXError, 'Prompt on launch for Credentials'):
            self.run_main()
        self.assertFalse(any((call[0] == 'POST' for call in self.server.calls)))

    def test_credentials_with_interactive_passwords_rejected(self):
        self.server.credentials[31]['inputs'] = {'password': 'ASK'}
        self.save_rows([['host-1', self.link(), '{}', None]])
        with self.assertRaisesRegex(runner.AWXError, 'interactive password'):
            self.run_main()
        self.assertFalse(any((call[0] == 'POST' for call in self.server.calls)))

    def test_blank_credentials_preserve_template_credentials(self):
        self.save_rows([['host-1', self.link(), '{}', None, 'none']])
        self.assertEqual(self.run_main(), 0)
        launch = next((call for call in self.server.calls if call[0] == 'POST'))
        self.assertNotIn('credentials', launch[2])

    def test_duplicate_host_with_different_credentials_is_rejected(self):
        self.save_rows([['host-1', self.link(), '{}', None, '31'], ['host-1', self.link(), '{}', None, '33']])
        with self.assertRaisesRegex(runner.AWXError, 'different job or variables or credentials'):
            self.run_main()
        self.assertEqual(self.server.calls, [])

    def test_invalid_settings(self):
        for contents in ('BATCH_SIZE = 0', 'BATCH_SIZE = True', "WAIT_FOR_COMPLETION = 'yes'", "EXECUTION_COLUMN = 'hostname'", "EXECUTION_COLUMN = 'Full_Credential'", 'JOB_TYPE = 123', "JOB_TYPE_VARIABLE = 'invalid-name'"):
            with self.subTest(contents=contents):
                settings_file = Path(self.directory.name) / 'variables'
                settings_file.write_text(contents)
                with self.assertRaises(runner.AWXError):
                    runner.read_execution_settings(settings_file)
if __name__ == '__main__':
    unittest.main()
