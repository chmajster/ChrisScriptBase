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
            'EXECUTION_COLUMN': 'exceiute_job_link', 'OUTPUT_COLUMN': 'output',
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

    def inventory(self, hosts):
        self.client.items = Mock(return_value=[{'id': index, 'name': name}
                                               for index, name in enumerate(hosts, 1)])
        self.client.get = Mock(return_value={'_meta': {'hostvars': {
            name: {'ostype': os} for name, os in hosts.items()
        }}})
        return {7: ({'defaults': {'inventory': {'id': 10}}}, [], {})}

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

    def test_plan_recognizes_history_under_the_legacy_column_name(self):
        book = self.workbook(['host1'])
        column = awx.excel.read_columns(book.active)['exceiute_job_link']
        book.active.cell(1, column).value = 'exceiute_job'
        previous = 'https://awx.example.test/api/v2/jobs/90/'
        book.active.cell(2, column).value = previous
        batches, existing, _, _ = self.plan(book)
        self.assertEqual(batches, [])
        self.assertEqual(existing[0]['execution'], previous)
        self.assertNotIn('exceiute_job', awx.excel.read_columns(book.active))
        self.client.post.assert_not_called()

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
        self.client.get = Mock(return_value={'_meta': {'hostvars': {
            'Host1': {'ostype': 'linux'}, 'HOST2': {'ostype': 'linux'},
        }}})
        cache = {7: ({'defaults': {'inventory': {'id': 10}}}, [], {})}
        awx.resolve_execution_hostnames(self.client, batches, cache)
        self.assertEqual(batches[0]['limit'], 'localhost,Host1,HOST2')
        self.assertNotIn('localhost', self.client.items.call_args.kwargs['name__iregex'].casefold())
        self.client.get.assert_called_once()
        self.assertEqual(self.client.get.call_args.args[0], 'inventories/10/script/?all=1&hostvars=1')
        # Older launch responses use the inventory from the template definition.
        batches[0]['limit'] = 'Host1,HOST2'
        self.client.template_detail_cache[7]['inventory'] = 10
        self.client.items.reset_mock()
        awx.resolve_execution_hostnames(self.client, batches, {7: ({'defaults': {}}, [], {})})
        self.assertEqual(batches[0]['limit'], 'localhost,Host1,HOST2')
        self.client.items.assert_called_once()

    def test_different_metadata_creates_separate_jobs_for_the_same_template(self):
        self.settings['BATCH_SIZE'] = 10
        for column, first, second in (
            ('inventory', 'app_inventory', 'other_inventory'),
            ('applicationid', 'app', 'other'),
            ('hops', '1_hop (HOPS: 1)', '2_hop (HOPS: 2)'),
            ('hops_count', 1, 2),
            ('os credential', 'machine-a', 'machine-b'),
            ('extra_credentials', 'vault-a', 'vault-b'),
            ('job_credentials', 'default-a', 'default-b'),
            ('jumphost_for_hops', 'jump-a', 'jump-b'),
            ('job_type', 'install', 'reinstall'),
        ):
            with self.subTest(column=column):
                book = self.workbook(['host1', 'host2'])
                self.set_row(book, 2, **{column: first})
                self.set_row(book, 3, **{column: second})
                batches, _, _, _ = self.plan(book)
                self.assertEqual(len(batches), 2)
                self.assertEqual([batch['template_id'] for batch in batches], [7, 7])
                self.assertEqual([batch['limit'] for batch in batches], ['localhost,host1', 'localhost,host2'])

    def test_same_hostname_with_different_metadata_is_a_separate_run(self):
        book = self.workbook(['host1', 'host1'])
        self.set_row(book, 2, inventory='app_inventory')
        self.set_row(book, 3, inventory='other_inventory')
        batches, _, _, _ = self.plan(book)
        self.assertEqual(len(batches), 2)
        self.assertEqual([batch['entries'][0]['rows'] for batch in batches], [[2], [3]])

    def test_credential_order_and_numeric_hop_format_do_not_split_equal_metadata(self):
        self.settings['BATCH_SIZE'] = 10
        book = self.workbook(['host1', 'host2'])
        for column in ('os credential', 'extra_credentials', 'job_credentials', 'jumphost_for_hops'):
            self.set_row(book, 2, **{column: 'a, b'})
            self.set_row(book, 3, **{column: 'b, a'})
        self.set_row(book, 2, hops_count=3)
        self.set_row(book, 3, hops_count='3.0')
        batches, _, _, _ = self.plan(book)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]['limit'], 'localhost,host1,host2')

    def test_json_order_is_ignored_but_nested_values_and_types_are_distinct(self):
        self.settings['BATCH_SIZE'] = 10
        book = self.workbook(['host1', 'host2', 'host3', 'host4'])
        for number, value in enumerate((
            '{"action":"install","options":{"retries":1,"enabled":true}}',
            '{"options":{"enabled":true,"retries":1},"action":"install"}',
            '{"action":"install","options":{"retries":"1","enabled":true}}',
            '{"action":"install","options":{"retries":1,"enabled":false}}',
        ), 2):
            self.set_row(book, number, job_variables=value)
        batches, _, _, _ = self.plan(book)
        self.assertEqual([batch['limit'] for batch in batches],
                         ['localhost,host1,host2', 'localhost,host3', 'localhost,host4'])

    def test_different_excel_inputs_stay_separate_after_a_shared_override(self):
        book = self.workbook(['host1', 'host2'])
        self.set_row(book, 2, job_variables='{"version": 1}')
        self.set_row(book, 3, job_variables='{"version": 2}')
        batches, _, _, _ = awx.build_execution_plan(book.active, self.client, self.settings,
                                                   survey_overrides={'version': 3})
        self.assertEqual(len(batches), 2)
        self.assertEqual([batch['variables']['version'] for batch in batches], [3, 3])

    def test_survey_answer_differences_create_separate_jobs(self):
        book = self.workbook(['host1', 'host2'])
        awx.excel.ensure_columns(book.active, awx.excel.read_columns(book.active), ['Survey_mode'])
        self.set_row(book, 2, survey_mode='install')
        self.set_row(book, 3, survey_mode='reinstall')
        batches, _, _, _ = self.plan(book)
        self.assertEqual([batch['variables']['mode'] for batch in batches], ['install', 'reinstall'])

    def test_notes_and_output_values_do_not_split_identical_launch_parameters(self):
        book = self.workbook(['host1', 'host2'])
        awx.excel.ensure_columns(book.active, awx.excel.read_columns(book.active), ['Uwagi'])
        self.set_row(book, 2, uwagi='first note', output='old output', artifacts='{"old":true}', awx_status='found')
        self.set_row(book, 3, uwagi='second note', output='other output', artifacts='{}', awx_status='')
        batches, _, _, _ = self.plan(book)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]['limit'], 'localhost,host1,host2')

    def test_os_version_is_informational_and_does_not_split_compatible_batches(self):
        book = self.workbook(['host1', 'host2'])
        self.set_row(book, 2, **{'os version': 'Ubuntu 22.04'})
        self.set_row(book, 3, **{'os version': 'RedHat 9.3'})
        batches, _, _, _ = self.plan(book)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]['limit'], 'localhost,host1,host2')

    def test_missing_or_invalid_os_prevents_plan_creation(self):
        for value in (None, '', 'none', 123, True, '=A1'):
            with self.subTest(value=value):
                book = self.workbook(['host1'])
                self.set_row(book, 2, ostype=value)
                with self.assertRaisesRegex(awx.AWXError, 'ostype'):
                    self.plan(book)
        self.client.post.assert_not_called()

    def test_missing_hosts_and_nonliteral_metadata_cannot_be_launched(self):
        for column, value in (('awx_status', 'not_found'), ('awx_status', 'no_hostname'),
                              ('applicationid', '=A1'), ('hops_count', -1)):
            with self.subTest(column=column, value=value):
                book = self.workbook(['host1'])
                self.set_row(book, 2, **{column: value})
                with self.assertRaises(awx.AWXError):
                    self.plan(book)
        self.client.post.assert_not_called()

    def test_duplicate_json_keys_cannot_silently_change_launch_variables(self):
        for value in ('{"action":"install","action":"delete"}', '{"options":{"version":1,"version":2}}'):
            with self.subTest(value=value):
                book = self.workbook(['host1'])
                self.set_row(book, 2, job_variables=value)
                with self.assertRaisesRegex(awx.AWXError, 'unique keys'):
                    self.plan(book)
        self.client.post.assert_not_called()

    def test_live_os_mismatch_prevents_launch_even_when_template_is_the_same(self):
        book = self.workbook(['host1', 'host2'])
        batches, _, _, _ = self.plan(book)
        cache = self.inventory({'host1': 'linux', 'host2': 'windows'})
        with self.assertRaisesRegex(awx.AWXError, "host2.*Excel ostype 'linux'.*AWX ostype 'windows'"):
            awx.resolve_execution_hostnames(self.client, batches, cache)
        self.client.post.assert_not_called()

    def test_live_os_is_read_once_per_inventory_across_separate_jobs(self):
        book = self.workbook(['host1', 'host2', 'host3'])
        self.set_row(book, 2, ostype='WINDOWS')
        self.set_row(book, 3, ostype='window')
        self.set_row(book, 4, ostype='linux', job_link='https://awx.example.test/api/v2/job_templates/8/')
        batches, _, _, _ = self.plan(book)
        cache = self.inventory({'host1': 'window', 'host2': 'Windows', 'host3': 'LINUX'})
        cache[8] = cache[7]
        awx.resolve_execution_hostnames(self.client, batches, cache)
        self.assertEqual([batch['limit'] for batch in batches], ['localhost,host1,host2', 'localhost,host3'])
        self.client.get.assert_called_once()
        self.client.items.assert_called_once()

    def test_unavailable_host_os_stops_instead_of_assuming_excel_is_correct(self):
        for value in (None, '', 'none', [], 123):
            with self.subTest(value=value):
                book = self.workbook(['host1'])
                batches, _, _, _ = self.plan(book)
                cache = self.inventory({'host1': value})
                with self.assertRaisesRegex(awx.AWXError, 'cannot verify ostype'):
                    awx.resolve_execution_hostnames(self.client, batches, cache)
        self.client.post.assert_not_called()

    def test_missing_template_inventory_cannot_bypass_os_verification(self):
        book = self.workbook(['host1'])
        batches, _, _, _ = self.plan(book)
        with self.assertRaisesRegex(awx.AWXError, 'configure its required inventory'):
            awx.resolve_execution_hostnames(self.client, batches, {7: ({'defaults': {}}, [], {})})
        self.client.post.assert_not_called()
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
        self.assertEqual(book.active.cell(2, column).value, 'https://awx.example.test/#/jobs/playbook/99/output')
        self.assertEqual(book.active.cell(2, column).hyperlink.target, book.active.cell(2, column).value)

    def test_job_links_open_web_ui_for_api_hash_and_path_template_urls(self):
        for source, expected in (
            (TEMPLATE_URL, 'https://awx.example.test/#/jobs/playbook/99/output'),
            ('https://awx.example.test/#/templates/job_template/7/details',
             'https://awx.example.test/#/jobs/playbook/99/output'),
            ('https://awx.example.test/templates/job_template/7/details',
             'https://awx.example.test/jobs/playbook/99/output'),
            ('https://awx.example.test/awx/api/v2/job_templates/7/',
             'https://awx.example.test/awx/#/jobs/playbook/99/output'),
            ('https://awx.example.test/awx/#/templates/job_template/7/details',
             'https://awx.example.test/awx/#/jobs/playbook/99/output'),
        ):
            with self.subTest(source=source):
                link = awx.job_url(source, self.client, 99)
                self.assertEqual(link, expected)
                self.assertEqual(awx.job_id_from_link(link, self.client), 99)

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
