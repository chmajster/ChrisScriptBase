"""Credential export regressions. Run: python -m unittest testy.test_awx_credentials -v."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import load_workbook

# All settings below are fixtures; skip the user's local connection configuration.
with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
    from lib import awx


TEMPLATE_URL = 'https://awx.example.test/#/templates/job_template/7/details'


class FullCredentialTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            'EXECUTION_COLUMN': 'exceiute_job', 'OUTPUT_COLUMN': 'output',
            'SHEET_NAME': 'Results', 'JOB_URL': '', 'JOB_TYPE': '',
            'JOB_TYPE_VARIABLE': 'action', 'EXCEL_COLUMNS': [],
        }
        overrides = patch.multiple(
            awx, CREDENTIAL_VARIABLES=['os_credential'], EXTRA_CREDENTIAL_VARIABLES=[],
            FULL_CREDENTIAL_SEPARATOR=', ',
            OS_JOBS={'linux': {'url': TEMPLATE_URL, 'variables': {}}},
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        credentials = [
            (101, 'template-machine', 'ssh', 11),
            (102, 'host-machine', 'ssh', 11),
            (201, 'shared-vault', 'vault', 22),
            (301, 'app_1hop', 'cloud', 33),
            (302, 'app_alt_1hop', 'cloud', 33),
        ]
        self.client = awx.AWXClient('https://awx.example.test', 'test-token')
        self.client.get = Mock(side_effect=AssertionError('Unexpected AWX request'))
        self.client.items = Mock(side_effect=AssertionError('Unexpected AWX request'))
        self.client.template_detail_cache[7] = {'id': 7, 'name': 'template-job'}
        for credential_id, name, kind, credential_type in credentials:
            credential = {'id': credential_id, 'name': name, 'kind': kind,
                          'credential_type': credential_type, 'inputs': {}}
            self.client.credential_cache[name] = credential
            self.client.credential_cache[str(credential_id)] = credential
        self.client.jumphost_credential_cache['app'] = [
            self.client.credential_cache[name] for name in ('app_1hop', 'app_alt_1hop')
        ]
        self.template_cache = {
            7: ({'defaults': {'credentials': [self.client.credential_cache[name]
                                            for name in ('template-machine', 'shared-vault')]}}, [], {})
        }

    def report(self, host_values=(), *, with_jumps=False):
        return {'inventory': {'name': 'app_inventory'}, 'hosts': [{
            'name': 'server1', 'ostype': 'linux',
            'all_groups': [{'id': 1, 'name': 'app_1hop'}] if with_jumps else [],
            'credential_groups': [{'variable': 'os_credential', 'value': value}
                                  for value in host_values],
        }]}

    def build(self, host_values=(), *, with_jumps=False):
        headers, rows = awx.build_inventory_rows(
            self.report(host_values, with_jumps=with_jumps), self.settings, 'output',
            client=self.client, template_cache=self.template_cache,
        )
        return headers, rows, dict(zip(headers, rows[0]))

    def test_os_credential_matching_template_machine_is_kept(self):
        for value in ('template-machine', 101, '101', ' template-machine '):
            with self.subTest(value=value):
                _, _, record = self.build([value])
                self.assertEqual(record['Full_Credential'], f'shared-vault, {str(value).strip()}')

    def test_inherited_machine_without_host_selection_is_omitted(self):
        _, _, record = self.build()
        self.assertEqual(record['Full_Credential'], 'shared-vault')
        self.assertEqual(record['OS Credential'], 'none')
        self.assertEqual(record['Job_Credentials'], 'template-machine, shared-vault')
        self.assertEqual(record['Extra_Credentials'], 'template-machine, shared-vault')

    def test_different_host_machine_replaces_template_machine(self):
        _, _, record = self.build(['host-machine'])
        self.assertEqual(record['Full_Credential'], 'shared-vault, host-machine')

    def test_explicit_extra_machine_is_kept_and_deduplicated(self):
        with patch.object(awx, 'EXTRA_CREDENTIAL_VARIABLES', ['template-machine', 'shared-vault']):
            _, _, record = self.build([101, 'template-machine'])
        self.assertEqual(record['Full_Credential'], 'template-machine, shared-vault')

    def test_os_credential_survives_exported_workbook_save(self):
        headers, rows, _ = self.build(['template-machine'], with_jumps=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'inventory.xlsx'
            awx.save_inventory_excel(headers, rows, path, self.settings, 'output')
            book = load_workbook(path)
            self.addCleanup(book.close)
            columns = awx.excel.read_columns(book.active)
            self.assertEqual(book.active.cell(2, columns['full_credential']).value,
                             'shared-vault, template-machine, app_1hop')

    def test_refresh_restores_os_credential_and_preserves_jump_and_history(self):
        settings = awx.common.normalize_refresh_settings(dict(self.settings))
        self.client.organization_cache[('name', 'app')] = {'id': 1, 'name': 'app'}
        groups = awx.templates.name_match_groups(settings, {'name': 'server1', 'INVENTORY': 'app_inventory', 'ostype': 'linux'})
        self.client.template_search_cache[(1, tuple(groups))] = [{'id': 7, 'name': 'Zabbix Unix Install'}]
        headers, rows, _ = self.build(['template-machine'], with_jumps=True)
        book = awx.excel.create_workbook(headers, rows)
        self.addCleanup(book.close)
        sheet = book.active
        columns = awx.excel.read_columns(sheet)
        sheet.cell(2, columns['full_credential']).value = 'shared-vault, app_1hop'
        sheet.cell(2, columns['jumphost_for_hops']).value = 'app_alt_1hop'
        history = 'https://awx.example.test/#/jobs/playbook/99/output'
        sheet.cell(2, columns['exceiute_job']).value = history
        sheet.cell(2, columns['output']).value = 'previous output'
        metadata, names = awx.metadata_by_host(
            self.report(['template-machine'], with_jumps=True), self.settings,
            client=self.client, template_cache=self.template_cache,
        )
        awx.refresh_sheet(sheet, {2: 'server1'}, columns, columns['hostname'], metadata, names)
        self.assertEqual(sheet.cell(2, columns['full_credential']).value,
                         'shared-vault, template-machine, app_alt_1hop')
        self.assertEqual(sheet.cell(2, columns['os credential']).value, 'template-machine')
        self.assertEqual(sheet.cell(2, columns['exceiute_job']).value, history)
        self.assertEqual(sheet.cell(2, columns['output']).value, 'previous output')


if __name__ == '__main__':
    unittest.main()
