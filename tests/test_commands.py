from dataclasses import replace
from itertools import product

from support import *
from stylelora.project_db.errors import *
from stylelora.project_db.validation import now
from stylelora.project_db.repositories import schemes

# Independent transcription of F01 §5.1 and §5.3; special command edges below.
SOURCE_TRANSITIONS = {('running','paused'),('paused','running'),('running','done'),
                      ('running','canceled'),('paused','canceled'),('paused','done'),('draft','canceled')}
JOB_TRANSITIONS = {('running','paused'),('paused','running'),('running','done'),
                   ('running','failed'),('running','canceled'),('pending','canceled'),
                   ('paused','canceled'),('pending','failed'),('paused','done'),('paused','failed')}


class CommandTests(ProjectCase):
    def test_S01_source_and_job_edge_tables(self):
        # Exhaustive state pairs with independent savepoint fixtures. Special
        # start/retry edges have distinct commands and are tested below.
        def check(db):
            source = sources.create_source(db,SourceDraft('import','edges'))
            for before,after in product(('draft','running','paused','done','canceled'),repeat=2):
                db.execute('SAVEPOINT edge')
                locked = before != 'draft'
                db.execute('UPDATE sources SET status=?,locked=?,started_at=?,finished_at=? WHERE source_id=?',
                           (before,int(locked),'t' if locked else None,'t' if before in ('done','canceled') else None,source.source_id))
                db.execute("INSERT INTO source_items(source_id,seq,prompt,status,attempts,error,updated_at) VALUES(?,1,'p','failed',1,'failure','t')",(source.source_id,))
                with self.subTest(machine='source',before=before,after=after):
                    if before==after or (before,after) in SOURCE_TRANSITIONS:
                        cmd.transition_source(db,source.source_id,expected=before,target=after,reason='test')
                    else:
                        with self.assertRaises(StateConflict):
                            cmd.transition_source(db,source.source_id,expected=before,target=after,reason='test')
                db.execute('ROLLBACK TO edge')
                db.execute('RELEASE edge')
            job = jobs.create_batch(db,'edges',(JobRequest('wd','all',Scope(selected_ids=())),))[0]
            for before,after in product(('pending','running','paused','done','failed','canceled'),repeat=2):
                db.execute('SAVEPOINT edge')
                db.execute('UPDATE jobs SET status=?,started_at=?,finished_at=? WHERE job_id=?',
                           (before,None if before=='pending' else 't','t' if before in ('done','failed','canceled') else None,job.job_id))
                with self.subTest(machine='job',before=before,after=after):
                    if before==after or (before,after) in JOB_TRANSITIONS:
                        cmd.transition_job(db,job.job_id,expected=before,target=after)
                    else:
                        with self.assertRaises(StateConflict):
                            cmd.transition_job(db,job.job_id,expected=before,target=after)
                db.execute('ROLLBACK TO edge')
                db.execute('RELEASE edge')
        self.write(check)
        image = self.image()
        for target in ('keep','reject','unreviewed','reject','keep','unreviewed'):
            image = self.write(lambda db,i=image,t=target: images.set_review(db,i.image_id,expected=i.review_status,target=t)).value

    def test_S02_start_atomic_and_locked(self):
        source,seeds = self.source(count=2,start=False)
        with self.assertRaises(ValidationError):
            self.write(lambda db: cmd.start_source(db,source.source_id,(seeds[0],ItemSeed(2,'p','square'))))
        self.assertEqual(self.items(source),())
        self.assertFalse(self.read(lambda db: sources.get_source(db,source.source_id)).locked)
        self.write(lambda db: cmd.start_source(db,source.source_id,seeds))
        for patch in (SourcePatch(type='nai'),SourcePatch(config={'schema_version':1}),SourcePatch(style_prompt='x'),SourcePatch(negative_prompt='x'),SourcePatch(target_count=9)):
            with self.subTest(patch=patch), self.assertRaises(LockedSource):
                self.write(lambda db,p=patch: sources.update_source(db,source.source_id,p))
        with self.assertRaises(SeedConflict):
            self.write(lambda db: sources.seed_items(db,source.source_id,(ItemSeed(1,'changed'),)))

    def test_S03_cost_policies(self):
        for policy in ('deny','ask','allow'):
            source,_ = self.source(policy=policy)
            self.assertEqual(cmd.evaluate_cost(self.project,source.source_id,estimated_anlas=0),'submit')
            self.assertEqual(cmd.evaluate_cost(self.project,source.source_id,estimated_anlas=1),'submit' if policy=='allow' else 'paused')
            self.assertEqual(self.items(source)[0].attempts,0)
            if policy=='deny':
                cycle = self.project.writer.cycles[source.source_id][0]
                with self.assertRaises(StateConflict):
                    cmd.confirm_cost_and_resume(self.project,source.source_id,expected_cycle_id=cycle)

    def test_S03a_cycle_confirmation_lifetime(self):
        source,_ = self.source()
        sid = source.source_id
        cmd.evaluate_cost(self.project,sid,estimated_anlas=1)
        cycle = self.project.writer.cycles[sid][0]
        cmd.confirm_cost_and_resume(self.project,sid,expected_cycle_id=cycle)
        self.assertEqual(cmd.evaluate_cost(self.project,sid,estimated_anlas=2),'submit')
        self.write(lambda db: cmd.transition_source(db,sid,expected='running',target='paused',reason='user'))
        with self.assertRaises(StateConflict):
            cmd.confirm_cost_and_resume(self.project,sid,expected_cycle_id=cycle)
        self.write(lambda db: cmd.transition_source(db,sid,expected='paused',target='running',reason='user'))
        self.assertEqual(cmd.evaluate_cost(self.project,sid,estimated_anlas=2),'paused')
        self.reopen()
        self.assertEqual(self.project.writer.cycles,{})

    def test_S03b_policy_changes_do_not_revoke_sent(self):
        source,_ = self.source(count=2,policy='allow')
        items = self.items(source)
        self.write(lambda db: cmd.mark_sent(db,items[0].item_id))
        self.write(lambda db: cmd.set_cost_policy(db,source.source_id,policy='ask'))
        self.assertEqual(cmd.evaluate_cost(self.project,source.source_id,estimated_anlas=1),'paused')
        self.assertEqual(self.items(source)[0].status,'sent')
        self.assertNotIn('runtime_json',[f.name for f in __import__('dataclasses').fields(SourceRow)])

    def test_S04_attempts_explicit_retry(self):
        source,_ = self.source()
        item = self.items(source)[0]
        sent = self.write(lambda db: cmd.mark_sent(db,item.item_id)).value
        self.assertEqual(sent.attempts,1)
        self.assertFalse(self.write(lambda db: cmd.mark_sent(db,item.item_id)).changed)
        self.reopen()
        self.assertEqual(self.items(source)[0].status,'sent')
        self.write(lambda db: cmd.fail_item(db,item.item_id,attempt=1,error='network_failed'))
        self.write(lambda db: cmd.transition_source(db,source.source_id,expected='paused',target='done',reason='complete'))
        first = self.write(lambda db: cmd.retry_failed_item(db,item.item_id,expected_attempt=1))
        second = self.write(lambda db: cmd.retry_failed_item(db,item.item_id,expected_attempt=1))
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(first.value.attempts,2)
        self.assertEqual(self.read(lambda db: sources.get_source(db,source.source_id)).status,'paused')
        self.write(lambda db: cmd.transition_source(db,source.source_id,expected='paused',target='running',reason='user'))
        self.assertEqual(self.write(lambda db: cmd.mark_sent(db,item.item_id)).value.attempts,2)

    def test_S05_failed_items_and_multilayer_jobs(self):
        source,_ = self.source()
        item = self.items(source)[0]
        self.write(lambda db: cmd.mark_sent(db,item.item_id))
        self.write(lambda db: cmd.fail_item(db,item.item_id,attempt=1,error='failed'))
        self.write(lambda db: cmd.transition_source(db,source.source_id,expected='running',target='done',reason='complete'))
        image = self.image()
        job = self.job((image.image_id,),executor='llm_vision',targets=['tags','nl'])
        result = self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,self.outcome(job,image))).value
        self.assertEqual((result.done_count,result.total),(1,1))
        self.assertEqual(self.count('annotations'),2)
        self.write(lambda db: cmd.transition_job(db,job.job_id,expected='running',target='done'))
        failed = self.job((image.image_id,))
        self.write(lambda db: cmd.commit_job_outcome(db,failed.job_id,JobOutcome(image.image_id,'failed',reason='bad_image',run_token=self.project.writer.job_runs[failed.job_id])))
        result = self.write(lambda db: cmd.transition_job(db,failed.job_id,expected='running',target='done')).value
        self.assertEqual((result.status,result.failed_count),('done',1))

    def test_S06_review_missing(self):
        image = self.image(('a',))
        path = self.root/'images'/image.filename
        self.write(lambda db: images.set_review(db,image.image_id,expected='unreviewed',target='reject'))
        self.assertTrue(path.exists())
        self.write(lambda db: images.set_review(db,image.image_id,expected='reject',target='keep'))
        path.unlink()
        self.reopen()
        value = self.read(lambda db: images.get_image(db,image.image_id))
        self.assertEqual((value.review_status,value.file_missing),('keep',True))
        self.write(lambda db: images.set_review(db,image.image_id,expected='keep',target='reject'))
        for target in ('keep','unreviewed'):
            with self.assertRaises(FileMissing):
                self.write(lambda db,t=target: images.set_review(db,image.image_id,expected='reject',target=t))

    def test_I01_seed_start_replay(self):
        source,seeds = self.source(count=2)
        before = self.items(source)
        self.write(lambda db: cmd.start_source(db,source.source_id,seeds))
        self.assertEqual(self.items(source),before)
        with self.assertRaises(SeedConflict):
            self.write(lambda db: cmd.start_source(db,source.source_id,(ItemSeed(1,'different'),seeds[1])))

    def test_I02_complete_replay(self):
        source,_ = self.source()
        originals = (OriginalInput('tags',(Tag('a'),)),)
        artifact = self.artifact(self.items(source)[0],originals=originals)
        first = self.write(lambda db: cmd.complete_item(db,artifact,originals))
        replay = self.write(lambda db: cmd.complete_item(db,artifact,originals))
        self.assertEqual(first.value.image_id,replay.value.image_id)
        self.assertFalse(replay.changed)
        self.assertEqual((self.count('images'),self.count('annotations')),(1,1))
        for changed in (replace(artifact,attempt=2),replace(artifact,sha256='f'*64)):
            with self.assertRaises(ArtifactConflict):
                self.write(lambda db,a=changed: cmd.complete_item(db,a,originals))

    def test_I03_cache_reversion_and_all(self):
        image = self.image()
        for key,mode,expected in [('a','selected',1),('a','selected',1),('b','selected',2),('a','selected',3),('a','all',4)]:
            job = self.job((image.image_id,),mode=mode)
            outcome = self.outcome(job,image,key=key)
            self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,outcome))
            self.assertFalse(self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,outcome)).changed)
            self.assertEqual(self.count('annotations'),expected)

    def test_I04_multilayer_failure_rolls_back(self):
        image = self.image()
        job = self.job((image.image_id,),executor='llm_vision',targets=['tags','nl'])
        outcome = self.outcome(job,image)
        bad = replace(outcome,annotations=(outcome.annotations[0],replace(outcome.annotations[1],confidence=2.0)))
        with self.assertRaises(ValidationError):
            self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,bad))
        self.assertEqual(self.count('annotations'),0)
        self.assertEqual(self.read(lambda db: jobs.get_job(db,job.job_id)).done_count,0)

    def test_I05_outcomes_and_frozen_scope(self):
        image = self.image()
        for kind in ('done','failed','skipped'):
            job = self.job((image.image_id,))
            outcome = self.outcome(job,image) if kind=='done' else JobOutcome(image.image_id,kind,reason='test',run_token=self.project.writer.job_runs[job.job_id])
            self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,outcome))
            self.reopen()
            self.assertFalse(self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,outcome)).changed)
            with self.assertRaises(OutcomeConflict):
                self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,JobOutcome(image.image_id,'failed',reason='changed')))
            self.image(payload=b'new')
            frozen = self.read(lambda db: jobs.get_job(db,job.job_id))
            self.assertEqual(frozen.progress_json['image_ids'],[image.image_id])
            self.assertEqual(frozen.done_count+frozen.failed_count+frozen.skipped_count,1)

    def test_I06_rating_cas(self):
        image = self.image()
        manual = RatingValue('explicit','manual',True,now())
        self.write(lambda db: images.set_rating(db,image.image_id,expected=None,value=manual))
        with self.assertRaises(StateConflict):
            self.write(lambda db: images.set_rating(db,image.image_id,expected=None,value=RatingValue('general','model',True,now())))
        self.assertEqual(self.write(lambda db: images.clear_recomputable_ratings(db,(image.image_id,))),0)
        self.assertEqual(self.read(lambda db: images.rating_of(images.get_image(db,image.image_id))),manual)
