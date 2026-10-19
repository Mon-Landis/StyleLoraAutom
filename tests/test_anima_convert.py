from dataclasses import replace
from decimal import Decimal, Inexact, ROUND_DOWN, localcontext
import unittest

from stylelora.prompts.anima_convert import convert_anima_to_nai, parse_anima, scan_anima
from stylelora.prompts.nai_convert import ConversionPolicy, SourceSpan, WarningCode, convert_nai_to_anima


class AnimaConversionTests(unittest.TestCase):
    def test_artist_and_explicit_weights(self):
        result = convert_anima_to_nai('@foo bar, (@baz qux:1.5), (long hair:1.3), (cat:4)')
        self.assertEqual(result.text, 'artist:foo_bar, 1.5::artist:baz_qux::, 1.5::long_hair::, 6::cat::')
        self.assertFalse(result.warnings)

    def test_nested_and_square_attention(self):
        cases = {'((cat))': '1.35::cat::', '[cat]': '0.91::cat::',
                 '((cat:1.3):2)': '3.67::cat::', '(cat,dog:1.3)': '1.5::cat::, 1.5::dog::',
                 '([cat])': 'cat', '(cat)': '1.17::cat::'}
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(convert_anima_to_nai(source).text, expected)

    def test_escapes_and_nai_special_characters(self):
        result = convert_anima_to_nai(r'name \(series\), \[x\], a\,b, \\, {x}, a::b')
        self.assertEqual(result.text, r'name_(series), \[x\], a\,b, \\, \{x\}, a\:\:b')
        self.assertFalse(result.warnings)

    def test_natural_language_and_line_order(self):
        source = 'blue hair\nred eyes\n  A (cat:1.3) sits here.  \nlong hair\nA cat sits here\n(cat:1.3)\n'
        self.assertEqual(convert_anima_to_nai(source).text,
                         'blue_hair\nred_eyes\n  A (cat:1.3) sits here.  \nlong_hair\nA cat sits here\n1.5::cat::\n')
        for sentence in ('Hi! cat', 'Cat?', 'a b c d, cat'):
            self.assertEqual(convert_anima_to_nai(sentence).text, sentence)

    def test_low_weights_linear_and_drop(self):
        # U25: weights <= 1 are not rescaled; non-positive weights are dropped.
        result = convert_anima_to_nai('(cat:0.1), (dog:0), (bird:-1), (@foo:0.1)')
        self.assertEqual(result.text, '0.1::cat::, 0.1::artist:foo::')
        self.assertFalse(result.warnings)
        self.assertEqual(convert_anima_to_nai('(cat:0.4)').text, '0.4::cat::')
        self.assertEqual(convert_anima_to_nai('(cat:0.403)').text, '0.4::cat::')
        self.assertEqual(convert_anima_to_nai('(cat:0.7)').text, '0.7::cat::')

    def test_roundtrip_and_custom_ratio(self):
        source = '1.5::a_b::'
        middle = convert_nai_to_anima(source)
        self.assertEqual(middle.text, '(a b:1.3)')
        self.assertEqual(convert_anima_to_nai(middle.text).text, source)
        policy = ConversionPolicy(content_weight_ratio=Decimal('.5'))
        self.assertEqual(convert_nai_to_anima(source, policy=policy).text, '(a b:1.25)')
        self.assertEqual(convert_anima_to_nai('(a b:1.25)', policy=policy).text, source)

    def test_decimal_context_isolation(self):
        source = '[[cat]], ((dog:1.3)), (x:0.403)'
        expected = convert_anima_to_nai(source)
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = 3, ROUND_DOWN
            ctx.traps[Inexact] = True
            self.assertEqual(convert_anima_to_nai(source), expected)

    def test_recovery_limits_and_types(self):
        for source, code in [('(cat', WarningCode.W_UNCLOSED_BRACKET), ('cat)', WarningCode.W_UNMATCHED_CLOSE),
                             ('()', WarningCode.W_EMPTY_BLOCK), ('@', WarningCode.W_EMPTY_ARTIST),
                             ('cat\\', WarningCode.W_DANGLING_ESCAPE), ('(cat:NaN)', WarningCode.W_INVALID_NUMBER)]:
            with self.subTest(source=source):
                self.assertIn(code, [w.code for w in convert_anima_to_nai(source).warnings])
        for source, policy, code in [('abcd', ConversionPolicy(max_chars=3), WarningCode.W_INPUT_LIMIT),
                                     ('(((cat)))', ConversionPolicy(max_depth=2), WarningCode.W_DEPTH_LIMIT)]:
            result = convert_anima_to_nai(source, policy=policy)
            self.assertEqual((result.text, result.tokens), ('', ()))
            self.assertEqual([w.code for w in result.warnings], [code])
        for text, policy in [(None, None), (b'cat', None), ('cat', {})]:
            with self.assertRaises(TypeError):
                convert_anima_to_nai(text, policy=policy)

    def test_scanner_parser_contract(self):
        policy = ConversionPolicy()
        parsed = parse_anima(scan_anima('(cat:1.3)', policy=policy).items, policy=policy)
        self.assertEqual(parsed.items[0].raw_weight, Decimal('1.3'))
        self.assertEqual(parsed.items[0].span, SourceSpan(1, 4))

    def test_multiline_attention_and_blank_lines(self):
        result = convert_anima_to_nai('(cat,\ndog:1.3)\n\nA cat.\nblue hair')
        self.assertEqual(result.text, '1.5::cat::\n1.5::dog::\n\nA cat.\nblue_hair')
        self.assertFalse(result.warnings)
        self.assertEqual(result.tokens[1].span, SourceSpan(6, 9))

    def test_prefix_text_does_not_swallow_following_tags(self):
        cases = {'s (from above:1.2), x': 's, 1.33::from_above::, x',
                 's [cat], y': 's, 0.91::cat::, y',
                 'a\n\nb, c\n': 'a\n\nb, c\n'}
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(convert_anima_to_nai(source).text, expected)
        result = convert_anima_to_nai('(cat, dog\nbird')
        self.assertIn(WarningCode.W_UNCLOSED_BRACKET, [w.code for w in result.warnings])
        self.assertEqual(result.text, '1.17::cat::, 1.17::dog::\n1.17::bird::')

    def test_nai_numeric_weight_after_text(self):
        cases = {'s -1::from above::, -1::full body::, -1::crotchless::': 's',
                 's 1.5::from above::, x': 's, (from above:1.3), x',
                 'a\n\n1.5::b::\nc': 'a\n\n(b:1.3)\nc'}
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(convert_nai_to_anima(source).text, expected)
