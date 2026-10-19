from dataclasses import FrozenInstanceError, replace
import gzip
from io import BytesIO
import json
from pathlib import Path
import struct
import unittest
import zlib

from PIL import Image

from stylelora.prompts.nai_metadata import ExtractedPrompt, MetadataLimits, extract_positive


def chunk(kind, body):
    return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))


def png(*metadata, image=None):
    output = BytesIO()
    (image or Image.new('RGB', (2, 3))).save(output, format='PNG')
    raw = output.getvalue()
    return raw[:33] + b''.join(metadata) + raw[33:]


def text(key, value, kind=b'tEXt', *, compressed=False):
    encoded = value.encode('utf-8' if kind == b'iTXt' else 'latin-1')
    if kind == b'iTXt':
        body = bytes([int(compressed), 0]) + b'en-US\0' + '翻译标题'.encode() + b'\0'
        body += zlib.compress(encoded) if compressed else encoded
    elif kind == b'zTXt':
        body = b'\0' + zlib.compress(encoded)
    else:
        body = encoded
    return chunk(kind, key.encode() + b'\0' + body)


def comment(value, **kwargs):
    return text('Comment', json.dumps(value, ensure_ascii=True), **kwargs)


def v4(base='base', characters=('character',)):
    return {'prompt': base, 'v4_prompt': {'caption': {
        'base_caption': base, 'char_captions': [{'char_caption': c} for c in characters]}}}


