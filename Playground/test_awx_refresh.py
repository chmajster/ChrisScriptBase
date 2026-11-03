"""Refresh discovery and workbook regressions using an offline AWX fixture."""

import io
import json
import re
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
        self.ostypes = {10: 'linux', 20: 'windows'}
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
        self.client.post = Mock(side_effect=AssertionError('Unexpected launch request'))
        self.organizations = [{'id': 13, 'name': 'Operations'}, {'id': 14, 'name': 'app1'},
                              {'id': 15, 'name': 'B42'}, {'id': 16, 'name': 'aixapp'},
                              {'id': 17, 'name': '123'}]
        self.organization_templates = {}
        self.job_templates = [
            {'id': 7, 'name': 'Zabbix IT2 Unix install', 'extra_vars': '{"from_awx": true, "action": "update"}'},
            {'id': 8, 'name': 'Zabbix IT2 Window install', 'extra_vars': '{"from_awx": true}'},
            {'id': 9, 'name': 'Unrelated template', 'extra_vars': '{}'},
        ]

    def host(self, number, name, inventory):
        return {'id': number, 'name': name, 'inventory': inventory,
                'summary_fields': {'inventory': {'name': self.inventories[inventory]}}}

    def items(self, path, **filters):
        if path == 'organizations/':
            return list(self.organizations)
        if re.fullmatch(r'organizations/[0-9]+/job_templates/', path):
            number = int(path.split('/')[1])
            selected = self.organization_templates.get(number)
            return [item for item in self.job_templates if selected is None or item['id'] in selected]
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
            self.assertIn(number, (10, 20, 50))
            return [{'id': number * 10, 'name': '1_hop'},
                    {'id': number * 10 + 1, 'name': 'credential_parent'}]
        if path.endswith('/hosts/'):
            number = int(path.split('/')[1])
            return [host for host in self.candidates if host['inventory'] == number]
        raise AssertionError(f'Unexpected AWX list request: {path}')

    def get(self, path, **options):
        if path.startswith('organizations/'):
            number = int(path.split('/')[1])
            return next(item for item in self.organizations if item['id'] == number)
        if re.fullmatch(r'job_templates/[0-9]+/', path):
            number = int(path.split('/')[1])
            return next(item for item in self.job_templates if item['id'] == number)
        if path.endswith('/script/?all=1&hostvars=1'):
            number = int(path.split('/')[1])
            self.assertIn(number, (10, 20, 50))
            names = [host['name'] for host in self.candidates if host['inventory'] == number]
            os = self.ostypes[number]
            app = self.inventories[number].split('_', 1)[0]
            return {'_meta': {'hostvars': {name: {'ostype': os} for name in names}},
                    '1_hop': {'hosts': names},
                    'credential_parent': {'children': ['1_hop'], 'vars': {'os_credential': f'{app}-os'}}}
        if path in ('job_templates/7/launch/', 'job_templates/8/launch/', 'job_templates/10/launch/', 'job_templates/27/launch/'):
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
        self.write_workbook([' ,linux1,, ', ' WIN1, ', 'missing,', 'LINUX1'])
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
            self.assertIn('/job_templates/', row['job_link'])
            self.assertEqual(row['template_job_name'], 'Zabbix IT2 Window install' if app == 'B42'
                             else 'Zabbix IT2 Unix install')
            template_id = 8 if app == 'B42' else 7
            self.assertEqual(json.loads(row['job_variables']),
                             json.loads(self.job_templates[template_id - 7]['extra_vars']))
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

    def test_auto_template_refresh_uses_window_and_writes_name_link_and_defaults(self):
        self.write_config(JOB_TEMPLATE_AUTO_DISCOVERY=True, JOB_TEMPLATE_ORGANIZATION='Operations',
                          JOB_URL='https://awx.example.test/api/v2/job_templates/999/')
        self.write_workbook(['linux1,', 'Linux2', 'WIN1,'])
        code, output, _ = self.refresh()
        self.assertEqual(code, 0, output)
        book = load_workbook(self.path)
        self.addCleanup(book.close)
        columns = awx.excel.read_columns(book.active)
        self.assertLess(columns['template_job_name'], columns['job_link'])
        for number in range(2, book.active.max_row + 1):
            os = book.active.cell(number, columns['ostype']).value
            template_id = 8 if os == 'windows' else 7
            self.assertEqual(book.active.cell(number, columns['template_job_name']).value,
                             self.job_templates[template_id - 7]['name'])
            self.assertEqual(book.active.cell(number, columns['job_link']).value,
                             f'https://awx.example.test/api/v2/job_templates/{template_id}/')
            variables = json.loads(book.active.cell(number, columns['job_variables']).value)
            self.assertTrue(variables['from_awx'])
            self.assertEqual(variables, json.loads(self.job_templates[template_id - 7]['extra_vars']))
            self.assertEqual(book.active.cell(number, columns['output']).value, 'previous output')
            self.assertEqual(book.active.cell(number, columns['survey_mode']).value, 'install')
        searches = [call for call in self.client.items.call_args_list
                    if re.fullmatch(r'organizations/[0-9]+/job_templates/', call.args[0])]
        self.assertEqual(len(searches), 2)
        self.assertEqual({call.args[0] for call in searches},
                         {'organizations/14/job_templates/', 'organizations/15/job_templates/'})
        self.assertTrue(any('window' in call.kwargs['name__iregex'] for call in searches))
        self.assertFalse(any('windows' in call.kwargs['name__iregex'] for call in searches))
        self.assertFalse(any('999' in call.args[0] for call in self.client.get.call_args_list))
        self.client.post.assert_not_called()

    def test_auto_template_missing_or_ambiguous_does_not_save(self):
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                if ambiguous:
                    self.job_templates.append({'id': 17, 'name': 'Zabbix IT2 Unix install other'})
                else:
                    self.job_templates = [item for item in self.job_templates if item['id'] != 7]
                self.client.template_search_cache.clear()
                self.write_config(JOB_TEMPLATE_AUTO_DISCOVERY=True, JOB_TEMPLATE_ORGANIZATION=13)
                self.write_workbook(['Linux1'])
                original = self.path.read_bytes()
                code, output, _ = self.refresh()
                self.assertEqual(code, 1, output)
                self.assertIn('no job template' if not ambiguous else 'ambiguous job templates', output)
                self.assertEqual(self.path.read_bytes(), original)
                if not ambiguous:
                    self.job_templates.insert(0, {'id': 7, 'name': 'Zabbix IT2 Unix install'})

    def test_template_console_only_displays_matching_jobs(self):
        self.write_config()
        before = set(Path(self.directory.name).iterdir())
        captured = io.StringIO()
        with patch.object(awx, 'AWXClient', return_value=self.client):
            with redirect_stdout(captured), redirect_stderr(captured):
                code = awx.run('templates', ['--organization', 'operations'], variables_file=self.config_path)
        self.assertEqual(code, 0, captured.getvalue())
        self.assertIn('Zabbix IT2 Unix install', captured.getvalue())
        self.assertIn('Zabbix IT2 Window install', captured.getvalue())
        self.assertNotIn('Unrelated template', captured.getvalue())
        self.assertEqual(set(Path(self.directory.name).iterdir()), before)
        self.client.get.assert_not_called()
        self.client.post.assert_not_called()

    def test_template_console_cli_overrides_scoped_pattern_and_organization(self):
        self.write_config(TEMPLATES_OVERRIDES={'JOB_TEMPLATE_NAME_PATTERN': '*unix*',
                                              'JOB_TEMPLATE_ORGANIZATION': 'wrong-organization'})
        captured = io.StringIO()
        with patch.object(awx, 'AWXClient', return_value=self.client):
            with redirect_stdout(captured), redirect_stderr(captured):
                code = awx.run('templates', ['--organization', '13', '--name-pattern', '*window*'],
                               variables_file=self.config_path)
        self.assertEqual(code, 0, captured.getvalue())
        self.assertIn('Zabbix IT2 Window install', captured.getvalue())
        self.assertNotIn('Zabbix IT2 Unix install', captured.getvalue())

    def test_template_console_can_derive_window_from_hostname(self):
        self.write_config(JOB_TEMPLATE_ORGANIZATION='Operations')
        captured = io.StringIO()
        with patch.object(awx, 'AWXClient', return_value=self.client):
            with redirect_stdout(captured), redirect_stderr(captured):
                code = awx.run('templates', ['--hostname', 'WIN1'], variables_file=self.config_path)
        self.assertEqual(code, 0, captured.getvalue())
        self.assertIn('Zabbix IT2 Window install', captured.getvalue())
        self.assertNotIn('Zabbix IT2 Unix install', captured.getvalue())
        self.client.post.assert_not_called()

    def test_template_placeholders_are_literal_and_reject_unknown_values(self):
        settings = awx.common.normalize_template_settings({
            'JOB_TEMPLATE_NAME_PATTERN': '*{hostname}*{application_id}*{os_name}*',
        })
        expression, = awx.templates.name_regexes(settings, {'name': 'win.1', 'INVENTORY': 'app+1_mirror_inventory',
                                                          'ostype': 'windows'})
        self.assertIsNotNone(re.fullmatch(expression, 'job win.1 app+1 window'))
        self.assertIsNone(re.fullmatch(expression, 'job winX1 app+1 window'))
        settings['JOB_TEMPLATE_NAME_PATTERN'] = '*{unsupported}*'
        with self.assertRaisesRegex(awx.AWXError, 'supports only'):
            awx.templates.name_regexes(settings)
        settings['JOB_TEMPLATE_NAME_PATTERN'] = '*{os_name}*'
        with self.assertRaisesRegex(awx.AWXError, 'Cannot expand'):
            awx.templates.name_regexes(settings, {'name': 'server1', 'ostype': 'unknown'})

    def test_missing_application_organization_does_not_save(self):
        self.organizations = [item for item in self.organizations if item['name'] != 'app1']
        self.write_config()
        self.write_workbook(['Linux1'])
        original = self.path.read_bytes()
        code, output, factory = self.refresh()
        self.assertEqual(code, 1, output)
        self.assertIn("No accessible organization named 'app1'", output)
        factory.assert_called_once()
        self.assertEqual(self.path.read_bytes(), original)

    def test_refresh_ignores_manual_job_settings_and_uses_awx_variables(self):
        template_values = {
            'action': 'from-template', 'server_config': {'port': 10050, 'enabled': True},
            'packages': ['agent', 'extra'], 'label': 'Łódź',
        }
        self.job_templates[0]['extra_vars'] = json.dumps(template_values, ensure_ascii=False)
        self.write_config(JOB_TEMPLATE_AUTO_DISCOVERY=False, JOB_TEMPLATE_ORGANIZATION={'ignored': True},
                          JOB_URL={'ignored': True}, JOB_TYPE=123,
                          OS_JOBS={'invalid-manual-job': {'variables': {'package': 'custom'}}})
        self.write_workbook(['Linux1'])
        book = load_workbook(self.path)
        book.active.cell(1, book.active.max_column + 1).value = 'Job_Variables'
        book.active.cell(2, book.active.max_column).value = '{"obsolete": true}'
        book.save(self.path)
        book.close()
        code, output, _ = self.refresh()
        self.assertEqual(code, 0, output)
        book = load_workbook(self.path)
        self.addCleanup(book.close)
        columns = awx.excel.read_columns(book.active)
        variables = json.loads(book.active.cell(2, columns['job_variables']).value)
        self.assertEqual(variables, template_values)
        self.assertEqual(book.active.cell(2, columns['template_job_name']).value, self.job_templates[0]['name'])
        self.assertEqual(book.active.cell(2, columns['output']).value, 'previous output')
        self.assertEqual(sum(call.args[0] == 'job_templates/7/'
                             for call in self.client.get.call_args_list), 1)

    def test_refresh_scopes_identical_template_names_to_each_application(self):
        self.ostypes[20] = 'linux'
        self.job_templates.append({'id': 27, 'name': self.job_templates[0]['name'],
                                   'extra_vars': '{"application": "B42"}'})
        self.organization_templates.update({14: [7], 15: [27]})
        self.write_config(JOB_TEMPLATE_AUTO_DISCOVERY=False, JOB_TEMPLATE_ORGANIZATION='Operations')
        self.write_workbook(['Linux1', 'WIN1'])
        code, output, _ = self.refresh()
        self.assertEqual(code, 0, output)
        book = load_workbook(self.path)
        self.addCleanup(book.close)
        columns = awx.excel.read_columns(book.active)
        for number in range(2, book.active.max_row + 1):
            app = book.active.cell(number, columns['applicationid']).value
            template_id = 7 if app == 'app1' else 27
            self.assertEqual(book.active.cell(number, columns['job_link']).value,
                             f'https://awx.example.test/api/v2/job_templates/{template_id}/')
            self.assertEqual(book.active.cell(number, columns['template_job_name']).value,
                             self.job_templates[0]['name'])
            self.assertIn(f'Organizacja (ApplicationID): {app}', output)
        self.assertFalse(any(call.args[0] == 'organizations/13/job_templates/'
                             for call in self.client.items.call_args_list))
        self.client.post.assert_not_called()

    def test_numeric_application_id_is_an_organization_name(self):
        settings = awx.common.normalize_template_settings({'JOB_TEMPLATE_ORGANIZATION': 'Operations'})
        self.organization_templates[17] = [7]
        selected = awx.templates.template_for_host(self.client, settings,
            {'name': 'server123', 'INVENTORY': '123_mirror_inventory', 'ApplicationID': '123', 'ostype': 'linux'})
        self.assertEqual(selected['id'], 7)
        self.assertIn(('name', '123'), self.client.organization_cache)
        self.assertTrue(any(call.args[0] == 'organizations/17/job_templates/'
                            for call in self.client.items.call_args_list))
        self.assertFalse(any(call.args[0] == 'organizations/123/' for call in self.client.get.call_args_list))

    def test_refresh_without_manual_template_configuration(self):
        removed = {'OS_JOBS', 'JOB_URL', 'JOB_TEMPLATE_AUTO_DISCOVERY', 'JOB_TEMPLATE_ORGANIZATION'}
        values = {key: value for key, value in self.settings.items() if key not in removed}
        self.config_path.write_text(''.join(f'{key} = {value!r}\n' for key, value in values.items()), encoding='utf-8')
        self.write_workbook(['Linux1'])
        code, output, _ = self.refresh()
        self.assertEqual(code, 0, output)
        self.assertIn('Organizacja (ApplicationID): app1', output)
        self.assertIn(self.job_templates[0]['name'], output)

    def test_template_words_match_in_any_order(self):
        for pattern in ('*zabbix*install*windows', 'zabbix install windows'):
            with self.subTest(pattern=pattern):
                settings = awx.common.normalize_template_settings({'JOB_TEMPLATE_NAME_PATTERN': pattern})
                expression, = awx.templates.name_regexes(settings)
                for name in ('Zabbix Windows Agent Install', 'Install Zabbix Agent Windows',
                             'WINDOWS Agent INSTALL for ZABBIX servers'):
                    self.assertIsNotNone(re.fullmatch(expression, name, re.IGNORECASE), name)
                for name in ('Zabbix Install Linux', 'Zabbix Windows', 'Install Windows'):
                    self.assertIsNone(re.fullmatch(expression, name, re.IGNORECASE), name)

    def test_template_search_checks_every_word_after_api_prefilter(self):
        self.job_templates[1]['name'] = 'Zabbix Windows Agent Install'
        self.job_templates.append({'id': 17, 'name': 'Windows install other agent'})
        settings = awx.common.normalize_template_settings({
            'JOB_TEMPLATE_ORGANIZATION': 'Operations',
            'JOB_TEMPLATE_NAME_PATTERN': '*zabbix*install*windows',
        })
        _, matches = awx.templates.find_templates(self.client, settings)
        self.assertEqual([item['id'] for item in matches], [8])

    def test_auto_template_refresh_matches_window_windows_and_aix_in_any_order(self):
        self.inventories[50] = 'aixapp_mirror_inventory'
        self.ostypes[50] = 'aix'
        self.candidates.append(self.host(51, 'AIX1', 50))
        self.credentials.extend([
            {'id': 150, 'name': 'aixapp-os', 'kind': 'ssh', 'credential_type': 11},
            {'id': 250, 'name': 'aixapp_1hop', 'kind': 'cloud', 'credential_type': 33},
        ])
        self.job_templates[0]['name'] = 'Install Agent Zabbix Unix'
        self.job_templates[1]['name'] = 'Zabbix Windows Agent Install'
        self.job_templates.append({'id': 10, 'name': 'AIX Zabbix Agent Install', 'extra_vars': '{}'})
        for value in ('window', 'windows', ' WINDOWS '):
            with self.subTest(ostype=value):
                self.ostypes[20] = value
                self.write_config(JOB_TEMPLATE_AUTO_DISCOVERY=True, JOB_TEMPLATE_ORGANIZATION='Operations',
                                  JOB_TEMPLATE_NAME_PATTERN='*zabbix*install*{os_name}*')
                self.write_workbook(['WIN1,', 'AIX1', 'Linux1'])
                code, output, _ = self.refresh()
                self.assertEqual(code, 0, output)
                book = load_workbook(self.path)
                try:
                    sheet = book.active
                    columns = awx.excel.read_columns(sheet)
                    expected = {'win1': ('windows', 8), 'AIX1': ('aix', 10), 'Linux1': ('linux', 7)}
                    for number in range(2, sheet.max_row + 1):
                        hostname = sheet.cell(number, columns['hostname']).value
                        os, template_id = expected[hostname]
                        template = next(item for item in self.job_templates if item['id'] == template_id)
                        self.assertEqual(sheet.cell(number, columns['ostype']).value, os)
                        self.assertEqual(sheet.cell(number, columns['template_job_name']).value, template['name'])
                        self.assertEqual(sheet.cell(number, columns['job_link']).value,
                                         f'https://awx.example.test/api/v2/job_templates/{template_id}/')
                        self.assertEqual(sheet.cell(number, columns['output']).value, 'previous output')
                finally:
                    book.close()
        self.client.post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
