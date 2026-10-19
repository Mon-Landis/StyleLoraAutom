import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from fastapi.testclient import TestClient
from stylelora.api import create_app
from stylelora.runtime import Runtime
from stylelora.runtime.prompt_generation import PromptGeneration
from stylelora.prompts.groups import GroupRef
from stylelora.prompts.types import plain, PromptError


class FakeLLM:
    def __init__(self):
        self.calls, self.roles = [], []
        self.hook = None

    async def resolve_role(self, role):
        self.roles.append(role)
        return object()

    async def complete_json(self, request, repair_limit=0):
        self.calls.append(request)
        group = request.payload['groups'][0]
        value = dict(status='ok', groups=[dict(name=group['name'], prompts=[
            dict(nl='A cat at a window.', disable=False)
            for _ in range(group['count'])])], issues=[])
        if self.hook:
            await self.hook(request, value)
        # Exercise the actual whole-reply validation boundary, with no network.
        import json
        from stylelora.llm.json_output import validate_json_reply
        validate_json_reply(json.dumps(value), plain(request.response_schema))
        return SimpleNamespace(value=value)


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.llm = FakeLLM()
        self.rt = Runtime(Path(self.temp.name)/'product', llm=self.llm)
        self.addAsyncCleanup(self.rt.close)
        self.svc = PromptGeneration(self.rt)
        self.rt.prompt_generation = self.svc
        self.groups = self.svc.groups
        created = self.groups.create({'name':'theme'}, 'group')
        self.ref = GroupRef('global', None, created['dictionary_id'])

    async def enqueue(self, count=1, key=None):
        view = await self.svc.enqueue(self.ref, 'forest', count, key or str(uuid4()))
        _, resource, _ = self.rt.tasks._record(view['task_id'])
        return self.svc.records[resource.id], view

    async def run_task(self, count=1, key=None):
        record, view = await self.enqueue(count, key)
        await self.svc.wait(record['id'])
        return record, plain(await self.rt.tasks.get(view['task_id']))

    async def test_single_payload_and_replay(self):
        record, view = await self.run_task(key='same')
        again = await self.svc.enqueue(self.ref, 'forest', 1, 'same')
        self.assertEqual(view['task_id'], again['task_id'])
        self.assertEqual(view['generated'], 1)
        self.assertEqual(self.llm.roles, ['prompt'])
        payload = plain(self.llm.calls[0].payload)
        self.assertEqual(payload['groups'], [{'name':'theme','count':1,'brief':'forest'}])
        self.assertNotIn('text_mode', payload); self.assertNotIn('subject_mix', payload)
        self.assertIn('SFW ONLY', payload['content_safety'])
        result = dict(status='ok', groups=[dict(name='theme', prompts=[dict(nl='A cat at a window.', disable=False)])], issues=[])
        self.assertEqual(await self.svc.apply_result(record, record['batches'][0], result), record['reports'][0])
        self.assertEqual(len(self.groups.store.load(self.ref).entries), 1)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_batches_are_concurrent(self):
        all_started = asyncio.Event()
        async def hook(request, value):
            if len(self.llm.calls) == 3:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), 2)
        self.llm.hook = hook
        record, view = await self.run_task(45)
        self.assertEqual([r.payload['groups'][0]['count'] for r in self.llm.calls], [20,20,5])
        self.assertEqual(view['generated'], 45)
        self.assertEqual(len(record['reports']), 3)

    async def test_nai_normalization_and_strict_single_field_contract(self):
        async def hook(request, value):
            value['groups'][0]['prompts'] = [
                {'nl': 'long_hair, {cat}', 'disable': False},
                {'nl': 'cat', 'disable': False, 'tag': []},
                {'nl': 'cat', 'disable': False, 'expand_nl': ''},
                {'nl': 'cat', 'disable': False, 'variants': {}},
                {'nl': '{broken', 'disable': False}]
        self.llm.hook = hook
        record, view = await self.run_task(5)
        from stylelora.prompts.prompt_normalize import normalize_prompt
        group = self.groups.store.load(self.ref)
        self.assertEqual((view['generated'], view['failed']), (1, 4))
        self.assertEqual(group.entries[0].nl, normalize_prompt('long_hair, {cat}', 'nai').text)
        self.assertTrue(group.entries[0].generated.nl)
        self.assertEqual(group.entries[0].variants, {})
        self.assertEqual(record['content_schema_version'], 5)
        self.assertEqual(record['normalization_version'], 'nai-storage-v1')
        self.assertEqual(self.llm.calls[0].payload['prompt_format'], 'nai')
        entry_schema = plain(self.llm.calls[0].response_schema)['properties']['groups']['items']['properties']['prompts']['items']['anyOf'][0]
        self.assertEqual(set(entry_schema['properties']), {'nl', 'disable', 'aspect'})

    async def test_old_generation_recovers_receipt_but_rejects_new_callback(self):
        record, _ = await self.run_task()
        record.pop('content_schema_version')
        record.update(status='running', reports=[])
        self.svc.save(record)
        restored = PromptGeneration(self.rt)
        recovered = restored.records[record['id']]
        self.assertEqual(recovered['status'], 'paused')
        self.assertEqual(recovered['error_code'], 'prompt:schema_changed')
        self.assertEqual(len(recovered['reports']), 1)
        before = self.groups.store.load(self.ref)
        with self.assertRaises(PromptError) as caught:
            await restored.apply_result(recovered, {'batch': 1, 'requested': 1}, {})
        self.assertEqual(caught.exception.code, 'prompt:schema_changed')
        self.assertEqual(self.groups.store.load(self.ref), before)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertFalse(restored.active)

    async def test_partial_and_invalid_siblings(self):
        async def hook(request, value):
            value['status'] = 'partial'
            value['groups'][0]['prompts'] = [value['groups'][0]['prompts'][0], {'tag':42}]
        self.llm.hook = hook
        record, view = await self.run_task(4)
        self.assertEqual(view['status'], 'done')
        self.assertEqual((view['generated'], view['failed']), (1,3))
        self.assertEqual({i['code'] for i in view['issues']}, {'count_short','entry:invalid'})

    async def test_partial_preserves_model_count_short(self):
        issue = dict(group='theme', index=-1, code='count_short', detail='Only one variation available.')
        async def hook(request, value):
            value['status'] = 'partial'
            value['groups'][0]['prompts'] = value['groups'][0]['prompts'][:1]
            value['issues'] = [issue]
        self.llm.hook = hook
        _, view = await self.run_task(3)
        self.assertEqual(view['issues'], [issue])
        self.assertEqual((view['generated'], view['failed']), (1,2))

    async def test_replay_conflict_and_mapping_rejection(self):
        record, _ = await self.run_task(key='same')
        with self.assertRaises(PromptError):
            await self.svc.enqueue(self.ref, 'other theme', 1, 'same')
        from stylelora.runtime.prompt_generation import unpack_result
        for value in [dict(status='ok', groups=[], issues=[]),
                      dict(status='partial', groups=[dict(name='theme',prompts=[])],issues=[]),
                      dict(status='refused_nsfw',groups=[],issues=[])]:
            with self.assertRaises(PromptError):
                unpack_result(value, 'theme', 1)

    async def test_nsfw_and_reserved_metadata(self):
        group = self.groups.store.load(self.ref)
        self.groups.edit(self.ref, {'nsfw':True}, group.revision, 'nsfw')
        view = await self.svc.enqueue(self.ref, 'theme', 1, 'metadata', subject_mix={'future':1})
        _, resource, _ = self.rt.tasks._record(view['task_id'])
        await self.svc.wait(resource.id)
        self.assertEqual(self.llm.roles, ['prompt_nsfw'])
        payload = self.llm.calls[0].payload
        self.assertEqual(payload['rating_policy'], 'nsfw_allowed')
        self.assertIn('NSFW REQUESTED', payload['content_safety'])
        self.assertNotIn('subject_mix', payload)

    async def test_cas_conflict_preserves_edit_and_name(self):
        mutate = self.groups.store.mutate
        raced = False
        def race(ref, revision, key, operation, payload, change):
            nonlocal raced
            if operation == 'generation-append' and not raced:
                raced = True
                self.groups.edit(ref, {'nl':'Human entry'}, revision, 'human', action='add')
                group = self.groups.store.load(ref)
                self.groups.edit(ref, {'name':'Renamed'}, group.revision, 'rename')
            return mutate(ref, revision, key, operation, payload, change)
        self.groups.store.mutate = race
        _, view = await self.run_task()
        group = self.groups.store.load(self.ref)
        self.assertEqual(view['generated'], 1)
        self.assertEqual(group.name, 'Renamed')
        self.assertEqual(group.entries[0].nl, 'Human entry')
        self.assertEqual(len(group.entries), 2)

    async def test_cas_conflict_retries_are_bounded(self):
        mutate = self.groups.store.mutate
        attempts = 0
        def race(ref, revision, key, operation, payload, change):
            nonlocal attempts
            if operation == 'generation-append':
                attempts += 1
                group = self.groups.store.load(ref)
                self.groups.edit(ref, {'nl':f'Human {attempts}'}, group.revision, f'human-{attempts}', action='add')
            return mutate(ref, revision, key, operation, payload, change)
        self.groups.store.mutate = race
        _, view = await self.run_task()
        from stylelora.runtime.prompt_generation import CAS_RETRY_LIMIT
        self.assertEqual(attempts, CAS_RETRY_LIMIT)
        self.assertEqual(view['status'], 'failed')
        self.assertEqual(view['generated'], 0)
        self.assertEqual(view['batches'][0]['status'], 'failed')

    async def test_brief_limit(self):
        from stylelora.runtime.prompt_generation import BRIEF_LIMIT
        with self.assertRaises(PromptError):
            await self.svc.enqueue(self.ref, 'x'*(BRIEF_LIMIT+1), 1, 'long')
        view = await self.svc.enqueue(self.ref, 'x'*BRIEF_LIMIT, 1, 'edge')
        self.assertEqual(view['requested'], 1)

    async def test_cancel_retains_completed_batch(self):
        waiting = asyncio.Event()
        async def hook(request, value):
            if request.payload['groups'][0]['count'] == 1:
                waiting.set()
                await asyncio.Event().wait()
        self.llm.hook = hook
        record, view = await self.enqueue(21)
        await asyncio.wait_for(waiting.wait(), 2)
        await self.rt.tasks.cancel(view['task_id'], 'cancel')
        await self.svc.wait(record['id'])
        self.assertEqual(record['status'], 'canceled')
        self.assertEqual(len(self.groups.store.load(self.ref).entries), 20)

    async def test_failed_batch_retains_other_results(self):
        async def hook(request, value):
            if request.payload['groups'][0]['count'] == 1:
                raise RuntimeError('fake failure')
        self.llm.hook = hook
        _, view = await self.run_task(21)
        self.assertEqual(view['status'], 'failed')
        self.assertEqual((view['generated'], view['failed']), (20,1))

    async def test_project_close_pauses(self):
        await self.rt.open(Path(self.temp.name)/'project.slproj', create={'name':'Project','trigger_word':''})
        created = self.groups.create({'name':'project'}, 'project')
        self.ref = GroupRef('project', self.rt.current.project_id, created['dictionary_id'])
        waiting = asyncio.Event()
        async def hook(request, value):
            waiting.set()
            await asyncio.Event().wait()
        self.llm.hook = hook
        record, view = await self.enqueue()
        await waiting.wait()
        await self.rt.close_project()
        self.assertEqual((await self.rt.tasks.get(view['task_id'])).status, 'paused')

    async def test_restore_does_not_replay(self):
        record, _ = await self.run_task()
        record['status'] = 'running'; self.svc.save(record)
        restored = PromptGeneration(self.rt)
        self.assertEqual(restored.records[record['id']]['status'], 'paused')
        self.assertEqual(len(self.llm.calls), 1)

    async def test_restore_ignores_legacy_completion_records(self):
        # A completion task persisted before F31 has no batches; loading must not crash.
        import json
        legacy = dict(id=str(uuid4()), ref=plain(self.ref), entries=[], base_revision=1,
                      status='done', reports=[], inputs=[], request_key='old', digest='d',
                      group_name='theme', version=1, content_schema_version=5,
                      normalization_version='nai-storage-v1')
        self.svc.root.mkdir(parents=True, exist_ok=True)
        (self.svc.root/'legacy.json').write_text(json.dumps(legacy), encoding='utf-8')
        (self.svc.root/'broken.json').write_text('{not json', encoding='utf-8')
        restored = PromptGeneration(self.rt)
        self.assertNotIn(legacy['id'], restored.records)
        self.assertEqual(restored.records, {})

    async def test_restore_recovers_committed_batch_report(self):
        # Simulate a crash after the group snapshot (with receipt) but before the report save.
        record, _ = await self.run_task()
        record['reports'] = []; record['status'] = 'running'; self.svc.save(record)
        restored = PromptGeneration(self.rt)
        self.rt.prompt_generation = restored
        recovered = restored.records[record['id']]
        self.assertEqual(recovered['status'], 'paused')
        self.assertEqual(len(recovered['reports']), 1)
        view = plain(await restored.snapshot(record['id']))
        self.assertEqual(view['generated'], 1)
        self.assertEqual(len(self.groups.store.load(self.ref).entries), 1)
        self.assertEqual(len(self.llm.calls), 1)


