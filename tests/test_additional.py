from dataclasses import replace
from itertools import product
import sqlite3
from unittest.mock import patch

from support import *
from stylelora.project_db.errors import *
from stylelora.project_db.validation import now
from stylelora.project_db.repositories import schemes


class AdditionalTests(ProjectCase):
    def test_S01_item_edges_exhaustive(self):
        image = self.image()
        source,_ = self.source()
        item = self.items(source)[0]
        def check(db):
            # Every public item-state command is exercised against every state.
            calls = {'sent':lambda: cmd.mark_sent(db,item.item_id),
                     'failed':lambda: cmd.fail_item(db,item.item_id,attempt=1,error='failure'),
                     'pending':lambda: cmd.retry_failed_item(db,item.item_id,expected_attempt=1),
                     'skipped':lambda: cmd.skip_duplicate(db,item.item_id,sha256=image.sha256)}
            edges = {('pending','sent'),('sent','failed'),('failed','pending'),('pending','skipped'),('sent','skipped')}
            for before,target in product(('pending','sent','done','failed','skipped'),calls):
                db.execute('SAVEPOINT item_edge')
                # done needs a distinct image reference because item/image is unique.
                image_id = None
                if before=='done':
                    cursor = db.execute('INSERT INTO images(filename,width,height,sha256,file_size,source_id,created_at) VALUES(?,1,1,?,1,?,?)',
                                        ('edge.png',image.sha256,source.source_id,now()))
                    image_id = cursor.lastrowid
                db.execute('UPDATE source_items SET status=?,image_id=?,attempts=1,error=? WHERE item_id=?',
                           (before,image_id,'duplicate_sha256' if before=='skipped' else 'failure' if before=='failed' else None,item.item_id))
                allowed = (before,target) in edges or before==target and target in ('sent','failed','skipped')
                with self.subTest(before=before,target=target):
                    if allowed:
                        calls[target]()
                    else:
                        with self.assertRaises(StateConflict):
                            calls[target]()
                db.execute('ROLLBACK TO item_edge')
                db.execute('RELEASE item_edge')
        self.write(check)

    def test_D02_accept_supported_enum_values(self):
        image = self.image(('a',))
        job = self.job((image.image_id,))
        def check(db):
            for rating,source in product(('general','sensitive','questionable','explicit'),('keyword','model','fallback','refusal','manual','detector')):
                db.execute('UPDATE images SET rating=?,rating_source=?,rating_verified=?,rated_at=? WHERE image_id=?',
                           (rating,source,int(source in ('manual','refusal')),now(),image.image_id))
            for status in ('unreviewed','keep','reject'):
                db.execute('UPDATE images SET review_status=?',(status,))
            for policy in ('deny','ask','allow'):
                db.execute('UPDATE sources SET cost_policy=?',(policy,))
            for aspect in (None,'landscape','portrait','square'):
                db.execute('UPDATE source_items SET aspect=?',(aspect,))
            for executor,mode in product(('wd','llm_vision'),('selected','uncovered','all')):
                db.execute('UPDATE jobs SET executor=?,mode=?',(executor,mode))
            for layer,kind in product(('original','wd','llm','manual','rule'),('tags','nl')):
                if layer in ('wd','rule') and kind=='nl':
                    continue
                db.execute('INSERT INTO annotations(image_id,source,type,content,created_at,is_current) VALUES(?,?,?,?,?,0)',
                           (image.image_id,layer,kind,'[]' if kind=='tags' else 'text',now()))
        self.write(check)

    def test_C03_commit_unknown_not_replayed(self):
        writer = self.project.writer
        original = writer._connection
        calls = []
        class CommitFailure:
            def __getattr__(self,name):
                return getattr(original,name)
            def execute(self,sql,*args):
                if sql=='COMMIT':
                    raise sqlite3.OperationalError('simulated I/O failure')
                return original.execute(sql,*args)
        with patch.object(writer,'_connection',CommitFailure()):
            with self.assertRaises(CommitUnknown):
                self.write(lambda db: (calls.append('once'),sources.create_source(db,SourceDraft('import','unknown'))))
            with self.assertRaises(CommitUnknown):
                self.write(lambda db: calls.append('twice'))
        self.assertEqual(calls,['once'])
        self.reopen()
        self.assertEqual(self.count('sources'),0)

    def test_project_create_open_and_metadata(self):
        from stylelora.project_db import update_metadata
        self.assertIs(open_project(self.root),self.project)
        with self.assertRaises(ProjectExists):
            create_project(self.root,name='duplicate',trigger_word='')
        metadata = update_metadata(self.project,name='Renamed',trigger_word='style')
        self.reopen()
        self.assertEqual(self.project.metadata,metadata)

    def test_stale_session_and_readonly(self):
        with read_snapshot(self.project) as db:
            with self.assertRaises(StorageError):
                db.execute("UPDATE sources SET name='bad'")
        with self.assertRaises(WrongThread):
            db.execute('SELECT 1')

    def test_image_missing_requires_actual_matching_bytes(self):
        image = self.image()
        self.write(lambda db: images.set_file_missing(db,image.image_id,missing=True))
        (self.root/'images'/image.filename).write_bytes(b'changed')
        with self.assertRaises(FileMissing):
            self.write(lambda db: images.set_file_missing(db,image.image_id,missing=False,verified_sha256=image.sha256))

    def test_batch_idempotent_wd_then_llm(self):
        image = self.image()
        scope = Scope(selected_ids=(image.image_id,))
        requests = (JobRequest('llm_vision','all',scope),JobRequest('wd','selected',scope))
        first = self.write(lambda db: jobs.create_batch(db,'batch',requests))
        second = self.write(lambda db: jobs.create_batch(db,'batch',requests))
        self.assertEqual(first,second)
        self.assertEqual([j.executor for j in first],['wd','llm_vision'])
        with self.assertRaises(StateConflict):
            self.write(lambda db: jobs.create_batch(db,'batch',requests[:1]))

    def test_scheme_idempotent_update_token(self):
        old = self.read(lambda db: schemes.get_import_scheme(db,1))
        value = ImportSchemeInput(old.name,old.reader,old.params_json,old.is_default)
        self.assertFalse(self.write(lambda db: schemes.update_import_scheme(db,1,value,expected_updated_at=old.updated_at)).changed)
        changed = self.write(lambda db: schemes.update_import_scheme(db,1,replace(value,name='new'),expected_updated_at=old.updated_at)).value
        self.assertGreater(changed.updated_at,old.updated_at)
        with self.assertRaises(StateConflict):
            self.write(lambda db: schemes.update_import_scheme(db,1,replace(value,name='stale'),expected_updated_at=old.updated_at))
