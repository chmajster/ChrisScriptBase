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

class FakeAWX:

    def __init__(self):
        self.calls = []
        self.hosts = [{'id': 1, 'name': 'windows-1'}, {'id': 2, 'name': 'linux-1'}, {'id': 3, 'name': 'aix-1'}, {'id': 4, 'name': 'unrequested-host'}]

    def get(self, path, **kwargs):
        self.calls.append(('GET', path, kwargs))
        if path == 'inventories/1/':
            return {'id': 1, 'name': 'test_inventory'}
        if path == 'inventories/1/script/?all=1&hostvars=1':
            return {'_meta': {'hostvars': {'windows-1': {'ostype': 'windows'}, 'linux-1': {'ostype': 'linux'}, 'aix-1': {'ostype': 'aix'}}}, 'windows-group': {'vars': {'os_credential': 'windows-key'}, 'hosts': ['windows-1']}, 'linux-group': {'vars': {'os_credential': 'linux-key'}, 'hosts': ['linux-1']}, 'linux-other': {'vars': {'os_credential': 'linux-second-key'}, 'hosts': ['linux-1']}, 'aix-group': {'vars': {'os_credential': 'aix-key'}, 'hosts': ['aix-1']}}
        raise AssertionError(path)

    def items(self, path, **kwargs):
        self.calls.append(('ITEMS', path, kwargs))
        if path == 'inventories/1/hosts/':
            return iter(self.hosts)
        if path == 'inventories/1/groups/':
            return iter(({'id': index, 'name': name} for index, name in enumerate(('windows-group', 'linux-group', 'linux-other', 'aix-group'), start=11)))
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
        self.settings.update(JOB_TYPE='install', JOB_TYPE_VARIABLE='operation')
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
            self.assertEqual(record['Full_Credential'].split(', ')[0], 'shared-vault')
            self.assertEqual(record['Job_Type'], 'install')
            self.assertEqual(json.loads(record['Job_Variables']), {'source': os, 'operation': 'install'})
            self.assertEqual(record['output'], f"log for {name}")
            self.assertEqual(record['notes'], f"note for {name}")
            self.assertEqual(record['AWX_Status'], 'found')
        self.assertEqual(records[1]['Credential'], 'linux-key, linux-second-key')
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
        expected = {'hostname', 'INVENTORY', 'ostype', 'Job_Link', 'Job_Variables', 'Extra_Credentials', 'Credential', 'Full_Credential', 'Job_Type', 'AWX_Status', 'exceiute_job', 'output'}
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
        to_delete = {'INVENTORY', 'Job_Link', 'Full_Credential', 'Credential', 'Job_Type'}
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
if __name__ == '__main__':
    unittest.main()
