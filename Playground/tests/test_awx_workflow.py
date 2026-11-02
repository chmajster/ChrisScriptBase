"""Full script-to-script workflow on temporary files and a local fake AWX API."""
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
from urllib.parse import parse_qs, urlsplit
from openpyxl.worksheet.table import Table
from lib import awx_config as common
from lib import awx_excel as excel
from lib import awx as runner
from lib import awx as inventory
from lib import awx as collector
from lib import awx as refresh
from tests.test_awx_refresh_excel import FakeAWX as InventoryResponses

class WorkflowAPI(BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def respond(self, body, status=200, content_type='application/json'):
        data = body.encode('utf-8') if isinstance(body, str) else json.dumps(body).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == '/api/v2/credentials/':
            name = parse_qs(parsed.query)['name'][0]
            names = {'shared-vault': 10, 'linux-key': 11, 'windows-key': 12}
            credential_id = names.get(name)
            matches = [{'id': credential_id, 'name': name, 'credential_type': 1 if credential_id == 10 else 2, 'kind': 'vault' if credential_id == 10 else 'ssh', 'inputs': {}}] if credential_id else []
            self.respond({'results': matches, 'next': None, 'count': len(matches)})
        elif parsed.path.endswith('/launch/'):
            self.respond({'ask_limit_on_launch': True, 'ask_variables_on_launch': True, 'ask_credential_on_launch': True, 'defaults': {'job_slice_count': 1}})
        elif parsed.path.endswith('/stdout/'):
            job_id = parsed.path.split('/')[4]
            self.respond(f"Job {job_id}: complete stdout\n", content_type='text/plain; charset=utf-8')
        elif '/jobs/' in parsed.path:
            self.respond({'status': 'successful', 'inventory': 1, 'summary_fields': {'inventory': {'name': 'test_inventory'}}})
        else:
            self.respond({'detail': 'Not found'}, 404)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.launches.append(payload)
        self.respond({'job': 101, 'ignored_fields': {}}, 201)

class WorkflowTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'hosts.xlsx'
        self.server = HTTPServer(('127.0.0.1', 0), WorkflowAPI)
        self.server.launches = []
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.connection = deepcopy(inventory.INVENTORY_DEFAULTS)
        self.connection.update(AWX_URL=self.base, AWX_TOKEN='test-token', INVENTORY=1, OUTPUT_FILE=self.root / 'hosts.json', EXCEL_OUTPUT_FILE=self.path, CREDENTIAL_VARIABLES=['os_credential'], EXTRA_CREDENTIAL_VARIABLES=['shared-vault'], OS_JOBS={os: {'url': f"{self.base}/#/templates/job_template/{index}/details", 'variables': {}} for index, os in enumerate(('windows', 'linux'), start=7)})
        self.execution = deepcopy(common.EXECUTION_DEFAULTS)
        self.execution.update(JOB_TYPE='install', BATCH_SIZE=10)
        self.outputs = deepcopy(common.OUTPUT_DEFAULTS)
        self.outputs['OUTPUT_DIRECTORY'] = str(self.root / 'output')
        self.fake_inventory = InventoryResponses()
        self.fake_inventory.request_count = 0
        self.fake_inventory.hosts = [host for host in self.fake_inventory.hosts if host['name'] in ('linux-1', 'windows-1')]
        original_get = self.fake_inventory.get

        def get(path, **kwargs):
            result = original_get(path, **kwargs)
            if 'linux-other' in result:
                result['linux-other']['hosts'] = []
            return result
        self.fake_inventory.get = get
        self.addCleanup(inventory.apply_settings, inventory.INVENTORY_DEFAULTS)

    def assert_layout(self, execution_column, output_column):
        workbook = excel.load_workbook(self.path)
        sheet = workbook['Results']
        names = [cell.value for cell in sheet[1]]
        self.assertEqual(names[:12], excel.headers(execution_column, output_column))
        self.assertEqual(len(names), len(set((name.casefold() for name in names))))
        self.assertEqual(sheet.freeze_panes, 'B2')
        self.assertFalse(sheet.sheet_view.showGridLines)
        self.assertEqual(sheet['A1'].fill.fgColor.rgb, '001F3A5F')
        self.assertEqual(sheet['A1'].font.color.rgb, '00FFFFFF')
        self.assertTrue(sheet['A1'].font.bold)
        self.assertEqual(sheet.auto_filter.ref, sheet.dimensions)
        self.assertEqual([sheet.cell(row, 1).value for row in (2, 3)], ['windows-1', 'linux-1'])
        columns = excel.read_columns(sheet)
        records = {sheet.cell(row, 1).value: {name: sheet.cell(row, index).value for name, index in columns.items()} for row in (2, 3)}
        workbook.close()
        return records

    def workflow(self, execution_column='exceiute_job', output_column='output', missing_inventory_header=False):
        self.execution['EXECUTION_COLUMN'] = execution_column
        self.outputs['EXECUTION_COLUMN'] = execution_column
        self.outputs['OUTPUT_COLUMN'] = output_column
        with patch.object(inventory, 'read_inventory_settings', side_effect=lambda *args: deepcopy(self.connection)), patch.object(common, 'read_execution_settings', side_effect=lambda *args: deepcopy(self.execution)), patch.object(common, 'read_output_settings', side_effect=lambda *args: deepcopy(self.outputs)), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with patch.object(inventory, 'AWXClient', return_value=self.fake_inventory):
                self.assertEqual(inventory.inventory_main([]), 0)
            generated = self.assert_layout(execution_column, output_column)
            workbook = excel.load_workbook(self.path)
            sheet = workbook['Results']
            columns = excel.read_columns(sheet)
            old_job = f"{self.base}/#/jobs/playbook/88/output"
            sheet.cell(2, columns[execution_column.casefold()]).value = old_job
            sheet.cell(2, columns[execution_column.casefold()]).hyperlink = old_job
            sheet.cell(2, columns[output_column.casefold()]).value = 'historical stdout'
            sheet.cell(1, sheet.max_column + 1).value = 'owner'
            sheet.cell(2, sheet.max_column).value = 'Windows team'
            workbook.create_sheet('Notes')['A1'] = 'keep this'
            if missing_inventory_header:
                sheet.cell(1, columns['inventory']).value = None
            for column in sorted((cell.column for cell in sheet[1] if cell.value in {'INVENTORY', 'Job_Link', 'Job_Variables', 'Full_Credential', 'Job_Type'}), reverse=True):
                sheet.delete_cols(column)
            for row in sheet:
                for cell in row:
                    if cell.hyperlink:
                        cell.hyperlink.ref = cell.coordinate
            sheet.add_table(Table(displayName='Hosts', ref=sheet.dimensions))
            workbook.save(self.path)
            workbook.close()
            with patch.object(inventory, 'AWXClient', return_value=self.fake_inventory):
                self.assertEqual(refresh.refresh_main([]), 0)
            refreshed = self.assert_layout(execution_column, output_column)
            for hostname in generated:
                for name in excel.METADATA_COLUMNS:
                    self.assertEqual(refreshed[hostname][name.casefold()], generated[hostname][name.casefold()])
            self.assertEqual(refreshed['windows-1'][execution_column.casefold()], old_job)
            self.assertEqual(refreshed['windows-1'][output_column.casefold()], 'historical stdout')
            self.assertEqual(refreshed['windows-1']['owner'], 'Windows team')
            self.assertEqual(runner.execute_main([]), 0)
            launched = self.assert_layout(execution_column, output_column)
            self.assertEqual(len(self.server.launches), 1)
            self.assertEqual(self.server.launches[0]['limit'], 'linux-1')
            self.assertEqual(self.server.launches[0]['credentials'], [10, 11])
            self.assertEqual(self.server.launches[0]['extra_vars'], {'action': 'install'})
            self.assertEqual(launched['windows-1'][execution_column.casefold()], old_job)
            self.assertEqual(collector.output_main([]), 0)
            collected = self.assert_layout(execution_column, output_column)
            self.assertEqual(collected['windows-1'][output_column.casefold()], 'Job 88: complete stdout\n')
            self.assertEqual(collected['linux-1'][output_column.casefold()], 'Job 101: complete stdout\n')
            self.assertEqual((self.root / 'output' / '101.test_inventory.linux.txt').read_text(), 'Job 101: complete stdout\n')
            workbook = excel.load_workbook(self.path)
            self.assertEqual(workbook['Notes']['A1'].value, 'keep this')
            sheet = workbook['Results']
            self.assertEqual(sheet.tables['Hosts'].ref, sheet.dimensions)
            self.assertEqual([column.name for column in sheet.tables['Hosts'].tableColumns], [cell.value for cell in sheet[1]])
            workbook.close()

    def test_all_four_scripts_share_the_same_schema_and_formatting(self):
        self.workflow()

    def test_all_four_scripts_respect_custom_execution_and_output_columns(self):
        self.workflow('launched_job', 'job_stdout')

    def test_missing_header_keeps_unlabelled_data_and_repairs_all_stages(self):
        self.workflow(missing_inventory_header=True)
if __name__ == '__main__':
    unittest.main()
