"""Survey validation and terminal answers without a live AWX server."""

import contextlib
import io
import unittest
from copy import deepcopy
from unittest.mock import patch

from lib import awx_survey as survey
from lib.awx_config import AWXError


class SurveyTests(unittest.TestCase):
    def question(self, kind='text', **values):
        return {'variable': 'answer', 'type': kind, 'required': True, **values}

    def test_supported_types_and_bounds(self):
        cases = [('text', 'hello', {'min': 1, 'max': 5}), ('textarea', 'a\nb', {'max': 4}),
                 ('password', 'secret', {'min': 6}), ('integer', 2, {'min': 1, 'max': 3}),
                 ('float', 2.5, {'min': 2, 'max': 3}), ('float', 2, {}),
                 ('multiplechoice', 'green', {'choices': 'red\ngreen\nblue'}),
                 ('multiselect', ['red', 'blue'], {'choices': ['red', 'green', 'blue']})]
        for kind, value, extra in cases:
            with self.subTest(kind=kind):
                survey.validate_answer(self.question(kind, **extra), value)

    def test_invalid_types_choices_and_ranges_are_rejected(self):
        cases = [('text', 1, {}), ('text', '', {'min': 1}), ('textarea', 'long', {'max': 3}),
                 ('password', 'secret', {'max': 3}), ('integer', True, {}), ('integer', '2', {}),
                 ('integer', 0, {'min': 1}), ('integer', 4, {'max': 3}), ('float', True, {}),
                 ('float', float('nan'), {}), ('float', float('inf'), {}),
                 ('multiplechoice', 'invalid', {'choices': ['red']}),
                 ('multiselect', 'red', {'choices': ['red']}),
                 ('multiselect', ['invalid'], {'choices': ['red']})]
        for kind, value, extra in cases:
            with self.subTest(kind=kind, value=value), self.assertRaises(AWXError):
                survey.validate_answer(self.question(kind, **extra), value)

    def test_defaults_fill_required_fields_and_keep_explicit_answers(self):
        questions = [self.question('multiplechoice', default='install', choices=['install', 'update']),
                     {'variable': 'count', 'type': 'integer', 'required': True, 'default': 3}]
        result = survey.prepare_variables(questions, {'answer': 'update'}, 7)
        self.assertEqual(result, {'answer': 'update', 'count': 3})
        self.assertEqual(survey.prepare_variables(questions, {}, 7)['answer'], 'install')

    def test_optional_fields_and_multiselect_defaults(self):
        questions = [{'variable': 'empty_text', 'type': 'text', 'required': False, 'min': 0},
                     {'variable': 'absent_text', 'type': 'textarea', 'required': False, 'min': 1},
                     {'variable': 'absent_number', 'type': 'integer', 'required': False, 'default': ''},
                     {'variable': 'selected', 'type': 'multiselect', 'required': True,
                      'default': 'red\nblue', 'choices': 'red\ngreen\nblue'}]
        self.assertEqual(survey.prepare_variables(questions, {}, 7), {'empty_text': '', 'selected': ['red', 'blue']})

    def test_missing_answers_report_all_variables_without_values(self):
        questions = [self.question(), {'variable': 'number', 'type': 'integer', 'required': True}]
        with self.assertRaises(AWXError) as error:
            survey.prepare_variables(questions, {}, 7)
        for text in ("'answer' is required", "'number' is required", 'SURVEY_ANSWERS', '--survey'):
            self.assertIn(text, str(error.exception))
        with self.assertRaises(AWXError) as error:
            survey.prepare_variables([self.question('password', max=1)], {'answer': 'private-value'}, 7)
        self.assertNotIn('private-value', str(error.exception))

    def test_encrypted_password_defaults_are_kept_as_awx_placeholders(self):
        question = self.question('password', default='$encrypted$', min=20)
        self.assertEqual(survey.prepare_variables([question], {}, 7), {'answer': '$encrypted$'})
        question['default'] = None
        with self.assertRaises(AWXError):
            survey.validate_answer(question, '$encrypted$')

    def test_malformed_specs_and_answer_dictionaries(self):
        for spec in ({}, {'spec': {}}, {'spec': [None]}, {'spec': [self.question(), self.question()]},
                     {'spec': [self.question('unknown')]}):
            with self.subTest(spec=spec), self.assertRaises(AWXError):
                survey.read_questions(spec, 7)
        for values in ([], False, {'': 1}, {1: 'value'}, {'answer': float('nan')}, {'answer': {1, 2}}):
            with self.subTest(values=values), self.assertRaises(AWXError):
                survey.answer_mapping(values)

    def test_interactive_choices_validate_and_reuse_answer_for_next_batch(self):
        question = self.question('multiplechoice', choices=['install', 'update'])
        prompted = {}
        with patch.object(survey.sys.stdin, 'isatty', return_value=True), \
             patch('builtins.input', side_effect=['invalid', '2']) as input_mock, \
             contextlib.redirect_stdout(io.StringIO()):
            first = survey.prepare_variables([question], {}, 7, interactive=True, prompted=prompted)
            second = survey.prepare_variables([question], {}, 7, interactive=True, prompted=prompted)
        self.assertEqual(first, {'answer': 'update'})
        self.assertEqual(second, first)
        self.assertEqual(input_mock.call_count, 2)

    def test_interactive_multiselect_numbers_and_stored_default(self):
        question = self.question('multiselect', choices=['red', 'green', 'blue'])
        with patch.object(survey.sys.stdin, 'isatty', return_value=True), \
             patch('builtins.input', return_value='1,3'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(survey.prepare_variables([question], {}, 7, interactive=True), {'answer': ['red', 'blue']})
        question = self.question('integer', default=2)
        with patch.object(survey.sys.stdin, 'isatty', return_value=True), \
             patch('builtins.input', return_value=''), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(survey.prepare_variables([question], {}, 7, interactive=True), {'answer': 2})

    def test_password_questions_use_hidden_input_and_never_show_defaults(self):
        question = self.question('password', default='private-default')
        with patch.object(survey.sys.stdin, 'isatty', return_value=True), \
             patch.object(survey.getpass, 'getpass', return_value='private-answer') as password_input, \
             patch('builtins.input', side_effect=AssertionError('Password must be hidden')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            result = survey.prepare_variables([question], {}, 7, interactive=True)
        password_input.assert_called_once()
        self.assertEqual(result, {'answer': 'private-answer'})
        self.assertNotIn('private-default', output.getvalue())
        self.assertNotIn('private-answer', output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            survey.display_questions(7, True, [question])
        self.assertNotIn('private-default', output.getvalue())

    def test_nonterminal_and_eof_fail_cleanly(self):
        with patch.object(survey.sys.stdin, 'isatty', return_value=False), self.assertRaisesRegex(AWXError, 'terminal'):
            survey.prepare_variables([self.question()], {}, 7, interactive=True)
        with patch.object(survey.sys.stdin, 'isatty', return_value=True), \
             patch('builtins.input', side_effect=EOFError), contextlib.redirect_stdout(io.StringIO()), \
             self.assertRaisesRegex(AWXError, 'input ended'):
            survey.prepare_variables([self.question()], {}, 7, interactive=True)

    def test_answers_and_specs_are_not_modified(self):
        values = {'answer': ['red']}
        question = self.question('multiselect', choices=['red', 'blue'])
        before = deepcopy((values, question))
        result = survey.prepare_variables([question], values, 7)
        result['answer'].append('blue')
        self.assertEqual((values, question), before)


if __name__ == '__main__':
    unittest.main()
