import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from stylelora.prompts._groups_v4 import (
    Group, GroupRef, GeneratedFields, FieldVersions, PendingField,
    content, decode_group, migrate_group, migrate_v2, migrate_v3, validate_entry,
)
from stylelora.prompts.group_store import GroupStore
from stylelora.prompts.dictionaries import (
    load_dictionary, save_dictionary, append_entries, delete_dictionary,
)
from stylelora.prompts.types import PromptDictionary, PromptEntry, PromptError, plain


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.identity = str(uuid4())
        self.entry = validate_entry({'tag': ['tree']})

    def test_defaults_and_content_boundary(self):
        self.assertEqual(plain(self.entry.generated), dict(tag=False, expand_nl=False, nl=False, variants={}))
        self.assertEqual(plain(self.entry.edit_versions), dict(tag=0, expand_nl=0, nl=0, variants={}))
        self.assertEqual(dict(self.entry.pending), {})
        self.assertEqual(set(content(self.entry)), {'tag', 'expand_nl', 'nl', 'disable', 'aspect', 'variants'})

    def test_variants_normalize_and_freeze(self):
        entry = validate_entry({'tag': ['tree'], 'variants': {' A.b ': ' \r\nx\ry\n ', 'a.b': ''}})
        self.assertEqual(dict(entry.variants), {'A.b': ' \nx\ny\n ', 'a.b': ''})
        with self.assertRaises(TypeError):
            entry.variants['new'] = ''

    def test_variants_invalid_with_field_paths(self):
        for variants in ([], {'': ''}, {'x\x00': ''}, {'\tx': ''}, {'x' * 129: ''}, {'a': 1},
                         {'a': 'x' * 16001}, {' a': '', 'a ': ''}, {str(i): '' for i in range(65)}):
            with self.subTest(variants=repr(variants)[:80]), self.assertRaises(PromptError) as caught:
                validate_entry({'tag': ['tree'], 'variants': variants})
            self.assertEqual(caught.exception.code, 'schema:invalid')
            self.assertTrue(caught.exception.issues[0].field.startswith('variants'))

    def test_variants_limits(self):
        variants = {str(i): '' for i in range(63)} | {'x' * 128: 'y' * 16000}
        self.assertEqual(len(validate_entry({'nl': 'tree', 'variants': variants}).variants), 64)

    def test_variants_cannot_replace_base_content(self):
        with self.assertRaises(PromptError):
            validate_entry({'variants': {'a': 'tree'}})

    def test_draft_rejects_internal_state(self):
        for key in ('generated', 'pending', 'edit_versions', 'completion', 'preview'):
            with self.subTest(key=key), self.assertRaises(PromptError):
                validate_entry({'tag': ['tree'], key: {}})

    def test_internal_state_roundtrip(self):
        entry = replace(self.entry, variants={'a.b': 'text'},
                        generated=GeneratedFields(tag=True, variants={'a.b': True}),
                        edit_versions=FieldVersions(nl=2, variants={'a.b': 3}),
                        pending={'nl': PendingField('pending', 'fake-task', 1, 2, 'fake-digest')})
        group = Group(self.identity, 'test', entries=(entry,), auto_complete=False)
        self.assertEqual(decode_group(plain(group)), group)
        with self.assertRaises(TypeError):
            entry.pending['tag'] = entry.pending['nl']

    def test_internal_state_invalid(self):
        for changes in ({'generated': {'tag': 1}}, {'generated': {'variants': {'a': 1}}},
                        {'edit_versions': {'nl': True}}, {'edit_versions': {'variants': {'a': -1}}},
                        {'pending': {'variants': {}}}, {'pending': {'nl': {}}},
                        {'pending': {'nl': dict(state='running', task_id='t', attempt=1, edit_version=0, input_digest='d')}}):
            with self.subTest(changes=changes), self.assertRaises(PromptError):
                replace(self.entry, **changes)

    def test_v1_v2_stable_ids_single_revision(self):
        for version in (1, 2):
            raw = dict(schema_version=version, dictionary_id=self.identity, name='old', kind='prompts',
                       revision=7, entries=[dict(tags=['tree'], natural_language='forest', rating='explicit')],
                       _operations={'old': {'digest': 'saved', 'result': 7}})
            first = migrate_group(raw, auto_complete_default=False)
            self.assertEqual(first, migrate_group(raw, auto_complete_default=False))
            self.assertEqual(first.entries[0].entry_id, migrate_v2(raw).entries[0].entry_id)
            self.assertEqual((first.schema_version, first.revision, first.auto_complete, first.nsfw), (4, 8, False, True))
            self.assertIn('old', first._operations)

    def test_v3_preserves_snapshot(self):
        raw = plain(Group(self.identity, 'old', scope='project', entries=(self.entry,), revision=5))
        raw['schema_version'] = 3
        raw.pop('auto_complete')
        for key in ('variants', 'generated', 'pending', 'edit_versions'):
            raw['entries'][0].pop(key)
        raw['entries'][0]['preview'] = dict(asset_revision=str(uuid4()), sha256='a' * 64, width=20, height=30, mime='image/webp')
        raw['_operations'] = {'old': {'digest': 'd', 'result': 5}}
        result = migrate_v3(raw, auto_complete_default=False)
        self.assertEqual((result.revision, result.scope, result.auto_complete), (6, 'project', False))
        self.assertEqual(plain(result.entries[0].preview), raw['entries'][0]['preview'])
        self.assertEqual(result.entries[0].entry_id, self.entry.entry_id)
        self.assertEqual(migrate_group(plain(result)), result)

    def test_group_bool_and_version_validation(self):
        with self.assertRaises(PromptError):
            Group(self.identity, 'bad', auto_complete=1)
        with self.assertRaises(PromptError):
            migrate_group({'schema_version': 5})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.identity = str(uuid4())
        self.ref = GroupRef('global', None, self.identity)
        self.store = GroupStore(self.root / 'StyleLoraAutom')
        self.path = self.store.path(self.ref)
        self.path.parent.mkdir(parents=True)
        self.raw = plain(Group(self.identity, 'old', entries=(validate_entry({'nl': 'forest'}),), revision=3))
        self.raw['schema_version'] = 3
        self.raw.pop('auto_complete')

    def write(self):
        data = ('\ufeff' + json.dumps(self.raw, indent=2) + '\n').encode('utf-8')
        self.path.write_bytes(data)
        return data

    def test_backup_original_bytes_and_restart(self):
        original = self.write()
        first = self.store.load(self.ref)
        self.assertEqual(self.path.with_suffix('.v3.bak').read_bytes(), original)
        self.assertEqual(first.revision, 4)
        self.assertFalse(hasattr(first, 'auto_complete'))
        self.assertIn('migration:v5', first._operations)
        saved = self.path.read_bytes()
        self.assertEqual(GroupStore(self.root / 'StyleLoraAutom').load(self.ref), first)
        self.assertEqual(self.path.read_bytes(), saved)

    def test_backup_failure_preserves_original(self):
        original = self.write()
        with patch('stylelora.prompts.group_store.os.fsync', side_effect=OSError), self.assertRaises(PromptError) as caught:
            self.store.load(self.ref)
        self.assertEqual(caught.exception.code, 'storage:io_error')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.path.with_suffix('.v3.bak').exists())
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])
        self.assertEqual(self.store.load(self.ref).revision, 4)

    def test_partial_backup_is_quarantined_and_rebuilt(self):
        original = self.write()
        backup = self.path.with_suffix('.v3.bak')
        backup.write_bytes(original[:20])
        self.assertEqual(self.store.load(self.ref).revision, 4)
        self.assertEqual(backup.read_bytes(), original)
        quarantined = list(self.path.parent.glob('*.bak.corrupt-*'))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), original[:20])

    def test_backup_atomic_publication_failure_and_retry(self):
        original = self.write()
        backup = self.path.with_suffix('.v3.bak')
        def fail_publish(staging, destination):
            self.assertEqual(Path(staging).parent, backup.parent)
            self.assertEqual(Path(staging).read_bytes(), original)
            self.assertFalse(backup.exists())
            raise OSError('publication failed')
        with patch('stylelora.prompts.group_store.os.link', side_effect=fail_publish), self.assertRaises(PromptError):
            self.store.load(self.ref)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(backup.exists())
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])
        self.assertEqual(self.store.load(self.ref).revision, 4)
        self.assertEqual(backup.read_bytes(), original)

    def test_valid_backup_is_not_replaced(self):
        original = self.write()
        backup = self.path.with_suffix('.v3.bak')
        backup.write_bytes(original)
        before = backup.stat()
        with patch('stylelora.prompts.group_store.os.link', side_effect=AssertionError('must not replace')):
            self.store.load(self.ref)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(list(self.path.parent.glob('*.bak.corrupt-*')), [])

    def test_publish_failure_retry(self):
        original = self.write()
        with patch('stylelora.prompts.group_store._write', side_effect=OSError), self.assertRaises(PromptError):
            self.store.load(self.ref)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.store.load(self.ref).revision, 4)

    def test_tombstone_upgrade_without_entry_decode(self):
        self.raw = dict(schema_version=3, dictionary_id=self.identity, scope='global', revision=8,
                        _deleted=True, _operations={'delete': {'digest': 'd', 'result': {'revision': 8}}})
        self.write()
        with patch('stylelora.prompts.group_store.migrate_group', side_effect=AssertionError), self.assertRaises(PromptError) as caught:
            self.store.load(self.ref)
        self.assertEqual(caught.exception.code, 'dictionary:not_found')
        saved = json.loads(self.path.read_bytes())
        self.assertTrue(saved['_deleted'])
        self.assertEqual((saved['schema_version'], saved['revision']), (5, 9))
        self.assertIn('delete', saved['_operations'])
        with self.assertRaises(PromptError):
            self.store.save(self.ref, Group(self.identity, 'resurrect'), 9, 'new')

    def test_high_version_protects_all_writers(self):
        self.raw['schema_version'] = 6
        original = self.write()
        with self.assertRaises(PromptError) as caught:
            self.store.load(self.ref)
        self.assertEqual(caught.exception.code, 'newer_version')
        with self.assertRaises(PromptError):
            save_dictionary(PromptDictionary(self.identity, 'old'), expected_revision=3, request_key='x', root=self.root)
        self.assertEqual(self.path.read_bytes(), original)

    def test_legacy_read_and_write_protection(self):
        for version in (3, 4):
            self.raw['schema_version'] = version
            original = self.write()
            legacy = load_dictionary(self.identity, root=self.root)
            self.assertEqual(legacy.revision, 3)
            actions = (
                lambda: save_dictionary(legacy, expected_revision=3, request_key='s', root=self.root),
                lambda: append_entries(self.identity, [PromptEntry(('tree',))], expected_revision=3, request_key='a', root=self.root),
                lambda: delete_dictionary(self.identity, expected_revision=3, root=self.root),
            )
            for action in actions:
                with self.assertRaises(PromptError) as caught:
                    action()
                self.assertEqual(caught.exception.code, 'dictionary:legacy_read_only')
                self.assertEqual(self.path.read_bytes(), original)

    def test_v1_v2_backup_names(self):
        for version in (1, 2):
            self.raw = dict(schema_version=version, dictionary_id=self.identity, name='old', kind='prompts',
                            revision=1, entries=[dict(tags=['tree'])])
            original = self.write()
            group = self.store.load(self.ref)
            self.assertEqual((group.schema_version, group.revision), (5, 2))
            self.assertEqual(self.path.with_suffix(f'.v{version}.bak').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
