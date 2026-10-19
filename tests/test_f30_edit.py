import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from fastapi.testclient import TestClient
from stylelora.api import create_app
from stylelora.runtime import Runtime
from stylelora.runtime.prompt_groups import apply_user_patch, service
from stylelora.prompts.groups import (
    GeneratedFields, FieldVersions, PendingField, GroupRef, validate_entry,
)
from stylelora.prompts.types import PromptError


class EditTests(unittest.TestCase):
    def entry(self):
        return replace(validate_entry({'nl': 'A forest.',
                                       'variants': {'short': 'Trees', 'other': 'Forest'}}),
                       generated=GeneratedFields(True, {'short': True, 'other': True}),
                       pending={'nl': PendingField('pending', 'fake', 1, 0, 'digest')})

    def test_field_isolation_same_value_and_pending(self):
        entry = self.entry()
        updated = apply_user_patch(entry, {'nl': entry.nl, 'variants': {'short': 'Trees'}})
        self.assertFalse(updated.generated.nl)
        self.assertFalse(updated.generated.variants['short'])
        self.assertTrue(updated.generated.variants['other'])
        self.assertEqual(updated.edit_versions.nl, 1)
        self.assertEqual(dict(updated.edit_versions.variants), {'short': 1})
        self.assertEqual(updated.pending, entry.pending)
        self.assertEqual(apply_user_patch(entry, {'disable': True, 'aspect': 'square', 'variants': {}}).generated,
                         entry.generated)

    def test_delete_recreate_and_normalization(self):
        entry = apply_user_patch(self.entry(), {'variants': {' short ': None, 'a.b': ' x\r\ny\r '}})
        self.assertNotIn('short', entry.variants)
        self.assertNotIn('short', entry.generated.variants)
        self.assertEqual(entry.edit_versions.variants['short'], 1)
        self.assertEqual(entry.variants['a.b'], ' x\ny\n ')
        entry = apply_user_patch(entry, {'variants': {'short': ''}})
        self.assertEqual(entry.edit_versions.variants['short'], 2)
        self.assertEqual(entry.variants['other'], 'Forest')

    def test_deleted_versions_survive_more_than_64_historical_keys(self):
        entry = self.entry()
        for i in range(65):
            entry = apply_user_patch(entry, {'variants': {str(i): ''}})
            entry = apply_user_patch(entry, {'variants': {str(i): None}})
        self.assertEqual(len(entry.edit_versions.variants), 65)
        self.assertEqual(len(entry.variants), 2)

    def test_validation_and_readonly_state(self):
        for patch in ({'variants': None}, {'variants': {'x': 1}}, {'variants': {' x': '', 'x ': ''}},
                      {'variants': {'bad\n': ''}}, {'variants': {'x': 'x'*16001}},
                      {'variants': {str(i): '' for i in range(64)}},
                      {'tag': [], 'expand_nl': '', 'nl': ''},
                      *({field: {}} for field in ('generated', 'pending', 'edit_versions', 'preview', 'completion'))):
            with self.subTest(patch=patch), self.assertRaises(PromptError):
                apply_user_patch(self.entry(), patch)

    def test_base_versions_and_removed_fields(self):
        entry = self.entry()
        for field in ('nl',):
            updated = apply_user_patch(entry, {field: getattr(entry, field)})
            self.assertEqual(getattr(updated.edit_versions, field), 1)
            self.assertFalse(getattr(updated.generated, field))
        for field in ('tag', 'expand_nl'):
            with self.assertRaises(PromptError):
                apply_user_patch(entry, {field: ''})


class EditApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rt = Runtime(Path(self.temp.name)/'product')
        self.client = TestClient(create_app(self.rt), base_url='http://127.0.0.1:8000')
        self.client.__enter__()
        self.addCleanup(lambda: self.client.__exit__(None, None, None))
        self.client.headers['Authorization'] = 'Bearer '+self.rt.token

    def write(self, method, path, body, revision=0, key=None):
        headers = {'Idempotency-Key': key or str(uuid4())}
        headers.update({'If-None-Match': '*'} if method == 'POST' and path == '/api/prompt-groups'
                       else {'If-Match': f'"{revision}"'})
        return self.client.request(method, path, json=body, headers=headers)

    def create(self, **extra):
        response = self.write('POST', '/api/prompt-groups', {'name': 'Test', **extra})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_preferences_group_switch_and_replay(self):
        self.assertTrue(self.client.get('/api/settings').json()['prompt_auto_complete_default'])
        first = self.create()
        response = self.write('PATCH', '/api/settings', {'prompt_auto_complete_default': False}, key='pref')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['etag'], '"1"')
        self.assertEqual(self.write('PATCH', '/api/settings', {'prompt_auto_complete_default': False}, key='pref').json(), response.json())
        self.assertEqual(self.write('PATCH', '/api/settings', {'prompt_auto_complete_default': True}, key='pref').status_code, 409)
        self.assertEqual(self.write('PATCH', '/api/settings', {'prompt_auto_complete_default': True}).status_code, 412)
        second = self.create()
        self.assertFalse(second['auto_complete'])
        self.assertTrue(self.create(auto_complete=True)['auto_complete'])
        path = '/api/prompt-groups/'+first['dictionary_id']
        self.assertTrue(self.client.get(path).json()['auto_complete'])
        response = self.write('PATCH', path, {'auto_complete': False}, 1)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['auto_complete'])
        self.assertEqual(response.json()['schema_version'], 5)
        self.assertTrue(all('auto_complete' in g for g in self.client.get('/api/prompt-groups').json()['items']))
        self.assertFalse(service(self.rt).store.auto_complete_default())
        for bad in (0, 1, 'false', None):
            self.assertEqual(self.write('PATCH', '/api/settings', {'prompt_auto_complete_default': bad}, 1).status_code, 422)
            self.assertEqual(self.write('PATCH', path, {'auto_complete': bad}, 2).status_code, 422)
            self.assertEqual(self.write('POST', '/api/prompt-groups', {'name': 'Bad', 'auto_complete': bad}).status_code, 422)

    def test_entry_CAS_receipts_readonly_and_persistence(self):
        group = self.create()
        path = '/api/prompt-groups/'+group['dictionary_id']+'/entries'
        response = self.write('POST', path, {'nl': 'forest', 'variants': {'short': 'Tree'}}, 1)
        self.assertEqual(response.status_code, 201, response.text)
        entry = response.json()
        self.assertFalse(entry['generated']['nl'])
        path += '/'+entry['entry_id']
        body = {'nl': 'forest', 'variants': {'short': None, 'new': ' x\r\ny '}}
        response = self.write('PATCH', path, body, 2, 'edit')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['etag'], '"3"')
        self.assertEqual(response.json()['variants'], {'new': ' x\ny '})
        self.assertEqual(response.json()['edit_versions']['nl'], 1)
        self.assertEqual(self.write('PATCH', path, body, 2, 'edit').json(), response.json())
        self.assertEqual(self.write('PATCH', path, {}, 2, 'edit').status_code, 409)
        self.assertEqual(self.write('PATCH', path, {'nl': 'stale'}, 2).status_code, 412)
        self.assertEqual(self.client.get(path).json(), response.json())
        for field in ('generated', 'pending', 'edit_versions', 'completion', 'preview'):
            self.assertEqual(self.write('PATCH', path, {field: {}}, 3).status_code, 422)
        ref = GroupRef('global', None, group['dictionary_id'])
        saved = service(self.rt).store.load(ref).entries[0]
        self.assertEqual(saved.edit_versions.variants['short'], 1)
        self.assertEqual(saved.edit_versions.nl, 1)
        self.assertEqual(self.write('POST', path.rsplit('/', 1)[0], {'nl': 'forest', 'variants': {'x': None}}, 3).status_code, 422)
        missing = path.rsplit('/', 1)[0]+'/'+str(uuid4())
        self.assertEqual(self.write('PATCH', missing, {'nl': 'missing'}, 3).status_code, 404)

    def test_normalized_http_ingress_and_atomic_errors(self):
        group = self.create()
        path = '/api/prompt-groups/'+group['dictionary_id']+'/entries'
        body = {'nl': '\n@blue_cat\r\n', 'source_format': 'anima',
                'variants': {'key_name': '@blue_cat', 'empty': ''}}
        response = self.write('POST', path, body, 1, 'normalized')
        self.assertEqual(response.status_code, 201, response.text)
        entry = response.json()
        self.assertEqual(entry['nl'], '\nartist:blue cat\n')
        self.assertEqual(entry['variants'], {'key_name': 'artist:blue cat', 'empty': ''})
        self.assertNotIn('source_format', entry)
        self.assertEqual(self.write('POST', path, body, 1, 'normalized').json(), entry)
        target = path+'/'+entry['entry_id']
        for invalid, code in [({'nl': '{cat'}, 'normalization:syntax'),
                              ({'nl': '  '}, 'normalization:empty'),
                              ({'tag': []}, 'schema:invalid'),
                              ({'expand_nl': 'old'}, 'schema:invalid')]:
            failure = self.write('PATCH', target, invalid, 2)
            self.assertEqual(failure.status_code, 422, failure.text)
            self.assertEqual(failure.json()['error']['code'], code)
        self.assertEqual(self.client.get(target).json(), entry)
        updated = self.write('PATCH', target, {'source_format': 'anima', 'variants': {'new': '@other_artist'}}, 2)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()['nl'], entry['nl'])
        self.assertEqual(updated.json()['edit_versions']['nl'], 0)