class GenerationApiTests(unittest.TestCase):
    def test_count_bounds_and_response(self):
        with tempfile.TemporaryDirectory() as temp:
            rt = Runtime(Path(temp)/'product', llm=FakeLLM())
            with TestClient(create_app(rt), base_url='http://127.0.0.1:8000') as client:
                client.headers['Authorization'] = 'Bearer '+rt.token
                svc = rt.prompt_generation
                group = svc.groups.create({'name':'test'}, 'group')
                body = dict(type='prompt_generation', ref=plain(GroupRef('global',None,group['dictionary_id'])), brief='forest')
                for count in (0,201,-1,True,1.5,'2'):
                    response = client.post('/api/tasks', json=dict(body,count=count), headers={'Idempotency-Key':str(uuid4())})
                    self.assertEqual(response.status_code,422,response.text)
                response = client.post('/api/tasks', json=dict(body,count=1,brief='x'*4001), headers={'Idempotency-Key':str(uuid4())})
                self.assertEqual(response.status_code,422,response.text)
                response = client.post('/api/tasks', json=dict(body,count=1), headers={'Idempotency-Key':'valid'})
                self.assertEqual(response.status_code,202,response.text)
                detail = client.get('/api/tasks/'+response.json()['task_id'])
                self.assertEqual(detail.json()['type'],'prompt_generation')
                self.assertIn('reports',detail.json())
