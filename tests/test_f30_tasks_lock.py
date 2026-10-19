import unittest
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4
import test_f27_completion as fixtures
from stylelora.prompts.groups import PendingField, CompletionState
from stylelora.prompts.types import PromptError, plain
from stylelora.runtime.prompt_completion import PromptCompletion


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.CompletionTests.asyncSetUp
    add = fixtures.CompletionTests.add

    def freeze(self, version=5):
        self.add({'nl': 'Human'})
        group = self.groups.store.load(self.ref)
        identity = str(uuid4())
        record = dict(id=identity, task_id=identity, ref=plain(self.ref), status='running',
                      request_key=identity, reports=[], version=0, error_code=None,
                      inputs=[dict(entry_id=group.entries[0].entry_id, attempt=1, missing=['nl'])])
        if version is not None:
            record['content_schema_version'] = version
        entry = replace(group.entries[0], pending={'nl': PendingField('pending', identity, 1, 0, 'digest')},
                        completion=CompletionState('running', (), identity))
        self.groups.store.save(self.ref, replace(group, entries=(entry,)), group.revision, str(uuid4()))
        self.svc.records[identity] = record
        self.svc.save(record)
        return record

    def entry(self):
        return self.groups.store.load(self.ref).entries[0]

    async def test_recovery_releases_owned_lock_without_model(self):
        record = self.freeze()
        restored = PromptCompletion(self.rt)
        self.assertEqual(restored.records[record['id']]['status'], 'paused')
        self.assertEqual(restored.records[record['id']]['error_code'], 'completion:interrupted')
        self.assertFalse(self.entry().pending)
        self.assertEqual(self.entry().nl, 'Human')
        self.assertFalse(restored.active)
        self.assertEqual(self.llm.calls, [])

    async def test_cleanup_failure_is_durable_and_preserves_newer_owner(self):
        record = self.freeze()
        record['status'] = 'paused'
        with patch.object(self.svc, 'release_pending', side_effect=OSError('disk unavailable')):
            self.svc.cleanup(record)
        self.assertTrue(record['cleanup_pending'])
        self.assertEqual(record['error_code'], 'storage:io_error')
        with patch.object(PromptCompletion, 'release_pending', side_effect=OSError('still unavailable')):
            restored = PromptCompletion(self.rt)
        self.assertTrue(restored.records[record['id']]['cleanup_pending'])
        self.assertTrue(self.entry().pending)
        group = self.groups.store.load(self.ref)
        newer = replace(self.entry().pending['nl'], attempt=2)
        self.groups.store.save(self.ref, replace(group, entries=(replace(self.entry(), pending={'nl': newer}),)),
                               group.revision, str(uuid4()))
        restored = PromptCompletion(self.rt)
        self.assertFalse(restored.records[record['id']]['cleanup_pending'])
        self.assertEqual(self.entry().pending, {'nl': newer})
        self.assertEqual(self.llm.calls, [])

    async def test_schema_gate_and_late_callback(self):
        record = self.freeze(version=None)
        restored = PromptCompletion(self.rt)
        recovered = restored.records[record['id']]
        self.assertEqual(recovered['status'], 'paused')
        self.assertEqual(recovered['error_code'], 'prompt:schema_changed')
        self.assertEqual((await restored.snapshot(record['id'])).recovery.action, 'none')
        before = self.groups.store.load(self.ref)
        with self.assertRaises(PromptError) as caught:
            restored.apply_result(recovered, recovered['inputs'][0], {'tag': ['late'], 'nl': 'late'})
        self.assertEqual(caught.exception.code, 'prompt:schema_changed')
        self.assertEqual(self.groups.store.load(self.ref), before)
        self.assertEqual(self.llm.calls, [])

    async def test_project_reopen_retries_cleanup_without_publication(self):
        from pathlib import Path
        from stylelora.prompts.groups import GroupRef
        path = Path(self.temp.name)/'project.slproj'
        await self.rt.open(path, create={'name': 'Project', 'trigger_word': ''})
        created = self.groups.create({'name': 'scoped'}, 'scoped')
        self.ref = GroupRef('project', self.rt.current.project_id, created['dictionary_id'])
        record = self.freeze(version=4)
        record['status'] = 'paused'
        record['error_code'] = 'prompt:schema_changed'
        with patch.object(self.svc, 'release_pending', side_effect=OSError('disk unavailable')):
            self.svc.cleanup(record)
        await self.rt.close_project()
        await self.rt.open(path)
        self.ref = replace(self.ref, project_token=self.rt.current.project_id)
        self.assertEqual(record['ref'], plain(self.ref))
        self.assertFalse(record['cleanup_pending'])
        self.assertFalse(self.entry().pending)
        before = self.groups.store.load(self.ref)
        view = await self.svc.enqueue(self.ref, None, before.revision, 'reopened', trigger='auto_leave')
        self.assertEqual(view['status'], 'done')
        self.assertEqual(self.groups.store.load(self.ref), before)
        self.assertEqual(self.llm.calls, [])
