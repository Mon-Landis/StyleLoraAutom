import io
import json
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from fastapi.testclient import TestClient
from stylelora.api import create_app
from stylelora.runtime import Runtime
from stylelora.prompts.group_exchange import GroupExchange
from stylelora.prompts.group_store import GroupStore, JSON_LIMIT, ENTRY_LIMIT
from stylelora.prompts.group_txt import HEADER, LEGACY_HEADER, encode_group_txt, decode_group_txt
from stylelora.prompts.groups import GroupRef, GeneratedFields, PendingField, FieldVersions, content, validate_entry
from stylelora.prompts.types import PromptError


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.default = False
        self.calls = 0
        def preference():
            self.calls += 1
            return self.default
        self.store = GroupStore(Path(self.temp.name), auto_complete_default=preference)
        self.exchange = GroupExchange(self.store, clock=lambda: 100)
        self.row = dict(nl='A forest.', disable=True,
                        variants={'多行': ' A\r\nB\rC\t"尾" ', 'literal': r'A\nB', 'empty': '', 'a.b': '雪'})

    def preview(self, rows=None, metadata=None, **options):
        raw = dict(prompts=rows if rows is not None else [self.row], group_metadata=metadata or {})
        return self.exchange.preview_import(json.dumps(raw).encode(), options)

    def commit(self, preview, key=None):
        return self.exchange.commit_import(preview['preview_id'], preview['digest'],
                                           preview['base_revision'], key or str(uuid4()))

    def load(self, preview):
        return self.store.load(GroupRef(**preview['ref']))

    def test_roundtrip_all_formats_and_copy_inherits(self):
        preview = self.preview(metadata={'auto_complete': False})
        self.commit(preview)
        ref = GroupRef(**preview['ref'])
        self.default = True
        expected = content(self.load(preview).entries[0])
        for format in ('json', 'txt', 'zip', 'folder'):
            with self.subTest(format=format):
                data = self.exchange.export_group(ref, format)
                if format == 'folder':
                    package = Path(self.temp.name)/'package'
                    package.mkdir()
                    with zipfile.ZipFile(io.BytesIO(self.exchange.artifacts[data['handle']][1])) as archive:
                        archive.extractall(package)
                    data = package
                copied = self.exchange.preview_import(data, {'format': format})
                self.assertFalse(copied['metadata']['auto_complete'])
                self.assertNotEqual(copied['ref'], preview['ref'])
                self.commit(copied)
                entry = self.load(copied).entries[0]
                self.assertEqual(content(entry), expected)
                self.assertEqual(entry.generated, GeneratedFields())
                self.assertFalse(entry.pending)
                self.assertEqual(entry.edit_versions, FieldVersions())
        self.assertEqual(self.calls, 0)

    def test_default_frozen_and_confirm_replay(self):
        raw = json.dumps({'prompts': [self.row]}).encode()
        preview = self.exchange.preview_import(raw, request_key='preview')
        self.default = True
        self.assertEqual(self.exchange.preview_import(raw, request_key='preview'), preview)
        result = self.commit(preview, 'commit')
        self.assertEqual(self.commit(preview, 'commit'), result)
        self.assertFalse(self.load(preview).auto_complete)
        self.assertEqual(self.calls, 1)

    def test_append_keeps_target_switch_and_variant_distinctions(self):
        target = self.preview(metadata={'auto_complete': False})
        self.commit(target)
        data = encode_group_txt({'auto_complete': True}, [validate_entry(self.row | {'variants': {'other': ''}})])
        preview = self.exchange.preview_import(data, {'format': 'txt', 'level': 'entries', 'ref': target['ref']})
        result = self.commit(preview)
        self.assertEqual(result['inserted'], 1)
        self.assertFalse(self.load(target).auto_complete)
        self.assertEqual(len(self.load(target).entries), 2)
        preview = self.preview(rows=[self.row | {'variants': {'json': ''}}, {'nl': 'invalid', 'generated': {}}],
                               metadata={'auto_complete': True}, level='entries', ref=target['ref'])
        self.assertEqual(preview['skipped_count'], 1)
        self.assertEqual(preview['issues'][0]['row'], 2)
        self.commit(preview)
        self.assertFalse(self.load(target).auto_complete)
        self.assertEqual(len(self.load(target).entries), 3)

    def test_txt_escapes_bom_crlf_and_unicode(self):
        entry = validate_entry(self.row)
        data = encode_group_txt({'name': '测试'}, [entry])
        self.assertEqual(len(data.splitlines()), 3)
        self.assertFalse(data.startswith(b'\xef\xbb\xbf'))
        self.assertIn(b'A\\nB', data)
        self.assertIn(b'A\\\\nB', data)
        decoded = decode_group_txt(b'\xef\xbb\xbf'+data.replace(b'\n', b'\r\n'))
        self.assertEqual(content(validate_entry(decoded['prompts'][0])), content(entry))
        self.assertEqual(decoded['row_numbers'], [3])

    def test_txt_partial_rows_have_physical_numbers(self):
        lines = [LEGACY_HEADER, '{"group_metadata":{}}', json.dumps(self.row),
                 '{"nl":"x","variants":{"a":"x","a":"y"}}',
                 '{"nl":"x","variants":{" a":"x","a ":"y"}}',
                 '{"nl":"x","variants":{"a":1}}', '{"nl":"x","generated":{}}',
                 '{broken', '', '{"nl":"second"}']
        preview = self.exchange.preview_import('\n'.join(lines).encode(), {'format': 'txt'})
        self.assertEqual(preview['valid_count'], 2)
        self.assertEqual(preview['skipped_count'], 6)
        self.assertEqual({i['row'] for i in preview['issues']}, set(range(4, 10)))
        self.commit(preview)
        self.assertEqual(len(self.load(preview).entries), 2)

    def test_header_metadata_errors_block_import(self):
        for text, row in ((HEADER.replace('v2', 'v3')+'\nforest', 1),
                          (HEADER+'\n{"group_metadata":{"auto_complete":1}}\n{"nl":"x"}', 2),
                          (HEADER+'\n{"group_metadata":{},"group_metadata":{}}', 2),
                          (HEADER+'\n{"group_metadata":{"unknown":1}}', 2)):
            with self.subTest(text=text), self.assertRaises(PromptError) as caught:
                self.exchange.preview_import(text.encode(), {'format': 'txt'})
            self.assertEqual(caught.exception.issues[0].row, row)

    def test_legacy_txt_and_missing_switch(self):
        preview = self.exchange.preview_import('forest, a quiet place\\nunder stars\n\n雪景'.encode(), {'format': 'txt'})
        self.assertEqual(preview['valid_count'], 2)
        self.assertTrue(preview['warnings'])
        self.default = True
        self.commit(preview)
        group = self.load(preview)
        self.assertFalse(group.auto_complete)
        self.assertTrue(all(not e.variants for e in group.entries))
        self.assertIn('\n', group.entries[0].nl)
        self.default = False
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('group.json', json.dumps({'prompts': [self.row | {'entry_id': str(uuid4())}]}))
        preview = self.exchange.preview_import(stream.getvalue(), {'format': 'zip'})
        self.default = True
        self.commit(preview)
        self.assertFalse(self.load(preview).auto_complete)

    def test_internal_state_rejected_and_not_exported(self):
        for field in ('generated', 'pending', 'edit_versions', 'completion', 'preview', '_operations'):
            for format in ('json', 'txt', 'zip'):
                row = self.row | {field: {}}
                if format == 'zip':
                    row['entry_id'] = str(uuid4())
                raw = json.dumps({'prompts': [row]}).encode()
                if format == 'txt':
                    raw = (LEGACY_HEADER+'\n{"group_metadata":{}}\n'+json.dumps(row)).encode()
                elif format == 'zip':
                    stream = io.BytesIO()
                    with zipfile.ZipFile(stream, 'w') as archive:
                        archive.writestr('group.json', raw)
                    raw = stream.getvalue()
                with self.subTest(field=field, format=format), self.assertRaises(PromptError):
                    self.exchange.preview_import(raw, {'format': format})
        preview = self.preview()
        self.commit(preview)
        ref = GroupRef(**preview['ref'])
        def mark(group):
            entry = replace(group.entries[0], generated=GeneratedFields(nl=True, variants={'多行': True}),
                             pending={'nl': PendingField('pending', 'fake', 1, 0, 'digest')},
                            edit_versions=FieldVersions(nl=2))
            return replace(group, entries=(entry,)), {}
        self.store.mutate(ref, 1, 'mark', 'test', {}, mark)
        for format in ('json', 'txt', 'zip'):
            data = self.exchange.export_group(ref, format)
            if format == 'zip':
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    data = archive.read('group.json')
            for field in ('generated', 'pending', 'edit_versions', 'completion', '_operations'):
                self.assertNotIn(('"'+field+'"').encode(), data)

    def test_limits_encoding_empty_and_json_duplicate_keys(self):
        for data, format, code in ((b'x'*(JSON_LIMIT+1), 'txt', 'import:limit'),
                                   ((LEGACY_HEADER+'\n{"group_metadata":{}}\n'+ '{"nl":"x"}\n'* (ENTRY_LIMIT+1)).encode(), 'txt', 'import:limit'),
                                   (b'\xff', 'txt', 'import:encoding'), (b'', 'txt', 'import:empty'),
                                   (b'{"prompts":[],"prompts":[]}', 'json', 'schema:invalid')):
            with self.subTest(format=format, code=code), self.assertRaises(PromptError) as caught:
                self.exchange.preview_import(data, {'format': format})
            self.assertEqual(caught.exception.code, code)


