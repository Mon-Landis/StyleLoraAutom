from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
from unittest.mock import patch

from support import *
from stylelora.project_db import write_transaction
from stylelora.project_db.errors import *
from stylelora.project_db.connection import WriteSession
from stylelora.project_db import recovery, migrations
from stylelora.project_db.receipts import receipt_path
from stylelora.project_db.repositories import schemes


class RuntimeTests(ProjectCase):
    def test_C01_fifo_single_writer(self):
        threads = []
        def task(index):
            def work(db):
                threads.append(threading.get_ident())
                return sources.create_source(db,SourceDraft('import',str(index))).source_id
            return self.write(work)
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(task,range(20)))
        self.assertEqual(len(set(result)),20)
        self.assertEqual(len(set(threads)),1)
        self.assertNotEqual(threads[0],threading.get_ident())
        with self.assertRaises(WrongThread), write_transaction(self.project):
            pass

    def test_C02_snapshot_and_process_lock(self):
        with read_snapshot(self.project) as db:
            before = db.execute('SELECT count(*) FROM sources').fetchone()[0]
            self.source()
            self.assertEqual(db.execute('SELECT count(*) FROM sources').fetchone()[0],before)
        self.assertEqual(self.count('sources'),before+1)
        code = 'from pathlib import Path; from stylelora.project_db import open_project; from stylelora.project_db.errors import ProjectLocked; import sys\ntry: open_project(Path(sys.argv[1]))\nexcept ProjectLocked: sys.exit(23)\n'
        result = subprocess.run([sys.executable,'-c',code,str(self.root)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,23,result.stderr)
        self.reopen()

    def test_C03_busy_and_disk_full_rollback(self):
        # Actual SQLite busy and FULL, not an exception-only repository mock.
        other = sqlite3.connect(self.root/'project.db',isolation_level=None)
        try:
            other.execute('BEGIN IMMEDIATE')
            future = submit_write(self.project,lambda db: sources.create_source(db,SourceDraft('import','busy')))
            with self.assertRaises(Busy):
                future.result(timeout=10)
        finally:
            other.rollback()
            other.close()
        def full(db):
            count = db.execute('PRAGMA page_count').fetchone()[0]
            db.execute(f'PRAGMA max_page_count={count}')
            sources.create_source(db,SourceDraft('import','x'*1000000))
        with self.assertRaises(DiskFull):
            self.write(full)
        self.assertEqual(self.count('sources'),0)

    def test_P01_bounded_pages_and_rule_budget(self):
        self.write(lambda db: [sources.create_source(db,SourceDraft('import',str(i))) for i in range(12)])
        first = self.read(lambda db: sources.list_sources(db,SourceFilter(),PageRequest(limit=5)))
        self.write(lambda db: sources.create_source(db,SourceDraft('import','late')))
        identities = [v.source_id for v in first.items]
        page = first
        while page.next_after_id is not None:
            page = self.read(lambda db: sources.list_sources(db,SourceFilter(),PageRequest(page.next_after_id,5,first.upper_id)))
            identities.extend(v.source_id for v in page.items)
        self.assertEqual(len(identities),12)
        self.assertEqual(len(set(identities)),12)
        with self.assertRaises(ValidationError):
            self.read(lambda db: sources.list_sources(db,SourceFilter(),PageRequest(limit=1001)))
        image = self.image()
        plan = self.rule(image,('a',))
        plan = replace(plan,changes=plan.changes*5001)
        with self.assertRaises(BatchTooLarge):
            self.write(lambda db: cmd.apply_rule(db,plan))
        self.assertEqual(self.count('operation_log'),0)

    def test_R01_pause_once(self):
        image = self.image()
        source,_ = self.source()
        job = self.job((image.image_id,))
        self.reopen()
        self.assertIn(source.source_id,self.project.writer.report.paused_source_ids)
        self.assertIn(job.job_id,self.project.writer.report.paused_job_ids)
        report = recovery.recover_project(self.project)
        self.assertEqual((report.paused_source_ids,report.paused_job_ids),((),()))

    def test_R02_complete_absent_unknown(self):
        source,_ = self.source(count=3)
        items = self.items(source)
        artifact = self.artifact(items[0])
        for item in items[1:]:
            self.write(lambda db,i=item: cmd.mark_sent(db,i.item_id))
        self.reopen()
        self.assertEqual(self.items(source)[0].status,'done')
        self.assertEqual(self.items(source)[2].status,'sent')
        self.write(lambda db: recovery.reconcile_item(db,RecoveryEvidence(items[1].item_id,1,'absent',reason='probe_finished')))
        self.assertEqual(self.items(source)[1].status,'failed')
        self.assertEqual(self.count('images'),1)

    def test_R03_four_crash_windows(self):
        for window in ('incomplete','before_rename','after_rename','after_commit'):
            with self.subTest(window=window):
                source,_ = self.source()
                item = self.items(source)[0]
                artifact = self.artifact(item,payload=window.encode())
                if window == 'incomplete':
                    receipt_path(self.root,item.item_id,1).unlink()
                    self.reopen()
                    self.assertEqual(self.items(source)[0].status,'sent')
                    continue
                if window == 'after_rename':
                    original = WriteSession.execute
                    def fail(db,sql,parameters=()):
                        if sql.startswith('INSERT INTO images'):
                            raise RuntimeError('crash')
                        return original(db,sql,parameters)
                    with patch.object(WriteSession,'execute',fail), self.assertRaises(RuntimeError):
                        self.write(lambda db: cmd.complete_item(db,artifact,()))
                elif window == 'after_commit':
                    saved = receipt_path(self.root,item.item_id,1).read_bytes()
                    result = self.write(lambda db: cmd.complete_item(db,artifact,())).value
                    receipt = json.loads(saved)
                    receipt.update(candidate_id=result.image_id,filename=result.filename)
                    migrations.atomic_json(receipt_path(self.root,item.item_id,1),receipt)
                self.reopen()
                finished = self.items(source)[0]
                self.assertEqual(finished.status,'done')
                count = self.count('images')
                self.reopen()
                self.assertEqual(self.count('images'),count)
                self.assertFalse(receipt_path(self.root,item.item_id,1).exists())

    def test_R03_orphan_never_overwritten(self):
        source,_ = self.source()
        item = self.items(source)[0]
        artifact = self.artifact(item)
        orphan = self.root/'images'/'1.png'
        orphan.write_bytes(b'unrelated')
        self.reopen()
        self.assertEqual(orphan.read_bytes(),b'unrelated')
        self.assertIn('1.png',self.project.writer.report.orphan_filenames)
        self.assertNotEqual(self.items(source)[0].image_id,1)

    def test_R04_deleted_reject_audit_preserves_history(self):
        image = self.image(('a',))
        plan = self.rule(image,('b',))
        operation = self.write(lambda db: cmd.apply_rule(db,plan))
        self.write(lambda db: images.set_review(db,image.image_id,expected='unreviewed',target='reject'))
        (self.root/'images'/image.filename).unlink()
        self.reopen()
        self.assertTrue(self.read(lambda db: images.get_image(db,image.image_id)).file_missing)
        self.assertEqual(self.count('annotations'),2)
        self.write(lambda db: cmd.undo_rule(db,operation.op_id))

    def test_E01_move_and_expendable_cache(self):
        source,_ = self.source()
        item = self.items(source)[0]
        self.artifact(item)
        close_project(self.project)
        shutil.rmtree(self.root/'cache')
        moved = self.root.with_name('moved.slproj')
        self.root.rename(moved)
        self.root = moved
        self.project = open_project(moved)
        self.assertEqual(self.items(source)[0].status,'done')
        self.assertTrue((moved/'cache').is_dir())
        with self.assertRaises(ValidationError):
            self.write(lambda db: sources.create_source(db,SourceDraft('import','bad',{'schema_version':1,'endpoint':'https://secret'})))

    def test_E02_scheme_defaults_and_no_export_mutation(self):
        image = self.image(('a',))
        value = ImportSchemeInput('other',params_json={'schema_version':1,'encoding':'utf-8-sig'})
        second = self.write(lambda db: schemes.create_import_scheme(db,value))
        with self.assertRaises(StateConflict):
            self.write(lambda db: schemes.delete_import_scheme(db,1))
        def rollback(db):
            schemes.set_default_import_scheme(db,second.scheme_id)
            raise RuntimeError('injected')
        with self.assertRaises(RuntimeError):
            self.write(rollback)
        self.assertEqual(self.read(schemes.get_default_import_scheme).scheme_id,1)
        self.write(lambda db: schemes.set_default_import_scheme(db,second.scheme_id))
        self.write(lambda db: schemes.delete_import_scheme(db,1))
        with self.assertRaises(StateConflict):
            self.write(lambda db: schemes.delete_export_scheme(db,1))
        old = self.read(lambda db: schemes.get_export_scheme(db,1))
        value = ExportSchemeInput(old.name,filter_json=old.filter_json,caption_config_json=old.caption_config_json,output_config_json=old.output_config_json,pipeline_json=({'schema_version':1,'processor':'future'},))
        updated = self.write(lambda db: schemes.update_export_scheme(db,1,value,expected_updated_at=old.updated_at)).value
        with self.assertRaises(UnsupportedScheme):
            schemes.require_executable(updated)
        self.assertEqual((self.count('images'),self.count('annotations')),(1,1))

    def test_core_query_indexes(self):
        image = self.image(('a',))
        def check(db):
            queries = [("SELECT * FROM images WHERE sha256=?",(image.sha256,),'ix_images_sha256'),
                       ("SELECT * FROM annotations WHERE image_id=? AND source=? AND type=? AND is_current=1",(image.image_id,'original','tags'),'ux_annotations_current'),
                       ("SELECT * FROM source_items WHERE source_id=? AND status=? ORDER BY seq",(image.source_id,'pending'),'ix_source_items_source_status_seq')]
            for sql,args,index in queries:
                plan = ' '.join(r[3] for r in db.execute('EXPLAIN QUERY PLAN '+sql,args))
                self.assertIn(index,plan)
        self.read(check)


class MigrationTests(ProjectCase):
    def setUp(self):
        super().setUp()
        close_project(self.project)
        metadata = json.loads((self.root/'project.json').read_text())
        metadata['format_version'] = 1
        metadata.pop('mode', None)
        migrations.atomic_json(self.root/'project.json', metadata)
        with closing(sqlite3.connect(self.root/'project.db')) as db:
            db.execute('PRAGMA user_version=1')
        with patch.object(migrations, 'CURRENT_VERSION', 1):
            self.project = open_project(self.root)

    def registry(self, upgrade):
        # Inject a trusted migration to exercise rollback and publication recovery.
        info = MigrationInfo(1,2,'test_step',hashlib.sha256(b'test_step_v1').hexdigest())
        return migrations.Migration(info,upgrade)

    def test_M01_trusted_migration_preserves_history(self):
        image = self.image(('a',))
        close_project(self.project)
        step = self.registry(lambda db: db.execute("UPDATE sources SET name='migrated'"))
        with patch.object(migrations,'CURRENT_VERSION',2), patch.object(migrations,'REGISTRY',(step,)):
            self.project = open_project(self.root)
            self.assertEqual(self.project.metadata.format_version,2)
            self.assertEqual(self.count('annotations'),1)
            self.assertEqual(self.read(lambda db: migrations.verify_integrity(db,full=True)),())
            self.assertEqual(self.read(lambda db: sources.get_source(db,image.source_id)).name,'migrated')

    def test_M02_sql_failure_and_json_publish_recovery(self):
        self.source()
        close_project(self.project)
        def fail(db):
            db.execute("UPDATE sources SET name='bad'")
            raise RuntimeError('injected')
        step = self.registry(fail)
        with patch.object(migrations,'CURRENT_VERSION',2), patch.object(migrations,'REGISTRY',(step,)):
            with self.assertRaises(MigrationFailed):
                open_project(self.root)
        with closing(sqlite3.connect(self.root/'project.db')) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT name FROM sources').fetchone()[0],'Source')
        calls = []
        step = self.registry(lambda db: (calls.append(1),db.execute("UPDATE sources SET name='upgraded'")))
        atomic = migrations.atomic_json
        def fail_publish(path,value):
            if path.name=='project.json':
                raise OSError('injected publish failure')
            atomic(path,value)
        with patch.object(migrations,'CURRENT_VERSION',2), patch.object(migrations,'REGISTRY',(step,)):
            with patch.object(migrations,'atomic_json',fail_publish), self.assertRaises(OSError):
                open_project(self.root)
            self.project = open_project(self.root)
            self.assertEqual(calls,[1])
            self.assertEqual(self.project.metadata.format_version,2)
            self.assertFalse((self.root/'.migration-state.json').exists())

    def test_M03_future_versions_unchanged_and_untrusted_mismatch(self):
        close_project(self.project)
        original = json.loads((self.root/'project.json').read_text())
        for format_version,user_version,error in ((3,1,NewerProject),(1,3,NewerProject),(1,0,VersionMismatch)):
            metadata = {**original,'format_version':format_version}
            migrations.atomic_json(self.root/'project.json',metadata)
            with closing(sqlite3.connect(self.root/'project.db')) as db:
                db.execute(f'PRAGMA user_version={user_version}')
            before = tuple(hashlib.sha256((self.root/p).read_bytes()).hexdigest() for p in ('project.json','project.db'))
            with self.assertRaises(error):
                open_project(self.root)
            after = tuple(hashlib.sha256((self.root/p).read_bytes()).hexdigest() for p in ('project.json','project.db'))
            self.assertEqual(before,after)

    def test_M04_missing_chain_space_and_checksum(self):
        close_project(self.project)
        with patch.object(migrations,'CURRENT_VERSION',2), patch.object(migrations,'REGISTRY',()):
            with self.assertRaises(VersionMismatch):
                open_project(self.root)
        step = self.registry(lambda db: None)
        with patch.object(migrations,'CURRENT_VERSION',2), patch.object(migrations,'REGISTRY',(step,)):
            with patch.object(migrations.shutil,'disk_usage',return_value=shutil._ntuple_diskusage(1,1,0)), self.assertRaises(MigrationFailed):
                open_project(self.root)
            self.assertFalse((self.root/'.migration-state.json').exists())
            atomic = migrations.atomic_json
            def fail(path,value):
                if path.name=='project.json':
                    raise OSError('publish')
                atomic(path,value)
            with patch.object(migrations,'atomic_json',fail), self.assertRaises(OSError):
                open_project(self.root)
            marker = self.root/'.migration-state.json'
            data = json.loads(marker.read_text())
            data['checksums'] = ['tampered']
            atomic(marker,data)
            with self.assertRaises(MigrationRecoveryRequired):
                open_project(self.root)
            data['checksums'] = [step.info.checksum]
            atomic(marker,data)
            restored = migrations.restore_migration_backup(self.root)
            self.assertEqual(restored.format_version,1)
            with closing(sqlite3.connect(self.root/'project.db')) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
            self.assertEqual(json.loads((self.root/'project.json').read_text())['format_version'],1)
