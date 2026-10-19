from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from fastapi.testclient import TestClient
from stylelora.api import create_app
from stylelora.runtime import Runtime


class PromptConvertApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.rt = Runtime(Path(temp.name) / 'StyleLoraAutom')
        self.client = TestClient(create_app(self.rt), base_url='http://127.0.0.1:8000')
        self.client.__enter__()
        self.addCleanup(lambda: self.client.__exit__(None, None, None))
        self.client.headers['Authorization'] = 'Bearer ' + self.rt.token

    def post(self, value, **kwargs):
        return self.client.post('/api/tools/prompt-convert', json=value, **kwargs)

    def test_both_directions_without_project(self):
        for direction, source, expected in [('nai_to_anima', '1.5::a_b::', '(a b:1.3)'),
                                             ('anima_to_nai', '(a b:1.3)', '1.5::a_b::')]:
            result = self.post({'direction': direction, 'text': source})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json(), {'text': expected, 'warnings': []})

    def test_warning_shape(self):
        result = self.post({'direction': 'nai_to_anima', 'text': '9::x::'})
        self.assertEqual(result.json(), {'text': '(x:3.2)', 'warnings': [{'code': 'W_WEIGHT_CLAMPED', 'start': 3, 'end': 4}]})
        result = self.post({'direction': 'anima_to_nai', 'text': '(x:0.1)'})
        self.assertEqual(result.json(), {'text': '0.1::x::', 'warnings': []})

    def test_strict_validation(self):
        valid = {'direction': 'nai_to_anima', 'text': 'cat'}
        for value in [None, [], {}, valid | {'extra': 1}, {'text': 'cat'},
                      valid | {'direction': []}, valid | {'direction': 'bad'},
                      valid | {'text': 1}, valid | {'text': None}, valid | {'text': 'x' * 200001}]:
            with self.subTest(value=str(value)[:80]):
                result = self.post(value)
                self.assertEqual(result.status_code, 422, result.text)
                self.assertEqual(result.json()['error']['code'], 'input:invalid')
        self.assertEqual(self.post(valid | {'text': ''}).status_code, 200)
        self.assertEqual(self.post(valid | {'text': ',' * 200000}).status_code, 200)

    def test_authentication_precedes_validation(self):
        del self.client.headers['Authorization']
        self.assertEqual(self.post({}).status_code, 401)
        self.client.headers['Authorization'] = 'Bearer invalid'
        self.assertEqual(self.post({}).status_code, 401)

    def test_no_idempotency_storage_or_conflicts(self):
        key = str(uuid4())
        before = dict(self.rt.idempotency)
        for text, headers in [('cat', {}), ('dog', {'Idempotency-Key': key}),
                              ('bird', {'Idempotency-Key': key}), ('fish', {'Idempotency-Key': 'optional'})]:
            result = self.post({'direction': 'nai_to_anima', 'text': text}, headers=headers)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['text'], text)
        self.assertEqual(self.rt.idempotency, before)