def stealth(value=None, *, compressed=True, payload=None, bit_length=None, marker=None):
    if payload is None:
        payload = json.dumps(value, ensure_ascii=False).encode()
        if compressed:
            payload = gzip.compress(payload, mtime=0)
    marker = marker or (b'stealth_pngcomp' if compressed else b'stealth_pnginfo')
    data = marker + struct.pack('>I', len(payload) * 8 if bit_length is None else bit_length) + payload
    width = 32
    height = (len(data) * 8 + width - 1) // width
    image = Image.new('RGBA', (width, height), (10, 20, 30, 254))
    pixels = image.load()
    for index in range(len(data) * 8):
        bit = (data[index // 8] >> (7 - index % 8)) & 1
        pixels[index // height, index % height] = (10, 20, 30, 254 | bit)
    return image


class MetadataTests(unittest.TestCase):
    def assert_bad(self, data, *, limits=MetadataLimits(), code='metadata:invalid'):
        result = extract_positive(data, limits=limits)
        self.assertEqual((result.text, result.source), ('', None))
        self.assertTrue(any(i.code == code and i.severity == 'error' for i in result.issues), result)
        return result

    def test_text_encodings(self):
        for kind, compressed in ((b'tEXt', False), (b'iTXt', False), (b'iTXt', True), (b'zTXt', False)):
            with self.subTest(kind=kind, compressed=compressed):
                result = extract_positive(png(comment(v4('猫 café', ('角色',)), kind=kind, compressed=compressed)))
                self.assertEqual(result, ExtractedPrompt('猫 café\n\n角色', 'Comment.v4_prompt', ()))
        self.assertEqual(extract_positive(png(text('Description', 'café'))).text, 'café')
        for compressed in (True, False):
            self.assertEqual(extract_positive(png(text('Description', '猫\n角色', kind=b'iTXt', compressed=compressed))).text, '猫\n角色')

    def test_priorities_across_duplicate_comments(self):
        result = extract_positive(png(text('Description', 'description'), comment({'prompt': 'earlier'}),
                                      comment(v4()), text('parameters', 'parameters')))
        self.assertEqual(result.text, 'base\n\ncharacter')
        self.assertEqual(extract_positive(png(comment(v4('first')), comment(v4('second')))).text, 'first\n\ncharacter')

    def test_fallback_candidates(self):
        for bad in ('{bad', '{"prompt":"a","prompt":"b"}', '{"prompt":NaN}', '[]'):
            with self.subTest(bad=bad):
                result = extract_positive(png(text('Comment', bad), comment({'prompt': 'fallback'})))
                self.assertEqual(result.text, 'fallback')
                self.assertEqual(result.issues[0].severity, 'warning')

    def test_malformed_v4_types_fall_back_to_prompt(self):
        cases = [None, [], {'caption': []}, {'caption': {'base_caption': 3}},
                 {'caption': {'base_caption': 'x', 'char_captions': {}}},
                 {'caption': {'base_caption': 'x', 'char_captions': [None]}},
                 {'caption': {'base_caption': 'x', 'char_captions': [{'char_caption': 2}]}}]
        for invalid in cases:
            with self.subTest(invalid=invalid):
                result = extract_positive(png(comment({'v4_prompt': invalid, 'prompt': 'ok'})))
                self.assertEqual(result.text, 'ok')
                self.assertTrue(result.issues)

    def test_preserve_whitespace_empty_characters_and_negative_exclusion(self):
        obj = v4('\nbase_, very aesthetic, masterpiece, no text', (' ', '', ' char_1 ', '\nchar2'))
        obj.update(uc='SECRET_NEGATIVE', v4_negative_prompt=v4('SECRET_NEGATIVE'))
        obj['v4_prompt']['caption']['char_captions'][2]['centers'] = [{'x': 0, 'y': 1}]
        result = extract_positive(png(comment(obj)))
        self.assertEqual(result.text, '\nbase_, very aesthetic, masterpiece, no text\n\n char_1 \n\n\nchar2')
        self.assertEqual(extract_positive(png(comment(v4('base', ())))).text, 'base')
        self.assertEqual(extract_positive(png(comment(v4('', (' role ',))))).text, '\n\n role ')

    def test_empty_and_lower_priority(self):
        self.assertEqual(extract_positive(png(comment(v4(' ', ('', ' '))))), ExtractedPrompt('', None, ()))
        self.assertEqual(extract_positive(png(comment({'prompt': 42}), text('Description', '\n d '))).text, '\n d ')
        self.assertEqual(extract_positive(png(text('Description', ' '), text('parameters', 'p'))).source, 'parameters')
        self.assertEqual(extract_positive(png()), ExtractedPrompt('', None, ()))

    def test_a1111_line_boundaries(self):
        for suffix in ('Negative prompt: secret\nSteps: 20, Sampler: x', 'Steps: 20, Sampler: x'):
            result = extract_positive(png(text('parameters', 'positive\nsecond\n' + suffix)))
            self.assertEqual(result.text, 'positive\nsecond\n')
        for value in ('a Negative prompt: literal', 'a Steps: 20, literal', 'Steps: not a parameter', 'Steps: 20'):
            self.assertEqual(extract_positive(png(text('parameters', value))).text, value)

    def test_private_chunk_ignored(self):
        result = extract_positive(png(chunk(b'taKo', b'signed_hash=SECRET'), comment({'prompt': 'ok'})))
        self.assertEqual(result, ExtractedPrompt('ok', 'Comment.prompt', ()))

    def test_stealth_markers_and_single_comment_wrapper(self):
        for compressed in (True, False):
            for obj in (v4('猫', ('角色',)), {'Comment': json.dumps(v4('猫', ('角色',)))}):
                with self.subTest(compressed=compressed, wrapped='Comment' in obj):
                    result = extract_positive(png(image=stealth(obj, compressed=compressed)))
                    self.assertEqual(result.text, '猫\n\n角色')
                    self.assertEqual(result.source, 'stealth_pngcomp' if compressed else 'stealth_pnginfo')
                    self.assertFalse(result.issues)

    def test_stealth_reuses_description_and_parameters(self):
        for obj, expected in (({'Description': 'd'}, 'd'), ({'parameters': 'p\nNegative prompt: n'}, 'p\n')):
            self.assertEqual(extract_positive(png(image=stealth(obj))).text, expected)

    def test_stealth_no_recursive_wrapper_and_no_fabricated_alpha(self):
        wrapped = {'Comment': json.dumps({'Comment': json.dumps({'prompt': 'hidden'})})}
        self.assertEqual(extract_positive(png(image=stealth(wrapped))).text, '')
        image = stealth({'prompt': 'hidden'}).convert('RGB')
        self.assertEqual(extract_positive(png(image=image)), ExtractedPrompt('', None, ()))
        self.assertEqual(extract_positive(png(image=stealth({}, marker=b'unknown_marker!'))).text, '')

    def test_metadata_prevents_stealth_attempt(self):
        image = stealth(payload=b'bad gzip')
        self.assertEqual(extract_positive(png(comment({'prompt': 'ok'}), image=image)),
                         ExtractedPrompt('ok', 'Comment.prompt', ()))
        result = extract_positive(png(text('Comment', 'bad JSON'), image=stealth({'prompt': 'fallback'})))
        self.assertEqual(result.text, 'fallback')
        self.assertEqual(result.issues[0].severity, 'warning')

    def test_malformed_text_blocks_warn_and_continue(self):
        bodies = [(b'tEXt', b'no separator'), (b'zTXt', b'Comment\0\1bad'),
                  (b'zTXt', b'Comment\0\0bad'), (b'iTXt', b'Comment\0\2\0\0\0bad'),
                  (b'iTXt', b'Comment\0\0\1\0\0bad'), (b'iTXt', b'Comment\0\0\0no separators'),
                  (b'iTXt', b'Comment\0\0\0\0\0\xff')]
        compressed = zlib.compress(b'{"prompt":"bad"}')
        bodies += [(b'zTXt', b'Comment\0\0' + compressed[:-1]),
                   (b'zTXt', b'Comment\0\0' + compressed + compressed),
                   (b'iTXt', b'Comment\0\1\0en\0title\0' + compressed + b'trailing')]
        for kind, body in bodies:
            with self.subTest(kind=kind, body=body):
                result = extract_positive(png(chunk(kind, body), text('Description', 'ok')))
                self.assertEqual(result.text, 'ok')
                self.assertEqual(result.issues[0].code, 'metadata:invalid')

    def test_png_signature_crc_bounds_and_iend(self):
        raw = png(comment({'prompt': 'ok'}))
        bad_crc = bytearray(raw)
        bad_crc[40] ^= 1
        cases = [b'', b'not PNG', raw[:-1], raw[:-12], raw + b'trailing', bytes(bad_crc),
                 raw[:33] + struct.pack('>I', 0x7fffffff) + b'tEXt' + raw[33:],
                 raw[:33] + chunk(b'IEND', b'') + raw[33:],
                 raw[:33] + raw[8:33] + raw[33:],
                 raw[:33] + chunk(b'ABCD', b'unknown critical') + raw[33:]]
        for index, data in enumerate(cases):
            with self.subTest(index=index):
                self.assert_bad(data)

    def test_damaged_idat_is_rejected_even_with_prompt(self):
        raw = png()
        bad = raw[:33] + comment({'prompt': 'must not pass'}) + chunk(b'IDAT', b'broken') + raw[-12:]
        self.assert_bad(bad)

    def test_pixel_file_and_text_limits(self):
        raw = png(comment({'prompt': 'x'}))
        self.assert_bad(raw, limits=replace(MetadataLimits(), file_bytes=len(raw) - 1), code='metadata:limit')
        self.assert_bad(raw, limits=replace(MetadataLimits(), pixels=5), code='metadata:limit')
        self.assert_bad(raw, limits=replace(MetadataLimits(), text_chunk_bytes=10), code='metadata:limit')
        self.assert_bad(raw, limits=replace(MetadataLimits(), text_decoded_bytes=3), code='metadata:limit')
        self.assert_bad(png(text('Unknown', '12345'), text('Description', '67890')),
                        limits=replace(MetadataLimits(), total_text_bytes=9), code='metadata:limit')
        self.assertEqual(extract_positive(png(text('Description', '12345')),
                                         limits=replace(MetadataLimits(), text_decoded_bytes=5, total_text_bytes=5)).text, '12345')

    def test_zlib_and_gzip_bombs(self):
        for kind in (b'zTXt', b'iTXt'):
            self.assert_bad(png(text('Description', 'x' * 10000, kind=kind, compressed=True)),
                            limits=replace(MetadataLimits(), text_decoded_bytes=128), code='metadata:limit')
        self.assert_bad(png(image=stealth({'prompt': 'x' * 10000})),
                        limits=replace(MetadataLimits(), text_decoded_bytes=128), code='metadata:limit')

    def test_normal_idat_does_not_use_text_limit(self):
        self.assertEqual(extract_positive(png(), limits=replace(MetadataLimits(), text_chunk_bytes=1)),
                         ExtractedPrompt('', None, ()))

    def test_stealth_bad_lengths_payloads_and_compression(self):
        compressed = gzip.compress(b'{"prompt":"x"}', mtime=0)
        for payload in (b'bad', compressed[:-1], compressed + compressed, compressed + b'trailing'):
            with self.subTest(payload=payload):
                self.assert_bad(png(image=stealth(payload=payload)))
        for length in (1, 0xffffffff, 80000):
            self.assert_bad(png(image=stealth(payload=b'x', bit_length=length)))
        self.assert_bad(png(image=stealth(payload=b'\xff', compressed=False)))
        result = extract_positive(png(image=stealth(payload=b'not JSON', compressed=False)))
        self.assertEqual(result.text, '')
        self.assertTrue(result.issues)
        self.assert_bad(png(image=stealth({'prompt': 'a' * 100}, compressed=False)),
                        limits=replace(MetadataLimits(), text_chunk_bytes=20), code='metadata:limit')

    def test_stealth_uses_remaining_total_budget(self):
        self.assert_bad(png(text('Unknown', 'x' * 90), image=stealth({'prompt': 'abc'})),
                        limits=replace(MetadataLimits(), total_text_bytes=100), code='metadata:limit')

    def test_diagnostics_bounded_and_do_not_expose_metadata(self):
        secret = 'signed_hash=SECRET_MUST_NOT_LEAK'
        result = extract_positive(png(*(text('Comment', secret) for _ in range(100))))
        self.assertLessEqual(len(result.issues), 32)
        self.assertNotIn('SECRET', repr(result))
        self.assertNotIn('signed_hash', repr(result))
        result = extract_positive(png(*(text('Comment', secret) for _ in range(100)),
                                      image=stealth(payload=b'bad')))
        self.assertEqual(result.issues[-1].severity, 'error')

    def test_limits_and_results_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            MetadataLimits().pixels = 1
        with self.assertRaises(FrozenInstanceError):
            extract_positive(png()).text = 'x'
        for value in (0, -1, True, 40_000_001):
            with self.assertRaises(ValueError):
                MetadataLimits(pixels=value)


class DevcacheMetadataTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1] / 'devcache'

    def compare_sample(self, filename, *, duplicate_tail=False):
        image_path = self.root / filename
        prompt_path = self.root / 'prompt.txt'
        if not image_path.is_file() or not prompt_path.is_file():
            self.skipTest('Optional devcache sample or prompt.txt is absent')
        expected = prompt_path.read_text(encoding='utf-8-sig')
        main, character = expected.split('character:', 1)
        main = main.split('main:', 1)[1]
        result = extract_positive(image_path.read_bytes())
        self.assertEqual(result.source, 'Comment.v4_prompt')
        self.assertFalse(result.issues)
        # Pillow is an independent reader for these known, trusted fixtures.
        with Image.open(image_path) as image:
            caption = json.loads(image.info['Comment'])['v4_prompt']['caption']
        base = caption['base_caption']
        chars = [c['char_caption'] for c in caption['char_captions'] if c['char_caption'].strip()]
        self.assertEqual(result.text, base + ''.join('\n\n' + c for c in chars))
        if duplicate_tail:
            tail = ', very aesthetic, masterpiece, no text'
            self.assertTrue(base.startswith('\n'))
            self.assertTrue(base.endswith(tail))
            base = base[:-len(tail)]

        def normalized(value):
            return '\n'.join(line.strip().replace('_', ' ') for line in value.splitlines() if line.strip())

        self.assertEqual(normalized(base + '\n\n' + '\n\n'.join(chars)), normalized(main + '\n\n' + character))

    def test_sample_a(self):
        self.compare_sample('23-48-53-4153890605.png')

    def test_sample_b(self):
        self.compare_sample('naigen_nai_20261019-234218_832_1216.png', duplicate_tail=True)
