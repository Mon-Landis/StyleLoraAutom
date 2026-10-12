import asyncio
from dataclasses import replace
import hashlib
import io
import json
from unittest.mock import patch
from PIL import Image
from support import ProjectCase
from stylelora.project_db.types import JobRequest, Scope, ImageFilter, Tag, RatingValue, AnnotationInput
from stylelora.project_db.repositories import jobs, annotations, images
from stylelora.project_db import commands
from stylelora.project_db.errors import CommitUnknown, DiskFull, StateConflict
from stylelora.project_db.validation import now
from stylelora.executors.runner import JobRunner, create_jobs
from stylelora.executors.cache import cache_keys
from stylelora.executors.events import EventBus
from stylelora.executors.types import ExecutionResult, Unavailable
from stylelora.vision.executor import VisionExecutor, SkillStore
from stylelora.vision.rating import RatingRouter
from stylelora.llm.types import LlmReply, RoleBinding, ImageTier


def png(color='red'):
    output = io.BytesIO()
    Image.new('RGB', (8, 8), color).save(output, format='PNG')
    return output.getvalue()


def snapshot(name='wd'):
    return {'schema_version': 1, 'role': name, 'model': 'test-model', 'protocol': 'test',
            'params': {}, 'target_types': ['tags'] if name == 'wd' else ['tags', 'nl'],
            'skill_hash': 'a'*64, 'concurrency': 1, 'timeout': 300, 'retries': 2, 'routing': {}}


class FakeExecutor:
    def __init__(self, hook=None):
        self.calls, self.hook = [], hook

    async def execute(self, context):
        self.calls.append(context)
        if self.hook:
            await self.hook(context)
        source = 'wd' if context.snapshot['role'] == 'wd' else 'llm'
        actual = {'provider': 'cpu'}
        keys = cache_keys(context, actual_model='test-model', actual_params=actual)
        values = tuple(AnnotationInput(context.image.image_id, source, kind,
                (Tag('blue sky', .9),) if kind == 'tags' else 'A sky.', model_name='test-model',
                params={'schema_version': 1, 'actual': actual}, cache_key=keys[source, kind])
                for kind in context.snapshot['target_types'])
        return ExecutionResult('done', annotations=values, rating=context.rating)


class FakeLlm:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    async def resolve_role(self, role):
        return RoleBinding('product-only', role, 'nsfw-model' if role == 'vision_nsfw' else 'test-model', 'test', 1)

    async def complete(self, request):
        self.requests.append(request)
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(request)
        return LlmReply(json.dumps(value) if isinstance(value, dict) else value,
                        request.binding.model, request.request_id, actual_tier=ImageTier())


