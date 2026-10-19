import hashlib
import io
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image, PngImagePlugin
from starlette.requests import Request

from stylelora.api.uploads import bounded_body, upload
from stylelora.prompts.types import PromptError
from stylelora.runtime.contracts import Failure
from stylelora.runtime.prompt_groups import service
from test_f27_api import GroupApiTests as _GroupApiTests


def png(text='cat', color='red'):
    stream = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_itxt('Comment', json.dumps({'prompt': text}))
    Image.new('RGB', (24, 16), color).save(stream, 'PNG', pnginfo=metadata)
    return stream.getvalue()


class F31HttpTests(unittest.TestCase):
    setUp = _GroupApiTests.setUp
    headers = _GroupApiTests.headers
    group = _GroupApiTests.group
    entry = _GroupApiTests.entry

    def preview(self, files=None, options=None, headers=None):
        return self.client.post('/api/prompt-imports/preview',
            files=files if files is not None else [('files', ('same.png', png(), 'image/png'))],
            data={'options': json.dumps(options or {'format': 'nai_images', 'level': 'group'})},
            headers=headers or self.headers())

    def confirm(self, preview, headers=None):
        return self.client.post('/api/prompt-imports/' + preview['preview_id'] + '/confirm',
            json={'digest': preview['digest'], 'ref': preview['ref']},
            headers=headers or self.headers(create=True))

    def test_multiple_files_order_duplicates_confirm_and_durable_replay(self):
        files = [('files', ('same.png', png(color=color), 'image/png')) for color in ('red', 'blue')]
        files.insert(1, ('files', ('broken.png', b'broken', 'image/png')))
        headers = self.headers()
        response = self.preview(files, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertEqual([(r['file_index'], r['name'], r['status']) for r in preview['files']],
                         [(1, 'same.png', 'valid'), (2, 'broken.png', 'skipped'), (3, 'same.png', 'valid')])
        self.assertEqual(preview['valid_count'], 2)
        self.assertEqual(self.preview(files, headers=headers).json(), preview)
        changed = self.preview(list(reversed(files)), headers=headers)
        self.assertEqual(changed.status_code, 409, changed.text)
        headers = self.headers(create=True)
        confirmed = self.confirm(preview, headers)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()['inserted'], 2)
        self.assertEqual(confirmed.headers['etag'], '"1"')
        service(self.rt).exchange.previews.clear()
        self.assertEqual(self.confirm(preview, headers).json(), confirmed.json())

    def test_empty_reports_and_options_validation(self):
        response = self.preview([('files', ('bad.png', b'bad', 'image/png'))])
        self.assertEqual(response.status_code, 422, response.text)
        error = response.json()['error']
        self.assertEqual(error['code'], 'import:empty')
        self.assertEqual(error['details']['files'][0]['name'], 'bad.png')
        self.assertTrue(error['details']['files'][0]['issues'])
        for options in ({'format': 'nai_images', 'level': 'entries'},
                        {'format': 'nai_images', 'source_format': 'anima'}):
            self.assertEqual(self.preview(options=options).status_code, 422)
        for files in ([('file', ('one.png', png()))],
                      [('files', ('one.png', png())), ('file', ('two.png', png()))],
                      [('files', ('one.png', png())), ('options', (None, '{}'))]):
            self.assertEqual(self.preview(files).status_code, 422)

    def test_append_requires_revision(self):
        path = self.group()
        ref = {'scope': 'global', 'project_token': None, 'group_id': path.rsplit('/', 1)[1]}
        response = self.preview(options={'format': 'nai_images', 'level': 'entries', 'ref': ref})
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertEqual(self.confirm(preview, self.headers()).status_code, 428)
        self.assertEqual(self.confirm(preview, self.headers(0)).status_code, 412)
        response = self.confirm(preview, self.headers(1))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['etag'], '"2"')

    def test_limits_before_and_during_read_and_file_count(self):
        with patch('stylelora.api.security.IMPORT_BODY_LIMIT', 128):
            response = self.client.post('/api/prompt-imports/preview', content=b'{}',
                headers=self.headers() | {'Content-Type': 'application/json', 'Content-Length': '129'})
            self.assertEqual(response.status_code, 413, response.text)
            response = self.client.post('/api/prompt-imports/preview', content=iter([b'x'*64, b'x'*65]),
                headers=self.headers() | {'Content-Type': 'multipart/form-data; boundary=test'})
            self.assertEqual(response.status_code, 413, response.text)
            self.assertEqual(response.json()['error']['code'], 'import:limit')
        with patch('stylelora.api.routes.prompt_groups.IMPORT_FILE_COUNT', 1):
            response = self.preview([('files', ('x.png', png())), ('files', ('x.png', png()))])
            self.assertEqual(response.status_code, 413, response.text)
        with patch('stylelora.api.routes.prompt_groups.IMPORT_FILE_LIMIT', 10):
            response = self.preview()
            self.assertEqual(response.status_code, 413, response.text)
        self.assertFalse(service(self.rt).exchange.previews)

    def test_thumbnail_cache_auth_missing_expired_and_size(self):
        preview = self.preview().json()
        path = '/api/prompt-imports/' + preview['preview_id'] + '/entries/' + preview['entries'][0]['entry_id'] + '/preview'
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['content-type'], 'image/webp')
        self.assertEqual(response.headers['etag'], '"' + hashlib.sha256(response.content).hexdigest() + '"')
        self.assertEqual(Image.open(io.BytesIO(response.content)).format, 'WEBP')
        cached = self.client.get(path, headers={'If-None-Match': '"other", W/' + response.headers['etag']})
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.content, b'')
        self.assertEqual(cached.headers['vary'], 'Authorization')
        self.assertEqual(self.client.get(path + '?size=full').status_code, 200)
        self.assertEqual(self.client.get(path + '?size=bad').status_code, 422)
        self.assertEqual(self.client.get(path, headers={'Authorization': 'Bearer bad', 'If-None-Match': '*'}).status_code, 401)
        missing = path.replace(preview['entries'][0]['entry_id'], str(uuid4()))
        self.assertEqual(self.client.get(missing).status_code, 404)
        service(self.rt).exchange.previews.clear()
        self.assertEqual(self.client.get(path, headers={'If-None-Match': '*'}).status_code, 410)

    def test_zip_export_import_round_trip_and_legacy_formats(self):
        preview = self.preview().json()
        self.assertEqual(self.confirm(preview).status_code, 200)
        path = '/api/prompt-groups/' + preview['ref']['group_id']
        self.entry(path, 1, nl='dog', variants={'bright': 'bright dog'}, disable=True)
        before = self.client.get(path + '/export').json()
        exported = self.client.get(path + '/export?format=nai_image_zip')
        self.assertEqual(exported.status_code, 200, exported.text if exported.status_code != 200 else '')
        self.assertEqual(exported.headers['content-type'], 'application/zip')
        self.assertTrue(exported.headers['content-disposition'].endswith('.zip"'))
        response = self.preview([('file', ('group.zip', exported.content, 'application/zip'))],
                                {'format': 'nai_image_zip', 'level': 'group'})
        self.assertEqual(response.status_code, 200, response.text)
        confirmed = self.confirm(response.json())
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        restored = '/api/prompt-groups/' + confirmed.json()['ref']['group_id']
        self.assertCountEqual(self.client.get(restored + '/export').json()['prompts'], before['prompts'])
        rows = self.client.get(restored + '/entries').json()['items']
        self.assertEqual(sum(row['preview'] is not None for row in rows), 1)
        for format, mime in [('json', 'application/json'), ('txt', 'text/plain'), ('zip', 'application/zip')]:
            response = self.client.get(path + '/export', params={'format': format})
            self.assertEqual(response.status_code, 200, response.text if response.status_code != 200 else '')
            self.assertTrue(response.headers['content-type'].startswith(mime))
            self.assertTrue(response.headers['content-disposition'].endswith('.' + format + '"'))
        self.assertEqual(self.client.get(path + '/export?format=unknown').status_code, 422)

    def test_export_filename_cannot_inject_headers(self):
        with patch.object(service(self.rt).exchange, 'export_group', return_value=b'{}'), \
             patch.object(service(self.rt), 'ref', return_value=None):
            response = self.client.get('/api/prompt-groups/evil%22%0D%0AX-Injected%3Ayes/export')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('x-injected', response.headers)
        self.assertEqual(response.headers['content-disposition'], 'attachment; filename="evil___X-Injected_yes.json"')

    def test_error_mapping_over_http(self):
        statuses = {413: ['import:limit', 'image:too_large'],
                    422: ['image:invalid', 'import:metadata_mismatch', 'import:unsafe_archive',
                          'import:empty', 'schema:invalid', 'normalization:syntax',
                          'normalization:empty', 'import:conversion_failed'],
                    404: ['image:not_found'], 409: ['entry:id_conflict']}
        for status, codes in statuses.items():
            for code in codes:
                with self.subTest(code=code), patch.object(service(self.rt).exchange, 'preview_images', side_effect=PromptError(code)):
                    response = self.preview()
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertEqual(response.json()['error']['code'], code)


class UploadReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_tiny_parts_stop_receiving_at_1001st_header(self):
        calls = 0
        async def receive():
            nonlocal calls
            calls += 1
            self.assertLessEqual(calls, 1001)
            return {'type': 'http.request', 'more_body': True, 'body':
                (b'--test\r\n' if calls == 1 else b'\r\n--test\r\n') +
                b'Content-Disposition: form-data; name="files"; filename="same"\r\n\r\nx'}
        request = Request({'type': 'http', 'headers': [
            (b'content-type', b'multipart/form-data; boundary=test')]}, receive)
        with self.assertRaises(Failure) as caught:
            await upload(request, {'files'}, {'files'}, repeated=('files',),
                         file_count=1000, limit_code='import:limit')
        self.assertEqual((caught.exception.status, caught.exception.code), (413, 'import:limit'))
        self.assertEqual(calls, 1001)

    async def test_fragmented_boundaries_and_file_bytes_stop_early(self):
        raw = (b'--test\r\nContent-Disposition: form-data; name="file"\r\n\r\n'
               b'ab\r\n--testXz\r\n--test--\r\n')
        calls = 0
        async def receive():
            nonlocal calls
            value = raw[calls:calls + 1]
            calls += 1
            return {'type': 'http.request', 'body': value, 'more_body': calls < len(raw)}
        def request():
            return Request({'type': 'http', 'headers': [
                (b'content-type', b'multipart/form-data; boundary=test')]}, receive)
        result = await upload(request(), {'file'}, {'file'})
        self.assertEqual(result['file'], b'ab\r\n--testXz')
        calls = 0
        with self.assertRaises(Failure) as caught:
            await upload(request(), {'file'}, {'file'}, file_limit=1, limit_code='import:limit')
        self.assertEqual(caught.exception.status, 413)
        self.assertLess(calls, len(raw))

    async def test_preflight_does_not_read_and_stream_stops_at_limit(self):
        calls = []

        async def receive():
            calls.append(True)
            return {'type': 'http.request', 'body': b'123', 'more_body': True}

        request = Request({'type': 'http', 'headers': [(b'content-length', b'7')]}, receive)
        with self.assertRaises(Failure) as raised:
            await bounded_body(request, 6, 'import:limit')
        self.assertEqual(raised.exception.status, 413)
        self.assertEqual(calls, [])
        request = Request({'type': 'http', 'headers': []}, receive)
        with self.assertRaises(Failure):
            await bounded_body(request, 5, 'import:limit')
        self.assertEqual(len(calls), 2)


# unittest discovery must not collect the imported fixture class a second time.
del _GroupApiTests
