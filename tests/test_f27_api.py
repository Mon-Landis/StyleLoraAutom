import io
import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from fastapi.testclient import TestClient
from stylelora.api import create_app
from stylelora.runtime import Runtime
from stylelora.runtime.prompt_groups import service
from test_f27_groups import image_bytes


class GroupApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.rt = Runtime(Path(self.temp.name)/'StyleLoraAutom')
        self.client = TestClient(create_app(self.rt), base_url='http://127.0.0.1:8000')
        self.client.__enter__(); self.addCleanup(lambda: self.client.__exit__(None,None,None))
        self.client.headers['Authorization'] = 'Bearer '+self.rt.token

    def headers(self, revision=None, key=None, create=False):
        headers = {'Idempotency-Key':key or str(uuid4())}
        if revision is not None:
            headers['If-Match'] = '"'+str(revision)+'"'
        if create:
            headers['If-None-Match'] = '*'
        return headers

    def group(self, nsfw=False):
        response = self.client.post('/api/prompt-groups', json={'name':'sample', 'nsfw':nsfw}, headers=self.headers(create=True))
        self.assertEqual(response.status_code,201,response.text)
        return '/api/prompt-groups/'+response.json()['dictionary_id']

    def entry(self, path, revision=1, **value):
        response = self.client.post(path+'/entries', json=value or {'nl':'A cat.'}, headers=self.headers(revision))
        self.assertEqual(response.status_code,201,response.text)
        return path+'/entries/'+response.json()['entry_id']

    def test_T04_T05_http_cas_and_durable_replay(self):
        path = self.group()
        missing = self.client.patch(path, json={'name':'new'}, headers=self.headers())
        self.assertEqual(missing.status_code,428,missing.text)
        self.assertEqual(missing.json()['error']['code'],'revision:required')
        headers = self.headers(1)
        first = self.client.patch(path, json={'name':'new'}, headers=headers)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.headers['etag'],'"2"')
        second = self.client.patch(path, json={'name':'new'}, headers=headers)
        self.assertEqual(first.json(),second.json())
        conflict = self.client.patch(path, json={'name':'other'}, headers=headers)
        self.assertEqual(conflict.status_code,409,conflict.text)
        stale = self.client.patch(path, json={'name':'other'}, headers=self.headers(1))
        self.assertEqual(stale.status_code,412,stale.text)
        self.assertEqual(self.client.get(path).json()['name'],'new')

    def test_T07_entry_delete_replays(self):
        path = self.group(); entry = self.entry(path)
        headers = self.headers(2)
        first = self.client.delete(entry, headers=headers)
        second = self.client.delete(entry, headers=headers)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json(),second.json())
        self.assertEqual(self.client.get(entry).status_code,404)

    def test_T08_keyset_and_stale_filter_revision(self):
        svc = service(self.rt)
        for n in range(55):
            svc.create({'name':f'group {n}'}, f'create-{n}')
        first = self.client.get('/api/prompt-groups').json()
        self.assertEqual(len(first['items']),50)
        second = self.client.get('/api/prompt-groups', params={'cursor':first['next_cursor']}).json()
        self.assertEqual(len(second['items']),5)
        self.assertFalse({r['dictionary_id'] for r in first['items']} & {r['dictionary_id'] for r in second['items']})
        changed = self.client.get('/api/prompt-groups', params={'cursor':first['next_cursor'], 'q':'new'})
        self.assertEqual(changed.status_code,409)
        self.assertEqual(self.client.get('/api/prompt-groups?cursor=garbage').status_code,422)
        path = self.group(); self.entry(path); self.entry(path,2,nl='A dog.')
        page = self.client.get(path+'/entries?limit=1').json()
        self.client.patch(path,json={'name':'edited'},headers=self.headers(3))
        self.assertEqual(self.client.get(path+'/entries',params={'cursor':page['next_cursor'],'limit':1}).status_code,409)

    def test_T09_backend_nsfw_invariant(self):
        path = self.group(nsfw=True); entry = self.entry(path)
        before = [self.client.get(url).json() for url in ['/api/prompt-groups',path,path+'/entries',path+'/export']]
        config = self.client.get('/api/settings').json()
        response = self.client.patch('/api/settings',json={'nsfw_enabled':True},headers=self.headers(config['revision']))
        self.assertEqual(response.status_code,200,response.text)
        after = [self.client.get(url).json() for url in ['/api/prompt-groups',path,path+'/entries',path+'/export']]
        self.assertEqual(before,after)
        self.assertTrue(self.client.get(entry).json()['nsfw'])
        self.assertEqual(len(self.client.get('/api/prompt-groups?nsfw=true').json()['items']),1)

    def test_T11_import_http_and_export_handle(self):
        headers = self.headers()
        response = self.client.post('/api/prompt-imports/preview',json={'data':{'prompts':[{'nl':'A cat.'}]}},headers=headers)
        self.assertEqual(response.status_code,200,response.text)
        preview = response.json()
        repeated = self.client.post('/api/prompt-imports/preview',json={'data':{'prompts':[{'nl':'A cat.'}]}},headers=headers)
        self.assertEqual(repeated.json(),preview)
        path = '/api/prompt-imports/'+preview['preview_id']+'/confirm'
        response = self.client.post(path,json={'digest':preview['digest'],'ref':preview['ref']},headers=self.headers(create=True))
        self.assertEqual(response.status_code,200,response.text)
        group = '/api/prompt-groups/'+response.json()['ref']['group_id']
        handle = self.client.get(group+'/export?format=folder').json()
        self.assertNotIn(str(self.rt.product.root), json.dumps(handle))
        self.assertEqual(self.client.get(handle['download_url']).status_code,200)

    def test_T20_image_etag_and_nsfw(self):
        path = self.group(nsfw=True); entry = self.entry(path)
        response = self.client.put(entry+'/preview',files={'image':('input.png',image_bytes(),'image/png')},headers=self.headers(2))
        self.assertEqual(response.status_code,200,response.text)
        full = self.client.get(entry+'/preview')
        self.assertEqual(full.status_code,200,full.text)
        self.assertEqual(full.headers['x-nsfw'],'true')
        self.assertEqual(self.client.get(entry+'/preview',headers={'If-None-Match':full.headers['etag']}).status_code,304)
        self.assertEqual(self.client.get(entry+'/preview?size=thumb').headers['content-type'],'image/webp')
        self.assertIn('private',full.headers['cache-control'])

    def test_T21_settings_defaults_and_process_reveal(self):
        config = self.client.get('/api/settings').json()
        self.assertFalse(config['nsfw_enabled']); self.assertEqual(config['nsfw_display_mode'],'click_reveal')
        for mode in ('hover','show','click_reveal'):
            response = self.client.patch('/api/settings',json={'nsfw_display_mode':mode},headers=self.headers(config['revision']))
            self.assertEqual(response.status_code,200,response.text); config = response.json()
        self.assertEqual(self.client.post('/api/safety/reveal',headers=self.headers()).json(), {'revealed_this_run':True})
        self.assertTrue(service(self.rt).revealed_this_run)
        self.assertNotIn('revealed_this_run',self.rt.product.get())
        response = self.client.patch('/api/settings',json={'image_display':'show','nsfw_display_mode':'hover'},headers=self.headers(config['revision']))
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.client.patch('/api/settings',json={'nsfw_enabled':False},headers=self.headers(0)).status_code,412)

    def test_completion_task_type_is_unsupported(self):
        path = self.group()
        body = {'type':'prompt_completion','ref':{'scope':'global','project_token':None,
                'group_id':path.rsplit('/',1)[1]},'base_revision':1}
        response = self.client.post('/api/tasks',json=body,headers=self.headers())
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(response.json()['error']['code'],'task:type_unsupported')
        self.assertEqual(self.client.post('/api/tasks',json=body|{'type':'unknown'},headers=self.headers()).status_code,422)

    def test_project_replay_revalidates_context(self):
        self.client.portal.call(lambda: self.rt.open(Path(self.temp.name)/'project.slproj',create={'name':'Project','trigger_word':''}))
        token = self.rt.current.project_id
        headers = self.headers(create=True)
        body = {'name':'project group','scope':'project','project_token':token}
        first = self.client.post('/api/prompt-groups',json=body,headers=headers)
        self.assertEqual(first.status_code,201,first.text)
        self.client.portal.call(self.rt.close_project)
        second = self.client.post('/api/prompt-groups',json=body,headers=headers)
        self.assertEqual(second.status_code,409,second.text)
        self.assertEqual(second.json()['error']['code'],'project:closed')

