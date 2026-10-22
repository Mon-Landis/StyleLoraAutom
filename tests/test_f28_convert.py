"""F28 §8 vectors, including independent token and diagnostic expectations."""

from dataclasses import replace
from decimal import Context, Decimal, Inexact, ROUND_DOWN, ROUND_HALF_UP, localcontext
import unittest

from stylelora.prompts.nai_convert import (
    ConversionPolicy, ConversionResult, LexKind, SourceSpan, WarningCode, WeightedTag,
    compress_weights, convert_nai_to_anima, normalize_nai_tags, parse_nai, scan_nai,
)


P = ConversionPolicy()


def token(value, start, end, exponent=0, numeric='1', weight='1'):
    with localcontext(Context(prec=128, rounding=ROUND_HALF_UP)):
        raw = Decimal('1.05') ** exponent * Decimal(numeric)
    return value, raw, Decimal(weight), SourceSpan(start, end)


# Warning tuples specify code suffix and exact triggering source interval.
VECTORS = [
    (1, '', '', [], [], {}),
    (2, 'cat', 'cat', [token('cat', 0, 3)], [], {}),
    (3, '{cat}', '(cat:1.03)', [token('cat', 1, 4, 1, weight='1.03')], [], {}),
    (4, '[cat]', '(cat:0.95)', [token('cat', 1, 4, -1, weight='.95')], [], {}),
    (5, '{{{cat}}}', '(cat:1.09)', [token('cat', 3, 6, 3, weight='1.09')], [], {}),
    (7, '[[[cat]]]', '(cat:0.86)', [token('cat', 3, 6, -3, weight='.86')], [], {}),
    (9, '{[cat]}', 'cat', [token('cat', 2, 5)], [], {}),
    (10, '{a,[b],c}', '(a:1.03), b, (c:1.03)',
     [token('a', 1, 2, 1, weight='1.03'), token('b', 4, 5),
      token('c', 7, 8, 1, weight='1.03')], [], {}),
    (11, '1.5::cat::', '(cat:1.3)', [token('cat', 5, 8, numeric='1.5', weight='1.3')], [], {}),
    (12, '2::a,b::', '(a:1.6), (b:1.6)',
     [token('a', 3, 4, numeric='2', weight='1.6'), token('b', 5, 6, numeric='2', weight='1.6')], [], {}),
    (13, '2::a,b', '(a:1.6), (b:1.6)',
     [token('a', 3, 4, numeric='2', weight='1.6'), token('b', 5, 6, numeric='2', weight='1.6')],
     [('UNCLOSED_NUMERIC', 0, 3)], {}),
    (14, '{2::cat::}', '(cat:1.66)', [token('cat', 4, 7, 1, '2', '1.66')], [], {}),
    (15, '2::{cat}::', '(cat:1.66)', [token('cat', 4, 7, 1, '2', '1.66')], [], {}),
    (16, 'name (series)', r'name \(series\)', [token('name (series)', 0, 13)], [], {}),
    (17, r'name \(series\)', r'name \(series\)', [token('name (series)', 0, 15)], [], {}),
    (18, 'artist:xxx_(yyy)', r'@xxx \(yyy\)', [token('@xxx (yyy)', 0, 16)], [], {}),
    (19, '{artist:xxx_(yyy)}', r'(@xxx \(yyy\):1.05)',
     [token('@xxx (yyy)', 1, 17, 1, weight='1.05')], [], {}),
    (20, 'blue_hair', 'blue hair', [token('blue hair', 0, 9)], [], {}),
    (21, 'blue_hair', 'blue_hair', [token('blue_hair', 0, 9)], [], {'ordinary_underscores': 'preserve'}),
    (22, '-1::cat::,dog', 'dog', [token('dog', 10, 13)], [], {}),
    (23, '0::cat::', '', [], [], {}),
    (24, '0.01::cat::', '(cat:0.01)', [token('cat', 6, 9, numeric='.01', weight='.01')], [], {}),
    (25, '0.5::cat::', '(cat:0.5)', [token('cat', 5, 8, numeric='.5', weight='.5')], [], {}),
    (26, 'a,2::b::,5::c::', 'a, (b:1.6), (c:3.2)',
     [token('a', 0, 1), token('b', 5, 6, numeric='2', weight='1.6'),
      token('c', 12, 13, numeric='5', weight='3.2')], [('WEIGHT_CLAMPED', 12, 13)], {}),
    (27, '3.2::a::', '(a:2.32)', [token('a', 5, 6, numeric='3.2', weight='2.32')], [], {}),
    (28, '1.004::a::', 'a', [token('a', 7, 8, numeric='1.004')], [], {}),
    (29, '1.005::a::', 'a', [token('a', 7, 8, numeric='1.005')], [], {}),
    (30, '{cat', '(cat:1.03)', [token('cat', 1, 4, 1, weight='1.03')],
     [('UNCLOSED_BRACKET', 0, 1)], {}),
    (31, 'cat}', 'cat', [token('cat', 0, 3)], [('UNMATCHED_CLOSE', 3, 4)], {}),
    (32, '{cat]', '(cat:1.03)', [token('cat', 1, 4, 1, weight='1.03')],
     [('UNCLOSED_BRACKET', 0, 1), ('UNMATCHED_CLOSE', 4, 5)], {}),
    (33, r'\{cat\}', r'\{cat\}', [token('{cat}', 0, 7)], [], {}),
    (34, '{}', '', [], [('EMPTY_BLOCK', 0, 1)], {}),
    (35, '2::::', '', [], [('EMPTY_BLOCK', 0, 3)], {}),
    (36, '1e3::cat::', '1e3::cat::', [token('1e3::cat::', 0, 10)],
     [('INVALID_NUMBER', 0, 5), ('UNKNOWN_DCOLON', 8, 10)], {}),
    (37, 'NaN::cat::', 'NaN::cat::', [token('NaN::cat::', 0, 10)],
     [('INVALID_NUMBER', 0, 5), ('UNKNOWN_DCOLON', 8, 10)], {}),
    (38, 'a|b', 'a|b', [token('a|b', 0, 3)], [('UNSUPPORTED_ALTERNATION', 1, 2)], {}),
    (39, 'a::b', 'a::b', [token('a::b', 0, 4)], [('UNKNOWN_DCOLON', 1, 3)], {}),
    (40, ' a， b\nc,,', 'a, b\nc', [token('a', 0, 2), token('b', 3, 5), token('c', 6, 7)], [], {}),
    (41, 'a{b}c', 'a, (b:1.03), c',
     [token('a', 0, 1), token('b', 2, 3, 1, weight='1.03'), token('c', 4, 5)], [], {}),
    (42, '{{{cat}}}', '', [], [('DEPTH_LIMIT', 2, 3)], {'max_depth': 2}),
    (43, 'artist:', '', [], [('EMPTY_ARTIST', 0, 7)], {}),
    (44, 'cat,cat', 'cat, cat', [token('cat', 0, 3), token('cat', 4, 7)], [], {}),
    (45, r'a\,b', r'a\,b', [token('a,b', 0, 4)], [], {}),
    (46, 'abc', '', [], [('INPUT_LIMIT', 2, 3)], {'max_chars': 2}),
    (47, '{{{cat', '(cat:1.09)', [token('cat', 3, 6, 3, weight='1.09')],
     [('UNCLOSED_BRACKET', 0, 1), ('UNCLOSED_BRACKET', 1, 2), ('UNCLOSED_BRACKET', 2, 3)], {}),
    (48, 'a\\', 'a\\\\', [token('a\\', 0, 2)], [('DANGLING_ESCAPE', 1, 2)], {}),
    (49, '2::a 3::b', '(a 3:1.6), b',
     [token('a 3', 3, 6, numeric='2', weight='1.6'), token('b', 8, 9)], [], {}),
    (50, '[' * 20 + 'x' + ']' * 20, '(x:0.38)', [token('x', 20, 21, -20, weight='.38')], [], {}),
]


