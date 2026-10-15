import asyncio
import csv
from dataclasses import asdict, dataclass
import math
import time
from PIL import Image, ImageOps
from stylelora.executors.cache import cache_keys
from stylelora.executors.types import ExecutionResult, Unavailable
from stylelora.project_db.types import AnnotationInput, Tag
from stylelora.tagging.normalization import normalize_tag
from .device import DeviceInfo, probe_device, plan_batch
from stylelora.resources.compute import ComputeDemand, shared_compute, run_worker


@dataclass(frozen=True)
class TaggerConfig:
    backend: str
    model: str
    revision: str
    general_threshold: float = .35
    character_threshold: float = .85
    batch_size: int = 1
    manifest_hash: str = ''

    def __post_init__(self):
        for value in (self.general_threshold, self.character_threshold):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError('threshold_invalid')
        if type(self.batch_size) is not int or self.batch_size < 1:
            raise ValueError('batch_invalid')


@dataclass(frozen=True)
class TaggerResult:
    tags: tuple[Tag, ...]
    actual_model: str
    params: dict


def preprocess(path, schema):
    required = ('size', 'channel_order', 'layout', 'dtype', 'scale', 'offset')
    if any(key not in schema for key in required) or schema['channel_order'] not in ('RGB', 'BGR') or schema['layout'] not in ('NHWC', 'NCHW') or schema['dtype'] != 'float32':
        raise Unavailable('manifest_invalid')
    size = schema['size']
    if type(size) is not int or not 1 <= size <= 4096:
        raise Unavailable('manifest_invalid')
    with Image.open(path) as source:
        rgba = ImageOps.exif_transpose(source).convert('RGBA')
        rgb = Image.alpha_composite(Image.new('RGBA', rgba.size, 'white'), rgba).convert('RGB')
        side = max(rgb.size)
        square = Image.new('RGB', (side, side), 'white')
        square.paste(rgb, ((side-rgb.width)//2, (side-rgb.height)//2))
        square = square.resize((size, size), Image.Resampling.BICUBIC)
        pixels = [[(channel*schema['scale']+schema['offset']) for channel in
                   (pixel[::-1] if schema['channel_order'] == 'BGR' else pixel)] for pixel in square.getdata()]
    if schema['layout'] == 'NHWC':
        return [pixels[y*size:(y+1)*size] for y in range(size)]
    return [[[pixels[y*size+x][channel] for x in range(size)] for y in range(size)] for channel in range(3)]


def select_tags(scores, labels, config):
    if len(scores) != len(labels):
        raise ValueError('output_schema')
    tags, evidence = {}, []
    for score, label in zip(scores, labels):
        score = float(score)
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError('output_schema')
        category = str(label.get('category', label.get('type', 'general'))).lower()
        category = {'general': '0', 'character': '4', 'rating': '9', '0': '0', '4': '4', '9': '9'}.get(category, category)
        if category not in ('0', '4', '9'):
            raise ValueError('label_schema')
        if category == '9':
            continue
        threshold = config.character_threshold if category == '4' else config.general_threshold
        if score >= threshold:
            name = normalize_tag(label['name'])
            if not name:
                raise ValueError('label_schema')
            tags[name] = max(tags.get(name, 0), score)
            evidence.append({'raw': label['name'], 'category': category, 'tag': name})
    return tuple(Tag(name, score) for name, score in tags.items()), evidence


class WdExecutor:
    name = 'wd'

    def __init__(self, config, manifest, store, *, infer=None, device_probe=probe_device, compute=None):
        self.config, self.manifest, self.store = config, manifest, store
        self.infer, self.device_probe = infer, device_probe
        self._worker = asyncio.Lock()
        self._sessions = {}
        self.compute = compute
        self._registered = set()

    def availability(self):
        try:
            if self.config.backend != 'onnx':
                raise Unavailable('launcher_unavailable')
            if (self.config.model, self.config.revision, self.config.manifest_hash) != (
                    self.manifest.model_id, self.manifest.revision, self.manifest.digest):
                raise Unavailable('manifest_invalid')
            self.store.verify_model(self.manifest)
            if self.infer is None:
                try:
                    import numpy
                    import onnxruntime
                except ImportError as exc:
                    raise Unavailable('dependencies_missing') from exc
            return {'status': 'available', 'reason': None}
        except Unavailable as exc:
            return {'status': 'unavailable', 'reason': exc.reason}

    def _infer(self, tensors, provider):
        if self.infer is not None:
            return self.infer(tensors, provider)
        import numpy as np
        import onnxruntime as ort
        if provider not in self._sessions:
            directory = self.store.verify_model(self.manifest)
            model = next(f.name for f in self.manifest.files if f.name.endswith('.onnx'))
            self._sessions[provider] = ort.InferenceSession(str(directory/model), providers=[
                'CUDAExecutionProvider' if provider == 'cuda' else 'CPUExecutionProvider'])
        session = self._sessions[provider]
        schema = self.manifest.tensor_schema
        inputs, outputs = session.get_inputs(), session.get_outputs()
        if (len(inputs) != 1 or len(outputs) != 1 or inputs[0].name != schema.get('input_name')
                or outputs[0].name != schema.get('output_name') or inputs[0].type != 'tensor(float)'):
            raise Unavailable('tensor_schema_mismatch')
        if provider == 'cuda' and 'CUDAExecutionProvider' not in session.get_providers():
            raise Unavailable('cuda_unavailable')
        return session.run([schema['output_name']], {schema['input_name']: np.asarray(tensors, dtype=np.float32)})[0]

    async def validate(self, snapshot):
        if snapshot['model'] != self.config.model or snapshot['params'].get('tagger') != asdict(self.config):
            raise Unavailable('config_mismatch')
        if snapshot['params'].get('preprocess_version') != self.manifest.preprocess_version:
            raise Unavailable('config_mismatch')

    async def predict(self, images, config, cancel):
        if config != self.config:
            raise Unavailable('config_mismatch')
        availability = self.availability()
        if availability['status'] != 'available':
            raise Unavailable(availability['reason'])
        directory = self.store.verify_model(self.manifest)
        label_file = next(f.name for f in self.manifest.files if f.name.endswith('.csv'))
        with (directory/label_file).open(encoding='utf-8-sig', newline='') as stream:
            labels = list(csv.DictReader(stream))
        result = []
        compute = self.compute or shared_compute()
        async with self._worker:
            remaining = list(images)
            while remaining:
                cancel.raise_if_cancelled()
                device = self.device_probe()
                batch = plan_batch(device, self.manifest, config.batch_size)
                if self.manifest.tensor_schema.get('batch', 1) != 'dynamic':
                    batch = 1
                provider = device.provider
                if (not device.total_bytes or device.free_bytes is None or
                        not self.manifest.tensor_schema.get('memory_per_image') or
                        'resident_bytes' not in self.manifest.tensor_schema):
                    provider, batch = 'cpu', 1
                batch = max(1, batch)
                while True:
                    cancel.raise_if_cancelled()
                    demand = ComputeDemand(str(id(self)), 'gpu:0' if provider == 'cuda' else 'cpu', 'onnx',
                        self.manifest.digest, self.manifest.tensor_schema.get('resident_bytes', 0),
                        self.manifest.tensor_schema.get('memory_per_image', 0), min(batch, len(remaining)))
                    # Device information is observed by the shared controller at admission.
                    if self.compute is None:
                        compute.probe = self.device_probe
                    lease = await compute.acquire(demand, cancel=cancel, deadline=time.monotonic()+300)
                    selected = remaining[:lease.actual_batch]
                    batch = lease.actual_batch
                    try:
                        tensors = await run_worker(lambda: [preprocess(i.path, self.manifest.tensor_schema) for i in selected])
                        cancel.raise_if_cancelled()
                        scores = await run_worker(lambda: self._infer(tensors, provider))
                        break
                    except Exception as exc:
                        # Exception text stays local; only fixed diagnostics leave.
                        oom = isinstance(exc, MemoryError) or 'out of memory' in str(exc).lower()
                        cancel.raise_if_cancelled()
                        if provider == 'cuda' and oom and batch > 1:
                            batch = max(1, batch//2)
                        elif provider == 'cuda':
                            provider, batch = 'cpu', 1
                        else:
                            raise Unavailable('oom' if oom else 'inference_failed') from exc
                    finally:
                        await lease.release()
                        original = lease.provider
                        if original != provider and original in self._sessions:
                            del self._sessions[original]
                            if original in self._registered:
                                compute.release_resident(self.manifest.digest, demand.device_id)
                                self._registered.remove(original)
                        elif original in self._sessions and original not in self._registered:
                            compute.register_resident(self.manifest.digest, demand.device_id, demand.resident_bytes)
                            self._registered.add(original)
                cancel.raise_if_cancelled()
                if len(scores) != len(selected):
                    raise ValueError('output_schema')
                for row in scores:
                    tags, mapping = select_tags(row, labels, config)
                    params = {'provider': provider, 'actual_batch': len(selected),
                              'preprocess_version': self.manifest.preprocess_version,
                              'manifest_hash': self.manifest.digest, 'tag_mapping': mapping,
                              'revision': config.revision, 'backend': config.backend,
                              'general_threshold': config.general_threshold,
                              'character_threshold': config.character_threshold}
                    result.append(TaggerResult(tags, config.model, params))
                del remaining[:len(selected)]
        return tuple(result)

    async def unload(self):
        async with self._worker:
            self._sessions.clear()
            compute = self.compute or shared_compute()
            for provider in self._registered:
                compute.release_resident(self.manifest.digest, 'gpu:0' if provider == 'cuda' else 'cpu')
            self._registered.clear()

    async def execute(self, context):
        try:
            result = (await self.predict((context.image,), self.config, context.cancel))[0]
        except Unavailable as exc:
            if exc.reason in ('oom', 'inference_failed', 'tensor_schema_mismatch'):
                return ExecutionResult('failed', exc.reason)
            raise
        keys = cache_keys(context, actual_model=result.actual_model, actual_params=result.params)
        params = {'schema_version': 1, 'requested': asdict(self.config), 'actual': result.params}
        return ExecutionResult('done', annotations=(AnnotationInput(context.image.image_id, 'wd', 'tags',
            result.tags, model_name=result.actual_model, params=params, cache_key=keys['wd', 'tags']),))
