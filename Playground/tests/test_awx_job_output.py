"""Output collector tests use a local fake AWX and temporary Excel/text files."""
import contextlib
import io
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch
from lib import awx as collector

class FakeAWX(BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def respond(self, status, body, content_type='application/json'):
        data = body.encode('utf-8') if isinstance(body, str) else json.dumps(body).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.server.calls.append(self.path)
        if self.headers.get('Authorization') != 'Bearer test-token':
            self.respond(401, {})
            return
        if self.path == '/api/v2/inventories/5/':
            self.respond(200, {'id': 5, 'name': 'source-inventory'})
            return
        parts = self.path.split('/')
        job = self.server.jobs.get(parts[4]) if len(parts) > 4 else None
        if job is None:
            self.respond(404, {'detail': 'not found'})
        elif 'stdout/' in self.path:
            self.respond(job.get('stdout_status', 200), job['stdout'], job.get('content_type', 'text/plain; charset=utf-8'))
        else:
            statuses = job.setdefault('statuses', ['successful'])
            status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
            self.respond(200, {'id': int(parts[4]), 'status': status, 'inventory': 5, 'summary_fields': job.get('summary_fields', {'inventory': {'id': 5, 'name': 'test_inventory'}})})

class OutputTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), FakeAWX)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'hosts.xlsx'
        self.output_directory = Path(self.directory.name) / 'output'
        self.connection = deepcopy(collector.INVENTORY_DEFAULTS)
        self.connection.update(AWX_URL=self.base, AWX_TOKEN='test-token', TIMEOUT=2)
        self.settings = deepcopy(collector.OUTPUT_DEFAULTS)
        self.settings.update(OUTPUT_DIRECTORY=str(self.output_directory), POLL_INTERVAL=0.001)
        self.launch_settings = deepcopy(collector.EXECUTION_DEFAULTS)
        self.server.calls = []
        self.server.jobs = {'101': {'stdout': 'PLAY [all]\nLinux output: zaĹĽĂłĹ‚Ä‡\n'}, '102': {'stdout': 'Windows output\n'}}
        self.client = collector.AWXClient(self.base, 'test-token')
        self.console = io.StringIO()
        self.addCleanup(collector.apply_settings, collector.INVENTORY_DEFAULTS)

    def link(self, job_id):
        return f"{self.base}/#/jobs/playbook/{job_id}/output"

    def save_rows(self, rows, headers=None):
        workbook = collector.Workbook()
        sheet = workbook.active
        sheet.title = 'Results'
        sheet.append(headers or ['hostname', 'ostype', 'exceiute_job', 'output'])
        for row in rows:
            sheet.append(row)
        if headers is None:
            for row in range(2, sheet.max_row + 1):
                if str(sheet.cell(row, 3).value or '').startswith('http'):
                    sheet.cell(row, 3).hyperlink = sheet.cell(row, 3).value
        workbook.save(self.path)
        workbook.close()

    def run_main(self, *args):
        with patch.object(collector, 'read_inventory_settings', return_value=self.connection), patch.object(collector, 'read_execution_settings', return_value=self.launch_settings), patch.object(collector, 'read_output_settings', return_value=self.settings), contextlib.redirect_stdout(self.console), contextlib.redirect_stderr(self.console):
            return collector.output_main(['-file', str(self.path), *args])

    def cell(self, sheet, row, name):
        return sheet.cell(row, collector.excel.read_columns(sheet)[name.casefold()])

    def records(self):
        values = self.read_values()
        return {row[0]: dict(zip(values[0], row)) for row in values[1:]}

    def read_values(self):
        workbook = collector.load_workbook(self.path)
        values = list(workbook.active.values)
        workbook.close()
        return values

    def test_download_once_per_job_populates_excel_and_utf8_files(self):
        self.save_rows([['linux-1', 'linux', self.link(101), None], ['linux-2', 'LINUX', self.link(101), None], ['win-1', 'windows', self.link(102), None]])
        self.assertEqual(self.run_main(), 0)
        stdout_calls = [path for path in self.server.calls if 'stdout/' in path]
        self.assertEqual(sorted(stdout_calls), ['/api/v2/jobs/101/stdout/?format=txt_download', '/api/v2/jobs/102/stdout/?format=txt_download'])
        self.assertEqual((self.output_directory / '101.test_inventory.linux.txt').read_bytes(), self.server.jobs['101']['stdout'].encode('utf-8'))
        values = self.read_values()
        records = self.records()
        self.assertEqual(records['linux-1']['output'], records['linux-2']['output'])
        self.assertEqual(records['linux-1']['output'], self.server.jobs['101']['stdout'])
        self.assertEqual([row[0] for row in values[1:]], ['win-1', 'linux-1', 'linux-2'])
        workbook = collector.load_workbook(self.path)
        self.assertEqual(self.cell(workbook.active, 2, 'exceiute_job').hyperlink.target, self.link(102))
        workbook.close()

    def test_adds_output_column_and_handles_same_job_with_multiple_os_values(self):
        self.save_rows([['linux', self.link(101)], ['aix', self.link(101)]], headers=['ostype', 'exceiute_job'])
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.read_values()[0], tuple(collector.excel.headers()))
        self.assertTrue((self.output_directory / '101.test_inventory.linux.txt').exists())
        self.assertTrue((self.output_directory / '101.test_inventory.aix.txt').exists())
        self.assertEqual(len([path for path in self.server.calls if 'stdout/' in path]), 1)

    def test_long_or_xml_invalid_output_remains_complete_in_text_file(self):
        stdout = '=literal\x00\x0b\n' + 'x' * 40000
        self.server.jobs['101']['stdout'] = stdout
        self.save_rows([['host-1', 'linux', self.link(101), None]])
        self.assertEqual(self.run_main(), 0)
        self.assertEqual((self.output_directory / '101.test_inventory.linux.txt').read_bytes(), stdout.encode())
        workbook = collector.load_workbook(self.path)
        cell = self.cell(workbook.active, 2, 'output')
        self.assertEqual(cell.data_type, 's')
        self.assertEqual(len(cell.value), collector.EXCEL_CELL_LIMIT)
        self.assertNotIn('\x00', cell.value)
        self.assertIn('Output truncated', cell.value)
        workbook.close()

    def test_waits_for_running_job_and_collects_failed_job_output(self):
        self.server.jobs['101']['statuses'] = ['running', 'failed']
        self.save_rows([['host-1', 'linux', self.link(101), None]])
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.server.calls.count('/api/v2/jobs/101/'), 2)
        self.assertEqual(self.read_values()[1][self.read_values()[0].index('output')], self.server.jobs['101']['stdout'])

    def test_inventory_name_fallback_and_invalid_filename_characters(self):
        self.server.jobs['101']['summary_fields'] = {}
        self.server.jobs['102']['summary_fields'] = {'inventory': {'name': '../bad:inventory?'}}
        self.save_rows([['host-1', 'linux', self.link(101), None], ['host-2', 'windows', self.link(102), None]])
        self.assertEqual(self.run_main(), 0)
        self.assertTrue((self.output_directory / '101.source-inventory.linux.txt').exists())
        self.assertTrue((self.output_directory / '102._bad_inventory_.windows.txt').exists())

    def test_skips_empty_and_pending_rows_and_repairs_schema_without_requests(self):
        self.save_rows([['h1', 'linux', None, None], ['h2', 'aix', 'LAUNCH_PENDING template=7', None]])
        original = self.path.read_bytes()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.read_values()[0], tuple(collector.excel.headers()))
        self.assertEqual(self.records()['h2']['exceiute_job'], 'LAUNCH_PENDING template=7')
        self.assertFalse(self.output_directory.exists())

    def test_failed_download_keeps_old_output_and_collects_other_jobs(self):
        self.server.jobs['101']['stdout_status'] = 403
        self.save_rows([['host-1', 'linux', self.link(101), 'previous output'], ['host-2', 'windows', self.link(102), None]])
        self.assertEqual(self.run_main(), 1)
        values = self.read_values()
        records = self.records()
        self.assertEqual(records['host-1']['output'], 'previous output')
        self.assertEqual(records['host-2']['output'], self.server.jobs['102']['stdout'])
        self.assertFalse((self.output_directory / '101.test_inventory.linux.txt').exists())

    def test_running_job_can_be_collected_without_waiting(self):
        self.settings['WAIT_FOR_COMPLETION'] = False
        self.server.jobs['101']['statuses'] = ['running']
        self.save_rows([['host-1', 'linux', self.link(101), None]])
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.server.calls.count('/api/v2/jobs/101/'), 1)

    def test_invalid_urls_rejected_before_requests(self):
        for link in ('https://other.example/#/jobs/playbook/101/output', self.base + '/#/templates/job_template/101/details'):
            with self.subTest(link=link):
                self.save_rows([['host-1', 'linux', link, None]])
                with self.assertRaises(collector.AWXError):
                    self.run_main()
                self.assertEqual(self.server.calls, [])

    def test_job_link_formats(self):
        for link in (self.link(101), self.base + '/jobs/playbook/101/output', self.base + '/api/controller/v2/jobs/101/'):
            self.assertEqual(collector.job_id_from_link(link, self.client), 101)

    def test_configuration_validation(self):
        for content in ("OUTPUT_DIRECTORY = ''", "WAIT_FOR_COMPLETION = 'yes'", 'POLL_INTERVAL = 0', 'UNKNOWN = 1'):
            with self.subTest(content=content):
                path = Path(self.directory.name) / 'variables'
                path.write_text(content)
                with self.assertRaises(collector.AWXError):
                    collector.read_output_settings(path)
if __name__ == '__main__':
    unittest.main()