class F28Vectors(unittest.TestCase):
    def test_T06_additive_rejected(self):
        with self.assertRaises(ValueError):
            convert_nai_to_anima('{{{cat}}}', policy=replace(P, bracket_mode='additive'))

    @unittest.skip('T08 deprecated by Q01')
    def test_T08_deprecated(self):
        pass


def vector_test(row):
    def test(self):
        _, source, output, tokens, warnings, overrides = row
        result = convert_nai_to_anima(source, policy=replace(P, **overrides))
        self.assertEqual(result.text, output)
        self.assertEqual([(t.value, t.raw_weight, t.weight, t.span) for t in result.tokens], tokens)
        self.assertEqual(', '.join(t.rendered for t in result.tokens), output.replace('\n', ', '))
        self.assertEqual([(w.code, w.span) for w in result.warnings],
                         [(WarningCode('W_' + code), SourceSpan(start, end))
                          for code, start, end in warnings])
    return test


for row in VECTORS:
    setattr(F28Vectors, f'test_T{row[0]:02}', vector_test(row))


class F28Boundaries(unittest.TestCase):
    def test_determinism_and_default_policy(self):
        for _, source, *_ in VECTORS:
            with self.subTest(source=source):
                original = source[:]
                self.assertEqual(convert_nai_to_anima(source), convert_nai_to_anima(source, policy=P))
                self.assertEqual(source, original)

    def test_depth_boundary_and_atomic_failure(self):
        result = convert_nai_to_anima('{' * 128 + 'x' + '}' * 128)
        self.assertEqual(result.text, '(x:3.2)')
        self.assertEqual([w.code for w in result.warnings], [WarningCode.W_WEIGHT_CLAMPED])
        result = convert_nai_to_anima('|,cat,' + '{' * 129)
        self.assertEqual(result.text, '')
        self.assertEqual(result.tokens, ())
        self.assertEqual([w.code for w in result.warnings], [WarningCode.W_DEPTH_LIMIT])

    def test_input_boundary(self):
        self.assertEqual(convert_nai_to_anima(',' * 1000000), ConversionResult('', (), ()))
        result = convert_nai_to_anima('|' + ',' * 1000000)
        self.assertEqual(result.tokens, ())
        self.assertEqual([w.code for w in result.warnings], [WarningCode.W_INPUT_LIMIT])

    def test_numeric_boundary(self):
        for size in (64, 65):
            with self.subTest(size=size):
                source = '9' * size + '::x::'
                result = convert_nai_to_anima(source)
                self.assertEqual(result.text, '(x:3.2)' if size == 64 else source)
                self.assertEqual([w.code for w in result.warnings], [WarningCode.W_WEIGHT_CLAMPED] if size == 64 else
                                 [WarningCode.W_INVALID_NUMBER, WarningCode.W_UNKNOWN_DCOLON])

    def test_quantization_zero_and_hard_clamp(self):
        result = convert_nai_to_anima('-1::a::,0::b::,.0049::c::,.005::d::,.8::e::,9::f::')
        self.assertEqual(result.text, '(d:0.01), (e:0.8), (f:3.2)')
        self.assertEqual([w.code for w in result.warnings], [WarningCode.W_WEIGHT_CLAMPED])

    def test_compression_ratio_and_artist_clamp(self):
        tags = tuple(WeightedTag('x', Decimal(w), SourceSpan(0, 1))
                     for w in ('-1', '0', '.001', '.005', '.8', '1', '1.005', '3.2', '9'))
        first = compress_weights(tags, policy=P)
        self.assertEqual([t.weight for t in first.items], list(map(Decimal, ['.01', '.8', '1', '1', '2.32', '3.2'])))
        artists = compress_weights(tuple(WeightedTag('@x', t.raw_weight, t.span) for t in tags), policy=P)
        self.assertEqual([t.weight for t in artists.items], list(map(Decimal, ['.01', '.8', '1', '1.01', '3.2', '3.2'])))
        self.assertEqual([w.code for w in first.warnings], [WarningCode.W_WEIGHT_CLAMPED])
        self.assertEqual([w.code for w in artists.warnings], [WarningCode.W_WEIGHT_CLAMPED])

    def test_normalization_idempotence(self):
        for policy in (P, replace(P, ordinary_underscores='space')):
            tags = (WeightedTag(' artist:foo_bar_(x)  ', Decimal(1), SourceSpan(0, 22)),)
            first = normalize_nai_tags(tags, policy=policy)
            self.assertEqual(normalize_nai_tags(first.items, policy=policy), first)

    def test_decimal_context_isolation(self):
        source = '[[[x]]],1.005::y::'
        expected = convert_nai_to_anima(source)
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = 3, ROUND_DOWN
            ctx.traps[Inexact] = True
            self.assertEqual(convert_nai_to_anima(source), expected)

    def test_policy_validation(self):
        bad_values = {'bracket_mode': ['additive', 'unknown'], 'ordinary_underscores': ['bad'],
                      'max_weight': [Decimal('NaN'), Decimal('Infinity'), Decimal(1),
                                     Decimal('3.21'), Decimal('2.001')],
                      'content_weight_ratio': [Decimal('NaN'), Decimal('Infinity'), Decimal(0), Decimal('-1'), Decimal('1.01')],
                      'decimal_places': [1, 3], 'max_depth': [0, -1],
                      'max_chars': [0], 'max_numeric_chars': [0]}
        for name, values in bad_values.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    replace(P, **{name: value})
        for name, value in [('content_weight_ratio', .6), ('max_weight', 3.2), ('max_chars', True), ('max_depth', 1.5),
                            ('max_numeric_chars', '64'), ('decimal_places', 2.0),
                            ('bracket_mode', None), ('ordinary_underscores', 1)]:
            with self.subTest(name=name), self.assertRaises(TypeError):
                replace(P, **{name: value})
        for source, policy in [(None, P), (b'cat', P), ('cat', {}), ('cat', False)]:
            with self.assertRaises(TypeError):
                convert_nai_to_anima(source, policy=policy)

    def test_custom_max_weight(self):
        self.assertEqual(convert_nai_to_anima('4::a::', policy=replace(P, max_weight=Decimal('2.25'))).text,
                         '(a:2.25)')

    def test_lexical_priority_and_unicode_spans(self):
        source = '\ufeff猫\\,犬\r\n2::a,b::'
        lexed = scan_nai(source, policy=P)
        self.assertEqual([(t.kind, t.span) for t in lexed.items], [
            (LexKind.TEXT, SourceSpan(1, 2)), (LexKind.ESCAPED, SourceSpan(2, 4)),
            (LexKind.TEXT, SourceSpan(4, 5)), (LexKind.SEP, SourceSpan(5, 7)),
            (LexKind.NUM_OPEN, SourceSpan(7, 10)), (LexKind.TEXT, SourceSpan(10, 11)),
            (LexKind.SEP, SourceSpan(11, 12)), (LexKind.TEXT, SourceSpan(12, 13)),
            (LexKind.DCOLON, SourceSpan(13, 15)), (LexKind.EOF, SourceSpan(15, 15))])
        self.assertEqual(convert_nai_to_anima(source).text, '猫\\,犬\n(a:1.6), (b:1.6)')

    def test_candidate_positions_and_recovery(self):
        for source, output in [(' +.5::x::', '(x:0.5)'), ('2::x::3::y::', '(x:1.6), (y:2.2)'),
                               ('a 2::b', 'a, (b:1.6)'), ('1e3', '1e3'),
                               ('{[x}y]}', 'xy'), ('a\ufeffb', 'a\ufeffb')]:
            with self.subTest(source=source):
                self.assertEqual(convert_nai_to_anima(source).text, output)
        for candidate in ('--1', 'Infinity', 'nan', '1.', '1e3'):
            result = convert_nai_to_anima(candidate + '::x::')
            self.assertEqual([w.code for w in result.warnings],
                             [WarningCode.W_INVALID_NUMBER, WarningCode.W_UNKNOWN_DCOLON])

    def test_empty_blocks_and_warning_sort(self):
        result = convert_nai_to_anima('{{2::')
        self.assertEqual(result.text, '')
        self.assertEqual([(w.code.value, w.span.start) for w in result.warnings], [
            ('W_EMPTY_BLOCK', 0), ('W_UNCLOSED_BRACKET', 0),
            ('W_EMPTY_BLOCK', 1), ('W_UNCLOSED_BRACKET', 1),
            ('W_EMPTY_BLOCK', 2), ('W_UNCLOSED_NUMERIC', 2)])
        result = convert_nai_to_anima('0::artist: ::')
        self.assertIn(WarningCode.W_EMPTY_ARTIST, [w.code for w in result.warnings])

    def test_escaped_literals(self):
        result = convert_nai_to_anima('a\\|b,\\[x\\],a\\\nb,a\\，b,\\\\')
        self.assertEqual(result.text, 'a|b, \\[x\\], a b, a，b, \\\\')
        self.assertFalse(result.warnings)

    def test_underscore_is_a_numeric_weight_boundary(self):
        # `_` stands in for spaces in NAI metadata. A converter-path `_` before
        # a numeric weight must match the space-separated semantics.
        cases = [
            ('_1.8::x::', '(x:1.48)'),
            ('._1.8::x::', '., (x:1.48)'),
            ('a,_1.2::x::', 'a, (x:1.12)'),
            ('AABBQAQQA_1.5::nnnn::.', 'AABBQAQQA, (nnnn:1.3), .'),
            ('1_2::y::', '1, (y:1.6)'),
            ('_1.8::AAA::._1.6::DDD::._AABBQAQQA_1.5::nnnn::.',
             '(AAA:1.48), ., (DDD:1.36), . AABBQAQQA, (nnnn:1.3), .'),
        ]
        for source, output in cases:
            with self.subTest(source=source):
                result = convert_nai_to_anima(source)
                self.assertEqual(result.text, output)
                self.assertFalse(result.warnings)
        # Legal digit-containing words keep their existing behaviour.
        for source, output in [('year_2025', 'year 2025'), ('no_text', 'no text'),
                               ('3::3d::', '(3d:2.2)'), ('._3::3d::', '., (3d:2.2)')]:
            with self.subTest(source=source):
                self.assertEqual(convert_nai_to_anima(source).text, output)


        lexed = scan_nai('-1::x::', policy=P)
        parsed = parse_nai(lexed.items, policy=P)
        self.assertEqual(parsed.items, (WeightedTag('x', Decimal(-1), SourceSpan(4, 5)),))


if __name__ == '__main__':
    unittest.main()
