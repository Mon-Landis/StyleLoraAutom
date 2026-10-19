import json
import unittest
from uuid import uuid4
from dataclasses import replace
import test_f30_tasks_lock as fixtures
from stylelora.prompts.types import PromptError, plain
from stylelora.runtime.prompt_completion import PromptCompletion


class WritebackTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.PublicationTests.asyncSetUp
    add = fixtures.PublicationTests.add
    freeze = fixtures.PublicationTests.freeze
    entry = fixtures.PublicationTests.entry

    async def test_existing_nl_and_edit_versions_prevent_overwrite(self):
        record = self.freeze()
        item = record['inputs'][0]
        item.update(content={'nl': ''}, edit_versions={'nl': 0})
        group = self.groups.store.load(self.ref)
        self.groups.edit(self.ref, {'nl': 'Human'}, group.revision, 'same-value', self.entry().entry_id)
        report = self.svc.apply_result(record, item, {'nl': 'model'})
        self.assertEqual(report['applied_fields'], [])
        self.assertEqual(report['discarded_fields'], ['nl'])
        self.assertEqual(self.entry().nl, 'Human')
        self.assertEqual(self.entry().edit_versions.nl, 1)
        self.assertFalse(self.entry().generated.nl)
        before = self.groups.store.load(self.ref)
        self.assertEqual(self.svc.apply_result(record, item, {'nl': 'model'}), report)
        self.assertEqual(self.groups.store.load(self.ref), before)
        record.update(reports=[], status='failed')
        self.svc.save(record)
        restored = PromptCompletion(self.rt)
        self.assertEqual(restored.records[record['id']]['reports'], [report])

    async def test_old_three_field_locks_migrate_and_do_not_replay(self):
        record = self.freeze(version=4)
        group = self.groups.store.load(self.ref)
        raw = plain(group)
        raw['schema_version'] = 4
        entry = raw['entries'][0]
        entry.update(tag=['cat'], expand_nl='Window')
        for field in ('tag', 'expand_nl'):
            entry['pending'][field] = dict(entry['pending']['nl'])
            entry['generated'][field] = False
            entry['edit_versions'][field] = 0
        entry['completion']['missing'] = ['tag', 'expand_nl', 'nl']
        self.groups.store.path(self.ref).write_text(json.dumps(raw), encoding='utf-8')
        restored = PromptCompletion(self.rt)
        self.assertEqual(restored.records[record['id']]['error_code'], 'prompt:schema_changed')
        self.assertFalse(self.entry().pending)
        self.assertEqual(self.entry().completion.state, 'complete')
        self.assertEqual(self.entry().nl, 'cat\n\nWindow\n\nHuman')
        self.assertEqual(self.llm.calls, [])
        self.assertFalse(restored.active)

    async def test_old_receipt_recovered_before_schema_rejection(self):
        record = self.freeze(version=4)
        item = record['inputs'][0]
        report = {'entry_id': item['entry_id'], 'applied_fields': ['tag'], 'error_code': None}
        group = self.groups.store.load(self.ref)
        report = self.groups.store.mutate(self.ref, group.revision, self.svc.result_key(record, item),
                                         'completion-result', {}, lambda old: (old, report))
        restored = PromptCompletion(self.rt)
        self.assertEqual(restored.records[record['id']]['reports'], [report])
        self.assertEqual(restored.records[record['id']]['error_code'], 'prompt:schema_changed')
        self.assertEqual(self.llm.calls, [])