class JobTests(ProjectCase):
    def batch(self, ids, name='wd', mode='selected', batch='batch'):
        return asyncio.run(create_jobs(self.project, [JobRequest(name, mode, Scope(selected_ids=tuple(ids)))], batch_id=batch))[0]

    def test_T18_T20_cache_all_and_resume_ledger(self):
        image = self.image(payload=png())
        executor = FakeExecutor()
        runner = JobRunner(self.project, {'wd': executor}, snapshots={'wd': snapshot()})
        first = self.batch([image.image_id])
        self.assertEqual(asyncio.run(runner.run(first.job_id)).done_count, 1)
        second = self.batch([image.image_id], batch='second')
        self.assertEqual(asyncio.run(runner.run(second.job_id)).skipped_count, 1)
        third = self.batch([image.image_id], mode='all', batch='third')
        self.assertEqual(asyncio.run(runner.run(third.job_id)).done_count, 1)
        asyncio.run(runner.run(third.job_id))
        self.assertEqual(len(executor.calls), 2)

    def test_T21_wd_first_and_block_failed_predecessor(self):
        image = self.image(payload=png())
        requests = [JobRequest(name, 'selected', Scope(selected_ids=(image.image_id,))) for name in ('llm_vision', 'wd')]
        wd, llm = asyncio.run(create_jobs(self.project, requests, batch_id='both'))
        executor = FakeExecutor()
        runner = JobRunner(self.project, {'wd': executor, 'llm_vision': executor}, snapshots={name: snapshot(name) for name in ('wd', 'llm_vision')})
        self.assertEqual(asyncio.run(runner.run(llm.job_id)).status, 'pending')
        self.write(lambda db: commands.transition_job(db, wd.job_id, expected='pending', target='failed'))
        self.assertEqual(asyncio.run(runner.run(llm.job_id)).status, 'pending')
        self.assertEqual(executor.calls, [])

    def test_T21_wd_evidence_read_after_predecessor(self):
        image = self.image(payload=png())
        requests = [JobRequest(name, 'selected', Scope(selected_ids=(image.image_id,))) for name in ('llm_vision', 'wd')]
        wd, llm = asyncio.run(create_jobs(self.project, requests, batch_id='both'))
        executor = FakeExecutor()
        runner = JobRunner(self.project, {'wd': executor, 'llm_vision': executor}, snapshots={name: snapshot(name) for name in ('wd', 'llm_vision')})
        async def run():
            await runner.run(wd.job_id)
            await runner.run(llm.job_id)
        asyncio.run(run())
        self.assertEqual(executor.calls[1].evidence.wd_tags, (Tag('blue sky', .9),))

    def test_T21_pause_accepts_inflight_cancel_discards(self):
        for action in ('pause', 'cancel'):
            image = self.image(payload=png('blue' if action == 'pause' else 'red'))
            job = self.batch([image.image_id], batch=action)
            async def run():
                entered, release = asyncio.Event(), asyncio.Event()
                async def hold(context):
                    entered.set()
                    await release.wait()
                executor = FakeExecutor(hold)
                runner = JobRunner(self.project, {'wd': executor}, snapshots={'wd': snapshot()})
                task = asyncio.create_task(runner.run(job.job_id))
                await entered.wait()
                await runner.control(job.job_id, action)
                release.set()
                return await task
            final = asyncio.run(run())
            self.assertEqual(final.status, 'done' if action == 'pause' else 'canceled')
            self.assertEqual(final.done_count, 1 if action == 'pause' else 0)

    def test_T18_missing_changed_and_reject_scope(self):
        image = self.image(payload=png())
        self.write(lambda db: images.set_review(db, image.image_id, expected='unreviewed', target='reject'))
        from stylelora.executors.scope import resolve_scope
        self.assertEqual(resolve_scope(self.project, Scope(filter=ImageFilter()), mode='all'), ())
        self.assertEqual(resolve_scope(self.project, Scope(selected_ids=(image.image_id,)), mode='selected'), (image.image_id,))
        (self.root/'images'/image.filename).unlink()
        executor = FakeExecutor()
        job = self.batch([image.image_id])
        runner = JobRunner(self.project, {'wd': executor}, snapshots={'wd': snapshot()})
        self.assertEqual(asyncio.run(runner.run(job.job_id)).skipped_count, 1)
        self.assertEqual(executor.calls, [])

    def test_T22_commit_unknown_queries_without_reinference(self):
        image = self.image(payload=png())
        executor = FakeExecutor()
        job = self.batch([image.image_id])
        runner = JobRunner(self.project, {'wd': executor}, snapshots={'wd': snapshot()})
        original = runner.write
        async def write(work):
            result = await original(work)
            if isinstance(result, tuple) and len(result) == 2 and hasattr(result[0], 'done_count'):
                raise CommitUnknown('test')
            return result
        runner.write = write
        self.assertEqual(asyncio.run(runner.run(job.job_id)).done_count, 1)
        self.assertEqual(len(executor.calls), 1)

    def test_T22_disk_full_retains_result_for_resume(self):
        image = self.image(payload=png())
        executor = FakeExecutor()
        job = self.batch([image.image_id])
        runner = JobRunner(self.project, {'wd': executor}, snapshots={'wd': snapshot()})
        original = runner._commit
        async def fail(*args):
            raise DiskFull('test')
        runner._commit = fail
        self.assertEqual(asyncio.run(runner.run(job.job_id)).status, 'paused')
        runner._commit = original
        async def resume():
            await runner.control(job.job_id, 'resume')
            return await runner.run(job.job_id)
        self.assertEqual(asyncio.run(resume()).done_count, 1)
        self.assertEqual(len(executor.calls), 1)

    def test_T23_batch_replay_normalizes_and_conflict(self):
        image = self.image(payload=png())
        first = self.batch([image.image_id, image.image_id])
        self.assertEqual(first, self.batch([image.image_id]))
        with self.assertRaises(StateConflict):
            self.batch([image.image_id], mode='all')

    def test_T22_events_committed_and_gap(self):
        image = self.image(payload=png())
        job = self.batch([image.image_id])
        bus = EventBus(capacity=1)
        observations = []
        def observe(event):
            if event.kind == 'job.image_completed':
                observations.append(self.read(lambda db: jobs.get_job(db, job.job_id)).done_count)
        bus.subscribe(observe)
        runner = JobRunner(self.project, {'wd': FakeExecutor()}, snapshots={'wd': snapshot()}, events=bus)
        asyncio.run(runner.run(job.job_id))
        self.assertEqual(observations, [1])
        self.assertEqual(bus.replay(bus.epoch, 0)[0], 'resync_required')
        self.assertEqual(bus.replay('previous', bus.sequence)[0], 'resync_required')


