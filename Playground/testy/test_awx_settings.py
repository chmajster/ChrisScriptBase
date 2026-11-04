"""Settings snapshots must preserve parameters without exposing secrets or formulas."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
    from lib import awx_excel as excel


class SettingsSheetTests(unittest.TestCase):
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
