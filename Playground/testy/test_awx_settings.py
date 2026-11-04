"""Settings snapshots must preserve parameters without exposing secrets or formulas."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from openpyxl.comments import Comment
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
    from lib import awx_excel as excel


class SettingsSheetTests(unittest.TestCase):
    def test_legacy_execution_column_merges_links_and_comments(self):
        book = excel.Workbook()
        self.addCleanup(book.close)
        sheet = book.active
        sheet.append(['hostname', 'exceiute_job', 'exceiute_job_link', 'Survey_mode'])
        sheet.append(['host1', 'https://awx.example.test/api/v2/jobs/90/', None])
        sheet.append(['host2', 'https://awx.example.test/api/v2/jobs/91/',
                      'https://awx.example.test/excecution/jobs/playbook/91/output'])
        sheet.cell(2, 2).comment = Comment('old history', 'Operator')
        sheet.cell(2, 3).comment = Comment('new history', 'AWX')
        rule = DataValidation(type='list', formula1='"install,update"', promptTitle='AWX survey')
        rule.add('D2:D10')
        sheet.add_data_validation(rule)
        columns, _ = excel.prepare_sheet(sheet)
        self.assertNotIn('exceiute_job', columns)
        cell = sheet.cell(2, columns['exceiute_job_link'])
        self.assertEqual(cell.value, 'https://awx.example.test/api/v2/jobs/90/')
        self.assertIn('old history', cell.comment.text)
        self.assertIn('new history', cell.comment.text)
        survey_column = get_column_letter(columns['survey_mode'])
        self.assertEqual(str(rule.sqref), f'{survey_column}2:{survey_column}10')
        excel.format_sheet(sheet)
        self.assertEqual(cell.value, 'https://awx.example.test/excecution/jobs/playbook/90/output')
        self.assertEqual(cell.hyperlink.target, cell.value)

    def test_saved_history_links_use_execution_ui_and_preserve_run_ids_and_comments(self):
        links = [
            'https://awx.example.test/api/v2/jobs/90/',
            'https://awx.example.test/#/jobs/playbook/91/output',
            'https://awx.example.test/#/job/playbook/92/output',
            'https://awx.example.test/awx/jobs/playbook/93/output',
            'https://awx.example.test/excecution/jobs/playbook/94/output',
        ]
        template = 'https://awx.example.test/#/templates/job_template/7/details'
        book = excel.create_workbook(['hostname', 'Job_Link', 'exceiute_job_link'],
                                     [[f'host{number}', template, link] for number, link in enumerate(links)])
        self.addCleanup(book.close)
        columns = excel.read_columns(book.active)
        for row in range(2, len(links) + 2):
            book.active.cell(row, columns['exceiute_job_link']).comment = Comment('previous run', 'Operator')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.xlsx'
            excel.save_workbook(book, path)
            saved = excel.load_workbook(path)
            try:
                for row, job_id in enumerate(range(90, 95), 2):
                    cell = saved.active.cell(row, columns['exceiute_job_link'])
                    expected = f'https://awx.example.test/excecution/jobs/playbook/{job_id}/output'
                    self.assertEqual(cell.value, expected)
                    self.assertEqual(cell.hyperlink.target, expected)
                    self.assertEqual(cell.comment.text, 'previous run')
                    self.assertEqual(saved.active.cell(row, columns['job_link']).value, template)
            finally:
                saved.close()

    def test_conflicting_old_and_new_job_history_stops_migration(self):
        book = excel.Workbook()
        self.addCleanup(book.close)
        sheet = book.active
        sheet.append(['hostname', 'exceiute_job', 'exceiute_job_link'])
        sheet.append(['host1', 'https://awx.example.test/api/v2/jobs/90/',
                      'https://awx.example.test/#/jobs/playbook/91/output'])
        original = list(sheet.values)
        with self.assertRaisesRegex(excel.AWXError, 'różną historię'):
            excel.prepare_sheet(sheet)
        self.assertEqual(list(sheet.values), original)

    def test_secrets_are_removed_and_parameter_types_survive_save_and_reload(self):
        book = excel.create_workbook(excel.headers(), [])
        self.addCleanup(book.close)
        timestamp = datetime(2026, 11, 4, 12, 30)
        excel.add_settings_sheet(book, {
            'Eksport': {'Data eksportu (UTC)': timestamp, 'Liczba hostów': 0},
            'Parametry': {
                'AWX_TOKEN': 'token-value', 'DEBUG': False,
                'OS_JOBS': {'linux': {'variables': {
                    'password': 'password-value', 'access_code': 'survey-password-value',
                    'items': [{'api_key': 'api-key-value'}], 'version': '=1+1',
                }}},
                'SURVEY_ANSWERS': {'access_code': 'another-password-value'},
                'AWX_URL': 'https://awx.example.test/?value=token-value',
                'CREDENTIAL_VARIABLES': ['os_credential'],
            },
        }, sensitive_keys={'access_code'}, secrets=('token-value',))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.xlsx'
            excel.save_workbook(book, path)
            saved = excel.load_workbook(path)
            try:
                sheet = saved['Ustawienia']
                values = {row[1]: row[2] for row in sheet.iter_rows(min_row=5, values_only=True)}
                self.assertNotIn('AWX_TOKEN', values)
                for secret in ('token-value', 'password-value', 'survey-password-value',
                               'another-password-value', 'api-key-value'):
                    self.assertNotIn(secret, str(values))
                self.assertEqual(values['OS_JOBS.linux.variables.password'], '***')
                self.assertEqual(values['SURVEY_ANSWERS.access_code'], '***')
                self.assertEqual(values['CREDENTIAL_VARIABLES.[1]'], 'os_credential')
                self.assertEqual(values['Data eksportu (UTC)'], timestamp)
                self.assertEqual(values['Liczba hostów'], 0)
                self.assertIs(values['DEBUG'], False)
                self.assertEqual(saved.active.title, 'Results')
                cell = next(row[2] for row in sheet.iter_rows(min_row=5)
                            if row[1].value == 'OS_JOBS.linux.variables.version')
                self.assertEqual(cell.value, '=1+1')
                self.assertEqual(cell.data_type, 's')
            finally:
                saved.close()

    def test_reserved_name_does_not_overwrite_host_data(self):
        book = excel.create_workbook(excel.headers(), [], sheet_name='ustawienia')
        self.addCleanup(book.close)
        with self.assertRaises(excel.AWXError):
            excel.add_settings_sheet(book, {'Eksport': {'Liczba hostów': 0}})
        self.assertEqual(book.active['A1'].value, 'hostname')
        self.assertEqual(book.sheetnames, ['ustawienia'])


if __name__ == '__main__':
    unittest.main()
