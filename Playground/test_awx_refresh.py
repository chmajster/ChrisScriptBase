"""Refresh discovery and workbook regressions using an offline AWX fixture."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import Workbook, load_workbook

with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
    from lib import awx


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'hosts.xlsx'
        self.config_path = Path(self.directory.name) / 'variables'
        self.settings = {
            'AWX_URL': 'https://awx.example.test', 'AWX_TOKEN': 'test-token',
            'INVENTORY': None, 'CREDENTIAL_VARIABLES': ['os_credential'],
            'EXTRA_CREDENTIAL_VARIABLES': [], 'FULL_CREDENTIAL_SEPARATOR': ', ',
            'OUTPUT_FILE': 'unused.json', 'EXCEL_OUTPUT_FILE': str(self.path),
            'TIMEOUT': 30, 'PAGE_SIZE': 200,
            'OS_JOBS': {os: {'url': f'https://awx.example.test/#/templates/job_template/{number}/details',
                            'variables': {'action': 'install'}}
                        for os, number in [('linux', 7), ('windows', 8)]},
            'INPUT_FILE': None, 'SHEET_NAME': 'Results', 'BATCH_SIZE': 10,
            'EXECUTION_COLUMN': 'exceiute_job', 'JOB_TYPE': '', 'JOB_TYPE_VARIABLE': 'action',
            'WAIT_FOR_COMPLETION': False, 'POLL_INTERVAL': 1, 'JOB_WAIT_TIMEOUT': 30,
            'HOSTS': [], 'JOB_URL': '', 'SURVEY_ANSWERS': {}, 'SURVEY_INTERACTIVE': False,
            'SURVEY_EXCEL_DEFAULTS': {}, 'EXCEL_COLUMNS': [],
            'REFRESH_INVENTORY_PATTERN': '*_mirror_inventory',
            'OUTPUT_COLUMN': 'output', 'OUTPUT_DIRECTORY': 'output',
            'DEBUG': False, 'DRY_RUN': False, 'HOST_COLUMN': None, 'COLOR': 'never',
        }
        overrides = patch.multiple(awx, **{key: self.settings[key] for key in awx.common.INVENTORY_KEYS})
        overrides.start()
        self.addCleanup(overrides.stop)
        self.inventories = {10: 'app1_mirror_inventory', 20: 'B42_mirror_inventory',
                            30: 'app1_inventory', 40: 'app1_mirror_inventory_backup'}
        self.candidates = [self.host(1, 'Linux1', 10), self.host(2, 'win1', 20),
                           self.host(3, 'Linux2', 10), self.host(4, 'Linux1', 30),
                           self.host(5, 'prefixLinux1', 10), self.host(6, 'Linux1', 40),
                           self.host(7, 'other-only', 30)]
        self.credentials = [
            {'id': 900, 'name': 'template-machine', 'kind': 'ssh', 'credential_type': 11},
            {'id': 901, 'name': 'shared-vault', 'kind': 'vault', 'credential_type': 22},
        ]
        for number, name in self.inventories.items():
            app = name.split('_', 1)[0]
            if number in (10, 20):
                self.credentials.extend([
                    {'id': number + 100, 'name': f'{app}-os', 'kind': 'ssh', 'credential_type': 11},
                    {'id': number + 200, 'name': f'{app}_1hop', 'kind': 'cloud', 'credential_type': 33},
                ])
        self.client = awx.AWXClient(self.settings['AWX_URL'], self.settings['AWX_TOKEN'])
        self.client.items = Mock(side_effect=self.items)
        self.client.get = Mock(side_effect=self.get)

    def host(self, number, name, inventory):
        return {'id': number, 'name': name, 'inventory': inventory,
                'summary_fields': {'inventory': {'name': self.inventories[inventory]}}}

    def items(self, path, **filters):
        if path == 'hosts/':
            # Deliberately return extra results to check local exact-name/pattern validation.
            return list(self.candidates)
        if path == 'credentials/':
            return [credential for credential in self.credentials
                    if (not filters.get('name') or credential['name'] == filters['name'])
                    and (not filters.get('name__icontains')
                         or filters['name__icontains'].casefold() in credential['name'].casefold())]
        if path.endswith('/groups/'):
            number = int(path.split('/')[1])
            self.assertIn(number, (10, 20))
            return [{'id': number * 10, 'name': '1_hop'},
                    {'id': number * 10 + 1, 'name': 'credential_parent'}]
        if path.endswith('/hosts/'):
            number = int(path.split('/')[1])
            return [host for host in self.candidates if host['inventory'] == number]
        raise AssertionError(f'Unexpected AWX list request: {path}')

    def get(self, path, **options):
        if path.endswith('/script/?all=1&hostvars=1'):
            number = int(path.split('/')[1])
            self.assertIn(number, (10, 20))
            names = [host['name'] for host in self.candidates if host['inventory'] == number]
            os = 'linux' if number == 10 else 'windows'
            app = self.inventories[number].split('_', 1)[0]
            return {'_meta': {'hostvars': {name: {'ostype': os} for name in names}},
                    '1_hop': {'hosts': names},
                    'credential_parent': {'children': ['1_hop'], 'vars': {'os_credential': f'{app}-os'}}}
        if path in ('job_templates/7/launch/', 'job_templates/8/launch/'):
            return {'defaults': {'credentials': self.credentials[:2]}, 'survey_enabled': True}
        if path.endswith('/survey_spec/'):
            return {'spec': [{'variable': 'mode', 'type': 'multiplechoice',
                              'choices': 'install\nupdate', 'default': 'install', 'required': False}]}
        if path.startswith('inventories/'):
            number = int(path.split('/')[1])
            return {'id': number, 'name': self.inventories[number]}
        raise AssertionError(f'Unexpected AWX detail request: {path}')

    def write_config(self, **overrides):
        values = {**self.settings, **overrides}
        self.config_path.write_text(''.join(f'{key} = {value!r}\n' for key, value in values.items()), encoding='utf-8')

    def write_workbook(self, names):
        book = Workbook()
        sheet = book.active
        sheet.title = 'Results'
        sheet.append(['hostname', 'INVENTORY', 'ApplicationID', 'exceiute_job', 'output',
                      'Artifacts', 'artifact filter', 'Uwagi'])
        for name in names:
            sheet.append([name, 'stale_inventory', 'stale-app',
                          'https://awx.example.test/#/jobs/playbook/99/output',
                          'previous output', '{"old": true}', 'previous message', 'keep note'])
        book.create_sheet('Notes')['A1'] = 'keep other sheet'
        book.save(self.path)
        book.close()

    def refresh(self, variables=None):
        captured = io.StringIO()
        with patch.object(awx, 'AWXClient', return_value=self.client) as factory:
            with redirect_stdout(captured), redirect_stderr(captured):
                code = awx.run('refresh', ['--file', str(self.path)], variables=variables,
                               variables_file=self.config_path)
        return code, captured.getvalue(), factory

    def test_dynamic_inventories_exact_names_order_and_source_cache(self):
        report = awx.fetch_refresh_report(self.client, ['WIN1', 'linux1', 'Linux2', 'missing', 'LINUX1'],
                                          '*_mirror_inventory')
        self.assertEqual([host['name'] for host in report['hosts']], ['win1', 'Linux1', 'Linux2'])
        self.assertEqual([host['INVENTORY'] for host in report['hosts']],
                         ['B42_mirror_inventory', 'app1_mirror_inventory', 'app1_mirror_inventory'])
        self.assertEqual(report['missing_hosts'], ['missing'])
        self.assertEqual(report['hosts'][1]['credential_groups'][0]['value'], 'app1-os')
        self.assertTrue(report['hosts'][1]['credential_groups'][0]['inherited'])
        for number in (10, 20):
            self.assertEqual(sum(call.args[0] == f'inventories/{number}/groups/'
                                 for call in self.client.items.call_args_list), 1)
            self.assertEqual(sum(call.args[0] == f'inventories/{number}/script/?all=1&hostvars=1'
                                 for call in self.client.get.call_args_list), 1)
        filters = self.client.items.call_args_list[0].kwargs
        self.assertIn('inventory__name__iregex', filters)
        self.assertIn('name__iregex', filters)

    def test_editable_pattern_supports_case_question_mark_and_literal_dot(self):
        for pattern, expected in [('APP?_MIRROR_INVENTORY', ['Linux1']),
                                  ('B42_*', ['win1']), ('app1.mirror_inventory', [])]:
            with self.subTest(pattern=pattern):
                report = awx.fetch_refresh_report(self.client, ['Linux1', 'win1'], pattern)
                self.assertEqual([host['name'] for host in report['hosts']], expected)

    def test_empty_host_list_does_not_query_awx(self):
        report = awx.fetch_refresh_report(self.client, [], '*_mirror_inventory')
        self.assertEqual(report, {'hosts': [], 'host_count': 0, 'missing_hosts': []})
        self.client.items.assert_not_called()
        self.client.get.assert_not_called()

    def test_missing_summary_fetches_inventory_name_once(self):
        for host in self.candidates:
            host.pop('summary_fields')
        awx.fetch_refresh_report(self.client, ['Linux1', 'Linux2'], '*_mirror_inventory')
        self.assertEqual(sum(call.args[0] == 'inventories/10/'
                             for call in self.client.get.call_args_list), 1)

    def test_inventory_export_keeps_selected_inventory_scope(self):
        report = awx.fetch_report(self.client, 10, ['LINUX1'])
        self.assertEqual(report['inventory'], {'id': 10, 'name': 'app1_mirror_inventory'})
        self.assertEqual([host['name'] for host in report['hosts']], ['Linux1'])
        self.assertEqual(self.client.items.call_args_list[0].args[0], 'inventories/10/hosts/')

    def test_refresh_populates_all_metadata_and_preserves_history(self):
        self.write_config()
        self.write_workbook(['linux1', 'WIN1', 'missing', 'LINUX1'])
        code, output, _ = self.refresh()
        self.assertEqual(code, 0, output)
        book = load_workbook(self.path)
        self.addCleanup(book.close)
        sheet = book['Results']
        columns = awx.excel.read_columns(sheet)
        rows = [dict(zip(columns, [sheet.cell(number, column).value for column in columns.values()]))
                for number in range(2, sheet.max_row + 1)]
        self.assertEqual([row['hostname'] for row in rows], ['win1', 'Linux1', 'Linux1', 'missing'])
        for row in rows:
            self.assertEqual(row['output'], 'previous output')
            self.assertEqual(row['artifacts'], '{"old": true}')
            self.assertEqual(row['artifact filter'], 'previous message')
            self.assertEqual(row['uwagi'], 'keep note')
            self.assertTrue(row['exceiute_job'].endswith('/99/output'))
            if row['hostname'] == 'missing':
                self.assertEqual(row['awx_status'], 'not_found')
                continue
            app = 'B42' if row['hostname'] == 'win1' else 'app1'
            self.assertEqual(row['inventory'], f'{app}_mirror_inventory')
            self.assertEqual(row['applicationid'], app)
            self.assertEqual(row['ostype'], 'windows' if app == 'B42' else 'linux')
            self.assertEqual(row['hops_count'], 1)
            self.assertEqual(row['os credential'], f'{app}-os')
            self.assertEqual(row['full_credential'], f'shared-vault, {app}-os, {app}_1hop')
            self.assertEqual(row['jumphost_for_hops'], f'{app}_1hop')
            self.assertEqual(row['job_credentials'], 'template-machine, shared-vault')
            self.assertEqual(row['extra_credentials'], 'template-machine, shared-vault')
            self.assertEqual(row['survey_mode'], 'install')
            self.assertEqual(row['awx_status'], 'found')
            self.assertIn('job_template/', row['job_link'])
            self.assertEqual(row['job_variables'], '{"action": "install"}')
        self.assertEqual(book['Notes']['A1'].value, 'keep other sheet')

    def test_pattern_override_restricts_discovery(self):
        self.write_config(REFRESH_OVERRIDES={'REFRESH_INVENTORY_PATTERN': 'app1_*'})
        self.write_workbook(['WIN1'])
        code, output, _ = self.refresh({'REFRESH_INVENTORY_PATTERN': 'B42_mirror_inventory'})
        self.assertEqual(code, 0, output)
        book = load_workbook(self.path)
        self.addCleanup(book.close)
        columns = awx.excel.read_columns(book.active)
        self.assertEqual(book.active.cell(2, columns['inventory']).value, 'B42_mirror_inventory')
        self.assertEqual(book.active.cell(2, columns['awx_status']).value, 'found')

    def test_ambiguous_host_aborts_without_saving(self):
        self.candidates.append(self.host(99, 'linux1', 20))
        self.write_config()
        self.write_workbook(['Linux1'])
        original = self.path.read_bytes()
        code, output, _ = self.refresh()
        self.assertEqual(code, 1, output)
        self.assertIn('app1_mirror_inventory', output)
        self.assertIn('B42_mirror_inventory', output)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(any('/groups/' in call.args[0] for call in self.client.items.call_args_list))

    def test_empty_workbook_repairs_schema_without_awx(self):
        self.write_config()
        self.write_workbook([])
        code, output, factory = self.refresh()
        self.assertEqual(code, 0, output)
        factory.assert_not_called()
        book = load_workbook(self.path)
        self.addCleanup(book.close)
        columns = awx.excel.read_columns(book.active)
        self.assertTrue(all(name.casefold() in columns for name in awx.excel.METADATA_COLUMNS))


if __name__ == '__main__':
    unittest.main()
