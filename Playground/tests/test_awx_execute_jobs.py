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
        elif self.path.endswith('/survey_spec/'):
            self.respond(200, self.server.survey_spec)
        elif '/jobs/' in self.path:
            job_id = parsed.path.rstrip('/').split('/')[-1]
            self.respond(200, {'status': self.server.job_status_by_id.get(job_id, self.server.job_status),
                               'artifacts': self.server.job_artifacts})
        else:
            self.respond(404, {'detail': 'not found'})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append(('POST', self.path, payload))
        if self.server.launch_error is not None:
            self.respond(400, self.server.launch_error)
            return
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
        self.settings.update(HOSTS=[], JOB_URL='', SURVEY_ANSWERS={}, SURVEY_EXCEL_DEFAULTS={},
                             SURVEY_INTERACTIVE=False, JOB_TYPE='', JOB_TYPE_VARIABLE='action',
                             BATCH_SIZE=10, WAIT_FOR_COMPLETION=True, EXECUTION_COLUMN='exceiute_job')
        self.connection = deepcopy(runner.INVENTORY_DEFAULTS)
        self.connection.update(AWX_URL=self.base, AWX_TOKEN='test-token', TIMEOUT=2)
        self.client = runner.AWXClient(self.base, 'test-token')
        self.server.calls = []
        self.server.job_counter = 100
        self.server.job_status = 'successful'
        self.server.job_status_by_id = {}
        self.server.job_artifacts = {}
        self.server.ignored_fields = {}
        self.server.launch_error = None
        self.server.survey_spec = {'name': 'Test survey', 'description': '', 'spec': []}
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

    def test_foreign_template_reports_sheet_row_host_and_effective_link_source(self):
        foreign = 'https://other.example/#/templates/job_template/7/details'
        for hyperlink in (False, True):
            workbook = self.workbook([['host-1', self.link() if hyperlink else foreign, '{}', None]])
            self.addCleanup(workbook.close)
            sheet = workbook.active
            if hyperlink:
                sheet['B2'].hyperlink = foreign
            for action in (
                lambda: runner.build_execution_plan(sheet, self.client, self.settings),
                lambda: runner.update_survey_columns(self.client, sheet, self.settings),
            ):
                with self.subTest(hyperlink=hyperlink), self.assertRaises(runner.AWXError) as result:
                    action()
                message = str(result.exception)
                self.assertIn("Worksheet 'Results', row 2, host 'host-1'", message)
                self.assertIn(f'Job_Link URL: {foreign!r}', message)
                self.assertIn(f'AWX_URL: {self.base!r}', message)
                self.assertIn('hyperlink target (B2)' if hyperlink else 'cell value (B2)', message)
                self.assertIn(f'Job_Link cell text (B2): {sheet["B2"].value!r}', message)
        self.assertEqual(self.server.calls, [])

    def test_foreign_job_url_override_reports_configuration_source_before_requests(self):
        self.settings['JOB_URL'] = 'https://other.example/#/templates/job_template/14/details'
        self.save_rows([['host-1', self.link(), '{}', None]])
        with self.assertRaises(runner.AWXError) as result:
            self.run_main()
        message = str(result.exception)
        self.assertIn('JOB_URL / --job-url:', message)
        self.assertIn(self.settings['JOB_URL'], message)
        self.assertIn(f'AWX_URL: {self.base!r}', message)
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
        self.server.job_artifacts = {'host-0': {'message': 'Installation failed'}}
        with self.assertRaisesRegex(runner.AWXError, 'failed'):
            self.run_main()
        self.assertEqual(len([call for call in self.server.calls if call[0] == 'POST']), 1)
        workbook = runner.load_workbook(self.path)
        self.assertIn('/jobs/playbook/101/', self.cell(workbook.active, 2, 'exceiute_job').value)
        self.assertIsNone(self.cell(workbook.active, 12, 'exceiute_job').value)
        self.assertEqual(self.cell(workbook.active, 2, 'artifact filter').value, 'Installation failed')
        workbook.close()

    def test_completed_job_saves_artifacts_and_host_messages_without_collector(self):
        self.settings['WAIT_FOR_COMPLETION'] = True
        self.server.job_artifacts = {'results': {'host-1': {'message': 'Installed'},
                                                'host-2': {'message': 'Already installed'}}}
        self.save_rows([['host-1', self.link(), '{}', None], ['host-2', self.link(), '{}', None]])
        self.assertEqual(self.run_main(), 0)
        records = self.records()
        self.assertEqual(records['host-1']['artifact filter'], 'Installed')
        self.assertEqual(records['host-2']['artifact filter'], 'Already installed')
        self.assertEqual(json.loads(records['host-1']['artifacts']), self.server.job_artifacts)

    def test_launch_without_waiting_clears_stale_artifacts_and_keeps_job_link(self):
        self.settings['WAIT_FOR_COMPLETION'] = False
        workbook = self.workbook([['host-1', self.link(), '{}', None]])
        workbook.active['F1'] = 'Artifacts'
        workbook.active['G1'] = 'artifact filter'
        workbook.active['F2'] = '{"previous": true}'
        workbook.active['G2'] = 'old message'
        workbook.save(self.path)
        workbook.close()
        self.assertEqual(self.run_main(), 0)
        record = self.records()['host-1']
        self.assertEqual(record['artifacts'], '{}')
        self.assertIsNone(record['artifact filter'])
        self.assertIn('/jobs/playbook/101/', record['exceiute_job'])
        self.assertFalse(any('/jobs/' in call[1] for call in self.server.calls))

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

    def test_plan_uses_configured_job_url_before_excel_text_and_hyperlink(self):
        self.settings['JOB_URL'] = self.link(14)
        workbook = self.workbook([['host-1', self.link(7), '{}', None]])
        self.addCleanup(workbook.close)
        workbook.active['B2'].hyperlink = self.link(8)
        batches, _, _, _ = runner.build_execution_plan(workbook.active, self.client, self.settings)
        self.assertEqual(batches[0]['template_id'], 14)
        self.assertEqual(batches[0]['link'], self.link(14))
        self.assertTrue(batches[0]['job_url_overridden'])

    def test_credential_names_and_ids_resolve_and_are_sent_to_awx(self):
        self.save_rows([['host-1', self.link(), '{}', None, 'machine-cred, 32, 31']])
        self.assertEqual(self.run_main('--job-type', 'install'), 0)
        launch = next((call for call in self.server.calls if call[0] == 'POST'))
        self.assertEqual(launch[2]['credentials'], [31, 32])
        self.assertEqual(launch[2]['extra_vars'], {'action': 'install'})
        workbook = runner.load_workbook(self.path)
        self.addCleanup(workbook.close)
        self.assertEqual(self.cell(workbook.active, 2, 'Full_Credential').value, 'machine-cred, 32')

    def test_repeated_names_and_ids_are_removed_from_saved_full_credential(self):
        self.save_rows([['host-1', self.link(), '{}', None,
                         ' machine-cred, machine-cred , 031, 31, vault-cred, 32, 032']])
        self.assertEqual(self.run_main(), 0)
        launch = next(call for call in self.server.calls if call[0] == 'POST')
        self.assertEqual(launch[2]['credentials'], [31, 32])
        workbook = runner.load_workbook(self.path)
        self.addCleanup(workbook.close)
        self.assertEqual(self.cell(workbook.active, 2, 'Full_Credential').value, 'machine-cred, vault-cred')

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

    def test_missing_numeric_credential_identifies_hosts_and_configuration_source(self):
        self.save_rows([['host-1', self.link(), '{}', None, '123']])
        before = self.path.read_bytes()
        with self.assertRaises(runner.AWXError) as error:
            self.run_main()
        message = str(error.exception)
        for text in ("credential '123'", 'Full_Credential', 'host-1', 'EXTRA_CREDENTIAL_VARIABLES',
                     'awx_variable', 'refresh Excel', 'job/template credentials', 'AWX_TOKEN',
                     'stored in AWX', 'Job_Variables'):
            self.assertIn(text, message)
        self.assertFalse(any(call[0] == 'POST' for call in self.server.calls))
        self.assertEqual(self.path.read_bytes(), before)

    def test_jumphost_choices_are_added_to_launch_and_deduplicated(self):
        self.server.credentials.update({
            34: {'id': 34, 'name': 'app_3hop', 'credential_type': 31, 'inputs': {}},
            35: {'id': 35, 'name': 'app_3hop_extra', 'credential_type': 32, 'inputs': {}},
        })
        book = self.workbook([['host-1', self.link(), '{}', None, 'machine-cred,34', '34, app_3hop_extra']])
        book.active.cell(1, 6).value = 'jumphost_for_hops'
        book.save(self.path)
        book.close()
        self.assertEqual(self.run_main(), 0)
        launch = next(call for call in self.server.calls if call[0] == 'POST')
        self.assertEqual(launch[2]['credentials'], [31, 34, 35])

    def test_jumphost_additions_preserve_template_credentials_when_full_is_blank(self):
        self.server.credentials[34] = {'id': 34, 'name': 'app_3hop', 'credential_type': 31, 'inputs': {}}
        self.server.credentials[35] = {'id': 35, 'name': 'old_jumphost', 'credential_type': 31, 'inputs': {}}
        self.server.launch_details['defaults']['credentials'] = [self.server.credentials[31], self.server.credentials[35]]
        book = self.workbook([['host-1', self.link(), '{}', None, None, 'app_3hop']])
        book.active.cell(1, 6).value = 'jumphost_for_hops'
        book.save(self.path)
        book.close()
        self.assertEqual(self.run_main(), 0)
        launch = next(call for call in self.server.calls if call[0] == 'POST')
        self.assertEqual(launch[2]['credentials'], [31, 34])

    def test_jumphost_changes_split_batches(self):
        book = self.workbook([
            ['host-1', self.link(), '{}', None, 'machine-cred', 'app_3hop'],
            ['host-2', self.link(), '{}', None, 'machine-cred', 'app_3hop_other'],
        ])
        self.addCleanup(book.close)
        book.active.cell(1, 6).value = 'jumphost_for_hops'
        batches, _, _, _ = runner.build_execution_plan(book.active, self.client, self.settings)
        self.assertEqual(len(batches), 2)
        self.assertIn('app_3hop', batches[0]['credential_refs'])
        self.assertIn('app_3hop_other', batches[1]['credential_refs'])

    def test_conflicting_jumphost_credentials_stop_before_launch(self):
        self.server.credentials.update({
            34: {'id': 34, 'name': 'app_3hop', 'credential_type': 31},
            35: {'id': 35, 'name': 'app_3hop_other', 'credential_type': 31},
        })
        book = self.workbook([['host-1', self.link(), '{}', None, 'machine-cred', 'app_3hop,app_3hop_other']])
        book.active.cell(1, 6).value = 'jumphost_for_hops'
        book.save(self.path)
        book.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(runner.AWXError, 'jumphost_for_hops contains conflicting credentials'):
            self.run_main()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(any(call[0] == 'POST' for call in self.server.calls))

    def test_jumphost_formulas_are_rejected_before_launch(self):
        book = self.workbook([['host-1', self.link(), '{}', None, 'machine-cred', '=34']])
        book.active.cell(1, 6).value = 'jumphost_for_hops'
        book.save(self.path)
        book.close()
        with self.assertRaisesRegex(runner.AWXError, 'jumphost_for_hops must contain literal'):
            self.run_main()
        self.assertFalse(any(call[0] == 'POST' for call in self.server.calls))

    def test_conflicting_credential_types_stop_before_launch(self):
        self.save_rows([['host-1', self.link(), '{}', None, 'machine-cred,other-machine']])
        with self.assertRaisesRegex(runner.AWXError, 'conflicting credentials'):
            self.run_main()
        self.assertFalse(any((call[0] == 'POST' for call in self.server.calls)))

    def test_credential_prompt_must_be_enabled(self):
        self.server.launch_details['ask_credential_on_launch'] = False
        self.save_rows([['host-1', self.link(), '{}', None]])
        before = self.path.read_bytes()
        with self.assertRaises(runner.AWXError) as error:
            self.run_main()
        for text in ('Prompt on launch for Credentials', self.link(), 'ask_credential_on_launch=True',
                     'save the template and rerun', 'Requested credential IDs: 31', 'hosts: host-1'):
            self.assertIn(text, str(error.exception))
        self.assertFalse(any((call[0] == 'POST' for call in self.server.calls)))
        self.assertEqual(self.path.read_bytes(), before)

    def test_credentials_with_interactive_passwords_rejected(self):
        self.server.credentials[31]['inputs'] = {'password': 'ASK'}
        self.save_rows([['host-1', self.link(), '{}', None]])
        with self.assertRaisesRegex(runner.AWXError, 'interactive password'):
            self.run_main()
        self.assertFalse(any((call[0] == 'POST' for call in self.server.calls)))

    def enable_survey(self, questions, *, ask_variables=False):
        self.server.launch_details.update(survey_enabled=True, ask_variables_on_launch=ask_variables)
        self.server.survey_spec['spec'] = questions

    def test_survey_answers_are_sent_as_extra_vars_without_variables_prompt(self):
        self.enable_survey([{'variable': 'action', 'type': 'multiplechoice', 'required': True,
                             'choices': ['install', 'reinstall', 'update'], 'default': 'install'}])
        self.server.launch_details['variables_needed_to_start'] = ['action']
        self.settings['BATCH_SIZE'] = 1
        self.save_rows([['host-1', self.link(), '{}', None], ['host-2', self.link(), '{"action":"update"}', None]])
        self.assertEqual(self.run_main(), 0)
        launches = [call[2] for call in self.server.calls if call[0] == 'POST']
        self.assertEqual([item['extra_vars'] for item in launches], [{'action': 'install'}, {'action': 'update'}])
        self.assertEqual(sum(call[1].endswith('/survey_spec/') for call in self.server.calls), 1)

    def test_required_survey_is_checked_even_with_no_variables(self):
        self.enable_survey([{'variable': 'required_answer', 'type': 'text', 'required': True, 'min': 1}])
        self.save_rows([['host-1', self.link(), '{}', None]])
        before = self.path.read_bytes()
        with self.assertRaisesRegex(runner.AWXError, 'required_answer.*required'):
            self.run_main()
        self.assertFalse(any(call[0] == 'POST' for call in self.server.calls))
        self.assertEqual(self.path.read_bytes(), before)

    def test_every_batch_is_validated_before_first_post_or_workbook_write(self):
        self.enable_survey([{'variable': 'count', 'type': 'integer', 'required': True, 'min': 1, 'max': 3}], ask_variables=True)
        self.save_rows([['host-1', self.link(), '{"count":2}', None], ['host-2', self.link(), '{"count":4}', None]])
        before = self.path.read_bytes()
        with self.assertRaisesRegex(runner.AWXError, 'max limit'):
            self.run_main()
        self.assertFalse(any(call[0] == 'POST' for call in self.server.calls))
        self.assertEqual(self.path.read_bytes(), before)

    def test_survey_unknown_variables_are_rejected_when_not_prompted(self):
        self.enable_survey([{'variable': 'answer', 'type': 'text', 'required': False}])
        self.save_rows([['host-1', self.link(), '{"unknown":"value"}', None]])
        with self.assertRaisesRegex(runner.AWXError, 'outside its survey: unknown'):
            self.run_main()
        self.assertFalse(any(call[0] == 'POST' for call in self.server.calls))

    def test_survey_cli_excel_and_config_precedence_and_batch_grouping(self):
        self.enable_survey([{'variable': 'operation', 'type': 'multiplechoice', 'required': True,
                             'choices': ['install', 'reinstall', 'update']}])
        self.settings['SURVEY_ANSWERS'] = {'operation': 'install'}
        self.save_rows([['host-1', self.link(), '{"operation":"reinstall"}', None],
                        ['host-2', self.link(), '{}', None]])
        self.assertEqual(self.run_main('--survey-answers', '{"operation":"update"}'), 0)
        launches = [call[2] for call in self.server.calls if call[0] == 'POST']
        self.assertEqual(len(launches), 1)
        self.assertEqual(launches[0]['extra_vars'], {'operation': 'update'})
        self.assertEqual(launches[0]['limit'], 'host-1,host-2')

    def test_excel_survey_answers_override_configured_fallback(self):
        self.enable_survey([{'variable': 'operation', 'type': 'text', 'required': True}])
        self.settings['SURVEY_ANSWERS'] = {'operation': 'install'}
        self.save_rows([['host-1', self.link(), '{"operation":"reinstall"}', None],
                        ['host-2', self.link(), '{}', None]])
        self.assertEqual(self.run_main(), 0)
        launches = [call[2] for call in self.server.calls if call[0] == 'POST']
        self.assertEqual([item['extra_vars']['operation'] for item in launches], ['reinstall', 'install'])

    def test_job_type_can_supply_the_answer_named_by_survey(self):
        self.enable_survey([{'variable': 'installtesttypename', 'type': 'multiplechoice', 'required': True,
                             'choices': ['install', 'reinstall']}])
        self.settings['JOB_TYPE_VARIABLE'] = 'installtesttypename'
        self.save_rows([['host-1', self.link(), '{}', None]])
        self.assertEqual(self.run_main('--job-type', 'reinstall'), 0)
        launch = next(call[2] for call in self.server.calls if call[0] == 'POST')
        self.assertEqual(launch['extra_vars'], {'installtesttypename': 'reinstall'})
        self.assertEqual(self.records()['host-1']['job_type'], 'reinstall')

    def test_edited_survey_columns_override_json_and_config_and_split_batches(self):
        self.enable_survey([{'variable': 'operation', 'type': 'multiplechoice', 'required': True,
                             'choices': ['install', 'reinstall', 'update'], 'default': 'install'}])
        self.settings['SURVEY_ANSWERS'] = {'operation': 'install'}
        workbook = self.workbook([['host-1', self.link(), '{"operation":"install"}', None],
                                  ['host-2', self.link(), '{}', None]])
        workbook.active['F1'] = 'Survey_operation'
        workbook.active['F2'] = 'reinstall'
        workbook.active['F3'] = 'update'
        workbook.save(self.path)
        workbook.close()
        self.assertEqual(self.run_main(), 0)
        launches = [call[2] for call in self.server.calls if call[0] == 'POST']
        self.assertEqual([(item['limit'], item['extra_vars']) for item in launches],
                         [('host-1', {'operation': 'reinstall'}), ('host-2', {'operation': 'update'})])
        self.assertEqual(self.records()['host-1']['survey_operation'], 'reinstall')

    def test_survey_columns_are_added_with_default_and_used_as_typed_answers(self):
        self.enable_survey([{'variable': 'count', 'type': 'integer', 'required': True, 'default': 3}])
        self.settings['SURVEY_ANSWERS'] = {'count': 7}
        self.save_rows([['host-1', self.link(), '{}', None]])
        self.assertEqual(self.run_main(), 0)
        launch = next(call[2] for call in self.server.calls if call[0] == 'POST')
        self.assertEqual(launch['extra_vars'], {'count': 7})
        self.assertEqual(self.records()['host-1']['survey_count'], 7)
        self.assertEqual(sum(call[1].endswith('/survey_spec/') for call in self.server.calls), 1)

    def test_excel_defaults_for_other_templates_are_not_sent_to_the_job(self):
        self.enable_survey([{'variable': 'operation', 'type': 'text', 'required': True, 'default': 'install'}])
        self.settings['SURVEY_ANSWERS'] = {}
        self.settings['SURVEY_EXCEL_DEFAULTS'] = {'operation': 'reinstall', 'other_template_answer': 'unused'}
        self.save_rows([['host-1', self.link(), '{}', None]])
        self.assertEqual(self.run_main(), 0)
        launch = next(call[2] for call in self.server.calls if call[0] == 'POST')
        self.assertEqual(launch['extra_vars'], {'operation': 'reinstall'})
        self.assertEqual(self.records()['host-1']['survey_operation'], 'reinstall')

    def test_only_columns_of_the_launched_templates_survey_are_used(self):
        self.enable_survey([{'variable': 'operation', 'type': 'text', 'required': True, 'default': 'install'}])
        workbook = self.workbook([['host-1', self.link(), '{}', None]])
        workbook.active['F1'] = 'Survey_old_template_answer'
        workbook.active['F2'] = 'stale value'
        workbook.save(self.path)
        workbook.close()
        self.assertEqual(self.run_main(), 0)
        launch = next(call[2] for call in self.server.calls if call[0] == 'POST')
        self.assertEqual(launch['extra_vars'], {'operation': 'install'})

    def test_explicit_cli_answers_override_invalid_survey_cells(self):
        self.enable_survey([{'variable': 'count', 'type': 'integer', 'required': True}])
        workbook = self.workbook([['host-1', self.link(), '{}', None]])
        workbook.active['F1'] = 'Survey_count'
        workbook.active['F2'] = 'invalid unused answer'
        workbook.save(self.path)
        workbook.close()
        self.assertEqual(self.run_main('--survey-answers', '{"count":2}'), 0)
        launch = next(call[2] for call in self.server.calls if call[0] == 'POST')
        self.assertEqual(launch['extra_vars'], {'count': 2})

    def test_survey_cell_overrides_saved_job_type_and_cli_job_type_wins(self):
        self.enable_survey([{'variable': 'action', 'type': 'multiplechoice', 'required': True,
                             'choices': ['install', 'reinstall', 'update']}])
        self.settings['JOB_TYPE'] = 'install'
        workbook = self.workbook([['host-1', self.link(), '{}', None]])
        sheet = workbook.active
        sheet['F1'], sheet['F2'] = 'Survey_action', 'reinstall'
        sheet['G1'], sheet['G2'] = 'Job_Type', 'update'
        workbook.save(self.path)
        workbook.close()
        before = self.path.read_bytes()
        self.assertEqual(self.run_main(), 0)
        launch = next(call[2] for call in self.server.calls if call[0] == 'POST')
        self.assertEqual(launch['extra_vars'], {'action': 'reinstall'})
        self.assertEqual(self.records()['host-1']['job_type'], 'reinstall')
        self.path.write_bytes(before)
        self.server.calls = []
        self.assertEqual(self.run_main('--job-type', 'install'), 0)
        launch = next(call[2] for call in self.server.calls if call[0] == 'POST')
        self.assertEqual(launch['extra_vars'], {'action': 'install'})

    def test_dry_run_uses_survey_cells_without_fetching_schema_or_changing_excel(self):
        workbook = self.workbook([['host-1', self.link(), '{}', None], ['host-2', self.link(), '{}', None]])
        sheet = workbook.active
        sheet['F1'], sheet['F2'], sheet['F3'] = 'Survey_operation', 'install', 'reinstall'
        workbook.save(self.path)
        workbook.close()
        before = self.path.read_bytes()
        self.assertEqual(self.run_main('--dry-run'), 0)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn('batches: 2', self.output.getvalue())

    def test_survey_action_can_override_an_invalid_lower_priority_json_value(self):
        self.enable_survey([{'variable': 'action', 'type': 'multiplechoice', 'required': True,
                             'choices': ['install', 'reinstall']}])
        workbook = self.workbook([['host-1', self.link(), '{"action":3}', None]])
        workbook.active['F1'], workbook.active['F2'] = 'Survey_action', 'reinstall'
        workbook.save(self.path)
        workbook.close()
        self.assertEqual(self.run_main(), 0)
        launch = next(call[2] for call in self.server.calls if call[0] == 'POST')
        self.assertEqual(launch['extra_vars'], {'action': 'reinstall'})

    def test_show_survey_reads_disabled_spec_without_opening_excel_or_credentials(self):
        self.server.survey_spec['spec'] = [{'variable': 'operation', 'type': 'multiplechoice', 'required': True,
                                          'default': 'install', 'choices': ['install', 'update']}]
        self.assertFalse(self.path.exists())
        self.assertEqual(self.run_main('--show-survey', '--job-url', self.link()), 0)
        self.assertIn('survey disabled', self.output.getvalue())
        self.assertIn('operation', self.output.getvalue())
        self.assertEqual([call[0] for call in self.server.calls], ['GET', 'GET'])
        self.assertFalse(self.path.exists())

    def test_interactive_survey_dry_run_reuses_prompt_and_does_not_write_or_launch(self):
        self.enable_survey([{'variable': 'operation', 'type': 'multiplechoice', 'required': True,
                             'choices': ['install', 'reinstall']}])
        self.settings['BATCH_SIZE'] = 1
        self.save_rows([['host-1', self.link(), '{}', None], ['host-2', self.link(), '{}', None]])
        before = self.path.read_bytes()
        with patch.object(runner.survey.sys.stdin, 'isatty', return_value=True), \
             patch('builtins.input', return_value='2') as input_mock:
            self.assertEqual(self.run_main('--survey', '--dry-run'), 0)
        input_mock.assert_called_once()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(any(call[0] == 'POST' for call in self.server.calls))
        self.assertFalse(any('/credentials/' in call[1] for call in self.server.calls))

    def test_disabled_survey_fails_before_launch_when_prompting_requested(self):
        self.save_rows([['host-1', self.link(), '{}', None]])
        before = self.path.read_bytes()
        with self.assertRaisesRegex(runner.AWXError, 'survey is disabled.*Enable Survey'):
            self.run_main('--survey')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(any(call[0] == 'POST' for call in self.server.calls))

    def test_password_survey_answers_are_redacted_from_api_errors_and_dry_run(self):
        self.enable_survey([{'variable': 'private', 'type': 'password', 'required': True}])
        self.server.launch_error = {'detail': 'Bad answer: very-private-value'}
        self.save_rows([['host-1', self.link(), '{"private":"very-private-value"}', None]])
        with self.assertRaises(runner.AWXError) as error:
            self.run_main('-d')
        self.assertNotIn('very-private-value', str(error.exception))
        self.assertNotIn('very-private-value', self.output.getvalue())
        self.assertIn('***', str(error.exception))
        self.assertEqual(self.run_main('--dry-run'), 0)
        self.assertNotIn('very-private-value', self.output.getvalue())

    def test_malformed_survey_cli_answers_fail_without_requests(self):
        for answers in ('invalid JSON', '[]', 'null'):
            with self.subTest(answers=answers), self.assertRaises(runner.AWXError):
                self.run_main('--survey-answers', answers)
        self.assertEqual(self.server.calls, [])

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

    def selected_host_report(self, hostnames, missing=()):
        self.connection['EXTRA_CREDENTIAL_VARIABLES'] = []
        return {
            'inventory': {'id': 1, 'name': 'test_inventory'},
            'host_count': len(hostnames),
            'hosts': [{'id': index, 'name': name, 'ostype': 'linux',
                       'credential_groups': [{'variable': 'os_credential', 'value': 'machine-cred'}]}
                      for index, name in enumerate(hostnames, start=1)],
            'missing_hosts': list(missing),
        }

    def records(self):
        workbook = runner.load_workbook(self.path)
        sheet = workbook.active
        columns = runner.excel.read_columns(sheet)
        records = {sheet.cell(row, columns['hostname']).value:
                   {name: sheet.cell(row, column).value for name, column in columns.items()}
                   for row in range(2, sheet.max_row + 1)}
        workbook.close()
        return records

    def test_selected_hosts_only_launch_for_selected_rows(self):
        self.save_rows([['host-1', self.link(), '{}', None], ['host-2', self.link(), '{}', None]])
        self.assertEqual(self.run_main('--hosts', 'host-2'), 0)
        launches = [call for call in self.server.calls if call[0] == 'POST']
        self.assertEqual([call[2]['limit'] for call in launches], ['host-2'])
        records = self.records()
        self.assertIsNone(records['host-1']['exceiute_job'])
        self.assertIn('/101/', records['host-2']['exceiute_job'])

    def test_explicit_job_url_overrides_old_link_and_hyperlink_only_for_selected_host(self):
        self.save_rows([['host-1', self.link(), '{}', None], ['host-2', self.link(), '{}', None]])
        workbook = runner.load_workbook(self.path)
        self.cell(workbook.active, 3, 'Job_Link').hyperlink = self.link(8)
        workbook.save(self.path)
        workbook.close()
        self.assertEqual(self.run_main('--hosts', 'host-2', '--job-url', self.link(9)), 0)
        launch = next(call for call in self.server.calls if call[0] == 'POST')
        self.assertEqual(launch[1], '/api/v2/job_templates/9/launch/')
        records = self.records()
        self.assertEqual(records['host-1']['job_link'], self.link())
        self.assertIsNone(records['host-1']['exceiute_job'])
        self.assertEqual(records['host-2']['job_link'], self.link(9))
        workbook = runner.load_workbook(self.path)
        self.assertEqual(self.cell(workbook.active, 3, 'Job_Link').hyperlink.target, self.link(9))
        workbook.close()

    def test_explicit_job_url_can_replace_empty_excel_link(self):
        self.save_rows([['host-1', 'none', '{}', None]])
        self.assertEqual(self.run_main('--host', 'host-1', '--job-url', self.link(9)), 0)
        self.assertEqual([call[1] for call in self.server.calls if call[0] == 'POST'], ['/api/v2/job_templates/9/launch/'])

    def test_cli_host_and_job_url_override_config_selection(self):
        self.settings.update(HOSTS=['host-1'], JOB_URL=self.link(8))
        self.save_rows([['host-1', self.link(), '{}', None], ['host-2', self.link(), '{}', None]])
        self.assertEqual(self.run_main('--hosts', 'host-2', '--job-url', self.link(9)), 0)
        launches = [call for call in self.server.calls if call[0] == 'POST']
        self.assertEqual([(call[1], call[2]['limit']) for call in launches], [('/api/v2/job_templates/9/launch/', 'host-2')])

    def test_config_host_and_job_url_selection_work_without_cli_overrides(self):
        self.settings.update(HOSTS=['host-2'], JOB_URL=self.link(9))
        self.save_rows([['host-1', self.link(), '{}', None], ['host-2', self.link(), '{}', None]])
        self.assertEqual(self.run_main(), 0)
        launches = [call for call in self.server.calls if call[0] == 'POST']
        self.assertEqual([(call[1], call[2]['limit']) for call in launches], [('/api/v2/job_templates/9/launch/', 'host-2')])

    def test_selected_host_dry_run_leaves_excel_unchanged_and_makes_no_requests(self):
        self.save_rows([['host-1', self.link(), '{}', None], ['host-2', self.link(), '{}', None]])
        before = self.path.read_bytes()
        self.assertEqual(self.run_main('--hosts', 'host-2', '--job-url', self.link(9), '--dry-run'), 0)
        self.assertIn('template=9; limit=host-2', self.output.getvalue())
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_workbook_is_created_for_explicit_hosts_and_job_url(self):
        report = self.selected_host_report(['host-1', 'host-2'])
        with patch.object(runner, 'fetch_report', return_value=report) as fetch:
            self.assertEqual(self.run_main('--hosts', 'host-1,host-2', 'host-1', '--job-url', self.link(9), '--batch-size', '1'), 0)
        self.assertEqual(fetch.call_args.kwargs['hostnames'], ['host-1', 'host-2'])
        launches = [call for call in self.server.calls if call[0] == 'POST']
        self.assertEqual([call[2]['limit'] for call in launches], ['host-1', 'host-2'])
        self.assertTrue(all(call[1] == '/api/v2/job_templates/9/launch/' for call in launches))
        records = self.records()
        self.assertEqual(set(records), {'host-1', 'host-2'})
        self.assertEqual(records['host-1']['inventory'], 'test_inventory')
        self.assertEqual(records['host-1']['full_credential'], 'machine-cred')
        self.assertIn('/101/', records['host-1']['exceiute_job'])
        self.assertIn('/102/', records['host-2']['exceiute_job'])

    def test_missing_workbook_dry_run_fetches_metadata_but_does_not_save_or_launch(self):
        report = self.selected_host_report(['host-1'])
        with patch.object(runner, 'fetch_report', return_value=report) as fetch:
            self.assertEqual(self.run_main('--hosts', 'host-1', '--job-url', self.link(9), '--dry-run'), 0)
        fetch.assert_called_once()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.server.calls, [('GET', '/api/v2/job_templates/9/launch/', None)])
        self.assertIn('template=9; limit=host-1', self.output.getvalue())

    def test_new_hosts_use_extra_credentials_from_the_cli_selected_job(self):
        self.settings['JOB_URL'] = self.link(8)
        self.server.launch_details['defaults']['credentials'] = [self.server.credentials[32]]
        report = self.selected_host_report(['host-1'])
        with patch.object(runner, 'fetch_report', return_value=report):
            self.assertEqual(self.run_main('--hosts', 'host-1', '--job-url', self.link(9)), 0)
        record = self.records()['host-1']
        self.assertEqual(record['extra_credentials'], 'vault-cred')
        self.assertEqual(record['full_credential'], 'vault-cred, machine-cred')
        launch = next(call for call in self.server.calls if call[0] == 'POST')
        self.assertEqual(launch[1], '/api/v2/job_templates/9/launch/')
        self.assertEqual(launch[2]['credentials'], [31, 32])
        self.assertEqual(sum(call[1] == '/api/v2/job_templates/9/launch/' for call in self.server.calls if call[0] == 'GET'), 1)
        self.assertFalse(any('/job_templates/8/' in call[1] for call in self.server.calls))

    def test_new_selected_host_is_appended_without_launching_unselected_existing_hosts(self):
        report = self.selected_host_report(['host-2'])
        self.save_rows([['host-1', self.link(), '{}', None]])
        with patch.object(runner, 'fetch_report', return_value=report):
            self.assertEqual(self.run_main('--hosts', 'host-2', '--job-url', self.link(9)), 0)
        self.assertEqual([call[2]['limit'] for call in self.server.calls if call[0] == 'POST'], ['host-2'])
        self.assertIsNone(self.records()['host-1']['exceiute_job'])

    def test_host_missing_from_awx_stops_before_launch_and_file_changes(self):
        report = self.selected_host_report([], missing=['missing-host'])
        self.save_rows([['host-1', self.link(), '{}', None]])
        before = self.path.read_bytes()
        with patch.object(runner, 'fetch_report', return_value=report):
            with self.assertRaisesRegex(runner.AWXError, 'Selected hosts not found'):
                self.run_main('--hosts', 'missing-host', '--job-url', self.link(9))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.server.calls, [])

    def test_selected_execution_history_is_preserved_with_a_different_job_url(self):
        recorded = f'{self.base}/#/jobs/playbook/99/output'
        self.save_rows([['host-1', self.link(), '{}', recorded]])
        self.assertEqual(self.run_main('--hosts', 'host-1', '--job-url', self.link(9)), 0)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.records()['host-1']['exceiute_job'], recorded)
        self.assertEqual(self.records()['host-1']['job_link'], self.link())

    def test_confirmed_rerun_launches_once_and_keeps_previous_link_in_comment(self):
        old = self.base + '/#/jobs/playbook/99/output'
        self.save_rows([['host-1', self.link(), '{}', old], ['host-1', self.link(), '{}', old]])
        workbook = runner.load_workbook(self.path)
        self.cell(workbook.active, 2, 'exceiute_job').comment = runner.Comment('User note', 'User')
        workbook.save(self.path)
        workbook.close()
        with patch.object(runner.sys.stdin, 'isatty', return_value=True), \
             patch('builtins.input', return_value=' TAK ') as prompt:
            self.assertEqual(self.run_main(), 0)
        prompt.assert_called_once()
        self.assertIn('Czy potwierdzasz ponowne uruchomienie joba', prompt.call_args.args[0])
        self.assertIn('[tak/N]', prompt.call_args.args[0])
        launches = [call[2] for call in self.server.calls if call[0] == 'POST']
        self.assertEqual(len(launches), 1)
        self.assertEqual(launches[0]['limit'], 'host-1')
        workbook = runner.load_workbook(self.path)
        try:
            for row in (2, 3):
                cell = self.cell(workbook.active, row, 'exceiute_job')
                self.assertIn('/jobs/playbook/101/', cell.value)
                self.assertEqual(cell.hyperlink.target, cell.value)
                self.assertIn(old, cell.comment.text)
            self.assertIn('User note', self.cell(workbook.active, 2, 'exceiute_job').comment.text)
        finally:
            workbook.close()

    def test_declined_or_empty_confirmation_does_not_query_api_or_write_workbook(self):
        old = self.base + '/#/jobs/playbook/99/output'
        self.save_rows([['host-1', self.link(), '{}', old]])
        before = self.path.read_bytes()
        for answer in ('', 'nie', 'n', 'no', 'maybe'):
            with self.subTest(answer=answer), patch.object(runner.sys.stdin, 'isatty', return_value=True), \
                 patch('builtins.input', return_value=answer) as prompt:
                self.assertEqual(self.run_main(), 0)
                prompt.assert_called_once()
                self.assertEqual(self.server.calls, [])
                self.assertEqual(self.path.read_bytes(), before)

    def test_rerun_confirmation_eof_and_nonterminal_leave_workbook_unchanged(self):
        self.save_rows([['host-1', self.link(), '{}', self.base + '/#/jobs/playbook/99/output']])
        before = self.path.read_bytes()
        with patch.object(runner.sys.stdin, 'isatty', return_value=True), patch('builtins.input', side_effect=EOFError):
            self.assertEqual(self.run_main(), 0)
        with patch.object(runner.sys.stdin, 'isatty', return_value=False), \
             patch('builtins.input', side_effect=AssertionError('No prompt without a terminal')):
            self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_confirmed_rerun_with_preflight_failure_preserves_previous_file(self):
        self.save_rows([['host-1', self.link(), '{}', self.base + '/#/jobs/playbook/99/output']])
        self.server.launch_details['ask_limit_on_launch'] = False
        before = self.path.read_bytes()
        with patch.object(runner.sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='tak'), \
             self.assertRaisesRegex(runner.AWXError, 'Limit'):
            self.run_main()
        self.assertFalse(any(call[0] == 'POST' for call in self.server.calls))
        self.assertEqual(self.path.read_bytes(), before)

    def test_rerun_confirmation_applies_only_to_selected_hosts(self):
        old = self.base + '/#/jobs/playbook/99/output'
        self.save_rows([['host-1', self.link(), '{}', old], ['host-2', self.link(), '{}', old]])
        with patch.object(runner.sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='tak'):
            self.assertEqual(self.run_main('--hosts', 'host-2', '--job-url', self.link(9)), 0)
        launches = [call for call in self.server.calls if call[0] == 'POST']
        self.assertEqual(len(launches), 1)
        self.assertEqual(launches[0][1], '/api/v2/job_templates/9/launch/')
        self.assertEqual(launches[0][2]['limit'], 'host-2')
        records = self.records()
        self.assertEqual(records['host-1']['exceiute_job'], old)
        self.assertEqual(records['host-1']['job_link'], self.link())
        self.assertIn('/101/', records['host-2']['exceiute_job'])
        self.assertEqual(records['host-2']['job_link'], self.link(9))

    def test_confirmed_rerun_can_retry_a_failed_previous_job(self):
        self.save_rows([['host-1', self.link(), '{}', self.base + '/#/jobs/playbook/99/output']])
        self.server.job_status_by_id['99'] = 'failed'
        with patch.object(runner.sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='tak'):
            self.assertEqual(self.run_main(), 0)
        self.assertEqual(len([call for call in self.server.calls if call[0] == 'POST']), 1)
        self.assertIn('/101/', self.records()['host-1']['exceiute_job'])

    def test_dry_run_and_pending_markers_never_prompt_for_rerun(self):
        for execution, args in ((self.base + '/#/jobs/playbook/99/output', ['--dry-run']),
                                ('LAUNCH_PENDING template=7', [])):
            with self.subTest(execution=execution):
                self.save_rows([['host-1', self.link(), '{}', execution]])
                before = self.path.read_bytes()
                with patch.object(runner.sys.stdin, 'isatty', return_value=True), \
                     patch('builtins.input', side_effect=AssertionError('Unexpected rerun prompt')):
                    if args:
                        self.assertEqual(self.run_main(*args), 0)
                    else:
                        with self.assertRaisesRegex(runner.AWXError, 'LAUNCH_PENDING'):
                            self.run_main()
                self.assertEqual(self.server.calls, [])
                self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_host_selection_or_url_stops_before_requests(self):
        self.save_rows([['host-1', self.link(), '{}', None]])
        for args in (('--hosts', 'all:*'), ('--hosts', ''), ('--hosts', 'host-1', '--job-url', ''),
                     ('--hosts', 'host-1', '--job-url', 'https://other.example/#/templates/job_template/7/details'),
                     ('--hosts', 'host-1', '--job-url', f'{self.base}/#/jobs/playbook/101/output')):
            with self.subTest(args=args):
                before = self.path.read_bytes()
                with self.assertRaises(runner.AWXError):
                    self.run_main(*args)
                self.assertEqual(self.server.calls, [])
                self.assertEqual(self.path.read_bytes(), before)

    def test_hosts_accept_literal_lists_strings_and_reject_patterns(self):
        self.assertEqual(runner.common.normalize_hostnames(['host-1,host-2', 'host-1;host-3']), ['host-1', 'host-2', 'host-3'])
        self.assertEqual(runner.common.normalize_hostnames('host-1 host-2'), ['host-1', 'host-2'])
        for value in ('*', 'host[1:3]', 'host-1:host-2', [''], [3], 3):
            with self.subTest(value=value), self.assertRaises(runner.AWXError):
                runner.common.normalize_hostnames(value)

    def test_invalid_settings(self):
        for contents in ('BATCH_SIZE = 0', 'BATCH_SIZE = True', "WAIT_FOR_COMPLETION = 'yes'", "EXECUTION_COLUMN = 'hostname'", "EXECUTION_COLUMN = 'Full_Credential'", "EXECUTION_COLUMN = 'Artifacts'", "EXECUTION_COLUMN = 'artifact filter'", 'JOB_TYPE = 123', "JOB_TYPE_VARIABLE = 'invalid-name'"):
            with self.subTest(contents=contents):
                settings_file = Path(self.directory.name) / 'variables'
                settings_file.write_text(contents)
                with self.assertRaises(runner.AWXError):
                    runner.read_execution_settings(settings_file)
if __name__ == '__main__':
    unittest.main()
