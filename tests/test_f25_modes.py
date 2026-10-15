import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from contextlib import closing
from unittest.mock import patch
from uuid import uuid4

from stylelora.project_db.project import create_project, open_project, close_project, update_metadata
from stylelora.project_db import migrations
from stylelora.project_db.errors import Corrupt, NewerProject, VersionMismatch
from stylelora.modes.profiles import get_profile, freeze_profile, resolve_profile, ProfileError, digest
from stylelora.modes.statistics import ImageSummary, summarize
from stylelora.prompts.types import PromptDictionary, FeatureDictionary, PromptEntry, PromptError, plain
from stylelora.prompts import dictionaries as store
from stylelora.prompts.formatters import FormatContext, FormatResult, IDENTITY, apply_formatter, input_digest


class ProjectModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'mode.slproj'
        self.handle = create_project(self.root, name='mode', trigger_word='trigger')
        self.addCleanup(lambda: close_project(self.handle))

    def legacy(self):
        close_project(self.handle)
        raw = json.loads((self.root/'project.json').read_text())
        raw.pop('mode')
        raw['format_version'] = 1
        migrations.atomic_json(self.root/'project.json', raw)
        with closing(sqlite3.connect(self.root/'project.db')) as db:
            db.execute('PRAGMA user_version=1')

    def test_T01_new_and_legacy(self):
        raw = json.loads((self.root/'project.json').read_text())
        self.assertEqual((raw['mode'], raw['format_version']), ('style', 2))
        self.legacy()
        with self.assertRaisesRegex(VersionMismatch, 'Migration required'):
            open_project(self.root, migrate=False)
        self.handle = open_project(self.root)
        self.assertEqual(self.handle.metadata.mode, 'style')
        self.assertEqual(self.handle.schema_version, 2)
        with closing(sqlite3.connect(self.root/'project.db')) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)

    def test_T01_invalid_metadata_unchanged(self):
        close_project(self.handle)
        original = json.loads((self.root/'project.json').read_text())
        missing = dict(original)
        missing.pop('mode')
        for raw, error in ((missing, Corrupt), ({**original, 'format_version': 3}, NewerProject),
                           ({**original, 'mode': 'alien'}, ProfileError),
                           ({**original, 'mode': 'character'}, ProfileError)):
            with self.subTest(raw=raw):
                migrations.atomic_json(self.root/'project.json', raw)
                before = (self.root/'project.json').read_bytes()
                with self.assertRaises(error):
                    open_project(self.root)
                self.assertEqual(before, (self.root/'project.json').read_bytes())

    def test_T01_reserved_creation_does_not_publish(self):
        close_project(self.handle)
        for mode in ('character', 'action', 'object'):
            path = self.root.with_name(mode+'.slproj')
            with self.assertRaisesRegex(ProfileError, 'mode:unsupported'):
                create_project(path, name='reserved', trigger_word='', mode=mode)
            self.assertFalse(path.exists())

    def test_T02_interrupted_publish_backup_and_noop(self):
        self.legacy()
        original = (self.root/'project.json').read_bytes()
        atomic = migrations.atomic_json
        def fail(path, value):
            if path.name == 'project.json':
                raise OSError('injected publication failure')
            atomic(path, value)
        with patch.object(migrations, 'atomic_json', fail), self.assertRaises(OSError):
            open_project(self.root)
        backups = tuple((self.root/'.migration-backup').glob('*/project.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.handle = open_project(self.root)
        close_project(self.handle)
        before = (self.root/'project.json').stat().st_mtime_ns
        migrations.migrate_project(self.root, target_version=2)
        self.assertEqual((self.root/'project.json').stat().st_mtime_ns, before)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertFalse((self.root/'.migration-state.json').exists())

    def test_T06_metadata_preserved(self):
        old = self.handle.metadata
        value = update_metadata(self.handle, name='renamed', trigger_word='new')
        self.assertEqual((value.mode, value.format_version, value.created_at),
                         (old.mode, old.format_version, old.created_at))


class ProfileTests(unittest.TestCase):
    def test_T04_profile_guard_exceptions_and_QC(self):
        from stylelora.caption.blocklist import build_blocklist, check_blocklist
        from stylelora.tagging.catalog import LocalCatalog
        from stylelora.project_db.types import Tag
        from stylelora.caption.types import CaptionResult
        from stylelora.modes.qc import evaluate
        guard = build_blocklist('watercolor', LocalCatalog(), profile=get_profile())
        hits = check_blocklist((Tag('watercolor'), Tag('gradient hair')), 'Painted in watercolor.', guard)
        self.assertTrue(any(h.tag == 'watercolor' for h in hits))
        self.assertFalse(any(h.tag == 'gradient hair' for h in hits))
        self.assertTrue(any(h.nl_span is not None and h.certainty == 'review_required' for h in hits))
        value = CaptionResult((Tag('trigger'),), None, ('trigger:blocklist_conflict',), (), True, 'preserve')
        self.assertEqual(evaluate(value, get_profile())[0].severity, 'error')

    def test_T03_freeze_resolve_and_disabled(self):
        profile = get_profile()
        frozen = freeze_profile(profile)
        self.assertEqual(frozen, freeze_profile(get_profile('style', 1)))
        self.assertEqual(resolve_profile(plain(frozen)), profile)
        from stylelora.prompts.validation import skill_text
        self.assertEqual(frozen.template_hashes['R1'], hashlib.sha256(skill_text().encode()).hexdigest())
        self.assertFalse(profile.templates['R3'].enabled)
        self.assertNotIn('R6', profile.templates)
        self.assertFalse(get_profile('character', executable=False).enabled)
        with self.assertRaisesRegex(ProfileError, 'profile:unavailable'):
            resolve_profile(replace(frozen, revision=99))
        with self.assertRaisesRegex(ProfileError, 'profile:unavailable'):
            resolve_profile(replace(frozen, digest='tampered'))

    def test_T05_statistics_snapshot(self):
        result = summarize((ImageSummary(1, content_dimensions={'subject': ('cat',)}, qc_codes=('blocklist:hit',)*3),
                            ImageSummary(2, 'general', 'approved', {'subject': ('cat', 'dog')})), get_profile())
        self.assertEqual(result.denominator, 2)
        self.assertEqual(result.groups['rating'], {'unknown': 1, 'general': 1})
        self.assertEqual(result.groups['review_status'], {'unknown': 1, 'approved': 1})
        self.assertEqual(result.coverage['subject'], {'cat': 2, 'dog': 1})
        self.assertEqual(result.leakage_images, 1)
        self.assertNotEqual(freeze_profile(get_profile()).digest,
                            freeze_profile(replace(get_profile(), qc_policy='changed')).digest)


class DictionaryV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.value = PromptDictionary(str(uuid4()), 'prompts', entries=(PromptEntry(('cat',)),))

    def test_T07_v1_v2_and_features(self):
        encoded = store._encode(self.value)
        self.assertEqual(encoded['schema_version'], 2)
        self.assertEqual(store._decode(encoded), self.value)
        encoded.pop('category')
        encoded['schema_version'] = 1
        self.assertEqual(store._decode(encoded).category, 'full_content')
        feature = FeatureDictionary(str(uuid4()), 'features')
        self.assertNotIn('category', store._encode(feature))
        with self.assertRaisesRegex(PromptError, 'dictionary:category_unsupported'):
            replace(self.value, category='character')
        with self.assertRaises(PromptError):
            store._decode({**store._encode(feature), 'category': 'full_content'})

    def test_T07_category_filter(self):
        store.save_dictionary(self.value, expected_revision=0, request_key='new', root=self.root)
        feature = FeatureDictionary(str(uuid4()), 'features')
        store.save_dictionary(feature, expected_revision=0, request_key='feature', root=self.root)
        self.assertEqual(len(store.list_dictionaries(root=self.root)), 2)
        self.assertEqual(len(store.list_dictionaries(root=self.root, category='full_content')), 1)
        with self.assertRaises(PromptError):
            store.list_dictionaries(root=self.root, kind='features', category='full_content')

    def test_T08_historical_save_replay_survives_upgrade(self):
        old = store._encode(self.value)
        old['schema_version'] = 1
        old.pop('category')
        signature = store.digest({'operation': 'save', 'value': old, 'expected_revision': 0})
        old['_operations'] = {'old': {'digest': signature, 'result': 1}}
        path = store._path(self.value.dictionary_id, 'prompts', self.root)
        store._write(path, old)
        before = path.read_bytes()
        self.assertEqual(store.save_dictionary(self.value, expected_revision=0, request_key='old', root=self.root), 1)
        self.assertEqual(path.read_bytes(), before)
        store.append_entries(self.value.dictionary_id, (PromptEntry(('dog',)),), expected_revision=1, request_key='append', root=self.root)
        self.assertEqual(store.save_dictionary(self.value, expected_revision=0, request_key='old', root=self.root), 1)
        self.assertEqual(json.loads(path.read_bytes())['_operations']['old']['digest'], signature)

    def test_T08_new_replay_conflict_and_atomic_failure(self):
        store.save_dictionary(self.value, expected_revision=0, request_key='new', root=self.root)
        path = store._path(self.value.dictionary_id, 'prompts', self.root)
        before = path.read_bytes()
        self.assertEqual(store.save_dictionary(self.value, expected_revision=0, request_key='new', root=self.root), 1)
        with self.assertRaisesRegex(PromptError, 'dictionary:request_key_conflict'):
            store.save_dictionary(replace(self.value, name='different'), expected_revision=0, request_key='new', root=self.root)
        with self.assertRaisesRegex(PromptError, 'dictionary:revision_conflict'):
            store.save_dictionary(self.value, expected_revision=0, request_key='other', root=self.root)
        with patch.object(store, '_write', side_effect=OSError), self.assertRaisesRegex(PromptError, 'storage:io_error'):
            store.save_dictionary(self.value, expected_revision=1, request_key='io', root=self.root)
        self.assertEqual(before, path.read_bytes())


class FormatterTests(unittest.TestCase):
    def context(self, **values):
        return FormatContext(freeze_profile(get_profile()), values, '1')

    def test_T09_identity_and_placeholders(self):
        entry = PromptEntry(('cat',))
        result = apply_formatter(entry, context=self.context())
        self.assertEqual(result.entry, entry)
        self.assertEqual(result, apply_formatter(entry, context=self.context()))
        with self.assertRaisesRegex(PromptError, 'formatter:unavailable'):
            apply_formatter(PromptEntry(('{{character_name}}',)), context=self.context())

    def plugin(self):
        class FakeFormatter:
            id, version = 'fake', '1'
            def format(self, entry, context):
                def render(value):
                    for name, replacement in context.values.items():
                        value = value.replace('{{'+name+'}}', replacement)
                    return value
                output = PromptEntry(tuple(render(tag) for tag in entry.tags),
                                     render(entry.natural_language or '') or None, entry.aspect, entry.rating)
                return FormatResult(output, self.id, self.version, input_digest(entry, self, context), digest(output))
        return FakeFormatter()

    def test_T09_fake_three_variables(self):
        entry = PromptEntry(('{{character_name}}', '{{appearance}}'), '{{nl_description}}')
        result = apply_formatter(entry, formatter=self.plugin(), context=self.context(
            character_name='cat', appearance='Blue_Eyes', nl_description='A cat sits.'))
        self.assertEqual(result.entry.tags, ('cat', 'blue eyes'))
        self.assertEqual(result.entry.natural_language, 'A cat sits.')

    def test_T10_missing_unknown_nested_invalid(self):
        for entry, context, code in (
            (PromptEntry(('{{appearance}}',)), self.context(), 'formatter:missing_variable'),
            (PromptEntry(('{{unknown}}',)), self.context(), 'formatter:unavailable'),
            (PromptEntry(('{{appearance}}',)), self.context(appearance='{{character_name}}'), 'formatter:unavailable'),
            (PromptEntry(('{{appearance}}',)), self.context(appearance='cat, dog'), 'composer:forbidden_content')):
            with self.subTest(code=code), self.assertRaisesRegex(PromptError, code):
                apply_formatter(entry, formatter=self.plugin(), context=context)

    def test_T11_digest_changes_with_context_version(self):
        entry = PromptEntry(('cat',))
        first = apply_formatter(entry, context=self.context())
        second = apply_formatter(entry, context=replace(self.context(), source_revision='2'))
        self.assertNotEqual(first.input_digest, second.input_digest)
        self.assertEqual(first.output_digest, second.output_digest)

    def test_T11_generation_bypass_rejects_placeholder_before_send(self):
        from stylelora.generation.profile import build_request
        from types import SimpleNamespace
        with self.assertRaisesRegex(PromptError, 'formatter:unavailable'):
            build_request(None, None, SimpleNamespace(prompt='{{character_name}}', item_id=1), Path('.'))
