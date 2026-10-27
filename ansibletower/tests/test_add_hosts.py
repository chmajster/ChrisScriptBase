import io
import json
import sys
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import add_hosts
from tower_auth import TowerCredentials


class AddHostsTests(unittest.TestCase):
    def opener(self, data, status=201):
        opener = MagicMock()
        response = io.StringIO(json.dumps(data))
        response.status = status
        opener.open.return_value.__enter__.return_value = response
        return opener

    def test_creation_payload_auth_and_target(self):
        opener = self.opener({"id": 123, "name": "host01", "inventory": 42})
        self.assertEqual(add_hosts.create_host(opener, "https://tower.example/", "private-token",
                                              "host01", 42, 7), 123)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://tower.example/api/v2/hosts/")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"name": "host01", "inventory": 42, "enabled": True})
        self.assertEqual(request.get_header("Authorization"), "Bearer private-token")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 7)
        opener.open.assert_called_once()

    def test_unconfirmed_responses_are_errors(self):
        for data, status in [({}, 201), ([], 201), ({"id": True}, 201),
                             ({"id": 0, "name": "host01", "inventory": 42}, 201),
                             ({"id": 1, "name": "other", "inventory": 42}, 201),
                             ({"id": 1, "name": "host01", "inventory": 99}, 201),
                             ({"id": 1, "name": "host01", "inventory": 42}, 200)]:
            with self.subTest(data=data, status=status):
                opener = self.opener(data, status)
                with self.assertRaisesRegex(add_hosts.TowerError, "Creation may have completed"):
                    add_hosts.create_host(opener, "https://tower.example", "private-token", "host01", 42)
                opener.open.assert_called_once()

    def test_invalid_json_does_not_report_success(self):
        opener = self.opener({})
        opener.open.return_value.__enter__.return_value = io.StringIO("<html>Login</html>")
        opener.open.return_value.__enter__.return_value.status = 201
        with self.assertRaisesRegex(add_hosts.TowerError, "Invalid JSON"):
            add_hosts.create_host(opener, "https://tower.example", "private-token", "host01", 42)

    def test_http_failures_do_not_expose_response_body(self):
        for code in (301, 400, 401, 403, 404, 409, 500):
            with self.subTest(code=code):
                opener = MagicMock()
                opener.open.side_effect = HTTPError("url", code, "private-token", {},
                                                   io.BytesIO(b"private-token"))
                with self.assertRaises(add_hosts.TowerError) as caught:
                    add_hosts.create_host(opener, "https://tower.example", "private-token", "host01", 42)
                self.assertIn(str(code), str(caught.exception))
                self.assertNotIn("private-token", str(caught.exception))
                opener.open.assert_called_once()

    def test_connection_failure_is_not_retried(self):
        for error in (URLError("offline"), TimeoutError("timeout")):
            with self.subTest(error=error):
                opener = MagicMock()
                opener.open.side_effect = error
                with self.assertRaisesRegex(add_hosts.TowerError, "Creation may have completed"):
                    add_hosts.create_host(opener, "https://tower.example", "private-token", "host01", 42)
                opener.open.assert_called_once()

    def run_main(self, existing, args=None, create_result=123):
        out, err = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            credentials = stack.enter_context(patch("add_hosts.load_credentials", return_value=
                                                    TowerCredentials("https://tower.example", "private-token")))
            stack.enter_context(patch("add_hosts.build_opener"))
            exists = stack.enter_context(patch("add_hosts.host_exists", side_effect=existing))
            create = stack.enter_context(patch("add_hosts.create_host", return_value=create_result))
            if isinstance(create_result, (Exception, list)):
                create.side_effect = create_result
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            code = add_hosts.main(args if args is not None else
                                  ["--hosts", "host01 host02 HOST01", "--inventory", "42"])
        return code, out.getvalue(), err.getvalue(), exists, create, credentials

    def test_existing_hosts_skipped_and_duplicates_removed(self):
        code, out, err, exists, create, _ = self.run_main([True, False])
        self.assertEqual(code, 0)
        self.assertIn("SKIPPED (already exists): host01", out)
        self.assertIn("ADDED: host02 (ID 123)", out)
        self.assertIn("Summary: processed 2, added 1, skipped 1.", out)
        self.assertEqual(err, "")
        self.assertEqual(exists.call_count, 2)
        self.assertEqual(exists.call_args.args[3:6], ("host02", 42, 15))
        create.assert_called_once()
        self.assertEqual(create.call_args.args[1:], ("https://tower.example", "private-token", "host02", 42, 15))

    def test_dry_run_never_posts(self):
        code, out, err, _, create, _ = self.run_main(
            [False, True], ["--hosts", "host01 host02", "--inventory", "42", "--dry-run"])
        self.assertEqual(code, 0)
        create.assert_not_called()
        self.assertIn("WOULD ADD: host01", out)
        self.assertIn("Dry-run summary: checked 2, would add 1, skipped 1.", out)
        self.assertNotIn("ADDED:", out)
        self.assertEqual(err, "")

    def test_check_failure_stops_before_creation(self):
        code, out, err, exists, create, _ = self.run_main([add_hosts.TowerError("HTTP 403")])
        self.assertEqual(code, 2)
        exists.assert_called_once()
        create.assert_not_called()
        self.assertNotIn("ADDED:", out)
        self.assertIn("ERROR [host01]", err)

    def test_partial_addition_stops_at_first_failure(self):
        code, out, err, exists, create, _ = self.run_main(
            [False, False, False],
            ["--hosts", "host01 host02 host03", "--inventory", "42"],
            create_result=[123, add_hosts.TowerError("HTTP 400")])
        self.assertEqual(code, 2)
        self.assertEqual(exists.call_count, 2)
        self.assertEqual(create.call_count, 2)
        self.assertIn("ADDED: host01 (ID 123)", out)
        self.assertNotIn("ADDED: host02", out)
        self.assertNotIn("host03", out + err)
        self.assertNotIn("Summary:", out)
        self.assertIn("previously added hosts remain", err)
        self.assertNotIn("private-token", err)

    def test_input_validation(self):
        for args in ([], ["--inventory", "0"], ["--inventory", "42", "--hosts", " "],
                     ["--inventory", "42", "--hosts", "host01", "--timeout", "0"]):
            with self.subTest(args=args), self.assertRaises(SystemExit) as caught:
                self.run_main([], args)
            self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
