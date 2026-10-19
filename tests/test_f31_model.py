import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from stylelora.prompts.groups import (
    BASE_FIELDS, CONTENT, Entry, Group, GeneratedFields, FieldVersions,
    PendingField, PreviewRef, validate_entry, decode_group,
    migrate_group, migrate_v4, migrate_v3, migrate_v2, content, content_digest,
    render, freeze_groups, legacy_dictionary,
)
from stylelora.prompts.group_legacy import (
    legacy_version, merge_legacy_content, legacy_content_issues, adapt_legacy_entry,
    variant_issues,
)
from stylelora.prompts.types import PromptError, plain


class V5Tests(unittest.TestCase):
    def setUp(self):
        self.identity, self.entry_id = str(uuid4()), str(uuid4())

    def entry(self, **values):
        return validate_entry({'entry_id': self.entry_id, 'nl': ' forest ', **values})

    def group(self, *entries):
        return Group(self.identity, 'test', entries=entries)

    def test_v5_fields_and_defaults(self):
        entry = self.entry()
        self.assertEqual(BASE_FIELDS, {'nl'})
        self.assertEqual(CONTENT, {'nl', 'variants', 'disable', 'aspect'})
        self.assertFalse(hasattr(entry, 'tag'))
        self.assertFalse(hasattr(entry, 'expand_nl'))
        self.assertEqual(plain(entry.generated), {'nl': False, 'variants': {}})
        self.assertEqual(plain(entry.edit_versions), {'nl': 0, 'variants': {}})
        self.assertFalse(hasattr(entry, 'completion'))
        group = self.group(entry)
        self.assertEqual((group.schema_version, group.prompt_format, group.normalization_version),
                         (5, 'nai', 'nai-storage-v1'))
        self.assertEqual(decode_group(plain(group)), group)

    def test_required_nl_and_exact_length(self):
        for value in (None, '', ' \n\t', 1, ' ' + 'x' * 16000):
            with self.subTest(value=repr(value)[:30]), self.assertRaises(PromptError):
                self.entry(nl=value)
        self.assertEqual(len(self.entry(nl='x' * 16000).nl), 16000)
        with self.assertRaises(PromptError):
            Entry(self.entry_id)
        with self.assertRaises(PromptError):
            validate_entry({'variants': {'only': 'forest'}})

    def test_draft_rejects_removed_and_internal_fields(self):
        for key in ('tag', 'expand_nl', 'generated', 'edit_versions', 'pending', 'preview', 'completion'):
            with self.subTest(key=key), self.assertRaises(PromptError) as caught:
                self.entry(**{key: {}})
            self.assertIn(key, [i.field for i in caught.exception.issues])
        with self.assertRaises(PromptError):
            validate_entry({'entry_id': self.entry_id, 'nl': 'forest'}, allow_id=False)

    def test_validation_preserves_text_without_dialect_conversion(self):
        text = ' \n1.5::blue cat::, artist:name\n\n '
        group = self.group(self.entry(nl=text, variants={'night': text, 'empty': ''}))
        with patch('stylelora.prompts.groups.normalize_prompt', side_effect=AssertionError('read must not convert')):
            self.assertEqual(migrate_group(plain(group)), group)
        self.assertEqual(group.entries[0].nl, text)

    def test_variant_limits_keys_and_immutability(self):
        entry = self.entry(variants={' A.b ': ' \r\nx\ry\n ', 'a.b': ''})
        self.assertEqual(dict(entry.variants), {'A.b': ' \nx\ny\n ', 'a.b': ''})
        with self.assertRaises(TypeError):
            entry.variants['new'] = 'x'
        for variants in ([], {'': ''}, {'x\x00': ''}, {'x' * 129: ''}, {'a': 1},
                         {'a': 'x' * 16001}, {' a': '', 'a ': ''}, {str(i): '' for i in range(65)}):
            with self.subTest(variants=repr(variants)[:30]), self.assertRaises(PromptError):
                self.entry(variants=variants)
        self.assertEqual(len(self.entry(variants={str(i): 'x' * 16000 for i in range(64)}).variants), 64)

    def test_internal_state_roundtrip_and_digest_boundary(self):
        original = self.entry(variants={'night': ''})
        updated = replace(original, generated=GeneratedFields(True, {'night': True}),
                          edit_versions=FieldVersions(4, {'night': 2, 'deleted': 7}),
                          pending={'nl': PendingField('pending', 'fake-task', 1, 4, 'digest')},
                          preview=PreviewRef(str(uuid4()), 'a' * 64, 20, 30))
        # Pending completion locks are retired on decode; everything else round-trips.
        decoded = decode_group(plain(self.group(updated))).entries[0]
        self.assertEqual(decoded, replace(updated, pending={}))
        self.assertEqual(content_digest(original), content_digest(updated))
        for values in ({'nl': 'other'}, {'variants': {'night': 'dark'}}, {'disable': True}, {'aspect': 'square'}):
            self.assertNotEqual(content_digest(original), content_digest(replace(original, **values)))
        with self.assertRaises(TypeError):
            updated.edit_versions.variants['night'] = 10

    def test_reject_invalid_and_removed_internal_state(self):
        for values in ({'generated': {'tag': True}}, {'generated': {'nl': 1}},
                       {'edit_versions': {'expand_nl': 1}}, {'edit_versions': {'nl': True}},
                       {'edit_versions': {'nl': -1}}, {'pending': {'tag': {}}},
                       {'pending': {'nl': {}}}):
            with self.subTest(values=values), self.assertRaises(PromptError):
                replace(self.entry(), **values)

    def test_decode_strict_fields_and_metadata(self):
        raw = plain(self.group(self.entry()))
        for change in ({'schema_version': 6}, {'schema_version': True}, {'prompt_format': 'anima'},
                       {'normalization_version': 'unknown'}, {'entries': None}, {'oops': 1}):
            with self.subTest(change=change), self.assertRaises(PromptError):
                decode_group(raw | change)
        for row in ({'nl': 'missing id'}, {'entry_id': self.entry_id, 'nl': 'x', 'tag': []},
                    {'entry_id': self.entry_id, 'nl': 'x', 'completion': []},
                    {'entry_id': self.entry_id, 'nl': 'x', 'preview': {}}):
            with self.subTest(row=row), self.assertRaises(PromptError):
                decode_group(raw | {'entries': [row]})

    def test_legacy_read_mapping(self):
        entry = self.entry(nl='forest')
        mapped = legacy_dictionary(self.group(entry, replace(entry, entry_id=str(uuid4()), disable=True)))
        self.assertEqual(len(mapped.entries), 1)
        self.assertEqual(mapped.entries[0].tags, ())
        self.assertEqual(mapped.entries[0].natural_language, entry.nl)

    def test_store_accepts_v5_after_t5_cutover(self):
        from stylelora.prompts.groups import GroupRef
        from stylelora.prompts.group_store import GroupStore
        with tempfile.TemporaryDirectory() as directory:
            store = GroupStore(directory)
            ref = GroupRef('global', None, self.identity)
            group = self.group(self.entry())
            self.assertEqual(store.save(ref, group, 0, 'v5'), 1)
            self.assertEqual(store.load(ref).entries, group.entries)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.entry_id, self.identity = str(uuid4()), str(uuid4())
        self.row = {'entry_id': self.entry_id, 'tag': ['blue_cat', 'cat'],
                    'expand_nl': ' description ', 'nl': ' description '}
        self.raw = {'schema_version': 4, 'dictionary_id': self.identity, 'name': 'old',
                    'kind': 'prompts', 'scope': 'project', 'nsfw': True, 'revision': 7,
                    'auto_complete': False, 'entries': [self.row], '_operations': {'old': {'result': 7}}}

    def test_nl_only_from_legacy_nl_and_tag_expand_become_variants(self):
        # tag/expand_nl never merge into nl; they keep their original variant keys.
        self.assertEqual(merge_legacy_content(self.row), ' description ')
        entry = migrate_v4(self.raw).entries[0]
        self.assertEqual(entry.nl, ' description ')
        self.assertEqual(dict(entry.variants), {'tag': 'blue cat, cat', 'expand_nl': ' description '})
        self.assertEqual(legacy_content_issues(self.row)[0].code, 'migration:possible_duplicate')
        self.assertEqual(entry.entry_id, self.entry_id)
        self.assertNotIn('tag', plain(entry))
        self.assertNotIn('expand_nl', plain(entry))

    def test_tag_and_expand_nl_nl_all_present(self):
        # All three legacy fields present: nl keeps only the old nl, others are variants.
        self.row.update(generated={'tag': True, 'expand_nl': True, 'nl': True},
                        edit_versions={'tag': 4, 'expand_nl': 2, 'nl': 6})
        row = self.row | {'expand_nl': ' Window ', 'nl': 'A cat sits.'}
        issues = []
        result = adapt_legacy_entry(row, issues=issues)
        self.assertEqual(result['nl'], 'A cat sits.')
        self.assertEqual(result['variants'], {'tag': 'blue cat, cat', 'expand_nl': ' Window '})
        self.assertEqual(result['generated']['nl'], True)
        self.assertEqual(result['generated']['variants'], {'tag': True, 'expand_nl': True})
        self.assertEqual(result['edit_versions'], {'nl': 7, 'variants': {}})
        self.assertFalse(issues)

    def test_only_tag(self):
        # Only tag present: it fills nl (fallback) but is still kept as a variant.
        row = {'entry_id': self.entry_id, 'tag': ['blue_cat', 'cat']}
        issues = []
        result = adapt_legacy_entry(row, issues=issues)
        self.assertEqual(result['nl'], 'blue cat, cat')
        self.assertEqual(result['variants'], {'tag': 'blue cat, cat'})
        self.assertEqual([i.code for i in issues], ['migration:nl_fallback'])
        self.assertIn('source=tag', issues[0].message)
        self.assertEqual(migrate_v4(self.raw | {'entries': [row]}).entries[0].nl, 'blue cat, cat')

    def test_existing_variant_key_conflict_keeps_existing(self):
        # Variant text goes through normalize_prompt, so underscores become spaces.
        self.row.update(variants={'tag': 'kept_tag', 'expand_nl': 'kept_expand'})
        issues = []
        result = adapt_legacy_entry(self.row, issues=issues)
        self.assertEqual(result['nl'], ' description ')
        self.assertEqual(result['variants'], {'tag': 'kept tag', 'expand_nl': 'kept expand'})
        self.assertEqual([i.code for i in issues],
                         ['migration:variant_conflict', 'migration:variant_conflict'])
        self.assertEqual({i.field for i in issues}, {self.entry_id})
        self.assertEqual(variant_issues(self.row)[0].code, 'migration:variant_conflict')
        # A conflict is reported but never overwrites the existing value.
        self.assertEqual(migrate_v4(self.raw).entries[0].variants['tag'], 'kept tag')

    def test_variant_limit_warns_and_keeps_nl(self):
        self.row.update(tag=['new_tag'], expand_nl='new_expand', nl='A cat sits.',
                        variants={str(i): 'text' for i in range(63)})
        issues = []
        result = adapt_legacy_entry(self.row, issues=issues)
        self.assertEqual(result['nl'], 'A cat sits.')
        self.assertEqual(len(result['variants']), 64)
        self.assertIn('tag', result['variants'])
        self.assertNotIn('expand_nl', result['variants'])
        # Exactly at the limit the legacy keys still fit: no drop, no warning.
        self.row.update(variants={str(i): 'text' for i in range(62)})
        result = adapt_legacy_entry(self.row, issues=issues)
        self.assertEqual(len(result['variants']), 64)
        self.assertIn('expand_nl', result['variants'])
        self.assertEqual([i.code for i in issues], ['migration:variant_limit'])
        self.assertIn('limit=64', issues[0].message)
        self.assertEqual(migrate_v4(self.raw).entries[0].nl, 'A cat sits.')

    def test_partial_empty_vectors(self):
        cases = [({'tag': [], 'expand_nl': ' \n', 'nl': ' n '}, ' n ', {}),
                 ({'tag': ['z', 'a', 'z'], 'expand_nl': '', 'nl': ''}, 'z, a, z', {'tag': 'z, a, z'}),
                 ({'tag': [], 'expand_nl': ' e ', 'nl': ''}, ' e ', {'expand_nl': ' e '})]
        for row, expected, variants in cases:
            with self.subTest(row=row):
                result = adapt_legacy_entry(row | {'entry_id': self.entry_id})
                self.assertEqual(result['nl'], expected)
                self.assertEqual(result['variants'], variants)
        with self.assertRaises(PromptError):
            adapt_legacy_entry({'entry_id': self.entry_id, 'tag': [], 'nl': ' '})

    def test_nl_fallback_prefers_expand_nl_and_keeps_source_key_marks(self):
        # Empty nl, distinct tag/expand_nl, both generated: nl takes expand_nl and
        # generated.nl follows the source *field*, not its text.
        row = {'entry_id': self.entry_id, 'tag': ['blue_cat'], 'expand_nl': 'A blue cat.',
               'nl': '', 'generated': {'tag': True, 'expand_nl': True, 'nl': False}}
        issues = []
        result = adapt_legacy_entry(row, issues=issues)
        self.assertEqual(result['nl'], 'A blue cat.')
        self.assertEqual(result['variants'], {'tag': 'blue cat', 'expand_nl': 'A blue cat.'})
        self.assertEqual(result['generated'], {'nl': True, 'variants': {'tag': True, 'expand_nl': True}})
        self.assertEqual([i.code for i in issues], ['migration:nl_fallback'])
        self.assertIn('source=expand_nl', issues[0].message)
        self.assertNotIn('A blue cat.', issues[0].message)

    def test_anima_conversion_in_base_and_variants(self):
        self.row.update(tag=['(blue_cat:1.3)'], expand_nl='', nl='@foo_bar',
                        variants={'key_unchanged': '(dark_night:1.3)', 'empty': '', 'space': ' \r\n'})
        entry = migrate_v4(self.raw).entries[0]
        self.assertEqual(entry.nl, 'artist:foo bar')
        self.assertEqual(dict(entry.variants), {'key_unchanged': '1.5::dark night::', 'empty': '',
                                                'space': ' \n', 'tag': '1.5::blue cat::'})

    def test_generated_contributors_and_versions(self):
        self.row.update(generated={'tag': True, 'expand_nl': True, 'nl': True,
                                   'variants': {'night': True, 'expand_nl': False}},
                        edit_versions={'tag': 8, 'expand_nl': 3, 'nl': 5, 'variants': {'night': 4, 'deleted': 9}},
                        variants={'night': ''})
        entry = migrate_v4(self.raw).entries[0]
        self.assertTrue(entry.generated.nl)
        # tag/expand_nl generation marks map to the variant they produced; nl from old nl.
        self.assertEqual(dict(entry.generated.variants),
                         {'night': True, 'expand_nl': True, 'tag': True})
        self.assertEqual(entry.edit_versions.nl, 9)
        self.assertEqual(dict(entry.edit_versions.variants), {'night': 4, 'deleted': 9})
        self.row.update(generated={'nl': True})
        self.assertTrue(adapt_legacy_entry(self.row)['generated']['nl'])
        self.row.update(generated={'tag': True, 'expand_nl': True, 'nl': False})
        self.assertFalse(adapt_legacy_entry(self.row)['generated']['nl'])

    def test_preserves_group_preview_and_clears_old_locks(self):
        self.row.update(disable=True, aspect='portrait',
                        preview=plain(PreviewRef(str(uuid4()), 'a' * 64, 12, 15)),
                        pending={'tag': {'old': 'discard'}, 'nl': {'old': 'discard'}},
                        completion={'state': 'running', 'missing': ['tag'], 'task_id': 'old'})
        group = migrate_v4(self.raw)
        entry = group.entries[0]
        self.assertEqual((group.dictionary_id, group.scope, group.nsfw, group.revision),
                         (self.identity, 'project', True, 8))
        self.assertEqual(plain(group._operations), self.raw['_operations'])
        self.assertEqual(plain(entry.preview), self.row['preview'])
        self.assertEqual((entry.disable, entry.aspect), (True, 'portrait'))
        self.assertEqual(dict(entry.pending), {})
        self.assertFalse(hasattr(entry, 'completion'))

    def test_pure_deterministic_and_v5_reread_idempotent(self):
        before = copy.deepcopy(self.raw)
        first = migrate_v4(self.raw)
        self.assertEqual(self.raw, before)
        self.assertEqual(migrate_v4(self.raw), first)
        self.assertEqual(migrate_group(plain(first)), first)
        with self.assertRaises(TypeError):
            first._operations['old']['result'] = 100

    def test_v1_v2_direct_migration_stable_uuid_and_single_revision(self):
        for version in (1, 2):
            raw = {'schema_version': version, 'dictionary_id': self.identity, 'name': 'old',
                   'kind': 'prompts', 'revision': 7, 'entries': [
                       {'tags': ['cat', 'cat'], 'natural_language': ' forest ', 'rating': 'explicit'}]}
            with self.subTest(version=version):
                result = migrate_group(raw)
                self.assertEqual((result.schema_version, result.revision, result.nsfw), (5, 8, True))
                # natural_language is the old nl; tags become the "tag" variant.
                self.assertEqual(result.entries[0].nl, ' forest ')
                self.assertEqual(dict(result.entries[0].variants),
                                 {'tag': 'cat, cat', 'expand_nl': ' forest '})
                self.assertEqual(result.entries[0].entry_id, migrate_v2(raw).entries[0].entry_id)
                self.assertEqual(result.entries[0].edit_versions.nl, 1)
                self.assertFalse(result.entries[0].generated.nl)

    def test_v3_direct_migration_with_defaults(self):
        raw = self.raw | {'schema_version': 3}
        raw.pop('auto_complete')
        result = migrate_v3(raw)
        self.assertEqual((result.schema_version, result.revision), (5, 8))
        self.assertEqual(result.entries[0].entry_id, self.entry_id)
        self.assertEqual(result.entries[0].edit_versions.nl, 1)

    def test_long_nl_is_not_merged_with_tag_expand(self):
        # 8000 + 8000 no longer overflows because tag/expand_nl stay out of nl.
        self.row.update(tag=[], expand_nl='a' * 8000, nl='b' * 8000)
        entry = migrate_v4(self.raw).entries[0]
        self.assertEqual(entry.nl, 'b' * 8000)
        self.assertEqual(entry.variants['expand_nl'], 'a' * 8000)

    def test_overlong_nl_reports_identity_length_without_io(self):
        self.row.update(tag=[], expand_nl='a' * 8000, nl='b' * 16001)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.json'
            original = json.dumps(self.raw).encode()
            path.write_bytes(original)
            with self.assertRaises(PromptError) as caught:
                migrate_v4(json.loads(path.read_bytes()))
            issue = caught.exception.issues[0]
            self.assertEqual(issue.field, 'nl')
            self.assertIn(self.entry_id, issue.message)
            self.assertIn('length=16001', issue.message)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_normalized_length_limit_and_variant_length(self):
        self.row.update(tag=[], expand_nl='', nl='x' * 16000)
        self.assertEqual(len(migrate_v4(self.raw).entries[0].nl), 16000)
        self.row['variants'] = {'long': 'y' * 16001}
        with self.assertRaises(PromptError) as caught:
            migrate_v4(self.raw)
        self.assertEqual(caught.exception.issues[0].field, "variants['long']")

    def test_normalizer_limits_include_entry_diagnostics(self):
        self.row.update(tag=[], expand_nl='', nl='x' * 1000001)
        with self.assertRaises(PromptError) as caught:
            migrate_v4(self.raw)
        self.assertEqual(caught.exception.code, 'import:limit')
        self.assertIn(self.entry_id, caught.exception.issues[0].message)
        self.assertIn('source_length=1000001', caught.exception.issues[0].message)

    def test_old_v5_pending_lock_is_dropped_on_read(self):
        # The completion service is gone: a persisted pending lock would stick forever.
        raw = plain(migrate_v4(self.raw))
        raw['entries'][0]['pending'] = {'nl': {'state': 'pending', 'task_id': 'old-task',
                                               'attempt': 1, 'edit_version': 0,
                                               'input_digest': 'd'}}
        group = decode_group(raw)
        self.assertEqual(dict(group.entries[0].pending), {})

    def test_retired_fields_are_tolerated_and_dropped(self):
        # Old v5 data may still carry auto_complete and entry completion state.
        raw = plain(migrate_v4(self.raw))
        raw['auto_complete'] = True
        raw['entries'][0]['completion'] = {'state': 'pending', 'missing': ['nl'], 'task_id': 'old'}
        group = decode_group(raw)
        self.assertFalse(hasattr(group, 'auto_complete'))
        self.assertFalse(hasattr(group.entries[0], 'completion'))
        self.assertEqual(group.entries[0].nl, ' description ')
        # A malformed retired blob is still rejected rather than silently accepted.
        for value in ([], {'state': 'unknown'}, {'missing': ['tag']}):
            with self.subTest(value=value), self.assertRaises(PromptError):
                decode_group(raw | {'entries': [raw['entries'][0] | {'completion': value}]})

    def test_versions_and_malformed_legacy_rejected(self):
        for version in (None, True, 0, 6, '4'):
            with self.subTest(version=version), self.assertRaises(PromptError) as caught:
                legacy_version({'schema_version': version})
            self.assertEqual(caught.exception.code, 'newer_version')
        for values in ({'kind': 'features'}, {'entries': None}, {'revision': True},
                       {'entries': [self.row | {'tag': 'cat'}]},
                       {'entries': [self.row | {'generated': {'nl': 1}}]},
                       {'entries': [self.row | {'edit_versions': {'nl': -1}}]},
                       {'entries': [self.row | {'nl': '(unclosed'}]}):
            with self.subTest(values=values), self.assertRaises(PromptError):
                migrate_group(self.raw | values)


