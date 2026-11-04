"""OS VERSION uses the selected host's cached Ansible facts without launching jobs."""

import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
    from lib import awx


class OSVersionTests(unittest.TestCase):
    def setUp(self):
        self.client = awx.AWXClient('https://awx.example.test/awx/api/v2/', 'offline-token')
        self.client.get = Mock(side_effect=AssertionError('Unexpected fact request'))
        self.client.post = Mock(side_effect=AssertionError('Unexpected launch'))

    def test_linux_merges_lsb_id_and_release_in_one_value(self):
        for facts in (
            {'ansible_lsb': {'id': 'Ubuntu', 'release': '22.04'}},
            {'lsb': {'id': ' Ubuntu ', 'release': ' 22.04 '}},
            {'ansible_facts': {'lsb': {'id': 'Ubuntu', 'release': '22.04'}}},
        ):
            with self.subTest(facts=facts):
                self.assertEqual(awx.os_version_from_facts('LINUX', facts), 'Ubuntu 22.04')
        self.assertEqual(awx.os_version_from_facts('linux', {'ansible_lsb': {'id': 'RedHat', 'release': '09.03'}}),
                         'RedHat 09.03')

    def test_windows_reads_distribution_from_prefixed_or_nested_facts(self):
        expected = 'Microsoft Windows Server 2022 Datacenter'
        for facts in (
            {'ansible_distribution': expected},
            {'distribution': expected},
            {'ansible_facts': {'ansible_distribution': expected}},
            {'ansible_distributtion': expected},
        ):
            with self.subTest(facts=facts):
                self.assertEqual(awx.os_version_from_facts('window', facts), expected)

    def test_missing_or_malformed_facts_do_not_invent_an_os_version(self):
        for facts in (None, [], {}, {'ansible_lsb': None}, {'ansible_lsb': {'id': [], 'release': False}}):
            with self.subTest(facts=facts):
                self.assertEqual(awx.os_version_from_facts('linux', facts), 'none')
        self.assertEqual(awx.os_version_from_facts('windows', {'ansible_distribution': {}}), 'none')
        self.assertEqual(awx.os_version_from_facts('aix', {'ansible_lsb': {'id': 'Ubuntu'}}), 'none')
        self.assertEqual(awx.os_version_from_facts('linux', {'ansible_lsb': {'id': 'Ubuntu'}}), 'Ubuntu')

    def test_related_source_host_facts_are_requested_once_and_shared_by_mirror_hosts(self):
        path = '/api/v2/hosts/91/ansible_facts/'
        self.client.get.side_effect = None
        self.client.get.return_value = {'ansible_lsb': {'id': 'Ubuntu', 'release': '22.04'},
                                        'ansible_distribution': 'Windows Server 2022'}
        cache = {}
        for number in (501, 502):
            host = {'id': number, 'name': f'mirror{number}', 'related': {'ansible_facts': path}}
            self.assertEqual(awx.host_os_version(self.client, host, 'linux', cache), 'Ubuntu 22.04')
        self.assertEqual(awx.host_os_version(self.client, host, 'windows', cache), 'Windows Server 2022')
        self.client.get.assert_called_once()
        self.assertEqual(self.client.get.call_args.args[0], path)
        self.client.post.assert_not_called()

    def test_unavailable_or_invalid_facts_warn_once_and_do_not_stop_other_metadata(self):
        host = {'id': 1, 'name': 'host1'}
        for failure in (awx.AWXError('HTTP 403: offline-token'), ['invalid facts']):
            with self.subTest(failure=failure), redirect_stderr(io.StringIO()) as captured:
                self.client.get.reset_mock()
                self.client.get.side_effect = failure if isinstance(failure, Exception) else None
                self.client.get.return_value = failure
                cache = {}
                self.assertEqual(awx.host_os_version(self.client, host, 'linux', cache), 'none')
                self.assertEqual(awx.host_os_version(self.client, host, 'linux', cache), 'none')
                self.client.get.assert_called_once()
                self.assertIn('OS VERSION = none', captured.getvalue())
                self.assertNotIn('offline-token', captured.getvalue())
        self.client.post.assert_not_called()

    def test_related_facts_on_another_server_are_rejected_before_requesting_them(self):
        host = {'id': 1, 'name': 'host1',
                'related': {'ansible_facts': 'https://other.example.test/api/v2/hosts/1/ansible_facts/'}}
        with redirect_stderr(io.StringIO()):
            self.assertEqual(awx.host_os_version(self.client, host, 'linux', {}), 'none')
        self.client.get.assert_not_called()
        self.client.post.assert_not_called()

    def test_inline_facts_and_unsupported_os_do_not_make_extra_requests(self):
        host = {'id': 1, 'name': 'host1', 'ansible_facts': {'ansible_lsb': {'id': 'Debian', 'release': '12'}}}
        self.assertEqual(awx.host_os_version(self.client, host, 'linux', {}), 'Debian 12')
        self.assertEqual(awx.host_os_version(self.client, {'id': 2, 'name': 'aix1'}, 'aix', {}), 'none')
        self.client.get.assert_not_called()
        self.client.post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
