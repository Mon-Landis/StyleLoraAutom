import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest
from PIL import Image
from stylelora.vision.models import ModelFile, ModelManifest, ModelStore, manifest_digest
from stylelora.vision.wd import TaggerConfig, WdExecutor, preprocess, select_tags
from stylelora.vision.device import DeviceInfo, probe_device
from stylelora.executors.types import CancelToken, ImageInput, Unavailable


class FakeDownload:
    def __init__(self, files):
        self.files, self.calls = files, []

    async def stream(self, repo, revision, name):
        self.calls.append((repo, revision, name))
        yield self.files[name]


def fixture():
    files = {'model.onnx': b'fake model', 'tags.csv': b'name,category\nBlue_Sky,0\nperson,4\nsafe,9\n'}
    schema = {'size': 2, 'channel_order': 'BGR', 'layout': 'NHWC', 'dtype': 'float32',
              'scale': 1., 'offset': 0., 'batch': 'dynamic', 'memory_per_image': 100,
              'input_name': 'input', 'output_name': 'output',
              'csv_schema': {'name': 'name', 'category': 'category'}}
    manifest = ModelManifest('fake', 'tests/fake', 'a'*40, tuple(ModelFile(name, hashlib.sha256(data).hexdigest(), len(data)) for name, data in files.items()), schema, 'wd-prep-v1', '')
    return replace(manifest, digest=manifest_digest(manifest)), files


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ModelStore(self.root/'models')
        self.manifest, self.files = fixture()

    async def install(self):
        return await self.store.install_model(self.manifest, downloader=FakeDownload(self.files), cancel=CancelToken())

    async def test_T05_verified_install_reentry_and_fixed_commit(self):
        downloader = FakeDownload(self.files)
        first = await self.store.install_model(self.manifest, downloader=downloader, cancel=CancelToken())
        self.assertEqual(first, await self.store.install_model(self.manifest, downloader=downloader, cancel=CancelToken()))
        self.assertEqual(len(downloader.calls), 2)
        self.assertTrue(all(call[1] == 'a'*40 for call in downloader.calls))
        self.assertEqual(self.store.verify_model(self.manifest), first)

    async def test_T05_corrupt_truncated_oversized_and_untrusted(self):
        for data in (b'bad', b'x'*1000):
            files = dict(self.files, **{'model.onnx': data})
            with self.assertRaises(Unavailable):
                await self.store.install_model(self.manifest, downloader=FakeDownload(files), cancel=CancelToken())
            self.assertFalse(self.store.directory(self.manifest).exists())
        bad = replace(self.manifest, revision='main')
        with self.assertRaises(Unavailable):
            self.store.verify_model(bad)
        bad = replace(self.manifest, files=(replace(self.manifest.files[0], sha256=''), self.manifest.files[1]))
        bad = replace(bad, digest=manifest_digest(bad))
        with self.assertRaises(Unavailable):
            self.store.verify_model(bad)

    async def test_T05_escape_cancel_and_ready_preserved(self):
        directory = await self.install()
        bad = replace(self.manifest, files=(replace(self.manifest.files[0], name='../model.onnx'), self.manifest.files[1]))
        bad = replace(bad, digest=manifest_digest(bad))
        with self.assertRaises(Unavailable):
            await self.store.install_model(bad, downloader=FakeDownload(self.files), cancel=CancelToken())
        token = CancelToken()
        token.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.store.install_model(self.manifest, downloader=FakeDownload(self.files), cancel=token)
        self.assertEqual(self.store.verify_model(self.manifest), directory)

    async def test_T07_fake_BRG_NHWC_thresholds_and_order(self):
        await self.install()
        red, blue = self.root/'red.png', self.root/'blue.png'
        Image.new('RGB', (2, 2), 'red').save(red)
        Image.new('RGB', (2, 2), 'blue').save(blue)
        seen = []
        def infer(tensors, provider):
            seen.extend(tensors)
            return [[.35, .85, 1.] for tensor in tensors]
        config = TaggerConfig('onnx', 'fake', 'a'*40, manifest_hash=self.manifest.digest, batch_size=4)
        executor = WdExecutor(config, self.manifest, self.store, infer=infer, device_probe=lambda: DeviceInfo('cpu'))
        inputs = tuple(ImageInput(i, path, 'a'*64, 1, 2, 2) for i, path in enumerate((red, blue), 1))
        result = await executor.predict(inputs, config, CancelToken())
        self.assertEqual(seen[0][0][0], [0, 0, 255])
        self.assertEqual(seen[1][0][0], [255, 0, 0])
        self.assertEqual([tag.tag for tag in result[0].tags], ['blue sky', 'person'])
        self.assertEqual([item.params['actual_batch'] for item in result], [1, 1])

    async def test_T06_OOM_halves_then_cpu_once(self):
        await self.install()
        path = self.root/'red.png'
        Image.new('RGB', (2, 2), 'red').save(path)
        calls = []
        def infer(tensors, provider):
            calls.append((provider, len(tensors)))
            if provider == 'cuda':
                raise MemoryError()
            return [[.5, .9, 1.] for tensor in tensors]
        config = TaggerConfig('onnx', 'fake', 'a'*40, manifest_hash=self.manifest.digest, batch_size=4)
        executor = WdExecutor(config, self.manifest, self.store, infer=infer,
            device_probe=lambda: DeviceInfo('cuda', 8*1024**3, 6*1024**3))
        await executor.predict(tuple(ImageInput(i, path, 'a'*64, 1, 2, 2) for i in range(4)), config, CancelToken())
        self.assertEqual(calls[:4], [('cuda', 4), ('cuda', 2), ('cuda', 1), ('cpu', 1)])

    async def test_T08_unverified_launcher_and_missing_model_unavailable(self):
        config = TaggerConfig('launcher', 'fake', 'a'*40, manifest_hash=self.manifest.digest)
        executor = WdExecutor(config, self.manifest, self.store)
        self.assertEqual(executor.availability(), {'status': 'unavailable', 'reason': 'launcher_unavailable'})
        executor.config = replace(config, backend='onnx')
        self.assertEqual(executor.availability()['reason'], 'model_missing')

    def test_T06_nvml_fallback_smi_and_cpu(self):
        class Broken:
            def nvmlInit(self):
                raise RuntimeError()
        device = probe_device(nvml=Broken(), smi=lambda *args, **kwargs: '8192, 4096', providers=['CUDAExecutionProvider'])
        self.assertEqual((device.provider, device.free_bytes), ('cuda', 4096*1024**2))
        self.assertEqual(probe_device(nvml=Broken(), smi=lambda *args, **kwargs: 'bad', providers=[]).provider, 'cpu')

    def test_T04_normalization_max_conf_rating_excluded(self):
        labels = [{'name': ' Blue_Sky ', 'category': '0'}, {'name': 'blue sky', 'category': '0'}, {'name': 'explicit', 'category': '9'}]
        tags, evidence = select_tags([.5, .9, 1], labels, TaggerConfig('onnx', 'm', 'a'*40))
        self.assertEqual([(tag.tag, tag.conf) for tag in tags], [('blue sky', .9)])
