import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from f02_support import *
from stylelora.project_db import create_project, open_project, close_project, submit_write, read_snapshot
from stylelora.project_db import commands, receipts
from stylelora.project_db.repositories import sources, images, annotations
from stylelora.project_db.types import SourceDraft, ItemSeed, ItemFilter, PageRequest
from stylelora.project_db.recovery import reconcile_item
from stylelora.generation.profile import to_snapshot, build_request
from stylelora.generation.metadata import inspect_png
from stylelora.generation.artifacts import *
from stylelora.generation.recovery import probe


class MetadataTests(unittest.TestCase):
    def test_phash_failure_overrides_prompt_pass_and_mismatch(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'out.png'
            for prompt in ('style, content', 'content'):
                path.write_bytes(png_bytes(prompt))
                for failure in (OSError(), RuntimeError()):
                    with patch('stylelora.vision.phash.compute_phash', side_effect=failure):
                        result = inspect_png(path, style_prompt='style', sent_prompt='style, content')
                    self.assertEqual(result.validation, 'unverifiable')
                    self.assertIn('phash_unavailable', result.reason_codes)
                    self.assertIsNone(result.phash)
                with patch('stylelora.vision.phash.compute_phash', return_value='invalid'):
                    self.assertEqual(inspect_png(path, style_prompt='style', sent_prompt=prompt).validation, 'unverifiable')

    def test_T15_full_decode_size_hash_and_tag_comparison(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'out.png'
            for actual, style, sent, expected in (
                ('simple background, cat', 'simple_background', 'simple_background, cat', 'pass'),
                ('artist, cat', 'art', 'art, cat', 'mismatch'),
                ('art, masterpiece', 'art', 'art, masterpiece', 'pass'),
                ('art, masterpiece', 'art', 'art', 'mismatch')):
                path.write_bytes(png_bytes(actual, (64, 64)))
                result = inspect_png(path, style_prompt=style, sent_prompt=sent, expected_size=(64, 64))
                self.assertEqual(result.validation, expected)
                self.assertEqual(result.file_size, path.stat().st_size)
                self.assertEqual(len(result.sha256), 64)
            with self.assertRaises(BackendError) as caught:
                inspect_png(path, style_prompt='', sent_prompt='', expected_size=(128, 64))
            self.assertEqual(caught.exception.code, 'artifact:size_mismatch')
            path.write_bytes(path.read_bytes()[:30])
            with self.assertRaises(BackendError):
                inspect_png(path, style_prompt='', sent_prompt='')

    def test_T16_stripped_comment_bad_json_unknown_syntax(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'out.png'
            path.write_bytes(png_bytes('style', (64, 64)))
            self.assertEqual(inspect_png(path, style_prompt='style', sent_prompt='style', metadata_stripped=True).validation, 'pass')
            path.write_bytes(png_bytes(comment=False))
            result = inspect_png(path, style_prompt='style', sent_prompt='style', metadata_stripped=True)
            self.assertEqual(result.reason_codes, ('meta:stripped',))
            self.assertIsNone(result.png_meta)
            path.write_bytes(png_bytes('(style:1.2)'))
            self.assertEqual(inspect_png(path, style_prompt='style', sent_prompt='style').validation, 'unverifiable')
            info = PngImagePlugin.PngInfo()
            info.add_text('Comment', '{broken')
            Image.new('RGB', (64, 64)).save(path, pnginfo=info)
            self.assertEqual(inspect_png(path, style_prompt='style', sent_prompt='style').reason_codes, ('meta:invalid_comment',))


class ArtifactTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_rejects_stale_cycle_and_run_token(self):
        from stylelora.generation.recovery import recover_source
        from stylelora.project_db.types import RecoveryEvidence
        await self.sent()
        cycle = self.project.writer.cycles[self.source.source_id]
        marker = self.root / f'images/.pending/recovery-{self.source.source_id}.json'
        for kind in ('cycle', 'run_token'):
            self.project.writer.cycles[self.source.source_id] = cycle
            async def stale(*args):
                if kind == 'cycle':
                    self.project.writer.cycles[self.source.source_id] = ('stale', False)
                else:
                    data = json.loads(marker.read_text())
                    data['run_token'] = 'stale'
                    data.pop('digest')
                    data['digest'] = digest(data)
                    marker.write_text(json.dumps(data))
                return RecoveryEvidence(self.item.item_id, 1, 'unknown')
            with patch('stylelora.generation.recovery.probe', side_effect=stale), self.assertRaises(BackendError):
                await recover_source(self.project, self.source.source_id)
            self.assertTrue(marker.exists())

    async def test_recovery_rejects_wrong_backend_instance(self):
        await self.sent()
        self.request.save_path.write_bytes(png_bytes())
        write_receipt(self.root, replace(self.intent, phase='submitted', resource_id='resource',
                                        backend_instance='different-instance'))
        backend = LocalBackend()
        with patch.object(backend, 'reconcile', wraps=backend.reconcile) as reconcile:
            self.assertEqual((await probe(self.project, self.item, backend)).verdict, 'unknown')
            self.assertEqual(reconcile.call_count, 0)
        self.assertEqual(backend.submits, 0)

    async def test_recovery_crash_after_rename_recovers_again(self):
        fake = FakeLauncher()
        backend = await fake.backend(Path(self.temp.name) / 'journal')
        await backend.apply_profile(PROFILE)
        write_receipt(self.root, replace(self.intent, backend_instance=backend.instance))
        await self.sent()
        await backend.submit(await backend.prepare(self.request))
        from stylelora.generation.recovery import recover_source
        with patch('stylelora.project_db.repositories.annotations.append_version', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                await recover_source(self.project, self.source.source_id, backend)
        self.assertFalse(self.request.save_path.exists())
        self.assertTrue(read_attempt(self.root, self.item.item_id, 1).complete)
        self.assertEqual(await recover_source(self.project, self.source.source_id, backend), ())
        self.assertEqual(await recover_source(self.project, self.source.source_id, backend), ())
        self.assertEqual(fake.counts['submit_generation'], 1)
        with read_snapshot(self.project) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM images').fetchone()[0], 1)
        await backend.close()

    async def test_crash_before_submitted_write_remains_unknown_never_resubmits(self):
        fake = FakeLauncher()
        backend = await fake.backend(Path(self.temp.name) / 'journal')
        await backend.apply_profile(PROFILE)
        await self.sent()
        preparation = await backend.prepare(self.request)
        with patch('stylelora.generation.artifacts.write_receipt', side_effect=OSError('disk full')):
            with self.assertRaises(BackendError):
                await backend.submit(preparation)
        from stylelora.generation.recovery import recover_source
        for _ in range(2):
            self.assertEqual(await recover_source(self.project, self.source.source_id, backend), (self.item.item_id,))
        self.assertEqual(read_attempt(self.root, self.item.item_id, 1).phase, 'intent')
        self.assertEqual(fake.counts['submit_generation'], 1)
        backend.inflight = False
        await backend.close()

    async def crash_window(self, target, *, after=False):
        import importlib
        fake = FakeLauncher()
        backend = await fake.backend(Path(self.temp.name) / 'journal')
        await backend.apply_profile(PROFILE)
        self.intent = replace(self.intent, backend_instance=backend.instance, cycle_id='cycle', run_token='run')
        write_receipt(self.root, self.intent)
        await self.sent()
        preparation = await backend.prepare(self.request)
        module_name, name = target.rsplit('.', 1)
        original = getattr(importlib.import_module(module_name), name)
        def crash(*args, **kwargs):
            if after:
                original(*args, **kwargs)
            raise RuntimeError('injected crash')
        try:
            if name == 'record_submission':
                with patch(target, side_effect=crash), self.assertRaises(BackendError):
                    await backend.submit(preparation)
            else:
                result = await backend.submit(preparation)
                with patch(target, side_effect=crash), self.assertRaises(RuntimeError):
                    await accept_result(self.project, self.source, self.item, self.request, result)
            receipt = read_attempt(self.root, self.item.item_id, 1)
            self.assertEqual(receipt.resource_id, 'fake-resource')
            self.assertEqual((receipt.attempt, receipt.cycle_id, receipt.run_token), (1, 'cycle', 'run'))
            self.assertEqual(receipt.saved_path, self.intent.relative_staging_path)
            from stylelora.generation.recovery import recover_source
            self.assertEqual(await recover_source(self.project, self.source.source_id, backend), ())
            self.assertEqual(await recover_source(self.project, self.source.source_id, backend), ())
            self.assertEqual(fake.counts['submit_generation'], 1)
            validation = json.loads((self.request.save_path.parent / 'validation.json').read_text())
            self.assertEqual((validation['resource_id'], validation['source_id'], validation['attempt']),
                             ('fake-resource', self.source.source_id, 1))
            with read_snapshot(self.project) as db:
                self.assertEqual(sources.get_item(db, self.item.item_id).status, 'done')
                self.assertEqual(db.execute('SELECT count(*) FROM images').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT count(*) FROM annotations').fetchone()[0], 1)
        finally:
            backend.inflight = False
            await backend.close()

    async def test_crash_after_submitted_receipt(self):
        await self.crash_window('stylelora.generation.artifacts.record_submission', after=True)

    async def test_crash_before_complete_receipt(self):
        await self.crash_window('stylelora.generation.artifacts.write_receipt')

    async def test_crash_after_complete_receipt(self):
        await self.crash_window('stylelora.generation.artifacts.write_receipt', after=True)

    async def test_crash_before_validation_receipt(self):
        await self.crash_window('stylelora.generation.artifacts.save_validation')

    async def test_crash_after_validation_receipt(self):
        await self.crash_window('stylelora.generation.artifacts.save_validation', after=True)

    async def test_crash_before_publication_receipt(self):
        await self.crash_window('stylelora.project_db.receipts.prepare_receipt')

    async def test_crash_after_publication_receipt(self):
        await self.crash_window('stylelora.project_db.receipts.prepare_receipt', after=True)

    async def test_submitted_missing_file_exports_bound_resource_only(self):
        fake = FakeLauncher()
        backend = await fake.backend(Path(self.temp.name) / 'journal')
        await backend.apply_profile(PROFILE)
        write_receipt(self.root, replace(self.intent, backend_instance=backend.instance))
        await self.sent()
        await backend.submit(await backend.prepare(self.request))
        self.request.save_path.unlink()
        from stylelora.generation.recovery import recover_source
        self.assertEqual(await recover_source(self.project, self.source.source_id, backend), ())
        self.assertEqual(await recover_source(self.project, self.source.source_id, backend), ())
        self.assertEqual(fake.counts['submit_generation'], 1)
        self.assertEqual(fake.counts['save_generated_image'], 1)
        await backend.close()

    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name) / 'project.slproj'
        self.project = create_project(self.root, name='test', trigger_word='')
        self.config = SourceGenerationConfig(PROFILE, SMALL)
        self.source = await self.write(lambda db: sources.create_source(db, SourceDraft('nai', 'source',
            to_snapshot(self.config), style_prompt='style', target_count=1)))
        await self.write(lambda db: commands.start_source(db, self.source.source_id, (ItemSeed(1, 'content'),)))
        with read_snapshot(self.project) as db:
            self.item = sources.list_items(db, ItemFilter(self.source.source_id), PageRequest()).items[0]
        self.request = build_request(self.config, self.source, self.item, self.root)
        self.intent = AttemptReceipt(self.source.source_id, self.item.item_id, 1,
            request_digest(self.request, self.root), digest(self.source.config_json),
            self.request.save_path.relative_to(self.root).as_posix())
        write_receipt(self.root, self.intent)

    async def asyncTearDown(self):
        close_project(self.project)
        self.temp.cleanup()

    async def write(self, work):
        return await asyncio.wrap_future(submit_write(self.project, work))

    async def sent(self):
        self.item = (await self.write(lambda db: commands.mark_sent(db, self.item.item_id))).value

    async def test_T14_paths_gallery_and_attempt(self):
        self.assertEqual(self.request.save_path, self.root / f'images/.pending/{self.item.item_id}/1/out.png')
        gallery = Path(self.temp.name) / 'gallery.png'
        gallery.write_bytes(png_bytes())
        with self.assertRaises(BackendError):
            stage_result(self.root, self.request, GenerationResult(saved_path=gallery))
        self.assertTrue(gallery.exists())
        with self.assertRaises(BackendError):
            write_receipt(self.root, replace(self.intent, relative_staging_path='../gallery.png'))
        stage_result(self.root, self.request, GenerationResult(image_bytes=png_bytes()))
        with self.assertRaises(BackendError):
            stage_result(self.root, self.request, GenerationResult(image_bytes=png_bytes()))

    async def test_T17_atomic_reject_original_content_and_replay(self):
        await self.sent()
        with patch('stylelora.project_db.receipts.cleanup_receipt', lambda *args: None):
            image = await accept_result(self.project, self.source, self.item, self.request,
                GenerationResult(image_bytes=png_bytes('content, masterpiece')))
        self.assertEqual(image.review_status, 'reject')
        _, artifact, originals = receipts.read_receipt(self.root, self.item.item_id, 1)
        self.assertEqual(originals[0].content, 'content')
        self.assertNotIn('style', originals[0].content)
        await self.write(lambda db: images.set_review(db, image.image_id, expected='reject', target='keep'))
        replay = await self.write(lambda db: commands.complete_item(db, artifact, originals))
        self.assertEqual(replay.value.image_id, image.image_id)
        self.assertEqual(replay.value.review_status, 'keep')
        with read_snapshot(self.project) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM images').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM annotations').fetchone()[0], 1)

    async def test_T18_intent_same_file_recent_empty_unknown(self):
        await self.sent()
        self.request.save_path.write_bytes(png_bytes())
        backend = LocalBackend()
        evidence = await probe(self.project, self.item, backend)
        self.assertEqual(evidence.verdict, 'unknown')
        self.assertEqual(backend.submits, 0)

    async def test_T19_complete_receipt_crash_before_and_after_rename(self):
        await self.sent()
        def crash(*args, **kwargs):
            raise RuntimeError('crash before commit')
        with patch('stylelora.project_db.repositories.annotations.append_version', crash):
            with self.assertRaises(RuntimeError):
                await accept_result(self.project, self.source, self.item, self.request,
                                    GenerationResult(image_bytes=png_bytes()))
        self.assertFalse(self.request.save_path.exists())
        self.assertTrue((self.root / 'images/1.png').exists())
        evidence = await probe(self.project, self.item)
        self.assertEqual(evidence.verdict, 'complete')
        await self.write(lambda db: reconcile_item(db, evidence))
        with read_snapshot(self.project) as db:
            self.assertEqual(sources.get_item(db, self.item.item_id).status, 'done')
            self.assertEqual(db.execute('SELECT count(*) FROM images').fetchone()[0], 1)

    async def test_T19_intent_and_png_windows_no_resubmit(self):
        await self.sent()
        for data in (None, png_bytes()):
            if data:
                self.request.save_path.write_bytes(data)
            backend = LocalBackend()
            evidence = await probe(self.project, self.item, backend)
            self.assertEqual(evidence.verdict, 'unknown')
            self.assertEqual(backend.submits, 0)

    async def test_T19_complete_identity_before_f01_receipt_probe_readonly(self):
        await self.sent()
        with patch('stylelora.generation.artifacts.register_artifact', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                await accept_result(self.project, self.source, self.item, self.request,
                    GenerationResult(image_bytes=png_bytes()))
        path = receipts.receipt_path(self.root, self.item.item_id, 1)
        self.assertFalse(path.exists())
        evidence = await probe(self.project, self.item)
        self.assertEqual(evidence.verdict, 'complete')
        self.assertFalse(path.exists())
        from stylelora.generation.recovery import prepare_reconciliation
        prepare_reconciliation(self.project, self.item, evidence)
        await self.write(lambda db: reconcile_item(db, evidence))
        self.assertTrue((self.root / 'images/1.png').exists())

    async def test_T20_candidate_reused_never_overwrites_other_image(self):
        await self.sent()
        with patch('stylelora.project_db.repositories.annotations.append_version', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                await accept_result(self.project, self.source, self.item, self.request,
                                    GenerationResult(image_bytes=png_bytes()))
        # Preserve the original staged bytes, while the rolled-back candidate is occupied.
        self.request.save_path.write_bytes((self.root / 'images/1.png').read_bytes())
        unrelated = png_bytes('unrelated')
        (self.root / 'images/1.png').write_bytes(unrelated)
        def occupy(db):
            db.execute('INSERT INTO images(image_id,filename,width,height,sha256,file_size,source_id,created_at) VALUES(1,?,?,?,?,?,?,?)',
                ('1.png', 640, 640, 'e' * 64, len(unrelated), self.source.source_id, 'test'))
        await self.write(occupy)
        evidence = await probe(self.project, self.item)
        self.assertEqual(evidence.verdict, 'complete')
        await self.write(lambda db: reconcile_item(db, evidence))
        self.assertEqual((self.root / 'images/1.png').read_bytes(), unrelated)
        with read_snapshot(self.project) as db:
            self.assertEqual(sources.get_item(db, self.item.item_id).image_id, 2)

    async def test_T20_project_move_after_receipt_and_atomic_reject_recovery(self):
        await self.sent()
        with patch('stylelora.project_db.repositories.annotations.append_version', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                await accept_result(self.project, self.source, self.item, self.request,
                                    GenerationResult(image_bytes=png_bytes('content')))
        close_project(self.project)
        moved = Path(self.temp.name) / 'moved.slproj'
        self.root.rename(moved)
        self.root = moved
        self.project = open_project(moved)
        with read_snapshot(self.project) as db:
            item = sources.get_item(db, self.item.item_id)
            self.assertEqual(item.status, 'done')
            self.assertEqual(images.get_image(db, item.image_id).review_status, 'reject')

    async def test_T20_corrupt_receipt_hash_conflict_and_relative_identity(self):
        await self.sent()
        self.request.save_path.write_bytes(png_bytes())
        original_digest = request_digest(self.request, self.root)
        moved_root = Path(self.temp.name) / 'moved'
        moved_request = replace(self.request, save_path=moved_root / self.intent.relative_staging_path)
        self.assertEqual(request_digest(moved_request, moved_root), original_digest)
        path = attempt_path(self.root, self.item.item_id, 1)
        path.write_text('{corrupt', encoding='utf-8')
        self.assertEqual((await probe(self.project, self.item)).verdict, 'unknown')
        path.unlink()
        write_receipt(self.root, replace(self.intent, complete=True, phase='complete', sha256='f' * 64,
            file_size=self.request.save_path.stat().st_size, width=640, height=640))
        self.assertEqual((await probe(self.project, self.item)).verdict, 'unknown')
