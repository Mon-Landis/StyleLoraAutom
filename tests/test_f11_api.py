import asyncio
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from fastapi.testclient import TestClient
from PIL import Image
from stylelora.api import create_app
from stylelora.runtime import Runtime
from stylelora.project_db import commands, submit_write, read_snapshot
from stylelora.project_db.repositories import sources, images
from stylelora.project_db.types import SourceDraft, ItemSeed, ItemFilter, PageRequest, ImageArtifact
from stylelora.project_db.receipts import prepare_receipt


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rt = Runtime(self.root/'product')
        self.client = TestClient(create_app(self.rt), base_url='http://127.0.0.1:8000')
        self.client.__enter__()
        self.addCleanup(lambda: self.client.__exit__(None, None, None))
        self.client.headers['Authorization'] = 'Bearer '+self.rt.token
        grant = self.rt.grant(self.root/'test.slproj', 'project_create')
        result = self.post('/api/projects', {'grant_id': grant['grant_id'], 'name': 'Test', 'trigger_word': 'style'})
        self.assertEqual(result.status_code, 202, result.text)
        operation = self.wait(result.json()['operation_id'])
        self.assertEqual(operation['status'], 'done', operation)
        self.pid = operation['result']['project_id']
        self.base = '/api/projects/'+self.pid

    def post(self, path, value, key=None, **headers):
        return self.client.post(path, json=value, headers={'Idempotency-Key': key or str(uuid4()), **headers})

    def wait(self, identity):
        import time
        for _ in range(500):
            value = self.client.get('/api/operations/'+identity).json()
            if value['status'] not in ('pending', 'running'):
                return value
            time.sleep(.002)
        self.fail('operation did not complete')

    def image(self, number=0, alpha=False):
        project = self.rt.current.handle
        source = submit_write(project, lambda db: sources.create_source(db, SourceDraft('import', 'source',
            {'schema_version': 1, 'reader': 'txt_sidecar', 'params': {'schema_version': 1}, 'import_manifest': [f'{number}.png']}))).result()
        submit_write(project, lambda db: commands.start_source(db, source.source_id, (ItemSeed(1, str(number)),))).result()
        with read_snapshot(project) as db:
            item = sources.list_items(db, ItemFilter(source.source_id), PageRequest()).items[0]
        item = submit_write(project, lambda db: commands.mark_sent(db, item.item_id)).result().value
        output = io.BytesIO()
        Image.new('RGBA' if alpha else 'RGB', (64, 32), (number % 255, 10, 20, 100) if alpha else (number % 255, 10, 20)).save(output, 'PNG')
        data = output.getvalue()
        path = project.root/'images'/'.pending'/str(item.item_id)/str(item.attempts)/'out.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        artifact = prepare_receipt(project.root, ImageArtifact(item.item_id, item.attempts,
            path.relative_to(project.root).as_posix(), '.png', 64, 32, None, hashlib.sha256(data).hexdigest(), len(data), None, ''), ())
        return submit_write(project, lambda db: commands.complete_item(db, artifact, ())).result().value

    def test_T06_auth_covers_media_events_unknown(self):
        for path in ('/api/session', self.base+'/images/1/content', '/api/events', '/api/unknown'):
            result = self.client.get(path, headers={'Authorization': 'Bearer wrong'})
            self.assertEqual(result.status_code, 401)
            self.assertIn('request_id', result.json()['error'])

    def test_T07_bootstrap_once_origin(self):
        code = self.rt.bootstrap_code()
        headers = {'Origin': 'http://127.0.0.1:8000'}
        result = self.client.post('/api/session/bootstrap', json={'code': code}, headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers['cache-control'], 'no-store')
        self.assertEqual(self.client.post('/api/session/bootstrap', json={'code': code}, headers=headers).status_code, 401)

    def test_T08_T09_host_origin_cors(self):
        for headers in ({'Host': 'localhost:8000'}, {'Origin': 'null'}, {'Origin': 'http://evil.test'}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.client.get('/api/session', headers=headers).status_code, 403)
        self.assertEqual(self.client.get('/api/session', headers={'X-Forwarded-Host': 'evil'}).status_code, 200)
        result = self.client.options('/api/config', headers={'Origin': 'http://127.0.0.1:8000'})
        self.assertEqual(result.status_code, 204)
        self.assertNotIn('access-control-allow-credentials', result.headers)

    def test_T10_T11_malformed_and_unknown(self):
        for raw in ('{"wd_enabled":true,"wd_enabled":false}', '{"cost_policy":NaN}', '{'):
            result = self.client.patch('/api/config', content=raw, headers={'Content-Type': 'application/json', 'If-Match': '"0"'})
            self.assertEqual(result.status_code, 400)
        self.assertEqual(self.client.patch('/api/config', json={'secret': 'private'}, headers={'If-Match': '"0"'}).status_code, 422)
        result = self.client.get('/api/does-not-exist', headers={'Accept': 'text/html'})
        self.assertEqual(result.status_code, 404)
        self.assertEqual(result.headers['content-type'], 'application/json')

    def test_T14_product_cas_noop_ABA(self):
        url = '/api/config'
        self.assertEqual(self.client.patch(url, json={'wd_enabled': True}).status_code, 428)
        first = self.client.patch(url, json={'wd_enabled': True}, headers={'If-Match': '"0"'})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.client.patch(url, json={'wd_enabled': False}, headers={'If-Match': '"0"'}).status_code, 409)
        second = self.client.patch(url, json={'wd_enabled': False}, headers={'If-Match': first.headers['etag']})
        self.assertEqual(second.json()['revision'], 2)
        self.assertEqual(second.json()['storage_thresholds'], {'project_bytes': 5_000_000_000})

    def test_T16_creation_replay_and_conflict(self):
        key = str(uuid4())
        first = self.post(self.base+'/sources', {'type': 'nai', 'name': 'Source'}, key)
        second = self.post(self.base+'/sources', {'type': 'nai', 'name': 'Source'}, key)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self.post(self.base+'/sources', {'type': 'nai', 'name': 'Other'}, key).status_code, 409)

    def test_T43_dimensions_alpha_readonly(self):
        self.image(1)
        self.image(2, True)
        rows = self.client.get(self.base+'/images').json()['items']
        self.assertEqual([(r['width'], r['height'], r['has_alpha']) for r in rows], [(64,32,False),(64,32,True)])
        result = self.client.patch(self.base+'/images/1', json={'width': 5}, headers={'If-Match': '"x"'})
        self.assertEqual(result.status_code, 422)

    def test_T44_keyset_around_total(self):
        for i in range(5):
            self.image(i)
        result = self.client.get(self.base+'/images?limit=2&around_id=4').json()
        self.assertEqual((result['total'], result['start_index'], result['anchor_index']), (5,2,3))
        self.assertEqual([r['image_id'] for r in result['items']], [3,4])
        self.image(10)
        page = self.client.get(self.base+'/images', params={'cursor': result['next_cursor'], 'limit': 2}).json()
        self.assertEqual(page['total'], 5)
        self.assertEqual([r['image_id'] for r in page['items']], [5])
        desc = self.client.get(self.base+'/images?limit=2&around_id=4&sort=-id').json()
        self.assertEqual((desc['start_index'], desc['anchor_index']), (2,2))

    def test_T12_T13_cursor_validation(self):
        self.image(1)
        self.image(2)
        for query in ('limit=0', 'limit=1001', 'offset=1', 'sort=id;DROP', 'source_id=true'):
            self.assertEqual(self.client.get(self.base+'/images?'+query).status_code, 422)
        token = self.client.get(self.base+'/images?limit=1').json()['next_cursor']
        for params in ({'cursor': token+'x'}, {'cursor': token, 'review_status': 'keep'}, {'cursor': token, 'around_id': 1}):
            self.assertEqual(self.client.get(self.base+'/images', params=params).status_code, 422)

    def test_T45_T46_review_preview_undo_replay(self):
        self.image(1)
        self.image(2)
        preview = self.post(self.base+'/images/review/preview', {'query': {}}).json()
        value = dict(query={}, preview_id=preview['preview_id'], digest=preview['digest'], expected_total=2, target='keep')
        key = str(uuid4())
        first = self.post(self.base+'/images/review', value, key)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), self.post(self.base+'/images/review', value, key).json())
        undo = dict(ids=[1,2], targets={str(v['image']['image_id']): v['old_review_status'] for v in first.json()['items']},
                    expected_versions={str(v['image']['image_id']): v['image']['version'] for v in first.json()['items']})
        self.assertEqual(self.post(self.base+'/images/review', undo).status_code, 200)
        self.assertEqual(self.post(self.base+'/images/review', value).status_code, 409)

    def test_T45_whole_batch_conflict_no_write(self):
        self.image(1)
        self.image(2)
        rows = self.client.get(self.base+'/images').json()['items']
        versions = {str(r['image_id']): r['version'] for r in rows}
        self.client.patch(self.base+'/images/2', json={'review_status': 'reject'}, headers={'If-Match': '"'+versions['2']+'"'})
        result = self.post(self.base+'/images/review', dict(ids=[1,2], target='keep', expected_versions=versions))
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.client.get(self.base+'/images/1').json()['review_status'], 'unreviewed')

    def test_T47_stats_filters_and_groups(self):
        self.image(1)
        self.image(2)
        result = self.client.get(self.base+'/images/stats', params=[('group_by', g) for g in ('review_status','rating','source_id','annotation_coverage')])
        self.assertEqual(result.status_code, 200, result.text)
        for group in result.json()['groups']:
            self.assertEqual(sum(v['count'] for v in group['buckets']), 2)
        self.assertEqual(self.client.get(self.base+'/images/stats?limit=1').status_code, 422)
        self.assertEqual(self.client.get(self.base+'/images/stats?review_status=keep').json()['total'], 0)

    def test_T22_T23_T48_media_etag_and_missing(self):
        row = self.image(1, True)
        for suffix in ('content', 'thumbnail'):
            url = self.base+f'/images/{row.image_id}/'+suffix
            first = self.client.get(url)
            self.assertEqual(first.status_code, 200, first.text if first.status_code != 200 else '')
            for tag in (first.headers['etag'], 'W/'+first.headers['etag'], '"other", W/'+first.headers['etag'], '*'):
                second = self.client.get(url, headers={'If-None-Match': tag})
                self.assertEqual(second.status_code, 304)
                self.assertEqual(second.content, b'')
                self.assertEqual(second.headers['vary'], 'Authorization')
            self.assertEqual(self.client.get(url, headers={'If-None-Match': '*', 'Authorization': 'wrong'}).status_code, 401)
        (self.rt.current.handle.root/'images'/row.filename).unlink()
        self.assertEqual(self.client.get(self.base+f'/images/{row.image_id}/content', headers={'If-None-Match': '*'}).status_code, 410)

    def test_T18_HTTP_paths_not_accepted(self):
        self.assertEqual(self.post('/api/projects/open', {'path': str(self.root)}).status_code, 422)
        self.assertEqual(self.post('/api/projects/open', {'grant_id': str(uuid4())}).status_code, 403)

    def test_T38_encoding_and_bool_integer(self):
        result = self.client.patch('/api/config', json={'wd_enabled': True}, headers={'Content-Encoding': 'gzip'})
        self.assertEqual(result.status_code, 415)
        result = self.post(self.base+'/images/review', {'ids': [True], 'target': 'keep', 'expected_versions': {'1': 'x'}})
        self.assertEqual(result.status_code, 422)

    def test_T03_T05_close_invalidates(self):
        view = self.client.get(self.base).json()
        result = self.post(self.base+'/close', {}, **{'If-Match': '"'+view['version']+'"'})
        self.assertEqual(self.wait(result.json()['operation_id'])['status'], 'done')
        self.assertEqual(self.client.get(self.base+'/images').status_code, 410)
        self.assertIsNone(self.client.get('/api/projects/current').json())

    def test_T15_T17_endpoint_secret_validation_roles(self):
        value = {'base_url':'http://fake.invalid/v1','protocol':'openai_chat','models':[{'name':'fake/model', 'supports_images':True}]}
        result = self.post('/api/llm/endpoints', value)
        self.assertEqual(result.status_code, 201, result.text)
        endpoint = result.json()
        invalid = self.post('/api/llm/endpoints', value | {'base_url':'http://user:DO-NOT-EXPOSE@fake.invalid'})
        self.assertNotIn('DO-NOT-EXPOSE', invalid.text)
        self.assertGreaterEqual(invalid.status_code, 400)
        roles = self.client.get('/api/llm/roles').json()
        roles['roles']['vision'] = {'endpoint_id':endpoint['endpoint_id'], 'model':'fake/model'}
        updated = self.client.put('/api/llm/roles', json={'roles':roles['roles']}, headers={'If-Match':'"'+roles['version']+'"'})
        self.assertEqual(updated.status_code, 200, updated.text)
        roles = updated.json()
        roles['roles']['clean'] = roles['roles']['vision']
        self.assertEqual(self.client.put('/api/llm/roles', json={'roles':roles['roles']}, headers={'If-Match':'"'+roles['version']+'"'}).status_code, 501)

    def test_T19_T20_FakeLauncher_start_cost_replay(self):
        from f02_support import FakeLauncher, PROFILE, SMALL
        from stylelora.backends.types import SourceGenerationConfig
        from stylelora.generation.profile import to_snapshot
        from stylelora.prompts.dictionaries import digest
        fake = FakeLauncher(estimated_anlas=2)
        async def factory():
            return await fake.backend(self.root/'journal')
        self.rt.backend_factory = factory
        source = self.post(self.base+'/sources', {'type':'nai','name':'fake','config':to_snapshot(SourceGenerationConfig(PROFILE, SMALL)), 'cost_policy':'ask', 'target_count':1}).json()
        seeds = (ItemSeed(1, 'content'),)
        value = {'kind':'nai','seeds':{'items':[{'seq':1,'prompt':'content','aspect':None}], 'digest':digest(seeds),'count':1}}
        key = str(uuid4())
        started = self.post(self.base+f'/sources/{source["source_id"]}/start', value, key, **{'If-Match':'"'+source['version']+'"'})
        self.assertEqual(started.status_code, 202, started.text)
        state = self.wait(started.json()['operation_id'])
        if state['status'] == 'failed':
            raise self.rt.supervisor.failures[started.json()['operation_id']]
        self.assertEqual(state['status'], 'done')
        replay = self.post(self.base+f'/sources/{source["source_id"]}/start', value, key, **{'If-Match':'"stale"'})
        self.assertEqual(started.json(), replay.json())
        self.assertEqual(fake.counts['submit_generation'], 0)
        row = self.client.get(self.base+f'/sources/{source["source_id"]}').json()
        self.assertEqual(row['status'], 'paused')
        account = self.client.get('/api/backends/nai/account-status', params={'project_id':self.pid, 'source_id':source['source_id']}).json()
        self.assertEqual(account['remaining_estimated_anlas'], 2)

    def test_T25_manual_refs_and_unknown_merge_meta(self):
        image = self.image(1)
        url = self.base+f'/annotations/{image.image_id}'
        view = self.client.get(url).json()
        value = {'tags':[{'tag':'blue hair','conf':None}], 'read_set':view['refs'], 'write_nl':True, 'natural_language':'Caption'}
        result = self.client.put(url+'/manual', json=value, headers={'If-Match':'"'+view['version']+'"'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.client.put(url+'/manual', json=value, headers={'If-Match':'"'+view['version']+'"'}).status_code, 409)
        value['merge_meta'] = {}
        self.assertEqual(self.client.put(url+'/manual', json=value, headers={'If-Match':'"x"'}).status_code, 422)

    def test_T26_rules_preview_apply_and_undo(self):
        self.image(1)
        value = {'rule':{'schema_version':1,'delete':None,'write':{'mode':'all','tags':['blue hair']},'condition':None}, 'scope':{'selected_ids':[1]}}
        preview = self.post(self.base+'/rules/preview', value)
        self.assertEqual(preview.status_code, 200, preview.text)
        p = preview.json()
        result = self.post(self.base+'/rules/apply', {'preview_id':p['preview_id'], 'digest':p['digest']}, **{'If-Match':'"'+p['version']+'"'})
        self.assertEqual(result.status_code, 200, result.text)
        op = self.client.get(self.base+'/operation-log/'+str(result.json()['op_id'])).json()
        undo = self.post(self.base+'/operation-log/'+str(op['op_id'])+'/undo', {}, **{'If-Match':'"'+op['version']+'"'})
        self.assertEqual(undo.status_code, 200, undo.text)

    def test_T21_dictionary_create_and_page_stale(self):
        first = self.post('/api/dictionaries', {'kind':'prompts','name':'one','entries':[{'tags':['cat']}]})
        self.assertEqual(first.status_code, 201, first.text)
        self.post('/api/dictionaries', {'kind':'prompts','name':'two','entries':[{'tags':['dog']}]})
        page = self.client.get('/api/dictionaries?limit=1').json()
        self.assertTrue(page['has_more'])
        self.post('/api/dictionaries', {'kind':'prompts','name':'three','entries':[{'tags':['bird']}]})
        self.assertEqual(self.client.get('/api/dictionaries', params={'cursor':page['next_cursor']}).status_code, 409)

    def test_T30_export_copy_preview_report(self):
        from stylelora.tagging.catalog import LocalCatalog
        self.rt.catalog = LocalCatalog({'schema_version':1,'kind':'tag_catalog','revision':1,'tags':[]}, complete=True)
        self.image(1)
        row = self.client.get(self.base+'/images/1').json()
        self.client.patch(self.base+'/images/1', json={'review_status':'keep'}, headers={'If-Match':'"'+row['version']+'"'})
        preview = self.post(self.base+'/exports/preview', {'scheme_id':1})
        self.assertEqual(preview.status_code, 202, preview.text)
        completed = self.wait(preview.json()['operation_id'])
        self.assertEqual(completed['status'], 'done', completed)
        p = completed['result']
        result = self.post(self.base+'/exports', {'scheme_id':1,'preview_id':p['preview_id'],'digest':p['digest']})
        self.assertEqual(result.status_code, 202, result.text)
        import time
        for _ in range(500):
            export = self.client.get(result.headers['location']).json()
            if export['status'] not in ('planned','running'):
                break
            time.sleep(.002)
        self.assertEqual(export['status'], 'finished', export)
        report = self.client.get(export['report_url']).json()
        self.assertEqual(report['exported'], 1)

    def test_T17_role_map_atomic_failure_and_ABA(self):
        from unittest.mock import patch
        from stylelora.llm.types import LlmFailure
        value = {'base_url':'http://fake.invalid','protocol':'openai_chat','models':[{'name':'fake','supports_images':True}]}
        endpoint = self.post('/api/llm/endpoints', value).json()
        before = self.rt.product.llm.role_map()
        roles = dict(before[1])
        roles['prompt'] = {'endpoint_id':endpoint['endpoint_id'], 'model':'fake'}
        roles['vision'] = {'endpoint_id':endpoint['endpoint_id'], 'model':'missing'}
        with self.assertRaises(LlmFailure):
            self.rt.product.llm.save_role_map(roles, expected_revision=before[0])
        self.assertEqual(self.rt.product.llm.role_map(), before)
        roles['vision'] = roles['prompt']
        with patch('stylelora.llm.config.atomic_json', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                self.rt.product.llm.save_role_map(roles, expected_revision=before[0])
        self.assertEqual(self.rt.product.llm.role_map(), before)
        changed = self.rt.product.llm.save_role_map(roles, expected_revision=before[0])
        restored = self.rt.product.llm.save_role_map(before[1], expected_revision=changed[0])
        self.assertGreater(restored[0], before[0])
        with self.assertRaises(LlmFailure):
            self.rt.product.llm.save_role_map(roles, expected_revision=before[0])

    def test_T27_jobs_fake_executor_and_outcomes(self):
        from test_f15_jobs import FakeExecutor, snapshot
        self.image(1)
        executor = FakeExecutor()
        self.rt.executors = {'wd':executor}
        self.rt.snapshots = {'wd':snapshot()}
        result = self.post(self.base+'/jobs/batches', {'batch_id':str(uuid4()), 'executors':['wd'], 'mode':'selected', 'scope':{'selected_ids':[1]}})
        self.assertEqual(result.status_code, 202, result.text)
        job = result.json()[0]
        import time
        for _ in range(500):
            view = self.client.get(self.base+'/jobs/'+str(job['job_id'])).json()
            if view['status'] not in ('pending','running'):
                break
            time.sleep(.002)
        self.assertEqual(view['status'], 'done', view)
        outcomes = self.client.get(self.base+'/jobs/'+str(job['job_id'])+'/outcomes')
        self.assertEqual(outcomes.status_code, 200, outcomes.text)
        self.assertEqual(len(outcomes.json()['items']), 1)
        self.assertEqual(len(executor.calls), 1)

    def test_T14_metadata_CAS(self):
        view = self.client.get(self.base)
        updated = self.client.patch(self.base, json={'name':'Changed'}, headers={'If-Match':view.headers['etag']})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()['metadata']['name'], 'Changed')
        self.assertEqual(self.client.patch(self.base, json={'name':'Old'}, headers={'If-Match':view.headers['etag']}).status_code, 409)

    def test_T11_contract_count_and_reserved_errors(self):
        import re
        app = self.client.app
        routes = [(m, re.sub(r'\{[^}]+\}', '{}', r.path)) for r in app.routes if r.path.startswith('/api') for m in r.methods]
        self.assertEqual(len(routes), 168)  # F27 adds 21 routes to the existing contract.
        self.assertEqual(len(set(routes)), 168)
        result = self.post('/api/tagger/models/install', {'manifest_id': 'unknown'})
        self.assertEqual(result.status_code, 501)
        self.assertEqual(result.json()['error']['code'], 'tagger:download_unavailable')

    def test_T28_prompt_fake_LLM_seeds_product_only(self):
        from test_f07_prompts import Fake, TaskTests
        from stylelora.runtime.contracts import plain
        self.rt.llm = Fake()
        dictionary = self.post('/api/dictionaries', {'kind':'prompts','name':'input','entries':[{'tags':['forest']}]}).json()
        request = plain(TaskTests().request(optimize=True))
        request['prompt_dictionary_ids'] = [dictionary['dictionary_id']]
        result = self.post('/api/prompt-tasks', request)
        self.assertEqual(result.status_code, 202, result.text)
        identity = result.json()['task_id']
        import time
        for _ in range(500):
            task = self.client.get('/api/prompt-tasks/'+identity).json()
            if task['status'] not in ('pending','running'):
                break
            time.sleep(.002)
        self.assertEqual(task['status'], 'done', task)
        self.assertTrue(task['ready'])
        seeds = self.post('/api/prompt-tasks/'+identity+'/seeds', {}, **{'If-Match':'"'+task['version']+'"'})
        self.assertEqual(seeds.status_code, 200, seeds.text)
        self.assertEqual(seeds.json()['count'], 1)
        self.assertEqual(len(self.rt.llm.calls), 1)
        self.assertEqual(self.client.get(self.base+'/jobs').json()['items'], [])

    def test_T45_preview_expiry_and_batch_limit(self):
        self.image(1)
        preview = self.post(self.base+'/images/review/preview', {'query':{}}).json()
        before = self.rt.clock
        self.rt.clock = lambda: before()+901
        try:
            result = self.post(self.base+'/images/review', dict(query={}, expected_total=1, preview_id=preview['preview_id'], digest=preview['digest'], target='keep'))
            self.assertEqual(result.status_code, 410)
        finally:
            self.rt.clock = before
        result = self.post(self.base+'/images/review', dict(ids=list(range(1,5002)), target='keep', expected_versions={str(i):'x' for i in range(1,5002)}))
        self.assertEqual(result.status_code, 413)
        self.assertEqual(self.client.get(self.base+'/images/1').json()['review_status'], 'unreviewed')
