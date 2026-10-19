import hashlib
import io
import json
import stat
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image, PngImagePlugin

from stylelora.prompts.group_exchange import GroupExchange
from stylelora.prompts.group_image_pack import encode_png, read_image_pack
from stylelora.prompts.group_store import GroupStore, prepare_preview
from stylelora.prompts.groups import Group, GroupRef, Entry
from stylelora.prompts.nai_metadata import extract_positive
from stylelora.prompts.types import PromptError
from uuid import uuid4


class ImagePackTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = GroupStore(Path(temp.name))
        self.exchange = GroupExchange(self.store)
        self.ref = GroupRef('global', None, str(uuid4()))
        self.entries = (Entry(str(uuid4()), nl='雪, cat\n\n第二段', disable=True,
                              variants={'empty': '', 'alt': 'dog'}),
                        Entry(str(uuid4()), nl='reference'))
        asset = prepare_preview(encode_png('old metadata'), self.entries[1].entry_id)
        preview = self.store.publish(asset, self.ref, self.entries[1].entry_id)
        self.entries = (self.entries[0], replace(self.entries[1], preview=preview))
        group = Group(self.ref.group_id, '图像包', 'global', True, entries=self.entries,
                      auto_complete=False)
        self.store.save(self.ref, group, 0, 'setup')

    def pack(self):
        return self.exchange.export_group(self.ref, 'nai_image_zip')

    def rebuild(self, files, compression=zipfile.ZIP_STORED):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=compression) as archive:
            for name, data in files.items():
                info = zipfile.ZipInfo(name)
                info.filename = name  # Preserve malicious Windows names in fixtures.
                info.compress_type = compression
                archive.writestr(info, data)
        return stream.getvalue()

    def assert_code(self, code, fn):
        with self.assertRaises(PromptError) as error:
            fn()
        self.assertEqual(error.exception.code, code)

    def test_roundtrip_deterministic_lease_placeholder_and_metadata(self):
        read_image = self.store.image
        def leased_read(*args):
            self.assertTrue(self.store.leases)
            return read_image(*args)
        with patch.object(self.store, 'image', side_effect=leased_read) as image:
            packed = self.pack()
            image.assert_called_once()
        self.assertEqual(packed, self.pack())
        self.assertFalse(self.store.leases)
        files = read_image_pack(packed)
        raw = json.loads(files['group.json'])
        self.assertEqual(len(files), 3)
        self.assertFalse(raw['group_metadata']['auto_complete'])
        for entry in self.entries:
            spec = raw['preview_manifest'][entry.entry_id]
            png = files[spec['file']]
            self.assertIn(b'tEXtComment\0', png)
            self.assertEqual(extract_positive(png).text, entry.nl)
            with Image.open(io.BytesIO(png)) as decoded:
                self.assertEqual(set(decoded.info), {'Comment'})
                self.assertEqual(decoded.mode, 'RGB')
                comment = json.loads(decoded.info['Comment'])
                self.assertEqual(comment['prompt'], entry.nl)
                self.assertEqual(comment['v4_prompt']['caption'],
                    dict(base_caption=entry.nl, char_captions=[]))
                if spec['placeholder']:
                    self.assertEqual(decoded.size, (256, 256))
        p = self.exchange.preview_import(packed, {'format': 'nai_image_zip'})
        result = self.exchange.commit_import(p['preview_id'], p['digest'], 0, 'roundtrip')
        restored = self.store.load(GroupRef(**result['ref']))
        by_id = {e.entry_id: e for e in restored.entries}
        self.assertIsNone(by_id[self.entries[0].entry_id].preview)
        self.assertIsNotNone(by_id[self.entries[1].entry_id].preview)
        self.assertTrue(by_id[self.entries[0].entry_id].disable)
        self.assertEqual(dict(by_id[self.entries[0].entry_id].variants), {'empty': '', 'alt': 'dog'})
        self.assertEqual(restored.name, '图像包')
        self.assertTrue(restored.nsfw)

    def test_same_prompt_preserves_images_placeholders_and_disable(self):
        entries = []
        for color, disabled in (('red', False), ('blue', False),
                                ('red', True), (None, False), (None, True)):
            entry = Entry(str(uuid4()), nl='cat', disable=disabled)
            if color:
                stream = io.BytesIO()
                Image.new('RGB', (16, 16), color).save(stream, 'PNG')
                asset = prepare_preview(stream.getvalue(), entry.entry_id)
                entry = replace(entry, preview=self.store.publish(asset, self.ref, entry.entry_id))
            entries.append(entry)
        group = self.store.load(self.ref)
        self.store.save(self.ref, replace(group, entries=tuple(entries)), group.revision, 'same-nl')
        preview = self.exchange.preview_import(self.pack(), {'format': 'nai_image_zip'})
        self.assertEqual(preview['valid_count'], 5)
        result = self.exchange.commit_import(preview['preview_id'], preview['digest'], 0, 'same-nl-import')
        self.assertEqual((result['inserted'], result['duplicates'], result['conflicts']), (5, 0, []))
        restored = self.store.load(GroupRef(**result['ref']))
        self.assertEqual(len(restored.entries), 5)
        self.assertEqual({e.entry_id: (e.nl, e.disable, e.preview is None) for e in restored.entries},
                         {e.entry_id: (e.nl, e.disable, e.preview is None) for e in entries})
        by_id = {e.entry_id: e for e in restored.entries}
        self.assertNotEqual(by_id[entries[0].entry_id].preview.sha256,
                            by_id[entries[1].entry_id].preview.sha256)
        # A second import into the same group still recognizes actual duplicates.
        preview = self.exchange.preview_import(self.pack(),
            {'format': 'nai_image_zip', 'level': 'entries', 'ref': result['ref']})
        again = self.exchange.commit_import(preview['preview_id'], preview['digest'],
                                            restored.revision, 'same-nl-again')
        self.assertEqual((again['inserted'], again['duplicates']), (0, 5))

    def test_mismatch_even_when_digest_matches(self):
        files = read_image_pack(self.pack())
        raw = json.loads(files['group.json'])
        spec = raw['preview_manifest'][self.entries[0].entry_id]
        files[spec['file']] = encode_png('different')
        spec['sha256'] = hashlib.sha256(files[spec['file']]).hexdigest()
        files['group.json'] = json.dumps(raw).encode()
        self.assert_code('import:metadata_mismatch', lambda: self.exchange.preview_import(
            self.rebuild(files), {'format': 'nai_image_zip'}))

    def test_both_comment_fields_and_corrupt_png_with_matching_digest(self):
        for kind in ('prompt', 'characters', 'corrupt'):
            with self.subTest(kind=kind):
                files = read_image_pack(self.pack())
                raw = json.loads(files['group.json'])
                entry = self.entries[0]
                spec = raw['preview_manifest'][entry.entry_id]
                comment = dict(prompt=entry.nl, v4_prompt=dict(caption=dict(
                    base_caption=entry.nl, char_captions=[])))
                if kind == 'prompt':
                    comment['prompt'] = 'conflicting fallback'
                elif kind == 'characters':
                    comment['v4_prompt']['caption']['char_captions'] = [{'char_caption': 'extra'}]
                info = PngImagePlugin.PngInfo()
                info.add_text('Comment', json.dumps(comment))
                stream = io.BytesIO()
                Image.new('RGB', (16, 16)).save(stream, 'PNG', pnginfo=info)
                data = stream.getvalue() if kind != 'corrupt' else stream.getvalue()[:-8]
                files[spec['file']] = data
                spec['sha256'] = hashlib.sha256(data).hexdigest()
                files['group.json'] = json.dumps(raw).encode()
                self.assert_code('image:invalid' if kind == 'corrupt' else 'import:metadata_mismatch',
                    lambda: self.exchange.preview_import(self.rebuild(files), {'format': 'nai_image_zip'}))

    def test_manifest_is_exhaustive_and_boolean(self):
        for kind in ('missing', 'extra', 'duplicate', 'boolean', 'path'):
            with self.subTest(kind=kind):
                files = read_image_pack(self.pack())
                raw = json.loads(files['group.json'])
                identity = self.entries[0].entry_id
                spec = raw['preview_manifest'][identity]
                code = 'schema:invalid'
                if kind == 'missing':
                    del raw['preview_manifest'][identity]
                elif kind == 'extra':
                    raw['preview_manifest'][str(uuid4())] = spec
                elif kind == 'duplicate':
                    raw['prompts'].append(raw['prompts'][0])
                    code = 'entry:id_conflict'
                elif kind == 'boolean':
                    spec['placeholder'] = 1
                else:
                    spec['file'] = '../escape.png'
                    code = 'import:unsafe_archive'
                files['group.json'] = json.dumps(raw).encode()
                self.assert_code(code, lambda: self.exchange.preview_import(
                    self.rebuild(files), {'format': 'nai_image_zip'}))

    def test_manifest_missing_extra_corrupt_and_wrong_format(self):
        original = read_image_pack(self.pack())
        name = next(n for n in original if n.endswith('.png'))
        for files, code in (({k: v for k, v in original.items() if k != name}, 'image:not_found'),
                            (original | {'extra.png': b'extra'}, 'import:unsafe_archive'),
                            (original | {name: b'bad'}, 'image:invalid')):
            self.assert_code(code, lambda: self.exchange.preview_import(
                self.rebuild(files), {'format': 'nai_image_zip'}))
        self.assert_code('schema:invalid', lambda: self.exchange.preview_import(
            self.pack(), {'format': 'zip'}))
        legacy = self.exchange.export_group(self.ref, 'zip')
        p = self.exchange.preview_import(legacy, {'format': 'zip'})
        self.assertEqual(p['valid_count'], 2)
        self.assert_code('schema:invalid', lambda: self.exchange.preview_import(
            legacy, {'format': 'nai_image_zip'}))

    def test_unsafe_names_links_duplicates_and_bomb(self):
        for name in ('../escape', '/absolute', 'images/a:ads.png', 'images\\a.png',
                     'images/CON.png', 'images/a. /b'):
            self.assert_code('import:unsafe_archive', lambda: read_image_pack(self.rebuild({name: b'x'})))
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            info = zipfile.ZipInfo('images/link.png')
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, b'target')
        self.assert_code('import:unsafe_archive', lambda: read_image_pack(stream.getvalue()))
        self.assert_code('import:unsafe_archive', lambda: read_image_pack(
            self.rebuild({'group.json': b'{}', 'GROUP.JSON': b'{}'})))
        self.assert_code('import:limit', lambda: read_image_pack(
            self.rebuild({'group.json': b'0' * 1000000}, zipfile.ZIP_DEFLATED)))
        with patch('stylelora.prompts.group_image_pack.ENTRY_LIMIT', 1):
            self.assert_code('import:limit', lambda: read_image_pack(self.rebuild(
                {'group.json': b'{}', 'a': b'a', 'b': b'b'})))
        with patch('stylelora.prompts.group_image_pack.IMAGE_LIMIT', 10):
            self.assert_code('import:limit', lambda: read_image_pack(self.rebuild({'a': b'x'*11})))
        with patch('stylelora.prompts.group_image_pack.EXPANDED_LIMIT', 10):
            self.assert_code('import:limit', lambda: read_image_pack(self.rebuild({'group.json': b'x'*11})))

    def test_export_missing_corrupt_empty_and_size_limit(self):
        with patch.object(self.store, 'image', side_effect=PromptError('image:not_found')):
            self.assert_code('image:invalid', self.pack)
        with patch.object(self.store, 'image', return_value=(b'corrupt', 'ignored')):
            self.assert_code('image:invalid', self.pack)
        with patch('stylelora.prompts.group_image_pack.IMAGE_LIMIT', 10):
            self.assert_code('image:too_large', self.pack)
        group = self.store.load(self.ref)
        self.store.save(self.ref, replace(group, entries=()), group.revision, 'empty')
        self.assert_code('import:empty', self.pack)
        self.assertEqual(json.loads(self.exchange.export_group(self.ref))['prompts'], [])

    def test_clean_pixels_strip_metadata_and_alpha(self):
        stream = io.BytesIO()
        info = PngImagePlugin.PngInfo()
        info.add_text('signed_hash', 'fake')
        info.add_text('Comment', '{"prompt":"old"}')
        Image.new('RGBA', (16, 16), (12, 34, 56, 254)).save(stream, 'PNG', pnginfo=info)
        png = encode_png('new', stream.getvalue())
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.mode, 'RGB')
            self.assertEqual(set(image.info), {'Comment'})
        self.assertEqual(extract_positive(png).text, 'new')
        self.assert_code('image:invalid', lambda: encode_png('x', b'broken'))


if __name__ == '__main__':
    unittest.main()
