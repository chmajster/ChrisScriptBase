import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_hosts
import tower_auth


class CredentialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tower_credentials.json"
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_load_file_and_hide_token_from_repr(self):
        self.write({"url": "https://tower.example/", "token": "private-token"})
        credentials = tower_auth.load_credentials(credentials_file=self.path, prompt=False)
        self.assertEqual(credentials.url, "https://tower.example")
        self.assertEqual(credentials.token, "private-token")
        self.assertNotIn("private-token", repr(credentials))

    def test_environment_and_explicit_url_override_file(self):
        self.write({"url": "https://file.example", "token": "file-token"})
        with patch.dict(os.environ, {"TOWER_URL": "https://env.example", "TOWER_TOKEN": "env-token"}):
            credentials = tower_auth.load_credentials(credentials_file=self.path, prompt=False)
            self.assertEqual(credentials.url, "https://env.example")
            self.assertEqual(credentials.token, "env-token")
            credentials = tower_auth.load_credentials("https://cli.example", credentials_file=self.path)
            self.assertEqual(credentials.url, "https://cli.example")

    def test_missing_file_still_allows_environment(self):
        with patch.dict(os.environ, {"TOWER_URL": "https://env.example", "TOWER_TOKEN": "env-token"}):
            self.assertEqual(tower_auth.load_credentials(credentials_file=self.path).token, "env-token")

    def test_default_path_is_next_to_module(self):
        self.assertEqual(tower_auth.CREDENTIALS_FILE.parent, Path(tower_auth.__file__).parent)
        self.write({"url": "https://file.example", "token": "file-token"})
        with patch("tower_auth.CREDENTIALS_FILE", self.path):
            self.assertEqual(tower_auth.load_credentials(prompt=False).token, "file-token")

    def test_prompt_and_unattended_missing_token(self):
        self.write({"url": "https://file.example", "token": ""})
        with patch("tower_auth.getpass.getpass", return_value="entered-token") as prompt:
            self.assertEqual(tower_auth.load_credentials(credentials_file=self.path).token, "entered-token")
            prompt.assert_called_once_with("Tower API token: ")
        with patch("tower_auth.getpass.getpass") as prompt:
            with self.assertRaises(tower_auth.CredentialsError):
                tower_auth.load_credentials(credentials_file=self.path, prompt=False)
            prompt.assert_not_called()

    def test_invalid_files_and_urls_do_not_expose_secrets(self):
        invalid = [[], {"url": None}, {"token": 42},
                   {"url": "http://tower.example", "token": "private-token"},
                   {"url": "https://user:private-token@tower.example"},
                   {"url": "https://[invalid", "token": "private-token"},
                   {"url": "https://tower.example:bad", "token": "private-token"},
                   {"url": "https://tower.example", "token": "private-token\nheader"}]
        for data in invalid:
            with self.subTest(data=data):
                self.write(data)
                with self.assertRaises(tower_auth.CredentialsError) as caught:
                    tower_auth.load_credentials(credentials_file=self.path, prompt=False)
                self.assertNotIn("private-token", str(caught.exception))
        self.path.write_text('{"token": "private-token",', encoding="utf-8")
        with self.assertRaises(tower_auth.CredentialsError) as caught:
            tower_auth.load_credentials(credentials_file=self.path, prompt=False)
        self.assertNotIn("private-token", str(caught.exception))

    def test_host_checker_uses_shared_file(self):
        self.write({"url": "https://file.example", "token": "file-token"})
        out = io.StringIO()
        with patch("tower_auth.CREDENTIALS_FILE", self.path), \
                patch("check_hosts.host_exists", return_value=True) as lookup, \
                redirect_stdout(out), redirect_stderr(out):
            self.assertEqual(check_hosts.main(["--hosts", "host01"]), 0)
        self.assertEqual(lookup.call_args.args[1:4], ("https://file.example", "file-token", "host01"))
        self.assertNotIn("file-token", out.getvalue())
        self.assertIn("EXISTS: host01", out.getvalue())


if __name__ == "__main__":
    unittest.main()
