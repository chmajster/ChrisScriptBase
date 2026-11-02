"""Tolerant URL parsing without executing or rewriting the variables file."""

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from lib import awx_config as config


class ConfigParserTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "awx_variable"

    def read(self, content, *, windows=False):
        data = content.encode("utf-8")
        if windows:
            data = b"\xef\xbb\xbf" + data.replace(b"\n", b"\r\n")
        self.path.write_bytes(data)
        values = config.read_variables_file(self.path)
        self.assertEqual(self.path.read_bytes(), data)
        return values

    def test_posted_layout_accepts_blank_multiline_urls_for_all_systems(self):
        content = """# Job template URLs and extra_vars selected by host ostype.
OS_JOBS = {'windows': {'url': '

', 'variables': {

}},
 'linux': {'url': '

 ', 'variables': {

 }},
 'aix': {'url': '
 ', 'variables': {

 }}
 }
"""
        for windows in (False, True):
            with self.subTest(windows=windows):
                jobs = self.read(content, windows=windows)["OS_JOBS"]
                self.assertEqual(jobs, {os_name: {"url": "", "variables": {}}
                                        for os_name in ("windows", "linux", "aix")})

    def test_wrapped_connection_and_template_urls_remove_indentation(self):
        content = """AWX_URL = '
  https://awx.test/
  api/v2/
 '
JOB_URL = "
  https://awx.test/#/templates/
  job_template/14/details
"
OS_JOBS = {'linux': {'url': '
  https://awx.test/#/templates/job_template/7/details
 ', 'variables': {'action': 'install'}}}
"""
        values = self.read(content)
        self.assertEqual(values["AWX_URL"], "https://awx.test/api/v2/")
        self.assertEqual(values["JOB_URL"], "https://awx.test/#/templates/job_template/14/details")
        self.assertEqual(values["OS_JOBS"]["linux"]["url"], "https://awx.test/#/templates/job_template/7/details")
        self.assertEqual(values["OS_JOBS"]["linux"]["variables"], {"action": "install"})

    def test_single_double_triple_and_raw_url_literals(self):
        for delimiter in ("'", '"', "'''", '"""'):
            for prefix in ("", "r", "u"):
                with self.subTest(delimiter=delimiter, prefix=prefix):
                    content = "JOB_URL = " + prefix + delimiter + "\n https://awx.test/\n job_template/7/\n" + delimiter + "\n"
                    self.assertEqual(self.read(content)["JOB_URL"], "https://awx.test/job_template/7/")

    def test_escaped_newlines_and_adjacent_literals_are_normalized(self):
        values = self.read("JOB_URL = ('\\n https://awx.test/\\n'\n '#/templates/\\n'\n 'job_template/7/details\\n')\n")
        self.assertEqual(values["JOB_URL"], "https://awx.test/#/templates/job_template/7/details")

    def test_operation_overrides_use_the_same_url_normalization(self):
        values = self.read("""EXECUTE_OVERRIDES = {'JOB_URL': '
 https://awx.test/#/templates/job_template/8/details
 '}
REFRESH_OVERRIDES = {'OS_JOBS': {'linux': {'url': '
 https://awx.test/#/templates/job_template/9/details
 '}}}
""")
        self.assertEqual(values["EXECUTE_OVERRIDES"]["JOB_URL"], "https://awx.test/#/templates/job_template/8/details")
        self.assertEqual(values["REFRESH_OVERRIDES"]["OS_JOBS"]["linux"]["url"], "https://awx.test/#/templates/job_template/9/details")

    def test_job_variables_keep_multiline_text_even_when_named_url(self):
        variables = {"note": "first line\n  second line\n", "url": "\n  preserve this text\n"}
        content = "OS_JOBS = {'linux': {'url': '\n https://awx.test/\n', 'variables': " + repr(variables) + "}}\n"
        job = self.read(content)["OS_JOBS"]["linux"]
        self.assertEqual(job["url"], "https://awx.test/")
        self.assertEqual(job["variables"], variables)

    def test_comments_and_unrelated_triple_quoted_text_are_unchanged(self):
        note = "Example: 'url': '\n  this is documentation\n'\n"
        content = "# 'url': 'broken comment\nOS_JOBS = {'linux': {'url': '', 'variables': {'note': '''" + note + "'''}}}\n"
        self.assertEqual(self.read(content)["OS_JOBS"]["linux"]["variables"]["note"], note)

    def test_error_line_numbers_still_refer_to_original_file(self):
        content = "JOB_URL = '\n https://awx.test/\n'\n\nBATCH_SIZE =\n"
        with self.assertRaisesRegex(config.AWXError, r"line 5\."):
            self.read(content)

    def test_unfinished_url_and_invalid_non_url_literals_still_report_errors(self):
        for content in ("JOB_URL = '\n https://awx.test/\n", "INVENTORY = '\n inventory\n'\n",
                        "OS_JOBS = {'linux': {'variables': {'note': '\n text\n'}}}\n"):
            with self.subTest(content=content), self.assertRaisesRegex(config.AWXError, "Syntax error"):
                self.read(content)

    def test_configuration_expressions_are_never_executed(self):
        marker = Path(self.directory.name) / "must_not_exist"
        content = "JOB_URL = '\n https://awx.test/\n'\nBATCH_SIZE = __import__('pathlib').Path(" + repr(str(marker)) + ").write_text('executed')\n"
        with self.assertRaisesRegex(config.AWXError, "must use a literal value"):
            self.read(content)
        self.assertFalse(marker.exists())

    def test_launcher_overrides_preserve_input_and_extra_vars(self):
        variables = {"note": "first\nsecond", "url": "\n  extra_var\n"}
        overrides = {"JOB_URL": "\n https://awx.test/\n", "OS_JOBS": {
            "linux": {"url": "\n https://awx.test/job_template/7/\n", "variables": variables}}}
        before = deepcopy(overrides)
        normalized = config.normalize_overrides(overrides)
        self.assertEqual(normalized["JOB_URL"], "https://awx.test/")
        self.assertEqual(normalized["OS_JOBS"]["linux"]["url"], "https://awx.test/job_template/7/")
        self.assertEqual(normalized["OS_JOBS"]["linux"]["variables"], variables)
        normalized["OS_JOBS"]["linux"]["variables"]["note"] = "changed"
        self.assertEqual(overrides, before)


if __name__ == "__main__":
    unittest.main()
