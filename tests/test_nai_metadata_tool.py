import json
import shutil
import struct
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock
from uuid import uuid4
import zlib

from fastapi.testclient import TestClient
from PIL import Image

from stylelora.api import create_app
from stylelora.prompts.nai_metadata import extract_positive, extract_positive_parts
from stylelora.runtime import Runtime

DEVCACHE = Path(__file__).resolve().parents[1] / 'devcache'


def chunk(kind, body):
    return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))


def png(*metadata):
    output = BytesIO()
    Image.new('RGB', (2, 3)).save(output, format='PNG')
    raw = output.getvalue()
    return raw[:33] + b''.join(metadata) + raw[33:]


def text(key, value):
    return chunk(b'tEXt', key.encode() + b'\0' + value.encode('latin-1'))


def comment(value):
    return text('Comment', json.dumps(value))


def v4(base='base', characters=('character',)):
    return {'prompt': base, 'v4_prompt': {'caption': {
        'base_caption': base, 'char_captions': [{'char_caption': c} for c in characters]}}}


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.images = self.root / 'images'
        self.images.mkdir()
        self.rt = Runtime(self.root / 'product', picker=lambda purpose: None,
                          image_picker=lambda source: None)
        self.client = TestClient(create_app(self.rt), base_url='http://127.0.0.1:8000')
        self.client.__enter__()
        self.addCleanup(lambda: self.client.__exit__(None, None, None))
        self.client.headers['Authorization'] = 'Bearer ' + self.rt.token

    def write(self, name, data, folder=None):
        target = (folder or self.images) / name
        target.write_bytes(data if isinstance(data, bytes) else png(*data))
        return target

    def run_tool(self, picked, *, source='files', drop_mode='none', to_anima=False, mode=None):
        self.rt.image_picker = lambda kind: picked
        self.response = self.post({'source': source, 'drop_mode': mode or drop_mode,
                                   'to_anima': to_anima})
        return self.response

    def post(self, body):
        return self.client.post('/api/tools/nai-metadata/run', json=body,
                                headers={'Idempotency-Key': str(uuid4())})

    def test_drop_modes(self):
        image = self.write('a.png', [comment(v4('global_ base', (' char_one ', 'char_two')))])
        expected = {'none': 'global_ base\n\n char_one \n\nchar_two',
                    'head': 'char_one \n\nchar_two',
                    'global': 'char_one \n\nchar_two'}
        for mode, body in expected.items():
            with self.subTest(mode=mode):
                result = self.run_tool((image,), mode=mode)
                self.assertEqual(result.status_code, 200, result.text)
                item = result.json()['files'][0]
                self.assertEqual(item['status'], 'written')
                self.assertEqual(item['source'], 'Comment.v4_prompt')
                self.assertEqual(item['txt'], str(image.with_suffix('.txt')))
                self.assertEqual(image.with_suffix('.txt').read_text(encoding='utf-8'), body)
                self.assertEqual(item['chars'], len(body))
                self.assertEqual(result.json()['root'], str(self.images))

    def test_head_drops_first_blank_line_run(self):
        image = self.write('b.png', [comment({'prompt': 'one\ntwo\n  \n\nthree\nfour'})])
        result = self.run_tool((image,), drop_mode='head')
        self.assertEqual(self.response.json()['files'][0]['status'], 'written')
        self.assertEqual(image.with_suffix('.txt').read_text(encoding='utf-8'), 'three\nfour')

    def test_head_without_blank_line_keeps_text(self):
        image = self.write('c.png', [comment({'prompt': 'one\ntwo'})])
        self.run_tool((image,), drop_mode='head')
        self.assertEqual(image.with_suffix('.txt').read_text(encoding='utf-8'), 'one\ntwo')

    def test_global_without_chars_is_skipped(self):
        image = self.write('d.png', [comment({'prompt': 'plain base'})])
        result = self.run_tool((image,), drop_mode='global')
        item = self.response.json()['files'][0]
        self.assertEqual(item['status'], 'skipped')
        self.assertEqual(item['reason'], 'metadata:empty')
        self.assertIsNone(item['txt'])
        self.assertFalse(image.with_suffix('.txt').exists())
        self.assertEqual(result.json()['summary'], {'total': 1, 'written': 0, 'skipped': 1, 'failed': 0})

    def test_global_v4_with_blank_base_keeps_characters(self):
        image = self.write('e.png', [comment(v4('', (' only_char ',)))])
        result = self.run_tool((image,), drop_mode='global')
        self.assertEqual(self.response.json()['files'][0]['status'], 'written')
        self.assertEqual(image.with_suffix('.txt').read_text(encoding='utf-8'), 'only_char')

    def test_to_anima(self):
        image = self.write('f.png', [comment({'prompt': '1.5::x::, long_hair'})])
        kept = self.run_tool((image,), to_anima=False).json()['files'][0]
        self.assertEqual(kept['status'], 'written')
        self.assertEqual(image.with_suffix('.txt').read_text(encoding='utf-8'), '1.5::x::, long_hair')
        converted = self.run_tool((image,), to_anima=True).json()['files'][0]
        self.assertEqual(converted['status'], 'written')
        value = image.with_suffix('.txt').read_text(encoding='utf-8')
        self.assertNotIn('1.5::', value)
        self.assertIn('(x:1.3)', value)
        self.assertIn('long hair', value)
        self.assertIsInstance(converted['warnings'], list)

    def test_empty_metadata_skipped(self):
        image = self.write('g.png', [])
        result = self.run_tool((image,)).json()['files'][0]
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(result['reason'], 'metadata:empty')
        self.assertFalse(image.with_suffix('.txt').exists())

    def test_corrupt_and_unrelated_files_do_not_abort_batch(self):
        good = self.write('z_good.png', [comment({'prompt': 'ok'})])
        corrupt = self.write('a_corrupt.png', b'\x89PNG\r\n\x1a\nbroken')
        other = self.write('m_other.jpg', b'not an image')
        result = self.run_tool((corrupt, other, good)).json()
        statuses = {f['name']: f['status'] for f in result['files']}
        self.assertEqual(statuses, {'a_corrupt.png': 'failed', 'm_other.jpg': 'failed',
                                    'z_good.png': 'written'})
        self.assertTrue(any(i['severity'] == 'error' for i in result['files'][0]['issues']))
        self.assertEqual(result['summary'], {'total': 3, 'written': 1, 'skipped': 0, 'failed': 2})
        self.assertEqual(good.with_suffix('.txt').read_text(encoding='utf-8'), 'ok')

    def test_write_failure_is_per_file(self):
        image = self.write('h.png', [comment({'prompt': 'ok'})])
        with mock.patch('stylelora.prompts.nai_metadata_export.os.replace',
                        side_effect=OSError('disk')):
            item = self.run_tool((image,)).json()['files'][0]
        self.assertEqual(item['status'], 'failed')
        self.assertEqual([i['code'] for i in item['issues']], ['storage:io_error'])
        self.assertFalse(image.with_suffix('.txt').exists())
        self.assertEqual(list(self.images.glob('*.tmp')), [])

    def test_overwrite_is_atomic_and_exact(self):
        image = self.write('i.png', [comment({'prompt': 'one\n\ntwo'})])
        txt = image.with_suffix('.txt')
        txt.write_bytes(b'old')
        first = self.run_tool((image,)).json()['files'][0]
        self.assertTrue(first['overwritten'])
        self.assertEqual(txt.read_bytes(), b'one\n\ntwo')
        self.assertFalse(txt.read_bytes().startswith(b'\xef\xbb\xbf'))
        self.assertNotIn(b'\r', txt.read_bytes())
        second = self.run_tool((image,)).json()['files'][0]
        self.assertTrue(second['overwritten'])

    def test_fresh_file_not_overwritten(self):
        image = self.write('j.png', [comment({'prompt': 'ok'})])
        self.assertFalse(self.run_tool((image,)).json()['files'][0]['overwritten'])

    def test_folder_mode_non_recursive_sorted(self):
        self.write('b.png', [comment({'prompt': 'b'})])
        self.write('A.PNG', [comment({'prompt': 'a'})])
        self.write('c.jpg', b'ignored')
        self.write('d.txt', b'ignored')
        nested = self.images / 'nested'
        nested.mkdir()
        self.write('e.png', [comment({'prompt': 'e'})], folder=nested)
        result = self.run_tool(self.images, source='folder').json()
        self.assertEqual(result['root'], str(self.images))
        self.assertEqual([f['name'] for f in result['files']], ['A.PNG', 'b.png'])
        self.assertEqual(result['summary'], {'total': 2, 'written': 2, 'skipped': 0, 'failed': 0})
        self.assertFalse((nested / 'e.txt').exists())

    def test_folder_mode_skips_symlink(self):
        self.write('a.png', [comment({'prompt': 'a'})])
        target = self.root / 'outside.png'
        target.write_bytes(png(comment({'prompt': 'x'})))
        try:
            (self.images / 'link.png').symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        result = self.run_tool(self.images, source='folder').json()
        self.assertEqual([f['name'] for f in result['files']], ['a.png'])

    def test_folder_limit(self):
        for index in range(1001):
            (self.images / f'{index:04}.png').write_bytes(b'')
        result = self.run_tool(self.images, source='folder')
        self.assertEqual(result.status_code, 413, result.text)
        self.assertEqual(result.json()['error']['code'], 'import:limit')
        self.assertEqual(list(self.images.glob('*.txt')), [])

    def test_canceled(self):
        result = self.run_tool(None, source='folder')
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {'canceled': True, 'root': None,
                                         'summary': {'total': 0, 'written': 0, 'skipped': 0, 'failed': 0},
                                         'files': []})

    def test_picker_unavailable(self):
        self.rt.picker = self.rt.image_picker = None
        result = self.post({'source': 'files', 'drop_mode': 'none', 'to_anima': False})
        self.assertEqual(result.status_code, 501, result.text)
        self.assertEqual(result.json()['error']['code'], 'picker:unavailable')


    def test_files_mode_rejects_non_png_without_writing(self):
        # A .txt holding PNG bytes must never be overwritten by its own prompt.
        decoy = self.images / 'picture.txt'
        decoy.write_bytes(png(comment({'prompt': 'prompt text'})))
        good = self.write('ok.png', [comment({'prompt': 'ok'})])
        result = self.run_tool((decoy, good)).json()
        self.assertEqual([f['name'] for f in result['files']], ['picture.txt', 'ok.png'])
        decoy_row = result['files'][0]
        self.assertEqual(decoy_row['status'], 'failed')
        self.assertEqual(decoy_row['reason'], 'image:unsupported')
        self.assertEqual([i['code'] for i in decoy_row['issues']], ['image:unsupported'])
        self.assertIsNone(decoy_row['txt'])
        self.assertEqual(decoy.read_bytes(), png(comment({'prompt': 'prompt text'})))
        self.assertEqual(result['files'][1]['status'], 'written')
        self.assertEqual((self.images / 'ok.txt').read_text(encoding='utf-8'), 'ok')

    def test_picker_type_filter_is_png_only(self):
        from stylelora.runtime.picker import _IMAGE_TYPES
        patterns = [pattern for _, pattern in _IMAGE_TYPES]
        self.assertEqual(patterns, ['*.png'])

    def test_non_png_cannot_target_a_png_input(self):
        png_input = self.write('x.png', [comment({'prompt': 'from png'})])
        self.run_tool((png_input,))
        # Same stem, non-PNG extension: x.jpg -> x.txt (the png's own output).
        other = self.images / 'x.jpg'
        other.write_bytes(b'not an image')
        result = self.run_tool((other, png_input)).json()
        rows = {f['name']: f for f in result['files']}
        self.assertEqual(rows['x.jpg']['status'], 'failed')
        self.assertEqual(rows['x.jpg']['reason'], 'image:unsupported')
        self.assertEqual(rows['x.png']['reason'] if rows['x.png']['status'] != 'written' else 'written',
                         'output:conflict')
        self.assertTrue(all(r['status'] != 'written' for r in result['files']))
        self.assertEqual(png_input.with_suffix('.txt').read_text(encoding='utf-8'), 'from png')

    def test_case_collision_blocks_all_colliding_outputs(self):
        upper = self.write('x.PNG', [comment({'prompt': 'upper'})])
        lower = self.write('x.png', [comment({'prompt': 'lower'})])
        result = self.run_tool((upper, lower)).json()
        self.assertEqual(result['summary'], {'total': 2, 'written': 0, 'skipped': 0, 'failed': 2})
        for row in result['files']:
            self.assertEqual(row['status'], 'failed')
            self.assertEqual(row['reason'], 'output:conflict')
            self.assertEqual([i['code'] for i in row['issues']], ['output:conflict'])
        self.assertFalse((self.images / 'x.txt').exists())

    def test_collision_survives_non_ascii_content(self):
        first = self.write('dup.PNG', [comment({'prompt': 'a'})])
        second = self.write('dup.png', [comment({'prompt': 'b'})])
        result = self.run_tool((first, second)).json()
        self.assertTrue(all(f['status'] == 'failed' for f in result['files']))

    def test_unencodable_prompt_fails_only_that_file(self):
        bad = self.write('surrogate.png', [comment({'prompt': 'a\ud800b'})])
        good = self.write('good.png', [comment({'prompt': 'fine'})])
        result = self.run_tool((bad, good)).json()
        rows = {f['name']: f for f in result['files']}
        self.assertEqual(rows['surrogate.png']['status'], 'failed')
        self.assertEqual(rows['surrogate.png']['reason'], 'metadata:invalid')
        self.assertEqual([i['code'] for i in rows['surrogate.png']['issues']], ['metadata:invalid'])
        self.assertFalse(bad.with_suffix('.txt').exists())
        self.assertEqual(rows['good.png']['status'], 'written')
        self.assertEqual(good.with_suffix('.txt').read_text(encoding='utf-8'), 'fine')

    def test_unexpected_exception_is_per_file(self):
        import stylelora.prompts.nai_metadata_export as export
        good = self.write('good.png', [comment({'prompt': 'fine'})])
        bad = self.write('bad.png', [comment({'prompt': 'boom'})])
        original = export.extract_positive_parts
        real_bad = bad

        def explode(data, **kwargs):
            if data == real_bad.read_bytes():
                raise ValueError('unexpected')
            return original(data, **kwargs)

        with mock.patch.object(export, 'extract_positive_parts', explode):
            result = self.run_tool((bad, good)).json()
        rows = {f['name']: f for f in result['files']}
        self.assertEqual(rows['bad.png']['status'], 'failed')
        self.assertEqual([i['code'] for i in rows['bad.png']['issues']], ['storage:io_error'])
        self.assertFalse(bad.with_suffix('.txt').exists())
        self.assertEqual(rows['good.png']['status'], 'written')

    def test_cross_drive_commonpath_keeps_root_null(self):
        image = self.write('a.png', [comment({'prompt': 'a'})])
        with mock.patch('stylelora.prompts.nai_metadata_export.os.path.commonpath',
                        side_effect=ValueError('different drives')):
            result = self.run_tool((image,)).json()
        self.assertIsNone(result['root'])
        self.assertEqual(result['files'][0]['status'], 'written')

    def test_conversion_uses_default_policy(self):
        import stylelora.prompts.nai_metadata_export as export
        from stylelora.prompts.nai_convert import ConversionPolicy
        self.assertEqual(export.POLICY, ConversionPolicy())
        self.assertEqual(export.POLICY.max_chars, ConversionPolicy().max_chars)
        image = self.write('big.png', [comment({'prompt': '1.5::x::'})])
        self.run_tool((image,), to_anima=True)
        self.assertEqual(image.with_suffix('.txt').read_text(encoding='utf-8'), '(x:1.3)')

    def test_conversion_refusal_reports_real_code(self):
        image = self.write('long.png', [comment({'prompt': '1.5::x::'})])
        import stylelora.prompts.nai_metadata_export as export
        from stylelora.prompts.nai_convert import ConversionResult, ConversionWarning
        from stylelora.prompts.nai_convert import SourceSpan, WarningCode
        refused = ConversionResult(
            '', (), (ConversionWarning(WarningCode.W_INPUT_LIMIT, SourceSpan(0, 1),
                                       'W_INPUT_LIMIT'),))
        with mock.patch.object(export, 'convert_nai_to_anima', return_value=refused):
            row = self.run_tool((image,), to_anima=True).json()['files'][0]
        self.assertEqual(row['status'], 'skipped')
        self.assertEqual(row['reason'], 'W_INPUT_LIMIT')
        self.assertEqual(row['warnings'], ['W_INPUT_LIMIT'])
        self.assertFalse(image.with_suffix('.txt').exists())

    def test_outer_trim_still_applies(self):
        image = self.write('pad.png', [comment({'prompt': '  \n a \n  b  \n\n'})])
        self.run_tool((image,))
        self.assertEqual(image.with_suffix('.txt').read_text(encoding='utf-8'), 'a \n  b')

    def test_response_is_json_serializable(self):
        import json
        self.write('a.png', [comment({'prompt': 'a'})])
        self.write('bad.png', b'bad')
        body = self.run_tool((self.images / 'a.png', self.images / 'bad.png')).json()
        for row in body['files']:
            for issue in row['issues']:
                self.assertEqual(set(issue) >= {'code', 'severity', 'message'}, True)
        json.dumps(body)

    def test_parser_errors_are_per_file(self):
        import stylelora.prompts.nai_metadata_export as export
        from stylelora.prompts.types import PromptError
        good = self.write('good.png', [comment({'prompt': 'fine'})])
        bad = self.write('bad.png', [comment({'prompt': 'boom'})])
        original = export.extract_positive_parts
        bad_bytes = bad.read_bytes()

        def failing(data, **kwargs):
            if data == bad_bytes:
                raise PromptError('schema:invalid')
            return original(data, **kwargs)

        with mock.patch.object(export, 'extract_positive_parts', failing):
            result = self.run_tool((bad, good)).json()
        rows = {f['name']: f for f in result['files']}
        self.assertEqual(rows['bad.png']['status'], 'failed')
        self.assertEqual([i['code'] for i in rows['bad.png']['issues']], ['schema:invalid'])
        self.assertEqual(rows['good.png']['status'], 'written')

    def test_busy_picker_returns_409(self):
        import stylelora.prompts.nai_metadata_export as export
        self.assertTrue(export._picker_lock.acquire(blocking=False))
        tries = []

        def slow(source):
            tries.append(source)
            return None

        self.rt.image_picker = slow
        try:
            result = self.post({'source': 'files', 'drop_mode': 'none', 'to_anima': False})
        finally:
            export._picker_lock.release()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()['error']['code'], 'picker:busy')
        self.assertEqual(tries, [])

    def test_lock_released_after_failure(self):
        import stylelora.prompts.nai_metadata_export as export

        def broken(source):
            raise RuntimeError('tk failed')

        self.rt.image_picker = broken
        result = self.post({'source': 'files', 'drop_mode': 'none', 'to_anima': False})
        self.assertEqual(result.status_code, 501, result.text)
        self.assertEqual(result.json()['error']['code'], 'picker:unavailable')
        self.assertTrue(export._picker_lock.acquire(blocking=False))
        export._picker_lock.release()

    def test_lock_released_after_limit(self):
        import stylelora.prompts.nai_metadata_export as export
        for index in range(1001):
            (self.images / f'{index:04}.png').write_bytes(b'')
        self.assertEqual(self.run_tool(self.images, source='folder').status_code, 413)
        self.assertTrue(export._picker_lock.acquire(blocking=False))
        export._picker_lock.release()

    def test_picker_exception_maps_to_501(self):
        import stylelora.prompts.nai_metadata_export as export

        def broken(source):
            raise OSError('no display')

        self.rt.image_picker = broken
        result = self.post({'source': 'folder', 'drop_mode': 'none', 'to_anima': False})
        self.assertEqual(result.status_code, 501, result.text)
        self.assertEqual(result.json()['error']['code'], 'picker:unavailable')
        self.assertTrue(export._picker_lock.acquire(blocking=False))
        export._picker_lock.release()

    def test_only_image_picker_is_required(self):
        # rt.picker is the legacy folder capability flag and must not gate this tool.
        self.rt.picker = None
        result = self.run_tool(None, source='folder')
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['canceled'])

    def test_run_happens_off_the_event_loop(self):
        import threading
        image = self.write('a.png', [comment({'prompt': 'a'})])
        threads = []

        def picker(source):
            threads.append(threading.current_thread())
            return (image,)

        self.rt.image_picker = picker
        import stylelora.prompts.nai_metadata_export as export
        original = export._write

        def record(path, text):
            threads.append(threading.current_thread())
            return original(path, text)

        with mock.patch.object(export, '_write', record):
            result = self.post({'source': 'files', 'drop_mode': 'none', 'to_anima': False})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['summary']['written'], 1)
        self.assertEqual(len(threads), 2)
        self.assertTrue(all(t is not threading.main_thread() for t in threads))
        self.assertIs(threads[0], threads[1])

    def test_strict_body(self):
        good = {'source': 'files', 'drop_mode': 'none', 'to_anima': False}
        for body in ({**good, 'path': 'C:/tmp'}, {**good, 'source': 'both'},
                     {**good, 'drop_mode': 'all'}, {**good, 'to_anima': 1},
                     {k: v for k, v in good.items() if k != 'to_anima'}, []):
            with self.subTest(body=body):
                result = self.post(body)
                self.assertEqual(result.status_code, 422, result.text)
                self.assertEqual(result.json()['error']['code'], 'input:invalid')
        result = self.post([1])
        self.assertEqual(result.status_code, 422)
        result = self.client.post('/api/tools/nai-metadata/run', content=b'{bad',
                                  headers={'Content-Type': 'application/json',
                                           'Idempotency-Key': str(uuid4())})
        self.assertEqual(result.status_code, 400)


