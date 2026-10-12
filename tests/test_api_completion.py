import json
import unittest
from uuid import uuid4
import test_f11_api


class CompletionTests(unittest.TestCase):
    setUp = test_f11_api.ApiTests.setUp
    post = test_f11_api.ApiTests.post
    wait = test_f11_api.ApiTests.wait
    image = test_f11_api.ApiTests.image

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text)
        return response.json() if status != 204 else None

    def operation(self, response):
        value = self.ok(response, 202)
        result = self.wait(value['operation_id'])
        self.assertEqual(result['status'], 'done', result)
        return result['result']

    def test_A023_A027_presets(self):
        built = self.ok(self.client.get('/api/presets'))['items'][0]
        value = {'name': 'Custom', 'sizes': built['sizes']}
        created = self.ok(self.post('/api/presets', value), 201)
        url = '/api/presets/' + created['preset_id']
        self.assertEqual(self.ok(self.client.get(url))['name'], 'Custom')
        updated = self.ok(self.client.put(url, json=value | {'name': 'Changed'},
            headers={'If-Match': '"' + created['version'] + '"'}))
        self.assertEqual(updated['name'], 'Changed')
        self.ok(self.client.delete(url, headers={'If-Match': '"' + updated['version'] + '"'}), 204)
        self.assertEqual(self.client.delete('/api/presets/large', headers={'If-Match': '"1"'}).status_code, 409)

    def test_A015_A018_delete(self):
        from unittest.mock import Mock
        self.rt.product.secret_store = Mock()
        endpoint = self.ok(self.post('/api/llm/endpoints', {'base_url': 'http://127.0.0.1:9',
            'protocol': 'openai_chat', 'models': [{'name': 'test'}]}), 201)
        url = '/api/llm/endpoints/' + endpoint['endpoint_id']
        token = self.ok(self.post(url + '/tokens', {'value': 'fake'}), 201)
        self.ok(self.client.delete(url + '/tokens/' + token['token_ref'],
            headers={'If-Match': '"' + token['version'] + '"'}), 204)
        self.rt.product.secret_store.delete_secret.assert_called_once()
        current = self.ok(self.client.get(url))
        self.ok(self.client.delete(url, headers={'If-Match': '"' + current['version'] + '"'}), 204)

    def upload(self, url, data, **form):
        return self.client.post(url, files={'file': ('input.json', data, 'application/json')},
            data=form, headers={'Idempotency-Key': str(uuid4())})

    def test_A032_A036_dictionary_exchange(self):
        value = {'kind': 'prompts', 'name': 'Dict', 'entries': [{'tags': ['forest']}]}
        dictionary = self.ok(self.post('/api/dictionaries', value), 201)
        url = '/api/dictionaries/' + dictionary['dictionary_id']
        exported = self.client.get(url + '/export?format=json')
        self.ok(exported)
        preview = self.ok(self.upload('/api/dictionaries/imports/preview', exported.content,
                                      format='json', kind='prompts'))
        committed = self.ok(self.post(url + '/imports', {k: preview[k] for k in ('preview_id', 'digest')},
                                     **{'If-Match': '"' + dictionary['version'] + '"'}))
        self.assertEqual(committed['duplicates'], 1)
        current = self.ok(self.client.get(url))
        self.ok(self.client.delete(url, headers={'If-Match': '"' + current['version'] + '"'}), 204)

    def test_A072_A073_rules_import(self):
        preview = self.ok(self.upload('/api/rules/imports/preview', b'[{"name":"test","write":["forest"]}]'))
        self.assertTrue(preview['issues'])
        current = self.ok(self.client.get('/api/rules'))
        report = self.ok(self.post('/api/rules/imports/commit', {k: preview[k] for k in ('preview_id', 'digest')},
                         **{'If-Match': '"' + current['version'] + '"'}))
        self.assertEqual(report['inserted'], 1)

    def test_A117_A122_storage(self):
        self.image()
        self.assertIsNone(self.ok(self.client.get(self.base + '/storage/summary')))
        project = self.rt.current.handle.root
        (project/'models').mkdir()
        (project/'models'/'weights.onnx').write_bytes(b'x'*1000)
        scan = self.ok(self.post(self.base + '/storage/scans', {}), 202)
        result = self.wait(scan['operation_id'])
        self.assertEqual(result['status'], 'done', result)
        page = self.ok(self.client.get('/api/storage/scans/' + scan['operation_id']))
        self.assertEqual(page['items'][0]['threshold_bytes'], 5_000_000_000)
        self.assertEqual(sum(page['items'][0]['partitions'].values()), page['items'][0]['total_bytes'])
        self.assertTrue(self.ok(self.client.get(self.base + '/storage/summary'))['stale'])
        self.assertEqual(len(self.operation(self.post('/api/storage/scans', {}))), 1)
        response = self.client.get(self.base + '/images/1/thumbnail')
        self.assertEqual(response.status_code, 200)
        preview = self.operation(self.post(self.base + '/storage/cleanup/preview', {'kind': 'thumbnails'}))
        self.assertEqual(preview['count'], 1)
        report = self.operation(self.post(self.base + '/storage/cleanup',
            {k: preview[k] for k in ('preview_id', 'digest')} | {'confirm': True}))
        self.assertEqual(report['items'][0]['status'], 'deleted')
        self.assertTrue((project/'models'/'weights.onnx').exists())

    def test_A065_A067_A069_annotations(self):
        self.image()
        image = self.ok(self.client.get(self.base + '/images/1'))
        self.operation(self.post(self.base + '/images/1/verify-file', {}, **{'If-Match': '"' + image['version'] + '"'}))
        annotation = self.ok(self.client.get(self.base + '/annotations/1'))
        result = self.ok(self.post(self.base + '/annotations/manual/bulk', {'ids': [1], 'add': ['forest'],
            'remove': [], 'read_set': annotation['refs'], 'expected_versions': {'1': annotation['version']}}))
        self.assertEqual(result['changed_count'], 1)
        history = self.ok(self.client.get(self.base + '/annotations/1/history?source=manual&type=tags'))
        self.assertEqual(len(history['items']), 1)
        self.assertTrue(history['items'][0]['is_current'])

    def test_A047_A100_A110_A133_A137(self):
        self.image()
        project = self.ok(self.client.get(self.base))
        self.operation(self.post(self.base + '/recovery/reconcile', {}, **{'If-Match': '"' + project['version'] + '"'}))
        scheme = self.ok(self.client.get(self.base + '/import-schemes'))['items'][0]
        result = self.ok(self.post(self.base + '/import-schemes/1/set-default', {'expected_versions': {'1': scheme['version']}}))
        self.assertTrue(result['is_default'])
        summary = self.ok(self.client.get(self.base + '/imports/1/summary'))
        self.assertEqual(summary['counts']['done'], 1)
        scan = self.ok(self.post(self.base + '/duplicates/scans', {'threshold': 0, 'scope': {'selected_ids': [1]}}), 202)
        self.wait(scan['operation_id'])
        url = self.base + '/duplicates/scans/' + scan['scan_id']
        page = self.ok(self.client.get(url))
        self.assertEqual(page['skipped']['null'], 1)
        self.ok(self.client.delete(url, headers={'If-Match': '"' + page['version'] + '"'}), 204)
        self.assertEqual(self.client.get(url).status_code, 410)
        self.assertEqual(self.ok(self.client.get('/api/tagger/models'))['items'], [])

    def test_A085_no_rating_adapter(self):
        self.image()
        result = self.post(self.base + '/ratings/reclassify', {'scope': {'selected_ids': [1]}})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()['error']['code'], 'rating:unavailable')

    def test_A037_A091_prompt_coverage_append(self):
        from test_f07_prompts import TaskTests
        from stylelora.runtime.contracts import plain
        dictionary = self.ok(self.post('/api/dictionaries', {'kind': 'prompts', 'name': 'input',
            'entries': [{'tags': ['forest']}]}), 201)
        request = plain(TaskTests().request(n=1, optimize=False))
        request['prompt_dictionary_ids'] = [dictionary['dictionary_id']]
        coverage = self.operation(self.post('/api/coverage/preview', request))
        self.assertEqual(coverage['status'], 'satisfied')
        from test_f07_prompts import Fake
        self.rt.llm = Fake()
        request['optimize'] = True
        task = self.ok(self.post('/api/prompt-tasks', request), 202)
        import time
        for _ in range(500):
            task = self.ok(self.client.get('/api/prompt-tasks/' + task['task_id']))
            if task['status'] == 'done':
                break
            time.sleep(.002)
        result = self.ok(self.post('/api/prompt-tasks/' + task['task_id'] + '/append',
            {'dictionary_id': dictionary['dictionary_id'], 'indices': [0], 'expected_versions': {
                dictionary['dictionary_id']: dictionary['version'], task['task_id']: task['version']}}))
        self.assertEqual(result['inserted'] + result['duplicates'], 1)

    def test_A109_root_manifest(self):
        from stylelora.exchange.importer import ImportService
        from stylelora.tagging.catalog import LocalCatalog
        from PIL import Image
        root = self.root/'input'
        root.mkdir()
        Image.new('RGB', (8, 8)).save(root/'one.png')
        catalog = LocalCatalog(translations={'forest': 'Forest'})
        svc = ImportService(catalog)
        source = svc.create_import(self.rt.current.handle, svc.register_root(root))
        current = self.ok(self.client.get(self.base + '/sources/' + str(source.source_id)))
        grant = self.rt.grant(root, 'import_root', self.pid)
        result = self.operation(self.post(self.base + '/sources/' + str(source.source_id) + '/import-root',
            {'root_handle': grant['grant_id']}, **{'If-Match': '"' + current['version'] + '"'}))
        self.assertTrue(result['verified'])

    def test_static_api_and_bootstrap(self):
        from stylelora.api import create_app
        from fastapi.testclient import TestClient
        dist = self.root/'dist'
        dist.mkdir()
        (dist/'index.html').write_text('<html>test app</html>')
        client = TestClient(create_app(self.rt, frontend_dist=dist), base_url='http://127.0.0.1:8000')
        self.assertIn('test app', client.get('/any/path').text)
        self.assertEqual(client.get('/api/unknown').status_code, 401)
        headers = {'Authorization': 'Bearer ' + self.rt.token}
        self.assertEqual(client.get('/api/unknown', headers=headers).status_code, 404)
        value = self.ok(client.get('/api/session?bootstrap=true', headers=headers))
        code = value['bootstrap_url'].split('#bootstrap=')[1]
        self.ok(client.post('/api/session/bootstrap', json={'code': code}, headers={'Origin': 'http://127.0.0.1:8000'}))
        self.assertEqual(client.post('/api/session/bootstrap', json={'code': code}, headers={'Origin': 'http://127.0.0.1:8000'}).status_code, 401)

    def test_exports_restart_index(self):
        from stylelora.tagging.catalog import LocalCatalog
        from stylelora.exchange.exporter import export_project
        from stylelora.runtime.exchange import Exchange
        catalog = LocalCatalog({'schema_version': 1, 'kind': 'tag_catalog', 'revision': 1,
            'tags': [{'tag': 'forest'}]}, complete=True)
        identity = str(uuid4())
        report = export_project(self.rt.current.handle, 1, catalog=catalog, request_key=identity)
        self.assertEqual(report.export_id, identity)
        self.rt.exchange = Exchange(self.rt)
        page = self.ok(self.client.get(self.base + '/exports'))
        self.assertEqual(page['items'][0]['export_id'], identity)
        self.assertEqual(self.ok(self.client.get(self.base + '/exports/' + identity + '/report'))['export_id'], identity)
        root = self.rt.current.handle.root
        project = self.ok(self.client.get(self.base))
        self.operation(self.post(self.base + '/close', {}, **{'If-Match': '"' + project['version'] + '"'}))
        grant = self.rt.grant(root, 'project_open')
        reopened = self.operation(self.post('/api/projects/open', {'grant_id': grant['grant_id']}))
        self.rt.exchange = Exchange(self.rt)
        url = '/api/projects/' + reopened['project_id'] + '/exports/' + identity
        self.assertEqual(self.ok(self.client.get(url))['status'], 'finished')

    def test_cleanup_stale_and_rejected_missing(self):
        self.image()
        image = self.ok(self.client.get(self.base + '/images/1'))
        self.ok(self.client.patch(self.base + '/images/1', json={'review_status': 'reject'},
            headers={'If-Match': '"' + image['version'] + '"'}))
        preview = self.operation(self.post(self.base + '/storage/cleanup/preview', {'kind': 'rejected_files'}))
        payload = {k: preview[k] for k in ('preview_id', 'digest')} | {'confirm': True}
        denied = self.ok(self.post(self.base + '/storage/cleanup', payload | {'confirm': False}), 202)
        self.assertEqual(self.wait(denied['operation_id'])['error']['code'], 'cleanup:confirmation_required')
        report = self.operation(self.post(self.base + '/storage/cleanup', payload))
        self.assertEqual(report['items'][0]['status'], 'deleted')
        image = self.ok(self.client.get(self.base + '/images/1'))
        self.assertTrue(image['file_missing'])
        self.assertEqual(image['review_status'], 'reject')
        stale = self.ok(self.post(self.base + '/storage/cleanup', payload), 202)
        self.assertEqual(self.wait(stale['operation_id'])['error']['code'], 'cleanup:preview_stale')

    def test_ui_discovery_dpapi_and_command(self):
        import io
        from unittest.mock import patch, Mock
        from stylelora.runtime.instance import publish_instance, attach_url, discovery_path
        from stylelora.__main__ import main
        self.assertTrue(discovery_path().is_relative_to(__import__('pathlib').Path.home()))
        path = self.root/'user'/'instance.json'
        with patch('stylelora.runtime.instance.discovery_path', return_value=path):
            publish_instance(self.rt)
            self.assertNotIn(self.rt.token, path.read_text())
            result = {'instance_id': self.rt.instance_id, 'bootstrap_url': 'http://127.0.0.1:8000/#bootstrap=one'}
            opener = Mock()
            opener.open.return_value = io.BytesIO(json.dumps(result).encode())
            with patch('urllib.request.build_opener', return_value=opener):
                self.assertEqual(attach_url(), result['bootstrap_url'])
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_header('Authorization'), 'Bearer ' + self.rt.token)
        with patch('stylelora.__main__.attach_url', return_value=result['bootstrap_url']), patch('stylelora.__main__.webbrowser.open') as browser:
            self.assertEqual(main(['ui']), 0)
            browser.assert_called_once_with(result['bootstrap_url'])

    def test_external_501_reasons(self):
        paths = [('POST', '/llm/endpoints/x/models/probe'), ('POST', '/llm/endpoints/x/models/test-image'),
            ('POST', '/dictionaries/launcher/preview'), ('GET', '/backends/nai/capabilities'),
            ('POST', '/tagger/models/install'), ('GET', '/tagger/installations/x'), ('DELETE', '/tagger/installations/x')]
        for method, path in paths:
            response = self.client.request(method, '/api' + path, json={} if method == 'POST' else None,
                headers={'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'})
            self.assertEqual(response.status_code, 501)
            self.assertIn('reason', response.json()['error']['details'])
            self.assertNotEqual(response.json()['error']['code'], 'feature:unsupported')

    def test_A127_A130_translations(self):
        self.ok(self.client.put('/api/translations', json={'tag': 'forest', 'value': '树林'}, headers={'If-Match': '"0"'}))
        found = self.ok(self.post('/api/tags/translations/lookup', {'tags': ['forest', 'unknown']}))
        self.assertEqual(found['items'][0]['translation'], '树林')
        self.assertTrue(found['items'][1]['unknown'])
        preview = self.ok(self.upload('/api/tags/imports/preview', b'{"sky":"Sky"}', kind='translations', format='json'))
        report = self.ok(self.post('/api/tags/imports/commit', {k: preview[k] for k in ('preview_id', 'digest')},
                                  **{'If-Match': '"1"'}))
        self.assertEqual(report['inserted'], 1)
