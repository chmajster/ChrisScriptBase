"""Refresh tests use fake AWX responses and temporary workbooks only."""
import contextlib
import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from lib import awx as refresh

class FakeAWX(refresh.AWXClient):

    def __init__(self, base_url='http://awx.test'):
        super().__init__(base_url, 'test-token')
        self.calls = []
        self.surveys = {}
        self.template_credentials = {}
        self.credential_records = {name: {'id': index, 'name': name, 'credential_type': 1, 'kind': 'ssh', 'inputs': {}}
                                   for index, name in enumerate(('windows-key', 'linux-key', 'linux-second-key', 'aix-key'), 101)}
        self.hosts = [{'id': 1, 'name': 'windows-1'}, {'id': 2, 'name': 'linux-1'}, {'id': 3, 'name': 'aix-1'}, {'id': 4, 'name': 'unrequested-host'}]

    def get(self, path, **kwargs):
        self.calls.append(('GET', path, kwargs))
        if path == 'inventories/1/':
            return {'id': 1, 'name': 'test_inventory'}
        if path.startswith('job_templates/') and path.endswith('/launch/'):
            template_id = int(path.split('/')[1])
            return {'survey_enabled': template_id in self.surveys,
                    'defaults': {'credentials': self.template_credentials.get(template_id, [])}}
        if path.startswith('job_templates/') and path.endswith('/survey_spec/'):
            return {'spec': self.surveys[int(path.split('/')[1])]}
        if path.startswith('credentials/'):
            credential_id = int(path.strip('/').split('/')[1])
            return next(value for value in self.credential_records.values() if value['id'] == credential_id)
        if path == 'inventories/1/script/?all=1&hostvars=1':
            return {'_meta': {'hostvars': {'windows-1': {'ostype': 'windows'}, 'linux-1': {'ostype': 'linux'}, 'aix-1': {'ostype': 'aix'}}}, 'windows-group': {'vars': {'os_credential': 'windows-key'}, 'hosts': ['windows-1']}, 'linux-group': {'vars': {'os_credential': 'linux-key'}, 'hosts': ['linux-1']}, 'linux-other': {'vars': {'os_credential': 'linux-second-key'}, 'hosts': ['linux-1']}, 'aix-group': {'vars': {'os_credential': 'aix-key'}, 'hosts': ['aix-1']}}
        raise AssertionError(path)

    def items(self, path, **kwargs):
        self.calls.append(('ITEMS', path, kwargs))
        if path == 'inventories/1/hosts/':
            return iter(self.hosts)
        if path == 'inventories/1/groups/':
            return iter(({'id': index, 'name': name} for index, name in enumerate(('windows-group', 'linux-group', 'linux-other', 'aix-group'), start=11)))
        if path == 'credentials/':
            return iter(value for value in self.credential_records.values()
                        if (value['name'] == kwargs['name'] if 'name' in kwargs
                            else kwargs.get('name__icontains', '').casefold() in value['name'].casefold()))
        raise AssertionError(path)

class RefreshTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'hosts.xlsx'
        self.client = FakeAWX()
        self.connection = deepcopy(refresh.INVENTORY_DEFAULTS)
        self.connection.update(AWX_URL='http://awx.test', AWX_TOKEN='test-token', INVENTORY=1, EXCEL_OUTPUT_FILE=self.path, CREDENTIAL_VARIABLES=['os_credential'], EXTRA_CREDENTIAL_VARIABLES=['shared-vault'], FULL_CREDENTIAL_SEPARATOR=', ', OS_JOBS={os: {'url': f"http://awx.test/#/templates/job_template/{index}/details", 'variables': {'source': os}} for index, os in enumerate(('windows', 'linux', 'aix'), start=7)})
        self.settings = deepcopy(refresh.EXECUTION_DEFAULTS)
        self.settings.update(JOB_TYPE='install', JOB_TYPE_VARIABLE='operation', JOB_URL='',
                             SURVEY_ANSWERS={}, SURVEY_EXCEL_DEFAULTS={}, SURVEY_INTERACTIVE=False)
        self.output = io.StringIO()
        self.addCleanup(refresh.apply_settings, refresh.INVENTORY_DEFAULTS)

    def save_rows(self, headers, rows, sheet_name='Results'):
        workbook = refresh.Workbook()
        sheet = workbook.active
        sheet.title = sheet_name
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                if str(cell.value or '').startswith('http'):
                    cell.hyperlink = cell.value
        other = workbook.create_sheet('Notes')
        other['A1'] = 'Keep this worksheet'
        workbook.save(self.path)
        workbook.close()

    def run_main(self, *args):
        with patch.object(refresh, 'read_inventory_settings', return_value=self.connection), patch.object(refresh, 'read_execution_settings', return_value=self.settings), patch.object(refresh, 'AWXClient', return_value=self.client), contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            return refresh.refresh_main(['-file', str(self.path), *args])

    def read_records(self, sheet_name='Results'):
        workbook = refresh.load_workbook(self.path)
        sheet = workbook[sheet_name]
        rows = list(sheet.values)
        records = [dict(zip(rows[0], row)) for row in rows[1:]]
        workbook.close()
        return records

    def test_refreshes_from_host_column_only_and_preserves_job_history(self):
        headers = ['hostname', 'ostype', 'Job_Link', 'Job_Variables', 'Full_Credential', 'Job_Type', 'exceiute_job', 'output', 'notes', 'INVENTORY']
        rows = [[name, 'stale-os', 'http://old.example/template', '{"old":true}', 'old-key', 'old-action', f"http://awx.test/#/jobs/playbook/{job_id}/output", f"log for {name}", f"note for {name}", 'old-inventory'] for job_id, name in enumerate(('aix-1', 'linux-1', 'windows-1'), start=101)]
        self.save_rows(headers, rows)
        self.assertEqual(self.run_main(), 0)
        records = self.read_records()
        self.assertEqual([row['hostname'] for row in records], ['windows-1', 'linux-1', 'aix-1'])
        for record in records:
            name = record['hostname']
            os = name.split('-')[0]
            self.assertEqual(record['ostype'], os)
            self.assertEqual(record['INVENTORY'], 'test_inventory')
            self.assertEqual(record['ApplicationID'], 'test')
            self.assertEqual(record['Full_Credential'].split(', ')[0], 'shared-vault')
            self.assertEqual(record['Job_Type'], 'install')
            self.assertEqual(json.loads(record['Job_Variables']), {'source': os, 'operation': 'install'})
            self.assertEqual(record['output'], f"log for {name}")
            self.assertEqual(record['notes'], f"note for {name}")
            self.assertEqual(record['AWX_Status'], 'found')
        self.assertEqual(records[1]['OS Credential'], 'linux-key, linux-second-key')
        self.assertEqual(records[1]['Full_Credential'], 'shared-vault, linux-key, linux-second-key')
        workbook = refresh.load_workbook(self.path)
        sheet = workbook['Results']
        self.assertEqual(sheet.cell(2, refresh.excel.read_columns(sheet)['exceiute_job']).hyperlink.target, 'http://awx.test/#/jobs/playbook/103/output')
        self.assertEqual(sheet.cell(2, refresh.excel.read_columns(sheet)['job_link']).hyperlink.target, self.connection['OS_JOBS']['windows']['url'])
        self.assertEqual(workbook['Notes']['A1'].value, 'Keep this worksheet')
        workbook.close()
        host_call = next((call for call in self.client.calls if call[1] == 'inventories/1/hosts/'))
        self.assertEqual(host_call[2]['name__in'], 'aix-1,linux-1,windows-1')

    def test_host_only_sheet_gets_all_metadata_and_canonical_hostname(self):
        self.save_rows(['host_limit'], [['linux-1'], ['windows-1']])
        self.assertEqual(self.run_main(), 0)
        records = self.read_records()
        self.assertEqual([row['hostname'] for row in records], ['windows-1', 'linux-1'])
        self.assertEqual(records[0]['host_limit'], 'windows-1')
        self.assertEqual(records[0]['Full_Credential'], 'shared-vault, windows-key')
        self.assertEqual(records[0]['INVENTORY'], 'test_inventory')

    def test_duplicate_hosts_are_looked_up_once_and_rows_are_preserved(self):
        self.save_rows(['hostname', 'output'], [['linux-1', 'first log'], ['linux-1', 'second log']])
        self.assertEqual(self.run_main(), 0)
        records = self.read_records()
        self.assertEqual([row['output'] for row in records], ['first log', 'second log'])
        self.assertEqual(records[0]['Full_Credential'], records[1]['Full_Credential'])
        host_call = next((call for call in self.client.calls if call[1] == 'inventories/1/hosts/'))
        self.assertEqual(host_call[2]['name__in'], 'linux-1')

    def test_missing_hosts_have_stale_metadata_cleared_but_keep_history(self):
        self.save_rows(['hostname', 'Job_Link', 'Full_Credential', 'exceiute_job', 'output', 'INVENTORY'], [['not-found', 'old-template', 'old-key', 'http://awx.test/#/jobs/playbook/99/output', 'old log', 'old-inventory'], ['windows-1', None, None, None, None, None]])
        self.assertEqual(self.run_main(), 0)
        record = self.read_records()[1]
        self.assertEqual(record['AWX_Status'], 'not_found')
        self.assertEqual(record['Full_Credential'], 'none')
        self.assertEqual(record['INVENTORY'], 'none')
        self.assertEqual(record['Job_Link'], 'none')
        self.assertEqual(record['output'], 'old log')
        self.assertIn('99', record['exceiute_job'])

    def test_all_missing_hosts_are_marked_without_removing_rows(self):
        self.save_rows(['hostname', 'Full_Credential'], [['not-found', 'old-key']])
        self.assertEqual(self.run_main(), 0)
        record = self.read_records()[0]
        self.assertEqual(record['hostname'], 'not-found')
        self.assertEqual(record['AWX_Status'], 'not_found')
        self.assertEqual(record['Full_Credential'], 'none')

    def test_custom_host_column_and_sheet(self):
        self.save_rows(['Servers'], [['aix-1']], sheet_name='Input')
        self.assertEqual(self.run_main('--host-column', 'servers', '--sheet', 'Input'), 0)
        self.assertEqual(self.read_records('Input')[0]['hostname'], 'aix-1')

    def test_active_worksheet_fallback_and_case_insensitive_host_header(self):
        self.save_rows(['HOSTNAME'], [['windows-1']], sheet_name='Sheet1')
        self.assertEqual(self.run_main(), 0)
        records = self.read_records('Sheet1')
        self.assertEqual(records[0]['hostname'], 'windows-1')
        self.assertEqual(records[0]['AWX_Status'], 'found')

    def test_api_error_leaves_existing_workbook_unchanged(self):
        self.save_rows(['hostname', 'Full_Credential'], [['linux-1', 'old-key']])
        before = self.path.read_bytes()
        with patch.object(self.client, 'get', side_effect=refresh.AWXError('AWX unavailable')):
            with self.assertRaisesRegex(refresh.AWXError, 'unavailable'):
                self.run_main()
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_input_stops_before_api_requests(self):
        for headers, rows in ((['hostname'], [['all:*']]), (['hostname', 'HOSTNAME'], [['linux-1', 'aix-1']])):
            with self.subTest(headers=headers, rows=rows):
                self.save_rows(headers, rows)
                before = self.path.read_bytes()
                with self.assertRaises(refresh.AWXError):
                    self.run_main()
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.client.calls, [])

    def test_missing_schema_columns_are_recreated_including_job_history_columns(self):
        self.save_rows(['hostname'], [['windows-1']])
        self.assertEqual(self.run_main(), 0)
        record = self.read_records()[0]
        expected = {'hostname', 'INVENTORY', 'ApplicationID', 'ostype', 'Job_Link', 'Job_Credentials', 'Job_Variables', 'HOPS', 'HOPS_COUNT', 'jumphost_for_hops', 'Extra_Credentials', 'OS Credential', 'Full_Credential', 'Job_Type', 'AWX_Status', 'exceiute_job', 'output', 'Artifacts', 'artifact filter'}
        self.assertEqual(set(record), expected)
        self.assertIsNone(record['exceiute_job'])
        self.assertIsNone(record['output'])
        self.assertEqual(record['INVENTORY'], 'test_inventory')
        self.assertIn('Added missing columns', self.output.getvalue())

    def test_deleted_columns_are_restored_on_next_refresh_without_losing_history(self):
        self.save_rows(['hostname', 'exceiute_job', 'output'], [['windows-1', 'http://awx.test/#/jobs/playbook/99/output', 'saved log']])
        self.assertEqual(self.run_main(), 0)
        workbook = refresh.load_workbook(self.path)
        sheet = workbook['Results']
        to_delete = {'INVENTORY', 'Job_Link', 'Full_Credential', 'OS Credential', 'Job_Type'}
        for column in sorted((cell.column for cell in sheet[1] if cell.value in to_delete), reverse=True):
            sheet.delete_cols(column)
        workbook.save(self.path)
        workbook.close()
        self.assertEqual(self.run_main(), 0)
        record = self.read_records()[0]
        self.assertEqual(record['INVENTORY'], 'test_inventory')
        self.assertEqual(record['Full_Credential'], 'shared-vault, windows-key')
        self.assertEqual(record['Job_Type'], 'install')
        self.assertEqual(record['exceiute_job'], 'http://awx.test/#/jobs/playbook/99/output')
        self.assertEqual(record['output'], 'saved log')

    def test_repeated_refresh_does_not_duplicate_columns(self):
        self.save_rows(['hostname'], [['linux-1']])
        self.assertEqual(self.run_main(), 0)
        first_headers = list(self.read_records()[0])
        self.assertEqual(self.run_main(), 0)
        workbook = refresh.load_workbook(self.path)
        headers = [cell.value for cell in workbook['Results'][1]]
        workbook.close()
        self.assertEqual(headers, first_headers)
        self.assertEqual(len(headers), len(set((name.casefold() for name in headers))))

    def test_refresh_preserves_artifacts_and_repairs_deleted_artifact_columns(self):
        self.save_rows(['hostname', 'Artifacts', 'artifact filter', 'exceiute_job'],
                       [['linux-1', '{"linux-1":{"message":"Done"}}', 'Done', 'http://awx.test/#/jobs/playbook/99/output']])
        self.assertEqual(self.run_main(), 0)
        record = self.read_records()[0]
        self.assertEqual(record['Artifacts'], '{"linux-1":{"message":"Done"}}')
        self.assertEqual(record['artifact filter'], 'Done')
        workbook = refresh.load_workbook(self.path)
        sheet = workbook['Results']
        for column in sorted((cell.column for cell in sheet[1] if cell.value in ('Artifacts', 'artifact filter')), reverse=True):
            sheet.delete_cols(column)
        workbook.save(self.path)
        workbook.close()
        self.assertEqual(self.run_main(), 0)
        record = self.read_records()[0]
        self.assertIn('Artifacts', record)
        self.assertIn('artifact filter', record)
        self.assertIn('/jobs/playbook/99/', record['exceiute_job'])

    def test_refresh_detects_survey_columns_and_updates_configured_answers(self):
        self.settings['JOB_TYPE'] = ''
        self.client.surveys[7] = [{'variable': 'operation', 'type': 'multiplechoice', 'required': True,
                                   'choices': ['install', 'reinstall'], 'default': 'install'}]
        self.settings['SURVEY_ANSWERS'] = {'operation': 'reinstall', 'not_detected': 'unused'}
        self.save_rows(['hostname'], [['windows-1']])
        self.assertEqual(self.run_main(), 0)
        record = self.read_records()[0]
        self.assertEqual(record['Survey_operation'], 'reinstall')
        self.assertNotIn('Survey_not_detected', record)
        workbook = refresh.load_workbook(self.path)
        sheet = workbook['Results']
        column = refresh.excel.read_columns(sheet)['survey_operation']
        sheet.cell(2, column).value = 'install'
        workbook.save(self.path)
        workbook.close()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.read_records()[0]['Survey_operation'], 'reinstall')
        workbook = refresh.load_workbook(self.path)
        column = refresh.excel.read_columns(workbook['Results'])['survey_operation']
        workbook['Results'].delete_cols(column)
        workbook.save(self.path)
        workbook.close()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.read_records()[0]['Survey_operation'], 'reinstall')

    def test_refresh_excel_defaults_override_existing_and_generated_job_variables(self):
        self.client.surveys[7] = [{'variable': 'operation', 'type': 'multiplechoice', 'required': True,
                                  'choices': ['install', 'reinstall'], 'default': 'install'}]
        self.settings['SURVEY_EXCEL_DEFAULTS'] = {'operation': 'reinstall'}
        self.settings['SURVEY_ANSWERS'] = {'operation': 'install'}
        old_job = 'http://awx.test/#/jobs/playbook/99/output'
        self.save_rows(['hostname', 'Survey_operation', 'exceiute_job', 'output', 'Artifacts', 'artifact filter'],
                       [['windows-1', 'install', old_job, 'old stdout', '{"old":true}', 'old message']])
        self.assertEqual(self.run_main(), 0)
        record = self.read_records()[0]
        self.assertEqual(record['Survey_operation'], 'reinstall')
        self.assertEqual(json.loads(record['Job_Variables'])['operation'], 'install')
        self.assertEqual(record['exceiute_job'], old_job)
        self.assertEqual(record['output'], 'old stdout')
        self.assertEqual(record['Artifacts'], '{"old":true}')
        self.assertEqual(record['artifact filter'], 'old message')

    def test_refresh_invalid_configured_survey_answer_does_not_overwrite_file(self):
        self.client.surveys[7] = [{'variable': 'operation', 'type': 'multiplechoice', 'required': True,
                                  'choices': ['install', 'reinstall'], 'default': 'install'}]
        self.settings['SURVEY_EXCEL_DEFAULTS'] = {'operation': 'invalid choice'}
        self.save_rows(['hostname', 'Survey_operation'], [['windows-1', 'install']])
        before = self.path.read_bytes()
        with self.assertRaisesRegex(refresh.AWXError, 'survey choices'):
            self.run_main()
        self.assertEqual(self.path.read_bytes(), before)

    def test_refresh_uses_global_job_url_for_survey_discovery(self):
        self.settings['JOB_URL'] = 'http://awx.test/#/templates/job_template/14/details'
        self.client.surveys[14] = [{'variable': 'installtesttypename', 'type': 'text', 'required': True, 'default': 'install'}]
        self.save_rows(['hostname'], [['linux-1'], ['windows-1']])
        self.assertEqual(self.run_main(), 0)
        self.assertTrue(all(record['Survey_installtesttypename'] == 'install' for record in self.read_records()))
        self.assertTrue(all(record['Job_Link'] == self.settings['JOB_URL'] for record in self.read_records()))
        self.assertEqual(sum(call[1] == 'job_templates/14/survey_spec/' for call in self.client.calls), 1)

    def test_global_job_url_fills_empty_os_links_and_keeps_history(self):
        self.settings['JOB_URL'] = 'http://awx.test/#/templates/job_template/14/details'
        for job in self.connection['OS_JOBS'].values():
            job['url'] = ''
        execution = 'http://awx.test/#/jobs/playbook/99/output'
        self.save_rows(['hostname', 'Job_Link', 'exceiute_job', 'output', 'Survey_installtesttypename'],
                       [['linux-1', 'http://awx.test/#/templates/job_template/8/details', execution, 'previous stdout', 'reinstall'],
                        ['windows-1', 'none', None, None, 'install']])
        self.assertEqual(self.run_main(), 0)
        records = {record['hostname']: record for record in self.read_records()}
        self.assertTrue(all(record['Job_Link'] == self.settings['JOB_URL'] for record in records.values()))
        self.assertEqual(records['linux-1']['exceiute_job'], execution)
        self.assertEqual(records['linux-1']['output'], 'previous stdout')
        self.assertEqual(records['linux-1']['Survey_installtesttypename'], 'reinstall')
        workbook = refresh.load_workbook(self.path)
        try:
            columns = refresh.excel.read_columns(workbook.active)
            for row in range(2, workbook.active.max_row + 1):
                self.assertEqual(workbook.active.cell(row, columns['job_link']).hyperlink.target, self.settings['JOB_URL'])
        finally:
            workbook.close()

    def test_inventory_rows_use_the_same_global_job_url_as_refresh(self):
        refresh.apply_settings(self.connection)
        self.settings['JOB_URL'] = 'http://awx.test/#/templates/job_template/14/details'
        report = refresh.fetch_report(self.client, 1)
        names, rows = refresh.build_inventory_rows(report, self.settings)
        column = names.index('Job_Link')
        self.assertTrue(all(row[column] == self.settings['JOB_URL'] for row in rows))
        self.assertTrue(all(host['Job_Link'] == self.settings['JOB_URL'] for host in report['hosts']))

    def test_empty_extra_config_reads_credentials_from_the_selected_template(self):
        self.connection['EXTRA_CREDENTIAL_VARIABLES'] = []
        self.settings['JOB_URL'] = 'http://awx.test/#/templates/job_template/14/details'
        self.client.template_credentials[14] = [{'id': 31, 'name': 'job-vault', 'credential_type': 2}]
        self.save_rows(['hostname'], [['windows-1'], ['aix-1']])
        self.assertEqual(self.run_main(), 0)
        for record in self.read_records():
            self.assertEqual(record['Extra_Credentials'], 'job-vault')
            self.assertEqual(record['Job_Credentials'], 'job-vault')
            self.assertEqual(record['Full_Credential'], 'job-vault, ' + record['OS Credential'])
        self.assertEqual(sum(call[1] == 'job_templates/14/launch/' for call in self.client.calls), 1)

    def test_configured_extra_credentials_override_template_defaults(self):
        self.connection['EXTRA_CREDENTIAL_VARIABLES'] = ['configured-vault']
        self.client.template_credentials[7] = [{'id': 31, 'name': 'template-vault', 'credential_type': 2}]
        self.save_rows(['hostname'], [['windows-1']])
        self.assertEqual(self.run_main(), 0)
        record = self.read_records()[0]
        self.assertEqual(record['Extra_Credentials'], 'configured-vault')
        self.assertEqual(record['Job_Credentials'], 'template-vault')
        names = list(record)
        self.assertEqual(names.index('Job_Link') + 1, names.index('Job_Credentials'))
        self.assertEqual(record['Full_Credential'], 'configured-vault, windows-key')
        self.assertNotIn('template-vault', record['Full_Credential'])

    def test_template_and_host_credentials_are_not_duplicated_in_full_credential(self):
        self.connection['EXTRA_CREDENTIAL_VARIABLES'] = []
        self.client.template_credentials[7] = [{'id': 31, 'name': 'windows-key', 'credential_type': 1}]
        self.save_rows(['hostname'], [['windows-1']])
        self.assertEqual(self.run_main(), 0)
        record = self.read_records()[0]
        self.assertEqual(record['Extra_Credentials'], 'windows-key')
        self.assertEqual(record['Full_Credential'], 'windows-key')

    def test_configured_and_group_credentials_are_trimmed_and_deduplicated_in_exports(self):
        self.connection['EXTRA_CREDENTIAL_VARIABLES'] = [' windows-key ', 'windows-key', 'windows-key ']
        refresh.apply_settings(self.connection)
        report = refresh.fetch_report(self.client, 1, hostnames=['windows-1'])
        headers, rows = refresh.build_inventory_rows(report, self.settings, client=self.client)
        self.assertEqual(report['hosts'][0]['Full_Credential'], 'windows-key')
        self.assertEqual(rows[0][headers.index('Full_Credential')], 'windows-key')
        self.save_rows(['hostname'], [['windows-1']])
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.read_records()[0]['Full_Credential'], 'windows-key')

    def test_configured_id_and_group_name_of_same_credential_are_not_repeated(self):
        self.connection['EXTRA_CREDENTIAL_VARIABLES'] = ['101', '0101']
        self.save_rows(['hostname'], [['windows-1']])
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.read_records()[0]['Full_Credential'], '101')
        self.assertEqual(sum(call[1] == 'credentials/' for call in self.client.calls), 1)

    def test_credential_names_remain_case_sensitive_and_first_occurrence_wins(self):
        full = refresh.merge_full_credentials(self.client, [' Key ', 'Key'], ['key', ' Key'], [])
        self.assertEqual(full, ['Key', 'key'])
        self.assertEqual(self.client.calls, [])

    def test_host_credential_replaces_inherited_machine_credential_only_in_full(self):
        self.connection['EXTRA_CREDENTIAL_VARIABLES'] = []
        self.client.template_credentials[7] = [{'id': 31, 'name': 'template-machine', 'credential_type': 1},
                                              {'id': 32, 'name': 'job-vault', 'credential_type': 2}]
        self.save_rows(['hostname'], [['windows-1']])
        self.assertEqual(self.run_main(), 0)
        record = self.read_records()[0]
        self.assertEqual(record['Extra_Credentials'], 'template-machine, job-vault')
        self.assertEqual(record['Full_Credential'], 'job-vault, windows-key')
        self.assertEqual(record['OS Credential'], 'windows-key')

    def test_vault_credentials_with_different_ids_are_kept(self):
        self.client.credential_records['host-vault'] = {'id': 55, 'name': 'host-vault', 'credential_type': 2,
                                                        'kind': 'vault', 'inputs': {'vault_id': 'host'}}
        inherited = [('job-vault', {'id': 32, 'name': 'job-vault', 'credential_type': 2})]
        full = refresh.merge_full_credentials(self.client, ['job-vault'], ['host-vault'], inherited)
        self.assertEqual(full, ['job-vault', 'host-vault'])

    def test_explicit_extras_are_not_silently_replaced(self):
        full = refresh.merge_full_credentials(self.client, ['configured-machine'], ['windows-key'], [])
        self.assertEqual(full, ['configured-machine', 'windows-key'])
        self.assertEqual(self.client.calls, [])

    def test_same_template_credential_named_and_referenced_by_id_is_not_repeated(self):
        inherited = [('windows-key', {'id': 101, 'name': 'windows-key', 'credential_type': 1})]
        self.assertEqual(refresh.merge_full_credentials(self.client, ['windows-key'], ['101'], inherited), ['101'])
        self.assertEqual(self.client.calls, [])

    def test_metadata_and_launch_reuse_host_credential_lookup(self):
        inherited = [('template-machine', {'id': 31, 'name': 'template-machine', 'credential_type': 1})]
        for _ in range(2):
            self.assertEqual(refresh.merge_full_credentials(self.client, ['template-machine'], ['windows-key'], inherited), ['windows-key'])
        batch = {'limit': 'windows-1', 'credential_refs': ('windows-key',)}
        refresh.resolve_credentials(self.client, [batch])
        self.assertEqual(batch['credentials'], [101])
        self.assertEqual(sum(call[1] == 'credentials/' for call in self.client.calls), 1)

    def test_unknown_host_credential_keeps_template_defaults_for_later_validation(self):
        inherited = [('template-machine', {'id': 31, 'name': 'template-machine', 'credential_type': 1})]
        full = refresh.merge_full_credentials(self.client, ['template-machine'], ['missing-host-key'], inherited)
        self.assertEqual(full, ['template-machine', 'missing-host-key'])

    def test_template_credentials_are_selected_separately_for_each_os(self):
        self.connection['EXTRA_CREDENTIAL_VARIABLES'] = []
        for template_id, name in ((7, 'windows-vault'), (8, 'linux-vault'), (9, 'aix-vault')):
            self.client.template_credentials[template_id] = [{'id': template_id + 20, 'name': name}]
        self.save_rows(['hostname'], [['aix-1'], ['linux-1'], ['windows-1']])
        self.assertEqual(self.run_main(), 0)
        for record in self.read_records():
            self.assertEqual(record['Extra_Credentials'], record['ostype'] + '-vault')

    def test_foreign_os_template_reports_host_and_configuration_key(self):
        foreign = 'https://other.example/#/templates/job_template/15/details'
        self.connection['OS_JOBS']['windows']['url'] = foreign
        self.save_rows(['hostname'], [['windows-1']])
        before = self.path.read_bytes()
        with self.assertRaises(refresh.AWXError) as result:
            self.run_main()
        message = str(result.exception)
        self.assertIn("Host 'windows-1', ostype='windows'", message)
        self.assertIn("source: OS_JOBS['windows']['url']", message)
        self.assertIn(foreign, message)
        self.assertIn("AWX_URL: 'http://awx.test'", message)
        self.assertEqual(self.path.read_bytes(), before)

    def test_template_credentials_without_a_name_or_with_ambiguous_names_use_ids(self):
        self.client.template_credentials[7] = [{'id': 41}, {'id': 42, 'name': '123'},
                                               {'id': 43, 'name': 'same'}, {'id': 44, 'name': 'same'}]
        values = refresh.template_credential_values(self.client, self.connection['OS_JOBS']['windows']['url'], {})
        self.assertEqual(values, ['41', '42', '43', '44'])

    def test_export_json_and_excel_include_template_credentials(self):
        self.connection['EXTRA_CREDENTIAL_VARIABLES'] = []
        self.client.template_credentials[7] = [{'id': 31, 'name': 'job-vault'}]
        refresh.apply_settings(self.connection)
        report = refresh.fetch_report(self.client, 1, hostnames=['windows-1'])
        cache = {}
        names, rows = refresh.build_inventory_rows(report, self.settings, client=self.client, template_cache=cache)
        self.assertEqual(report['hosts'][0]['Extra_Credentials'], ['job-vault'])
        self.assertEqual(report['hosts'][0]['Job_Credentials'], ['job-vault'])
        refresh.save_inventory_excel(names, rows, self.path, self.settings, client=self.client, template_cache=cache)
        self.assertEqual(self.read_records()[0]['Extra_Credentials'], 'job-vault')
        self.assertEqual(sum(call[1] == 'job_templates/7/launch/' for call in self.client.calls), 1)

    def test_missing_host_column_is_added_without_touching_existing_rows(self):
        self.save_rows(['notes', 'INVENTORY', 'exceiute_job', 'output'], [['existing data', 'original-inventory', 'http://awx.test/#/jobs/playbook/99/output', 'saved log']])
        self.assertEqual(self.run_main(), 0)
        record = self.read_records()[0]
        self.assertIn('hostname', record)
        self.assertIsNone(record['hostname'])
        self.assertEqual(record['notes'], 'existing data')
        self.assertEqual(record['INVENTORY'], 'original-inventory')
        self.assertEqual(record['output'], 'saved log')
        self.assertEqual(record['exceiute_job'], 'http://awx.test/#/jobs/playbook/99/output')
        self.assertEqual(self.client.calls, [])
        self.assertIn('Fill it with hostnames', self.output.getvalue())

    def test_explicit_missing_host_column_reports_typo(self):
        self.save_rows(['hostname'], [['windows-1']])
        before = self.path.read_bytes()
        with self.assertRaisesRegex(refresh.AWXError, 'Cannot find host column'):
            self.run_main('--host-column', 'typo')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.calls, [])

    def test_configured_execution_column_is_recreated_and_preserved(self):
        self.settings['EXECUTION_COLUMN'] = 'launched_job'
        self.save_rows(['hostname'], [['windows-1']])
        self.assertEqual(self.run_main(), 0)
        self.assertIn('launched_job', self.read_records()[0])
        workbook = refresh.load_workbook(self.path)
        sheet = workbook['Results']
        column = next((cell.column for cell in sheet[1] if cell.value == 'launched_job'))
        sheet.cell(2, column).value = 'http://awx.test/#/jobs/playbook/99/output'
        workbook.save(self.path)
        workbook.close()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.read_records()[0]['launched_job'], 'http://awx.test/#/jobs/playbook/99/output')

    def test_header_only_workbook_gets_inventory_column_without_awx_requests(self):
        self.save_rows(['hostname'], [])
        self.assertEqual(self.run_main(), 0)
        workbook = refresh.load_workbook(self.path)
        headers = [cell.value for cell in workbook['Results'][1]]
        self.assertIn('INVENTORY', headers)
        self.assertIn('AWX_Status', headers)
        self.assertIn('exceiute_job', headers)
        self.assertIn('output', headers)
        self.assertEqual(workbook['Results'].max_row, 1)
        workbook.close()
        self.assertEqual(self.client.calls, [])
        self.assertIn('No hostnames', self.output.getvalue())

    def test_completely_empty_workbook_gets_headers_without_awx_requests(self):
        self.save_rows([], [])
        self.assertEqual(self.run_main(), 0)
        workbook = refresh.load_workbook(self.path)
        headers = [cell.value for cell in workbook['Results'][1]]
        self.assertEqual(headers[:2], ['hostname', 'INVENTORY'])
        self.assertEqual(workbook['Notes']['A1'].value, 'Keep this worksheet')
        workbook.close()
        self.assertEqual(self.client.calls, [])

    def test_blank_hostname_clears_stale_metadata_and_preserves_job_history(self):
        self.save_rows(['hostname', 'INVENTORY', 'ostype', 'Job_Link', 'exceiute_job', 'output'], [[None, 'old-inventory', 'linux', 'old-template', 'http://awx.test/#/jobs/playbook/99/output', 'old log']])
        self.assertEqual(self.run_main(), 0)
        record = self.read_records()[0]
        self.assertIsNone(record['INVENTORY'])
        self.assertIsNone(record['ostype'])
        self.assertIsNone(record['Job_Link'])
        self.assertEqual(record['AWX_Status'], 'no_hostname')
        self.assertEqual(record['output'], 'old log')
        self.assertIn('99', record['exceiute_job'])
        self.assertEqual(self.client.calls, [])

    def test_empty_awx_inventory_marks_requested_hosts_not_found(self):
        self.client.hosts = []
        self.save_rows(['hostname', 'INVENTORY'], [['windows-1', 'old-inventory']])
        self.assertEqual(self.run_main(), 0)
        record = self.read_records()[0]
        self.assertEqual(record['INVENTORY'], 'none')
        self.assertEqual(record['AWX_Status'], 'not_found')
        self.assertFalse(any(('/groups/' in call[1] for call in self.client.calls)))

    def test_inventory_header_is_updated_case_insensitively(self):
        self.save_rows(['hostname', 'inventory'], [['windows-1', 'old-inventory']])
        self.assertEqual(self.run_main(), 0)
        record = self.read_records()[0]
        self.assertIn('INVENTORY', record)
        self.assertNotIn('inventory', record)
        self.assertEqual(record['INVENTORY'], 'test_inventory')

    def test_smart_inventory_uses_source_inventory_name(self):
        original_get = self.client.get
        original_items = self.client.items

        def get(path, **kwargs):
            if path == 'inventories/2/':
                self.client.calls.append(('GET', path, kwargs))
                return {'id': 2, 'name': 'source_inventory'}
            if path == 'inventories/2/script/?all=1&hostvars=1':
                return {'_meta': {'hostvars': {'windows-1': {'ostype': 'windows'}}}}
            return original_get(path, **kwargs)

        def items(path, **kwargs):
            return iter([]) if path == 'inventories/2/groups/' else original_items(path, **kwargs)
        self.client.hosts = [{'id': 1, 'name': 'windows-1', 'inventory': 2}]
        self.save_rows(['hostname'], [['windows-1']])
        with patch.object(self.client, 'get', side_effect=get), patch.object(self.client, 'items', side_effect=items):
            self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.read_records()[0]['INVENTORY'], 'source_inventory')
        self.assertEqual(self.read_records()[0]['ApplicationID'], 'source')

    def test_regular_export_has_inventory_column_even_when_inventory_is_empty(self):
        refresh.apply_settings(self.connection)
        self.client.hosts = []
        report = refresh.fetch_report(self.client, 1)
        headers, rows = refresh.build_inventory_rows(report)
        self.assertIn('INVENTORY', headers)
        self.assertEqual(rows, [])
        refresh.save_inventory_excel(headers, rows, self.path)
        workbook = refresh.load_workbook(self.path)
        self.assertIn('INVENTORY', [cell.value for cell in workbook.active[1]])
        self.assertEqual(workbook.active.max_row, 1)
        workbook.close()

    def test_legacy_fetch_report_still_rejects_all_missing_hosts(self):
        refresh.apply_settings(self.connection)
        with self.assertRaisesRegex(refresh.AWXError, 'None of the hosts'):
            refresh.fetch_report(self.client, 1, hostnames=['not-found'])

    def test_hops_refreshes_from_direct_and_parent_groups_without_changing_history(self):
        hop_groups = [
            {'id': 71, 'name': 'it2_grp_cred_unix_3hop'},
            {'id': 72, 'name': 'network_12_HOP_remote'},
            {'id': 73, 'name': 'local_0_hop'},
            {'id': 74, 'name': 'unrelated_99_hop'},
            {'id': 75, 'name': 'shop'},
        ]
        original_get, original_items = self.client.get, self.client.items

        def get(path, **kwargs):
            data = original_get(path, **kwargs)
            if path == 'inventories/1/script/?all=1&hostvars=1':
                data.update({
                    'it2_grp_cred_unix_3hop': {'children': ['windows-group']},
                    'network_12_HOP_remote': {'hosts': ['windows-1'], 'children': ['windows-group']},
                    'local_0_hop': {'hosts': ['windows-1']},
                    'unrelated_99_hop': {'hosts': ['other-host']},
                    'shop': {'hosts': ['windows-1']},
                })
            return data

        def items(path, **kwargs):
            records = original_items(path, **kwargs)
            return iter([*records, *hop_groups]) if path == 'inventories/1/groups/' else records

        history = 'http://awx.test/#/jobs/playbook/99/output'
        self.save_rows(['hostname', 'HOPS', 'exceiute_job', 'output'],
                       [['windows-1', 'stale', history, 'saved log'], ['linux-1', 'stale', None, None]])
        with patch.object(self.client, 'get', side_effect=get), patch.object(self.client, 'items', side_effect=items):
            self.assertEqual(self.run_main(), 0)
        records = self.read_records()
        self.assertEqual(records[0]['HOPS'], 'it2_grp_cred_unix_3hop (HOPS: 3)\nlocal_0_hop (HOPS: 0)\nnetwork_12_HOP_remote (HOPS: 12)')
        self.assertEqual(records[1]['HOPS'], 'none')
        self.assertIsNone(records[0]['HOPS_COUNT'])
        self.assertIsNone(records[1]['HOPS_COUNT'])
        names = list(records[0])
        self.assertEqual(names.index('HOPS') + 1, names.index('HOPS_COUNT'))
        self.assertEqual(names.index('HOPS_COUNT') + 1, names.index('jumphost_for_hops'))
        self.assertEqual(names.index('jumphost_for_hops') + 1, names.index('Extra_Credentials'))
        self.assertEqual(records[0]['exceiute_job'], history)
        self.assertEqual(records[0]['output'], 'saved log')

    def test_hops_export_is_shared_by_json_and_excel(self):
        refresh.apply_settings(self.connection)
        report = refresh.fetch_report(self.client, 1, hostnames=['windows-1'])
        host = report['hosts'][0]
        host['all_groups'].append({'id': 71, 'name': 'remote_2_hop_linux'})
        names, rows = refresh.build_inventory_rows(report, self.settings)
        self.assertEqual(host['HOPS'], 'remote_2_hop_linux (HOPS: 2)')
        self.assertEqual(rows[0][names.index('HOPS')], host['HOPS'])
        self.assertEqual(host['HOPS_COUNT'], 2)
        self.assertIs(type(rows[0][names.index('HOPS_COUNT')]), int)
        self.assertEqual(host['ApplicationID'], 'test')
        self.assertEqual(rows[0][names.index('ApplicationID')], host['ApplicationID'])
        self.assertEqual(names.index('INVENTORY') + 1, names.index('ApplicationID'))
        self.assertEqual(names.index('HOPS') + 1, names.index('HOPS_COUNT'))
        self.assertEqual(names.index('HOPS_COUNT') + 1, names.index('jumphost_for_hops'))
        self.assertEqual(names.index('jumphost_for_hops') + 1, names.index('Extra_Credentials'))
        refresh.save_inventory_excel(names, rows, self.path, settings=self.settings)
        book = refresh.load_workbook(self.path)
        try:
            cell = book.active.cell(2, refresh.excel.read_columns(book.active)['hops_count'])
            self.assertEqual(cell.value, 2)
            self.assertEqual(cell.data_type, 'n')
            self.assertEqual(cell.number_format, '0')
        finally:
            book.close()

    def test_refresh_preserves_zero_hop_count_as_a_number(self):
        refresh.apply_settings(self.connection)
        report = refresh.fetch_report(self.client, 1, hostnames=['windows-1'])
        report['hosts'][0]['all_groups'].extend([
            {'id': 71, 'name': 'local_0_hop'}, {'id': 72, 'name': 'another_0hop'},
        ])
        metadata, headers = refresh.metadata_by_host(report, self.settings)
        self.save_rows(['hostname', 'HOPS_COUNT'], [['windows-1', 'stale']])
        book = refresh.load_workbook(self.path)
        try:
            sheet = book['Results']
            columns = refresh.excel.read_columns(sheet)
            refresh.refresh_sheet(sheet, {2: 'windows-1'}, columns, columns['hostname'], metadata, headers)
            refresh.excel.save_workbook(book, self.path)
        finally:
            book.close()
        self.assertIs(type(self.read_records()[0]['HOPS_COUNT']), int)
        self.assertEqual(self.read_records()[0]['HOPS_COUNT'], 0)

if __name__ == '__main__':
    unittest.main()
