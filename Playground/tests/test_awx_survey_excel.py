"""Editable survey columns preserve host answers across workbook repair/save."""

import json
import tempfile
import unittest
from pathlib import Path

from lib import awx_excel as excel
from lib import awx_survey_excel as survey_excel
from lib.awx_config import AWXError


class SurveyExcelTests(unittest.TestCase):
    def setUp(self):
        self.book = excel.Workbook()
        self.addCleanup(self.book.close)
        self.sheet = self.book.active
        self.sheet.append(['hostname', 'Job_Variables'])
        self.sheet.append(['host-1', '{}'])
        self.sheet.append(['host-2', '{}'])
        self.settings = {'EXECUTION_COLUMN': 'exceiute_job', 'OUTPUT_COLUMN': 'output', 'SURVEY_ANSWERS': {}}

    def question(self, variable='operation', kind='multiplechoice', **kwargs):
        return {'variable': variable, 'type': kind, 'required': True,
                'choices': ['install', 'reinstall', 'update'], 'default': 'install', **kwargs}

    def populate(self, questions, rows=None):
        cache = {7: ({'survey_enabled': True}, questions, {})}
        return survey_excel.populate_columns(self.sheet, {7: rows or [2, 3]}, cache, self.settings)

    def cell(self, row, name):
        return self.sheet.cell(row, excel.read_columns(self.sheet)[name.casefold()])

    def test_detected_questions_get_columns_config_defaults_and_dropdowns(self):
        self.settings['SURVEY_ANSWERS'] = {'operation': 'reinstall', 'undetected': 'ignored'}
        added = self.populate([self.question()])
        self.assertEqual(added, ['Survey_operation'])
        self.assertEqual(self.cell(2, 'Survey_operation').value, 'reinstall')
        self.assertNotIn('survey_undetected', excel.read_columns(self.sheet))
        self.assertIn('Variable: operation', self.cell(1, 'Survey_operation').comment.text)
        rule = self.sheet.data_validations.dataValidation[0]
        self.assertEqual(rule.formula1, '"install,reinstall,update"')
        self.assertEqual(str(rule.sqref), 'C2:C1048576')

    def test_edited_answers_are_kept_and_deleted_columns_are_restored(self):
        self.populate([self.question()])
        self.cell(2, 'Survey_operation').value = 'update'
        self.settings['SURVEY_ANSWERS'] = {'operation': 'reinstall'}
        self.assertEqual(self.populate([self.question()]), [])
        self.assertEqual(self.cell(2, 'Survey_operation').value, 'update')
        self.assertEqual(len(self.sheet.data_validations.dataValidation), 1)
        self.sheet.delete_cols(excel.read_columns(self.sheet)['survey_operation'])
        self.assertEqual(self.populate([self.question()]), ['Survey_operation'])
        self.assertEqual(self.cell(2, 'Survey_operation').value, 'reinstall')

    def test_job_variables_override_config_for_initial_values(self):
        self.sheet['B2'] = '{"operation":"update"}'
        self.settings['SURVEY_ANSWERS'] = {'operation': 'reinstall'}
        self.populate([self.question()])
        self.assertEqual(self.cell(2, 'Survey_operation').value, 'update')
        self.assertEqual(self.cell(3, 'Survey_operation').value, 'reinstall')

    def test_excel_defaults_apply_only_to_detected_questions(self):
        self.settings['SURVEY_ANSWERS'] = {'operation': 'install'}
        self.settings['SURVEY_EXCEL_DEFAULTS'] = {'operation': 'reinstall', 'other_template_answer': 'unused'}
        self.populate([self.question()])
        self.assertEqual(self.cell(2, 'Survey_operation').value, 'reinstall')
        self.assertNotIn('survey_other_template_answer', excel.read_columns(self.sheet))

    def test_refresh_mode_overwrites_only_explicitly_configured_answers(self):
        questions = [self.question(), self.question('note', 'text', default='default text')]
        self.populate(questions)
        self.cell(2, 'Survey_operation').value = 'update'
        self.cell(2, 'Survey_note').value = 'user text'
        self.sheet['B2'] = '{"operation":"install"}'
        self.settings['SURVEY_ANSWERS'] = {'operation': 'install'}
        self.settings['SURVEY_EXCEL_DEFAULTS'] = {'operation': 'reinstall', 'undetected': 'ignored'}
        cache = {7: ({}, questions, {})}
        survey_excel.populate_columns(self.sheet, {7: [2, 3]}, cache, self.settings, overwrite_configured=True)
        self.assertEqual(self.cell(2, 'Survey_operation').value, 'reinstall')
        self.assertEqual(self.cell(2, 'Survey_note').value, 'user text')
        self.assertNotIn('survey_undetected', excel.read_columns(self.sheet))

    def test_refresh_mode_uses_survey_answers_when_excel_defaults_have_no_key(self):
        questions = [self.question()]
        self.populate(questions)
        self.cell(2, 'Survey_operation').value = 'update'
        self.settings['SURVEY_ANSWERS'] = {'operation': 'reinstall'}
        survey_excel.populate_columns(self.sheet, {7: [2]}, {7: ({}, questions, {})}, self.settings,
                                      overwrite_configured=True)
        self.assertEqual(self.cell(2, 'Survey_operation').value, 'reinstall')

    def test_only_questions_for_each_rows_template_are_read(self):
        cache = {7: ({}, [self.question()], {}),
                 8: ({}, [self.question('environment', default='test', choices=['test', 'production'])], {})}
        survey_excel.populate_columns(self.sheet, {7: [2], 8: [3]}, cache, self.settings)
        self.assertEqual(self.cell(2, 'Survey_operation').value, 'install')
        self.assertIsNone(self.cell(2, 'Survey_environment').value)
        self.assertEqual(self.cell(3, 'Survey_environment').value, 'test')
        self.cell(2, 'Survey_environment').value = 'stale other template answer'
        self.assertEqual(survey_excel.read_answers(self.sheet, 2, cache[7][1]), {'operation': 'install'})

    def test_text_numbers_multiselect_and_stored_password_defaults_round_trip(self):
        questions = [self.question('count', 'integer', default=3),
                     self.question('ratio', 'float', default=2.5),
                     self.question('servers', 'multiselect', choices=['red', 'blue'], default='red\nblue'),
                     self.question('text', 'text', default='=literal'),
                     self.question('private', 'password', default='$encrypted$')]
        self.populate(questions)
        self.assertEqual(self.cell(2, 'Survey_text').data_type, 's')
        self.cell(2, 'Survey_count').value = '7'
        self.cell(2, 'Survey_ratio').value = '3.5'
        values = survey_excel.read_answers(self.sheet, 2, questions)
        self.assertEqual(values, {'count': 7, 'ratio': 3.5, 'servers': ['red', 'blue'],
                                  'text': '=literal', 'private': '$encrypted$'})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'hosts.xlsx'
            excel.prepare_sheet(self.sheet)
            excel.save_workbook(self.book, path)
            loaded = excel.load_workbook(path)
            try:
                self.assertEqual(survey_excel.read_answers(loaded.active, 2, questions), values)
            finally:
                loaded.close()

    def test_formula_and_wrong_types_are_rejected_without_disclosing_values(self):
        questions = [self.question('count', 'integer', default=1)]
        self.populate(questions)
        self.cell(2, 'Survey_count').value = '=1+1'
        with self.assertRaisesRegex(AWXError, 'Row 2.*formulas'):
            survey_excel.read_answers(self.sheet, 2, questions)
        self.cell(2, 'Survey_count').value = 'private-bad-value'
        with self.assertRaises(AWXError) as error:
            survey_excel.read_answers(self.sheet, 2, questions)
        self.assertNotIn('private-bad-value', str(error.exception))
        self.assertEqual(survey_excel.read_answers(self.sheet, 2, questions, skip_variables={'count'}), {})

    def test_long_or_complex_choices_use_header_instructions(self):
        self.populate([self.question(choices=['with,comma', 'with"quote', 'x' * 260], default='with,comma')])
        self.assertEqual(len(self.sheet.data_validations.dataValidation), 0)
        self.assertIn('with,comma', self.cell(1, 'Survey_operation').comment.text)

    def test_same_variable_with_different_template_choices_has_a_union_dropdown(self):
        cache = {7: ({}, [self.question(choices=['install'], default='install')], {}),
                 8: ({}, [self.question(choices=['update'], default='update')], {})}
        survey_excel.populate_columns(self.sheet, {7: [2], 8: [3]}, cache, self.settings)
        self.assertEqual(self.sheet.data_validations.dataValidation[0].formula1, '"install,update"')
        self.assertEqual(self.cell(2, 'Survey_operation').value, 'install')
        self.assertEqual(self.cell(3, 'Survey_operation').value, 'update')

    def test_reserved_names_are_prefixed_and_column_collisions_fail(self):
        self.populate([self.question('hostname', 'text', default='answer')])
        self.assertEqual(self.cell(2, 'hostname').value, 'host-1')
        self.assertEqual(self.cell(2, 'Survey_hostname').value, 'answer')
        self.settings['EXECUTION_COLUMN'] = 'Survey_operation'
        with self.assertRaisesRegex(AWXError, 'conflicts'):
            self.populate([self.question()])

    def test_case_collisions_are_reported_before_columns_are_added(self):
        with self.assertRaisesRegex(AWXError, 'differ only in case'):
            self.populate([self.question('Operation'), self.question('operation')])
        self.assertNotIn('survey_operation', excel.read_columns(self.sheet))

    def test_dropdown_target_moves_with_the_column_during_schema_repair(self):
        self.sheet.insert_cols(1)
        self.sheet['A1'] = 'Survey_operation'
        self.populate([self.question()])
        self.assertEqual(str(self.sheet.data_validations.dataValidation[0].sqref), 'A2:A1048576')
        excel.prepare_sheet(self.sheet)
        column = excel.read_columns(self.sheet)['survey_operation']
        letter = excel.get_column_letter(column)
        self.assertEqual(str(self.sheet.data_validations.dataValidation[0].sqref), f'{letter}2:{letter}1048576')
        self.assertEqual(self.cell(2, 'Survey_operation').value, 'install')

    def test_defaults_that_excel_would_truncate_or_corrupt_fail_clearly(self):
        for value in ('x' * 32768, 'text\x00value'):
            with self.subTest(length=len(value)), self.assertRaisesRegex(AWXError, 'cannot be represented'):
                self.populate([self.question('text', 'text', default=value)])


if __name__ == '__main__':
    unittest.main()
