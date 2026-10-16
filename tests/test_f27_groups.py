import io
import json
import shutil
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from PIL import Image
from stylelora.prompts.groups import *
from stylelora.prompts.group_store import GroupStore, prepare_preview
from stylelora.prompts.group_exchange import GroupExchange, parse_json, read_package
from stylelora.prompts.dictionaries import _write, load_dictionary, save_dictionary


def image_bytes(size=(64, 32)):
    stream = io.BytesIO()
    Image.new('RGB', size, 'red').save(stream, 'PNG')
    return stream.getvalue()


class GroupsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'StyleLoraAutom'
        self.current = None
        self.store = GroupStore(self.root, lambda: self.current)
        self.exchange = GroupExchange(self.store)
        self.ref = GroupRef('global', None, str(uuid4()))

    def group(self, entries=(), nsfw=False, ref=None):
        ref = ref or self.ref
        group = Group(ref.group_id, 'sample', ref.scope, nsfw, tuple(entries))
        self.store.save(ref, group, 0, 'create')
        return self.store.load(ref)

    def error(self, code, call):
        with self.assertRaises(PromptError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def preview(self, rows, **options):
        return self.exchange.preview_import(json.dumps({'prompts': rows}).encode(), options)

    def test_T01_content_and_missing(self):
        self.error('schema:invalid', lambda: validate_entry({'tag': [], 'nl': ' '}))
        for field, value in [('tag', ['cat']), ('nl', 'A cat.'), ('expand_nl', 'A cat.')]:
            entry = validate_entry({field: value})
            self.assertEqual(entry.completion.state, 'pending')
            if field == 'expand_nl':
                self.assertEqual(entry.nl, entry.expand_nl)
                self.assertEqual(entry.completion.missing, ('tag',))

    def test_T02_field_issues(self):
        with self.assertRaises(PromptError) as caught:
            validate_entry({'tag':['cat'], 'disable':'false', 'aspect':'wide', 'oops':1})
        self.assertEqual({i.field for i in caught.exception.issues}, {'disable', 'aspect', 'oops'})
        self.assertEqual(validate_entry({'tag':['Cat', 'cat']}).tag, ('cat',))

    def test_T03_migrate_and_readonly_adapter(self):
        raw = dict(schema_version=2, kind='prompts', dictionary_id=self.ref.group_id, name='legacy', revision=7,
                   entries=[dict(tags=['cat'], natural_language='Sitting.', rating='explicit', aspect='square')])
        _write(self.store.path(self.ref), raw)
        first = self.store.load(self.ref); second = self.store.load(self.ref)
        self.assertEqual(first, second)
        self.assertEqual(first.revision, 8); self.assertTrue(first.nsfw)
        self.assertEqual(first.entries[0].nl, '')
        self.assertEqual(first.entries[0].expand_nl, 'Sitting.')
        self.assertEqual(len(list(self.store.path(self.ref).parent.glob('*.bak'))), 1)
        old = load_dictionary(self.ref.group_id, root=self.root.parent)
        self.assertIsNone(old.entries[0].rating)
        self.error('dictionary:legacy_read_only', lambda: save_dictionary(old, expected_revision=8, request_key='legacy', root=self.root.parent))

    def test_T04_cas_preserves_snapshot(self):
        group = self.group([validate_entry({'nl':'A cat.'})])
        self.error('dictionary:revision_conflict', lambda: self.store.save(self.ref, replace(group, name='changed'), 0, 'stale'))
        self.assertEqual(self.store.load(self.ref), group)

    def test_T05_replay_before_cas(self):
        group = Group(self.ref.group_id, 'sample')
        self.assertEqual(self.store.save(self.ref, group, 0, 'same'), 1)
        self.assertEqual(self.store.save(self.ref, group, 0, 'same'), 1)
        self.error('dictionary:request_key_conflict', lambda: self.store.save(self.ref, replace(group, name='other'), 0, 'same'))

    def test_T06_atomic_failure_and_orphans(self):
        entry = validate_entry({'nl':'A cat.'}); group = self.group([entry])
        asset = self.store.publish(prepare_preview(image_bytes(), entry.entry_id), self.ref, entry.entry_id)
        with patch('stylelora.prompts.dictionaries.os.replace', side_effect=OSError):
            self.error('storage:io_error', lambda: self.store.save(self.ref, replace(group, entries=(replace(entry, preview=asset),)), 1, 'fail'))
        self.assertIsNone(self.store.load(self.ref).entries[0].preview)
        self.store.collect_orphans(self.ref, 0)
        self.assertFalse((self.store.root(self.ref)/'previews'/self.ref.group_id/asset.asset_revision).exists())

    def test_T06_orphan_before_new_group_commit(self):
        identity = str(uuid4())
        asset = self.store.publish(prepare_preview(image_bytes(),identity),self.ref,identity)
        self.store.collect_orphans(self.ref,0)
        self.assertFalse((self.store.root(self.ref)/'previews'/self.ref.group_id/asset.asset_revision).exists())

    def test_T07_tombstone_replay_and_lease(self):
        entry = validate_entry({'nl':'A cat.'})
        asset = self.store.publish(prepare_preview(image_bytes(), entry.entry_id), self.ref, entry.entry_id)
        self.group([replace(entry, preview=asset)])
        with self.store.lease(self.ref) as group:
            first = self.store.delete(self.ref, 1, 'delete')
            self.assertEqual(first, self.store.delete(self.ref, 1, 'delete'))
            self.store.collect_orphans(self.ref, 0)
            self.assertTrue(self.store.image(self.ref, group.entries[0])[0])
        self.store.collect_orphans(self.ref, 0)
        self.assertEqual(self.store.list()[0], ())

    def test_T10_scope_merge_and_old_token(self):
        self.group(nsfw=True)
        self.current = ('p1', self.root.parent/'project')
        project = GroupRef('project', 'p1', self.ref.group_id)
        self.group(ref=project)
        groups, _ = self.store.list()
        self.assertEqual([g.scope for g in groups], ['project','global'])
        self.current = None
        self.error('project:closed', lambda: self.store.delete(project, 1, 'closed'))
        self.assertEqual(len(self.store.list()[0]), 1)
        self.assertTrue(self.store.load(self.ref).nsfw)

    def test_T11_json_folder_zip_preview_confirm(self):
        preview = self.preview([{'nl':'A cat.'}])
        self.assertEqual(self.store.list()[0], ())
        result = self.exchange.commit_import(preview['preview_id'], preview['digest'], 0, 'import')
        ref = GroupRef(**result['ref'])
        package = self.exchange.export_group(ref, 'zip')
        zipped = self.exchange.preview_import(package, {'format':'zip'})
        self.assertEqual(zipped['valid_count'], 1)
        folder = self.root.parent/'folder'; folder.mkdir()
        for name, data in read_package(package).items():
            target = folder/name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
        imported = self.exchange.preview_import(folder, {'format':'folder'})
        self.assertEqual(imported['entries'][0]['nl'], 'A cat.')
        changed = self.preview([{'nl':'A cat.'}], metadata={'name':'other'})
        self.assertNotEqual(changed['digest'], preview['digest'])

    def test_T12_mixed_rows_and_canonical_key(self):
        preview = self.preview([{'nl':'A cat.'}, {'disable':'false'}, {'nl':None}])
        self.assertEqual(preview['valid_count'], 1); self.assertEqual(preview['skipped_count'], 2)
        self.assertEqual({i['row'] for i in preview['issues']}, {2,3})
        self.error('import:empty', lambda: self.preview([{}]))
        for raw in ({'prompt':[{'nl':'x'}]}, {'prompts':[{'nl':'x'}], 'promprt':[]}):
            self.error('schema:invalid', lambda: self.exchange.preview_import(json.dumps(raw).encode()))

    def test_T13_conflicting_id_frozen_and_duplicate_commit(self):
        self.group()
        preview = self.preview([{'nl':'A cat.'}], metadata={'group_id': self.ref.group_id})
        self.assertNotEqual(preview['ref']['group_id'], self.ref.group_id)
        first = self.exchange.commit_import(preview['preview_id'], preview['digest'], 0, 'same')
        self.assertEqual(first, self.exchange.commit_import(preview['preview_id'], preview['digest'], 0, 'same'))
        self.assertEqual(len(self.store.list()[0]), 2)

    def test_T14_append_failure_is_atomic(self):
        entry = validate_entry({'nl':'A cat.'}); group = self.group([entry])
        preview = self.exchange.preview_import(b'{"nl":"A dog."}', {'level':'entries', 'ref':plain(self.ref)})
        self.store.save(self.ref, replace(group, name='edited'), 1, 'edit')
        self.error('dictionary:revision_conflict', lambda: self.exchange.commit_import(preview['preview_id'], preview['digest'], 1, 'confirm'))
        self.assertEqual(len(self.store.load(self.ref).entries), 1)

    def test_T14_image_publication_failure_keeps_append_target(self):
        source = validate_entry({'nl':'A cat.'})
        image = self.store.publish(prepare_preview(image_bytes(), source.entry_id), self.ref, source.entry_id)
        self.group([replace(source, preview=image)])
        package = self.exchange.export_group(self.ref, 'zip')
        other = GroupRef('global',None,str(uuid4()))
        original = self.group([validate_entry({'nl':'A dog.'})],ref=other)
        preview = self.exchange.preview_import(package,{'format':'zip','level':'entries','ref':plain(other)})
        with patch.object(self.store,'publish',side_effect=PromptError('storage:io_error')):
            self.error('storage:io_error',lambda: self.exchange.commit_import(preview['preview_id'],preview['digest'],1,'failed'))
        self.assertEqual(self.store.load(other),original)

    def test_T15_unsafe_archive(self):
        for names in [['../bad'], ['/bad'], ['C:/bad'], ['previews/a:b'], ['CON.txt'], ['group.json','GROUP.JSON'], ['extra.txt']]:
            with self.subTest(names=names):
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, 'w') as archive:
                    for name in names:
                        archive.writestr(name, '{"prompts":[{"nl":"A cat."}]}')
                self.error('import:unsafe_archive' if names != ['extra.txt'] else 'schema:invalid',
                           lambda: self.exchange.preview_import(stream.getvalue(), {'format':'zip'}))
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            info = zipfile.ZipInfo('group.json'); info.external_attr = (0o120777 << 16)
            archive.writestr(info, '{}')
        self.error('import:unsafe_archive', lambda: read_package(stream.getvalue()))

    def test_T16_bomb_limit_and_truncation(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('group.json', ' '*200000)
        self.error('import:limit', lambda: read_package(stream.getvalue()))
        self.error('import:unsafe_archive', lambda: read_package(stream.getvalue()[:-20]))
        with patch('stylelora.prompts.group_exchange.EXPANDED_LIMIT', 5):
            plain_zip = io.BytesIO()
            with zipfile.ZipFile(plain_zip, 'w') as archive:
                archive.writestr('group.json', '{"prompts":[]}')
            self.error('import:limit', lambda: read_package(plain_zip.getvalue()))
        self.assertEqual(self.store.list()[0], ())

    def test_T17_encoding_duplicate_keys_constants(self):
        self.assertEqual(parse_json(b'\xef\xbb\xbf{}'), {})
        self.error('import:encoding', lambda: parse_json(b'\xff'))
        for raw in [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}']:
            self.error('schema:invalid', lambda: parse_json(raw))

    def test_T18_export_disabled_json_no_internal_fields_and_manifest(self):
        entry = validate_entry({'tag':['cat'], 'disable':True})
        preview = self.store.publish(prepare_preview(image_bytes(), entry.entry_id), self.ref, entry.entry_id)
        self.group([replace(entry, preview=preview)], nsfw=True)
        raw = parse_json(self.exchange.export_group(self.ref))
        self.assertEqual(set(raw['prompts'][0]), CONTENT)
        self.assertTrue(raw['prompts'][0]['disable'])
        self.assertEqual(raw['prompts'][0]['nl'], '')
        package = read_package(self.exchange.export_group(self.ref, 'zip'))
        manifest = parse_json(package['group.json'])['preview_manifest'][entry.entry_id]
        self.assertEqual(manifest['sha256'], preview.sha256)
        result = self.exchange.preview_import(self.exchange.export_group(self.ref, 'zip'), {'format':'zip'})
        self.assertEqual(result['valid_count'], 1)

    def test_T19_images_limits_and_thumbnails(self):
        identity = str(uuid4())
        self.error('image:invalid', lambda: prepare_preview(b'garbage', identity))
        with patch('stylelora.prompts.group_store.IMAGE_LIMIT', 1):
            self.error('image:too_large', lambda: prepare_preview(image_bytes(), identity))
        with patch('stylelora.prompts.group_store.PIXEL_LIMIT', 1):
            self.error('image:too_large', lambda: prepare_preview(image_bytes(), identity))
        asset = prepare_preview(image_bytes((3000, 1500)), identity)
        self.assertEqual((asset.width, asset.height), (2048,1024))
        self.assertEqual(Image.open(io.BytesIO(asset.thumb)).size, (320,160))
        self.assertFalse(Image.open(io.BytesIO(asset.full)).getexif())

    def test_T22_seven_missing_combinations(self):
        keys = ['tag','expand_nl','nl']; values = [['cat'], 'Sitting.', 'A cat sits.']
        for mask in range(1,8):
            raw = {k:v for bit,(k,v) in enumerate(zip(keys,values)) if mask & (1<<bit)}
            entry = validate_entry(raw)
            missing = tuple(k for k in keys if k not in raw and not (mask == 2 and k == 'nl'))
            self.assertEqual(entry.completion.missing, missing)

    def test_T26_render_disable_dedupe_seed(self):
        entries = [validate_entry({'tag':['cat']}), validate_entry({'tag':['cat'], 'nl':'different'}),
                   validate_entry({'nl':'A dog.'}), validate_entry({'nl':'A dog.'}), validate_entry({'nl':'Disabled.', 'disable':True})]
        group = self.group(entries, nsfw=True)
        a, warnings = freeze_groups((group,), 'full_nl', 3, 2)
        b, _ = freeze_groups((group,), 'full_nl', 3, 2)
        self.assertEqual(a,b); self.assertEqual(len(a.items),2)
        self.assertTrue(warnings)
        self.assertEqual(render(entries[2], 'tags_plus_nl').effective_mode, 'full_nl')
        self.error('source:insufficient', lambda: freeze_groups((group,), 'full_nl', 0, 3))

    @unittest.skip('T27 为前端长按计时，本任务禁止修改 frontend/')
    def test_T27_frontend_hold_timers(self):
        pass

    @unittest.skip('T28 为前端键盘与迟到响应，本任务禁止修改 frontend/')
    def test_T28_frontend_accessibility(self):
        pass

    def test_T29_project_copy_and_export_snapshot(self):
        self.current = ('first', self.root.parent/'p1')
        ref = GroupRef('project','first',self.ref.group_id)
        self.group([validate_entry({'nl':'A cat.'})], ref=ref)
        shutil.copytree(self.current[1], self.root.parent/'p2')
        self.current = ('second', self.root.parent/'p2')
        self.error('project:closed', lambda: self.store.load(ref))
        self.assertEqual(self.store.load(replace(ref, project_token='second')).dictionary_id, ref.group_id)

    def test_T29_export_lease_survives_concurrent_replace(self):
        entry = validate_entry({'nl':'A cat.'})
        first = self.store.publish(prepare_preview(image_bytes(),entry.entry_id),self.ref,entry.entry_id)
        self.group([replace(entry,preview=first)])
        original_image = self.store.image
        def replace_during_export(ref, frozen_entry, *args):
            group = self.store.load(ref)
            second = self.store.publish(prepare_preview(image_bytes((20,20)),entry.entry_id),ref,entry.entry_id)
            self.store.save(ref,replace(group,entries=(replace(entry,preview=second),)),group.revision,'swap')
            self.store.collect_orphans(ref,0)
            return original_image(ref,frozen_entry,*args)
        with patch.object(self.store,'image',side_effect=replace_during_export):
            exported = self.exchange.export_group(self.ref,'zip')
        package = read_package(exported)
        manifest = parse_json(package['group.json'])['preview_manifest'][entry.entry_id]
        self.assertEqual(manifest['sha256'],first.sha256)
        self.assertNotEqual(self.store.load(self.ref).entries[0].preview.sha256,first.sha256)

    def test_nai_lazy_result_contract_and_row_failure(self):
        import sys
        from types import SimpleNamespace
        calls = []
        def convert(text):
            calls.append(text)
            if text == 'broken':
                raise ValueError('untrusted details')
            return SimpleNamespace(text=text.replace('kitty','cat'),warnings=[{'code':'converted'}])
        fake = SimpleNamespace(convert_nai_to_anima=convert)
        with patch.dict(sys.modules,{'stylelora.prompts.nai_convert':fake}):
            preview = self.preview([{'tag':['kitty'],'nl':'A kitty.'},{'tag':['broken']}],source_format='nai')
        self.assertEqual(preview['entries'][0]['tag'],['cat'])
        self.assertEqual(preview['valid_count'],1); self.assertEqual(preview['skipped_count'],1)
        self.assertTrue(preview['warnings']); self.assertIn('A kitty.',calls)

    def test_corrupt_group_isolation_and_nai_missing(self):
        self.group()
        bad = self.store.root(self.ref)/(str(uuid4())+'.json'); bad.write_text('{broken')
        groups, issues = self.store.list()
        self.assertEqual(len(groups),1); self.assertEqual(issues[0].code,'storage:corrupt_snapshot')
        import sys
        with patch.dict(sys.modules, {'stylelora.prompts.nai_convert': None}):
            self.error('import:unsupported', lambda: self.preview([{'tag':['cat']}], source_format='nai'))
