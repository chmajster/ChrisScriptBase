"""Batch limit and launch regressions using an offline AWX client."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
    from lib import awx


TEMPLATE_URL = 'https://awx.example.test/api/v2/job_templates/7/'


class LocalhostBatchTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            'EXECUTION_COLUMN': 'exceiute_job', 'OUTPUT_COLUMN': 'output',
            'BATCH_SIZE': 2, 'JOB_URL': '', 'JOB_TYPE': '', 'JOB_TYPE_VARIABLE': 'action',
            'SURVEY_ANSWERS': {}, 'WAIT_FOR_COMPLETION': False, 'EXCEL_COLUMNS': [],
        }
        overrides = patch.multiple(awx, PAGE_SIZE=200, FULL_CREDENTIAL_SEPARATOR=', ')
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = awx.AWXClient('https://awx.example.test', 'test-token')
        self.client.get = Mock(side_effect=AssertionError('Unexpected AWX detail request'))
        self.client.items = Mock(side_effect=AssertionError('Unexpected AWX list request'))
        self.client.post = Mock(return_value={'job': 99, 'artifacts': {}})

    def workbook(self, hostnames):
        book = awx.excel.create_workbook(
            ['hostname', 'Job_Link', 'Job_Variables', 'Full_Credential'],
            [[name, TEMPLATE_URL, '{"action": "install"}', 'none'] for name in hostnames],
        )
        self.addCleanup(book.close)
        return book

    def plan(self, book):
        return awx.build_execution_plan(book.active, self.client, self.settings)

    def test_every_batch_starts_with_localhost_without_using_an_extra_slot(self):
        book = self.workbook(['host1', 'host2', 'host3'])
        batches, existing, _, skipped = self.plan(book)
        self.assertEqual([batch['limit'] for batch in batches],
                         ['localhost,host1,host2', 'localhost,host3'])
        self.assertEqual([len(batch['entries']) for batch in batches], [2, 1])
        self.assertEqual(book.active.max_row, 4)
        self.assertEqual(existing, [])
        self.assertEqual(skipped, 0)

    def test_existing_localhost_is_moved_first_and_not_duplicated(self):
        self.settings['BATCH_SIZE'] = 10
        book = self.workbook(['host1', 'LOCALHOST', 'host2', 'localhost'])
        batches, _, _, _ = self.plan(book)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]['limit'], 'localhost,host1,host2')
        self.assertEqual(len(batches[0]['entries']), 3)
        localhost = next(entry for entry in batches[0]['entries']
                         if entry['hostname'].casefold() == 'localhost')
        self.assertEqual(localhost['rows'], [3, 5])

    def test_inventory_name_resolution_keeps_localhost_first(self):
        self.settings['BATCH_SIZE'] = 10
        book = self.workbook(['host1', 'LOCALHOST', 'host2'])
        batches, _, _, _ = self.plan(book)
        self.client.items = Mock(return_value=[{'id': 1, 'name': 'Host1'}, {'id': 2, 'name': 'HOST2'}])
        cache = {7: ({'defaults': {'inventory': {'id': 10}}}, [], {})}
        awx.resolve_execution_hostnames(self.client, batches, cache)
        self.assertEqual(batches[0]['limit'], 'localhost,Host1,HOST2')
        self.assertNotIn('localhost', self.client.items.call_args.kwargs['name__iregex'].casefold())
        self.client.get.assert_not_called()
        # Older launch responses may omit inventory metadata; the limit still applies.
        batches[0]['limit'] = 'Host1,HOST2'
        self.client.items.reset_mock()
        awx.resolve_execution_hostnames(self.client, batches, {7: ({'defaults': {}}, [], {})})
        self.assertEqual(batches[0]['limit'], 'localhost,Host1,HOST2')
        self.client.items.assert_not_called()

    def test_launch_payload_always_starts_with_localhost(self):
        book = self.workbook(['host1'])
        batches, existing, column, _ = self.plan(book)
        batches[0].update(limit='host1,localhost,localhost', credentials=[])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'execution.xlsx'
            with redirect_stdout(io.StringIO()):
                awx.execute_plan(book, book.active, path, self.client, self.settings,
                                 batches, existing, column)
        self.client.post.assert_called_once_with('job_templates/7/launch/', {
            'limit': 'localhost,host1', 'extra_vars': {'action': 'install'},
        })
        self.assertEqual(book.active.max_row, 2)
        self.assertIn('/99/', book.active.cell(2, column).value)


if __name__ == '__main__':
    unittest.main()