class ExchangeApiTests(unittest.TestCase):
    def test_txt_multipart_export_and_confirm(self):
        with TemporaryDirectory() as temp:
            rt = Runtime(Path(temp)/'product')
            with TestClient(create_app(rt), base_url='http://127.0.0.1:8000') as client:
                client.headers['Authorization'] = 'Bearer '+rt.token
                data = encode_group_txt({'auto_complete': False}, [validate_entry({'nl': 'Forest', 'variants': {'x': 'A\nB'}})])
                response = client.post('/api/prompt-imports/preview',
                                       files={'file': ('group.txt', data, 'text/plain')},
                                       data={'options': '{"format":"txt"}'}, headers={'Idempotency-Key': 'preview'})
                self.assertEqual(response.status_code, 200, response.text)
                preview = response.json()
                response = client.post('/api/prompt-imports/'+preview['preview_id']+'/confirm',
                                       json={'digest': preview['digest']},
                                       headers={'Idempotency-Key': 'confirm', 'If-None-Match': '*'})
                self.assertEqual(response.status_code, 200, response.text)
                confirmed = response.json()
                from stylelora.runtime.prompt_groups import service
                service(rt).exchange = GroupExchange(service(rt).store)
                response = client.post('/api/prompt-imports/'+preview['preview_id']+'/confirm',
                                       json={'digest': preview['digest'], 'ref': preview['ref']},
                                       headers={'Idempotency-Key': 'confirm', 'If-None-Match': '*'})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), confirmed)
                response = client.get('/api/prompt-groups/'+preview['ref']['group_id']+'/export?format=txt')
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers['content-type'], 'text/plain; charset=utf-8')
                self.assertIn('.txt"', response.headers['content-disposition'])
                decoded = decode_group_txt(response.content)
                self.assertFalse(decoded['group_metadata']['auto_complete'])
                self.assertEqual(decoded['prompts'][0]['variants'], {'x': 'A\nB'})
