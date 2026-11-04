"""Offline checks for the debug-only AWX connection and authentication probe."""

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
    from lib import awx


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.token = 'private-test-token'
        self.client = awx.AWXClient('https://awx.example.test/awx/api/v2/', self.token)
        self.client.opener = Mock()
        timeout = patch.object(awx, 'TIMEOUT', 3)
        timeout.start()
        self.addCleanup(timeout.stop)

    def response(self, body):
        response = io.BytesIO(json.dumps(body).encode('utf-8'))
        response.status = 200
        self.client.opener.open.return_value = response

    def test_disabled_debug_sends_no_request_and_prints_no_probe(self):
        captured = io.StringIO()
        with redirect_stdout(captured):
            awx.check_connection(self.client)
            awx.check_connection(self.client, debug=False)
        self.client.opener.open.assert_not_called()
        self.assertEqual(self.client.request_count, 0)
        self.assertEqual(captured.getvalue(), '')

    def test_debug_uses_one_authenticated_get_with_configured_api_prefix_and_timeout(self):
        self.response({'count': 1, 'results': [{'id': 7, 'username': 'operator', 'is_active': True}]})
        captured = io.StringIO()
        with redirect_stdout(captured):
            awx.check_connection(self.client, debug=True)
        self.client.opener.open.assert_called_once()
        request = self.client.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://awx.example.test/awx/api/v2/me/')
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.get_header('Authorization'), f'Bearer {self.token}')
        self.assertEqual(self.client.opener.open.call_args.kwargs['timeout'], 3)
        self.assertEqual(self.client.request_count, 1)
        self.assertIn('uwierzytelnienie: OK', captured.getvalue())
        self.assertIn('operator (ID: 7)', captured.getvalue())
        self.assertNotIn(self.token, captured.getvalue())

    def test_authentication_and_network_failures_stop_the_probe_and_hide_the_token(self):
        url = self.client._resolve_url('me/')
        for error in (
            HTTPError(url, 401, 'Unauthorized', None, io.BytesIO()),
            HTTPError(url, 403, 'Forbidden', None, io.BytesIO()),
            URLError('connection refused: ' + self.token),
            TimeoutError('connection timed out'),
        ):
            with self.subTest(error=error), redirect_stdout(io.StringIO()) as captured:
                self.client.opener.open.side_effect = error
                with self.assertRaises(awx.AWXError) as raised:
                    awx.check_connection(self.client, debug=True)
                self.assertIn('Operacja została zatrzymana', str(raised.exception))
                self.assertNotIn(self.token, str(raised.exception))
                self.assertNotIn(self.token, captured.getvalue())

    def test_successful_http_without_an_authenticated_active_user_is_rejected(self):
        for body in (
            {}, {'results': []}, {'results': [None]},
            {'results': [{'id': 1, 'username': ''}]},
            {'results': [{'id': True, 'username': 'operator'}]},
            {'results': [{'id': 1, 'username': 'operator', 'is_active': False}]},
            {'results': [{'id': 1, 'username': 'first'}, {'id': 2, 'username': 'second'}]},
        ):
            with self.subTest(body=body), redirect_stdout(io.StringIO()):
                self.response(body)
                with self.assertRaisesRegex(awx.AWXError, 'Test połączenia z AWX nie powiódł się'):
                    awx.check_connection(self.client, debug=True)


if __name__ == '__main__':
    unittest.main()
