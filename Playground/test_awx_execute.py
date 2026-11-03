"""Batch limit and launch regressions using an offline AWX client."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
    from lib import awx


TEMPLATE_URL = 'https://awx.example.test/api/v2/job_templates/7/'


class ExecutionBatchTests(unittest.TestCase):
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
        self.client.template_detail_cache.update({
            7: {'id': 7, 'name': 'Install job'},
            8: {'id': 8, 'name': 'Install job'},
        })

    def workbook(self, hostnames):
        book = awx.excel.create_workbook(
            ['hostname', 'ostype', 'template_job_name', 'Job_Link', 'Job_Variables', 'Full_Credential'],
            [[name, 'linux', 'Install job', TEMPLATE_URL, '{"action": "install"}', 'none'] for name in hostnames],
        )
        self.addCleanup(book.close)
        return book

    def plan(self, book):
        return awx.build_execution_plan(book.active, self.client, self.settings)

    def set_row(self, book, number, **values):
        columns = awx.excel.read_columns(book.active)
        for name, value in values.items():
            book.active.cell(number, columns[name.casefold()]).value = value

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

    def test_same_template_and_variables_are_split_by_os(self):
        self.settings['BATCH_SIZE'] = 10
        book = self.workbook(['win1', 'linux1', 'aix1', 'win2'])
        for number, os in enumerate(('WINDOWS', 'linux', 'aix', 'window'), 2):
            self.set_row(book, number, ostype=os)
        batches, _, _, _ = self.plan(book)
        self.assertEqual([batch['ostype'] for batch in batches], ['windows', 'linux', 'aix'])
        self.assertEqual([batch['limit'] for batch in batches],
                         ['localhost,win1,win2', 'localhost,linux1', 'localhost,aix1'])

    def test_launches_match_excel_variables_and_full_credentials(self):
        book = self.workbook(['host1', 'host2', 'host3', 'host4'])
        for number, version, references in ((2, 1, 'a, b'), (3, 1, 'b, a'),
                                             (4, 2, 'a, b'), (5, 1, 'a, c')):
            self.set_row(book, number, job_variables=json.dumps({'version': version, 'action': 'install'}),
                         full_credential=references)
        batches, existing, column, _ = self.plan(book)
        self.assertEqual(len(batches), 3)
        for number, name in enumerate(('a', 'b', 'c'), 101):
            self.client.credential_cache[name] = {'id': number, 'credential_type': number, 'kind': 'cloud', 'inputs': {}}
        awx.resolve_credentials(self.client, batches, template_cache={7: ({'defaults': {'credentials': []}}, [], {})})
        self.client.post.side_effect = [{'job': number, 'artifacts': {}} for number in (99, 100, 101)]
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(io.StringIO()):
                awx.execute_plan(book, book.active, Path(directory) / 'execution.xlsx', self.client,
                                 self.settings, batches, existing, column)
        payloads = [call.args[1] for call in self.client.post.call_args_list]
        self.assertEqual([payload['limit'] for payload in payloads],
                         ['localhost,host1,host2', 'localhost,host3', 'localhost,host4'])
        self.assertEqual([payload['extra_vars']['version'] for payload in payloads], [1, 2, 1])
        self.assertEqual([payload['credentials'] for payload in payloads], [[101, 102], [101, 102], [101, 103]])
        self.assertEqual(book.active.cell(2, column).value, book.active.cell(3, column).value)
        self.assertIn('/100/', book.active.cell(4, column).value)
        self.assertIn('/101/', book.active.cell(5, column).value)

    def test_same_hostname_with_different_variables_is_a_separate_run(self):
        book = self.workbook(['host1', 'host1'])
        self.set_row(book, 2, job_variables='{"action": "install", "version": 1}')
        self.set_row(book, 3, job_variables='{"action": "install", "version": 2}')
        batches, _, _, _ = self.plan(book)
        self.assertEqual(len(batches), 2)
        self.assertEqual([batch['entries'][0]['rows'] for batch in batches], [[2], [3]])
        self.assertEqual([batch['variables']['version'] for batch in batches], [1, 2])

    def test_excel_variables_action_has_priority_over_stale_job_type(self):
        book = self.workbook(['host1', 'host2'])
        self.settings['JOB_TYPE'] = 'reinstall'
        self.set_row(book, 2, job_variables='{"action": "update"}', job_type='install')
        self.set_row(book, 3, job_variables='{"action": "install"}', job_type='install')
        batches, _, _, _ = self.plan(book)
        self.assertEqual([batch['variables']['action'] for batch in batches], ['update', 'install'])

    def test_excel_link_has_priority_over_configured_job_url(self):
        book = self.workbook(['host1'])
        self.settings['JOB_URL'] = 'https://awx.example.test/api/v2/job_templates/8/'
        batches, _, _, _ = self.plan(book)
        self.assertEqual(batches[0]['template_id'], 7)
        self.assertFalse(batches[0]['job_url_overridden'])

    def test_template_name_or_hyperlink_mismatch_stops_before_launch(self):
        for mismatch in ('name', 'hyperlink'):
            with self.subTest(mismatch=mismatch):
                book = self.workbook(['host1'])
                if mismatch == 'name':
                    self.set_row(book, 2, template_job_name='Different job')
                else:
                    columns = awx.excel.read_columns(book.active)
                    book.active.cell(2, columns['job_link']).hyperlink = 'https://awx.example.test/api/v2/job_templates/8/'
                with self.assertRaisesRegex(awx.AWXError, 'template_job_name' if mismatch == 'name' else 'hyperlink'):
                    self.plan(book)
                self.client.post.assert_not_called()

    def test_distinct_template_ids_with_identical_names_are_separate(self):
        book = self.workbook(['host1', 'host2'])
        self.set_row(book, 3, job_link='https://awx.example.test/api/v2/job_templates/8/')
        batches, _, _, _ = self.plan(book)
        self.assertEqual([batch['template_id'] for batch in batches], [7, 8])
        self.assertEqual([batch['template_job_name'] for batch in batches], ['Install job', 'Install job'])

    def test_ui_and_api_links_to_the_same_template_share_a_batch(self):
        book = self.workbook(['host1', 'host2'])
        self.set_row(book, 2, job_link='https://awx.example.test/#/templates/job_template/7/details')
        columns = awx.excel.read_columns(book.active)
        book.active.cell(2, columns['job_link']).hyperlink = TEMPLATE_URL
        batches, _, _, _ = self.plan(book)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]['limit'], 'localhost,host1,host2')

    def test_template_definition_is_checked_once_per_id(self):
        book = self.workbook(['host1', 'host2'])
        self.client.template_detail_cache.clear()
        self.client.get = Mock(return_value={'id': 7, 'name': 'Install job'})
        self.plan(book)
        self.client.get.assert_called_once()
        self.assertEqual(self.client.get.call_args.args[0], 'job_templates/7/')

    def test_explicit_job_url_override_updates_name_and_link_in_excel(self):
        book = self.workbook(['host1'])
        link = 'https://awx.example.test/api/v2/job_templates/8/'
        self.client.template_detail_cache[8]['name'] = 'Explicit replacement job'
        batches, existing, column, _ = awx.build_execution_plan(
            book.active, self.client, self.settings, job_url_override=link)
        batches[0]['credentials'] = []
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(io.StringIO()):
                awx.execute_plan(book, book.active, Path(directory) / 'execution.xlsx', self.client,
                                 self.settings, batches, existing, column)
        columns = awx.excel.read_columns(book.active)
        self.assertEqual(self.client.post.call_args.args[0], 'job_templates/8/launch/')
        self.assertEqual(book.active.cell(2, columns['job_link']).value, link)
        self.assertEqual(book.active.cell(2, columns['job_link']).hyperlink.target, link)
        self.assertEqual(book.active.cell(2, columns['template_job_name']).value, 'Explicit replacement job')


if __name__ == '__main__':
    unittest.main()
