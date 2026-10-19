import asyncio
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from fastapi.testclient import TestClient
from stylelora.api import create_app
from stylelora.runtime import Runtime
from stylelora.runtime.tasks import ResourceRef
from test_f26_tasks import FakeAdapter


class TaskApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rt = Runtime(Path(self.temp.name)/'product')
        self.client = TestClient(create_app(self.rt), base_url='http://127.0.0.1:8000')
        self.client.__enter__()
        self.addCleanup(lambda: self.client.__exit__(None, None, None))
        self.client.headers['Authorization'] = 'Bearer '+self.rt.token

    def test_T13_T17_api_cancel_and_sanitized_summary(self):
        adapter = FakeAdapter()
        identity = self.rt.tasks.register(adapter, ResourceRef('job', 'fake'), None)
        try:
            page = self.client.get('/api/tasks')
            self.assertEqual(page.status_code, 200, page.text)
            self.assertEqual(page.json()['items'][0]['task_id'], identity)
            self.assertIn('event_cursor', page.json())
            response = self.client.post('/api/tasks/'+identity+'/cancel', json={'request_key':'stop'},
                                        headers={'Idempotency-Key':str(uuid4())})
            self.assertEqual(response.status_code, 202, response.text)
            replay = self.client.post('/api/tasks/'+identity+'/cancel', json={'request_key':'stop'},
                                      headers={'Idempotency-Key':str(uuid4())})
            self.assertEqual(replay.status_code, 200, replay.text)
            detail = self.client.get('/api/tasks/'+identity).json()
            self.assertEqual(detail['status'], 'canceling')
            self.assertFalse({'token','path','prompt','result'} & set(detail))
            self.assertEqual(self.client.get('/api/tasks/missing').status_code, 404)
            self.assertEqual(self.client.get('/api/tasks?kind=invalid').status_code, 422)
        finally:
            self.client.portal.call(adapter.exited.set)

    def test_prompt_generation_unavailable_maps_to_503(self):
        created = self.client.post('/api/prompt-groups', json={'name': 'Gen'},
                                   headers={'Idempotency-Key': str(uuid4()), 'If-None-Match': '*'})
        self.assertEqual(created.status_code, 201, created.text)
        ref = {'scope': 'global', 'project_token': None, 'group_id': created.json()['dictionary_id']}
        response = self.client.post('/api/tasks', json={'type': 'prompt_generation', 'ref': ref,
                                                        'brief': 'cats', 'count': 1},
                                    headers={'Idempotency-Key': str(uuid4())})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()['error']['code'], 'generation:unavailable')

    def test_T26_T30_settings_api_CAS_and_validation(self):
        value = self.client.get('/api/settings/resources').json()
        self.assertEqual(value['network']['token_default'], -1)
        self.assertEqual(value['compute']['reserve_bytes'], 1024**3)
        body = {'expected_revision':value['revision'], 'network':value['network'], 'compute':value['compute']}
        body['network']['global_limit'] = 3
        response = self.client.put('/api/settings/resources', json=body, headers={'Idempotency-Key':str(uuid4())})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.rt.network.policy.global_limit, 3)
        response = self.client.put('/api/settings/resources', json=body, headers={'Idempotency-Key':str(uuid4())})
        self.assertEqual(response.status_code, 409, response.text)
        body['expected_revision'] = '1'
        body['compute']['gpu_slots'] = 0
        response = self.client.put('/api/settings/resources', json=body, headers={'Idempotency-Key':str(uuid4())})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get('/api/settings/resources').json()['revision'], '1')
