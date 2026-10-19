import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from stylelora.runtime import Runtime
from stylelora.runtime.prompt_completion import PromptCompletion, select_completion_fields
from stylelora.prompts.groups import GroupRef
from stylelora.prompts.types import PromptError


class FakeLLM:
    def __init__(self):
        self.calls = []

    async def resolve_role(self, role):
        self.calls.append(role)
        raise AssertionError('v5 completion must not call LLM')


class CompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.llm = FakeLLM()
        self.rt = Runtime(Path(self.temp.name)/'product', llm=self.llm)
        self.addAsyncCleanup(self.rt.close)
        self.svc = PromptCompletion(self.rt)
        self.rt.prompt_completion = self.svc
        self.groups = self.svc.groups
        created = self.groups.create({'name': 'test'}, 'group')
        self.ref = GroupRef('global', None, created['dictionary_id'])

    def add(self, raw):
        group = self.groups.store.load(self.ref)
        return self.groups.edit(self.ref, raw, group.revision, str(uuid4()), action='add')['entry_id']

    async def run_task(self, ids=None, key=None):
        group = self.groups.store.load(self.ref)
        view = await self.svc.enqueue(self.ref, ids, group.revision, key or str(uuid4()))
        return self.svc.records[view['resources'][0]['id']], view

    async def test_valid_entries_are_noop_for_both_triggers(self):
        identity = self.add({'nl': 'cat', 'disable': True, 'variants': {'short': ''}})
        before = self.groups.store.load(self.ref)
        self.rt.llm = None
        for trigger in ('manual', 'auto_leave'):
            ids = [identity] if trigger == 'manual' else None
            view = await self.svc.enqueue(self.ref, ids, before.revision, trigger, trigger=trigger)
            self.assertEqual(view['status'], 'done')
            record = self.svc.records[view['resources'][0]['id']]
            self.assertEqual(record['inputs'], [])
            self.assertEqual(record['content_schema_version'], 5)
            self.assertEqual(record['normalization_version'], 'nai-storage-v1')
        self.assertEqual(self.groups.store.load(self.ref), before)
        self.assertEqual(self.llm.calls, [])

    async def test_empty_and_terminal_entries_never_publish(self):
        from dataclasses import replace
        from stylelora.prompts.groups import CompletionState
        record, _ = await self.run_task()
        self.assertEqual(record['inputs'], [])
        self.add({'nl': 'cat'})
        for state in ('failed', 'paused', 'canceled', 'queued', 'running', 'complete'):
            group = self.groups.store.load(self.ref)
            entry = replace(group.entries[0], completion=CompletionState(state))
            self.groups.store.save(self.ref, replace(group, entries=(entry,)), group.revision, str(uuid4()))
            before = self.groups.store.load(self.ref)
            for trigger in ('manual', 'auto_leave'):
                view = await self.svc.enqueue(self.ref, None, before.revision, str(uuid4()), trigger=trigger)
                self.assertEqual(view['status'], 'done')
                self.assertEqual(self.groups.store.load(self.ref), before)
        self.assertEqual(self.llm.calls, [])
        self.assertFalse(self.svc.active)
        self.assertEqual(select_completion_fields(before.entries[0], automatic=False), ())

    async def test_replay_cas_and_request_validation(self):
        identity = self.add({'nl': 'cat'})
        record, first = await self.run_task([identity], 'same')
        again = await self.svc.enqueue(self.ref, [identity], record['base_revision'], 'same')
        self.assertEqual(first['task_id'], again['task_id'])
        for ids, revision, key, code in [
            ([], record['base_revision'], 'same', 'dictionary:request_key_conflict'),
            (None, 1, 'stale', 'dictionary:revision_conflict'),
            ([str(uuid4())], record['base_revision'], 'missing', 'entry:not_found')]:
            with self.assertRaises(PromptError) as caught:
                await self.svc.enqueue(self.ref, ids, revision, key)
            self.assertEqual(caught.exception.code, code)
        restored = PromptCompletion(self.rt)
        self.assertEqual(restored.records[record['id']]['status'], 'done')
        self.assertFalse(restored.active)
        self.assertEqual(self.llm.calls, [])
