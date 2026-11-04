"""Invalid execution links must not stop collection for other Excel rows."""

import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
    from lib import awx


class OutputPlanTests(unittest.TestCase):
    def setUp(self):
        self.client = awx.AWXClient('https://awx.example.test', 'offline-token')
        self.client.get = Mock(side_effect=AssertionError('Unexpected API request'))
        self.client.post = Mock(side_effect=AssertionError('Unexpected launch'))
        self.settings = {'EXECUTION_COLUMN': 'exceiute_job_link', 'OUTPUT_COLUMN': 'output'}

    def workbook(self, links):
        book = awx.excel.create_workbook(
            ['hostname', 'ostype', 'exceiute_job_link', 'output'],
            [[f'host{index}', 'linux', link, 'keep previous output'] for index, link in enumerate(links, 1)],
        )
        self.addCleanup(book.close)
        return book

    def test_invalid_links_and_formulas_are_skipped_before_grouping_later_valid_jobs(self):
        invalid = [
            'https://awx.example.test/',
            'https://awx.example.test/api/v2/job_templates/7/',
            'https://awx.example.test/?token=offline-token',
            'https://[invalid',
            'https://awx.example.test:invalid/api/v2/jobs/99/',
            'https://other.example.test/api/v2/jobs/99/',
            '=HYPERLINK("https://awx.example.test/api/v2/jobs/99/","Job")',
        ]
        valid = ['https://awx.example.test/api/v2/jobs/99/',
                 'https://awx.example.test/#/jobs/playbook/99/output']
        book = self.workbook([*invalid, *valid, None, 'LAUNCH_PENDING template=7'])
        captured = io.StringIO()
        with redirect_stderr(captured):
            jobs, _, skipped = awx.build_output_plan(book.active, self.client, self.settings)
        self.assertEqual(list(jobs), [99])
        self.assertEqual([entry['row'] for entry in jobs[99]], [len(invalid) + 2, len(invalid) + 3])
        self.assertEqual(skipped, len(invalid) + 2)
        self.assertEqual(captured.getvalue().count('Pomijam wiersz'), len(invalid))
        self.assertIn("Arkusz: 'Results'", captured.getvalue())
        self.assertIn("Kolumna: 'exceiute_job_link'", captured.getvalue())
        self.assertIn(f'Wartość komórki: {invalid[-1]!r}', captured.getvalue())
        self.assertIn('Cel hiperłącza: brak', captured.getvalue())
        self.assertIn('Źródło sprawdzanego adresu: wartość komórki (formuła Excela)', captured.getvalue())
        self.assertNotIn('offline-token', captured.getvalue())
        self.client.get.assert_not_called()
        self.client.post.assert_not_called()

    def test_invalid_hyperlink_target_is_skipped_and_an_all_invalid_plan_is_empty(self):
        book = self.workbook(['https://awx.example.test/api/v2/jobs/99/'])
        column = awx.excel.read_columns(book.active)['exceiute_job_link']
        book.active.cell(2, column).hyperlink = 'https://awx.example.test/'
        book.active.cell(2, column).value = '  https://awx.example.test/api/v2/jobs/99/\n'
        captured = io.StringIO()
        with redirect_stderr(captured):
            jobs, _, skipped = awx.build_output_plan(book.active, self.client, self.settings)
        self.assertEqual(jobs, {})
        self.assertEqual(skipped, 1)
        self.assertIn(f'Pomijam wiersz 2 ({book.active.cell(2, column).coordinate})', captured.getvalue())
        self.assertIn("Wartość komórki: '  https://awx.example.test/api/v2/jobs/99/\\n'", captured.getvalue())
        self.assertIn("Cel hiperłącza: 'https://awx.example.test/'", captured.getvalue())
        self.assertIn('Źródło sprawdzanego adresu: cel hiperłącza', captured.getvalue())
        self.assertIn('Powód pominięcia:', captured.getvalue())
        self.assertIn('Oczekiwany adres: https://awx.example.test/api/v2/jobs/<ID>/', captured.getvalue())
        self.client.get.assert_not_called()
        self.client.post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
