import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_hosts


class TowerTests(unittest.TestCase):
    def opener(self, data):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = io.StringIO(json.dumps(data))
        return opener

    def test_parse_pasted_hosts(self):
        self.assertEqual(check_hosts.parse_hosts(" CXCX\tCXC\ncxczxc CXCX cxc "),
                         ["CXCX", "CXC", "cxczxc"])
        self.assertEqual(check_hosts.parse_hosts(" \n\t"), [])

    def test_exact_query_inventory_auth_and_timeout(self):
        opener = self.opener({"count": 3, "next": "/api/v2/hosts/?page=2"})
        self.assertTrue(check_hosts.host_exists(opener, "https://tower.example/", "secret",
                                               "host+01.example", 42, 7))
        request = opener.open.call_args.args[0]
        self.assertEqual(urlsplit(request.full_url).path, "/api/v2/hosts/")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {
            "name__iexact": ["host+01.example"], "page_size": ["1"], "inventory": ["42"],
        })
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 7)
        opener.open.assert_called_once()

    def test_missing_host(self):
        opener = self.opener({"count": 0, "results": []})
        self.assertFalse(check_hosts.host_exists(opener, "https://tower.example", "secret", "x"))
        self.assertNotIn("inventory", parse_qs(urlsplit(opener.open.call_args.args[0].full_url).query))

    def test_bad_responses_are_errors(self):
        for data in ({}, [], {"count": -1}, {"count": True}, {"count": "1"}):
            with self.subTest(data=data), self.assertRaises(check_hosts.TowerError):
                check_hosts.host_exists(self.opener(data), "https://tower.example", "secret", "x")
        opener = self.opener({})
        opener.open.return_value.__enter__.return_value = io.StringIO("<html>Login</html>")
        with self.assertRaises(check_hosts.TowerError):
            check_hosts.host_exists(opener, "https://tower.example", "secret", "x")

    def test_http_and_network_errors(self):
        for error in [HTTPError("url", code, "error", {}, None) for code in (301, 401, 403, 404, 500)] + [
            URLError("offline"), TimeoutError("timeout")
        ]:
            with self.subTest(error=error):
                opener = MagicMock()
                opener.open.side_effect = error
                with self.assertRaises(check_hosts.TowerError):
                    check_hosts.host_exists(opener, "https://tower.example", "secret", "x")

    def test_redirects_are_not_followed(self):
        self.assertIsNone(check_hosts.NoRedirect().redirect_request(
            None, None, 302, "Found", {}, "https://other.example"))

    def run_main(self, results, args=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"TOWER_URL": "https://tower.example", "TOWER_TOKEN": "secret"}), \
                patch("check_hosts.host_exists", side_effect=results) as lookup, \
                redirect_stdout(out), redirect_stderr(err):
            code = check_hosts.main(args or ["--hosts", "host01 host02 HOST01"])
        return code, out.getvalue(), err.getvalue(), lookup

    def test_main_success_and_missing(self):
        for results, expected in [([True, True], 0), ([True, False], 1)]:
            with self.subTest(results=results):
                code, out, err, lookup = self.run_main(results)
                self.assertEqual(code, expected)
                self.assertEqual(lookup.call_count, 2)
                self.assertIn("EXISTS: host01", out)
                self.assertIn("Summary: checked 2", out)
                self.assertEqual(err, "")
                if expected == 1:
                    self.assertIn("MISSING: host02", out)

    def test_api_error_stops_without_reporting_missing(self):
        code, out, err, lookup = self.run_main([check_hosts.TowerError("HTTP 403")])
        self.assertEqual(code, 2)
        lookup.assert_called_once()
        self.assertNotIn("MISSING", out)
        self.assertNotIn("Summary", out)
        self.assertIn("ERROR [host01]", err)
        self.assertNotIn("secret", err)

    def test_invalid_configuration_does_not_query_tower(self):
        for args in (["--hosts", " "], ["--url", "http://tower.example"],
                     ["--inventory", "0"], ["--timeout", "-1"],
                     ["--url", "https://user:password@tower.example"]):
            with self.subTest(args=args), self.assertRaises(SystemExit) as caught:
                self.run_main([], args)
            self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
