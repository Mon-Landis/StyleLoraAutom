from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from stylelora.api import create_app
from stylelora.runtime import Runtime


class EndpointParallelApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.rt = Runtime(Path(temp.name) / 'product')
        self.client = TestClient(create_app(self.rt), base_url='http://127.0.0.1:8000')
        self.client.__enter__()
        self.addCleanup(lambda: self.client.__exit__(None, None, None))
        self.client.headers['Authorization'] = 'Bearer ' + self.rt.token
        self.body = dict(base_url='http://localhost:9999/v1', protocol='openai_chat', models=[{'name': 'model'}])

    def create(self, body):
        return self.client.post('/api/llm/endpoints', json=body, headers={'Idempotency-Key': str(uuid4())})

    def test_default_create_update_read_and_omitted_update(self):
        response = self.create(self.body)
        self.assertEqual(response.status_code, 201, response.text)
        endpoint = response.json()
        self.assertEqual(endpoint['max_parallel'], 4)
        url = '/api/llm/endpoints/' + endpoint['endpoint_id']
        for patch, expected in (({'max_parallel': -1}, -1), ({}, -1), ({'max_parallel': 6}, 6)):
            response = self.client.put(url, json=self.body | patch,
                                       headers={'If-Match': '"' + endpoint['version'] + '"', 'Idempotency-Key': str(uuid4())})
            self.assertEqual(response.status_code, 200, response.text)
            endpoint = response.json()
            self.assertEqual(endpoint['max_parallel'], expected)
            self.assertEqual(self.client.get(url).json()['max_parallel'], expected)
        self.assertEqual(self.client.get('/api/llm/endpoints').json()['items'][0]['max_parallel'], 6)
        self.assertEqual(self.rt.product.llm.endpoint(endpoint['endpoint_id']).max_parallel, 6)
        self.assertEqual(self.create(self.body | {'max_parallel': -1}).json()['max_parallel'], -1)

    def test_update_preserves_model_capability_metadata(self):
        from dataclasses import replace
        endpoint = self.create(self.body | {'models': [{'name': 'model', 'supports_images': True}]}).json()
        stored = self.rt.product.llm.endpoint(endpoint['endpoint_id'])
        tested = replace(stored, models=(replace(stored.models[0], capability_source='tested', tested_at='2026-10-18T00:00:00Z'),))
        self.rt.product.llm.save_endpoint(tested, expected_revision=stored.revision)
        endpoint = self.client.get('/api/llm/endpoints/' + endpoint['endpoint_id']).json()
        body = self.body | {'models': [{'name': 'model', 'supports_images': True}], 'max_parallel': 2}
        response = self.client.put('/api/llm/endpoints/' + endpoint['endpoint_id'], json=body,
                                   headers={'If-Match': '"' + endpoint['version'] + '"', 'Idempotency-Key': str(uuid4())})
        self.assertEqual(response.status_code, 200, response.text)
        model = self.rt.product.llm.endpoint(endpoint['endpoint_id']).models[0]
        self.assertEqual((model.capability_source, model.tested_at), ('tested', '2026-10-18T00:00:00Z'))

    def test_invalid_create_and_update(self):
        endpoint = self.create(self.body).json()
        for value in (0, -2, True, False, 1.5, '4', None):
            with self.subTest(value=value):
                body = self.body | {'max_parallel': value}
                for response in (self.create(body), self.client.put('/api/llm/endpoints/' + endpoint['endpoint_id'],
                        json=body, headers={'If-Match': '"' + endpoint['version'] + '"', 'Idempotency-Key': str(uuid4())})):
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertIn('input:invalid', response.text)
