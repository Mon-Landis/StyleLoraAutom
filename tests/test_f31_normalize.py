from decimal import Decimal, Inexact, ROUND_DOWN, localcontext
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from stylelora.prompts.anima_convert import convert_anima_to_nai
from stylelora.prompts.nai_convert import WarningCode
from stylelora.prompts.nai_metadata import extract_positive
from stylelora.prompts.prompt_normalize import normalize_prompt, render_for_endpoint
from stylelora.prompts.types import PromptError


class NormalizeTests(TestCase):
    def test_empty_converted_bodies_are_dropped_idempotently(self):
        for source, blocks in (('nai', ('{_}', '[___]', '2::__::', '{{_}}', '{ _ , cat }')),
                               ('anima', ('(_:1.3)', '(___)'))):
            for block in blocks:
                for text in (f'cat, {block}', f'{block}, cat', f'cat, {block}, dog'):
                    with self.subTest(source=source, text=text):
                        result = normalize_prompt(text, source)
                        self.assertEqual(normalize_prompt(result.text, 'nai').text, result.text)
                        self.assertNotIn('::::', result.text)
                        self.assertNotIn(', ,', result.text)
                        self.assertEqual(result.text, result.text.strip(' ,'))
        self.assertEqual(normalize_prompt('cat, {_}', 'nai').text, 'cat')
        self.assertEqual(normalize_prompt(r'a\,b, {_}, dog', 'nai').text, r'a\,b, dog')
        for text in ('{_}', '[__]', '2::_::'):
            self.assert_error(text, code='normalization:empty')

    def test_output_is_fully_reparsed(self):
        valid = normalize_prompt('cat', 'nai')
        from dataclasses import replace
        with patch('stylelora.prompts.prompt_normalize._render_storage',
                   return_value=replace(valid, text='cat, 1.05::::')):
            self.assert_error('cat')

    def test_numeric_looking_body_and_underscore_boundary(self):
        cases = {
            '3::3d::': '3::3d::',
            '._3::3d::': '. 3::3d::',
            '3::model2::': '3::model2::',
            '3::year_2025::': r'3::year \2025::',
            '2::3::3d::::': '6::3d::',
            '3::{3d}::': '3.15::3d::',
            '\n._3::3d::, 3::3d::\n\ncharacter_tag, character_tag  ':
                '\n. 3::3d::, 3::3d::\n\ncharacter tag, character tag  ',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                result = normalize_prompt(text, 'nai')
                self.assertEqual(result.text, expected)
                self.assertFalse(result.warnings)
                self.assertEqual(normalize_prompt(result.text, 'nai').text, expected)

    def test_user_sample_a(self):
        self.check_sample('23-48-53-4153890605.png')

    def test_user_sample_b(self):
        self.check_sample('naigen_nai_20261019-234218_832_1216.png')

    def check_sample(self, name):
        path = Path(__file__).resolve().parents[1] / 'devcache' / name
        if not path.exists():
            self.skipTest('optional user sample is absent')
        extracted = extract_positive(path.read_bytes())
        self.assertEqual(extracted.source, 'Comment.v4_prompt')
        result = normalize_prompt(extracted.text, 'nai')
        self.assertFalse(result.warnings)
        self.assertIn('3::3d::', result.text)
        self.assertEqual(result.text.count('\n'), extracted.text.count('\n'))
        self.assertEqual([line.strip() == '' for line in result.text.split('\n')],
                         [line.strip() == '' for line in extracted.text.split('\n')])
        self.assertEqual(normalize_prompt(result.text, 'nai').text, result.text)

    def assert_error(self, text, source='nai', code='normalization:syntax'):
        with self.assertRaises(PromptError) as caught:
            normalize_prompt(text, source)
        self.assertEqual(caught.exception.code, code)

    def test_nai_vectors_and_idempotence(self):
        cases = {
            '{{cat}}': '1.1025::cat::',
            '[cat]': '0.95238095::cat::',
            '{[cat]}': 'cat',
            '{2::cat::}': '2.1::cat::',
            '2::{cat}::': '2.1::cat::',
            '2::3::cat::::': '6::cat::',
            '100::cat::, 0::dog::, -2::bird::': '100::cat::, 0::dog::, -2::bird::',
            '\r\n year_2025, cat,cat  \r\n\r': '\n year 2025, cat,cat  \n\n',
            ' A {blue_cat} sits  here. ': ' A 1.05::blue cat:: sits  here. ',
            'a{b}c': 'a1.05::b::c',
            '{ cat ,\n dog  }': ' 1.05::cat:: ,\n 1.05::dog::  ',
            'artist:foo_bar, artist:foo_bar': 'artist:foo bar, artist:foo bar',
            r'a\,b, \{cat\}, \[x\], \\, a\:\:b': r'a\,b, \{cat\}, \[x\], \\, a\:\:b',
            r'artist\:literal': r'artist\:literal',
            '2::a\\\nb::': '2::a\\\nb::',
            '\ufeffcat': '\ufeffcat',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                result = normalize_prompt(text, 'nai')
                self.assertEqual(result.text, expected)
                again = normalize_prompt(result.text, 'nai')
                self.assertEqual(again.text, expected)
                self.assertFalse(again.warnings)

    def test_anima_source_and_legacy_default(self):
        cases = {
            '\n  (long hair:1.3), @foo_bar  \n': '\n  1.5::long hair::, artist:foo bar  \n',
            'A (blue_cat:1.3) sits here.': 'A 1.5::blue cat:: sits here.',
            'email a@b.com, @artist_name': 'email a@b.com, artist:artist name',
            r'\@literal, name \(series\)': '@literal, name (series)',
            '((cat))': '1.35::cat::',
            '(cat:0), (dog:-2)': '0::cat::, -2::dog::',
            '(cat,\ndog:1.3)': '1.5::cat::,\n1.5::dog::',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                result = normalize_prompt(text, 'anima')
                self.assertEqual(result.text, expected)
                self.assertEqual(normalize_prompt(result.text, 'nai').text, expected)
        self.assertEqual(convert_anima_to_nai('long hair').text, 'long_hair')
        self.assertEqual(convert_anima_to_nai('long hair', body_spaces=True).text, 'long hair')
        self.assertEqual(convert_anima_to_nai('(cat:0)').text, '')

    def test_rounding_and_context_isolation(self):
        for text, source in [('[cat]', 'nai'), ('1.000000005::cat::', 'nai'),
                             ('(cat)', 'anima'), ('(cat:0.403)', 'anima')]:
            expected = normalize_prompt(text, source)
            self.assertIn(WarningCode.W_NORMALIZATION_ROUNDED, [w.code for w in expected.warnings])
            with localcontext() as ctx:
                ctx.prec, ctx.rounding = 3, ROUND_DOWN
                ctx.traps[Inexact] = True
                self.assertEqual(normalize_prompt(text, source), expected)
        self.assertEqual(normalize_prompt('1.000000005::cat::', 'nai').text, '1.00000001::cat::')

    def test_invalid_structures(self):
        for text in ('{cat', 'cat}', '{[cat}]', '2::cat', '::cat',
                     '2::{cat::}', '{}', '2::::', 'NaN::cat::',
                     '1e9::cat::', 'cat\\', 'cat|dog', 'artist:'):
            with self.subTest(text=text):
                self.assert_error(text)
        for text in ('(cat', 'cat)', '([cat)]', '()', '(cat:NaN)', '@', 'cat\\'):
            with self.subTest(text=text):
                self.assert_error(text, 'anima')

    def test_limits_and_empty(self):
        for source in ('nai', 'anima'):
            self.assert_error('x' * 1000001, source, 'import:limit')
            self.assert_error(' \n\t', source, 'normalization:empty')
        self.assert_error('{' * 129 + 'x' + '}' * 129, code='import:limit')
        self.assert_error('(' * 129 + 'x' + ')' * 129, 'anima', 'import:limit')
        self.assert_error('1' * 65 + '::x::', code='import:limit')
        self.assert_error('(x:' + '1' * 65 + ')', 'anima', 'import:limit')
        self.assert_error('2::' * 129 + 'x' + '::' * 129, code='import:limit')
        self.assert_error('9' * 64 + '::{' + 'x}::', code='import:limit')
        self.assert_error('cat', 'auto', 'schema:invalid')
        self.assert_error(None, code='schema:invalid')

    def test_endpoint_and_storage_independence(self):
        stored = normalize_prompt('100::cat::, -2::dog::, 0::bird::, artist:foo_bar', 'nai')
        self.assertEqual(render_for_endpoint(stored.text, 'nai').text, stored.text)
        output = render_for_endpoint(stored.text, 'anima')
        self.assertEqual(output.text, '(cat:3.2), @foo bar')
        self.assertIn(WarningCode.W_WEIGHT_CLAMPED, [w.code for w in output.warnings])
        self.assertEqual(stored.tokens[0].weight, Decimal(100))
        self.assertIn('-2::dog::', stored.text)
        with self.assertRaises(PromptError) as caught:
            render_for_endpoint('-2::dog::', 'anima')
        self.assertEqual(caught.exception.code, 'normalization:empty')

    def test_conversion_failure_is_not_a_partial_result(self):
        with patch('stylelora.prompts.prompt_normalize.convert_anima_to_nai', side_effect=RuntimeError):
            self.assert_error('cat', 'anima', 'import:conversion_failed')
