"""Jumphost matching, Excel choices and preserved manual selections."""

import contextlib
import io
import unittest
from unittest.mock import patch

from lib import awx
from lib import awx_jumphosts as jumphosts
from tests import test_awx_refresh_excel as refresh_tests


class JumphostTests(unittest.TestCase):
    def setUp(self):
        self.client = refresh_tests.FakeAWX()
        self.client.credential_records = {
            str(credential_id): {'id': credential_id, 'name': name, 'credential_type': 31, 'inputs': {}}
            for credential_id, name in (
                (9, 'it2_backup_3hop_cred'), (7, 'it2_3hop_cred'),
                (10, 'it20_3hop_cred'), (11, 'it2_13hop_cred'),
                (12, 'it2_1hop_cred'), (13, 'IT2_3_HOP_cred'),
            )
        }

    def test_exact_application_and_hop_matching_is_sorted_and_cached(self):
        self.assertEqual(jumphosts.credential_options(self.client, 'it2', 3),
                         ['it2_3hop_cred', 'it2_backup_3hop_cred', 'IT2_3_HOP_cred'])
        self.assertEqual(jumphosts.credential_options(self.client, 'IT2', 1), ['it2_1hop_cred'])
        self.assertEqual(sum(call[1] == 'credentials/' for call in self.client.calls), 1)

    def test_zero_missing_and_unknown_counts_do_not_query_credentials(self):
        for count in (None, 0, -1):
            self.assertEqual(jumphosts.credential_options(self.client, 'it2', count), [])
        self.assertEqual(jumphosts.credential_options(self.client, 'none', 3), [])
        self.assertEqual(self.client.calls, [])

    def test_duplicate_numeric_and_separator_names_use_unambiguous_ids(self):
        self.client.credential_records['14'] = {'id': 14, 'name': 'it2_3hop_cred', 'credential_type': 32}
        self.client.credential_records['15'] = {'id': 15, 'name': 'it2_3hop,cred', 'credential_type': 33}
        options = jumphosts.credential_options(self.client, 'it2', 3)
        self.assertIn('7', options)
        self.assertIn('14', options)
        self.assertIn('15', options)
        self.assertNotIn('it2_3hop_cred', options)

    def test_named_dropdown_survives_sort_reorder_and_save_without_changing_selection(self):
        book = awx.Workbook()
        self.addCleanup(book.close)
        sheet = book.active
        sheet.append(['hostname', 'ostype', 'ApplicationID', 'HOPS_COUNT', 'jumphost_for_hops'])
        sheet.append(['linux-1', 'linux', 'it2', 3, 'it2_backup_3hop_cred'])
        sheet.append(['windows-1', 'windows', 'it2', 1, 'it2_1hop_cred'])
        jumphosts.populate_choices(sheet, self.client)
        self.assertEqual(len(sheet.data_validations.dataValidation), 2)
        first_rule = sheet.data_validations.dataValidation[0]
        definition = book.defined_names[first_rule.formula1.lstrip('=')]
        helper_name, bounds = next(definition.destinations)
        helper = book[helper_name]
        self.assertEqual(helper.sheet_state, 'hidden')
        self.assertEqual([row[0].value for row in helper[bounds]],
                         ['it2_3hop_cred', 'it2_backup_3hop_cred', 'IT2_3_HOP_cred'])
        self.assertFalse(first_rule.showErrorMessage)
        awx.excel.prepare_sheet(sheet)
        awx.excel.sort_host_rows(sheet)
        columns = awx.excel.read_columns(sheet)
        self.assertEqual(sheet.cell(3, columns['jumphost_for_hops']).value, 'it2_backup_3hop_cred')
        letter = awx.get_column_letter(columns['jumphost_for_hops'])
        self.assertEqual(str(first_rule.sqref), f'{letter}3')
        stream = io.BytesIO()
        awx.excel.format_sheet(sheet)
        book.save(stream)
        stream.seek(0)
        reloaded = awx.load_workbook(stream)
        self.addCleanup(reloaded.close)
        self.assertEqual(len(reloaded.active.data_validations.dataValidation), 2)
        self.assertEqual(reloaded.active.cell(3, columns['jumphost_for_hops']).value, 'it2_backup_3hop_cred')
        jumphosts.populate_choices(sheet, self.client)
        self.assertEqual(len(sheet.data_validations.dataValidation), 2)
        self.assertEqual(len(book.sheetnames), 2)

    def test_long_option_lists_use_ranges_instead_of_inline_excel_lists(self):
        self.client.credential_records = {
            str(i): {'id': i, 'name': f'it2_3hop_long_credential_name_{i}', 'credential_type': 31}
            for i in range(20)
        }
        book = awx.Workbook()
        self.addCleanup(book.close)
        sheet = book.active
        sheet.append(['ApplicationID', 'HOPS_COUNT', 'jumphost_for_hops'])
        sheet.append(['it2', 3, None])
        jumphosts.populate_choices(sheet, self.client)
        rule = sheet.data_validations.dataValidation[0]
        helper_name, bounds = next(book.defined_names[rule.formula1.lstrip('=')].destinations)
        self.assertEqual(len(book[helper_name][bounds]), 20)

    def test_full_list_is_cleaned_without_a_jumphost_column(self):
        book = awx.Workbook()
        self.addCleanup(book.close)
        sheet = book.active
        sheet.append(['Full_Credential'])
        sheet.append([' machine-cred, machine-cred , 031, 31, vault-cred, vault-cred '])
        with patch.object(awx.AWXClient, 'request', side_effect=AssertionError('No API calls')):
            awx.synchronise_full_credential(sheet, 2)
        self.assertEqual(sheet['A2'].value, 'machine-cred, 031, vault-cred')
        awx.synchronise_full_credential(sheet, 2)
        self.assertEqual(sheet['A2'].value, 'machine-cred, 031, vault-cred')

    def test_full_list_replaces_previous_selection_deduplicates_and_disables_jumps(self):
        book = awx.Workbook()
        self.addCleanup(book.close)
        sheet = book.active
        sheet.append(['Full_Credential', 'jumphost_for_hops'])
        sheet.append(['machine-cred, first_3hop', 'first_3hop'])
        awx.synchronise_full_credential(sheet, 2)
        self.assertEqual(sheet['A2'].value, 'machine-cred, first_3hop')
        sheet['B2'] = 'second_3hop, second_3hop'
        awx.synchronise_full_credential(sheet, 2)
        self.assertEqual(sheet['A2'].value, 'machine-cred, second_3hop')
        sheet['B2'] = 'none'
        awx.synchronise_full_credential(sheet, 2)
        self.assertEqual(sheet['A2'].value, 'machine-cred')
        sheet['A2'] = 'machine-cred, manually-added'
        sheet['B2'] = 'third_3hop'
        awx.synchronise_full_credential(sheet, 2)
        self.assertEqual(sheet['A2'].value, 'machine-cred, manually-added, third_3hop')