class VisionJobTests(ProjectCase):
    def setup_vision(self, replies, *, routing=None, keywords=()):
        image = self.image(tags=('person',), payload=png())
        skills = SkillStore(['R0 frozen', 'R2 visible content', 'R5 rating'])
        snap = snapshot('llm_vision')
        snap['roles'] = {'vision': {'model': 'test-model', 'protocol': 'test'},
                         'vision_nsfw': {'model': 'nsfw-model', 'protocol': 'test'},
                         'rating': {'model': 'test-model', 'protocol': 'test'}}
        snap['params']['skill_hashes'] = dict(zip(('R0', 'R2', 'R5'), skills.texts))
        snap['routing'] = routing or {}
        llm = FakeLlm(replies)
        executor = VisionExecutor(llm, skills=skills, project=self.project, router=RatingRouter(keywords=keywords))
        runner = JobRunner(self.project, {'llm_vision': executor}, snapshots={'llm_vision': snap})
        job = asyncio.run(create_jobs(self.project, [JobRequest('llm_vision', 'selected', Scope(selected_ids=(image.image_id,)))], batch_id='vision'))[0]
        return image, llm, runner, job

    def test_T16_refusal_persists_and_reroutes_distinct_model(self):
        image, llm, runner, job = self.setup_vision([
            {'status': 'refused_nsfw', 'reason': 'safety'}, {'vision_tags': [], 'vision_description': ''}])
        final = asyncio.run(runner.run(job.job_id))
        self.assertEqual(final.done_count, 1)
        row = self.read(lambda db: images.get_image(db, image.image_id))
        self.assertEqual((row.rating, row.rating_source, row.rating_verified), ('questionable', 'refusal', True))
        self.assertEqual([r.binding.role for r in llm.requests], ['vision', 'vision_nsfw'])
        self.assertEqual(self.read(lambda db: annotations.get_current(db, image.image_id, 'llm', 'nl')).model_name, 'nsfw-model')

    def test_T15_R5_uncertain_then_fallback_C10(self):
        image, llm, runner, job = self.setup_vision([
            {'rating': 'uncertain', 'reason': 'unknown'}, {'vision_tags': ['Blue_Sky'], 'vision_description': 'Sky.'}],
            routing={'use_rating_model': True, 'nsfwfallback': True})
        self.assertEqual(asyncio.run(runner.run(job.job_id)).done_count, 1)
        self.assertEqual([r.binding.role for r in llm.requests], ['rating', 'vision_nsfw'])
        self.assertFalse(llm.requests[1].payload['rating_verified'])
        self.assertFalse(llm.requests[1].payload['rating_policy']['r0_applies'])
        self.assertNotIn('natural_language', llm.requests[1].payload)

    def test_T16_R5_refusal_no_recursion(self):
        image, llm, runner, job = self.setup_vision([
            {'status': 'refused_nsfw', 'reason': 'safety'}, {'status': 'refused_nsfw', 'reason': 'again'}],
            routing={'use_rating_model': True})
        final = asyncio.run(runner.run(job.job_id))
        self.assertEqual(final.skipped_count, 1)
        self.assertEqual(len(llm.requests), 2)
        self.assertEqual(final.progress_json['outcomes'][str(image.image_id)]['reason'], 'refusal_repeated')

    def test_T17_missing_skill_does_not_call(self):
        image, llm, runner, job = self.setup_vision([])
        runner.snapshots['llm_vision']['params']['skill_hashes']['R2'] = '0'*64
        self.assertEqual(asyncio.run(runner.run(job.job_id)).status, 'failed')
        self.assertEqual(llm.requests, [])

    def test_T13_repair_budget_shared_across_reroute(self):
        image, llm, runner, job = self.setup_vision([
            '```json\n{}\n```', {'status': 'refused_nsfw', 'reason': 'safety'}, 'not json'])
        final = asyncio.run(runner.run(job.job_id))
        self.assertEqual(final.failed_count, 1)
        self.assertEqual(len(llm.requests), 3)

    def test_T16_manual_during_R2_causes_conflict(self):
        image, llm, runner, job = self.setup_vision([])
        def concurrent(request):
            self.write(lambda db: images.set_rating(db, image.image_id, expected=None,
                value=RatingValue('general', 'manual', True, now())))
            return {'vision_tags': ['sky'], 'vision_description': 'Sky.'}
        llm.replies.append(concurrent)
        final = asyncio.run(runner.run(job.job_id))
        self.assertEqual(final.failed_count, 1)
        self.assertIsNone(self.read(lambda db: annotations.get_current(db, image.image_id, 'llm', 'tags')))

    def test_T17_style_terms_fail_and_original_preserved(self):
        image, llm, runner, job = self.setup_vision([{'vision_tags': ['oil painting'], 'vision_description': ''}])
        runner.snapshots['llm_vision']['params']['forbidden_terms'] = ['oil painting']
        self.assertEqual(asyncio.run(runner.run(job.job_id)).failed_count, 1)
        self.assertEqual(self.read(lambda db: annotations.get_current(db, image.image_id, 'original', 'tags')).content, (Tag('person'),))
