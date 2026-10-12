import unittest
import asyncio
import json
import multiprocessing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from f02_support import *
from stylelora.generation.profile import *


def competing_process(journal, leases, output):
    async def attempt():
        fake = FakeLauncher()
        fake.lease_directory = Path(leases)
        backend = await fake.backend(Path(journal))
        try:
            await backend.apply_profile(PROFILE)
            output.put('acquired')
            await backend.close()
        except BackendError as exc:
            output.put(exc.code)
    asyncio.run(attempt())


class ProfileTests(unittest.TestCase):
    def test_T07_aspect_sizes_snapshot_and_immutable(self):
        self.assertEqual(resolve_aspect('portrait', 'square'), 'portrait')
        self.assertEqual(resolve_aspect(None, 'landscape'), 'landscape')
        self.assertIsNone(resolve_aspect(None, None))
        config = SourceGenerationConfig(PROFILE, SMALL)
        snapshot = to_snapshot(config)
        snapshot['dimensions']['square'][0] = 704
        self.assertEqual(config.sizes['square'].width, 640)
        self.assertEqual(LARGE['square'], PixelSize(1472, 1472))
        for width, height in ((65, 64), (0, 64), (True, 64)):
            with self.assertRaises(BackendError):
                PixelSize(width, height)
        with self.assertRaises(BackendError):
            SourceGenerationConfig(PROFILE, {**SMALL, 'square': PixelSize(128, 64)})
        with self.assertRaises(TypeError):
            config.sizes['square'] = PixelSize(64, 64)
        with TemporaryDirectory() as directory:
            source = SimpleNamespace(style_prompt=' style ', negative_prompt='negative')
            item = SimpleNamespace(aspect=None, item_id=2, attempts=0, prompt=' content ')
            request = build_request(config, source, item, Path(directory))
            self.assertEqual((request.width, request.height), (640, 640))
            self.assertEqual(request.positive, 'style, content')
            self.assertEqual(request.save_path, Path(directory) / 'images/.pending/2/1/out.png')

    def test_T09_validation_extra_references_and_fixed_values(self):
        validate_profile(PROFILE, CAPS)
        for profile in (replace(PROFILE, extra={'unknown': 2}), replace(PROFILE, extra={'steps': 9}),
                        replace(PROFILE, quality_toggle=True), replace(PROFILE, uc_preset=1)):
            with self.assertRaises(BackendError):
                validate_profile(profile, CAPS)
        with TemporaryDirectory() as directory:
            with self.assertRaises(UnsupportedFeature):
                GenerationRequest(item_id=1, attempt=1, positive='', negative='', width=64, height=64,
                    save_path=Path(directory) / 'out.png', references=(ReferenceInput(Path(directory), 1),))


class LeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_journal_writers_concurrent_apply(self):
        with TemporaryDirectory() as directory:
            fake = FakeLauncher()
            first = await fake.backend(Path(directory) / 'one')
            second = await fake.backend(Path(directory) / 'two')
            for _ in range(3):
                results = await asyncio.gather(first.apply_profile(PROFILE), second.apply_profile(PROFILE),
                                               return_exceptions=True)
                self.assertIsInstance(results[0], ProfileHandle)
                self.assertIsInstance(results[1], BackendError)
                self.assertEqual(results[1].code, 'profile:restore_pending')
                self.assertFalse(second.journal.exists())
                await first.restore(results[0])
            await first.close()
            await second.close()

    async def test_cross_process_lease_blocks_independent_writer(self):
        with TemporaryDirectory() as directory:
            fake = FakeLauncher()
            backend = await fake.backend(Path(directory) / 'one')
            await backend.apply_profile(PROFILE)
            context = multiprocessing.get_context('spawn')
            output = context.Queue()
            process = context.Process(target=competing_process,
                args=(str(Path(directory) / 'two'), str(fake.lease_directory), output))
            process.start()
            await asyncio.to_thread(process.join, 20)
            try:
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
                self.assertEqual(output.get(timeout=2), 'profile:restore_pending')
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join()
                output.close()
                await backend.close()

    async def test_journal_digest_fields_and_writable_keys_fail_closed(self):
        with TemporaryDirectory() as directory:
            fake = FakeLauncher()
            backend = await fake.backend(Path(directory))
            handle = await backend.apply_profile(PROFILE)
            original = json.loads(backend.journal.read_text())
            for kind in ('digest', 'extra', 'readonly', 'value'):
                data = json.loads(json.dumps(original))
                if kind == 'digest':
                    data['original']['steps'] = 999
                elif kind == 'extra':
                    data['token'] = 'not-allowed'
                elif kind == 'readonly':
                    data['original']['prompt'] = 'injected'
                    data['applied']['prompt'] = 'page prompt'
                    data['writable_keys'].append('prompt')
                else:
                    data['original']['steps'] = {'nested': 'invalid'}
                if kind != 'digest':
                    data.pop('digest')
                    data['digest'] = digest(data)
                backend.journal.write_text(json.dumps(data))
                writes = fake.counts['update_generation_settings']
                with self.assertRaises(BackendError) as caught:
                    await backend.recover_settings()
                self.assertEqual(caught.exception.code, 'profile:restore_conflict')
                self.assertTrue(backend.session.unknown_write)
                self.assertTrue(backend.lease_path.exists())
                self.assertEqual(fake.counts['update_generation_settings'], writes)
            backend.journal.write_text(json.dumps(original))
            await backend.restore(handle)

    async def test_owner_and_lease_id_checked_before_restore(self):
        with TemporaryDirectory() as directory:
            fake = FakeLauncher()
            backend = await fake.backend(Path(directory))
            handle = await backend.apply_profile(PROFILE)
            original = backend.lease_path.read_text()
            for key in ('owner', 'lease_id'):
                lease = json.loads(original)
                lease[key] = 'another-owner'
                backend.lease_path.write_text(json.dumps(lease))
                with self.assertRaises(BackendError):
                    await backend.restore(handle)
                self.assertEqual(fake.settings['steps'], 14)
                self.assertTrue(backend.journal.exists())
            backend.lease_path.write_text(original)
            await backend.close()

    async def test_generation_actual_whitelist_and_size_bounds(self):
        with TemporaryDirectory() as directory:
            fake = FakeLauncher()
            fake.submit_payload = {'token': 'secret', 'nested': {'payload': 'x' * 10000},
                'seed': 42, 'steps': {'bad': 'value'}, 'sampler': 'x' * 129,
                'scale': 5, 'width': 64, 'height': 64}
            backend = await fake.backend(Path(directory))
            await backend.apply_profile(PROFILE)
            request = GenerationRequest(item_id=1, attempt=1, positive='style', negative='', width=64, height=64,
                                        save_path=Path(directory) / 'out.png')
            result = await backend.submit(await backend.prepare(request))
            self.assertEqual(plain(result.actual), {'seed': 42, 'scale': 5, 'width': 64, 'height': 64})
            self.assertEqual(result.resource_id, 'fake-resource')
            self.assertLess(len(json.dumps(plain(result.actual))), 1024)
            await backend.close()

    async def test_T08_prepare_drift_medium_conflict_and_fixed(self):
        with TemporaryDirectory() as directory:
            fake = FakeLauncher(prepare_drift={'steps': 28})
            backend = await fake.backend(Path(directory))
            handle = await backend.apply_profile(PROFILE)
            request = GenerationRequest(item_id=1, attempt=1, positive='style', negative='', width=64, height=64,
                                        save_path=Path(directory) / 'out.png')
            with self.assertRaises(BackendError) as caught:
                await backend.prepare(request)
            self.assertEqual(caught.exception.code, 'profile:drift')
            self.assertEqual(fake.counts['submit_generation'], 0)
            self.assertEqual(fake.counts['cancel_generation_preparation'], 1)
            self.assertEqual(fake.settings['prompt'], 'page prompt')
            prepare = next(c for c in fake.calls if c.get('params', {}).get('name') == 'prepare_generation')
            self.assertEqual(prepare['params']['arguments']['characters'], [])
            await backend.restore(handle)
            self.assertEqual(fake.settings['steps'], 20)
            fake.settings['steps'] = 30
            handle = await backend.apply_profile(PROFILE)
            self.assertEqual(handle.original['steps'], 30)
            await backend.restore(handle)
            handle = await backend.apply_profile(replace(PROFILE, effort='medium'))
            with self.assertRaises(BackendError):
                await backend.prepare(replace(request, negative='custom'))
            await backend.close()

    async def test_T10_unknown_apply_restore_and_external_conflict(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake = FakeLauncher()
            backend = await fake.backend(root)
            fake.update_fault = 'lost'
            with self.assertRaises(BackendError):
                await backend.apply_profile(PROFILE)
            self.assertTrue(backend.journal.exists())
            self.assertIsNotNone(backend.handle)
            fake.update_fault = 'lost restore'
            await backend.restore(backend.handle)
            self.assertEqual(fake.counts['update_generation_settings'], 2)
            self.assertEqual(fake.settings['steps'], 20)
            handle = await backend.apply_profile(PROFILE)
            fake.settings['steps'] = 25
            with self.assertRaises(BackendError) as caught:
                await backend.restore(handle)
            self.assertEqual(caught.exception.code, 'profile:restore_conflict')
            self.assertEqual(fake.settings['steps'], 25)
            fake.settings['steps'] = 14
            await backend.close()

    async def test_T10_two_sources_and_restart_journal(self):
        with TemporaryDirectory() as directory:
            fake = FakeLauncher()
            backend = await fake.backend(Path(directory) / 'one')
            second = await fake.backend(Path(directory) / 'two')
            handle = await backend.apply_profile(PROFILE)
            with self.assertRaises(BackendError):
                await second.apply_profile(PROFILE)
            await backend.close()
            await second.apply_profile(PROFILE)
            await second.close()
            await backend.apply_profile(PROFILE)
            # Process exit: recovery may reclaim an expired durable lease only.
            import json
            lease = json.loads(backend.lease_path.read_text())
            lease['expires_at'] = 0
            backend.lease_path.write_text(json.dumps(lease))
            replacement = await fake.backend(Path(directory) / 'one')
            with self.assertRaises(BackendError):
                await replacement.apply_profile(PROFILE)
            await replacement.recover_settings()
            self.assertEqual(fake.settings['steps'], 20)
            self.assertFalse(replacement.journal.exists())
            await replacement.close()