class JumphostRefreshTests(unittest.TestCase):
    setUp = refresh_tests.RefreshTests.setUp
    save_rows = refresh_tests.RefreshTests.save_rows

    def test_default_selection_and_manual_alternatives_survive_refresh(self):
        self.client.credential_records.update({
            'test_3hop_first': {'id': 301, 'name': 'test_3hop_first', 'credential_type': 31},
            'test_3hop_second': {'id': 302, 'name': 'test_3hop_second', 'credential_type': 31},
        })
        awx.apply_settings(self.connection)
        report = awx.fetch_report(self.client, 1, hostnames=['windows-1'])
        report['hosts'][0]['all_groups'].append({'id': 91, 'name': 'test_3hop_group'})
        metadata, headers = awx.metadata_by_host(report, self.settings, client=self.client)
        self.assertEqual(metadata['windows-1']['jumphost_for_hops'], 'test_3hop_first')
        self.assertEqual(metadata['windows-1']['Full_Credential'], 'shared-vault, windows-key, test_3hop_first')
        self.assertEqual(report['hosts'][0]['jumphost_for_hops_options'], ['test_3hop_first', 'test_3hop_second'])
        self.save_rows(['hostname', 'ApplicationID', 'HOPS_COUNT', 'jumphost_for_hops', 'exceiute_job'],
                       [['windows-1', 'test', 3, 'test_3hop_second', 'saved-job']])
        book = awx.load_workbook(self.path)
        try:
            sheet = book['Results']
            columns = awx.excel.read_columns(sheet)
            with contextlib.redirect_stdout(io.StringIO()):
                awx.refresh_sheet(sheet, {2: 'windows-1'}, columns, columns['hostname'], metadata, headers)
            self.assertEqual(sheet.cell(2, columns['jumphost_for_hops']).value, 'test_3hop_second')
            self.assertEqual(sheet.cell(2, columns['full_credential']).value, 'shared-vault, windows-key, test_3hop_second')
            sheet.cell(2, columns['jumphost_for_hops']).value = None
            with contextlib.redirect_stdout(io.StringIO()):
                awx.refresh_sheet(sheet, {2: 'windows-1'}, columns, columns['hostname'], metadata, headers)
            self.assertEqual(sheet.cell(2, columns['jumphost_for_hops']).value, 'test_3hop_first')
            self.assertEqual(sheet.cell(2, columns['full_credential']).value, 'shared-vault, windows-key, test_3hop_first')
            self.assertEqual(sheet.cell(2, columns['exceiute_job']).value, 'saved-job')
            metadata['windows-1']['HOPS_COUNT'] = 4
            metadata['windows-1']['jumphost_for_hops'] = 'none'
            with contextlib.redirect_stdout(io.StringIO()):
                awx.refresh_sheet(sheet, {2: 'windows-1'}, columns, columns['hostname'], metadata, headers)
            self.assertEqual(sheet.cell(2, columns['jumphost_for_hops']).value, 'none')
            self.assertEqual(sheet.cell(2, columns['full_credential']).value, 'shared-vault, windows-key')
        finally:
            book.close()


if __name__ == '__main__':
    unittest.main()
