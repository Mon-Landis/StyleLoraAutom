import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from f02_support import *
from stylelora.generation.profile import *


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
            # Simulate process exit by releasing only the process-local lease registry.
            from stylelora.backends.nai_launcher.adapter import _leases
            _leases.clear()
            replacement = await fake.backend(Path(directory) / 'one')
            with self.assertRaises(BackendError):
                await replacement.apply_profile(PROFILE)
            await replacement.recover_settings()
            self.assertEqual(fake.settings['steps'], 20)
            self.assertFalse(replacement.journal.exists())
            await replacement.close()
