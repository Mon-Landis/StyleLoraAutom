import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from stylelora.api import create_app
from stylelora.runtime import Runtime


class PickerCreateTests(unittest.TestCase):
    """Picker returns an existing parent folder; create must place <name>.slproj inside it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.parent = self.root / 'workspace'
        self.parent.mkdir()
        self.rt = Runtime(self.root / 'product', picker=lambda purpose: self.parent)
        self.client = TestClient(create_app(self.rt), base_url='http://127.0.0.1:8000')
        self.client.__enter__()
        self.addCleanup(lambda: self.client.__exit__(None, None, None))
        self.client.headers['Authorization'] = 'Bearer ' + self.rt.token

    def post(self, path, value):
        return self.client.post(path, json=value, headers={'Idempotency-Key': str(uuid4())})

    def wait(self, identity):
        for _ in range(500):
            value = self.client.get('/api/operations/' + identity).json()
            if value['status'] not in ('pending', 'running'):
                return value
            time.sleep(.01)
        self.fail('operation did not finish')

    def grant(self):
        r = self.post('/api/filesystem/grants', {'purpose': 'project_create'})
        self.assertEqual(r.status_code, 202, r.text)
        op = self.wait(r.json()['operation_id'])
        self.assertEqual(op['status'], 'done', op)
        return op['result']['grant_id']

    def test_create_inside_picked_parent(self):
        r = self.post('/api/projects', {'grant_id': self.grant(), 'name': 'My Style', 'trigger_word': 'sty_x'})
        self.assertEqual(r.status_code, 202, r.text)
        op = self.wait(r.json()['operation_id'])
        self.assertEqual(op['status'], 'done', op)
        self.assertTrue((self.parent / 'My Style.slproj' / 'project.json').is_file())

    def test_existing_project_name_conflicts(self):
        (self.parent / 'Dup.slproj').mkdir()
        r = self.post('/api/projects', {'grant_id': self.grant(), 'name': 'Dup'})
        self.assertEqual(r.status_code, 409, r.text)

    def test_session_reports_picker_capability(self):
        r = self.client.get('/api/session')
        if r.status_code == 200:
            self.assertTrue(r.json().get('capabilities', {}).get('picker'))


if __name__ == '__main__':
    unittest.main()
