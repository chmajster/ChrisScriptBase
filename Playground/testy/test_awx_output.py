"""Invalid execution links must not stop collection for other Excel rows."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
    from lib import awx


class WorkbookAccessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'hosts.xlsx'
        self.content = b'unchanged workbook bytes'
        self.path.write_bytes(self.content)

    def test_access_probe_preserves_data_and_releases_its_handle(self):
        awx.excel.ensure_workbook_writable(self.path)
        awx.excel.ensure_workbook_writable(self.path)
        self.assertEqual(self.path.read_bytes(), self.content)

    def test_open_or_locked_workbook_is_rejected_and_can_be_checked_after_close(self):
        with self.path.open('r+b') as opened:
            if os.name != 'nt':
                import fcntl

                fcntl.flock(opened.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(awx.AWXError) as raised:
                awx.excel.ensure_workbook_writable(self.path)
            self.assertIn('otwarty lub zablokowany', str(raised.exception))
            self.assertIn(str(self.path), str(raised.exception))
            self.assertIn('Zamknij ten plik w Excelu', str(raised.exception))
        awx.excel.ensure_workbook_writable(self.path)
        self.assertEqual(self.path.read_bytes(), self.content)

    def test_missing_workbook_is_reported_without_creating_it(self):
        missing = self.path.with_name('missing.xlsx')
        with self.assertRaisesRegex(awx.AWXError, 'Plik Excel nie istnieje'):
            awx.excel.ensure_workbook_writable(missing)
        self.assertFalse(missing.exists())


class ArtifactStatusTests(unittest.TestCase):
    def test_status_is_derived_from_completed_word_and_placed_next_to_the_filter(self):
        messages = [
            ('Installation completed successfully.', 'completed'),
            ('COMPLETED', 'completed'),
            ('First step\nCompleted!', 'completed'),
            ('uncompleted', 'error'),
            ('completedness', 'error'),
            ('installation failed', 'error'),
            ('', 'error'),
            (None, 'error'),
        ]
        book = awx.excel.create_workbook(
            ['hostname', 'artifact filter'],
            [[f'host{index}', message] for index, (message, _) in enumerate(messages, 1)],
        )
        self.addCleanup(book.close)
        columns = awx.excel.read_columns(book.active)
        self.assertEqual(columns['artifact status'], columns['artifact filter'] + 1)
        self.assertEqual([book.active.cell(row, columns['artifact status']).value
                          for row in range(2, len(messages) + 2)], [status for _, status in messages])

    def test_saved_status_tracks_changed_messages_and_is_visible_with_artifact_filter(self):
        book = awx.excel.create_workbook(['hostname', 'artifact filter'], [['host1', 'completed']])
        self.addCleanup(book.close)
        columns = awx.excel.read_columns(book.active)
        book.active.cell(2, columns['artifact filter']).value = 'failed on retry'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'hosts.xlsx'
            awx.excel.save_workbook(book, path, excel_columns=['hostname', 'artifact filter'])
            saved = awx.excel.load_workbook(path)
            try:
                columns = awx.excel.read_columns(saved.active)
                cell = saved.active.cell(2, columns['artifact status'])
                self.assertEqual(cell.value, 'error')
                self.assertFalse(saved.active.column_dimensions[cell.column_letter].hidden)
                self.assertEqual(columns['artifact status'], columns['artifact filter'] + 1)
            finally:
                saved.close()

    def test_shared_artifact_writer_classifies_each_host_independently(self):
        book = awx.excel.create_workbook(['hostname'], [['host1'], ['host2'], ['host3']])
        self.addCleanup(book.close)
        awx.write_job_artifacts(book.active, {
            'status': 'failed', 'artifacts': {
                'host1': {'message': 'Installation COMPLETED successfully'},
                'host2': {'message': 'installation failed'},
            },
        }, [{'row': row} for row in (2, 3, 4)], 'https://awx.example.test/api/v2/jobs/99/')
        columns = awx.excel.read_columns(book.active)
        self.assertEqual([book.active.cell(row, columns['artifact status']).value for row in (2, 3, 4)],
                         ['completed', 'error', 'error'])


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

    def test_singular_and_plural_job_links_with_or_without_scheme_share_one_job(self):
        links = [
            'https://awx.example.test/#/job/playbook/99/output',
            'https://awx.example.test/#/jobs/playbook/99/output',
            'awx.example.test/#/job/playbook/99/output',
            '//awx.example.test/#/job/playbook/99/output',
            'https://awx.example.test/job/playbook/99/output',
            'https://awx.example.test/api/v2/jobs/99/',
        ]
        book = self.workbook(links)
        captured = io.StringIO()
        with redirect_stderr(captured):
            jobs, _, skipped = awx.build_output_plan(book.active, self.client, self.settings)
        self.assertEqual(list(jobs), [99])
        self.assertEqual([entry['row'] for entry in jobs[99]], list(range(2, len(links) + 2)))
        self.assertEqual(skipped, 0)
        self.assertEqual(captured.getvalue(), '')
        self.client.get.assert_not_called()
        self.client.post.assert_not_called()

    def test_schemeless_job_links_use_configured_scheme_and_require_matching_port(self):
        client = awx.AWXClient('http://awx.example.test:8080/api/v2/', 'offline-token')
        self.assertEqual(awx.job_id_from_link('awx.example.test:8080/#/job/playbook/99/output', client), 99)
        with self.assertRaises(awx.AWXError):
            awx.job_id_from_link('awx.example.test/#/job/playbook/99/output', client)

    def test_singular_links_still_require_a_valid_job_id_and_the_configured_server(self):
        for link in (
            'other.example.test/#/job/playbook/99/output',
            'https://other.example.test/#/job/playbook/99/output',
            'https://awx.example.test/#/job/playbook/<ID>/output',
            'https://awx.example.test/#/job/playbook/0/output',
            'https://awx.example.test/#/job/playbook/-1/output',
        ):
            with self.subTest(link=link), self.assertRaises(awx.AWXError):
                awx.job_id_from_link(link, self.client)
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
