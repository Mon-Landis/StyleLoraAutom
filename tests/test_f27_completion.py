import asyncio
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from stylelora.runtime import Runtime
from stylelora.runtime.prompt_completion import PromptCompletion
from stylelora.prompts.groups import GroupRef, validate_entry, content_digest, content
from stylelora.prompts.types import PromptError, plain
from stylelora.vision.executor import SkillStore


class FakeLLM:
    def __init__(self):
        self.calls = []
        self.hook = None
        self.wait = None

    async def resolve_role(self, role):
        return object()

    async def complete_json(self, request, repair_limit=0):
        self.calls.append(request)
        if request.cancel.canceled:
            raise AssertionError('Canceled request dispatched')
        if self.hook:
            self.hook()
        if self.wait:
            await self.wait.wait()
        return SimpleNamespace(value={'status':'ok','groups':[{'name':request.payload['groups'][0]['name'],
            'prompts':[{'index':0,'tag':['dog'],'expand_nl':'Sitting by a window.','nl':'A dog sits by a window.','disable':False}]}], 'issues':[]})


class CompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.llm = FakeLLM()
        self.rt = Runtime(Path(self.temp.name)/'StyleLoraAutom',llm=self.llm)
        self.addAsyncCleanup(self.rt.close)
        text = 'Fake local skill, no paid service.'
        self.svc = PromptCompletion(self.rt,skills=SkillStore([text]),skill_hash=hashlib.sha256(text.encode()).hexdigest())
        self.rt.prompt_completion = self.svc
        self.groups = self.svc.groups
        created = self.groups.create({'name':'test'},'group')
        self.ref = GroupRef('global',None,created['dictionary_id'])

    def add(self, raw):
        group = self.groups.store.load(self.ref)
        result = self.groups.edit(self.ref, raw, group.revision, str(uuid4()),action='add')
        return result['entry_id']

    async def run_task(self, ids=None, key=None):
        group = self.groups.store.load(self.ref)
        view = await self.svc.enqueue(self.ref,ids,group.revision,key or str(uuid4()))
        _, resource, _ = self.rt.tasks._record(view['task_id'])
        await self.svc.wait(resource.id)
        return self.svc.records[resource.id], view

    async def test_T22_fill_only_missing_preserve_disabled(self):
        identity = self.add({'tag':['cat'],'disable':True})
        record, _ = await self.run_task([identity])
        entry = self.groups.store.load(self.ref).entries[0]
        self.assertEqual(entry.tag,('cat',)); self.assertTrue(entry.disable)
        self.assertEqual(entry.completion.state,'complete')
        self.assertIn('tag',record['reports'][0]['warnings'])
        self.assertIn('disable',record['reports'][0]['warnings'])
        self.assertEqual(self.llm.calls[0].transport_retry_limit,0)

    async def test_T23_batch_skip_disabled_skill_unavailable(self):
        disabled = self.add({'tag':['cat'],'disable':True})
        enabled = self.add({'expand_nl':'A dog sits.'})
        record, _ = await self.run_task()
        self.assertEqual([i['entry_id'] for i in record['inputs']],[enabled])
        self.assertEqual(record['inputs'][0]['missing'],['tag'])
        self.svc.skill_hash = 'absent'
        group = self.groups.store.load(self.ref)
        with self.assertRaises(PromptError) as caught:
            await self.svc.enqueue(self.ref,[disabled],group.revision,'missing-skill')
        self.assertEqual(caught.exception.code,'completion:unavailable')
        self.assertEqual(len(self.llm.calls),1)
        self.assertEqual(self.groups.entry(group,disabled).completion.state,'pending')

    async def test_T24_manual_edit_stale_unrelated_merge_and_replay(self):
        identity = self.add({'tag':['cat']})
        other = self.add({'nl':'Other.'})
        def edit():
            group = self.groups.store.load(self.ref)
            self.groups.edit(self.ref,{'nl':'Human edit.'},group.revision,'human',identity)
        self.llm.hook = edit
        record, _ = await self.run_task([identity])
        self.assertEqual(record['reports'][0]['error_code'],'completion:stale')
        self.assertEqual(self.groups.entry(self.groups.store.load(self.ref),identity).nl,'Human edit.')
        def unrelated():
            group = self.groups.store.load(self.ref)
            self.groups.edit(self.ref,{'nl':'Changed other.'},group.revision,'unrelated',other)
        self.llm.hook = unrelated
        record, _ = await self.run_task([identity])
        self.assertEqual(record['status'],'done')
        before = self.groups.store.load(self.ref)
        first = self.svc.apply_result(record,record['inputs'][0],{'tag':['dog'],'expand_nl':'Sitting by a window.','nl':'A dog sits by a window.','disable':False})
        self.assertEqual(first,record['reports'][0])
        self.assertEqual(self.groups.store.load(self.ref),before)

    async def test_T25_cancel_and_recover_no_replay(self):
        identity = self.add({'tag':['cat']})
        self.llm.wait = asyncio.Event()
        group = self.groups.store.load(self.ref)
        view = await self.svc.enqueue(self.ref,[identity],group.revision,'queued')
        await asyncio.sleep(0)
        await self.rt.tasks.cancel(view['task_id'],'cancel')
        adapter, resource, _ = self.rt.tasks._record(view['task_id'])
        await adapter.wait(resource.id)
        self.assertEqual(adapter.records[resource.id]['status'],'canceled')
        self.assertEqual(self.groups.store.load(self.ref).entries[0].completion.state,'canceled')
        self.assertEqual(len(self.llm.calls),1)
        record = adapter.records[resource.id]
        record['status'] = 'running'; adapter.save(record)
        restored = PromptCompletion(self.rt,skills=self.svc.skills,skill_hash=self.svc.skill_hash)
        self.assertEqual(restored.records[resource.id]['status'],'paused')
        self.assertEqual(len(self.llm.calls),1)

    async def test_T09_nsfw_does_not_block_completion(self):
        identity = self.add({'tag':['cat']})
        group = self.groups.store.load(self.ref)
        self.groups.edit(self.ref,{'nsfw':True},group.revision,'nsfw')
        self.assertFalse(self.rt.product.get()['nsfw_enabled'])
        record, view = await self.run_task([identity])
        self.assertEqual(record['status'],'done')
        self.assertEqual((await self.rt.tasks.get(view['task_id'])).kind,'prompt')

    async def test_idempotent_enqueue(self):
        identity = self.add({'tag':['cat']})
        base = self.groups.store.load(self.ref).revision
        record, first = await self.run_task([identity], 'task-key')
        second = await self.svc.enqueue(self.ref,[identity],base,'task-key')
        self.assertEqual(first['task_id'],second['task_id']); self.assertEqual(len(self.llm.calls),1)

    async def test_complete_entry_is_noop_without_llm(self):
        identity = self.add({'tag':['cat'],'expand_nl':'Sitting.','nl':'A cat sits.'})
        self.rt.llm = None
        record, _ = await self.run_task([identity])
        self.assertEqual(record['status'],'done'); self.assertEqual(self.llm.calls,[])

    async def test_incomplete_keeps_valid_new_fields(self):
        identity = self.add({'tag':['cat']})
        entry = self.groups.store.load(self.ref).entries[0]
        record = {'id':str(uuid4()),'ref':plain(self.ref)}
        item = {'entry_id':identity,'digest':content_digest(entry),'missing':['expand_nl','nl']}
        report = self.svc.apply_result(record,item,{'expand_nl':'Sitting.'})
        self.assertEqual(report['error_code'],'completion:incomplete')
        stored = self.groups.store.load(self.ref).entries[0]
        self.assertEqual(stored.expand_nl,'Sitting.'); self.assertEqual(stored.tag,('cat',))

    async def test_mapping_rejects_wrong_index_or_count(self):
        from stylelora.runtime.prompt_completion import unpack_result
        for rows in [[], [{'index':1}], [{'index':0},{'index':1}]]:
            with self.assertRaises(PromptError):
                unpack_result({'status':'ok','groups':[{'name':'test','prompts':rows}]},'test')

    async def test_T25_unknown_result_never_retried(self):
        from stylelora.llm.types import LlmFailure
        identity = self.add({'tag':['cat']})
        def unknown():
            raise LlmFailure('llm:timeout',outcome='unknown')
        self.llm.hook = unknown
        record, _ = await self.run_task([identity])
        self.assertEqual(len(self.llm.calls),1)
        self.assertEqual(record['reports'][0]['error_code'],'llm:outcome_unknown')
        entry = self.groups.store.load(self.ref).entries[0]
        self.assertEqual(entry.completion.state,'failed'); self.assertEqual(entry.tag,('cat',))

    async def test_T25_project_close_pauses_entry_before_releasing_context(self):
        await self.rt.open(Path(self.temp.name)/'project.slproj',create={'name':'Project','trigger_word':''})
        created = self.groups.create({'name':'scoped'},'scoped')
        self.ref = GroupRef('project',self.rt.current.project_id,created['dictionary_id'])
        identity = self.add({'tag':['cat']})
        root = self.rt.current.handle.root
        self.llm.wait = asyncio.Event()
        group = self.groups.store.load(self.ref)
        view = await self.svc.enqueue(self.ref,[identity],group.revision,'project-task')
        await asyncio.sleep(0)
        await self.rt.close_project()
        self.assertEqual((await self.rt.tasks.get(view['task_id'])).status,'paused')
        raw = json_load(root/'dictionaries/prompts'/f'{self.ref.group_id}.json')
        self.assertEqual(raw['entries'][0]['completion']['state'],'paused')


def json_load(path):
    import json
    return json.loads(path.read_text(encoding='utf-8'))
