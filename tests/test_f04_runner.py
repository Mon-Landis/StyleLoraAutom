import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from f02_support import *
from stylelora.project_db import create_project, close_project, submit_write, read_snapshot
from stylelora.project_db import commands
from stylelora.project_db.repositories import sources
from stylelora.project_db.types import SourceDraft, ItemSeed
from stylelora.project_db.errors import StateConflict
from stylelora.generation.profile import to_snapshot
from stylelora.sources.runner import SourceRunner
from stylelora.sources.cost import all_items
from stylelora.backends.types import plain


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_gate_blocks_resume_before_settings_lease(self):
        from stylelora.generation.recovery import recover_source
        backend = LocalBackend(BackendError('mcp:write_outcome_unknown', 'submit', 'unknown'))
        runner, seeds = await self.make_runner(backend=backend)
        await runner.start(seeds)
        await runner.wait()
        entered, release = asyncio.Event(), asyncio.Event()
        from stylelora.project_db.types import RecoveryEvidence
        async def slow_probe(project, item, backend):
            entered.set()
            await release.wait()
            return RecoveryEvidence(item.item_id, item.attempts, 'unknown')
        with patch('stylelora.generation.recovery.probe', side_effect=slow_probe), \
                patch.object(backend, 'apply_profile', wraps=backend.apply_profile) as apply:
            recovery = asyncio.create_task(recover_source(self.project, runner.source_id, backend))
            await entered.wait()
            resume = asyncio.create_task(runner.resume())
            await asyncio.sleep(0)
            self.assertFalse(resume.done())
            self.assertEqual(apply.call_count, 0)
            release.set()
            self.assertTrue(await recovery)
            with self.assertRaises(BackendError):
                await resume
            self.assertEqual(apply.call_count, 0)
            self.assertEqual(backend.submits, 1)

    async def test_recovery_rejects_active_runner(self):
        from stylelora.generation.recovery import recover_source
        fake = FakeLauncher()
        fake.submit_release = asyncio.Event()
        runner, seeds = await self.make_runner(fake)
        await runner.start(seeds)
        await fake.submit_entered.wait()
        with self.assertRaises(BackendError):
            await recover_source(self.project, runner.source_id)
        fake.submit_release.set()
        await runner.wait()
        self.assertEqual(fake.counts['submit_generation'], 1)

    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = create_project(self.root / 'project.slproj', name='test', trigger_word='')
        self.runners = []

    async def asyncTearDown(self):
        for runner in self.runners:
            await runner.close()
            if runner.blocked_backend is not None:
                backend = runner.blocked_backend
                backend.inflight = False
                backend.preparations.clear()
                backend.session.resolve_write_barrier()
                await backend.close()
        close_project(self.project)
        self.temp.cleanup()

    async def make_runner(self, fake=None, *, count=1, policy='ask', backend=None):
        profile = backend.profile if backend else PROFILE
        source = await asyncio.wrap_future(submit_write(self.project, lambda db: sources.create_source(db,
            SourceDraft('nai', 'source', to_snapshot(SourceGenerationConfig(profile, SMALL)),
                        cost_policy=policy, style_prompt='style', target_count=count))))
        async def factory():
            return backend if backend is not None else await fake.backend(self.root / f'journal-{source.source_id}')
        runner = SourceRunner(self.project, source.source_id, factory, submit_interval_s=0)
        self.runners.append(runner)
        return runner, tuple(ItemSeed(i, f'content {i}') for i in range(1, count + 1))

    async def test_T11_policies_and_invalid_estimates(self):
        for policy in ('deny', 'ask', 'allow'):
            fake = FakeLauncher(estimated_anlas=2)
            runner, seeds = await self.make_runner(fake, policy=policy)
            await runner.start(seeds)
            await runner.wait()
            self.assertEqual(fake.counts['submit_generation'], int(policy == 'allow'))
            item = all_items(self.project, runner.source_id)[0]
            self.assertEqual(item.attempts, int(policy == 'allow'))
            self.assertEqual(runner.source().status, 'done' if policy == 'allow' else 'paused')
            self.assertEqual(fake.settings['steps'], 20)
        for estimate in (-1, None, True):
            fake = FakeLauncher(estimated_anlas=estimate)
            runner, seeds = await self.make_runner(fake)
            await runner.start(seeds)
            await runner.wait()
            self.assertEqual(fake.counts['submit_generation'], 0)
            self.assertEqual(runner.reason, 'cost:invalid_estimate')
            self.assertEqual(all_items(self.project, runner.source_id)[0].attempts, 0)

    async def test_T12_zero_then_paid_confirm_once_and_stale(self):
        fake = FakeLauncher(estimated_anlas=[0, 2, 2, 2])
        runner, seeds = await self.make_runner(fake, count=3)
        await runner.start(seeds)
        await runner.wait()
        self.assertEqual(fake.counts['submit_generation'], 1)
        self.assertEqual([i.attempts for i in all_items(self.project, runner.source_id)], [1, 0, 0])
        cycle = runner.cycle_id
        with self.assertRaises(StateConflict):
            await runner.confirm_cost_and_resume('stale')
        await asyncio.gather(runner.confirm_cost_and_resume(cycle), runner.confirm_cost_and_resume(cycle))
        await runner.wait()
        self.assertEqual(fake.counts['submit_generation'], 3)
        self.assertEqual(runner.source().status, 'done')
        self.assertFalse(self.project.writer.cycles[runner.source_id][1])
        await runner.confirm_cost_and_resume(cycle)
        self.assertEqual(fake.counts['submit_generation'], 3)

    async def test_T12_normal_resume_and_policy_clear_ack(self):
        fake = FakeLauncher(estimated_anlas=2)
        runner, seeds = await self.make_runner(fake)
        await runner.start(seeds)
        await runner.wait()
        first_cycle = runner.cycle_id
        await runner.resume()
        await runner.wait()
        self.assertEqual(fake.counts['submit_generation'], 0)
        self.assertNotEqual(first_cycle, runner.cycle_id)
        await runner.set_cost_policy('deny')
        with self.assertRaises(StateConflict):
            await runner.confirm_cost_and_resume(runner.cycle_id)
        await runner.set_cost_policy('allow')
        await runner.resume()
        await runner.wait()
        self.assertEqual(fake.counts['submit_generation'], 1)

    async def test_T13_explicit_rejection_timeout_and_unknown(self):
        for code, outcome in (('approval:rejected', 'rejected'), ('approval:timeout', 'rejected'),
                              ('mcp:write_outcome_unknown', 'unknown')):
            backend = LocalBackend(BackendError(code, 'submit', outcome))
            runner, seeds = await self.make_runner(backend=backend)
            await runner.start(seeds)
            await runner.wait()
            item = all_items(self.project, runner.source_id)[0]
            self.assertEqual(item.status, 'failed' if outcome == 'rejected' else 'sent')
            self.assertEqual(backend.submits, 1)
            self.assertEqual(runner.source().status, 'paused')

    async def test_T14_save_path_exists_pauses_without_attempt(self):
        fake = FakeLauncher(save_path_exists=True)
        runner, seeds = await self.make_runner(fake)
        await runner.start(seeds)
        await runner.wait()
        self.assertEqual(fake.counts['prepare_generation'], 1)
        self.assertEqual(fake.counts['submit_generation'], 0)
        self.assertEqual(all_items(self.project, runner.source_id)[0].attempts, 0)
        self.assertEqual(runner.reason, 'artifact:save_path_exists')

    async def test_T14_pending_trusted_existing_artifact_resume_register_only(self):
        from stylelora.generation.artifacts import (AttemptReceipt, write_receipt, request_digest, inspection_summary)
        from stylelora.generation.profile import build_request, from_snapshot
        from stylelora.generation.metadata import inspect_png
        backend = LocalBackend()
        runner, seeds = await self.make_runner(backend=backend)
        await asyncio.wrap_future(submit_write(self.project, lambda db: commands.start_source(db, runner.source_id, seeds)))
        source = runner.source()
        item = all_items(self.project, runner.source_id)[0]
        request = build_request(from_snapshot(source.config_json), source, item, self.project.root)
        request.save_path.parent.mkdir(parents=True)
        request.save_path.write_bytes(png_bytes(request.positive))
        inspection = inspect_png(request.save_path, style_prompt=source.style_prompt, sent_prompt=request.positive)
        receipt = AttemptReceipt(source.source_id, item.item_id, 1, request_digest(request, self.project.root),
            digest(source.config_json), request.save_path.relative_to(self.project.root).as_posix(), phase='complete',
            sha256=inspection.sha256, file_size=inspection.file_size, width=640, height=640, complete=True)
        write_receipt(self.project.root, receipt)
        await runner.start(seeds)
        await runner.wait()
        self.assertEqual(runner.source().status, 'paused')
        self.assertEqual(all_items(self.project, runner.source_id)[0].attempts, 0)
        await runner.resume()
        await runner.wait()
        self.assertEqual(all_items(self.project, runner.source_id)[0].status, 'done')
        self.assertEqual(backend.submits, 0)

    async def test_T21_pause_before_mark_sent_never_submits(self):
        backend = LocalBackend()
        original_prepare = backend.prepare
        entered, release = asyncio.Event(), asyncio.Event()
        async def preparing(request):
            entered.set()
            await release.wait()
            return await original_prepare(request)
        backend.prepare = preparing
        runner, seeds = await self.make_runner(backend=backend)
        await runner.start(seeds)
        await entered.wait()
        await runner.pause()
        release.set()
        await runner.wait()
        self.assertEqual(backend.submits, 0)
        self.assertEqual(all_items(self.project, runner.source_id)[0].attempts, 0)

    async def test_T23_real_adapter_unknown_submit_stays_sent_once(self):
        fake = FakeLauncher(submit_fault='timeout')
        runner, seeds = await self.make_runner(fake)
        await runner.start(seeds)
        await runner.wait()
        self.assertEqual(all_items(self.project, runner.source_id)[0].status, 'sent')
        self.assertEqual(runner.source().status, 'paused')
        self.assertEqual(fake.counts['submit_generation'], 1)
        with self.assertRaises(BackendError):
            await runner.resume()
        self.assertEqual(fake.counts['submit_generation'], 1)

    async def test_T21_pause_stop_after_mark_sent_late_completion(self):
        for action in ('pause', 'stop'):
            backend = LocalBackend()
            backend.release = asyncio.Event()
            runner, seeds = await self.make_runner(backend=backend, count=2)
            await runner.start(seeds)
            await backend.entered.wait()
            self.assertEqual(all_items(self.project, runner.source_id)[0].status, 'sent')
            await getattr(runner, action)()
            backend.release.set()
            await runner.wait()
            self.assertEqual([i.status for i in all_items(self.project, runner.source_id)], ['done', 'pending'])
            self.assertEqual(backend.submits, 1)
            self.assertEqual(runner.source().status, 'paused')

    async def test_T21_manual_retry_once_done_reopens(self):
        backend = LocalBackend(BackendError('approval:rejected', 'submit', 'rejected'))
        runner, seeds = await self.make_runner(backend=backend)
        await runner.start(seeds)
        await runner.wait()
        await asyncio.wrap_future(submit_write(self.project, lambda db: commands.transition_source(db,
            runner.source_id, expected='paused', target='done', reason='test')))
        item = all_items(self.project, runner.source_id)[0]
        await runner.retry_failed(item.item_id, 1)
        await runner.retry_failed(item.item_id, 1)
        self.assertEqual(all_items(self.project, runner.source_id)[0].attempts, 2)
        self.assertEqual(runner.source().status, 'paused')
        self.assertEqual(backend.submits, 1)

    async def test_T22_stateless_local_backend_completes_without_cost(self):
        backend = LocalBackend()
        runner, seeds = await self.make_runner(backend=backend, count=2, policy='deny')
        await runner.start(seeds)
        await runner.wait()
        self.assertEqual(runner.source().status, 'done')
        self.assertEqual(backend.submits, 2)
        self.assertEqual([i.status for i in all_items(self.project, runner.source_id)], ['done', 'done'])

    async def test_T23_summary_coalescing_remaining_no_secrets(self):
        fake = FakeLauncher(estimated_anlas=[0, 3])
        runner, seeds = await self.make_runner(fake, count=3)
        queue = runner.events.subscribe()
        await runner.start(seeds)
        await runner.wait()
        self.assertEqual(queue.qsize(), 1)
        event = await queue.get()
        text = json.dumps(plain(event))
        for forbidden in ('DO-NOT-LOG-SECRET', 'content 1', 'style,', 'base64', str(self.root)):
            self.assertNotIn(forbidden, text)
        account = runner.cost.account_status()
        self.assertEqual(account.remaining_estimated_anlas, 3)
        self.assertEqual(account.unpriced_items, 1)
        item = all_items(self.project, runner.source_id)[1]
        runner.cost.remember(item, 3)
        self.assertEqual(runner.cost.account_status().remaining_estimated_anlas, 3)
        self.assertIsNone(account.anlas_balance)

    async def test_T23_connection_failure_draft_unchanged(self):
        fake = FakeLauncher()
        runner, seeds = await self.make_runner(fake)
        async def unavailable():
            raise BackendError('discovery:not_running', 'discovery')
        runner.backend_factory = unavailable
        with self.assertRaises(BackendError):
            await runner.start(seeds)
        self.assertEqual(runner.source().status, 'draft')
        self.assertIsNone(runner.source().started_at)
        self.assertEqual(fake.counts['submit_generation'], 0)