class DropHeadTests(unittest.TestCase):
    def test_head_edges(self):
        from stylelora.prompts.nai_metadata_export import _drop_head
        cases = {
            'a\n\nb': 'b',
            'a\nb': 'a\nb',
            'a\n  \n\tb\nc': 'b\nc',
            '\na\nb': 'a\nb',
            'a\n\n\n\nb': 'b',
            'a\n\n': '',
            '  \na': 'a',
            'a\nb\n  ': '',  # trailing blank line is still a complete blank line
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(_drop_head(value), expected)


class PartsTests(unittest.TestCase):
    def test_parts_do_not_change_extraction(self):
        cases = [png(comment(v4('base', ('a', ' ', 'b')))),
                 png(comment({'prompt': 'fallback'})),
                 png(text('Description', 'd')),
                 png(comment(v4('', ('a',)))),
                 b'']
        for data in cases:
            parts = extract_positive_parts(data)
            self.assertEqual(parts.extraction, extract_positive(data))
        self.assertEqual((extract_positive_parts(cases[0]).base,
                          extract_positive_parts(cases[0]).chars), ('base', ('a', 'b')))
        self.assertEqual((extract_positive_parts(cases[1]).base,
                          extract_positive_parts(cases[1]).chars), (None, ()))


class DevcacheToolTests(unittest.TestCase):
    def test_global_matches_devcache_character_section(self):
        prompt_path = DEVCACHE / 'prompt.txt'
        names = ('23-48-53-4153890605.png', 'naigen_nai_20261019-234218_832_1216.png')
        if not prompt_path.is_file() or not all((DEVCACHE / n).is_file() for n in names):
            self.skipTest('Optional devcache samples are absent')
        expected = prompt_path.read_text(encoding='utf-8-sig').split('character:', 1)[1]

        def normalized(value):
            return '\n'.join(line.strip().replace('_', ' ')
                             for line in value.splitlines() if line.strip())

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in names:
                shutil.copyfile(DEVCACHE / name, root / name)
            rt = Runtime(root / 'product', picker=lambda purpose: None,
                         image_picker=lambda source: tuple(root / n for n in names))
            client = TestClient(create_app(rt), base_url='http://127.0.0.1:8000')
            with client:
                client.headers['Authorization'] = 'Bearer ' + rt.token
                result = client.post('/api/tools/nai-metadata/run',
                                     json={'source': 'files', 'drop_mode': 'global',
                                           'to_anima': False},
                                     headers={'Idempotency-Key': str(uuid4())})
            self.assertEqual(result.status_code, 200, result.text)
            body = result.json()
            self.assertEqual(body['summary']['written'], 2, body)
            for name in names:
                written = (root / name).with_suffix('.txt').read_text(encoding='utf-8')
                self.assertEqual(normalized(written), normalized(expected), name)


if __name__ == '__main__':
    unittest.main()
