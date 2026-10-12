"""HTTP contracts consumed by frontend/src/api; no external executors or LLM."""
import asyncio
import json
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from PIL import Image

from stylelora.api import create_app
from stylelora.runtime import Runtime
from stylelora.tagging.catalog import LocalCatalog


class FrontendContractTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        catalog = LocalCatalog({'schema_version': 1, 'kind': 'tag_catalog',
                                'revision': 1, 'tags': []}, complete=True)
        self.rt = Runtime(self.root / 'product', catalog=catalog)
        self.client = TestClient(create_app(self.rt), base_url='http://127.0.0.1:8000')
        self.client.__enter__()
        self.addCleanup(lambda: self.client.__exit__(None, None, None))

    def value(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def post(self, path, body, version=None):
        headers = {'Idempotency-Key': str(uuid4())}
        if version:
            headers['If-Match'] = f'"{version}"'
        return self.client.post(path, json=body, headers=headers)

    def shape(self, name, value, file='types.ts'):
        """Check actual responses against required TS interface keys, not a copied list."""
        source = (Path(__file__).resolve().parents[1] / 'frontend/src/api' / file).read_text(encoding='utf-8')
        match = re.search(r'export interface ' + name + r'(?:<[^>]+>)? \{\n(.*?)\n\}', source, re.S)
        self.assertIsNotNone(match, name)
        required = re.findall(r'^  (\w+):', match[1], re.M)
        self.assertTrue(required, name)
        self.assertIsInstance(value, dict)
        self.assertTrue(set(required) <= value.keys(), (name, set(required) - value.keys()))

    def wait(self, response):
        operation = self.value(response, 202)
        self.shape('Operation', operation)
        deadline = time.monotonic() + 10
        while operation['status'] in ('pending', 'running'):
            self.assertLess(time.monotonic(), deadline, operation)
            time.sleep(.01)
            operation = self.value(self.client.get('/api/operations/' + operation['operation_id']))
            self.shape('Operation', operation)
        self.assertEqual(operation['status'], 'done', operation)
        return operation['result']

    def test_frontend_main_flow(self):
        boot = self.value(self.client.post('/api/session/bootstrap',
            json={'code': self.rt.bootstrap_code()}, headers={'Origin': 'http://127.0.0.1:8000'}))
        self.assertEqual(boot['instance_id'], self.rt.instance_id)
        self.client.headers['Authorization'] = 'Bearer ' + boot['token']
        self.assertEqual(self.value(self.client.get('/api/session'))['instance_id'], boot['instance_id'])
        grant = self.rt.grant(self.root / 'contract.slproj', 'project_create')
        project = self.wait(self.post('/api/projects', {'grant_id': grant['grant_id'], 'name': 'Contract', 'trigger_word': 'style'}))
        self.shape('ProjectView', project)
        base = '/api/projects/' + project['project_id']
        known = self.value(self.client.get('/api/projects'))
        self.shape('Page', known)
        self.shape('KnownProject', known['items'][0], 'more.ts')
        self.wait(self.post(base + '/close', {}, project['version']))
        self.assertIsNone(self.value(self.client.get('/api/projects/current')))
        project = self.wait(self.post('/api/projects/open', {'known_id': known['items'][0]['known_id']}))
        self.shape('ProjectView', project)
        base = '/api/projects/' + project['project_id']

        incoming = self.root / 'incoming'
        incoming.mkdir()
        for i in range(5):
            Image.new('RGB', (16 + i, 12), (i * 30, 10, 20)).save(incoming / f'{i}.png')
        grant = self.rt.grant(incoming, 'import_root', project['project_id'])
        schemes = self.value(self.client.get(base + '/import-schemes'))
        preview = self.wait(self.post(base + '/imports/preview',
            {'scheme_id': schemes['items'][0]['scheme_id'], 'root_handle': grant['grant_id']}))
        self.wait(self.post(base + '/imports', {'preview_id': preview['preview_id'], 'digest': preview['digest'], 'name': 'PNGs'}))
        page = self.value(self.client.get(base + '/images', params={'limit': 2}))
        self.shape('Page', page)
        self.assertEqual(page['total'], 5)
        rows = list(page['items'])
        while page['has_more']:
            page = self.value(self.client.get(base + '/images', params={'limit': 2, 'cursor': page['next_cursor']}))
            self.shape('Page', page)
            self.assertEqual(page['total'], 5)
            rows.extend(page['items'])
        self.assertEqual(len({r['image_id'] for r in rows}), 5)
        for row in rows:
            self.shape('ImageItem', row)
            self.assertIsInstance(row['version'], str)
        anchor = self.value(self.client.get(base + '/images', params={'limit': 2, 'around_id': rows[3]['image_id']}))
        self.assertEqual((anchor['start_index'], anchor['anchor_index'], anchor['total']), (2, 3, 5))
        self.assertEqual(anchor['items'][1]['image_id'], rows[3]['image_id'])
        for key in ('thumbnail_url', 'original_url'):
            media = self.client.get(rows[0][key])
            self.assertEqual(media.status_code, 200)
            self.assertTrue(media.headers['content-type'].startswith('image/'))
            cached = self.client.get(rows[0][key], headers={'If-None-Match': media.headers['etag']})
            self.assertEqual(cached.status_code, 304)
            self.assertEqual(cached.content, b'')

        first = rows[0]
        result = self.value(self.post(base + '/images/review', {'ids': [first['image_id']], 'target': 'keep',
            'expected_versions': {str(first['image_id']): first['version']}}))
        self.shape('ImageItem', result['items'][0]['image'])
        preview = self.value(self.post(base + '/images/review/preview', {'query': {}}))
        body = dict(query={}, preview_id=preview['preview_id'], digest=preview['digest'], expected_total=5, target='reject')
        reviewed = self.value(self.post(base + '/images/review', body))['items']
        self.assertEqual(len(reviewed), 5)
        self.assertTrue(all(r['image']['review_status'] == 'reject' for r in reviewed))
        undone = self.value(self.post(base + '/images/review', {
            'ids': [r['image']['image_id'] for r in reviewed],
            'targets': {str(r['image']['image_id']): r['old_review_status'] for r in reviewed},
            'expected_versions': {str(r['image']['image_id']): r['image']['version'] for r in reviewed}}))
        self.assertEqual([r['image']['review_status'] for r in undone['items']], ['keep'] + ['unreviewed'] * 4)
        self.assertEqual(self.post(base + '/images/review', body).status_code, 409)
        filtered = self.value(self.client.get(base + '/images', params={'review_status': 'keep'}))
        self.assertEqual(filtered['total'], 1)
        self.assertEqual(self.value(self.client.get(base + '/images/stats'))['total'], 5)

        url = base + f'/annotations/{first["image_id"]}'
        view = self.value(self.client.get(url))
        self.shape('AnnotationView', view)
        edit = {'tags': [{'tag': 'blue hair', 'conf': None}], 'natural_language': 'Caption', 'write_nl': True, 'read_set': view['refs']}
        saved = self.value(self.client.put(url + '/manual', json=edit, headers={'If-Match': f'"{view["version"]}"'}))
        self.shape('ManualResult', saved)
        self.assertTrue(saved['changed'])
        self.assertEqual(self.client.put(url + '/manual', json=edit, headers={'If-Match': f'"{view["version"]}"'}).status_code, 409)
        fresh = self.value(self.client.get(url))
        self.shape('AnnotationView', fresh)
        self.assertNotEqual(view['version'], fresh['version'])
        self.assertEqual(fresh['tags'][0]['tag'], 'blue hair')
        self.assertEqual(self.client.put(url + '/manual', json=edit, headers={'If-Match': f'"{fresh["version"]}"'}).status_code, 409)

        rule = {'schema_version': 1, 'delete': {'mode': 'all', 'tags': ['blue hair']},
                'write': {'mode': 'all', 'tags': ['red hair']}, 'condition': None}
        preview = self.value(self.post(base + '/rules/preview', {'rule': rule, 'scope': {'selected_ids': [first['image_id']]}}))
        self.shape('Preview', preview, 'endpoints.ts')
        applied = self.value(self.post(base + '/rules/apply', {'preview_id': preview['preview_id'], 'digest': preview['digest']}, preview['digest']))
        self.assertEqual(applied['affected_count'], 1)
        log = self.value(self.client.get(base + '/operation-log', params={'limit': 20, 'sort': '-id'}))
        self.shape('Page', log)
        self.shape('OpLogRow', log['items'][0], 'endpoints.ts')
        op = log['items'][0]
        self.assertEqual(op['op_id'], applied['op_id'])
        undone = self.value(self.post(base + f'/operation-log/{op["op_id"]}/undo', {}, op['version']))
        self.shape('OpLogRow', undone, 'endpoints.ts')
        self.assertEqual(undone['status'], 'undone')
        self.assertEqual(self.value(self.client.get(base + f'/operation-log/{op["op_id"]}'))['status'], 'undone')

        schemes = self.value(self.client.get(base + '/export-schemes', params={'limit': 100}))
        self.shape('Page', schemes)
        self.shape('ExportScheme', schemes['items'][0], 'more.ts')
        sid = schemes['items'][0]['scheme_id']
        preview = self.wait(self.post(base + '/exports/preview', {'scheme_id': sid}))
        self.shape('ExportPreview', preview, 'more.ts')
        export = self.value(self.post(base + '/exports', {'scheme_id': sid, 'preview_id': preview['preview_id'], 'digest': preview['digest']}), 202)
        self.shape('ExportView', export)
        deadline = time.monotonic() + 10
        while export['status'] in ('planned', 'running'):
            self.assertLess(time.monotonic(), deadline, export)
            time.sleep(.01)
            export = self.value(self.client.get(base + '/exports/' + export['export_id']))
            self.shape('ExportView', export)
        self.assertEqual(export['status'], 'finished', export)
        self.assertEqual(self.value(self.client.get(export['report_url']))['exported'], 1)

        # TestClient buffers infinite streams. Bound transport to the first real
        # route frame, preserving authentication, serialization and disconnect cleanup.
        class FirstFrameResponse(StreamingResponse):
            def __init__(self, content, **kwargs):
                async def bounded():
                    try:
                        yield await asyncio.wait_for(anext(content), timeout=2)
                    finally:
                        await content.aclose()
                super().__init__(bounded(), **kwargs)
        with patch('stylelora.api.routes.session.StreamingResponse', FirstFrameResponse):
            response = self.client.get('/api/events', params={'project_id': project['project_id'], 'topics': 'image'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/event-stream', response.headers['content-type'])
        self.assertIn('event: stream.ready', response.text)
        data = json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith('data: ')))
        self.assertEqual(data['project_id'], project['project_id'])
        self.assertEqual(data['payload']['instance_id'], boot['instance_id'])
        self.assertEqual(self.rt.events.listeners, {})

    def test_prompt_page_sampling_body_without_llm(self):
        self.client.headers['Authorization'] = 'Bearer ' + self.rt.token
        dictionary = self.value(self.post('/api/dictionaries', {
            'kind': 'prompts', 'name': 'input', 'entries': [{'tags': ['forest']}]}), 201)
        task = self.value(self.post('/api/prompt-tasks', {
            'plan': {'primary_dimensions': ['scene', 'lighting'], 'cell_values': {'scene': ['forest'], 'lighting': ['sunlight']}, 'target_count': 1},
            'prompt_dictionary_ids': [dictionary['dictionary_id']],
            'coverage_enabled': False, 'optimize': False}), 202)
        self.assertIsInstance(task['task_id'], str)
        deadline = time.monotonic() + 5
        while task['status'] in ('pending', 'running'):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
            task = self.value(self.client.get('/api/prompt-tasks/' + task['task_id']))
        self.assertEqual(task['status'], 'done', task)
        self.assertTrue(task['ready'])
