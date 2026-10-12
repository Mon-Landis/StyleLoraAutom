from dataclasses import asdict, replace
import errno
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from support import ProjectCase
from f09_support import catalog
from stylelora.project_db import commands
from stylelora.project_db.project import update_metadata
from stylelora.project_db.repositories import images, annotations, sources, schemes
from stylelora.project_db.types import Tag, ImageFilter, ItemFilter, PageRequest, OriginalInput, SourcePatch
from stylelora.project_db.errors import ValidationError, UnsupportedScheme
from stylelora.exchange.types import ImportParams
from stylelora.exchange.readers.txt_sidecar import decode_sidecar, TxtSidecarReader
from stylelora.exchange.importer import ImportService, scan_import
from stylelora.exchange.exporter import export_project, ExportCanceled
from stylelora.exchange.writers.txt_sidecar import TxtSidecarWriter
from stylelora.exchange.schemes import validate_scheme, save_scheme
from stylelora.exchange.files import sha256, all_pages
from stylelora.tagging.jsonio import loads
from stylelora.tagging.trigger_validation import override_digest
from stylelora.annotations.editing import edit_manual, ManualEdit
from stylelora.annotations.projection import get_annotation_view

TEXT = '1girl, long_hair\n\n一名女孩站在树旁。'


class ReaderSchemaTests(unittest.TestCase):
    def test_T17_encodings_bom_gbk_strict_and_ascii(self):
        for data,encoding,actual,fallback in (
            (TEXT.encode(),'utf-8-sig','utf-8',False),
            (b'\xef\xbb\xbf'+TEXT.encode(),'auto','utf-8-sig',False),
            (TEXT.encode('gbk'),'gbk','gbk',False),
            (TEXT.encode('gbk'),'auto','gbk',True),
            (b'1girl','auto','utf-8',False),
        ):
            with self.subTest(encoding=encoding,actual=actual):
                result = decode_sidecar(data,encoding)
                self.assertEqual((result.encoding,result.fallback),(actual,fallback))
                self.assertNotIn('\ufffd',result.text)
        for data,encoding in ((TEXT.encode('gbk'),'utf-8-sig'),(b'\xff','auto'),(b'\xef\xbb\xbf\xff','auto'),(b'\xef\xbb\xbf\xff','gbk')):
            with self.assertRaises(ValidationError):
                decode_sidecar(data,encoding)
        self.assertIn(b'\xd2\xbb',TEXT.encode('gbk'))

    def test_T17_layout_empty_missing_unknown_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            image = Path(root)/'a.png'
            image.write_bytes(b'placeholder')
            reader = TxtSidecarReader()
            self.assertEqual(reader.read(image,ImportParams(),catalog()).originals,())
            sidecar = image.with_suffix('.txt')
            for newline in ('\n','\r\n','\r'):
                sidecar.write_bytes(TEXT.replace('\n',newline).encode())
                result = reader.read(image,ImportParams(),catalog())
                self.assertEqual(result.originals[0].content,(Tag('1girl'),Tag('long hair')))
                self.assertEqual(result.originals[1].content,'一名女孩站在树旁。')
            sidecar.write_bytes(b'')
            self.assertEqual(reader.read(image,ImportParams(),catalog()).originals[0].content,())
            sidecar.write_bytes(b'customtrigger, 1girl\n\nFirst.\nSecond.')
            result = reader.read(image,ImportParams(),catalog())
            self.assertEqual(result.originals[1].content,'customtrigger\n\nFirst.\nSecond.')
            self.assertEqual(result.warnings[0].code,'sidecar:unknown_to_nl')
            for text in ('1girl\nsecond line','(1girl:1.2)'):
                sidecar.write_text(text,encoding='utf-8')
                with self.assertRaises(ValidationError):
                    reader.read(image,ImportParams(),catalog())

    def test_T30_schema_defaults_unknown_types_paths_and_pipeline(self):
        value = validate_scheme({'name':'Export'})
        self.assertEqual(value.output_config_json['image_mode'],'copy')
        self.assertEqual(value.caption_config_json['blocklist_mode'],'warn')
        self.assertEqual(validate_scheme({'name':'Import','reader':'txt_sidecar'}).params_json['encoding'],'utf-8-sig')
        for patch_value in (
            {'surprise':1}, {'caption_config_json':{'schema_version':True}},
            {'caption_config_json':{'tag_layers':['bad']}}, {'caption_config_json':{'wd_min_confidence':2}},
            {'caption_config_json':{'wd_min_confidence':True}}, {'filter_json':{'source_ids':[True]}},
            {'filter_json':{'review_statuses':['reject']}}, {'filter_json':{'include_missing':True}},
            {'output_config_json':{'filename':'../x'}}, {'output_config_json':{'subdirectory':'../x'}},
            {'output_config_json':{'subdirectory':'C:/x'}}, {'output_config_json':{'image_mode':'hardlink'}},
            {'pipeline_json':[{'processor':'unknown','params':{}}]},
        ):
            with self.subTest(value=patch_value),self.assertRaises(ValidationError):
                validate_scheme({'name':'x',**patch_value})
        for raw in ('{"name":"a","name":"b"}','{"name":"a","caption_config_json":{"wd_min_confidence":NaN}}'):
            with self.assertRaises(ValidationError):
                validate_scheme(raw)
        self.assertTrue(validate_scheme({'name':'x','pipeline_json':[{'processor':'metadata_strip','params':{}}]}).pipeline_json)


class ExchangeTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.catalog = catalog()
        self.incoming = Path(self.temp.name)/'incoming'
        self.incoming.mkdir()
        self.importer = ImportService(self.catalog)
        self.handle = self.importer.register_root(self.incoming)
        update_metadata(self.project,name='Test',trigger_word='zztrigger')

    def incoming_image(self,name='a.png',color='red',caption=TEXT):
        path = self.incoming/name
        path.parent.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(16,12),color).save(path)
        if caption is not None:
            path.with_suffix('.txt').write_bytes(caption.encode())
        return path

    def imported(self,**kwargs):
        source = self.importer.create_import(self.project,self.handle,**kwargs)
        self.importer.start_import(self.project,source.source_id,self.handle)
        return source,self.read(lambda db:tuple(all_pages(lambda p:images.list_images(db,ImageFilter(source_ids=(source.source_id,)),p))))

    def keep(self,image):
        self.write(lambda db:images.set_review(db,image.image_id,expected='unreviewed',target='keep'))

    def export(self,scheme_id=1,**kwargs):
        return export_project(self.project,scheme_id,catalog=self.catalog,**kwargs)

    def test_T18_scan_recursive_sort_shared_stem_and_no_absolute_paths(self):
        self.incoming_image('z/b.png')
        self.incoming_image('A.png')
        self.incoming_image('A.bmp')
        manifest = scan_import(self.incoming)
        self.assertEqual([e.relative_path for e in manifest.entries],['A.bmp','A.png','z/b.png'])
        self.assertEqual(manifest.warnings[0].code,'sidecar:shared_stem')
        self.assertEqual(len(scan_import(self.incoming,recursive=False).entries),2)
        with self.assertRaises(ValidationError):
            scan_import(Path(self.temp.name),project_root=self.root)
        source = self.importer.create_import(self.project,self.handle)
        self.assertNotIn(str(self.incoming),str(source.config_json))
        self.assertEqual(source.config_json['import_manifest'],['A.bmp','A.png','z/b.png'])

    def test_T18_scan_never_follows_directory_links(self):
        self.incoming_image('normal.png')
        linked = self.incoming/'linked'
        from stylelora.exchange.importer import _linked
        linked.mkdir()
        self.incoming_image('linked/hidden.png')
        with patch('stylelora.exchange.importer._linked',lambda p: p.resolve() == linked.resolve() or _linked(p)):
            self.assertEqual([e.relative_path for e in scan_import(self.incoming).entries],['normal.png'])

    def test_T19_import_complete_originals_dimensions_hash_phash_duplicate(self):
        path = self.incoming_image()
        source,rows = self.imported()
        self.assertEqual(len(rows),1)
        image = rows[0]
        self.assertEqual((image.width,image.height),(16,12))
        self.assertEqual(image.sha256,sha256(path))
        self.assertEqual(len(image.phash),16)
        original = self.read(lambda db:annotations.get_current(db,image.image_id,'original','tags'))
        self.assertEqual(original.content,(Tag('1girl'),Tag('long hair')))
        self.assertEqual(original.params_json['encoding'],'utf-8')
        second,rows = self.imported()
        self.assertEqual(rows,())
        item = self.items(second)[0]
        self.assertEqual((item.status,item.image_id,item.error),('skipped',None,'duplicate_sha256'))
        self.assertEqual(self.count('images'),1)

    def test_T19_missing_txt_bad_image_and_final_duplicate_check(self):
        self.incoming_image('a.png',caption=None)
        (self.incoming/'b.png').write_bytes(b'bad image')
        source,rows = self.imported()
        self.assertEqual(len(rows),1)
        self.assertIsNone(self.read(lambda db:annotations.get_current(db,rows[0].image_id,'original','tags')))
        self.assertEqual([i.status for i in self.items(source)],['done','failed'])
        # Bypass precheck once to exercise the second check inside the writer transaction.
        original = images.find_by_sha256
        calls = 0
        def miss_once(db,sha):
            nonlocal calls
            calls += 1
            return () if calls == 1 else original(db,sha)
        with patch.object(images,'find_by_sha256',miss_once):
            second,rows = self.imported()
        self.assertEqual(self.items(second)[0].status,'skipped')
        self.assertEqual(self.count('images'),1)

    def test_T20_receipt_crash_replay_and_done_not_repeated(self):
        self.incoming_image()
        source = self.importer.create_import(self.project,self.handle)
        complete = self.importer._complete
        with patch.object(self.importer,'_complete',side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.importer.start_import(self.project,source.source_id,self.handle)
        self.assertEqual(self.items(source)[0].status,'sent')
        self.importer.start_import(self.project,source.source_id,self.handle)
        self.assertEqual(self.count('images'),1)
        self.importer.start_import(self.project,source.source_id,self.handle)
        self.assertEqual(self.count('images'),1)
        self.assertEqual(self.items(source)[0].status,'done')

    def test_T20_changed_input_pauses_without_illegal_failed(self):
        path = self.incoming_image()
        source = self.importer.create_import(self.project,self.handle)
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValidationError,'input_changed'):
            self.importer.start_import(self.project,source.source_id,self.handle)
        self.assertEqual(self.read(lambda db:sources.get_source(db,source.source_id)).status,'paused')
        self.assertEqual(self.items(source)[0].status,'pending')
        self.assertEqual(self.count('images'),0)

    def test_T21_scheme_snapshot_not_reinterpreted(self):
        self.incoming_image()
        source = self.importer.create_import(self.project,self.handle)
        original = self.read(lambda db:schemes.get_import_scheme(db,1))
        save_scheme(self.project,{'name':'Changed','reader':'txt_sidecar','params_json':{'encoding':'gbk'},'is_default':True},
                    scheme_id=1,expected_updated_at=original.updated_at)
        self.importer.start_import(self.project,source.source_id,self.handle)
        self.assertEqual(self.read(lambda db:sources.get_source(db,source.source_id)).config_json['params']['encoding'],'utf-8-sig')

    def test_T22_export_filter_nl_utf8_and_always_skip_reject_missing(self):
        self.incoming_image('a.png','red')
        self.incoming_image('b.png','blue')
        self.incoming_image('c.png','green')
        self.incoming_image('d.png','yellow')
        _,rows = self.imported()
        self.keep(rows[0])
        self.write(lambda db:images.set_review(db,rows[1].image_id,expected='unreviewed',target='reject'))
        self.write(lambda db:images.set_file_missing(db,rows[2].image_id,missing=True))
        scheme = save_scheme(self.project,{'name':'NL','caption_config_json':{'include_nl':True}})
        report = self.export(scheme.scheme_id)
        self.assertEqual((report.selected,report.exported,report.skipped,report.failed),(4,1,3,0))
        self.assertEqual(report.skip_reasons,{'export:reject':1,'export:file_missing':1,'export:review_filter':1})
        caption = (self.root/report.dir/f'{rows[0].image_id}.txt').read_bytes()
        self.assertFalse(caption.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(caption.decode(),'zztrigger, 1girl, long hair\n\n一名女孩站在树旁。')
        self.assertEqual(report.tag_frequency,{'1girl':1,'long hair':1,'zztrigger':1})
        self.assertEqual(report.trigger_count,1)
        self.assertEqual(loads((self.root/report.dir/'report.json').read_bytes())['selected'],4)

    def test_T23_versions_copy_source_unchanged_metadata_and_hardlink(self):
        self.incoming_image()
        _,rows = self.imported()
        self.keep(rows[0])
        scheme = save_scheme(self.project,{'name':'JSON','output_config_json':{'include_json':True,'subdirectory':'data'}})
        first,second = self.export(scheme.scheme_id),self.export(scheme.scheme_id)
        self.assertNotEqual(first.dir,second.dir)
        source = self.root/'images'/rows[0].filename
        before = source.read_bytes()
        (self.root/first.dir/'data'/rows[0].filename).write_bytes(b'modified copy')
        self.assertEqual(source.read_bytes(),before)
        self.assertEqual((self.root/second.dir/'data'/f'{rows[0].image_id}.json').read_text(),'null')
        with self.assertRaisesRegex(UnsupportedScheme,'unsupported'):
            TxtSidecarWriter().write_image(source,self.incoming/'hard.png',mode='hardlink')
        self.assertFalse((self.incoming/'hard.png').exists())

    def test_T24_pipeline_persisted_but_never_executed(self):
        scheme = save_scheme(self.project,{'name':'Reserved','pipeline_json':[{'processor':'metadata_strip','params':{}}]})
        self.assertEqual(self.read(lambda db:schemes.get_export_scheme(db,scheme.scheme_id)).pipeline_json,scheme.pipeline_json)
        with self.assertRaisesRegex(UnsupportedScheme,'pipeline:unsupported'):
            self.export(scheme.scheme_id)
        self.assertFalse((self.root/'exports'/str(scheme.scheme_id)).exists())

    def test_T25_snapshot_freezes_review_annotations_and_metadata(self):
        self.incoming_image()
        _,rows = self.imported()
        image = rows[0]
        self.keep(image)
        def mutate():
            view = get_annotation_view(self.project,image.image_id)
            edit_manual(self.project,ManualEdit(image.image_id,(Tag('b'),),None,view.refs))
            self.write(lambda db:images.set_review(db,image.image_id,expected='keep',target='reject'))
            update_metadata(self.project,name='changed',trigger_word='anothertrigger')
        report = self.export(after_freeze=mutate)
        self.assertEqual(report.exported,1)
        self.assertEqual((self.root/report.dir/f'{image.image_id}.txt').read_text(),'zztrigger, 1girl, long hair')

    def test_T25_changed_image_failure(self):
        self.incoming_image()
        _,rows = self.imported()
        self.keep(rows[0])
        report = self.export(after_freeze=lambda:(self.root/'images'/rows[0].filename).write_bytes(b'changed'))
        self.assertEqual(report.failure_reasons,{'export:image_changed':1})
        self.assertEqual(report.exported,0)
        self.assertEqual(list((self.root/report.dir).iterdir()),[self.root/report.dir/'report.json'])

    def test_T28_half_pair_diskfull_cancel_no_publication(self):
        self.incoming_image()
        _,rows = self.imported()
        self.keep(rows[0])
        class Broken(TxtSidecarWriter):
            def write_caption(self,path,caption,**kwargs):
                path.write_bytes(b'partial')
                raise OSError('failed')
        report = self.export(writer=Broken())
        self.assertEqual(report.failed,1)
        self.assertEqual(len(list((self.root/report.dir).iterdir())),1)
        class Full(Broken):
            def write_caption(self,path,caption,**kwargs):
                path.write_bytes(b'partial')
                raise OSError(errno.ENOSPC,'disk full')
        before = set((self.root/'exports'/'1').iterdir())
        with self.assertRaises(OSError):
            self.export(writer=Full())
        new = set((self.root/'exports'/'1').iterdir())-before
        self.assertEqual(len(new),1)
        self.assertTrue(next(iter(new)).name.startswith('.partial-'))
        with self.assertRaises(ExportCanceled):
            self.export(cancel=lambda:True)
        self.assertEqual(sha256(self.root/'images'/rows[0].filename),rows[0].sha256)

    def test_T15_export_override_bound_to_catalog_and_all_sources(self):
        self.incoming_image()
        _,rows = self.imported()
        self.keep(rows[0])
        update_metadata(self.project,name='Test',trigger_word='1girl')
        with self.assertRaisesRegex(ValidationError,'existing_tag'):
            self.export()
        report = self.export(trigger_override_digest=override_digest('1girl',self.catalog))
        self.assertEqual(report.trigger_count,1)
        self.assertEqual(report.tag_frequency['1girl'],1)
        self.assertEqual(report.trigger_override_digest,override_digest('1girl',self.catalog))
        source,_ = self.source(kind='nai',start=False)
        self.write(lambda db:sources.update_source(db,source.source_id,SourcePatch(style_prompt='1girl style')))
        with self.assertRaisesRegex(ValidationError,'in_style_prompt'):
            self.export(trigger_override_digest=override_digest('1girl',self.catalog))

    def test_T14_export_warn_remove_report_and_no_mutation(self):
        self.incoming_image(caption='1girl, watercolor')
        _,rows = self.imported()
        self.keep(rows[0])
        before = self.count('annotations')
        warn = self.export()
        self.assertEqual(warn.tag_frequency['watercolor'],1)
        self.assertEqual(warn.blocklist_hits[0].image_id,rows[0].image_id)
        scheme = save_scheme(self.project,{'name':'remove','caption_config_json':{'blocklist_mode':'remove'}})
        removed = self.export(scheme.scheme_id)
        self.assertNotIn('watercolor',removed.tag_frequency)
        self.assertEqual(self.count('annotations'),before)
