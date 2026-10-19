import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from stylelora.prompts.groups import Group, GroupRef, validate_entry, GeneratedFields
from stylelora.prompts.group_store import GroupStore
from stylelora.prompts.dictionaries import _write, load_dictionary, save_dictionary, append_entries, delete_dictionary
from stylelora.prompts.types import PromptError, plain
from stylelora.runtime.prompt_groups import apply_user_patch, normalize_fields


class StoreEditTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = GroupStore(self.root / 'StyleLoraAutom')
        self.ref = GroupRef('global', None, str(uuid4()))
        self.path = self.store.path(self.ref)
        self.row = dict(entry_id=str(uuid4()), tag=['blue_cat'], expand_nl='repeat', nl='repeat',
                        variants={'key_name': 'blue_cat', 'empty': ''})
        self.raw = dict(schema_version=4, dictionary_id=self.ref.group_id, name='old',
                        scope='global', kind='prompts', revision=7, entries=[self.row],
                        _operations={'old': {'digest': 'd', 'result': 7}})

    def write(self):
        _write(self.path, self.raw)
        return self.path.read_bytes()

    def test_v4_backup_receipt_and_idempotent_read(self):
        original = self.write()
        group = self.store.load(self.ref)
        self.assertEqual((group.schema_version, group.revision), (5, 8))
        self.assertEqual(group.entries[0].nl, 'repeat')
        self.assertEqual(group.entries[0].variants['key_name'], 'blue cat')
        self.assertEqual(group.entries[0].variants['tag'], 'blue cat')
        self.assertEqual(group.entries[0].variants['expand_nl'], 'repeat')
        self.assertEqual(group._operations['migration:v5']['result']['warnings'][0]['code'],
                         'migration:possible_duplicate')
        self.assertIn('old', group._operations)
        self.assertEqual(self.path.with_suffix('.v4.bak').read_bytes(), original)
        saved = self.path.read_bytes()
        with patch('stylelora.prompts.group_store._migration_backup', side_effect=AssertionError):
            self.assertEqual(self.store.load(self.ref), group)
        self.assertEqual(self.path.read_bytes(), saved)

    def test_different_valid_backup_blocks_migration(self):
        original = self.write()
        backup = self.path.with_suffix('.v4.bak')
        different = json.dumps(self.raw | {'name': 'different'}).encode()
        backup.write_bytes(different)
        with self.assertRaises(PromptError):
            self.store.load(self.ref)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(backup.read_bytes(), different)

    def test_patch_normalizes_only_submitted_values(self):
        entry = replace(validate_entry({'nl': '1.5::blue cat::', 'variants': {'key_name': 'keep_me'}}),
                        generated=GeneratedFields(True, {'key_name': True}))
        updated = apply_user_patch(entry, {'source_format': 'anima', 'variants': {'new_key': '@blue_cat', 'empty': ''}})
        self.assertEqual(updated.nl, entry.nl)
        self.assertEqual(updated.edit_versions.nl, 0)
        self.assertTrue(updated.generated.nl)
        self.assertEqual(updated.variants['key_name'], 'keep_me')
        self.assertTrue(updated.generated.variants['key_name'])
        self.assertEqual(updated.variants['new_key'], 'artist:blue cat')
        self.assertEqual(updated.variants['empty'], '')
        same = apply_user_patch(updated, {'nl': updated.nl})
        self.assertEqual(same.nl, updated.nl)
        self.assertEqual(same.edit_versions.nl, 1)
        self.assertFalse(same.generated.nl)

    def test_migration_preserves_assets_versions_and_clears_old_locks(self):
        preview = dict(asset_revision=str(uuid4()), sha256='a'*64, width=20, height=10)
        self.row.update(preview=preview, aspect='landscape', disable=True,
                        generated={'tag': True, 'expand_nl': True, 'nl': True, 'variants': {'key_name': True}},
                        edit_versions={'tag': 3, 'expand_nl': 5, 'nl': 2, 'variants': {'key_name': 4}},
                        pending={'tag': {'obsolete': 'lock'}}, completion={'state': 'queued', 'missing': ['tag']})
        self.write()
        group = self.store.load(self.ref)
        entry = group.entries[0]
        self.assertEqual(entry.preview.asset_revision, preview['asset_revision'])
        self.assertEqual((entry.aspect, entry.disable), ('landscape', True))
        self.assertTrue(entry.generated.nl)
        self.assertTrue(entry.generated.variants['key_name'])
        self.assertEqual((entry.edit_versions.nl, entry.edit_versions.variants['key_name']), (6, 4))
        self.assertEqual(dict(entry.pending), {})
        self.assertFalse(hasattr(entry, 'completion'))
        self.assertNotIn('tag', plain(entry))
        self.assertNotIn('expand_nl', plain(entry))

    def test_v4_tombstone_only_upgrades_envelope(self):
        self.raw.update(_deleted=True, entries='not decodable')
        original = self.write()
        with patch('stylelora.prompts.group_store.migrate_group', side_effect=AssertionError):
            with self.assertRaises(PromptError) as caught:
                self.store.load(self.ref)
        self.assertEqual(caught.exception.code, 'dictionary:not_found')
        saved = json.loads(self.path.read_bytes())
        self.assertEqual((saved['schema_version'], saved['revision']), (5, 8))
        self.assertEqual(saved['entries'], 'not decodable')
        self.assertEqual(self.path.with_suffix('.v4.bak').read_bytes(), original)
        before = self.path.read_bytes()
        with self.assertRaises(PromptError):
            self.store.load(self.ref)
        self.assertEqual(self.path.read_bytes(), before)

    def test_normalization_errors_and_removed_fields(self):
        for raw, code in [({'nl': '{cat'}, 'normalization:syntax'),
                          ({'nl': ' '}, 'normalization:empty'),
                          ({'nl': 'cat', 'tag': []}, 'schema:invalid'),
                          ({'nl': 'cat', 'source_format': 'guess'}, 'schema:invalid')]:
            with self.subTest(raw=raw), self.assertRaises(PromptError) as caught:
                normalize_fields(raw)
            self.assertEqual(caught.exception.code, code)

    def test_mutation_replay_before_callback_and_cas(self):
        entry = validate_entry(normalize_fields({'nl': 'blue_cat'}))
        group = Group(self.ref.group_id, 'new', entries=(entry,))
        self.store.save(self.ref, group, 0, 'create')
        def change(old):
            updated = apply_user_patch(old.entries[0], {'nl': entry.nl})
            return replace(old, entries=(updated,)), plain(updated)
        first = self.store.mutate(self.ref, 1, 'edit', 'patch', {'nl': entry.nl}, change)
        def fail(_):
            self.fail('replay must not call mutation')
        self.assertEqual(self.store.mutate(self.ref, 1, 'edit', 'patch', {'nl': entry.nl}, fail), first)
        self.assertEqual(self.store.load(self.ref).entries[0].edit_versions.nl, 1)
        with self.assertRaises(PromptError) as caught:
            self.store.mutate(self.ref, 1, 'stale', 'patch', {}, fail)
        self.assertEqual(caught.exception.code, 'dictionary:revision_conflict')

    def test_long_nl_reports_identity_and_preserves_original(self):
        # tag/expand_nl no longer merge into nl, so only nl itself can overflow.
        self.row.update(expand_nl='x' * 9000, nl='y' * 16001)
        original = self.write()
        with self.assertRaises(PromptError) as caught:
            self.store.load(self.ref)
        self.assertIn(self.row['entry_id'], caught.exception.issues[0].message)
        self.assertIn('length=', caught.exception.issues[0].message)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.path.with_suffix('.v4.bak').exists())

    def test_v5_legacy_readonly_and_future_version(self):
        self.write()
        self.store.load(self.ref)
        original = self.path.read_bytes()
        legacy = load_dictionary(self.ref.group_id, root=self.root)
        self.assertEqual(legacy.entries[0].tags, ())
        self.assertEqual(legacy.entries[0].natural_language, 'repeat')
        actions = [lambda: save_dictionary(legacy, expected_revision=8, request_key='s', root=self.root),
                   lambda: append_entries(self.ref.group_id, (), expected_revision=8, request_key='a', root=self.root),
                   lambda: delete_dictionary(self.ref.group_id, expected_revision=8, root=self.root)]
        for action in actions:
            with self.assertRaises(PromptError) as caught:
                action()
            self.assertEqual(caught.exception.code, 'dictionary:legacy_read_only')
            self.assertEqual(self.path.read_bytes(), original)
        self.raw['schema_version'] = 6
        original = self.write()
        with self.assertRaises(PromptError) as caught:
            self.store.load(self.ref)
        self.assertEqual(caught.exception.code, 'newer_version')
        self.assertEqual(self.path.read_bytes(), original)
