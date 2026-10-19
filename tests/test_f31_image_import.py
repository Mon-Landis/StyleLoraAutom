import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image, PngImagePlugin

from stylelora.prompts.group_exchange import GroupExchange
from stylelora.prompts.group_image_import import ImageImportError
from stylelora.prompts.group_store import GroupStore
from stylelora.prompts.groups import GroupRef
from stylelora.prompts.nai_metadata import extract_positive
from stylelora.prompts.prompt_normalize import normalize_prompt
from stylelora.prompts.types import PromptError


def png(text='year_2025, cat, cat', color='red', characters=None):
    info = PngImagePlugin.PngInfo()
    comment = {'prompt': text} if characters is None else {'v4_prompt': {'caption': {
        'base_caption': text, 'char_captions': [{'char_caption': c} for c in characters]}}}
    info.add_text('Comment', json.dumps(comment))
    stream = io.BytesIO()
    Image.new('RGB', (16, 24), color).save(stream, 'PNG', pnginfo=info)
    return stream.getvalue()


class ImageImportTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = 0
        self.store = GroupStore(self.root)
        self.exchange = GroupExchange(self.store, clock=lambda: self.now)

    def preview(self, files, **options):
        return self.exchange.preview_import(files, {'format': 'nai_images', **options})

    def commit(self, p, key='confirm', exchange=None):
        return (exchange or self.exchange).commit_import(p['preview_id'], p['digest'],
            p['base_revision'], key, GroupRef(**p['ref']))

    def test_mixed_reports_normalization_assets_and_freezing(self):
        p = self.preview([('same.png', png()), ('empty.png', png(' ')),
            ('bad.png', b'bad'), ('same.png', png('global', 'blue', ['one', '', 'two'])),
            ('syntax.png', png('{broken'))])
        self.assertEqual((p['valid_count'], p['skipped_count']), (2, 3))
        self.assertEqual([f['file_index'] for f in p['files']], [1, 2, 3, 4, 5])
        self.assertEqual([e['file_index'] for e in p['entries']], [1, 4])
        self.assertEqual([e['nl'] for e in p['entries']], ['year 2025, cat, cat', 'global\n\none\n\ntwo'])
        self.assertEqual(p['files'][0]['source'], 'Comment.prompt')
        for index, code in ((1, 'metadata:empty'), (2, 'image:invalid'), (4, 'normalization:syntax')):
            self.assertIn(code, [i['code'] for i in p['files'][index]['issues']])
        ref = GroupRef(**p['ref'])
        self.assertFalse(self.store.path(ref).exists())
        assets = self.exchange.previews[p['preview_id']][3]
        self.assertEqual([a.file_index for a in assets.values()], [1, 4])
        p['entries'][0]['nl'] = 'changed response'
        self.assertEqual(self.commit(p)['inserted'], 2)
        group = self.store.load(ref)
        self.assertEqual(group.entries[0].nl, 'year 2025, cat, cat')
        for entry in group.entries:
            data, sha = self.store.image(ref, entry)
            self.assertEqual(sha, entry.preview.sha256)
            with Image.open(io.BytesIO(data)) as image:
                self.assertEqual(image.format, 'WEBP')
                self.assertNotIn('Comment', image.info)
        raw = self.store.path(ref).read_text(encoding='utf-8')
        self.assertNotIn('file_index', raw)
        self.assertNotIn('same.png', raw)

    def test_same_text_different_image_and_existing_dedup(self):
        files = [('a.png', png()), ('b.png', png(color='blue')), ('c.png', png())]
        p = self.preview(files)
        result = self.commit(p)
        self.assertEqual((result['inserted'], result['duplicates']), (2, 1))
        appended = self.preview(files, level='entries', ref=p['ref'])
        result = self.commit(appended, 'append')
        self.assertEqual((result['inserted'], result['duplicates']), (0, 3))
        restarted = GroupExchange(GroupStore(self.root))
        self.assertEqual(self.commit(appended, 'append', restarted), result)

    def test_disable_and_text_only_entries_are_distinct_for_images(self):
        p = self.exchange.preview_import(json.dumps([{'nl': 'year 2025, cat, cat'}]).encode())
        self.commit(p)
        image = self.preview([('a.png', png())], level='entries', ref=p['ref'])
        self.assertEqual(self.commit(image, 'image')['inserted'], 1)
        ref = GroupRef(**p['ref'])
        from dataclasses import replace
        group = self.store.load(ref)
        self.store.save(ref, replace(group, entries=tuple(replace(e, disable=True) for e in group.entries)),
                        group.revision, 'disable')
        image = self.preview([('a.png', png())], level='entries', ref=p['ref'])
        self.assertEqual(self.commit(image, 'enabled-image')['inserted'], 1)

    def test_all_invalid_reports_and_no_preview(self):
        for files in ([('blank', png('')), ('bad', b'bad')], [('syntax', png('{broken'))], []):
            with self.subTest(files=len(files)), self.assertRaises(ImageImportError) as caught:
                self.preview(files)
            self.assertEqual(caught.exception.code, 'import:empty')
            self.assertEqual(len(caught.exception.files), len(files))
            self.assertFalse(self.exchange.previews)

    def test_limits_checked_before_extraction_and_single_image_skipped(self):
        for files, limit in (([('a', b'123'), ('b', b'456')], 5),
                             ([('a', b'')] * 1001, 100 * 1024**2)):
            with patch('stylelora.prompts.group_image_import.BATCH_BYTES', limit), \
                 patch('stylelora.prompts.group_image_import.extract_positive') as extract:
                with self.assertRaises(PromptError) as caught:
                    self.preview(files)
                self.assertEqual(caught.exception.code, 'import:limit')
                extract.assert_not_called()
                self.assertFalse(self.exchange.previews)
        p = self.preview([('large', b'x' * (10 * 1024**2 + 1)), ('valid', png())])
        self.assertEqual((p['valid_count'], p['skipped_count']), (1, 1))
        self.assertIn('metadata:limit', [i['code'] for i in p['files'][0]['issues']])

    def test_publish_failure_is_atomic_and_retryable(self):
        p = self.preview([('a', png()), ('b', png(color='blue'))])
        publish = self.store.publish
        calls = 0
        def failing(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PromptError('storage:io_error')
            return publish(*args)
        with patch.object(self.store, 'publish', side_effect=failing):
            with self.assertRaises(PromptError) as caught:
                self.commit(p)
        self.assertEqual(caught.exception.code, 'storage:io_error')
        self.assertFalse(self.store.path(GroupRef(**p['ref'])).exists())
        result = self.commit(p)
        self.assertEqual(result['inserted'], 2)
        self.assertEqual(self.commit(p), result)

    def test_preview_replay_expiry_source_and_ref_validation(self):
        files = [('a', png())]
        p = self.exchange.preview_images(files, request_key='preview')
        self.assertEqual(self.exchange.preview_images(files, request_key='preview'), p)
        with self.assertRaises(PromptError) as caught:
            self.exchange.preview_images([('a', png(color='blue'))], request_key='preview')
        self.assertEqual(caught.exception.code, 'dictionary:request_key_conflict')
        for options in ({'source_format': 'anima'}, {'level': 'entries'}):
            with self.assertRaises(PromptError):
                self.preview(files, **options)
        self.now = 901
        with self.assertRaises(PromptError) as caught:
            self.commit(p)
        self.assertEqual(caught.exception.code, 'preview:expired')

    def test_prepare_failure_never_imports_text_only(self):
        with patch('stylelora.prompts.group_image_import.prepare_preview', side_effect=PromptError('image:invalid')):
            with self.assertRaises(ImageImportError) as caught:
                self.preview([('a', png())])
        self.assertEqual(caught.exception.files[0]['issues'][-1]['code'], 'image:invalid')
        self.assertFalse(self.exchange.previews)

    def test_frozen_preview_image_access_and_expiry(self):
        p = self.preview([('a', png())])
        entry_id = p['entries'][0]['entry_id']
        data, sha = self.exchange.preview_image(p['preview_id'], entry_id)
        import hashlib
        self.assertEqual(hashlib.sha256(data).hexdigest(), sha)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.format, 'WEBP')
            self.assertLessEqual(max(image.size), 320)
            self.assertNotIn('Comment', image.info)
        with self.assertRaises(PromptError) as caught:
            self.exchange.preview_image(p['preview_id'], 'missing')
        self.assertEqual(caught.exception.code, 'image:not_found')
        self.now = 901
        with self.assertRaises(PromptError) as caught:
            self.exchange.preview_image(p['preview_id'], entry_id)
        self.assertEqual(caught.exception.code, 'preview:expired')

    def test_user_sample_a(self):
        self.check_sample('23-48-53-4153890605.png')

    def test_cache_expiry_on_create_and_access_and_confirm_release(self):
        p = self.preview([('a', png())])
        self.assertGreater(self.exchange.preview_bytes, 0)
        self.now = 900
        with self.assertRaises(PromptError):
            self.exchange.preview_image(p['preview_id'], p['entries'][0]['entry_id'])
        self.assertEqual(self.exchange.preview_bytes, 0)
        p = self.preview([('a', png())])
        self.now = 1800
        fresh = self.preview([('b', png())])
        self.assertNotIn(p['preview_id'], self.exchange.previews)
        assets = self.exchange.previews[fresh['preview_id']][3]
        asset_bytes = sum(len(a.full) + len(a.thumb) for a in assets.values())
        before = self.exchange.preview_bytes
        result = self.commit(fresh)
        self.assertEqual(before - self.exchange.preview_bytes, asset_bytes)
        self.assertFalse(assets)
        self.exchange.previews.clear()
        self.assertEqual(self.commit(fresh), result)

    def test_cache_count_and_bytes_evict_oldest(self):
        with patch('stylelora.prompts.group_exchange.PREVIEW_CACHE_COUNT', 1):
            first = self.preview([('a', png())])
            second = self.preview([('b', png())])
            self.assertEqual(list(self.exchange.previews), [second['preview_id']])
            self.assertNotIn(first['preview_id'], self.exchange.previews)
        cap = self.exchange.preview_bytes
        with patch('stylelora.prompts.group_exchange.PREVIEW_CACHE_BYTES', cap):
            third = self.preview([('b', png())])
            self.assertEqual(list(self.exchange.previews), [third['preview_id']])
            self.assertLessEqual(self.exchange.preview_bytes, cap)
        with patch('stylelora.prompts.group_exchange.PREVIEW_CACHE_BYTES', 1):
            with self.assertRaises(PromptError) as caught:
                self.preview([('b', png())])
            self.assertEqual(caught.exception.code, 'import:limit')
            self.assertEqual(self.exchange.preview_bytes, 0)

    def test_user_sample_b(self):
        self.check_sample('naigen_nai_20261019-234218_832_1216.png')

    def check_sample(self, name):
        path = Path(__file__).resolve().parents[1] / 'devcache' / name
        if not path.exists():
            self.skipTest('optional user sample is absent')
        data = path.read_bytes()
        extracted = extract_positive(data)
        self.assertTrue(extracted.text.strip())
        self.assertEqual(extracted.source, 'Comment.v4_prompt')
        expected = normalize_prompt(extracted.text, 'nai').text
        p = self.preview([(name, data)])
        self.assertEqual(p['entries'][0]['nl'], expected)
        self.assertEqual(p['valid_count'], 1)
        self.assertEqual(p['skipped_count'], 0)
        self.assertEqual(p['files'][0]['source'], extracted.source)
        self.assertEqual(self.commit(p)['inserted'], 1)
        group = self.store.load(GroupRef(**p['ref']))
        self.assertEqual(group.entries[0].nl, expected)


if __name__ == '__main__':
    unittest.main()
