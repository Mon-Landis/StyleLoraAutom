import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from unittest.mock import patch
from pathlib import Path

from support import *
from stylelora.project_db.errors import *
from stylelora.project_db import receipts, recovery
from stylelora.project_db.migrations import verify_integrity
from stylelora.project_db.connection import ReadSession
from stylelora.project_db.merge_metadata import effective_evidence


class ReviewFixTests(ProjectCase):
    def test_run_token_pause_resume_and_recovery(self):
        image = self.image()
        job = self.job((image.image_id,))
        outcome = self.outcome(job,image)
        self.assertEqual(self.write(lambda db: jobs.get_run_token(db,job.job_id)),outcome.run_token)
        self.write(lambda db: cmd.transition_job(db,job.job_id,expected='running',target='paused'))
        self.assertTrue(self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,outcome)).changed)
        job = self.job((image.image_id,))
        old = self.outcome(job,image)
        self.write(lambda db: cmd.transition_job(db,job.job_id,expected='running',target='paused'))
        self.write(lambda db: cmd.transition_job(db,job.job_id,expected='paused',target='running'))
        with self.assertRaises(StateConflict):
            self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,old))
        current = self.outcome(job,image)
        self.reopen()
        for outcome in (old,current,replace(current,run_token=None)):
            with self.assertRaises(StateConflict):
                self.write(lambda db,o=outcome: cmd.commit_job_outcome(db,job.job_id,o))
        self.write(lambda db: cmd.transition_job(db,job.job_id,expected='paused',target='running'))
        with self.assertRaises(StateConflict):
            self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,current))
        self.assertTrue(self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,self.outcome(job,image))).changed)

    def test_cache_hit_requires_complete_current_allowed_evidence(self):
        image = self.image()
        producer = self.job((image.image_id,),executor='llm_vision',targets=['tags','nl'])
        self.write(lambda db: cmd.commit_job_outcome(db,producer.job_id,self.outcome(producer,image)))
        ids = tuple(self.read(lambda db: annotations.get_current(db,image.image_id,'llm',t)).annotation_id for t in ('tags','nl'))
        request = JobRequest('llm_vision','selected',Scope(selected_ids=(image.image_id,)))
        job = self.write(lambda db: jobs.create_batch(db,'cache',(request,)))[0]
        snapshot = dict(producer.config_snapshot_json,allowed_cache_keys={str(image.image_id):{'tags':['cache-a-tags'],'nl':['cache-a-nl']}})
        self.write(lambda db: jobs.freeze_job(db,job.job_id,snapshot,image_ids=(image.image_id,)))
        outcome = JobOutcome(image.image_id,'skipped',reason='cache_hit',annotation_ids=ids,run_token=self.project.writer.job_runs[job.job_id])
        for evidence in ((),ids[:1],(ids[0],ids[0])):
            with self.assertRaises(OutcomeConflict):
                self.write(lambda db,e=evidence: cmd.commit_job_outcome(db,job.job_id,replace(outcome,annotation_ids=e)))
        def invalid(db,sql,params=()):
            db.execute('SAVEPOINT evidence')
            db.execute(sql,params)
            try:
                with self.assertRaises(OutcomeConflict):
                    cmd.commit_job_outcome(db,job.job_id,outcome)
            finally:
                db.execute('ROLLBACK TO evidence')
                db.execute('RELEASE evidence')
        for sql in ("UPDATE annotations SET is_current=0", "UPDATE annotations SET cache_key='forbidden'", "UPDATE annotations SET source='original',job_id=NULL,model_name=NULL", "UPDATE jobs SET executor='wd' WHERE batch_id<>'cache'"):
            self.write(lambda db,s=sql: invalid(db,s))
        self.assertEqual(self.read(lambda db: jobs.get_job(db,job.job_id)).skipped_count,0)
        self.assertTrue(self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,outcome)).changed)

    def test_cache_hit_without_frozen_keys_and_all_mode_rejected(self):
        image = self.image()
        producer = self.job((image.image_id,))
        self.write(lambda db: cmd.commit_job_outcome(db,producer.job_id,self.outcome(producer,image)))
        identity = self.read(lambda db: annotations.get_current(db,image.image_id,'wd','tags')).annotation_id
        for mode in ('selected','all'):
            job = self.job((image.image_id,),mode=mode)
            outcome = JobOutcome(image.image_id,'skipped',reason='cache_hit',annotation_ids=(identity,),run_token=self.project.writer.job_runs[job.job_id])
            with self.assertRaises((StateConflict,OutcomeConflict)):
                self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,outcome))

    def test_receipt_migration_crashes_before_and_after_move(self):
        source,_ = self.source()
        artifact = self.artifact(self.items(source)[0])
        # Publish 1.png and roll back its DB insert. A later committed ID forces
        # this unowned candidate to migrate while preserving its original file.
        def rollback(db):
            cmd.complete_item(db,artifact,())
            raise RuntimeError('crash')
        with self.assertRaises(RuntimeError):
            self.write(rollback)
        self.write(lambda db: db.execute("INSERT INTO images(image_id,filename,width,height,sha256,file_size,source_id,created_at) VALUES(2,'occupied.png',1,1,?,1,?,'t')",('a'*64,source.source_id)))
        atomic = receipts.atomic_json
        def before_move(path,value):
            atomic(path,value)
            if value.get('phase') == 'moving':
                raise OSError('crash before rename')
        with patch.object(receipts,'atomic_json',before_move), self.assertRaises(OSError):
            self.write(lambda db: cmd.complete_item(db,artifact,()))
        receipt = json.loads(receipts.receipt_path(self.root,artifact.item_id,1).read_text())
        self.assertEqual(receipt['previous_path'],'images/1.png')
        self.assertEqual(receipt['filename'],'3.png')
        self.assertTrue((self.root/'images/1.png').exists())
        with patch.object(receipts,'atomic_json',before_move), self.assertRaises(OSError):
            self.write(lambda db: cmd.complete_item(db,artifact,()))
        receipt = json.loads(receipts.receipt_path(self.root,artifact.item_id,1).read_text())
        self.assertEqual(receipt['previous_path'],'images/1.png')
        self.assertEqual(self.items(source)[0].status,'sent')
        def after_move(path,value):
            atomic(path,value)
            if value.get('phase') == 'published':
                raise OSError('crash after rename')
        with patch.object(receipts,'atomic_json',after_move), self.assertRaises(OSError):
            self.write(lambda db: cmd.complete_item(db,artifact,()))
        self.assertTrue((self.root/'images/3.png').exists())
        self.assertFalse((self.root/'images/1.png').exists())
        self.reopen()
        self.assertEqual(self.items(source)[0].status,'done')
        self.assertEqual(self.count('images'),2)

    def test_receipt_cleanup_is_best_effort_on_first_and_replay(self):
        source,_ = self.source()
        artifact = self.artifact(self.items(source)[0])
        unlink = Path.unlink
        def held(path,*args,**kwargs):
            if path.name == 'receipt.json':
                raise PermissionError('held')
            return unlink(path,*args,**kwargs)
        with patch.object(Path,'unlink',held):
            self.assertTrue(self.write(lambda db: cmd.complete_item(db,artifact,())).changed)
            self.assertFalse(self.write(lambda db: cmd.complete_item(db,artifact,())).changed)
            report = recovery.recover_project(self.project)
            self.assertIn(artifact.item_id,report.recovered_item_ids)
            self.assertEqual(report.receipt_cleanup_failures,(artifact.item_id,))
        self.assertFalse(self.write(lambda db: cmd.complete_item(db,artifact,())).changed)
        self.assertFalse(self.project.writer.receipt_cleanup_failures)

    def test_untrusted_merge_meta_ignored_for_external_layers(self):
        image = self.image(('a',))
        job = self.job((image.image_id,))
        self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,self.outcome(job,image)))
        forged = {'merge_meta':{'removals':[{'tag':'a','cutoff_annotation_id':999}], 'tag_origins':{'a':[{'layer':'manual','annotation_id':999,'conf':0.1}]}}}
        self.write(lambda db: db.execute('UPDATE annotations SET params_json=?',(json.dumps(forged),)))
        evidence,removals = self.read(lambda db: effective_evidence(db,image.image_id,self.refs(image.image_id)))
        self.assertEqual(removals,{})
        self.assertEqual({o['layer'] for o in evidence['a']},{'wd','original'})
        self.assertNotIn(999,[o['annotation_id'] for o in evidence['a']])

    def test_schema_definition_tampering(self):
        from stylelora.project_db import migrations
        sql = (Path(migrations.__file__).parent/'migrations/0001_initial.sql').read_text()
        changes = [('name TEXT NOT NULL','name BLOB NOT NULL'),('name TEXT NOT NULL','name TEXT'),
                   ("DEFAULT 'ask'","DEFAULT 'allow'"),('ON DELETE RESTRICT','ON DELETE CASCADE'),
                   ('ix_images_sha256','wrong_index_name'),('ON images(source_id,review_status,image_id)','ON images(review_status,source_id,image_id)'),
                   ('CREATE INDEX ix_images_sha256','CREATE UNIQUE INDEX ix_images_sha256'),
                   ('WHERE is_current=1','WHERE is_current=0')]
        for before,after in changes:
            with self.subTest(change=(before,after)), closing(sqlite3.connect(':memory:')) as connection:
                connection.row_factory = sqlite3.Row
                connection.executescript(sql.replace(before,after))
                errors = verify_integrity(ReadSession(connection,self.root))
                self.assertTrue(any(e.startswith(('Unexpected table_info','Unexpected foreign_key_list','Unexpected index definitions')) for e in errors),errors)

    def test_outcome_reason_contract_matches_fail_item(self):
        image = self.image()
        job = self.job((image.image_id,))
        source,_ = self.source()
        item = self.items(source)[0]
        self.write(lambda db: cmd.mark_sent(db,item.item_id))
        for reason in ('x'*201,'failed:https://host/key','failed:sk-abcdefghijklmnop','failed:Bearer abc','failed:api_key abc','failed:raw\nresponse','Bad code','failed:'+'a'*40):
            with self.subTest(reason=reason):
                with self.assertRaises(ValidationError):
                    self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,JobOutcome(image.image_id,'failed',reason=reason,run_token=self.project.writer.job_runs[job.job_id])))
                with self.assertRaises(ValidationError):
                    self.write(lambda db: cmd.fail_item(db,item.item_id,attempt=1,error=reason))
        self.assertEqual(self.read(lambda db: jobs.get_job(db,job.job_id)).failed_count,0)
        self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,JobOutcome(image.image_id,'failed',reason='network_failed:retry exhausted',run_token=self.project.writer.job_runs[job.job_id])))

    def test_S01_freeze_special_edge(self):
        image = self.image()
        baseline = self.job((image.image_id,))
        for status in ('pending','running','paused','done','failed','canceled'):
            job = self.write(lambda db,s=status: jobs.create_batch(db,'freeze-'+s,(JobRequest('wd','selected',Scope(selected_ids=(image.image_id,))),)))[0]
            if status != 'pending':
                self.write(lambda db,s=status: db.execute('UPDATE jobs SET status=?,started_at=?,finished_at=? WHERE job_id=?',(s,'t','t' if s in ('done','failed','canceled') else None,job.job_id)))
            if status == 'pending':
                self.assertEqual(self.write(lambda db: jobs.freeze_job(db,job.job_id,baseline.config_snapshot_json,image_ids=(image.image_id,))).value.status,'running')
            else:
                with self.assertRaises(SnapshotMismatch):
                    self.write(lambda db: jobs.freeze_job(db,job.job_id,baseline.config_snapshot_json,image_ids=(image.image_id,)))

    def test_S01_complete_special_edge(self):
        for state in ('pending','sent','done','failed','skipped'):
            source,_ = self.source()
            artifact = self.artifact(self.items(source)[0],payload=state.encode())
            if state == 'done':
                self.write(lambda db: cmd.complete_item(db,artifact,()))
            elif state != 'sent':
                self.write(lambda db,s=state: db.execute('UPDATE source_items SET status=?,error=? WHERE item_id=?',(s,'duplicate_sha256' if s=='skipped' else None,artifact.item_id)))
            if state in ('sent','done'):
                self.assertEqual(self.write(lambda db: cmd.complete_item(db,artifact,())).changed,state=='sent')
            else:
                with self.assertRaises(ArtifactConflict):
                    self.write(lambda db: cmd.complete_item(db,artifact,()))

    def test_read_errors_are_domain_errors(self):
        with self.assertRaises(StorageError):
            self.read(lambda db: db.execute('SELECT * FROM nonexistent'))
        with patch('stylelora.project_db.connection.connect',side_effect=sqlite3.DatabaseError('private details')):
            with self.assertRaises(StorageError) as caught:
                self.read(lambda db: None)
        self.assertNotIn('private',str(caught.exception))