class RenderTests(unittest.TestCase):
    def entry(self, text, **values):
        return validate_entry({'nl': text, **values})

    def group(self, *entries):
        return Group(str(uuid4()), 'sample', entries=entries)

    def test_render_nl_and_deprecated_aliases(self):
        entry = self.entry(' 1.5::cat::\n\ntext ', variants={'unused': 'other'})
        self.assertEqual(render(entry).text, entry.nl)
        self.assertFalse(render(entry).warnings)
        for mode in ('tags_plus_nl', 'full_nl'):
            result = render(entry, mode)
            self.assertEqual((result.text, result.requested_mode, result.effective_mode), (entry.nl, mode, 'nl'))
            self.assertEqual(result.warnings[0].code, 'render:deprecated_mode')
        with self.assertRaises(PromptError):
            render(entry, 'bad')

    def test_sampling_deduplicates_canonical_text_and_excludes_disabled(self):
        group = self.group(self.entry('{blue_cat}'), self.entry('1.05::blue cat::'),
                           self.entry('different', disable=True))
        frozen, warnings = freeze_groups((group,), 'nl', 3, 1, 'square')
        self.assertEqual((frozen.count, frozen.items[0].prompt, frozen.items[0].aspect), (1, '{blue_cat}', 'square'))
        self.assertFalse(warnings)
        with self.assertRaises(PromptError) as caught:
            freeze_groups((group,), 'nl', 3, 2)
        self.assertEqual(caught.exception.code, 'source:insufficient')
        self.assertIn('available=1; deficit=1', caught.exception.issues[0].message)

    def test_sampling_keeps_order_and_whitespace_semantics(self):
        texts = ('cat, dog', 'dog, cat', 'cat\n\ndog', 'cat dog', 'cat, cat')
        group = self.group(*(self.entry(text) for text in texts))
        frozen, _ = freeze_groups((group,), 'nl', 12, len(texts))
        self.assertEqual({item.prompt for item in frozen.items}, set(texts))

    def test_freeze_determinism_digest_and_snapshot_independence(self):
        group = self.group(self.entry('cat', aspect='portrait'), self.entry('dog'))
        first, warnings = freeze_groups((group,), 'full_nl', 12, 2, 'square')
        self.assertEqual(freeze_groups((group,), 'full_nl', 12, 2, 'square'), (first, warnings))
        self.assertEqual(len(warnings), 2)
        self.assertEqual({item.prompt: item.aspect for item in first.items}, {'cat': 'portrait', 'dog': 'square'})
        changed = replace(group, entries=(self.entry('other'),))
        next_frozen, _ = freeze_groups((changed,), 'nl', 12, 1)
        self.assertNotEqual(first.digest, next_frozen.digest)
        self.assertEqual({item.prompt for item in first.items}, {'cat', 'dog'})
        for seed, count in ((True, 1), (1, True), (1, -1)):
            with self.assertRaises(PromptError):
                freeze_groups((group,), 'nl', seed, count)
        self.assertEqual(freeze_groups((), 'nl', 1, 0)[0].count, 0)
        with self.assertRaises(PromptError):
            freeze_groups((), 'bad', 1, 0)


if __name__ == '__main__':
    unittest.main()
